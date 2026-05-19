#!/bin/sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$PROJECT_DIR/venv/bin/python"
DAYS="${1:-21}"

pick_python() {
  for candidate in "$VENV_PY" python3; do
    if [ "$candidate" = "python3" ]; then
      command -v python3 >/dev/null 2>&1 || continue
    elif [ ! -x "$candidate" ]; then
      continue
    fi

    "$candidate" - <<'PY' >/dev/null 2>&1
import importlib.util
import sys

required = ('flask', 'markdown')
ok = all(importlib.util.find_spec(name) is not None for name in required)
raise SystemExit(0 if ok else 1)
PY
    if [ $? -eq 0 ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="$(pick_python)" || {
  echo "No suitable Python interpreter found (need flask and markdown available)." >&2
  exit 1
}

cd "$PROJECT_DIR"
exec "$PYTHON_BIN" - "$DAYS" <<'PY'
import sys

from app import create_app

days = sys.argv[1]
app = create_app()
runner = app.test_cli_runner()
result = runner.invoke(args=['list-regular-meal-notifications', '--days', days])
sys.stdout.write(result.output)
if result.exception:
    raise result.exception
raise SystemExit(result.exit_code)
PY