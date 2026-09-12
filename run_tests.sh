#!/usr/bin/env bash
# Run the whole test suite: Python with pytest, browser code with node --test.
#
# Node is optional; if it is missing the JavaScript tests are skipped with a
# notice rather than failing the run.
set -uo pipefail
cd "$(dirname "$0")"

PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

status=0

echo "== python =="
"$PY" -m pytest tests/ || status=1

echo
echo "== javascript =="
if command -v node >/dev/null 2>&1; then
  # Node only auto-discovers conventionally named files, so pass ours.
  node --test tests/js/test_*.mjs || status=1
else
  echo "node not found; skipping the browser-side tests"
fi

echo
echo "== lint =="
if "$PY" -c "import pyflakes" 2>/dev/null; then
  "$PY" -m pyflakes claude_dashboard/*.py tools/*.py tests/*.py check_backend.py \
    && echo "pyflakes: clean" || status=1
else
  echo "pyflakes not installed; skipping"
fi

exit $status
