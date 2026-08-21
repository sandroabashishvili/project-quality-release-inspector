from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from .checks_common import _finding
from .models import Finding, Project


def _rel_values(node) -> set[str]:
    value = node.get("rel", [])
    if isinstance(value, str):
        value = value.split()
    return {str(item).lower() for item in value}


def _local_asset(project: Project, page: Path, raw: str) -> Path | None:
    if not raw or raw.startswith("data:"):
        return None
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme != "https":
        return None
    if parsed.netloc:
        if not project.live_url or parsed.netloc != urlparse(project.live_url).netloc:
            return None
        clean = parsed.path
    else:
        clean = parsed.path
    clean = unquote(clean)
    live_prefix = urlparse(project.live_url).path.strip("/") if project.live_url else ""
    if clean.startswith("/"):
        clean = clean.lstrip("/")
        if live_prefix and (clean == live_prefix or clean.startswith(live_prefix + "/")):
            clean = clean[len(live_prefix):].lstrip("/")
        return (project.path / clean).resolve()
    return (page.parent / clean).resolve()


def _icon_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    suffix = path.suffix.lower()
    if suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if suffix == ".ico" and len(data) >= 8 and data[:4] == b"\x00\x00\x01\x00":
        width, height = data[6], data[7]
        return (256 if width == 0 else width, 256 if height == 0 else height)
    if suffix == ".svg":
        text = data.decode("utf-8", errors="ignore")
        viewbox = re.search(r"viewBox\s*=\s*['\"]\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)", text, re.I)
        if viewbox:
            return int(float(viewbox.group(1))), int(float(viewbox.group(2)))
        width = re.search(r"\bwidth\s*=\s*['\"]([\d.]+)", text, re.I)
        height = re.search(r"\bheight\s*=\s*['\"]([\d.]+)", text, re.I)
        if width and height:
            return int(float(width.group(1))), int(float(height.group(1)))
    return None


def _all_frontend_text(root: Path) -> str:
    chunks: list[str] = []
    for suffix in ("*.css", "*.js", "*.html"):
        for path in root.rglob(suffix):
            if any(part in {".git", ".venv", "node_modules"} for part in path.parts):
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    return "\n".join(chunks)


def _normalized_url(value: str) -> str:
    parsed = urlparse(value.strip())
    path = parsed.path or "/"
    if not Path(path).suffix and not path.endswith("/"):
        path += "/"
    return parsed._replace(path=path, params="", query="", fragment="").geturl()


def _theme_findings(project: Project, frontend: str) -> list[Finding]:
    lower = frontend.lower()
    policy = project.theme_policy.strip().lower()
    automatic = bool(
        re.search(r"@media\s*\(\s*prefers-color-scheme\s*:\s*(?:dark|light)\s*\)", lower)
        or re.search(r"matchmedia\s*\(\s*['\"]\(prefers-color-scheme\s*:\s*(?:dark|light)\)['\"]\s*\)", lower)
    )
    manual_dark = bool(re.search(r"(?:data-theme|dataset\.theme)[^\n]{0,80}dark", lower))
    manual_light = bool(re.search(r"(?:data-theme|dataset\.theme)[^\n]{0,80}light", lower))
    fixed_dark = bool(re.search(r"color-scheme\s*:\s*(?:only\s+)?dark\b", lower))
    fixed_light = bool(re.search(r"color-scheme\s*:\s*(?:only\s+)?light\b", lower))

    if policy == "automatic":
        if not automatic:
            return [_finding(project, "theme", "error", "Automatic theme policy is declared but no prefers-color-scheme rule or system-theme listener exists")]
        return [_finding(project, "theme", "pass", "Automatic light/dark source rule is present; rendered palette is checked separately")]
    if policy == "manual":
        if not (manual_dark and manual_light):
            return [_finding(project, "theme", "error", "Manual theme policy is declared but both light and dark states are not identifiable")]
        return [_finding(project, "theme", "pass", "Manual light/dark state definitions are present")]
    if policy == "fixed-dark":
        if not fixed_dark:
            return [_finding(project, "theme", "error", "Fixed-dark theme policy is declared but CSS does not declare a dark color scheme")]
        return [_finding(project, "theme", "pass", "Site is intentionally configured as fixed dark; a light mode is not required")]
    if policy == "fixed-light":
        if not fixed_light:
            return [_finding(project, "theme", "error", "Fixed-light theme policy is declared but CSS does not declare a light color scheme")]
        return [_finding(project, "theme", "pass", "Site is intentionally configured as fixed light; a dark mode is not required")]

    detected = "automatic" if automatic else "manual" if manual_dark and manual_light else "fixed-dark" if fixed_dark else "fixed-light" if fixed_light else "unknown"
    return [_finding(project, "theme", "warning", "Theme policy is not declared, so theme expectations cannot be verified", details=f"detected={detected}")]


