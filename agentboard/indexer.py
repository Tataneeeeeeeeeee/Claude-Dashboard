"""Session index, on-disk cache and full-text search.

The index is a dictionary of :class:`~agentboard.parser.SessionMeta`
keyed by absolute transcript path, rebuilt by streaming every ``.jsonl`` under
``~/.claude/projects``.  Results are cached in
``~/.agentboard/cache.json`` keyed by ``(path, mtime, size)``, so a
restart re-scans only what changed.  On the reference install a cold scan of
189 MB takes about half a second; a warm start is instant.

Search deliberately avoids building a term index, which would have to hold a
large fraction of 183 MB of text in memory.  Instead it runs two cheap passes
per file:

1. read the file in binary and test the whole buffer for the term - a single
   pass in C that rejects most files outright;
2. only for files that match, walk the lines and parse the few that hit.

That keeps a full-corpus search well under a second while returning exact
per-message hits the viewer can jump to.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .parser import (
    SessionMeta,
    block_list,
    iter_entries,
    parse_timestamp,
    scan_session,
)
from .paths import claude_projects_dir, cache_path, decode_project_dir

__all__ = ["IndexProgress", "SessionIndex", "get_index"]

#: Bump when :meth:`SessionMeta.to_dict` changes shape, to invalidate caches.
CACHE_VERSION = 3


@dataclass
class IndexProgress:
    """Live state of an indexing run, polled by the UI progress bar."""

    running: bool = False
    total: int = 0
    done: int = 0
    current: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    from_cache: int = 0
    scanned: int = 0
    error: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for ``GET /api/index/status``."""
        elapsed = (self.finished_at or time.time()) - self.started_at if self.started_at else 0.0
        return {
            "running": self.running,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "percent": round(100.0 * self.done / self.total, 1) if self.total else 100.0,
            "from_cache": self.from_cache,
            "scanned": self.scanned,
            "elapsed_seconds": round(elapsed, 2),
            "error": self.error,
        }


