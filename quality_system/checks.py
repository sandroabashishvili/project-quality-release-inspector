from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from .checks_common import _finding, _iter_files
from .checks_core import check_git_hygiene, check_inventory, check_live_home
from .checks_operations import (
    check_automation_files,
    check_external_links,
    check_generator_contracts,
)
from .checks_website import check_website_essentials
from .models import Finding, Project
from .process import run_command, trim_output

def _is_indexable(path: Path, soup: BeautifulSoup) -> bool:
    if path.name == "404.html" or re.fullmatch(r"google[a-z0-9]+\.html", path.name, re.I):
        return False
    if "assets" in path.parts:
        return False
    robots = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    return not robots or "noindex" not in str(robots.get("content", "")).lower()


def _resolve_local_target(project: Project, page: Path, raw_url: str) -> Path | None:
    parsed = urlparse(raw_url)
    if parsed.scheme or parsed.netloc or raw_url.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    clean = unquote(parsed.path)
    if not clean:
        return page
    if clean.startswith("/"):
        clean = clean.lstrip("/")
        live_prefix = urlparse(project.live_url).path.strip("/") if project.live_url else ""
        if live_prefix and (clean == live_prefix or clean.startswith(live_prefix + "/")):
            clean = clean[len(live_prefix) :].lstrip("/")
        target = project.path / clean
    else:
        target = page.parent / clean
    if raw_url.endswith("/") or target.is_dir():
        target = target / "index.html"
    return target.resolve()


def _seo_missing(project: Project, path: Path, soup: BeautifulSoup) -> list[str]:
    missing: list[str] = []
    title = soup.find("title")
    description = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if not title or not title.get_text(strip=True):
        missing.append("title")
    if not description or not str(description.get("content", "")).strip():
        missing.append("description")
    if not soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}):
        missing.append("viewport")
    if not soup.find("link", attrs={"rel": lambda value: value and "canonical" in value}):
        missing.append("canonical")
    h1_count = len(soup.find_all("h1"))
    if h1_count != 1:
        missing.append(f"h1 count={h1_count}")
    if path == project.path / "index.html":
        for prop in ("og:title", "og:description", "og:image", "og:url"):
            if not soup.find("meta", attrs={"property": prop}):
                missing.append(prop)
        if not soup.find("meta", attrs={"name": "twitter:card"}):
            missing.append("twitter:card")
    return missing


def _inspect_html_page(
    project: Project,
    path: Path,
    issues: dict[str, list[str]],
    findings: list[Finding],
) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        findings.append(_finding(project, "html", "error", f"Cannot read HTML: {exc}", path))
        return
    rel = str(path.relative_to(project.path))
    soup = BeautifulSoup(text, "html.parser")
    ids = [str(node.get("id")) for node in soup.select("[id]")]
    repeated = sorted({item for item in ids if ids.count(item) > 1})
    if repeated:
        issues["duplicate_ids"].append(f"{rel}: {', '.join(repeated)}")
    for image in soup.find_all("img"):
        if not image.has_attr("alt"):
            issues["missing_alt"].append(f"{rel}: {image.get('src', '<unknown image>')}")
    for tag, attr in (("a", "href"), ("link", "href"), ("script", "src"), ("img", "src"), ("source", "src")):
        for node in soup.find_all(tag):
            raw = str(node.get(attr, "")).strip()
            if not raw or "{{" in raw or "${" in raw:
                continue
            parsed = urlparse(raw)
            if (
                tag == "a"
                and not parsed.scheme
                and not parsed.netloc
                and parsed.path.lower().endswith(".html")
            ):
                clean_route = parsed.path[:-5].rstrip("/") + "/"
                clean_target = _resolve_local_target(project, path, clean_route)
                issue_key = "avoidable_html_urls" if clean_target is not None and clean_target.exists() else "visible_html_urls"
                issues[issue_key].append(f"{rel}: {raw}")
            target = _resolve_local_target(project, path, raw)
            if target is not None and not target.exists():
                issues["broken_assets"].append(f"{rel}: {raw}")
    if _is_indexable(path, soup):
        missing = _seo_missing(project, path, soup)
        if missing:
            issues["seo_problems"].append(f"{rel}: {', '.join(missing)}")
    if "sandro-abashishvili.sandroabashishvili.chatgpt.site" in text:
        issues["old_domain_hits"].append(rel)
    for forbidden in project.forbidden_strings:
        if forbidden and forbidden in text:
            issues["old_domain_hits"].append(f"{rel}: {forbidden}")
    if "googletagmanager.com/gtag" in text and "consent" not in text.lower():
        issues["analytics_without_consent"].append(rel)


