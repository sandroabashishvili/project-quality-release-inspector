from __future__ import annotations

import re
from pathlib import Path

from .models import Finding, Project



IGNORED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "artifacts",
    "reports",
}
TEXT_SUFFIXES = {".html", ".css", ".js", ".json", ".md", ".py", ".txt", ".xml"}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "generic API key": re.compile(
        r"(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}['\"]"
    ),
}


def _finding(
    project: Project,
    check: str,
    severity: str,
    message: str,
    path: Path | str = "",
    details: str = "",
) -> Finding:
    display_path = ""
    if path:
        try:
            display_path = str(Path(path).resolve().relative_to(project.path.resolve()))
        except (ValueError, OSError):
            display_path = str(path)
    return Finding(project.id, check, severity, message, display_path, details)


def _iter_files(root: Path, suffixes: set[str] | None = None, ignored_patterns: list[str] | None = None):
    for path in root.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ignored_patterns and any(relative.match(pattern) for pattern in ignored_patterns):
            continue
        if suffixes and path.suffix.lower() not in suffixes:
            continue
        yield path
