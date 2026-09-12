"""Application bootstrap: native window, native menu, tray, clean shutdown.

Startup order matters and is deliberate:

1. Bind an HTTP server to a random loopback port and wait until it answers.
2. Create the pywebview window pointing at that port, restoring the geometry
   saved on the previous run.
3. Hand control to pywebview's event loop on the **main thread**, which every
   supported GUI toolkit requires.

Shutdown is the mirror image.  Closing the window stops uvicorn, joins its
thread, removes the tray icon and lets the process exit; nothing is left
running.  ``POST /api/shutdown`` from the UI takes the same path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__
from .config import load_config
from .paths import app_home, claude_home
from .server import BackgroundServer
from .window import WindowState, load_window_state, save_window_state

__all__ = ["Application", "main"]

#: Wayland never tells a client its own position, and GTK reports (0, 0)
#: there.  Persisting that would reopen the window jammed in the corner.
POSITION_IS_KNOWABLE = not (
    os.environ.get("WAYLAND_DISPLAY")
    or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
)

def _assets_dir() -> Path:
    """Where the icon files live, in a checkout or in a frozen bundle."""
    bundled = os.environ.get("CLAUDE_DASHBOARD_BUNDLE")
    if bundled:
        candidate = Path(bundled) / "assets"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent / "assets"


ASSETS = _assets_dir()
ICON_PNG = ASSETS / "icon.png"
ICON_ICO = ASSETS / "icon.ico"

HELP_TEXT = (
    "Claude Code Dashboard {version}\n\n"
    "A local, offline viewer for the data Claude Code stores in ~/.claude.\n"
    "It makes no network calls and treats ~/.claude as read-only apart from "
    "the trash-move and CLAUDE.md edits you explicitly request.\n\n"
    "Keyboard\n"
    "  /          focus search\n"
    "  j / k      next / previous session\n"
    "  Enter      open the selected session\n"
    "  r          resume in a terminal\n"
    "  Delete     move to trash\n"
    "  1 2 3      sessions / usage / config\n"
    "  ?          show shortcuts\n"
)


def _icon_for_platform() -> Optional[str]:
    """Pick the icon file pywebview should use for this OS."""
    if sys.platform == "win32" and ICON_ICO.is_file():
        return str(ICON_ICO)
    if ICON_PNG.is_file():
        return str(ICON_PNG)
    return None


class Api:
    """Methods exposed to the page as ``window.pywebview.api.*``.

    Only things the browser cannot do itself live here: native dialogs, the
    OS clipboard when the webview blocks it, and process shutdown.
    """

    def __init__(self, application: "Application") -> None:
        self._app = application

    # -- window ---------------------------------------------------------

    def get_info(self) -> Dict[str, Any]:
        """Environment details the UI shows in its About panel."""
        return {
            "version": __version__,
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "server_url": self._app.server.url if self._app.server else "",
            "claude_home": str(claude_home()),
            "app_home": str(app_home()),
            "native": True,
        }

    def minimize(self) -> None:
        """Minimise the window, or hide it when tray mode is on."""
        window = self._app.window
        if window is None:
            return
        if load_config().get("minimize_to_tray") and self._app.tray is not None:
            window.hide()
        else:
            window.minimize()

    def toggle_maximize(self) -> None:
        """Toggle between maximised and restored."""
        window = self._app.window
        if window is None:
            return
        if self._app.is_maximized:
            window.restore()
        else:
            window.maximize()

    def quit(self) -> None:
        """Close the window, which tears the whole process down."""
        self._app.request_quit()

    # -- native dialogs -------------------------------------------------

    def confirm(self, title: str, message: str) -> bool:
        """Native yes/no dialog.  Returns ``False`` if none can be shown."""
        window = self._app.window
        if window is None:
            return False
        try:
            return bool(window.create_confirmation_dialog(str(title), str(message)))
        except Exception:
            return False

    def save_file(self, suggested_name: str, content: str, file_types: Any = None) -> Dict[str, Any]:
        """Native save dialog, then write *content* to the chosen path.

        Returns ``{"saved": False, "cancelled": True}`` when the user backs
        out, so the UI can stay silent instead of showing an error.
        """
        import webview

        window = self._app.window
        if window is None:
            return {"saved": False, "error": "no window"}
        types = tuple(file_types) if isinstance(file_types, (list, tuple)) else ()
        try:
            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(Path.home()),
                save_filename=str(suggested_name),
                file_types=types,
            )
        except Exception as exc:
            return {"saved": False, "error": f"{type(exc).__name__}: {exc}"}
        if not result:
            return {"saved": False, "cancelled": True}
        target = Path(result if isinstance(result, str) else result[0])
        try:
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return {"saved": False, "error": str(exc)}
        return {"saved": True, "path": str(target)}

    def choose_folder(self) -> Dict[str, Any]:
        """Native folder picker, used by the backup export."""
        import webview

        window = self._app.window
        if window is None:
            return {"chosen": False}
        try:
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:
            return {"chosen": False, "error": str(exc)}
        if not result:
            return {"chosen": False, "cancelled": True}
        return {"chosen": True, "path": result[0] if isinstance(result, (list, tuple)) else result}

    def open_external(self, url: str) -> bool:
        """Open a URL in the user's real browser rather than in the window."""
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return False
        try:
            webbrowser.open(url)
            return True
        except Exception:
            return False


