#!/usr/bin/env bash
set -euo pipefail

SYSTEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SYSTEM_DIR"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

npm install
export PATH="$SYSTEM_DIR/node_modules/node/bin:$PATH"
npx playwright install chromium firefox webkit
npm audit --omit=dev


echo
echo "Portfolio Quality System is ready."
echo "Quick check: .venv/bin/python -m quality_system --mode quick --offline"
echo "Full check : .venv/bin/python -m quality_system --mode full"