def _html_findings(project: Project, issues: dict[str, list[str]], page_count: int) -> list[Finding]:
    findings: list[Finding] = []
    if issues["broken_assets"]:
        findings.append(_finding(project, "links", "error", f"{len(issues['broken_assets'])} broken local link/asset reference(s)", details="\n".join(issues["broken_assets"][:60])))
    else:
        findings.append(_finding(project, "links", "pass", f"Local links and assets resolve across {page_count} HTML page(s)"))
    mappings = (
        ("duplicate_ids", "html", "error", "Duplicate HTML id values found", 40),
        ("missing_alt", "accessibility", "warning", f"{len(issues['missing_alt'])} image(s) have no alt text", 40),
        ("seo_problems", "seo", "warning", f"{len(issues['seo_problems'])} indexable page(s) have SEO metadata issues", 60),
        ("old_domain_hits", "content", "error", "Deleted chatgpt.site domain is still referenced", 60),
        ("analytics_without_consent", "privacy", "warning", "Google tag appears without an obvious consent integration", 60),
        ("avoidable_html_urls", "clean-urls", "error", "Internal links expose .html even though a clean route exists", 60),
        ("visible_html_urls", "clean-urls", "info", "Explicit .html routes are used where no clean directory route exists", 20),
    )
    for key, check, severity, message, limit in mappings:
        if issues[key]:
            findings.append(_finding(project, check, severity, message, details="\n".join(issues[key][:limit])))
    if not issues["seo_problems"]:
        findings.append(_finding(project, "seo", "pass", "Indexable HTML pages have core metadata"))
    return findings


def check_html(project: Project) -> list[Finding]:
    if project.kind != "static":
        return []
    html_files = [
        path for path in _iter_files(project.path, {".html"}, project.ignored_files)
        if "partials" not in path.parts
    ]
    if not html_files:
        return [_finding(project, "html", "error", "No HTML files found")]
    keys = (
        "broken_assets", "duplicate_ids", "missing_alt", "seo_problems",
        "old_domain_hits", "analytics_without_consent", "avoidable_html_urls", "visible_html_urls",
    )
    issues = {key: [] for key in keys}
    read_findings: list[Finding] = []
    for path in html_files:
        _inspect_html_page(project, path, issues, read_findings)
    return read_findings + _html_findings(project, issues, len(html_files))