def _manifest_findings(project: Project, index: Path, soup: BeautifulSoup) -> list[Finding]:
    link = next((node for node in soup.find_all("link") if "manifest" in _rel_values(node)), None)
    if not link:
        return [_finding(project, "website-essentials", "warning", "Homepage has no web app manifest")]
    target = _local_asset(project, index, str(link.get("href", "")))
    if target is None or not target.exists():
        return [_finding(project, "website-essentials", "error", "Web app manifest link is not a valid local file")]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [_finding(project, "website-essentials", "error", "Web app manifest is invalid JSON", target, details=str(exc))]

    findings: list[Finding] = []
    missing = [key for key in ("name", "short_name", "start_url", "scope", "display", "icons") if not data.get(key)]
    if missing:
        findings.append(_finding(project, "website-essentials", "error", "Web app manifest is incomplete", target, details=", ".join(missing)))
        return findings

    expected_path = urlparse(project.live_url).path or "/"
    if not expected_path.endswith("/"):
        expected_path += "/"
    bad_paths = [
        f"{key}={data.get(key)!r}; expected {expected_path!r}"
        for key in ("start_url", "scope")
        if data.get(key) != expected_path
    ]
    if bad_paths:
        findings.append(_finding(project, "website-essentials", "error", "Manifest start_url or scope does not match the configured public site", target, details="\n".join(bad_paths)))

    icon_errors: list[str] = []
    declared_sizes: set[str] = set()
    icons = data.get("icons", [])
    if not isinstance(icons, list):
        icons = []
    for icon in icons:
        if not isinstance(icon, dict):
            icon_errors.append("icon entry is not an object")
            continue
        raw = str(icon.get("src", ""))
        declared = str(icon.get("sizes", ""))
        declared_sizes.update(declared.split())
        icon_path = _local_asset(project, target, raw)
        if not raw or icon_path is None or not icon_path.exists():
            icon_errors.append(f"missing icon: {raw or '<empty src>'}")
            continue
        actual = _icon_size(icon_path)
        for value in declared.split():
            match = re.fullmatch(r"(\d+)x(\d+)", value)
            if match and actual and actual != (int(match.group(1)), int(match.group(2))):
                icon_errors.append(f"{raw}: declared {value}, actual {actual[0]}x{actual[1]}")
    for required in ("192x192", "512x512"):
        if required not in declared_sizes:
            icon_errors.append(f"recommended install icon {required} is not declared")
    if icon_errors:
        findings.append(_finding(project, "website-essentials", "error", "Manifest icon contract is invalid", target, details="\n".join(icon_errors)))

    if not findings:
        findings.append(_finding(project, "website-essentials", "pass", "Web app manifest identity, paths and icon files are valid"))
    return findings


