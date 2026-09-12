"""The few operations that touch the filesystem or launch a process.

Everything the dashboard does is read-only except what is in this module,
and each of those operations is deliberately narrow:

* :func:`move_to_trash` relocates transcripts into
  ``~/.claude-dashboard/trash``.  Nothing is ever unlinked from
  ``~/.claude``; deletion is a move, and :func:`restore_batch` puts it back.
* :func:`launch_terminal`, :func:`open_folder` and :func:`open_in_editor`
  start an external process.

Every destructive call goes through :func:`validate_transcript_path`, which
refuses anything that is not a plain ``.jsonl`` file living strictly inside
``~/.claude/projects``.  Symlinks are rejected outright rather than followed,
so a link planted inside the projects tree cannot be used to reach a file
outside it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .config import load_config
from .paths import claude_projects_dir, is_within, trash_dir

__all__ = [
    "SafetyError",
    "validate_transcript_path",
    "TrashBatch",
    "move_to_trash",
    "list_trash",
    "restore_batch",
    "purge_trash",
    "delete_batch_permanently",
    "resume_command_string",
    "build_terminal_commands",
    "launch_terminal",
    "open_folder",
    "open_in_editor",
    "editor_available",
]

#: A session id must look like the uuid Claude Code uses for a filename.
#: Anything else is refused before it can reach a shell command.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Files that must never be touched even if something else slips through.
PROTECTED_NAMES = frozenset(
    {
        "settings.json",
        "settings.local.json",
        ".claude.json",
        "CLAUDE.md",
        ".credentials.json",
        "history.jsonl",
        "installed_plugins.json",
        "known_marketplaces.json",
    }
)


class SafetyError(Exception):
    """Raised when an operation would touch something it must not.

    The message is written for the user, because it is surfaced verbatim in
    the interface as a toast.
    """


# --------------------------------------------------------------- validation

def validate_transcript_path(candidate: Path | str, projects_dir: Path | None = None) -> Path:
    """Return *candidate* as a safe, absolute transcript path.

    The checks, in order:

    1. the textual path contains no ``..`` component;
    2. neither the file nor any parent up to the projects directory is a
       symlink;
    3. the resolved path lies strictly inside the resolved projects
       directory;
    4. it is an existing regular file ending in ``.jsonl``;
    5. its name is not one of :data:`PROTECTED_NAMES`.

    :raises SafetyError: with a message fit to show the user.
    """
    root = (projects_dir or claude_projects_dir())
    try:
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"The projects directory is unreadable: {root} ({exc})") from exc

    path = Path(candidate)
    if ".." in path.parts:
        raise SafetyError(f"Refusing a path containing '..': {candidate}")
    if not path.is_absolute():
        path = root_resolved / path

    if path.is_symlink():
        raise SafetyError(f"Refusing to act on a symlink: {path}")

    # Walk the parents too: a symlinked directory could otherwise redirect
    # the move outside the tree even though the leaf itself is a real file.
    for parent in path.parents:
        if parent == root_resolved or root_resolved in parent.parents:
            if parent.is_symlink():
                raise SafetyError(f"Refusing a path through a symlinked directory: {parent}")
        if parent == root_resolved:
            break

    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SafetyError(f"No such transcript: {path}") from exc
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"Cannot resolve {path}: {exc}") from exc

    if not is_within(resolved, root_resolved):
        raise SafetyError(
            f"Refusing to touch anything outside {root_resolved}: {resolved}"
        )
    if not resolved.is_file():
        raise SafetyError(f"Not a regular file: {resolved}")
    if resolved.suffix != ".jsonl":
        raise SafetyError(f"Only .jsonl transcripts can be removed, not {resolved.name}")
    if resolved.name in PROTECTED_NAMES:
        raise SafetyError(f"{resolved.name} is a configuration file and is protected")
    # A transcript always sits one level below the projects root.
    if resolved.parent.parent != root_resolved:
        raise SafetyError(
            f"Expected <projects>/<project>/<session>.jsonl, got {resolved}"
        )
    return resolved


def _safe_session_id(session_id: str) -> str:
    """Validate a session id before it reaches a command line."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise SafetyError(f"Refusing an unsafe session id: {session_id!r}")
    return session_id


# -------------------------------------------------------------------- trash

