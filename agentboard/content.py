"""Readers for the rest of ``~/.claude``: instructions, assets and todos.
Four things live here:
* ``CLAUDE.md`` discovery, reading and editing.  This is the only place the
  dashboard writes into files it did not create, so the write path is as
  narrow as the deletion guard: the target must literally be named
  ``CLAUDE.md``, must not be a symlink, and must sit either at the root of
  ``~/.claude`` or inside a project directory the index already knows.  A
  timestamped copy is taken before every save.
* Skills, commands and agents, wherever Claude Code keeps them: the
  top-level ``commands/`` and ``agents/`` folders and the plugin tree.
* The todo lists under ``~/.claude/todos``.
* A ZIP backup of the whole ``projects`` tree.
Every one of these directories is optional.  On the reference install
``todos/``, ``commands/``, ``agents/`` and the global ``CLAUDE.md`` do not
exist at all, so each reader returns an empty result rather than raising,
and the interface shows an empty state.
"""

from __future__ import annotations
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence
from .actions import SafetyError
from .paths import backup_dir, claude_home, claude_projects_dir, is_within

__all__ = [
    "MAX_TEXT_BYTES",
    "find_claude_md",
    "validate_claude_md_path",
    "save_claude_md",
    "list_claude_md_backups",
    "list_assets",
    "read_asset",
    "read_todos",
    "backup_projects",
]

#: Text files larger than this are truncated rather than sent to the UI.
MAX_TEXT_BYTES = 512 * 1024

#: Names accepted by the editor. Nothing else is ever written.
EDITABLE_NAMES = frozenset({"CLAUDE.md", "CLAUDE.local.md"})


def _read_head(path: Path, limit: int | None = None) -> Dict[str, Any]:
    """Read a text file defensively, reporting truncation and errors.

    The limit is resolved at call time rather than bound as a default, so
    :data:`MAX_TEXT_BYTES` stays a real knob.
    """
    limit = MAX_TEXT_BYTES if limit is None else limit
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"content": "", "error": str(exc), "size": 0, "truncated": False}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(limit)
    except OSError as exc:
        return {"content": "", "error": str(exc), "size": size, "truncated": False}
    return {
        "content": content,
        "error": None,
        "size": size,
        "truncated": size > limit,
    }


# ------------------------------------------------------------- CLAUDE.md

