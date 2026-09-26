#!/usr/bin/env bash
# One-time local setup for macOS/Linux. Run from the project root:  bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
$PY -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'; print('Python', sys.version.split()[0])"
[ -d .venv ] || $PY -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pip install -e .
[ -f .env ] || { cp .env.example .env; echo "Created .env (TRIAGE_BACKEND=mock)"; }
./.venv/bin/python -m pytest -q
echo -e "\nDone. Next:  source .venv/bin/activate && python -m claims_triage demo"
