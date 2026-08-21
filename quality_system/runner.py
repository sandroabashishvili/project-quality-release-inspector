from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .adapters import load_adapter
from .checks import run_static_checks
from .checks_operations import check_local_operations
from .config import SYSTEM_ROOT
from .models import Finding, Project, RunResult
from .process import run_command, trim_output


def _node_binary() -> str:
    bundled = SYSTEM_ROOT / "node_modules" / ".bin" / "node"
    return str(bundled) if bundled.exists() else "node"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return




class PrefixHandler(QuietHandler):
    def __init__(self, *args, prefix: str = "", **kwargs):
        self.url_prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""
        super().__init__(*args, **kwargs)

    def translate_path(self, request_path: str) -> str:
        if self.url_prefix and (request_path == self.url_prefix or request_path.startswith(self.url_prefix + "/")):
            request_path = request_path[len(self.url_prefix) :] or "/"
        return super().translate_path(request_path)
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(url: str, timeout: float = 20.0) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError(f"Readiness URL must use the local test server: {url}")
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            # The loopback HTTP URL is built and validated by the runner.
            with urllib.request.urlopen(url, timeout=2):  # nosec B310
                return
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Server did not become ready: {url}: {last_error}")


@contextlib.contextmanager
def static_server(root: Path, prefix: str = ""):
    handler = partial(PrefixHandler, directory=str(root), prefix=prefix)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{prefix.strip(chr(47)) + chr(47) if prefix.strip(chr(47)) else chr(47)}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@contextlib.contextmanager
def app_server(project: Project, artifacts_dir: Path):
    port = _free_port()
    log_path = artifacts_dir / f"{project.id}-server.log"
    log_handle = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    variables = {"port": str(port), "artifacts_dir": str(artifacts_dir), "project_path": str(project.path)}
    env.update({key: value.format(**variables) for key, value in project.start_env.items()})
    env.setdefault("PORT", str(port))
    command = project.start_command or [".venv/bin/python", "app/main.py"]
    executable = project.path / command[0]
    resolved_command = [str(executable), *command[1:]] if executable.exists() else command
    process = subprocess.Popen(resolved_command, cwd=project.path, env=env, stdout=log_handle, stderr=subprocess.STDOUT, text=True)
    base_url = f"http://127.0.0.1:{port}/"
    try:
        _wait_for(base_url + project.readiness_path.lstrip("/"))
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        log_handle.close()


