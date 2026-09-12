#!/usr/bin/env python3
"""Entry point for the packaged executable and for `python run_app.py`.

PyInstaller needs a plain script rather than a module, and the frozen
build has to be told where its bundled data went.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def _bundle_root() -> Path | None:
    """The directory PyInstaller unpacked data into, when frozen."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return None


def main() -> int:
    """Locate bundled assets, then hand over to the application."""
    root = _bundle_root()
    if root is not None:
        # The package is importable from the bundle; its web directory and
        # the icons live beside it rather than next to a source checkout.
        sys.path.insert(0, str(root))
        os.environ.setdefault("CLAUDE_DASHBOARD_BUNDLE", str(root))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))

    from claude_dashboard.app import main as run

    return run()


if __name__ == "__main__":
    # A frozen build may re-exec itself; without this a windowed binary can
    # spawn extra copies of the whole application on Windows.
    multiprocessing.freeze_support()
    raise SystemExit(main())
