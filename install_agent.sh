#!/bin/zsh
# One-time setup: create the venv and install dependencies for the US screener.

set -euo pipefail

DIR="${0:A:h}"
cd "$DIR"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "Done. Run the screener with ./run_us_agent.sh"
