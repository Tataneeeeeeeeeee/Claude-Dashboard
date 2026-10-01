#!/usr/bin/env bash
# Install Agentboard and create a `agentboard` command.
#
#   curl -fsSL <url>/install.sh | bash
#   ./install.sh                      # from a checkout
#   ./install.sh --uninstall
#
# Installs into ~/.local/share/agentboard and puts a launcher in
# ~/.local/bin. Nothing is written outside those two directories and, on
# Linux, the XDG desktop-entry directory.
set -euo pipefail

PREFIX="${AGENTBOARD_PREFIX:-$HOME/.local/share/agentboard}"
BINDIR="${AGENTBOARD_BIN:-$HOME/.local/bin}"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
LAUNCHER="$BINDIR/agentboard"

# Where to fetch the source when this script is piped into a shell rather
# than run from a checkout. Override to install a fork or a branch:
#   curl -fsSL .../install.sh | AGENTBOARD_SRC=<url-or-repo> bash
DEFAULT_SRC="https://github.com/Tataneeeeeeeeeee/Claude-Dashboard/archive/refs/heads/main.tar.gz"
SRC="${AGENTBOARD_SRC:-$DEFAULT_SRC}"

say()  { printf '%s\n' "$*"; }
# A host can answer 200 with an HTML page instead of an archive - GitHub
# does exactly that for a branch with no commits - so `curl -f` is not
# enough and the bytes have to be checked.
not_an_archive() {
  die "$(cat <<MSG
$1 did not return an archive.

The server answered, but with $2 rather than a package. That usually means:

  - the branch has no commits pushed yet, or
  - the repository is private, or
  - the branch name in the URL is wrong.

Push your code first, then run this again. To install from a local copy
meanwhile:

  AGENTBOARD_SRC=/path/to/checkout bash install.sh
MSG
)"
}
step() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Before the rename the app was installed as `claude-dashboard`. Those
# pieces are recognised by their contents and removed, so two copies never
# coexist. Its data directory is moved by the app itself on first run.
LEGACY_PREFIX="$HOME/.local/share/claude-dashboard"
LEGACY_LAUNCHER="$BINDIR/claude-dashboard"
LEGACY_DESKTOP="$DESKTOP_DIR/claude-dashboard.desktop"

remove_legacy() {
  if [ -f "$LEGACY_LAUNCHER" ] && grep -q "written by install.sh" "$LEGACY_LAUNCHER" 2>/dev/null; then
    rm -f "$LEGACY_LAUNCHER"
    say "  removed the old launcher $LEGACY_LAUNCHER"
  fi
  if [ -f "$LEGACY_DESKTOP" ] && grep -q "Claude Code Dashboard" "$LEGACY_DESKTOP" 2>/dev/null; then
    rm -f "$LEGACY_DESKTOP"
  fi
  if [ -f "$LEGACY_PREFIX/app/run_app.py" ] && [ -d "$LEGACY_PREFIX/venv" ]; then
    rm -rf "$LEGACY_PREFIX"
    say "  removed the old installation $LEGACY_PREFIX"
  fi
}

# ----------------------------------------------------------- uninstall

uninstall() {
  step "Removing Agentboard"
  rm -f  "$LAUNCHER"
  rm -f  "$DESKTOP_DIR/agentboard.desktop"
  rm -rf "$PREFIX"
  remove_legacy
  # mimeinfo.cache in that directory is shared with every other desktop
  # entry, so refresh it rather than deleting it.
  command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  say
  say "Removed the application, its virtualenv and the launcher."
  say "Your data is untouched:"
  say "  ~/.agentboard    this app's config, cache, trash and backups"
  say "  ~/.claude, ~/.codex, ~/.gemini, ...   each AI tool's own files"
  say
  say "Delete ~/.agentboard by hand if you want that gone too."
  exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall
[ "${1:-}" = "-u" ] && uninstall

# -------------------------------------------------------------- python

step "Checking Python"
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c 'import sys; print("%d%02d" % sys.version_info[:2])' 2>/dev/null || echo 0)
    if [ "$version" -ge 311 ] 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  fi
done
[ -n "$PYTHON" ] || die "Python 3.11 or newer is required but was not found on PATH."
say "  using $("$PYTHON" -c 'import sys; print(sys.executable, "-", sys.version.split()[0])')"

# ------------------------------------------------------ webview backend

