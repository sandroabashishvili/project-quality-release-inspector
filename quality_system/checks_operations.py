from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from bs4 import BeautifulSoup

from .models import Finding, Project
from .process import run_command, trim_output


DEFAULT_FORBIDDEN_DOMAINS = (
    "sandro-abashishvili.sandroabashishvili.chatgpt.site",
)


def _finding(project: str, check: str, severity: str, message: str, *, path: str = "", details: str = "") -> Finding:
    return Finding(project, check, severity, message, path=path, details=details)


def _workflow_files(project: Project) -> list[Path]:
    workflow_root = project.path / ".github" / "workflows"
    return sorted(path for path in workflow_root.glob("*.y*ml") if path.is_file()) if workflow_root.exists() else []


def check_automation_files(project: Project) -> list[Finding]:
    configured = [project.path / item for item in project.automation_paths]
    paths = [path for path in configured if path.is_file()] + _workflow_files(project)
    if not paths:
        return []
    forbidden = tuple(dict.fromkeys((*DEFAULT_FORBIDDEN_DOMAINS, *project.forbidden_strings)))
    forbidden_hits: list[str] = []
    workflow_errors: list[str] = []
    unpinned_actions: list[str] = []
    for path in paths:
        try:
            relative = str(path.relative_to(project.path))
        except ValueError:
            relative = str(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        for value in forbidden:
            if value and value in text:
                forbidden_hits.append(f"{relative}: {value}")
        if path.suffix.lower() not in {".yml", ".yaml"}:
            continue
        try:
            payload = yaml.safe_load(text)
            if not isinstance(payload, dict):
                workflow_errors.append(f"{relative}: workflow root must be a mapping")
                continue
        except yaml.YAMLError as exc:
            workflow_errors.append(f"{relative}: {exc}")
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = re.search(r"\buses:\s*['\"]?([^\s'\"]+)", line)
            if not match:
                continue
            action = match.group(1)
            if action.startswith(("./", "docker://")):
                continue
            if "@" not in action:
                unpinned_actions.append(f"{relative}:{line_number}: {action}")
    findings: list[Finding] = []
    if forbidden_hits:
        findings.append(_finding(project.id, "automation", "error", "Automation files contain a forbidden retired domain", details="\n".join(forbidden_hits)))
    else:
        findings.append(_finding(project.id, "automation", "pass", f"{len(paths)} automation/workflow file(s) contain no retired-domain override"))
    if workflow_errors:
        findings.append(_finding(project.id, "workflow", "error", "GitHub Actions workflow YAML is invalid", details="\n".join(workflow_errors)))
    elif _workflow_files(project):
        findings.append(_finding(project.id, "workflow", "pass", "GitHub Actions workflow YAML parsed successfully"))
    if unpinned_actions:
        findings.append(_finding(project.id, "workflow", "warning", "GitHub Actions use an action without an explicit version", details="\n".join(unpinned_actions)))
    return findings


def check_generator_contracts(project: Project) -> list[Finding]:
    findings: list[Finding] = []
    for contract in project.generator_contracts:
        name = str(contract.get("name") or "generator")
        command = [str(item) for item in contract.get("command", [])]
        if not command:
            findings.append(_finding(project.id, "generator-contract", "error", f"{name}: command is missing"))
            continue
        cwd = Path(str(contract.get("cwd") or project.path)).resolve()
        result = run_command(command, cwd=cwd, timeout=int(contract.get("timeout", 90)))
        output = result.stdout
        expected = [str(item) for item in contract.get("expected_strings", [])]
        forbidden = [str(item) for item in contract.get("forbidden_strings", [])]
        missing = [item for item in expected if item not in output]
        returned = [item for item in forbidden if item and item in output]
        if result.returncode != 0 or missing or returned:
            evidence = []
            if result.returncode != 0:
                evidence.append(f"exit={result.returncode}")
            if missing:
                evidence.append("missing: " + ", ".join(missing))
            if returned:
                evidence.append("forbidden returned: " + ", ".join(returned))
            evidence.append(trim_output(output, 4000))
            findings.append(_finding(project.id, "generator-contract", "error", f"{name}: generated output failed the public-domain contract", details="\n".join(filter(None, evidence))))
        else:
            findings.append(_finding(project.id, "generator-contract", "pass", f"{name}: generated output keeps the approved public domain"))
    return findings


def _external_urls(project: Project) -> list[str]:
    own_host = urlparse(project.live_url).hostname
    urls: set[str] = set()
    for route in project.browser_paths or ["/"]:
        clean = route.strip("/")
        path = project.path / clean
        page = path / "index.html" if not path.suffix else path
        if not page.exists() and route == "/":
            page = project.path / "index.html"
        if not page.exists():
            continue
        soup = BeautifulSoup(page.read_text(encoding="utf-8", errors="replace"), "html.parser")
        for node in soup.select("a[href]"):
            raw = str(node.get("href", "")).strip()
            if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            url = urljoin(project.live_url, raw)
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname == own_host:
                continue
            if parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
                continue
            urls.add(url.split("#", 1)[0])
    return sorted(urls)[: max(1, project.external_link_limit)]


def _probe_url(url: str) -> tuple[str, int | None, str]:
    headers = {"User-Agent": "Sandro-Quality-System/1.0 (+link-check)"}
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:  # nosec B310
                return url, int(response.status), ""
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in {400, 403, 405, 501}:
                continue
            return url, exc.code, str(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if method == "HEAD":
                continue
            return url, None, str(exc)
    return url, None, "no response"


def check_external_links(project: Project, *, offline: bool, full: bool) -> list[Finding]:
    if offline or not full or project.kind != "static" or not project.live_url:
        return []
    urls = _external_urls(project)
    if not urls:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        results = list(pool.map(_probe_url, urls))
    broken = [f"{url}: HTTP {status if status is not None else 'unreachable'} ({error})" for url, status, error in results if status in {404, 410} or status is None]
    restricted = [f"{url}: HTTP {status}" for url, status, _ in results if status in {401, 403, 429}]
    findings: list[Finding] = []
    if broken:
        findings.append(_finding(project.id, "external-links", "warning", f"{len(broken)} external link(s) are broken or unreachable", details="\n".join(broken)))
    else:
        findings.append(_finding(project.id, "external-links", "pass", f"{len(urls)} external link(s) responded without a confirmed 404/410"))
    if restricted:
        findings.append(_finding(project.id, "external-links", "info", "Some external sites block automated verification", details="\n".join(restricted)))
    return findings


def load_operations_config(system_root: Path) -> dict:
    for name in ("operations.local.json", "operations.json"):
        path = system_root / "config" / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def check_local_operations(system_root: Path, *, full: bool) -> list[Finding]:
    if not full:
        return []
    config = load_operations_config(system_root)
    if not config:
        return [_finding("quality-system", "automation", "info", "Local cron/systemd contract is not configured")]
    findings: list[Finding] = []
    cron = subprocess.run(["crontab", "-l"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if cron.returncode != 0:
        findings.append(_finding("quality-system", "cron", "info", "User crontab inventory is unavailable in this environment", details=trim_output(cron.stdout, 1000)))
        cron_text = ""
    else:
        cron_text = cron.stdout
    forbidden = [item for item in config.get("forbidden_strings", []) if item in cron_text]
    missing = [item for item in config.get("required_cron_patterns", []) if not re.search(item, cron_text)] if cron.returncode == 0 else []
    if forbidden:
        findings.append(_finding("quality-system", "cron", "error", "User crontab contains a forbidden retired domain", details="\n".join(forbidden)))
    if missing:
        findings.append(_finding("quality-system", "cron", "error", "Required scheduled workflow is missing from the user crontab", details="\n".join(missing)))
    if cron.returncode == 0 and not forbidden and not missing:
        findings.append(_finding("quality-system", "cron", "pass", f"User crontab matches {len(config.get('required_cron_patterns', []))} required workflow contract(s)"))
    timer = subprocess.run(["systemctl", "--user", "list-timers", "--all", "--no-pager"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if timer.returncode == 0:
        timer_forbidden = [item for item in config.get("forbidden_strings", []) if item in timer.stdout]
        findings.append(_finding("quality-system", "systemd", "error" if timer_forbidden else "pass", "User systemd timers contain a forbidden retired domain" if timer_forbidden else "User systemd timer inventory completed", details="\n".join(timer_forbidden)))
    else:
        findings.append(_finding("quality-system", "systemd", "info", "User systemd timer inventory is unavailable in this environment", details=trim_output(timer.stdout, 1000)))
    return findings