def _browser_findings(projects: list[Project], mode: str, offline: bool, artifacts_dir: Path, update_baselines: bool) -> list[Finding]:
    browser_script = SYSTEM_ROOT / "browser" / "run-browser-checks.mjs"
    if not (SYSTEM_ROOT / "node_modules" / "@playwright" / "test").exists():
        return [Finding("quality-system", "browser", "warning", "Playwright is not installed; run scripts/setup.sh")]
    targets = []
    with contextlib.ExitStack() as stack:
        findings: list[Finding] = []
        for project in projects:
            if not project.checks.get("browser", True) or not project.browser_paths or project.kind not in {"static", "flask"}:
                continue
            try:
                base_url = stack.enter_context(static_server(project.path, urlparse(project.live_url).path)) if project.kind == "static" else stack.enter_context(app_server(project, artifacts_dir))
            except Exception as exc:
                findings.append(Finding(project.id, "browser", "error", "Local test server failed to start", details=str(exc)))
                continue
            targets.append({"id": project.id, "baseUrl": base_url, "browserPaths": [route for route in project.browser_paths if route not in project.ignored_routes], "visualPaths": project.visual_paths, "crossBrowserPaths": project.cross_browser_paths, "login": project.login or None, "viewports": project.browser_viewports, "themePolicy": project.theme_policy, "mobileGridChecks": project.mobile_grid_checks})
        if not targets:
            return findings
        payload = {"mode": mode, "offline": offline, "projects": targets, "artifactsDir": str(artifacts_dir), "baselinesDir": str(SYSTEM_ROOT / "baselines"), "updateBaselines": update_baselines}
        input_path = artifacts_dir / "browser-input.json"
        input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_command([_node_binary(), str(browser_script), str(input_path)], cwd=SYSTEM_ROOT, timeout=900)
        if result.returncode != 0:
            findings.append(Finding("quality-system", "browser", "error", "Playwright runner failed", details=trim_output(result.stdout, 5000)))
            return findings
        try:
            raw = json.loads(result.stdout)
            findings.extend(Finding(**item) for item in raw["findings"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            findings.append(Finding("quality-system", "browser", "error", f"Could not parse Playwright results: {exc}", details=trim_output(result.stdout, 5000)))
        return findings


def _lighthouse_findings(projects: list[Project], artifacts_dir: Path) -> list[Finding]:
    script = SYSTEM_ROOT / "browser" / "run-lighthouse.mjs"
    targets = []
    with contextlib.ExitStack() as stack:
        for project in projects:
            if not project.checks.get("lighthouse", True) or project.kind != "static" or not project.lighthouse_path:
                continue
            base_url = stack.enter_context(static_server(project.path, urlparse(project.live_url).path))
            targets.append({"id": project.id, "url": base_url + project.lighthouse_path.lstrip("/")})
        if not targets:
            return []
        input_path = artifacts_dir / "lighthouse-input.json"
        input_path.write_text(json.dumps({"targets": targets, "artifactsDir": str(artifacts_dir)}), encoding="utf-8")
        result = run_command([_node_binary(), str(script), str(input_path)], cwd=SYSTEM_ROOT, timeout=900)
        if result.returncode != 0:
            return [Finding("quality-system", "lighthouse", "error", "Lighthouse runner failed", details=trim_output(result.stdout, 5000))]
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return [Finding("quality-system", "lighthouse", "error", f"Could not parse Lighthouse output: {exc}", details=trim_output(result.stdout, 5000))]
    findings = []
    thresholds = {"performance": 70, "accessibility": 90, "best-practices": 85, "seo": 90}
    for item in raw["results"]:
        if item.get("error"):
            findings.append(Finding(item["id"], "lighthouse", "warning", "Lighthouse could not complete", details=item["error"]))
            continue
        scores = item["scores"]
        low = [name for name, score in scores.items() if score < thresholds.get(name, 0)]
        findings.append(Finding(item["id"], "lighthouse", "warning" if low else "pass", "Lighthouse thresholds need attention" if low else "Lighthouse thresholds passed", path=item["reportPath"], details=", ".join(f"{name}={score}" for name, score in scores.items())))
    return findings


def _duplication_findings(projects: list[Project]) -> list[Finding]:
    executable = SYSTEM_ROOT / "node_modules" / ".bin" / "jscpd"
    if not executable.exists():
        return [Finding("quality-system", "duplication", "warning", "jscpd is not installed; run scripts/setup.sh")]
    findings = []
    for project in projects:
        if not project.checks.get("duplication", True) or project.kind not in {"static", "flask"}:
            continue
        scan_paths = project.duplication_paths or ["."]
        targets = [str((project.path / relative).resolve()) for relative in scan_paths]
        result = run_command([str(executable), *targets, "--min-lines", "12", "--min-tokens", "80", "--threshold", "8", "--ignore", "**/.git/**,**/.venv/**,**/node_modules/**,**/data/**,**/docs/**", "--silent"], cwd=SYSTEM_ROOT, timeout=240)
        details = trim_output(result.stdout)
        scope = ", ".join(scan_paths)
        findings.append(Finding(project.id, "duplication", "pass" if result.returncode == 0 else "warning", "Cross-file duplication is below the 8% threshold" if result.returncode == 0 else "Cross-file duplication exceeds the review threshold or scan failed", path=scope, details=details))
    return findings


def execute(projects: list[Project], *, mode: str, offline: bool, update_baselines: bool) -> RunResult:
    started = time.monotonic()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    artifacts_dir = SYSTEM_ROOT / "artifacts" / stamp
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    result = RunResult(started_at, mode, offline, [project.id for project in projects], artifacts_dir=str(artifacts_dir))
    for project in projects:
        result.findings.extend(run_static_checks(project, SYSTEM_ROOT, mode, offline))
        if project.adapter:
            try:
                adapter = load_adapter(project.adapter)
                result.findings.extend(adapter(project, {
                    "mode": mode, "offline": offline, "system_root": SYSTEM_ROOT,
                    "artifacts_dir": artifacts_dir,
                }))
            except (ImportError, ValueError, TypeError) as exc:
                result.findings.append(Finding(project.id, "adapter", "error", "Custom adapter failed", details=str(exc)))
    result.findings.extend(_browser_findings(projects, mode, offline, artifacts_dir, update_baselines))
    if mode == "full":
        result.findings.extend(check_local_operations(SYSTEM_ROOT, full=True))
        result.findings.extend(_duplication_findings(projects))
        result.findings.extend(_lighthouse_findings(projects, artifacts_dir))
    result.duration_seconds = time.monotonic() - started
    return result
