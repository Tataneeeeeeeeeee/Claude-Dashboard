"""Live updates: watch each provider's history and refresh the index.

AI tools append to a transcript continuously while a session is open,
so a naive watcher would re-scan a 30 MB file on every write.  Events are
therefore coalesced: a path that changes is queued, and a worker re-scans
it only once the writes have paused for ``settle_seconds``.

``watchdog`` is optional.  If it is missing, :func:`start_watcher` returns
``None`` and the application simply relies on the manual Refresh.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Set

__all__ = ["FileWatcher", "start_watcher"]


class FileWatcher:
    """Coalescing watcher over one provider's history directory."""

    def __init__(
        self,
        root: Path,
        on_change: Callable[[Set[str]], None],
        settle_seconds: float = 1.5,
        suffixes: Sequence[str] = (".jsonl",),
    ) -> None:
        self.root = Path(root)
        self.suffixes = tuple(suffixes)
        self.on_change = on_change
        self.settle_seconds = settle_seconds
        self._pending: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = None
        self._worker: Optional[threading.Thread] = None
        self.events_seen = 0

    # ------------------------------------------------------------ events

    def note(self, path: str) -> None:
        """Record that *path* changed; the worker debounces the rest."""
        if not path.endswith(self.suffixes):
            return
        with self._lock:
            self.events_seen += 1
            self._pending[path] = time.monotonic()

    def _drain(self) -> None:
        """Re-scan paths that have stopped changing."""
        while not self._stop.is_set():
            self._stop.wait(0.4)
            now = time.monotonic()
            ready: Set[str] = set()
            with self._lock:
                for path, stamp in list(self._pending.items()):
                    if now - stamp >= self.settle_seconds:
                        ready.add(path)
                        self._pending.pop(path, None)
            if ready:
                try:
                    self.on_change(ready)
                except Exception:
                    # A refresh failure must never kill the watcher.
                    pass

    # --------------------------------------------------------- lifecycle

    def start(self) -> bool:
        """Begin watching. Returns ``False`` when watchdog is unavailable."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return False
        if not self.root.is_dir():
            return False

        watcher = self

        class Handler(FileSystemEventHandler):
            """Forward every transcript event to the coalescing queue."""

            def on_any_event(self, event) -> None:
                if getattr(event, "is_directory", False):
                    return
                for attribute in ("src_path", "dest_path"):
                    path = getattr(event, attribute, None)
                    if isinstance(path, str):
                        watcher.note(path)

        try:
            self._observer = Observer()
            self._observer.schedule(Handler(), str(self.root), recursive=True)
            self._observer.start()
        except Exception:
            self._observer = None
            return False

        self._worker = threading.Thread(
            target=self._drain, name=f"agentboard-watch-{self.root.name}", daemon=True
        )
        self._worker.start()
        return True

    def stop(self) -> None:
        """Stop watching and join the worker."""
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                pass
            self._observer = None
        if self._worker is not None:
            self._worker.join(timeout=3)
            self._worker = None


def start_watcher(
    root: Path,
    on_change: Callable[[Set[str]], None],
    suffixes: Sequence[str] = (".jsonl",),
) -> Optional[FileWatcher]:
    """Create and start a watcher, or return ``None`` if unavailable."""
    watcher = FileWatcher(root, on_change, suffixes=suffixes)
    return watcher if watcher.start() else None
