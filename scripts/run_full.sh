#!/usr/bin/env bash
set -euo pipefail
SYSTEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SYSTEM_DIR"
export PATH="$SYSTEM_DIR/node_modules/node/bin:$PATH"
exec .venv/bin/python -m quality_system --mode full "$@"
