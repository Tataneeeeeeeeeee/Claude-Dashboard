#!/usr/bin/env bash
# Development launcher for macOS and Linux.
#
# Creates .venv if missing, installs requirements.txt, then opens the app.
# Pass --headless to serve the UI without a native window, or --check to
# report which webview backend is available.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".venv"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not found. Install Python 3.11 or newer." >&2
  exit 1
fi

version=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$version" in
  3.1[1-9]|3.[2-9][0-9]|[4-9].*) ;;
  *) echo "error: Python 3.11+ required, found $version." >&2; exit 1 ;;
esac

# On Linux the GTK webview backend needs the system PyGObject, which is only
# importable from a venv created with --system-site-packages.
venv_flags=()
if [ "$(uname -s)" = "Linux" ]; then
  venv_flags+=(--system-site-packages)
fi

if [ ! -d "$VENV" ]; then
  echo "creating virtualenv in $VENV"
  "$PYTHON" -m venv "${venv_flags[@]}" "$VENV"
fi

VPY="$VENV/bin/python"

# Re-install only when requirements.txt is newer than the last successful run.
stamp="$VENV/.requirements-stamp"
if [ ! -f "$stamp" ] || [ requirements.txt -nt "$stamp" ]; then
  echo "installing dependencies"
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install --quiet -r requirements.txt
  touch "$stamp"
fi

exec "$VPY" -m agentboard.app "$@"