class Application:
    """Owns the server, the window, the tray and the shutdown sequence."""

    def __init__(self, debug: bool = False, gui: str | None = None) -> None:
        self.debug = debug
        self.gui = gui
        self.server: Optional[BackgroundServer] = None
        self.window = None
        self.tray = None
        self.api = Api(self)
        self.is_maximized = False
        self._quitting = threading.Event()
        self._state = WindowState()

    # ---------------------------------------------------------- lifecycle

    def start(self) -> int:
        """Run the application.  Returns the process exit code."""
        import webview

        config = load_config()
        self._state = load_window_state(config)

        self.server = BackgroundServer()
        try:
            url = self.server.start()
        except (OSError, RuntimeError, TimeoutError) as exc:
            print(f"error: could not start the local server: {exc}", file=sys.stderr)
            return 1
        if self.debug:
            print(f"server listening on {url}")

        self.window = webview.create_window(
            "Claude Code Dashboard",
            url,
            js_api=self.api,
            width=self._state.width,
            height=self._state.height,
            x=self._state.x,
            y=self._state.y,
            min_size=self._state.min_size,
            maximized=self._state.maximized,
            background_color="#16181d",
            text_select=True,
            zoomable=True,
        )
        self.is_maximized = self._state.maximized
        self._wire_events()

        if config.get("minimize_to_tray"):
            self.tray = self._start_tray()

        # Watch for POST /api/shutdown coming from the page.
        threading.Thread(target=self._watch_shutdown, daemon=True).start()

        try:
            webview.start(
                gui=self.gui,
                debug=self.debug,
                menu=self._build_menu(),
                icon=_icon_for_platform(),
                private_mode=False,
                storage_path=str(app_home() / "webview"),
            )
        except Exception as exc:
            print(f"error: could not open a native window: {exc}", file=sys.stderr)
            self._teardown()
            return 2
        self._teardown()
        return 0

    def _wire_events(self) -> None:
        """Persist geometry as it changes and quit cleanly on close."""
        window = self.window
        if window is None:
            return

        def on_resized(width: int, height: int) -> None:
            """Remember the size, but only while not maximised."""
            if not self.is_maximized:
                self._state.width, self._state.height = int(width), int(height)

        def on_moved(x: int, y: int) -> None:
            """Remember the position when the platform actually reports one.

            Skipped while maximised, on Wayland, and for a bare (0, 0),
            which is what a toolkit emits when it does not know.
            """
            if self.is_maximized or not POSITION_IS_KNOWABLE:
                return
            if int(x) == 0 and int(y) == 0:
                return
            self._state.x, self._state.y = int(x), int(y)

        def on_maximized() -> None:
            self.is_maximized = True
            self._state.maximized = True

        def on_restored() -> None:
            self.is_maximized = False
            self._state.maximized = False

        def on_closing() -> bool:
            """Hide to the tray if configured, otherwise allow the close."""
            if (
                not self._quitting.is_set()
                and self.tray is not None
                and load_config().get("minimize_to_tray")
            ):
                window.hide()
                return False
            save_window_state(self._state)
            return True

        window.events.resized += on_resized
        window.events.moved += on_moved
        window.events.maximized += on_maximized
        window.events.restored += on_restored
        window.events.closing += on_closing

    def _watch_shutdown(self) -> None:
        """Turn ``POST /api/shutdown`` into a real window close."""
        if self.server is None:
            return
        self.server.shutdown_event.wait()
        self.request_quit()

    def request_quit(self) -> None:
        """Begin an orderly shutdown from any thread."""
        if self._quitting.is_set():
            return
        self._quitting.set()
        save_window_state(self._state)
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        window = self.window
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def _teardown(self) -> None:
        """Stop the HTTP server and join its thread."""
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        if self.server is not None:
            self.server.stop()
            self.server = None

    # --------------------------------------------------------------- tray

    def _start_tray(self):
        """Create the tray icon, or return ``None`` if unsupported."""
        from .tray import start_tray

        def on_open() -> None:
            """Bring the window back from the tray."""
            if self.window is not None:
                self.window.show()
                self.window.restore()

        def on_refresh() -> None:
            """Ask the page to reload its data."""
            self._call_js("window.dashboard && window.dashboard.refresh()")

        return start_tray(ICON_PNG, on_open, on_refresh, self.request_quit)

    # --------------------------------------------------------------- menu

    def _call_js(self, script: str) -> None:
        """Run JavaScript in the page, ignoring a not-yet-loaded window."""
        window = self.window
        if window is None:
            return
        try:
            window.evaluate_js(script)
        except Exception:
            pass

    def _build_menu(self) -> list:
        """The native File / View / Help menu bar."""
        from webview.menu import Menu, MenuAction, MenuSeparator

        def view(name: str):
            """Return a handler switching the page to *name*."""
            return lambda: self._call_js(
                f"window.dashboard && window.dashboard.setView({json.dumps(name)})"
            )

        def js(script: str):
            """Return a handler running *script* in the page."""
            return lambda: self._call_js(script)

        def export(kind: str):
            """Return a handler triggering an export from the page."""
            return lambda: self._call_js(
                f"window.dashboard && window.dashboard.exportCurrent({json.dumps(kind)})"
            )

        def show_about() -> None:
            """Native dialog with version and shortcut reference."""
            window = self.window
            if window is None:
                return
            try:
                window.create_confirmation_dialog(
                    "About Claude Code Dashboard", HELP_TEXT.format(version=__version__)
                )
            except Exception:
                pass

        def open_app_folder() -> None:
            """Reveal ~/.claude-dashboard in the system file manager."""
            self._reveal(app_home())

        return [
            Menu(
                "File",
                [
                    MenuAction("Refresh index", js("window.dashboard && window.dashboard.rebuild()")),
                    MenuSeparator(),
                    MenuAction("Export conversation as Markdown...", export("markdown")),
                    MenuAction("Export conversation as HTML...", export("html")),
                    MenuAction("Export conversation as JSON...", export("json")),
                    MenuAction("Export usage statistics as CSV...", export("csv")),
                    MenuSeparator(),
                    MenuAction("Open dashboard data folder", open_app_folder),
                    MenuSeparator(),
                    MenuAction("Quit", self.request_quit),
                ],
            ),
            Menu(
                "View",
                [
                    MenuAction("Sessions", view("sessions")),
                    MenuAction("Usage dashboard", view("usage")),
                    MenuAction("Configuration", view("config")),
                    MenuSeparator(),
                    MenuAction("Focus search", js("window.dashboard && window.dashboard.focusSearch()")),
                    MenuAction("Toggle theme", js("window.dashboard && window.dashboard.cycleTheme()")),
                    MenuSeparator(),
                    MenuAction("Reload UI", js("window.location.reload()")),
                ],
            ),
            Menu(
                "Help",
                [
                    MenuAction("Keyboard shortcuts", js("window.dashboard && window.dashboard.showShortcuts()")),
                    MenuAction("About", show_about),
                ],
            ),
        ]

    @staticmethod
    def _reveal(path: Path) -> None:
        """Open *path* in the platform file manager."""
        import subprocess

        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass


