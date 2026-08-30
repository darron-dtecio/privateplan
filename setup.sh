#!/usr/bin/env bash
# PrivatePlan setup - macOS / Linux
# Creates a repo-local .venv and installs dependencies. Nothing leaves your machine.
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null 2>&1 || { echo "Python 3 not found on PATH. Install Python 3.11+ and retry." >&2; exit 1; }

version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "Using Python $version"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11 or newer is required (found $version)." >&2; exit 1; }

[ -d .venv ] || { echo "Creating .venv ..."; python3 -m venv .venv; }

./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

echo
echo "Done. Next:"
echo "  source .venv/bin/activate"
echo "  python server.py"
echo "  then open http://127.0.0.1:5000/finance and load the sample household"
