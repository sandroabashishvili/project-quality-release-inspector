from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .models import Finding, RunResult
from .policy import finding_key


def _scan_files(history_dir: Path) -> list[Path]:
    scans = history_dir / "scans"
    if not scans.exists():
        return []
    return sorted(scans.glob("*.json"), reverse=True)


def load_previous(result: RunResult, history_dir: Path) -> dict[str, Any] | None:
    selected = sorted(result.selected_projects)
    for path in _scan_files(history_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("mode") == result.mode and sorted(data.get("selected_projects", [])) == selected:
            data["_history_path"] = str(path)
            return data
    return None


def compare_with_previous(result: RunResult, history_dir: Path) -> None:
    previous = load_previous(result, history_dir)
    if not previous:
        result.comparison = {
            "available": False,
            "new": len([item for item in result.findings if item.severity in {"error", "warning"}]),
            "fixed": 0,
            "unchanged": 0,
            "regressions": 0,
            "projects": {},
            "history": history_trends(history_dir, result.selected_projects),
        }
        for finding in result.findings:
            finding.change = "new" if finding.severity in {"error", "warning"} else "unchanged"
        return

    result.previous_scan = previous.get("started_at", "")
    old_findings = [item for item in previous.get("findings", []) if item.get("severity") in {"error", "warning"}]
    new_findings = [item for item in result.findings if item.severity in {"error", "warning"}]
    old_keys = {
        finding_key(Finding(**{key: item.get(key, "") for key in Finding.__dataclass_fields__}))
        for item in old_findings
    }
    new_by_key = {finding_key(item): item for item in new_findings}
    new_keys = set(new_by_key)
    for item in result.findings:
        key = finding_key(item)
        item.change = "unchanged" if key in old_keys else ("new" if item.severity in {"error", "warning"} else "unchanged")

    fixed_keys = old_keys - new_keys
    new_issue_keys = new_keys - old_keys
    fixed_items = []
    for raw in old_findings:
        fixture = Finding(**{key: raw.get(key, "") for key in Finding.__dataclass_fields__})
        if finding_key(fixture) in fixed_keys:
            fixed_items.append(raw)

    projects: dict[str, dict[str, Any]] = defaultdict(lambda: {"new": 0, "fixed": 0, "unchanged": 0, "regressions": 0})
    for key, item in new_by_key.items():
        bucket = projects[item.project]
        if key in new_issue_keys:
            bucket["new"] += 1
            if item.priority in {"critical", "high"}:
                bucket["regressions"] += 1
        else:
            bucket["unchanged"] += 1
    for item in fixed_items:
        projects[item.get("project", "unknown")]["fixed"] += 1

    result.comparison = {
        "available": True,
        "previous_scan": result.previous_scan,
        "new": len(new_issue_keys),
        "fixed": len(fixed_keys),
        "unchanged": len(new_keys & old_keys),
        "regressions": sum(1 for key in new_issue_keys if new_by_key[key].priority in {"critical", "high"}),
        "fixed_findings": fixed_items,
        "projects": dict(projects),
        "history": history_trends(history_dir, result.selected_projects),
    }


def history_trends(history_dir: Path, project_ids: list[str], limit: int = 30) -> dict[str, list[dict[str, Any]]]:
    trends: dict[str, list[dict[str, Any]]] = {project_id: [] for project_id in project_ids}
    for path in reversed(_scan_files(history_dir)[:limit]):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for project_id in project_ids:
            summary = data.get("project_summaries", {}).get(project_id)
            if not summary:
                continue
            lighthouse = {}
            for item in data.get("findings", []):
                if item.get("project") == project_id and item.get("check") == "lighthouse":
                    for pair in str(item.get("details", "")).split(","):
                        if "=" in pair:
                            key, value = pair.strip().split("=", 1)
                            try:
                                lighthouse[key] = int(value)
                            except ValueError:
                                pass
            trends[project_id].append({
                "started_at": data.get("started_at", ""),
                "health_score": summary.get("health_score", 0),
                "errors": summary.get("counts", {}).get("error", 0),
                "warnings": summary.get("counts", {}).get("warning", 0),
                "lighthouse": lighthouse,
            })
    return trends


def save_history(result: RunResult, history_dir: Path) -> Path:
    scans = history_dir / "scans"
    scans.mkdir(parents=True, exist_ok=True)
    stamp = result.started_at.replace(":", "-").replace("+", "_")
    path = scans / f"scan-{stamp}.json"
    path.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    index = {
        "latest_scan": result.started_at,
        "latest_file": str(path.relative_to(history_dir)),
        "project_summaries": result.project_summaries,
    }
    (history_dir / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
