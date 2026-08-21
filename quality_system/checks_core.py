from __future__ import annotations

import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .checks_common import SECRET_PATTERNS, TEXT_SUFFIXES, _finding, _iter_files
from .models import Finding, Project
from .process import run_command, trim_output

def check_inventory(project: Project) -> list[Finding]:
    findings: list[Finding] = []
    if not project.path.is_dir():
        return [_finding(project, "inventory", "error", "Project directory is missing", project.path)]

    for required in project.required_files:
        target = project.path / required
        if not target.exists():
            findings.append(_finding(project, "inventory", "error", f"Required file is missing: {required}", target))

    large_files = []
    for path in _iter_files(project.path, ignored_patterns=project.ignored_files):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 5 * 1024 * 1024:
            large_files.append(f"{path.relative_to(project.path)} ({size / 1024 / 1024:.1f} MB)")
    if large_files:
        findings.append(
            _finding(
                project,
                "inventory",
                "warning",
                f"{len(large_files)} unusually large project file(s)",
                details="\n".join(large_files[:20]),
            )
        )
    else:
        findings.append(_finding(project, "inventory", "pass", "Required files and file sizes look reasonable"))
    return findings


def check_git_hygiene(project: Project) -> list[Finding]:
    git_root = project.source_path or project.path
    if not (git_root / ".git").is_dir():
        if project.kind == "docs":
            return [_finding(project, "git", "info", "No Git repository; treated as local documentation")]
        return [_finding(project, "git", "warning", "No Git repository found", git_root)]

    findings: list[Finding] = []
    status = run_command(["git", "status", "--porcelain"], cwd=git_root)
    if status.returncode != 0:
        return [_finding(project, "git", "error", "git status failed", details=trim_output(status.stdout))]
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if changed:
        findings.append(
            _finding(
                project,
                "git",
                "warning",
                f"Working tree has {len(changed)} uncommitted item(s)",
                details="\n".join(changed[:30]),
            )
        )
    else:
        findings.append(_finding(project, "git", "pass", "Git working tree is clean"))

    tracked = run_command(["git", "ls-files", "-z"], cwd=git_root)
    tracked_paths = [item for item in tracked.stdout.split("\0") if item]
    forbidden = []
    patterns = (
        re.compile(r"(^|/)\.env$"),
        re.compile(r"(^|/)\.venv/"),
        re.compile(r"(^|/)node_modules/"),
        re.compile(r"(^|/)__pycache__/"),
        re.compile(r"\.(?:db|sqlite|sqlite3|log|pyc)$", re.I),
    )
    for item in tracked_paths:
        if any(pattern.search(item) for pattern in patterns):
            forbidden.append(item)
    if forbidden:
        findings.append(
            _finding(
                project,
                "git",
                "error",
                f"Git tracks {len(forbidden)} generated or sensitive file(s)",
                details="\n".join(forbidden[:30]),
            )
        )

    secret_hits = []
    for item in tracked_paths:
        path = git_root / item
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            match = pattern.search(text)
            if match and not any(marker in match.group(0).lower() for marker in ("example", "replace", "change-me", "your-")):
                secret_hits.append(f"{item}: possible {label}")
    if secret_hits:
        findings.append(
            _finding(project, "security", "error", "Possible secret material in tracked files", details="\n".join(secret_hits))
        )
    return findings



def check_live_home(project: Project, offline: bool) -> list[Finding]:
    if offline or not project.live_url:
        return []
    parsed_url = urlparse(project.live_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        return [_finding(project, "live", "error", "Configured live URL must be an absolute HTTPS URL")]
    try:
        request = urllib.request.Request(project.live_url, headers={"User-Agent": "PortfolioQualitySystem/1.0"})
        # The absolute HTTPS URL is validated above before opening it.
        with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            headers = {key.lower(): value for key, value in response.headers.items()}
        if status == 200 and "text/html" in content_type:
            findings = [_finding(project, "live", "pass", "Published homepage responds with HTML (HTTP 200)")]
            findings.append(
                _finding(project, "security-headers", "pass", "Published site enforces HTTPS with Strict-Transport-Security")
                if "strict-transport-security" in headers
                else _finding(project, "security-headers", "warning", "Published site does not return Strict-Transport-Security")
            )
            optional = {
                "content-security-policy": "Content-Security-Policy",
                "x-content-type-options": "X-Content-Type-Options",
                "referrer-policy": "Referrer-Policy",
                "permissions-policy": "Permissions-Policy",
            }
            missing = [label for key, label in optional.items() if key not in headers]
            findings.append(
                _finding(
                    project,
                    "security-headers",
                    "info" if missing else "pass",
                    "Optional browser security headers are controlled by the hosting platform" if missing else "Recommended browser security headers are present",
                    details=", ".join(missing),
                )
            )
            return findings
        return [_finding(project, "live", "warning", f"Published homepage returned HTTP {status} ({content_type})")]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return [_finding(project, "live", "warning", f"Published homepage could not be checked: {exc}")]