@dataclass
class ProjectInfo:
    """A project directory under ``~/.claude/projects`` and its sessions."""

    dir_name: str
    path: str
    exists: bool
    decoded_guess: bool
    session_count: int = 0
    message_count: int = 0
    total_tokens: int = 0
    file_size: int = 0
    last_activity: str | None = None
    has_claude_md: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the sidebar."""
        return {
            "dir_name": self.dir_name,
            "path": self.path,
            "name": Path(self.path).name or self.path,
            "exists": self.exists,
            "decoded_guess": self.decoded_guess,
            "session_count": self.session_count,
            "message_count": self.message_count,
            "total_tokens": self.total_tokens,
            "file_size": self.file_size,
            "last_activity": self.last_activity,
            "has_claude_md": self.has_claude_md,
        }


class SessionIndex:
    """In-memory index over all transcripts, with a disk-backed cache."""

    def __init__(self, projects_dir: Path | None = None) -> None:
        self._projects_dir = projects_dir or claude_projects_dir()
        self._lock = threading.RLock()
        self._sessions: Dict[str, SessionMeta] = {}
        self._projects: Dict[str, ProjectInfo] = {}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_loaded = False
        self.progress = IndexProgress()
        self.last_built: float = 0.0

    # ------------------------------------------------------------------ cache

    def _load_cache(self) -> None:
        """Read the disk cache once per process, ignoring anything unusable."""
        if self._cache_loaded:
            return
        self._cache_loaded = True
        path = cache_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return
        entries = raw.get("sessions")
        if isinstance(entries, dict):
            self._cache = {k: v for k, v in entries.items() if isinstance(v, dict)}

    def _save_cache(self) -> None:
        """Persist the current index.  Failures are non-fatal."""
        path = cache_path()
        payload = {
            "version": CACHE_VERSION,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "sessions": {key: meta.to_dict() for key, meta in self._sessions.items()},
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    # ------------------------------------------------------------------ build

    def transcript_paths(self) -> List[Path]:
        """Every ``.jsonl`` directly inside a project directory, sorted.

        Symlinks are skipped, both files and directories.  The deletion
        guard refuses them anyway, so indexing one would put a session in
        the list that can never be deleted, and a "select all" would fail
        as a whole because deletion is all-or-nothing.
        """
        root = self._projects_dir
        if not root.is_dir():
            return []
        found: List[Path] = []
        try:
            for child in sorted(root.iterdir()):
                if child.is_symlink() or not child.is_dir():
                    continue
                try:
                    for entry in sorted(child.iterdir()):
                        if entry.is_symlink():
                            continue
                        if entry.is_file() and entry.suffix == ".jsonl":
                            found.append(entry)
                except OSError:
                    continue
        except OSError:
            return []
        return found

    def build(self, force: bool = False, on_progress: Callable[[IndexProgress], None] | None = None) -> None:
        """(Re)build the index, reusing cache entries whose files are unchanged.

        *force* discards the cache and re-streams every transcript.
        """
        with self._lock:
            self._load_cache()
            files = self.transcript_paths()
            self.progress = IndexProgress(
                running=True, total=len(files), started_at=time.time()
            )
            sessions: Dict[str, SessionMeta] = {}
            try:
                for path in files:
                    key = str(path)
                    self.progress.current = path.name
                    cached = None if force else self._cache.get(key)
                    meta = None
                    if cached:
                        try:
                            stat = path.stat()
                            same = (
                                abs(float(cached.get("mtime", -1)) - stat.st_mtime) < 1e-6
                                and int(cached.get("file_size", -1)) == stat.st_size
                            )
                        except OSError:
                            same = False
                        if same:
                            try:
                                meta = SessionMeta.from_dict(cached)
                                self.progress.from_cache += 1
                            except (TypeError, ValueError):
                                meta = None
                    if meta is None:
                        meta = scan_session(path)
                        self.progress.scanned += 1
                    sessions[key] = meta
                    self.progress.done += 1
                    if on_progress is not None:
                        on_progress(self.progress)
            except Exception as exc:  # pragma: no cover - defensive
                self.progress.error = f"{type(exc).__name__}: {exc}"
            self._sessions = sessions
            self._cache = {k: v.to_dict() for k, v in sessions.items()}
            self._rebuild_projects()
            self.progress.running = False
            self.progress.finished_at = time.time()
            self.last_built = time.time()
            self._save_cache()

    def refresh_path(self, path: Path | str) -> SessionMeta | None:
        """Re-scan a single transcript after a filesystem event."""
        file_path = Path(path)
        key = str(file_path)
        with self._lock:
            if not file_path.exists():
                self._sessions.pop(key, None)
                self._cache.pop(key, None)
                self._rebuild_projects()
                return None
            if file_path.suffix != ".jsonl":
                return None
            meta = scan_session(file_path)
            self._sessions[key] = meta
            self._cache[key] = meta.to_dict()
            self._rebuild_projects()
            return meta

    def _rebuild_projects(self) -> None:
        """Recompute per-project rollups from the current session map."""
        projects: Dict[str, ProjectInfo] = {}
        for meta in self._sessions.values():
            dir_name = meta.project_dir
            info = projects.get(dir_name)
            if info is None:
                if meta.project_path:
                    path, guess = meta.project_path, False
                else:
                    path, guess = decode_project_dir(dir_name), True
                exists = Path(path).is_dir() if path else False
                info = ProjectInfo(
                    dir_name=dir_name,
                    path=path,
                    exists=exists,
                    decoded_guess=guess,
                    has_claude_md=exists and (Path(path) / "CLAUDE.md").is_file(),
                )
                projects[dir_name] = info
            info.session_count += 1
            info.message_count += meta.message_count
            info.total_tokens += meta.total_tokens
            info.file_size += meta.file_size
            if meta.last_timestamp and (
                info.last_activity is None or meta.last_timestamp > info.last_activity
            ):
                info.last_activity = meta.last_timestamp
        self._projects = projects

    # ------------------------------------------------------------------ reads

    def ensure_built(self) -> None:
        """Build the index if it has never been built."""
        if not self.last_built and not self.progress.running:
            self.build()

    @property
    def sessions(self) -> List[SessionMeta]:
        """All indexed sessions, newest activity first."""
        with self._lock:
            return sorted(
                self._sessions.values(),
                key=lambda m: m.last_timestamp or "",
                reverse=True,
            )

    @property
    def projects(self) -> List[ProjectInfo]:
        """All projects, most recently active first."""
        with self._lock:
            return sorted(
                self._projects.values(), key=lambda p: p.last_activity or "", reverse=True
            )

    def get(self, session_id: str) -> SessionMeta | None:
        """Look up one session by id, or by full transcript path."""
        with self._lock:
            direct = self._sessions.get(session_id)
            if direct is not None:
                return direct
            for meta in self._sessions.values():
                if meta.session_id == session_id:
                    return meta
        return None

    def stats(self) -> Dict[str, Any]:
        """Corpus-level counters for the header and the empty state."""
        with self._lock:
            metas = list(self._sessions.values())
        return {
            "session_count": len(metas),
            "project_count": len(self._projects),
            "message_count": sum(m.message_count for m in metas),
            "tool_calls": sum(m.tool_calls for m in metas),
            "total_tokens": sum(m.total_tokens for m in metas),
            "file_size": sum(m.file_size for m in metas),
            "corrupt_lines": sum(m.corrupt_lines for m in metas),
            "last_built": self.last_built,
            "projects_dir": str(self._projects_dir),
            "projects_dir_exists": self._projects_dir.is_dir(),
        }

    # ----------------------------------------------------------------- filter

    def filter(
        self,
        project: str | None = None,
        model: str | None = None,
        tool: str | None = None,
        since: str | None = None,
        until: str | None = None,
        query: str | None = None,
        branch: str | None = None,
        favorites: Iterable[str] | None = None,
        only_favorites: bool = False,
        tags: Dict[str, List[str]] | None = None,
        tag: str | None = None,
        min_messages: int | None = None,
        max_messages: int | None = None,
        sort: str = "recent",
    ) -> List[SessionMeta]:
        """Apply the sidebar filters and sorting to the index.

        *query* here is a cheap title/preview/path match for type-ahead; the
        full-text search over message bodies is :meth:`search`.
        """
        favorite_set = set(favorites or ())
        tag_map = tags or {}
        result: List[SessionMeta] = []
        for meta in self.sessions:
            if project and meta.project_dir != project and meta.project_path != project:
                continue
            if model and model not in meta.models:
                continue
            if tool and tool not in meta.tools:
                continue
            if branch and meta.git_branch != branch:
                continue
            if since and (meta.last_timestamp or "") < since:
                continue
            if until and (meta.first_timestamp or "") > until:
                continue
            if only_favorites and meta.session_id not in favorite_set:
                continue
            if tag and tag not in tag_map.get(meta.session_id, []):
                continue
            if min_messages is not None and meta.message_count < min_messages:
                continue
            if max_messages is not None and meta.message_count > max_messages:
                continue
            if query:
                needle = query.lower()
                haystack = " ".join(
                    [meta.title, meta.preview, meta.project_path, meta.session_id]
                ).lower()
                if needle not in haystack:
                    continue
            result.append(meta)
        return self.sort(result, sort)

    @staticmethod
    def sort(metas: List[SessionMeta], sort: str, pricing: Dict[str, Any] | None = None) -> List[SessionMeta]:
        """Order sessions by one of the supported sort keys."""
        from .usage import session_cost  # local import avoids a cycle

        if sort == "oldest":
            return sorted(metas, key=lambda m: m.first_timestamp or "")
        if sort == "longest":
            return sorted(metas, key=lambda m: m.duration_seconds, reverse=True)
        if sort == "messages":
            return sorted(metas, key=lambda m: m.message_count, reverse=True)
        if sort == "tokens":
            return sorted(metas, key=lambda m: m.total_tokens, reverse=True)
        if sort == "expensive":
            return sorted(metas, key=lambda m: session_cost(m, pricing), reverse=True)
        if sort == "size":
            return sorted(metas, key=lambda m: m.file_size, reverse=True)
        if sort == "title":
            return sorted(metas, key=lambda m: m.title.lower())
        return sorted(metas, key=lambda m: m.last_timestamp or "", reverse=True)

    def distinct(self) -> Dict[str, List[str]]:
        """Values available to the filter dropdowns."""
        models: Dict[str, int] = {}
        tools: Dict[str, int] = {}
        branches: Dict[str, int] = {}
        for meta in self.sessions:
            for name in meta.models:
                models[name] = models.get(name, 0) + 1
            for name, count in meta.tools.items():
                tools[name] = tools.get(name, 0) + count
            if meta.git_branch:
                branches[meta.git_branch] = branches.get(meta.git_branch, 0) + 1
        return {
            "models": sorted(models, key=lambda k: -models[k]),
            "tools": sorted(tools, key=lambda k: -tools[k]),
            "branches": sorted(branches, key=lambda k: -branches[k]),
        }

    # ----------------------------------------------------------------- search

    def search(
        self,
        term: str,
        limit: int = 400,
        context_chars: int = 160,
        project: str | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
    ) -> Dict[str, Any]:
        """Full-text search across every transcript.

        Returns per-message hits carrying enough identity (``session_id``,
        ``line``, ``uuid``) for the viewer to scroll straight to the match.
        """
        started = time.time()
        if not term or not term.strip():
            return {"term": term, "hits": [], "session_count": 0, "truncated": False,
                    "elapsed_seconds": 0.0, "scanned_files": 0, "matched_files": 0}

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(term if regex else re.escape(term), flags)
            raw_pattern = re.compile(
                (term if regex else re.escape(term)).encode("utf-8", "ignore"), flags
            )
        except re.error as exc:
            return {"term": term, "hits": [], "error": f"invalid pattern: {exc}",
                    "session_count": 0, "truncated": False, "elapsed_seconds": 0.0,
                    "scanned_files": 0, "matched_files": 0}

        hits: List[Dict[str, Any]] = []
        sessions_hit: set[str] = set()
        scanned = matched = 0
        truncated = False

        for meta in self.sessions:
            if project and meta.project_dir != project and meta.project_path != project:
                continue
            if truncated:
                break
            scanned += 1
            path = Path(meta.path)
            # Stage 1: one C-level pass over the raw bytes rejects most files.
            try:
                with open(path, "rb") as handle:
                    blob = handle.read()
            except OSError:
                continue
            if not raw_pattern.search(blob):
                continue
            matched += 1
            del blob
            # Stage 2: only now pay for JSON parsing.
            for line_no, entry, error in iter_entries(path):
                if error is not None:
                    continue
                kind = entry.get("type")
                if kind not in {"user", "assistant"}:
                    continue
                text = self._searchable_text(entry)
                if not text:
                    continue
                match = pattern.search(text)
                if match is None:
                    continue
                start = max(0, match.start() - context_chars // 2)
                end = min(len(text), match.end() + context_chars // 2)
                hits.append(
                    {
                        "session_id": meta.session_id,
                        "path": meta.path,
                        "project_dir": meta.project_dir,
                        "project_path": meta.project_path,
                        "title": meta.title,
                        "line": line_no,
                        "uuid": entry.get("uuid"),
                        "role": kind,
                        "timestamp": entry.get("timestamp"),
                        "snippet": ("…" if start else "")
                        + " ".join(text[start:end].split())
                        + ("…" if end < len(text) else ""),
                        "match_start": match.start() - start + (1 if start else 0),
                        "match_length": match.end() - match.start(),
                    }
                )
                sessions_hit.add(meta.session_id)
                if len(hits) >= limit:
                    truncated = True
                    break

        return {
            "term": term,
            "hits": hits,
            "session_count": len(sessions_hit),
            "truncated": truncated,
            "elapsed_seconds": round(time.time() - started, 3),
            "scanned_files": scanned,
            "matched_files": matched,
        }

    @staticmethod
    def _searchable_text(entry: Dict[str, Any]) -> str:
        """Flatten one entry into the text the search should look at.

        Covers prose, thinking, tool inputs and tool outputs, which is what
        makes "find the session where I ran that migration" work.
        """
        message = entry.get("message")
        parts: List[str] = []
        for block in block_list(message):
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif btype == "thinking" and isinstance(block.get("thinking"), str):
                parts.append(block["thinking"])
            elif btype == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    parts.append(name)
                payload = block.get("input")
                if isinstance(payload, dict):
                    for value in payload.values():
                        if isinstance(value, str):
                            parts.append(value)
            elif btype == "tool_result":
                content = block.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            parts.append(item["text"])
        return "\n".join(parts)

    # ------------------------------------------------------------ maintenance

    def older_than(self, days: int) -> List[SessionMeta]:
        """Sessions whose last activity predates *days* ago."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        out = []
        for meta in self.sessions:
            when = parse_timestamp(meta.last_timestamp)
            if when is not None and when < cutoff:
                out.append(meta)
        return out

    def smaller_than(self, messages: int) -> List[SessionMeta]:
        """Sessions with fewer than *messages* real messages (aborted runs)."""
        return [m for m in self.sessions if m.message_count < messages]

    def orphaned(self) -> List[SessionMeta]:
        """Sessions whose project directory no longer exists on disk."""
        out = []
        for meta in self.sessions:
            info = self._projects.get(meta.project_dir)
            if info is not None and not info.exists:
                out.append(meta)
        return out


_INSTANCE: Optional[SessionIndex] = None
_INSTANCE_LOCK = threading.Lock()


def get_index() -> SessionIndex:
    """Return the process-wide index, creating it on first use."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = SessionIndex()
        return _INSTANCE
