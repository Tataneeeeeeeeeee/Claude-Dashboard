"""Uvicorn on a random loopback port, running in a background thread.

The port is chosen by binding ``127.0.0.1:0`` and reading back what the OS
assigned, then handing the already-bound socket to uvicorn.  That removes the
race between "find a free port" and "bind it", and guarantees the server is
never reachable from another machine.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional

import uvicorn

from .api import create_app

__all__ = ["BackgroundServer"]


class BackgroundServer:
    """Owns the uvicorn instance and its thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, log_level: str = "warning") -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                f"refusing to bind {host!r}: the dashboard only ever listens on loopback"
            )
        self.host = host
        self._requested_port = port
        self.port: int = 0
        self.app = create_app()
        self._socket: Optional[socket.socket] = None
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._log_level = log_level

    @property
    def url(self) -> str:
        """Base URL of the running server."""
        return f"http://{self.host}:{self.port}"

    @property
    def shutdown_event(self) -> threading.Event:
        """Set when the UI asks the process to exit via ``POST /api/shutdown``."""
        return self.app.state.shutdown_event

    def start(self, timeout: float = 20.0) -> str:
        """Bind, launch the thread and block until the server accepts requests."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.host, self._requested_port))
        self._socket.listen(128)
        self.port = self._socket.getsockname()[1]

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level=self._log_level,
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        # uvicorn installs signal handlers only on the main thread.
        self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

        def run() -> None:
            assert self._server is not None and self._socket is not None
            self._server.run(sockets=[self._socket])

        self._thread = threading.Thread(target=run, name="agentboard-http", daemon=True)
        self._thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._server.started:
                return self.url
            if not self._thread.is_alive():
                raise RuntimeError("the HTTP server thread exited during startup")
            time.sleep(0.02)
        raise TimeoutError(f"server did not start within {timeout:.0f}s")

    def stop(self, timeout: float = 5.0) -> None:
        """Ask uvicorn to exit and wait for the thread to finish."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
