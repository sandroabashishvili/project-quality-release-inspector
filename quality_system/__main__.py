from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG, SYSTEM_ROOT, load_projects
from .history import compare_with_previous, save_history
from .policy import apply_policy
from .process import scan_lock
from .report import write_reports
from .runner import execute


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reusable multi-project quality and release validation.")
    parser.add_argument("--mode", choices=("quick", "full"), default="quick", help="quick=daily, full=before release")
    parser.add_argument("--project", action="append", default=[], help="Project id; repeat to select several")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Project configuration JSON")
    parser.add_argument("--offline", action="store_true", help="Skip live URLs and online dependency audits")
    parser.add_argument("--update-baselines", action="store_true", help="Approve current screenshots as visual baselines")
    parser.add_argument("--list-projects", action="store_true", help="Show configured project ids and exit")
    parser.add_argument("--ci", action="store_true", help="CI output; fail only when release verdict is NOT READY")
    parser.add_argument("--no-history", action="store_true", help="Do not compare with or save scan history")
    return parser


def release_exit_code(project_summaries: dict[str, dict]) -> int:
    return 1 if any(item.get("verdict") == "NOT READY" for item in project_summaries.values()) else 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        projects = load_projects(args.config.resolve())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.list_projects:
        for project in projects:
            print(f"{project.id:18} {project.name} [{project.profile}]")
        return 0
    if args.project:
        selected = set(args.project)
        unknown = selected - {project.id for project in projects}
        if unknown:
            print(f"Unknown project id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        projects = [project for project in projects if project.id in selected]

    try:
        with scan_lock(SYSTEM_ROOT / ".scan.lock"):
            result = execute(projects, mode=args.mode, offline=args.offline, update_baselines=args.update_baselines)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    apply_policy(projects, result)
    history_dir = SYSTEM_ROOT / "history"
    if not args.no_history:
        compare_with_previous(result, history_dir)
    html_path, json_path = write_reports(result, SYSTEM_ROOT / "reports")
    if not args.no_history:
        save_history(result, history_dir)

    counts = result.counts()
    not_ready = [
        summary["name"] for summary in result.project_summaries.values()
        if summary["verdict"] == "NOT READY"
    ]
    print("\nPortfolio Quality System")
    print(f"Projects : {len(projects)}")
    print(f"Mode     : {args.mode}")
    print(f"Errors   : {counts['error']}")
    print(f"Warnings : {counts['warning']}")
    print(f"Passed   : {counts['pass']}")
    print(f"Release  : {'NOT READY' if not_ready else 'READY / READY WITH WARNINGS'}")
    if not_ready:
        print(f"Blocked  : {', '.join(not_ready)}")
    print(f"Report   : {html_path}")
    print(f"JSON     : {json_path}")
    if args.ci:
        print(f"CI_RESULT={'FAIL' if not_ready else 'PASS'}")
    return release_exit_code(result.project_summaries)


if __name__ == "__main__":
    raise SystemExit(main())
