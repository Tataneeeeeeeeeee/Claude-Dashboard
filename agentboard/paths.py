"""Filesystem locations used by the dashboard.

Two roots matter:

``claude_home()``     ``~/.claude`` - read-only except for the explicit
                      operations in :mod:`agentboard.actions`.
``app_home()``        ``~/.agentboard`` - everything this app owns
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
    "env",
    "migrate_legacy_home",
    "config_path",
    "cache_path",
    "trash_dir",
    "backup_dir",
    "encode_project_dir",
    "decode_project_dir",
    "is_within",
    "human_size",
]


#: Environment variables from before the project was renamed, still honoured
#: so existing scripts and shells keep working.
LEGACY_ENV = {
    "AGENTBOARD_HOME": "CLAUDE_DASHBOARD_HOME",
    "AGENTBOARD_CLAUDE_HOME": "CLAUDE_DASHBOARD_CLAUDE_HOME",
    "AGENTBOARD_BUNDLE": "CLAUDE_DASHBOARD_BUNDLE",
}

#: Where this application kept its data before the rename.
LEGACY_APP_DIR = ".claude-dashboard"


def env(var: str) -> str | None:
    """Read ``$var``, falling back to its pre-rename name."""
    value = os.environ.get(var)
    if value:
        return value
    legacy = LEGACY_ENV.get(var)
    if not legacy:
        return None
    return os.environ.get(legacy) or None


def _env_path(var: str, default: Path) -> Path:
    """Return ``$var`` as a path when set, else *default*."""
    raw = env(var)
    return Path(raw).expanduser() if raw else default


def claude_home() -> Path:
    """Root of the Claude Code data directory (``~/.claude``).

    ``AGENTBOARD_CLAUDE_HOME`` wins, then Claude Code's own
    ``CLAUDE_CONFIG_DIR``, then the default.
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    default = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return _env_path("AGENTBOARD_CLAUDE_HOME", default)


def claude_projects_dir() -> Path:
    """Directory holding one sub-directory of transcripts per project."""
    return claude_home() / "projects"


def claude_json_path() -> Path:
    """Global Claude Code state file (``~/.claude.json``)."""
    override = env("AGENTBOARD_CLAUDE_HOME")
    if override:
        return Path(override).expanduser().parent / ".claude.json"
    return Path.home() / ".claude.json"


def app_home() -> Path:
    """Directory owned by this application, created on demand.

    The first run after the rename moves ``~/.claude-dashboard`` here, so
    config, annotations, cache, trash and backups carry over.
    """
    override = env("AGENTBOARD_HOME")
    if override:
        path = Path(override).expanduser()
    else:
        path = Path.home() / ".agentboard"
        migrate_legacy_home(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy_home(target: Path, legacy: Path | None = None) -> bool:
    """Move the pre-rename data directory to *target* if only it exists.

    A rename, not a copy: on the same filesystem it is atomic, and nothing
    is ever deleted.  Returns ``True`` when a move happened.
    """
    source = legacy if legacy is not None else Path.home() / LEGACY_APP_DIR
    if target.exists() or not source.is_dir() or source.is_symlink():
        return False
    try:
        source.rename(target)
    except OSError:
        return False
    return True


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