@dataclass
class TrashBatch:
    """One deletion, recoverable until it is purged."""

    batch_id: str
    deleted_at: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def total_bytes(self) -> int:
        """Combined size of every file in the batch."""
        return sum(int(item.get("size", 0)) for item in self.items)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the manifest and the HTTP API."""
        return {
            "batch_id": self.batch_id,
            "deleted_at": self.deleted_at,
            "items": self.items,
            "note": self.note,
            "total_bytes": self.total_bytes,
            "count": len(self.items),
        }


def _batch_dir(batch_id: str) -> Path:
    """Directory holding one trash batch, validated against traversal."""
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", batch_id or ""):
        raise SafetyError(f"Not a valid trash batch id: {batch_id!r}")
    return trash_dir() / batch_id


def move_to_trash(
    paths: Sequence[Path | str],
    metadata: Dict[str, Dict[str, Any]] | None = None,
    note: str = "",
) -> TrashBatch:
    """Move transcripts into a new timestamped trash batch.

    Every path is validated first and the whole batch is rejected if any one
    of them fails, so a bad entry cannot cause a partial deletion.

    :param metadata: optional per-session details (title, project) recorded
        in the manifest so the restore list stays readable.
    :raises SafetyError: if the list is empty or any path is unsafe.
    """
    if not paths:
        raise SafetyError("Nothing selected to delete")

    validated: List[Path] = []
    seen: set[Path] = set()
    for candidate in paths:
        resolved = validate_transcript_path(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        validated.append(resolved)

    stamp = datetime.now(timezone.utc)
    batch_id = f"{stamp.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"
    target = _batch_dir(batch_id)
    files_root = target / "files"
    files_root.mkdir(parents=True, exist_ok=False)

    batch = TrashBatch(batch_id=batch_id, deleted_at=stamp.isoformat(), note=note)
    moved: List[tuple[Path, Path]] = []
    try:
        for source in validated:
            project = source.parent.name
            destination_dir = files_root / project
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / source.name
            try:
                stat = source.stat()
                size, mtime = stat.st_size, stat.st_mtime
            except OSError:
                size, mtime = 0, 0.0
            # move, never copy-then-unlink: on the same filesystem this is
            # atomic, and across filesystems shutil falls back safely.
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
            extra = (metadata or {}).get(source.stem, {})
            batch.items.append(
                {
                    "session_id": source.stem,
                    "original_path": str(source),
                    "project_dir": project,
                    "stored_path": str(destination),
                    "size": size,
                    "mtime": mtime,
                    "title": extra.get("title", ""),
                    "project_path": extra.get("project_path", ""),
                    "message_count": extra.get("message_count"),
                }
            )
    except Exception as exc:
        # Undo whatever already moved, so a failure never leaves the tree
        # half-deleted.
        for source, destination in reversed(moved):
            try:
                shutil.move(str(destination), str(source))
            except OSError:
                pass
        shutil.rmtree(target, ignore_errors=True)
        raise SafetyError(f"Deletion aborted and rolled back: {exc}") from exc

    (target / "manifest.json").write_text(
        json.dumps(batch.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return batch


def list_trash() -> List[TrashBatch]:
    """Every recoverable batch, newest first.

    A batch whose manifest is missing or corrupt is reported with whatever
    files are still on disk, rather than hidden.
    """
    root = trash_dir()
    if not root.is_dir():
        return []
    batches: List[TrashBatch] = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        manifest = entry / "manifest.json"
        if manifest.is_file():
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
                batches.append(
                    TrashBatch(
                        batch_id=raw.get("batch_id", entry.name),
                        deleted_at=raw.get("deleted_at", ""),
                        items=raw.get("items", []) if isinstance(raw.get("items"), list) else [],
                        note=raw.get("note", ""),
                    )
                )
                continue
            except (OSError, ValueError):
                pass
        recovered = [
            {
                "session_id": f.stem,
                "original_path": "",
                "project_dir": f.parent.name,
                "stored_path": str(f),
                "size": f.stat().st_size if f.exists() else 0,
                "title": "(manifest lost)",
            }
            for f in (entry / "files").rglob("*.jsonl")
        ]
        batches.append(
            TrashBatch(batch_id=entry.name, deleted_at="", items=recovered, note="manifest unreadable")
        )
    return batches


def restore_batch(batch_id: str, session_ids: Iterable[str] | None = None) -> Dict[str, Any]:
    """Move files from a trash batch back to where they came from.

    The destination is validated the same way a deletion is, so a tampered
    manifest cannot write outside the projects tree.  A file whose original
    path is occupied is skipped rather than overwritten.
    """
    target = _batch_dir(batch_id)
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        raise SafetyError(f"No manifest for trash batch {batch_id}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SafetyError(f"Trash manifest is unreadable: {exc}") from exc

    wanted = set(session_ids) if session_ids is not None else None
    root = claude_projects_dir().resolve()
    restored: List[str] = []
    skipped: List[Dict[str, str]] = []
    remaining: List[Dict[str, Any]] = []

    for item in manifest.get("items", []):
        session_id = item.get("session_id", "")
        if wanted is not None and session_id not in wanted:
            remaining.append(item)
            continue

        stored = Path(item.get("stored_path", ""))
        original = Path(item.get("original_path", ""))
        if not stored.is_file():
            skipped.append({"session_id": session_id, "reason": "file missing from the trash"})
            continue
        if not is_within(stored, trash_dir().resolve()):
            skipped.append({"session_id": session_id, "reason": "stored outside the trash"})
            remaining.append(item)
            continue
        if ".." in original.parts or not is_within(original, root):
            skipped.append({"session_id": session_id, "reason": "original path is outside the projects tree"})
            remaining.append(item)
            continue
        if original.suffix != ".jsonl" or original.parent.parent != root:
            skipped.append({"session_id": session_id, "reason": "original path has an unexpected shape"})
            remaining.append(item)
            continue
        if original.exists():
            skipped.append({"session_id": session_id, "reason": "a file already exists at the original path"})
            remaining.append(item)
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stored), str(original))
            restored.append(session_id)
        except OSError as exc:
            skipped.append({"session_id": session_id, "reason": str(exc)})
            remaining.append(item)

    if remaining:
        manifest["items"] = remaining
        manifest["count"] = len(remaining)
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        shutil.rmtree(target, ignore_errors=True)

    return {"batch_id": batch_id, "restored": restored, "skipped": skipped}


def delete_batch_permanently(batch_id: str) -> Dict[str, Any]:
    """Remove one trash batch for good.

    This is the only code path that actually unlinks a transcript, and it
    only ever operates inside ``~/.claude-dashboard/trash``.
    """
    target = _batch_dir(batch_id)
    if not target.is_dir():
        raise SafetyError(f"No such trash batch: {batch_id}")
    if not is_within(target, trash_dir().resolve()):
        raise SafetyError("Refusing to delete outside the trash directory")
    count = len(list(target.rglob("*.jsonl")))
    shutil.rmtree(target)
    return {"batch_id": batch_id, "removed_files": count}


def purge_trash(days: int | None = None) -> Dict[str, Any]:
    """Drop batches older than the configured retention.

    Returns what was removed so the interface can report it; a retention of
    zero or less disables purging entirely.
    """
    retention = days if days is not None else int(load_config().get("trash_retention_days", 30))
    if retention <= 0:
        return {"purged": [], "retention_days": retention, "disabled": True}
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
    purged: List[str] = []
    for batch in list_trash():
        stamp = batch.deleted_at
        when = None
        if stamp:
            try:
                when = datetime.fromisoformat(stamp)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except ValueError:
                when = None
        if when is None:
            # Fall back to the directory's own timestamp.
            try:
                when = datetime.fromtimestamp(
                    _batch_dir(batch.batch_id).stat().st_mtime, tz=timezone.utc
                )
            except (OSError, SafetyError):
                continue
        if when < cutoff:
            try:
                delete_batch_permanently(batch.batch_id)
                purged.append(batch.batch_id)
            except (SafetyError, OSError):
                continue
    return {"purged": purged, "retention_days": retention, "disabled": False}


# ----------------------------------------------------------------- terminal

def resume_command_string(session_id: str, config: Dict[str, Any] | None = None) -> str:
    """The ``claude --resume <id>`` command, as configured."""
    session_id = _safe_session_id(session_id)
    settings = (config or load_config()).get("terminal", {})
    template = settings.get("resume_command") or "claude --resume {session_id}"
    return str(template).replace("{session_id}", session_id)


def _quote_for_cmd(text: str) -> str:
    """Wrap a value for Windows ``cmd.exe``, which does not use POSIX rules."""
    return '"' + text.replace('"', '""') + '"'


def build_terminal_commands(
    cwd: str,
    session_id: str,
    config: Dict[str, Any] | None = None,
    platform: str | None = None,
) -> List[List[str]]:
    """Candidate argv lists for opening a terminal, in preference order.

    A user-supplied template in ``config["terminal"][<platform>]`` wins; it
    is a list of argv lists, each element supporting the placeholders
    ``{cwd}``, ``{session_id}`` and ``{command}``.  Otherwise the built-in
    per-platform detection is used.

    Nothing is ever passed through a shell, so a directory name containing
    spaces or quotes cannot turn into extra arguments.
    """
    settings = (config or load_config()).get("terminal", {})
    system = platform or sys.platform
    key = "windows" if system.startswith("win") else "darwin" if system == "darwin" else "linux"
    command = resume_command_string(session_id, config)

    custom = settings.get(key)
    if isinstance(custom, list) and custom:
        candidates: List[List[str]] = []
        rows = custom if isinstance(custom[0], list) else [custom]
        for row in rows:
            if not isinstance(row, list):
                continue
            candidates.append([
                str(part)
                .replace("{cwd}", cwd)
                .replace("{session_id}", session_id)
                .replace("{command}", command)
                for part in row
            ])
        if candidates:
            return candidates

    if key == "windows":
        return [
            # Windows Terminal, the modern default.
            ["wt.exe", "-d", cwd, "cmd", "/k", command],
            # Fall back to a plain console window.
            ["cmd.exe", "/c", "start", "", "cmd", "/k",
             f"cd /d {_quote_for_cmd(cwd)} && {command}"],
        ]
    if key == "darwin":
        script = (
            'tell application "Terminal"\n'
            f'  do script "cd {_shell_quote(cwd)} && {command}"\n'
            "  activate\n"
            "end tell"
        )
        iterm = (
            'tell application "iTerm"\n'
            "  create window with default profile\n"
            f'  tell current session of current window to write text "cd {_shell_quote(cwd)} && {command}"\n'
            "  activate\n"
            "end tell"
        )
        return [["osascript", "-e", script], ["osascript", "-e", iterm]]

    inner = f"cd {_shell_quote(cwd)} && {command}; exec ${{SHELL:-/bin/sh}}"
    return [
        ["x-terminal-emulator", "-e", "sh", "-c", inner],
        ["gnome-terminal", "--working-directory", cwd, "--", "sh", "-c", inner],
        ["konsole", "--workdir", cwd, "-e", "sh", "-c", inner],
        ["xfce4-terminal", "--working-directory", cwd, "-e", f"sh -c {_shell_quote(inner)}"],
        ["alacritty", "--working-directory", cwd, "-e", "sh", "-c", inner],
        ["kitty", "--directory", cwd, "sh", "-c", inner],
        ["wezterm", "start", "--cwd", cwd, "--", "sh", "-c", inner],
        ["foot", "--working-directory", cwd, "sh", "-c", inner],
        ["xterm", "-e", "sh", "-c", inner],
    ]


def _shell_quote(text: str) -> str:
    """POSIX single-quote a value for embedding in a ``sh -c`` string."""
    return "'" + text.replace("'", "'\\''") + "'"


def launch_terminal(
    cwd: str,
    session_id: str,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Open a terminal in *cwd* running the resume command.

    Each candidate is tried in turn until one starts.  The project directory
    must still exist: resuming into a directory that has been deleted would
    start Claude Code somewhere unexpected.
    """
    session_id = _safe_session_id(session_id)
    directory = Path(cwd) if cwd else None
    if directory is None or not directory.is_dir():
        raise SafetyError(
            f"The project folder no longer exists: {cwd or '(unknown)'}"
        )

    attempts: List[Dict[str, str]] = []
    for argv in build_terminal_commands(str(directory), session_id, config):
        program = argv[0]
        if shutil.which(program) is None and not Path(program).is_file():
            attempts.append({"program": program, "error": "not found on PATH"})
            continue
        try:
            subprocess.Popen(
                argv,
                cwd=str(directory),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=not sys.platform.startswith("win"),
            )
            return {
                "launched": True,
                "program": program,
                "argv": argv,
                "command": resume_command_string(session_id, config),
                "attempts": attempts,
            }
        except OSError as exc:
            attempts.append({"program": program, "error": str(exc)})

    tried = ", ".join(a["program"] for a in attempts) or "nothing"
    raise SafetyError(
        "Could not open a terminal. Tried: " + tried
        + ". Set terminal.<platform> in config.json to your preferred command."
    )


