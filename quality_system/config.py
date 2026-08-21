from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import Project


SYSTEM_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG = SYSTEM_ROOT / "config" / "projects.local.json"
PUBLIC_CONFIG = SYSTEM_ROOT / "config" / "projects.json"
DEFAULT_CONFIG = LOCAL_CONFIG if LOCAL_CONFIG.exists() else PUBLIC_CONFIG
PROFILES_CONFIG = SYSTEM_ROOT / "config" / "profiles.json"
LEGACY_PROFILE_MAP = {
    "static": "static_website",
    "flask": "flask_web_app",
    "docs": "documentation",
    "python": "python_automation",
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_profiles(path: Path = PROFILES_CONFIG) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("profiles.json must contain a non-empty 'profiles' object")
    return profiles


def load_projects(config_path: Path = DEFAULT_CONFIG) -> list[Project]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.parent
    profiles = load_profiles()
    global_defaults = raw.get("defaults", {})
    projects: list[Project] = []
    seen: set[str] = set()
    for item in raw["projects"]:
        project_id = str(item.get("id", "")).strip()
        if not project_id or project_id in seen:
            raise ValueError(f"Project id is missing or duplicated: {project_id!r}")
        seen.add(project_id)
        profile_name = item.get("profile") or LEGACY_PROFILE_MAP.get(item.get("kind", ""), "custom")
        if profile_name not in profiles:
            raise ValueError(f"Unknown project profile {profile_name!r} for {project_id}")
        data = _merge(profiles[profile_name], global_defaults)
        data = _merge(data, item)
        data["profile"] = profile_name
        data.setdefault("kind", profiles[profile_name].get("kind", "docs"))
        theme_policy = str(data.get("theme_policy", "")).strip().lower()
        if theme_policy not in {"", "automatic", "manual", "fixed-dark", "fixed-light"}:
            raise ValueError(f"Unknown theme_policy {theme_policy!r} for {project_id}")
        data["theme_policy"] = theme_policy
        data["path"] = (base / data["path"]).resolve()
        if data.get("source_path"):
            data["source_path"] = (base / data["source_path"]).resolve()
        for contract in data.get("generator_contracts", []):
            if contract.get("cwd"):
                contract["cwd"] = str((base / contract["cwd"]).resolve())
        projects.append(Project(**data))
    return projects