def _wait_for_server(url: str, timeout: float = 20.0) -> bool:
    """Poll ``/api/health`` until it answers or *timeout* elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
                if json.loads(response.read()).get("ok"):
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.1)
    return False


def run_headless(open_browser: bool = False) -> int:
    """Serve the UI without a native window.

    A fallback for machines with no webview backend installed, and the mode
    the test suite and ``--check`` use.  It prints the URL and blocks.
    """
    server = BackgroundServer()
    url = server.start()
    print(f"Claude Code Dashboard {__version__}")
    print(f"serving on {url}  (no native window; press Ctrl+C to stop)")
    if not _wait_for_server(url):
        print("warning: the server did not answer /api/health", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.shutdown_event.wait()
    except KeyboardInterrupt:
        print()
    finally:
        server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        prog="claude-dashboard",
        description="Offline desktop dashboard for the data Claude Code stores in ~/.claude.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--debug", action="store_true", help="open the webview inspector")
    parser.add_argument(
        "--gui",
        default=None,
        help="force a pywebview backend (gtk, qt, edgechromium, cocoa)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="serve the UI without opening a native window",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="with --headless, open the default browser at the served URL",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether a native window backend is available, then exit",
    )
    args = parser.parse_args(argv)

    if args.check:
        return _check_backend()
    if args.headless:
        return run_headless(open_browser=args.browser)

    try:
        import importlib.util

        if importlib.util.find_spec("webview") is None:
            raise ImportError("webview")
    except ImportError:
        print(
            "error: pywebview is not installed.\n"
            "       install the dependencies with:  pip install -r requirements.txt\n"
            "       or run without a window:        python -m claude_dashboard.app --headless",
            file=sys.stderr,
        )
        return 3
    return Application(debug=args.debug, gui=args.gui).start()


def _check_backend() -> int:
    """Print which webview backend is usable and how to install one."""
    print(f"Claude Code Dashboard {__version__}")
    print(f"python      {sys.version.split()[0]}  ({sys.platform})")
    try:
        from importlib.metadata import version

        print(f"pywebview   {version('pywebview')}")
    except Exception:
        print("pywebview   NOT INSTALLED")
        print("\ninstall it with:  pip install -r requirements.txt")
        return 1

    import importlib

    backends = {
        "win32": [("edgechromium", "webview.platforms.edgechromium")],
        "darwin": [("cocoa", "webview.platforms.cocoa")],
    }.get(sys.platform, [("gtk", "webview.platforms.gtk"), ("qt", "webview.platforms.qt")])

    usable = []
    for name, module in backends:
        try:
            importlib.import_module(module)
            usable.append(name)
            print(f"backend     {name}: available")
        except Exception as exc:
            print(f"backend     {name}: unavailable ({type(exc).__name__}: {exc})")

    if usable:
        print(f"\nready: the window will use '{usable[0]}'")
        return 0

    print("\nNo native window backend is available.")
    if sys.platform.startswith("linux"):
        print("  Arch      sudo pacman -S --needed webkit2gtk-4.1 python-gobject")
        print("  Debian    sudo apt install python3-gi gir1.2-webkit2-4.1")
        print("  Fedora    sudo dnf install python3-gobject webkit2gtk4.1")
        print("  then recreate the venv with:  python -m venv --system-site-packages .venv")
    print("\nMeanwhile you can run:  python -m claude_dashboard.app --headless --browser")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