def check_sitemap(project: Project) -> list[Finding]:
    if project.kind != "static":
        return []
    sitemap = project.path / "sitemap.xml"
    sitemap_index = project.path / "sitemap_index.xml"
    robots = project.path / "robots.txt"
    findings: list[Finding] = []
    if sitemap.exists():
        text = sitemap.read_text(encoding="utf-8", errors="replace")
        urls = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text)
        if not urls:
            findings.append(_finding(project, "seo", "error", "sitemap.xml contains no URLs", sitemap))
        elif project.live_url and any(not url.startswith(project.live_url) for url in urls):
            findings.append(_finding(project, "seo", "warning", "sitemap.xml contains URL(s) outside the configured live base", sitemap))
        else:
            findings.append(_finding(project, "seo", "pass", f"sitemap.xml contains {len(urls)} URL(s)"))
    if sitemap_index.exists():
        text = sitemap_index.read_text(encoding="utf-8", errors="replace")
        sitemap_urls = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text)
        invalid = [url for url in sitemap_urls if not url.startswith("https://") or not url.endswith("/sitemap.xml")]
        if not sitemap_urls:
            findings.append(_finding(project, "seo", "error", "sitemap_index.xml contains no sitemap URLs", sitemap_index))
        elif invalid:
            findings.append(_finding(project, "seo", "error", "sitemap_index.xml contains invalid sitemap URL(s)", sitemap_index, details="\n".join(invalid)))
        elif len(sitemap_urls) != len(set(sitemap_urls)):
            findings.append(_finding(project, "seo", "error", "sitemap_index.xml contains duplicate sitemap URLs", sitemap_index))
        elif project.live_url and any(not url.startswith(project.live_url) for url in sitemap_urls):
            findings.append(_finding(project, "seo", "warning", "sitemap_index.xml contains sitemap(s) outside the configured live host", sitemap_index))
        else:
            findings.append(_finding(project, "seo", "pass", f"sitemap_index.xml references {len(sitemap_urls)} sitemap(s)"))
    if robots.exists() and project.live_url:
        text = robots.read_text(encoding="utf-8", errors="replace")
        if "sitemap:" not in text.lower():
            findings.append(_finding(project, "seo", "warning", "robots.txt does not advertise the sitemap", robots))
        elif sitemap_index.exists() and f"Sitemap: {project.live_url}sitemap_index.xml" not in text:
            findings.append(_finding(project, "seo", "error", "robots.txt does not advertise sitemap_index.xml", robots))
    return findings


def check_structured_files(project: Project) -> list[Finding]:
    from defusedxml import ElementTree as element_tree

    problems: list[str] = []
    checked = 0
    for path in _iter_files(project.path, {".json", ".xml"}, project.ignored_files):
        try:
            if path.stat().st_size > 10 * 1024 * 1024:
                continue
            if path.suffix.lower() == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            else:
                element_tree.parse(path)
            checked += 1
        except (OSError, UnicodeError, json.JSONDecodeError, element_tree.ParseError) as exc:
            problems.append(f"{path.relative_to(project.path)}: {exc}")
    if problems:
        return [_finding(project, "structured-data", "error", f"{len(problems)} invalid JSON/XML file(s)", details="\n".join(problems[:50]))]
    if checked:
        return [_finding(project, "structured-data", "pass", f"Parsed {checked} JSON/XML file(s)")]
    return []


def _python_roots(project: Project) -> list[Path]:
    return [project.path / item for item in project.python_paths if (project.path / item).exists()]


def _analyze_python_files(project: Project, py_files: list[Path]) -> dict[str, object]:
    analysis: dict[str, object] = {
        "syntax_errors": [], "oversized": [], "complex_functions": [],
        "normalized_functions": defaultdict(list),
    }
    for path in py_files:
        rel = str(path.relative_to(project.path))
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError) as exc:
            analysis["syntax_errors"].append(f"{rel}: {exc}")
            continue
        line_count = source.count("\n") + 1
        if line_count > 500:
            analysis["oversized"].append(f"{rel}: {line_count} lines")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            length = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
            if length > 80:
                analysis["complex_functions"].append(f"{rel}:{node.lineno} {node.name} ({length} lines)")
            if length >= 8:
                clone = ast.FunctionDef(
                    name="_", args=node.args, body=node.body, decorator_list=[],
                    returns=node.returns, type_comment=getattr(node, "type_comment", None),
                    type_params=getattr(node, "type_params", []),
                )
                analysis["normalized_functions"][ast.dump(clone, include_attributes=False)].append(
                    f"{rel}:{node.lineno} {node.name}"
                )
    return analysis