def check_website_essentials(project: Project) -> list[Finding]:
    if project.kind != "static":
        return []
    index = project.path / "index.html"
    if not index.exists():
        return [_finding(project, "website-essentials", "error", "Homepage index.html is missing")]
    soup = BeautifulSoup(index.read_text(encoding="utf-8", errors="replace"), "html.parser")
    findings: list[Finding] = []

    icon_links = [node for node in soup.find_all("link") if "icon" in _rel_values(node)]
    if not icon_links:
        findings.append(_finding(project, "website-essentials", "error", "Homepage has no favicon declaration"))
    else:
        missing: list[str] = []
        undersized: list[str] = []
        for node in icon_links:
            raw = str(node.get("href", ""))
            if raw.startswith("data:"):
                continue
            target = _local_asset(project, index, raw)
            if target is None or not target.exists():
                missing.append(raw)
                continue
            size = _icon_size(target)
            if size and min(size) < 8:
                undersized.append(f"{raw}: {size[0]}x{size[1]}")
        if missing:
            findings.append(_finding(project, "website-essentials", "error", "Favicon file is missing", details="\n".join(missing)))
        elif undersized:
            findings.append(_finding(project, "website-essentials", "error", "Favicon is smaller than 8x8", details="\n".join(undersized)))
        else:
            findings.append(_finding(project, "website-essentials", "pass", "Favicon declaration and file are valid"))

    apple = next((node for node in soup.find_all("link") if "apple-touch-icon" in _rel_values(node)), None)
    if not apple:
        findings.append(_finding(project, "website-essentials", "warning", "Homepage has no apple-touch-icon"))
    else:
        target = _local_asset(project, index, str(apple.get("href", "")))
        size = _icon_size(target) if target and target.exists() else None
        if target is None or not target.exists():
            findings.append(_finding(project, "website-essentials", "error", "apple-touch-icon file is missing"))
        elif size and (size[0] < 180 or size[1] < 180):
            findings.append(_finding(project, "website-essentials", "warning", f"apple-touch-icon is only {size[0]}x{size[1]}; 180x180 recommended"))
        else:
            findings.append(_finding(project, "website-essentials", "pass", "apple-touch-icon is available"))

    findings.extend(_manifest_findings(project, index, soup))

    canonical = soup.find("link", attrs={"rel": lambda value: value and "canonical" in value})
    canonical_url = str(canonical.get("href", "")) if canonical else ""
    if not canonical_url.startswith("https://"):
        findings.append(_finding(project, "website-essentials", "error", "Homepage canonical URL is missing or not HTTPS"))
    elif _normalized_url(canonical_url) != _normalized_url(project.live_url):
        findings.append(_finding(project, "website-essentials", "error", "Homepage canonical URL does not match the configured public site", details=f"canonical={canonical_url}\nconfigured={project.live_url}"))
    else:
        findings.append(_finding(project, "website-essentials", "pass", "Homepage canonical URL matches the configured HTTPS site"))

    og_image = soup.find("meta", attrs={"property": "og:image"})
    twitter_image = soup.find("meta", attrs={"name": "twitter:image"})
    sharing_urls = [str(node.get("content", "")) for node in (og_image, twitter_image) if node]
    if len(sharing_urls) != 2 or any(not url.startswith("https://") for url in sharing_urls):
        findings.append(_finding(project, "website-essentials", "error", "Explicit HTTPS og:image and twitter:image are required"))
    else:
        missing = []
        for raw in sharing_urls:
            target = _local_asset(project, index, raw)
            if target is not None and not target.exists():
                missing.append(raw)
        if len(set(sharing_urls)) != 1:
            findings.append(_finding(project, "website-essentials", "warning", "Open Graph and Twitter use different sharing images", details="\n".join(sharing_urls)))
        elif missing:
            findings.append(_finding(project, "website-essentials", "error", "Sharing preview image file is missing", details="\n".join(missing)))
        else:
            width = soup.find("meta", attrs={"property": "og:image:width"})
            height = soup.find("meta", attrs={"property": "og:image:height"})
            if not width or not height:
                findings.append(_finding(project, "website-essentials", "warning", "Sharing preview has no explicit width and height"))
            else:
                findings.append(_finding(project, "website-essentials", "pass", "Sharing preview metadata and image are explicit"))

    robots, sitemap, page_404 = (project.path / name for name in ("robots.txt", "sitemap.xml", "404.html"))
    missing_core = [path.name for path in (robots, sitemap, page_404) if not path.exists()]
    if missing_core:
        findings.append(_finding(project, "website-essentials", "warning", "Website discovery/fallback files are incomplete", details=", ".join(missing_core)))
    else:
        robots_text = robots.read_text(encoding="utf-8", errors="ignore")
        sitemap_urls = re.findall(r"^\s*Sitemap\s*:\s*(\S+)", robots_text, re.I | re.M)
        live = urlparse(project.live_url)
        live_prefix = live.path if live.path.endswith("/") else live.path + "/"
        valid_sitemaps = []
        for raw in sitemap_urls:
            parsed = urlparse(raw)
            same_public_location = parsed.scheme == "https" and parsed.netloc == live.netloc and parsed.path.startswith(live_prefix)
            target = _local_asset(project, robots, raw)
            if same_public_location and target is not None and target.exists() and target.suffix.lower() == ".xml":
                valid_sitemaps.append(raw)
        if not sitemap_urls:
            findings.append(_finding(project, "website-essentials", "error", "robots.txt does not reference a sitemap"))
        elif not valid_sitemaps:
            findings.append(_finding(project, "website-essentials", "error", "robots.txt sitemap reference does not resolve to an XML file in this public site", details=f"site={project.live_url}\nfound={' '.join(sitemap_urls)}"))
        else:
            findings.append(_finding(project, "website-essentials", "pass", "robots.txt references a local sitemap or sitemap index for this public site; sitemap.xml and 404.html are present"))

    if not project.live_url.startswith("https://"):
        findings.append(_finding(project, "website-essentials", "error", "Configured public site URL is not HTTPS"))
    else:
        findings.append(_finding(project, "website-essentials", "pass", "Configured public site uses HTTPS"))

    json_ld = soup.find_all("script", attrs={"type": "application/ld+json"})
    if not json_ld:
        findings.append(_finding(project, "website-essentials", "warning", "Homepage has no JSON-LD structured data"))
    else:
        invalid = []
        for node in json_ld:
            try:
                payload = json.loads(node.string or node.get_text())
                items = payload if isinstance(payload, list) else [payload]
                for item in items:
                    if not isinstance(item, dict) or not item.get("@context"):
                        invalid.append("Top-level JSON-LD entries require @context")
                        continue
                    graph = item.get("@graph")
                    has_type = bool(item.get("@type"))
                    valid_graph = isinstance(graph, list) and bool(graph) and all(isinstance(entry, dict) and entry.get("@type") for entry in graph)
                    if not has_type and not valid_graph:
                        invalid.append("JSON-LD requires @type or a non-empty @graph whose entries have @type")
            except (json.JSONDecodeError, TypeError) as exc:
                invalid.append(str(exc))
        findings.append(_finding(project, "website-essentials", "error" if invalid else "pass", "Homepage JSON-LD is invalid" if invalid else "Homepage JSON-LD is valid", details="\n".join(invalid)))

    frontend = _all_frontend_text(project.path)
    analytics_present = bool(re.search(r"G-[A-Z0-9]{6,}|googletagmanager\.com/gtag", frontend))
    consent_present = "consent" in frontend.lower() and ("localstorage" in frontend.lower() or "cookie" in frontend.lower())
    if analytics_present and not consent_present:
        findings.append(_finding(project, "website-essentials", "error", "Analytics is present without an identifiable consent gate"))
    else:
        findings.append(_finding(project, "website-essentials", "info" if analytics_present else "pass", "Analytics consent implementation markers are present; legal and behavioral consent still require browser review" if analytics_present else "No analytics integration requires consent"))

    nav_present = bool(soup.find("nav") or soup.select_one("[data-mobile-menu-button], #site-header"))
    footer_present = bool(soup.find("footer") or soup.select_one("#site-footer"))
    contact_present = bool(soup.find("a", href=re.compile(r"^(mailto:|tel:)|linkedin\.com|/contact|kontakt", re.I)))
    missing_landmarks = [name for name, present in (("navigation", nav_present), ("footer", footer_present), ("contact path", contact_present)) if not present]
    if missing_landmarks:
        findings.append(_finding(project, "website-essentials", "warning", "Homepage product landmarks are incomplete", details=", ".join(missing_landmarks)))
    else:
        findings.append(_finding(project, "website-essentials", "pass", "Navigation, footer and contact path are discoverable"))

    findings.extend(_theme_findings(project, frontend))
    lower = frontend.lower()
    print_present = "@media print" in lower
    reduced_present = "prefers-reduced-motion" in lower
    findings.append(_finding(project, "website-essentials", "pass" if print_present else "warning", "Print styles are present" if print_present else "Print styles are missing"))
    findings.append(_finding(project, "website-essentials", "pass" if reduced_present else "warning", "Reduced-motion support is present" if reduced_present else "Reduced-motion support is missing"))
    return findings
