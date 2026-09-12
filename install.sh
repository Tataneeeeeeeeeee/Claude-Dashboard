#!/usr/bin/env bash
# Install Claude Code Dashboard and create a `claude-dashboard` command.
#
#   curl -fsSL <url>/install.sh | bash
#   ./install.sh                      # from a checkout
#   ./install.sh --uninstall
#
# Installs into ~/.local/share/claude-dashboard and puts a launcher in
# ~/.local/bin. Nothing is written outside those two directories and, on
# Linux, the XDG desktop-entry directory.
set -euo pipefail

PREFIX="${CLAUDE_DASHBOARD_PREFIX:-$HOME/.local/share/claude-dashboard}"
BINDIR="${CLAUDE_DASHBOARD_BIN:-$HOME/.local/bin}"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
LAUNCHER="$BINDIR/claude-dashboard"

# Where to fetch the source when this script is piped into a shell rather
# than run from a checkout. Override to install a fork or a branch:
#   curl -fsSL .../install.sh | CLAUDE_DASHBOARD_SRC=<url-or-repo> bash
DEFAULT_SRC="https://github.com/Tataneeeeeeeeeee/Claude-Dashboard/archive/refs/heads/main.tar.gz"
SRC="${CLAUDE_DASHBOARD_SRC:-$DEFAULT_SRC}"

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

  CLAUDE_DASHBOARD_SRC=/path/to/checkout bash install.sh
MSG
)"
}
step() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------- uninstall

uninstall() {
  step "Removing Claude Code Dashboard"
  rm -f  "$LAUNCHER"
  rm -f  "$DESKTOP_DIR/claude-dashboard.desktop"
  rm -rf "$PREFIX"
  # mimeinfo.cache in that directory is shared with every other desktop
  # entry, so refresh it rather than deleting it.
  command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  say
  say "Removed the application, its virtualenv and the launcher."
  say "Your data is untouched:"
  say "  ~/.claude              Claude Code's own files"
  say "  ~/.claude-dashboard    this app's config, cache, trash and backups"
  say
  say "Delete ~/.claude-dashboard by hand if you want that gone too."
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
    say  "  Installation continues; 'claude-dashboard --headless --browser' works meanwhile."
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

if [ -n "$SCRIPT_DIR" ] && [ -d "$SCRIPT_DIR/claude_dashboard" ]; then
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
      SOURCE="$(find "$WORK/unpacked" -maxdepth 2 -name claude_dashboard -type d -print -quit)"
      [ -n "$SOURCE" ] || die "no claude_dashboard directory inside $SRC"
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
  die "no source to install from: CLAUDE_DASHBOARD_SRC is empty. Leave it unset to install the published version."
fi

[ -d "$SOURCE/claude_dashboard" ] || die "$SOURCE does not look like the project (no claude_dashboard/)"

# ------------------------------------------------------------- install

step "Installing into $PREFIX"
mkdir -p "$PREFIX" "$BINDIR"
rm -rf "$PREFIX/app"
mkdir -p "$PREFIX/app"

# Copy only what the application needs at runtime.
for item in claude_dashboard assets requirements.txt run_app.py README.md SCHEMA.md config.json; do
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
# Launcher for Claude Code Dashboard, written by install.sh.
# Every argument is passed through: --check, --headless, --browser, --debug.
exec "$PREFIX/venv/bin/python" "$PREFIX/app/run_app.py" "\$@"
LAUNCHEREOF
chmod +x "$LAUNCHER"
say "  $LAUNCHER"

if [ "$OS" = "Linux" ]; then
  mkdir -p "$DESKTOP_DIR"
  cat > "$DESKTOP_DIR/claude-dashboard.desktop" <<DESKTOPEOF
[Desktop Entry]
Type=Application
Name=Claude Code Dashboard
Comment=Browse and analyse the data Claude Code stores in ~/.claude
Exec=$LAUNCHER
Icon=$PREFIX/app/assets/icon.png
Terminal=false
Categories=Development;Utility;
StartupWMClass=run_app.py
DESKTOPEOF
  command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  say "  $DESKTOP_DIR/claude-dashboard.desktop"
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
say "    claude-dashboard"
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

say "Other commands:"
say "    claude-dashboard --check              report the webview backend"
say "    claude-dashboard --headless --browser run without a native window"
say "    $0 --uninstall   remove it again"
