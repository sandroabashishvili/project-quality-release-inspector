from __future__ import annotations

from typing import Any

from ..models import Finding, Project


def run(project: Project, context: dict[str, Any]) -> list[Finding]:
    """Example custom adapter. Copy this file and implement project-specific validation."""
    return [
        Finding(
            project.id,
            "custom-adapter",
            "info",
            f"Example adapter executed in {context['mode']} mode",
        )
    ]
