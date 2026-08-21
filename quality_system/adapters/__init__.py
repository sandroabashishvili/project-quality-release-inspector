from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from typing import Any

from ..models import Finding, Project


Adapter = Callable[[Project, dict[str, Any]], list[Finding]]


def load_adapter(name: str) -> Adapter | None:
    if not name:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"Invalid adapter name: {name!r}")
    module = importlib.import_module(f"{__name__}.{name}")
    runner = getattr(module, "run", None)
    if not callable(runner):
        raise ValueError(f"Adapter {name!r} must expose run(project, context)")
    return runner
