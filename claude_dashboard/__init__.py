"""Claude Code Dashboard - a local, offline viewer for ``~/.claude``.

The package is split into narrow modules:

``paths``     filesystem locations and the lossy project-name encoding
``config``    user configuration and the editable pricing table
``parser``    streaming JSONL reader for session transcripts
``indexer``   session index, on-disk cache and full-text search
``usage``     token / cost / tool aggregation over the index
``actions``   the few write operations (trash, resume, export)
``api``       FastAPI application serving the single-page UI
``server``    uvicorn on a random loopback port, in a background thread
``app``       pywebview bootstrap (native window, menu, tray)
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
