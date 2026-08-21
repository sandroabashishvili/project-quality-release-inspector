from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from .models import Finding, Project, RunResult


DEFAULT_PRIORITIES = {"error": "high", "warning": "medium", "info": "info", "pass": "none"}
CHECK_PRIORITIES = {
    "dependencies": {"error": "critical", "warning": "medium"},
    "security": {"error": "critical", "warning": "high"},
    "security-headers": {"warning": "medium", "info": "info"},
    "website-essentials": {"error": "high", "warning": "medium"},
    "tests": {"error": "critical"},
    "project-test": {"error": "critical"},
    "links": {"error": "high"},
    "browser": {"error": "high"},
    "responsive": {"error": "high"},
    "accessibility": {"error": "high", "warning": "medium"},
    "maintainability": {"warning": "low"},
    "duplication": {"warning": "low"},
    "ruff": {"warning": "low"},
    "dead-code": {"warning": "low"},
    "complexity": {"warning": "low"},
    "automation": {"error": "critical", "warning": "medium"},
    "cron": {"error": "critical", "warning": "high"},
    "systemd": {"error": "high", "warning": "medium"},
    "workflow": {"error": "critical", "warning": "medium"},
    "generator-contract": {"error": "critical"},
    "external-links": {"warning": "medium"},
    "cross-browser": {"error": "high"},
}
NEXT_ACTIONS = {
    "dependencies": "Upgrade or replace the vulnerable dependency, then rerun the full scan.",
    "security": "Review the security finding before release and remove exposed secrets immediately.",
    "security-headers": "Enable the missing response header in the hosting platform when the platform supports custom headers.",
    "website-essentials": "Complete the missing website identity, discovery, sharing, consent, fallback, or presentation requirement.",
    "tests": "Fix the failing application test before release.",
    "project-test": "Fix the project validation command before release.",
    "links": "Repair the broken local link or asset reference.",
    "responsive": "Correct the overflowing element at the reported viewport.",
    "accessibility": "Fix the reported WCAG violation and rerun the browser scan.",
    "browser": "Inspect the page/runtime failure and verify it in the reported viewport.",
    "seo": "Complete or correct the reported SEO metadata.",
    "maintainability": "Refactor when the file is next changed; this does not block release by default.",
    "duplication": "Extract shared code if the duplication represents the same responsibility.",
    "ruff": "Apply the reported Python code-quality correction.",
    "automation": "Remove the retired domain or unsafe override from the automation source.",
    "cron": "Restore the approved scheduled command and public-domain override.",
    "systemd": "Correct the user timer or service definition before relying on it.",
    "workflow": "Repair the GitHub Actions workflow and pin each external action to a version.",
    "generator-contract": "Correct the source generator so regenerated pages keep the approved public domain.",
    "external-links": "Review the external destination and replace or remove confirmed broken links.",
    "cross-browser": "Fix the browser-specific rendering or interaction failure.",
}


def finding_key(finding: Finding) -> str:
    raw = "\x1f".join((finding.project, finding.check, finding.path, finding.message))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def apply_policy(projects: list[Project], result: RunResult) -> None:
    by_id = {project.id: project for project in projects}
    for finding in result.findings:
        project = by_id.get(finding.project)
        override = project.severity_overrides.get(finding.check, {}) if project else {}
        if override.get("severity"):
            finding.severity = override["severity"]
        finding.priority = override.get("priority") or CHECK_PRIORITIES.get(finding.check, {}).get(
            finding.severity, DEFAULT_PRIORITIES.get(finding.severity, "medium")
        )
        finding.next_action = override.get("next_action") or NEXT_ACTIONS.get(
            finding.check, "Review the evidence and correct the issue if it is not intentional."
        )

    result.project_summaries = {
        project.id: summarize_project(project, [item for item in result.findings if item.project == project.id])
        for project in projects
    }
    system_findings = [item for item in result.findings if item.project == "quality-system"]
    if system_findings and projects:
        system_project = Project("quality-system", "Quality System", projects[0].path.parent, "docs")
        result.project_summaries["quality-system"] = summarize_project(system_project, system_findings)


def summarize_project(project: Project, findings: list[Finding]) -> dict[str, Any]:
    counts = Counter(item.severity for item in findings)
    priorities = Counter(item.priority for item in findings)
    rules = {
        "block_priorities": ["critical", "high"],
        "block_severities": ["error"],
        "critical_always_blocks": True,
        **project.release_rules,
    }
    blockers = [
        item
        for item in findings
        if (item.priority == "critical" and rules["critical_always_blocks"])
        or (item.priority in rules["block_priorities"] and item.severity in rules["block_severities"])
    ]
    actionable = [item for item in findings if item.severity in {"error", "warning"}]
    if blockers:
        verdict, reason = "NOT READY", blockers[0].message
    elif actionable:
        verdict, reason = "READY WITH WARNINGS", actionable[0].message
    else:
        verdict, reason = "READY", "No release-blocking findings"
    deductions = sum(
        {"critical": 25, "high": 12, "medium": 5, "low": 2}.get(item.priority, 0)
        for item in actionable
    )
    return {
        "id": project.id,
        "name": project.name,
        "profile": project.profile or project.kind,
        "verdict": verdict,
        "reason": reason,
        "health_score": max(0, 100 - deductions),
        "counts": {name: counts.get(name, 0) for name in ("error", "warning", "info", "pass")},
        "priorities": {
            name: priorities.get(name, 0) for name in ("critical", "high", "medium", "low", "info")
        },
        "top_issue": actionable[0].message if actionable else "No current issue",
    }