def _analysis_findings(project: Project, py_files: list[Path], analysis: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    syntax_errors = analysis["syntax_errors"]
    findings.append(
        _finding(project, "python", "error", "Python syntax error(s)", details="\n".join(syntax_errors))
        if syntax_errors else _finding(project, "python", "pass", f"Parsed {len(py_files)} Python file(s)")
    )
    if analysis["oversized"]:
        findings.append(_finding(project, "maintainability", "warning", "Python files exceed the 500-line review threshold", details="\n".join(analysis["oversized"])))
    if analysis["complex_functions"]:
        findings.append(_finding(project, "maintainability", "warning", "Python functions exceed the 80-line review threshold", details="\n".join(analysis["complex_functions"][:40])))
    duplicates = [locations for locations in analysis["normalized_functions"].values() if len(locations) > 1]
    if duplicates:
        findings.append(_finding(project, "duplication", "warning", f"{len(duplicates)} exact duplicated Python function body group(s)", details="\n\n".join("\n".join(group) for group in duplicates[:20])))
    return findings


def _ruff_finding(project: Project, system_root: Path, roots: list[Path]) -> Finding:
    ruff = system_root / ".venv" / "bin" / "ruff"
    if not ruff.exists():
        return _finding(project, "ruff", "warning", "Ruff is not installed; run setup")
    result = run_command([str(ruff), "check", *map(str, roots), "--output-format", "concise"], cwd=project.path)
    return _finding(
        project, "ruff", "pass" if result.returncode == 0 else "warning",
        "Ruff found no lint issues" if result.returncode == 0 else "Ruff reported code-quality issues",
        details=trim_output(result.stdout),
    )


def _deep_python_findings(project: Project, system_root: Path, roots: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    bandit = system_root / ".venv" / "bin" / "bandit"
    security_roots = [root for root in roots if root.name not in {"tests", "test"}]
    if bandit.exists() and security_roots:
        result = run_command([str(bandit), "-q", "-ll", "-r", *map(str, security_roots)], cwd=project.path)
        findings.append(_finding(project, "security", "pass" if result.returncode == 0 else "warning", "Bandit found no Python security findings" if result.returncode == 0 else "Bandit reported Python security findings", details=trim_output(result.stdout)))
    radon = system_root / ".venv" / "bin" / "radon"
    if radon.exists():
        result = run_command([str(radon), "cc", "-s", "-a", *map(str, roots)], cwd=project.path)
        difficult = [line.strip() for line in result.stdout.splitlines() if re.search(r"\s-\s+[C-F]\s*\(", line)]
        findings.append(_finding(project, "complexity", "warning" if difficult else "pass", f"Radon found {len(difficult)} item(s) at complexity C or worse" if difficult else "Radon found no complexity C-F items", details="\n".join(difficult[:60])))
    vulture = system_root / ".venv" / "bin" / "vulture"
    if vulture.exists():
        result = run_command([str(vulture), *map(str, roots), "--min-confidence", "90"], cwd=project.path)
        findings.append(_finding(project, "dead-code", "warning" if result.stdout.strip() else "pass", "Vulture reported high-confidence unused code" if result.stdout.strip() else "Vulture found no high-confidence unused code", details=trim_output(result.stdout)))
    return findings


def check_python(project: Project, system_root: Path, full: bool) -> list[Finding]:
    roots = _python_roots(project)
    if not roots:
        return []
    py_files = [path for root in roots for path in _iter_files(root, {".py"}, project.ignored_files)]
    findings = _analysis_findings(project, py_files, _analyze_python_files(project, py_files))
    findings.append(_ruff_finding(project, system_root, roots))
    if full:
        findings.extend(_deep_python_findings(project, system_root, roots))
    return findings


def check_javascript(project: Project) -> list[Finding]:
    js_files = list(_iter_files(project.path, {".js", ".mjs"}, project.ignored_files))
    if not js_files:
        return []
    failures = []
    for path in js_files:
        result = run_command(["node", "--check", str(path)], cwd=project.path)
        if result.returncode != 0:
            failures.append(f"{path.relative_to(project.path)}\n{trim_output(result.stdout, 500)}")
    if failures:
        return [_finding(project, "javascript", "error", "JavaScript syntax error(s)", details="\n\n".join(failures))]
    return [_finding(project, "javascript", "pass", f"JavaScript syntax valid in {len(js_files)} file(s)")]


def check_project_commands(project: Project) -> list[Finding]:
    findings: list[Finding] = []
    for command in project.project_commands:
        result = run_command(command, cwd=project.path)
        findings.append(
            _finding(
                project,
                "project-test",
                "pass" if result.returncode == 0 else "error",
                f"Project command {' '.join(command)} {'passed' if result.returncode == 0 else 'failed'}",
                details=trim_output(result.stdout),
            )
        )
    if project.test_command:
        executable = project.path / project.test_command[0]
        command = [str(executable), *project.test_command[1:]] if executable.exists() else project.test_command
        result = run_command(command, cwd=project.path, timeout=240)
        findings.append(
            _finding(
                project,
                "tests",
                "pass" if result.returncode == 0 else "error",
                "Application tests passed" if result.returncode == 0 else "Application tests failed",
                details=trim_output(result.stdout),
            )
        )
    return findings


def check_dependencies(project: Project, system_root: Path, offline: bool, full: bool) -> list[Finding]:
    if not full:
        return []
    requirements = project.path / "requirements.txt"
    if not requirements.exists():
        return []
    if offline:
        return [_finding(project, "dependencies", "info", "Dependency vulnerability audit skipped in offline mode")]
    audit = system_root / ".venv" / "bin" / "pip-audit"
    if not audit.exists():
        return [_finding(project, "dependencies", "warning", "pip-audit is not installed; run setup")]
    result = run_command([str(audit), "-r", str(requirements), "--progress-spinner", "off"], cwd=project.path, timeout=240)
    if result.returncode == 0:
        return [_finding(project, "dependencies", "pass", "No known Python dependency vulnerabilities")]
    output = trim_output(result.stdout)
    if "known vulnerabilit" in output.lower():
        return [_finding(project, "dependencies", "error", "Known Python dependency vulnerabilities found", details=output)]
    return [_finding(project, "dependencies", "warning", "Python dependency audit could not complete", details=output)]



def _enabled(project: Project, check: str, default: bool = True) -> bool:
    return project.checks.get(check, default)


def run_static_checks(project: Project, system_root: Path, mode: str, offline: bool) -> list[Finding]:
    full = mode == "full"
    findings: list[Finding] = []
    if _enabled(project, "inventory"):
        findings.extend(check_inventory(project))
    if _enabled(project, "git"):
        findings.extend(check_git_hygiene(project))
    if _enabled(project, "html"):
        findings.extend(check_html(project))
    if _enabled(project, "seo"):
        findings.extend(check_sitemap(project))
    if _enabled(project, "website_essentials"):
        findings.extend(check_website_essentials(project))
    if _enabled(project, "structured_data"):
        findings.extend(check_structured_files(project))
    if _enabled(project, "python"):
        findings.extend(check_python(project, system_root, full))
    if _enabled(project, "javascript"):
        findings.extend(check_javascript(project))
    if _enabled(project, "project_commands"):
        findings.extend(check_project_commands(project))
    if _enabled(project, "dependencies"):
        findings.extend(check_dependencies(project, system_root, offline, full))
    if _enabled(project, "live"):
        findings.extend(check_live_home(project, offline))
    if _enabled(project, "automation", False):
        findings.extend(check_automation_files(project))
    if _enabled(project, "generator_contract", False):
        findings.extend(check_generator_contracts(project))
    if _enabled(project, "external_links", False):
        findings.extend(check_external_links(project, offline=offline, full=full))
    return findings