OS="$(uname -s)"
if [ "$OS" = "Linux" ]; then
  if ! "$PYTHON" - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("WebKit2", "4.1")
PY
  then
    warn "the GTK WebKit backend is missing, so the app cannot open a window yet."
    say  "  Install it with one of:"
    say  "    Arch            sudo pacman -S --needed webkit2gtk-4.1 python-gobject"
    say  "    Debian/Ubuntu   sudo apt install python3-gi gir1.2-webkit2-4.1"
    say  "    Fedora          sudo dnf install python3-gobject webkit2gtk4.1"
    say  "  Installation continues; 'agentboard --headless --browser' works meanwhile."
  else
    say "  GTK WebKit backend found"
  fi
fi

# -------------------------------------------------------------- source

# Resolve the directory this script lives in, if it is a real file. When
# the script is piped into bash there is no such directory.
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

WORK=""
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; }
trap cleanup EXIT

if [ -n "$SCRIPT_DIR" ] && [ -d "$SCRIPT_DIR/agentboard" ]; then
  step "Installing from this checkout"
  SOURCE="$SCRIPT_DIR"
  say "  $SOURCE"
elif [ -n "$SRC" ]; then
  step "Fetching the source"
  WORK="$(mktemp -d)"
  # Order matters: an archive URL on a git host still contains the host
  # pattern, so the extensions have to be tested first or a .tar.gz gets
  # handed to `git clone`.
  case "$SRC" in
    *.tar.gz|*.tgz)
      command -v curl >/dev/null 2>&1 || die "curl is needed to download $SRC"
      curl -fsSL "$SRC" -o "$WORK/src.tar.gz" \
        || die "could not download $SRC"
      # A gzip stream starts with 0x1f 0x8b; anything else is an error page.
      if [ "$(head -c 2 "$WORK/src.tar.gz" | od -An -tx1 | tr -d ' \n')" != "1f8b" ]; then
        not_an_archive "$SRC" "$(file -b "$WORK/src.tar.gz" 2>/dev/null | cut -d, -f1)"
      fi
      mkdir -p "$WORK/src"
      tar -xzf "$WORK/src.tar.gz" -C "$WORK/src" --strip-components=1 \
        || die "could not unpack $SRC"
      SOURCE="$WORK/src"
      ;;
    *.zip)
      command -v curl >/dev/null 2>&1 || die "curl is needed to download $SRC"
      command -v unzip >/dev/null 2>&1 || die "unzip is needed to unpack $SRC"
      curl -fsSL "$SRC" -o "$WORK/src.zip" || die "could not download $SRC"
      # A zip starts with "PK".
      if [ "$(head -c 2 "$WORK/src.zip")" != "PK" ]; then
        not_an_archive "$SRC" "$(file -b "$WORK/src.zip" 2>/dev/null | cut -d, -f1)"
      fi
      unzip -q "$WORK/src.zip" -d "$WORK/unpacked" || die "could not unpack $SRC"
      SOURCE="$(find "$WORK/unpacked" -maxdepth 2 -name agentboard -type d -print -quit)"
      [ -n "$SOURCE" ] || die "no agentboard directory inside $SRC"
      SOURCE="$(dirname "$SOURCE")"
      ;;
    /*|./*)
      [ -d "$SRC" ] || die "no such directory: $SRC"
      SOURCE="$SRC"
      ;;
    *.git|git@*|http://*|https://*|ssh://*)
      command -v git >/dev/null 2>&1 || die "git is needed to clone $SRC"
      git clone --depth 1 --quiet "$SRC" "$WORK/src"
      SOURCE="$WORK/src"
      ;;
    *)
      die "cannot tell how to fetch '$SRC' (expected a git URL, a .tar.gz, a .zip or a path)"
      ;;
  esac
  say "  $SRC"
else
  # Unreachable while DEFAULT_SRC is set, but a blanked variable should
  # still say something useful rather than fall through silently.
  die "no source to install from: AGENTBOARD_SRC is empty. Leave it unset to install the published version."
fi

[ -d "$SOURCE/agentboard" ] || die "$SOURCE does not look like the project (no agentboard/)"

# ------------------------------------------------------------- install

step "Installing into $PREFIX"
remove_legacy
mkdir -p "$PREFIX" "$BINDIR"
rm -rf "$PREFIX/app"
mkdir -p "$PREFIX/app"

# Copy only what the application needs at runtime.
for item in agentboard assets requirements.txt run_app.py README.md SCHEMA.md config.json; do
  [ -e "$SOURCE/$item" ] && cp -R "$SOURCE/$item" "$PREFIX/app/"
done
find "$PREFIX/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

step "Creating the virtualenv"
venv_flags=""
[ "$OS" = "Linux" ] && venv_flags="--system-site-packages"
rm -rf "$PREFIX/venv"
# shellcheck disable=SC2086
"$PYTHON" -m venv $venv_flags "$PREFIX/venv"
VPY="$PREFIX/venv/bin/python"

step "Installing dependencies"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r "$PREFIX/app/requirements.txt"

# ------------------------------------------------------------ launcher

step "Creating the launcher"
cat > "$LAUNCHER" <<LAUNCHEREOF
#!/usr/bin/env bash
# Launcher for Agentboard, written by install.sh.
#
# Opens the window and returns straight away, so the terminal stays free
# and closing it does not close the app.
#
# Options that print to the terminal keep the foreground, because there
# would be nothing to read otherwise: --check, --version, --headless,
# --browser, --debug and --help. Add --foreground to any run to stay
# attached and watch the output.
set -eu

PYTHON="$PREFIX/venv/bin/python"
APP="$PREFIX/app/run_app.py"
LOG="\${AGENTBOARD_HOME:-\$HOME/.agentboard}/launch.log"

# --foreground is consumed here; the application does not know it.
if [ "\${1:-}" = "--foreground" ] || [ "\${1:-}" = "-F" ]; then
  shift
  exec "\$PYTHON" "\$APP" "\$@"
fi

for arg in "\$@"; do
  case "\$arg" in
    --check|--version|--headless|--browser|--debug|-h|--help)
      exec "\$PYTHON" "\$APP" "\$@"
      ;;
  esac
done

mkdir -p "\$(dirname "\$LOG")"
printf '\\n=== %s ===\\n' "\$(date)" >> "\$LOG"

# nohup detaches from the terminal, so the app survives closing it.
nohup "\$PYTHON" "\$APP" "\$@" >> "\$LOG" 2>&1 &
pid=\$!
disown 2>/dev/null || true

# A detached process that dies instantly would otherwise fail silently.
# One second is enough: the failures that matter here - no webview
# backend, a broken virtualenv - surface immediately.
sleep 1
if kill -0 "\$pid" 2>/dev/null; then
  echo "Agentboard started (pid \$pid)."
else
  echo "error: it exited immediately. Last lines of \$LOG:" >&2
  tail -n 20 "\$LOG" >&2
  exit 1
fi
LAUNCHEREOF
chmod +x "$LAUNCHER"
say "  $LAUNCHER"

if [ "$OS" = "Linux" ]; then
  mkdir -p "$DESKTOP_DIR"
  cat > "$DESKTOP_DIR/agentboard.desktop" <<DESKTOPEOF
[Desktop Entry]
Type=Application
Name=Agentboard
Comment=Browse and analyse the data Claude Code stores in ~/.claude
Exec=$LAUNCHER
Icon=$PREFIX/app/assets/icon.png
Terminal=false
Categories=Development;Utility;
StartupWMClass=run_app.py
DESKTOPEOF
  command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  say "  $DESKTOP_DIR/agentboard.desktop"
fi

# --------------------------------------------------------------- verify

step "Verifying"
if "$LAUNCHER" --version >/dev/null 2>&1; then
  say "  $("$LAUNCHER" --version)"
else
  die "the launcher did not run. Try: $VPY $PREFIX/app/run_app.py --check"
fi

say
say "Installed. Start it with:"
say
say "    agentboard"
say

case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *)
    warn "$BINDIR is not on your PATH, so the command will not be found yet."
    say  "  Add it with one of:"
    say  "    echo 'export PATH=\"\$PATH:$BINDIR\"' >> ~/.bashrc"
    say  "    echo 'export PATH=\"\$PATH:$BINDIR\"' >> ~/.zshrc"
    say  "  then open a new terminal. Until then, run it with:"
    say  "    $LAUNCHER"
    say
    ;;
esac

say "The window opens and the terminal is handed straight back to you."
say
say "Other commands:"
say "    agentboard --foreground         stay attached and watch the output"
say "    agentboard --check              report the webview backend"
say "    agentboard --headless --browser run without a native window"
say "    $0 --uninstall   remove it again"
