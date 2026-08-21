from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "pass": 3}
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "none": 5}


@dataclass(slots=True)
class Finding:
    project: str
    check: str
    severity: str
    message: str
    path: str = ""
    details: str = ""
    priority: str = ""
    next_action: str = ""
    change: str = "unchanged"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Project:
    id: str
    name: str
    path: Path
    kind: str
    live_url: str = ""
    browser_paths: list[str] = field(default_factory=list)
    visual_paths: list[str] = field(default_factory=list)
    lighthouse_path: str = ""
    required_files: list[str] = field(default_factory=list)
    python_paths: list[str] = field(default_factory=list)
    duplication_paths: list[str] = field(default_factory=list)
    project_commands: list[list[str]] = field(default_factory=list)
    test_command: list[str] = field(default_factory=list)
    login: dict[str, str] = field(default_factory=dict)
    source_path: Path | None = None
    profile: str = ""
    adapter: str = ""
    start_command: list[str] = field(default_factory=list)
    start_env: dict[str, str] = field(default_factory=dict)
    readiness_path: str = "/"
    checks: dict[str, bool] = field(default_factory=dict)
    browser_viewports: list[dict[str, Any]] = field(default_factory=list)
    theme_policy: str = ""
    mobile_grid_checks: list[dict[str, Any]] = field(default_factory=list)
    cross_browser_paths: list[str] = field(default_factory=list)
    automation_paths: list[str] = field(default_factory=list)
    forbidden_strings: list[str] = field(default_factory=list)
    generator_contracts: list[dict[str, Any]] = field(default_factory=list)
    external_link_limit: int = 30
    ignored_files: list[str] = field(default_factory=list)
    ignored_routes: list[str] = field(default_factory=list)
    severity_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    release_rules: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunResult:
    started_at: str
    mode: str
    offline: bool
    selected_projects: list[str]
    findings: list[Finding] = field(default_factory=list)
    artifacts_dir: str = ""
    duration_seconds: float = 0.0
    project_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    comparison: dict[str, Any] = field(default_factory=dict)
    previous_scan: str = ""

    def counts(self) -> dict[str, int]:
        counts = {key: 0 for key in SEVERITY_ORDER}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "mode": self.mode,
            "offline": self.offline,
            "selected_projects": self.selected_projects,
            "findings": [finding.as_dict() for finding in self.findings],
            "counts": self.counts(),
            "artifacts_dir": self.artifacts_dir,
            "duration_seconds": round(self.duration_seconds, 2),
            "project_summaries": self.project_summaries,
            "comparison": self.comparison,
            "previous_scan": self.previous_scan,
        }