# ------------------------------------------------------------- open helpers

def open_folder(path: str) -> Dict[str, Any]:
    """Reveal a directory in the platform file manager."""
    directory = Path(path) if path else None
    if directory is None or not directory.is_dir():
        raise SafetyError(f"The folder no longer exists: {path or '(unknown)'}")
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(directory))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(directory)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(directory)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise SafetyError(f"Could not open the file manager: {exc}") from exc
    return {"opened": True, "path": str(directory)}


def editor_available(config: Dict[str, Any] | None = None) -> bool:
    """Whether the configured editor command is on the PATH."""
    command = (config or load_config()).get("editor_command") or "code"
    return shutil.which(str(command).split()[0]) is not None


def open_in_editor(path: str, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Open a directory in the configured editor, usually VS Code."""
    directory = Path(path) if path else None
    if directory is None or not directory.is_dir():
        raise SafetyError(f"The folder no longer exists: {path or '(unknown)'}")
    command = str((config or load_config()).get("editor_command") or "code")
    argv = command.split() + [str(directory)]
    if shutil.which(argv[0]) is None:
        raise SafetyError(
            f"{argv[0]!r} is not on your PATH. Change editor_command in config.json."
        )
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=not sys.platform.startswith("win"))
    except OSError as exc:
        raise SafetyError(f"Could not start {argv[0]}: {exc}") from exc
    return {"opened": True, "program": argv[0], "path": str(directory)}