def find_claude_md(project_paths: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Locate the global and per-project instruction files.
    *project_paths* comes from the session index, so only directories the
    user has actually worked in are examined; the filesystem is never
    walked looking for them.
    """
    found: List[Dict[str, Any]] = []
    seen: set[Path] = set()
    for name in ("CLAUDE.md", "CLAUDE.local.md"):
        candidate = claude_home() / name
        if candidate.is_file():
            seen.add(candidate.resolve())
            stat = candidate.stat()
            found.append({
                "path": str(candidate),
                "scope": "global",
                "project": str(claude_home()),
                "name": name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "exists": True,
            })
    for project in project_paths:
        if not project:
            continue
        root = Path(project)
        if not root.is_dir():
            continue
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
            candidate = root / name
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            stat = candidate.stat()
            found.append({
                "path": str(candidate),
                "scope": "project",
                "project": str(root),
                "name": candidate.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "exists": True,
            })
    found.sort(key=lambda entry: (entry["scope"] != "global", entry["project"]))
    return found


def validate_claude_md_path(
    candidate: Path | str,
    project_paths: Sequence[str] = (),
) -> Path:
    """Return *candidate* as a path the editor is allowed to write.
    Accepts only a file named ``CLAUDE.md`` or ``CLAUDE.local.md`` that is
    not a symlink and lives either directly in ``~/.claude`` or inside one
    of *project_paths*.
    :raises SafetyError: with a message shown to the user.
    """
    path = Path(candidate)
    if ".." in path.parts:
        raise SafetyError(f"Refusing a path containing '..': {candidate}")
    if not path.is_absolute():
        raise SafetyError(f"An absolute path is required, got {candidate!r}")
    if path.is_symlink():
        raise SafetyError(f"Refusing to write through a symlink: {path}")
    if path.name not in EDITABLE_NAMES:
        raise SafetyError(
            f"Only {' and '.join(sorted(EDITABLE_NAMES))} can be edited, not {path.name}"
        )
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"Cannot resolve {path}: {exc}") from exc
    if resolved.name not in EDITABLE_NAMES:
        raise SafetyError(f"{resolved.name} is not an editable instruction file")
    home = claude_home().resolve()
    if resolved.parent == home:
        return resolved
    for project in project_paths:
        if not project:
            continue
        try:
            root = Path(project).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.parent == root or is_within(resolved, root):
            return resolved
    raise SafetyError(
        f"{resolved} is not in ~/.claude or in any project the dashboard has indexed"
    )


def save_claude_md(
    candidate: Path | str,
    content: str,
    project_paths: Sequence[str] = (),
) -> Dict[str, Any]:
    """Write an instruction file, taking a timestamped backup first.
    The backup lives under ``~/.agentboard/backups`` and is never
    pruned, so an accidental save is always recoverable.
    """
    path = validate_claude_md_path(candidate, project_paths)
    if not isinstance(content, str):
        raise SafetyError("Content must be text")
    backup_path = None
    if path.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        # Keep the project name in the backup file so several CLAUDE.md
        # files do not collide in one directory.
        tag = re.sub(r"[^A-Za-z0-9]+", "-", str(path.parent)).strip("-")[-60:]
        target_dir = backup_dir() / stamp
        target_dir.mkdir(parents=True, exist_ok=True)
        backup_path = target_dir / f"{tag}__{path.name}"
        backup_path.write_bytes(path.read_bytes())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".dashboard-tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise SafetyError(f"Could not write {path}: {exc}") from exc
    return {
        "path": str(path),
        "bytes": len(content.encode("utf-8")),
        "backup": str(backup_path) if backup_path else None,
    }


def list_claude_md_backups() -> List[Dict[str, Any]]:
    """Every backup taken before an instruction-file save, newest first."""
    root = backup_dir()
    if not root.is_dir():
        return []
    entries: List[Dict[str, Any]] = []
    for batch in sorted(root.iterdir(), reverse=True):
        if not batch.is_dir():
            continue
        for item in sorted(batch.iterdir()):
            if not item.is_file():
                continue
            original = item.name.split("__", 1)
            entries.append({
                "path": str(item),
                "taken_at": batch.name,
                "original_name": original[-1],
                "size": item.stat().st_size,
            })
    return entries


# ---------------------------------------------------------------- assets

@dataclass


class Asset:
    """A skill, slash command or sub-agent definition found on disk."""
    kind: str
    name: str
    path: str
    source: str
    description: str = ""
    size: int = 0
    frontmatter: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the browser."""
        return {
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "source": self.source,
            "description": self.description,
            "size": self.size,
            "frontmatter": self.frontmatter,
        }
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    """Read the simple ``key: value`` YAML header these files use.
    A real YAML parser is not a dependency worth adding for a handful of
    scalar keys; anything unrecognised is left out rather than guessed at.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    data: Dict[str, Any] = {}
    key = None
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        header = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if header:
            key = header.group(1)
            value = header.group(2).strip()
            if value.startswith(('"', "'")) and value.endswith(('"', "'")) and len(value) > 1:
                value = value[1:-1]
            data[key] = value
        elif key and line.startswith((" ", "\t", "-")):
            data[key] = f"{data.get(key, '')} {line.strip()}".strip()
    return data


def _asset_from_file(path: Path, kind: str, source: str) -> Asset:
    """Build an :class:`Asset` from a markdown definition file."""
    head = _read_head(path, 8192)
    frontmatter = _parse_frontmatter(head["content"])
    name = frontmatter.get("name") or (
        path.parent.name if path.name == "SKILL.md" else path.stem
    )
    description = frontmatter.get("description", "")
    if not description:
        body = _FRONTMATTER.sub("", head["content"]).strip()
        first = next((line.strip() for line in body.splitlines()
                      if line.strip() and not line.startswith("#")), "")
        description = first[:240]
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return Asset(
        kind=kind, name=str(name), path=str(path), source=source,
        description=str(description)[:400], size=size, frontmatter=frontmatter,
    )


def list_assets() -> Dict[str, Any]:
    """Every skill, command and agent Claude Code can see.
    Searches the user-level ``commands``/``agents``/``skills`` directories
    and the plugin tree, de-duplicating by resolved path.
    """
    home = claude_home()
    assets: List[Asset] = []
    seen: set[str] = set()

    def collect(root: Path, kind: str, source: str, pattern: str = "*.md") -> None:
        """Add every definition under *root* that has not been seen."""
        if not root.is_dir():
            return
        try:
            for path in sorted(root.rglob(pattern)):
                if not path.is_file() or path.is_symlink():
                    continue
                key = str(path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                assets.append(_asset_from_file(path, kind, source))
        except OSError:
            return
    collect(home / "commands", "command", "user")
    collect(home / "agents", "agent", "user")
    collect(home / "skills", "skill", "user", "SKILL.md")
    plugins_root = home / "plugins"
    for base in (plugins_root / "cache", plugins_root / "marketplaces"):
        if not base.is_dir():
            continue
        try:
            plugin_dirs = [p for p in base.rglob("*") if p.is_dir() and not p.is_symlink()]
        except OSError:
            continue
        for directory in plugin_dirs:
            label = _plugin_label(directory, plugins_root)
            if directory.name == "commands":
                collect(directory, "command", label)
            elif directory.name == "agents":
                collect(directory, "agent", label)
            elif directory.name == "skills":
                collect(directory, "skill", label, "SKILL.md")
    counts: Dict[str, int] = {}
    for asset in assets:
        counts[asset.kind] = counts.get(asset.kind, 0) + 1
    return {
        "assets": [a.to_dict() for a in sorted(assets, key=lambda a: (a.kind, a.name.lower()))],
        "counts": counts,
        "searched": [
            str(home / "commands"), str(home / "agents"), str(home / "skills"),
            str(plugins_root),
        ],
    }


def _plugin_label(directory: Path, plugins_root: Path) -> str:
    """A readable source name such as ``plugin: pr-review-toolkit``."""
    try:
        relative = directory.relative_to(plugins_root)
    except ValueError:
        return "plugin"
    parts = [p for p in relative.parts if p not in {"cache", "marketplaces", "plugins", ".claude"}]
    return f"plugin: {parts[1] if len(parts) > 1 else (parts[0] if parts else 'unknown')}"


def read_asset(path: Path | str) -> Dict[str, Any]:
    """Read one asset file for the preview pane.
    The path must resolve inside ``~/.claude``; the browser only ever
    offers paths it listed itself, but the check is repeated here.
    """
    target = Path(path)
    home = claude_home().resolve()
    if ".." in target.parts:
        raise SafetyError(f"Refusing a path containing '..': {path}")
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"Cannot read {path}: {exc}") from exc
    if not is_within(resolved, home):
        raise SafetyError(f"Refusing to read outside {home}: {resolved}")
    if not resolved.is_file():
        raise SafetyError(f"Not a file: {resolved}")
    result = _read_head(resolved)
    result["path"] = str(resolved)
    result["frontmatter"] = _parse_frontmatter(result["content"])
    return result


# ----------------------------------------------------------------- todos
#: Status values Claude Code writes, mapped to a display order.
TODO_ORDER = {"in_progress": 0, "pending": 1, "completed": 2}


def read_todos(session_titles: Dict[str, str] | None = None) -> Dict[str, Any]:
    """Parse ``~/.claude/todos`` into task lists linked to their session.
    The format is not documented and is absent on some installs, so every
    shape is handled defensively: a bare list, or an object with a
    ``todos`` key, and items that may use ``content``/``task``/``text``.
    """
    root = claude_home() / "todos"
    if not root.is_dir():
        return {"lists": [], "exists": False, "path": str(root), "total": 0}
    titles = session_titles or {}
    lists: List[Dict[str, Any]] = []
    try:
        files = sorted(root.glob("*.json"), reverse=True)
    except OSError:
        files = []
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            lists.append({
                "file": path.name, "path": str(path), "session_id": _session_from_todo(path.name),
                "items": [], "error": "unreadable JSON", "counts": {},
            })
            continue
        items_raw = raw if isinstance(raw, list) else (
            raw.get("todos") if isinstance(raw, dict) else None
        )
        if not isinstance(items_raw, list):
            items_raw = []
        items: List[Dict[str, Any]] = []
        for entry in items_raw:
            if isinstance(entry, str):
                items.append({"content": entry, "status": "pending", "id": ""})
                continue
            if not isinstance(entry, dict):
                continue
            content = (entry.get("content") or entry.get("task")
                       or entry.get("text") or entry.get("activeForm") or "")
            status = str(entry.get("status") or "pending")
            items.append({
                "content": str(content),
                "status": status,
                "id": str(entry.get("id", "")),
                "active_form": str(entry.get("activeForm", "")),
            })
        items.sort(key=lambda item: TODO_ORDER.get(item["status"], 3))
        counts: Dict[str, int] = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        session_id = _session_from_todo(path.name)
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            modified = ""
        lists.append({
            "file": path.name,
            "path": str(path),
            "session_id": session_id,
            "session_title": titles.get(session_id, ""),
            "items": items,
            "counts": counts,
            "modified": modified,
            "error": None,
        })
    return {
        "lists": lists,
        "exists": True,
        "path": str(root),
        "total": sum(len(entry["items"]) for entry in lists),
    }


def _session_from_todo(filename: str) -> str:
    """Extract the session uuid from a todo filename, if one is present."""
    match = re.match(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        filename,
    )
    return match.group(1) if match else ""


# ---------------------------------------------------------------- backup

def backup_projects(destination: Path | str, progress=None) -> Dict[str, Any]:
    """ZIP the whole ``projects`` tree to *destination*.
    Written to a temporary name and moved into place, so an interrupted
    run never leaves a half-written archive where the user expects a
    backup.
    """
    root = claude_projects_dir()
    if not root.is_dir():
        raise SafetyError(f"Nothing to back up: {root} does not exist")
    target = Path(destination)
    if target.is_dir():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = target / f"claude-projects-{stamp}.zip"
    if target.suffix.lower() != ".zip":
        target = target.with_suffix(".zip")
    files = [p for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()]
    temporary = target.with_name(target.name + ".part")
    total_bytes = 0
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, path in enumerate(files):
                try:
                    archive.write(path, path.relative_to(root.parent))
                    total_bytes += path.stat().st_size
                except OSError:
                    continue
                if progress is not None:
                    progress(index + 1, len(files))
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SafetyError(f"Backup failed: {exc}") from exc
    return {
        "path": str(target),
        "files": len(files),
        "source_bytes": total_bytes,
        "archive_bytes": target.stat().st_size if target.exists() else 0,
    }
