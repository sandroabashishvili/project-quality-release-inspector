from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path
from contextlib import contextmanager
from typing import IO, Mapping, Sequence


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 180,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=merged_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def trim_output(output: str, limit: int = 1800) -> str:
    text = output.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… output shortened …"



@contextmanager
def scan_lock(path: Path):
    handle: IO[str] = path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("Another Quality System scan is already running") from None
    try:
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
