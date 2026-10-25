#!/bin/zsh
# Run the US 52-week-high / gainer screener using the project venv.

set -euo pipefail

DIR="${0:A:h}"          # directory this script lives in (zsh-ism)
cd "$DIR"

PYTHON="$DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "venv python not found at $PYTHON. Run ./install_agent.sh first." >&2
  exit 1
fi

exec "$PYTHON" "$DIR/build_us_highs.py" "$@"
