#!/usr/bin/env bash
# Build the standalone executable for macOS or Linux.
#
#   ./build.sh            build into dist/
#   ./build.sh --clean    remove build/ and dist/ first
#
# The result needs no Python installation and opens with a double-click.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".venv"

if [ "${1:-}" = "--clean" ]; then
  echo "removing build/ and dist/"
  rm -rf build dist
fi

venv_flags=()
if [ "$(uname -s)" = "Linux" ]; then
  # The GTK webview backend needs the system PyGObject.
  venv_flags+=(--system-site-packages)
fi

if [ ! -d "$VENV" ]; then
  echo "creating virtualenv"
  "$PYTHON" -m venv "${venv_flags[@]}" "$VENV"
fi
VPY="$VENV/bin/python"

echo "installing build dependencies"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r requirements.txt
"$VPY" -m pip install --quiet "pyinstaller>=6.5"

# Regenerate the icons so the bundle never ships a stale one.
"$VPY" tools/make_icon.py >/dev/null

echo "building"
"$VPY" -m PyInstaller agentboard.spec --noconfirm --log-level WARN

if [ "$(uname -s)" = "Darwin" ]; then
  target="dist/Agentboard.app"
else
  target="dist/Agentboard"
fi

if [ ! -e "$target" ]; then
  echo "error: the build produced no output at $target" >&2
  exit 1
fi

size=$(du -sh "$target" | cut -f1)
echo
echo "built $target ($size)"
if [ "$(uname -s)" = "Linux" ]; then
  echo
  echo "note: the GTK/WebKit libraries are provided by your system, not bundled,"
  echo "      so this binary runs on machines that have webkit2gtk-4.1 installed."
fi
