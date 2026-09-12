"""Filesystem locations used by the dashboard.

Two roots matter:

``claude_home()``     ``~/.claude`` - read-only except for the explicit
                      operations in :mod:`claude_dashboard.actions`.
``app_home()``        ``~/.claude-dashboard`` - everything this app owns
                      (config, cache, trash, backups, notes).

The module also holds :func:`decode_project_dir`, the best-effort inverse of
Claude Code's project-directory encoding.  See ``SCHEMA.md`` section 2: the
encoding replaces ``/``, ``.`` and ``_`` with ``-`` and is therefore *not*
reversible.  The real ``cwd`` recorded inside each transcript is always
preferred; this decode is only a fallback for unreadable files.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "claude_home",
    "claude_projects_dir",
    "claude_json_path",
    "app_home",
    "config_path",
    "cache_path",
    "trash_dir",
    "backup_dir",
    "encode_project_dir",
    "decode_project_dir",
    "is_within",
    "human_size",
]


def _env_path(var: str, default: Path) -> Path:
    """Return ``$var`` as a path when set, else *default*."""
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else default


def claude_home() -> Path:
    """Root of the Claude Code data directory (``~/.claude``)."""
    return _env_path("CLAUDE_DASHBOARD_CLAUDE_HOME", Path.home() / ".claude")


def claude_projects_dir() -> Path:
    """Directory holding one sub-directory of transcripts per project."""
    return claude_home() / "projects"


def claude_json_path() -> Path:
    """Global Claude Code state file (``~/.claude.json``)."""
    override = os.environ.get("CLAUDE_DASHBOARD_CLAUDE_HOME")
    if override:
        return Path(override).expanduser().parent / ".claude.json"
    return Path.home() / ".claude.json"


def app_home() -> Path:
    """Directory owned by this application, created on demand."""
    path = _env_path("CLAUDE_DASHBOARD_HOME", Path.home() / ".claude-dashboard")
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    """Where the user-editable configuration lives."""
    return app_home() / "config.json"


def cache_path() -> Path:
    """Where the session index cache is persisted."""
    return app_home() / "cache.json"


def trash_dir() -> Path:
    """Root of the recoverable trash (one timestamped batch per deletion)."""
    return app_home() / "trash"


def backup_dir() -> Path:
    """Timestamped backups taken before editing any ``CLAUDE.md``."""
    return app_home() / "backups"


def encode_project_dir(cwd: str) -> str:
    """Encode an absolute path the way Claude Code names project directories.

    ``/home/a/my_app.v2`` becomes ``-home-a-my-app-v2``.  Verified against all
    53 transcripts on the reference install.
    """
    out = cwd
    for ch in "/._":
        out = out.replace(ch, "-")
    return out


def decode_project_dir(name: str) -> str:
    """Best-effort inverse of :func:`encode_project_dir`.

    The encoding is lossy, so this can only guess: every ``-`` becomes ``/``.
    Used solely when no transcript in the directory yields a usable ``cwd``.
    """
    if not name:
        return name
    return "/" + name.lstrip("-").replace("-", "/")


def is_within(child: Path, parent: Path) -> bool:
    """True when *child* resolves to a location strictly inside *parent*.

    Both sides are fully resolved first, so symlinks pointing outside *parent*
    are rejected.  Used as the guard on every destructive operation.
    """
    try:
        child_r = child.resolve()
        parent_r = parent.resolve()
    except OSError:
        return False
    if child_r == parent_r:
        return False
    return parent_r in child_r.parents


def human_size(num: float) -> str:
    """Format a byte count for display (``1.4 MB``)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"
