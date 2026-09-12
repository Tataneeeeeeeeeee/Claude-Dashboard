"""Optional system-tray icon.

Entirely optional: if ``pystray`` or a tray implementation is unavailable -
which is common on bare Wayland compositors - :func:`start_tray` returns
``None`` and the application carries on without one.  Nothing here may raise
into the caller.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

__all__ = ["start_tray", "TrayHandle"]


class TrayHandle:
    """Thin wrapper so callers do not need to know about pystray."""

    def __init__(self, icon, thread: threading.Thread) -> None:
        self._icon = icon
        self._thread = thread

    def stop(self) -> None:
        """Remove the tray icon; safe to call more than once."""
        try:
            self._icon.stop()
        except Exception:
            pass


def start_tray(
    icon_path: Path,
    on_open: Callable[[], None],
    on_refresh: Callable[[], None],
    on_quit: Callable[[], None],
    title: str = "Claude Code Dashboard",
) -> Optional[TrayHandle]:
    """Run a tray icon on its own thread.

    Returns ``None`` when a tray cannot be created, so the caller can simply
    fall back to "closing the window quits".
    """
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return None

    try:
        image = Image.open(icon_path)
    except (OSError, ValueError):
        return None

    menu = pystray.Menu(
        pystray.MenuItem("Open", lambda *_: on_open(), default=True),
        pystray.MenuItem("Refresh", lambda *_: on_refresh()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda *_: on_quit()),
    )
    try:
        icon = pystray.Icon("claude-dashboard", image, title, menu)
    except Exception:
        return None

    def run() -> None:
        """Block on the tray event loop, swallowing backend failures."""
        try:
            icon.run()
        except Exception:
            pass

    thread = threading.Thread(target=run, name="claude-dashboard-tray", daemon=True)
    thread.start()
    return TrayHandle(icon, thread)
