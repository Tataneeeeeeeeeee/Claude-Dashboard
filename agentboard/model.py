"""The normalized data model every provider adapter maps into.

Each AI tool stores its history differently - Claude Code writes one JSONL
line per content block, Codex CLI wraps everything in ``response_item``
envelopes, Gemini CLI rewrites a single JSON document per chat.  Adapters in
:mod:`agentboard.providers` translate all of them into the four shapes here,
and nothing downstream (index, usage, API, UI) needs to know which tool a
session came from beyond the ``provider`` id it carries.

``Session``  (:class:`SessionMeta`)   one conversation: counts, tokens,
                                       per-model / per-day buckets, tools.
``Message``  (:class:`ParsedMessage`) one renderable turn for the viewer.
``Usage``                              a token-counter dict keyed by
                                       :data:`TOKEN_KEYS`.
``Project``  (:class:`ProjectInfo`)   sessions grouped by working directory,
                                       possibly across several providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

__all__ = [
    "TOKEN_KEYS",
    "BILLABLE_KEYS",
    "EMPTY_USAGE",
    "EMPTY_MODEL_TOTALS",
    "EMPTY_DAY_TOTALS",
    "empty_usage",
    "add_usage",
    "parse_timestamp",
    "SessionMeta",
    "Session",
    "ParsedMessage",
    "Message",
    "ProjectInfo",
    "Project",
]

#: The token counters carried through every aggregation level.
#:
#: ``cache_write_5m`` / ``cache_write_1h`` are a breakdown of ``cache_write``
#: for providers that bill cache writes by time-to-live (Anthropic); other
#: providers leave them at zero.
TOKEN_KEYS = ("input", "output", "cache_write", "cache_write_5m", "cache_write_1h", "cache_read")

#: The counters that add up to "total tokens".  The TTL split is a breakdown
#: of ``cache_write`` and must not be added again.
BILLABLE_KEYS = ("input", "output", "cache_write", "cache_read")

#: Zero-valued usage record, the shape every consumer can rely on.
#: ``thinking`` is informational: providers already include reasoning tokens
#: in ``output``, which is where they are billed.
EMPTY_USAGE: Dict[str, int] = {**{k: 0 for k in TOKEN_KEYS}, "thinking": 0}

#: Zero rows for the per-model and per-day buckets on :class:`SessionMeta`.
EMPTY_MODEL_TOTALS: Dict[str, int] = {**{k: 0 for k in TOKEN_KEYS}, "messages": 0}
EMPTY_DAY_TOTALS: Dict[str, int] = {**{k: 0 for k in TOKEN_KEYS}, "messages": 0, "tool_calls": 0}


def empty_usage() -> Dict[str, int]:
    """A fresh zeroed usage record."""
    return dict(EMPTY_USAGE)


def add_usage(target: Dict[str, int], usage: Dict[str, int]) -> None:
    """Add every counter of *usage* into *target* in place."""
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + value


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp into an aware UTC ``datetime``.

    Accepts ISO-8601 strings (with or without ``Z``) and epoch seconds or
    milliseconds, which between them cover every provider seen so far.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 1e11:  # milliseconds
                seconds /= 1000.0
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


@dataclass
class SessionMeta:
    """Everything the session list, filters and dashboard need for one session.

    Produced by a provider adapter in a single streaming pass and cached on
    disk keyed by ``(path, mtime, size)``.
    """

    session_id: str
    path: str
    project_dir: str
    provider: str = "claude"
    project_path: str = ""
    title: str = ""
    title_source: str = "none"
    git_branch: str = ""
    versions: List[str] = field(default_factory=list)
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    message_count: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    sidechain_messages: int = 0
    error_messages: int = 0
    line_count: int = 0
    corrupt_lines: int = 0
    file_size: int = 0
    mtime: float = 0.0
    tokens: Dict[str, int] = field(default_factory=dict)
    models: Dict[str, Dict[str, int]] = field(default_factory=dict)
    tools: Dict[str, int] = field(default_factory=dict)
    daily: Dict[str, Dict[str, int]] = field(default_factory=dict)
    heatmap: Dict[str, int] = field(default_factory=dict)
    files_touched: List[str] = field(default_factory=list)
    reported_cost_usd: float | None = None
    reported_duration_ms: int | None = None
    preview: str = ""

    @property
    def duration_seconds(self) -> float:
        """Wall-clock span between the first and last timestamped entry."""
        start = parse_timestamp(self.first_timestamp)
        end = parse_timestamp(self.last_timestamp)
        if start is None or end is None:
            return 0.0
        return max(0.0, (end - start).total_seconds())

    @property
    def total_tokens(self) -> int:
        """Sum of the four billable token counters."""
        return sum(int(self.tokens.get(key, 0)) for key in BILLABLE_KEYS)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the cache file and the HTTP API."""
        data = {
            "session_id": self.session_id,
            "path": self.path,
            "project_dir": self.project_dir,
            "provider": self.provider,
            "project_path": self.project_path,
            "title": self.title,
            "title_source": self.title_source,
            "git_branch": self.git_branch,
            "versions": self.versions,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "message_count": self.message_count,
            "user_messages": self.user_messages,
            "assistant_messages": self.assistant_messages,
            "tool_calls": self.tool_calls,
            "sidechain_messages": self.sidechain_messages,
            "error_messages": self.error_messages,
            "line_count": self.line_count,
            "corrupt_lines": self.corrupt_lines,
            "file_size": self.file_size,
            "mtime": self.mtime,
            "tokens": self.tokens,
            "models": self.models,
            "tools": self.tools,
            "daily": self.daily,
            "heatmap": self.heatmap,
            "files_touched": self.files_touched,
            "reported_cost_usd": self.reported_cost_usd,
            "reported_duration_ms": self.reported_duration_ms,
            "preview": self.preview,
        }
        data["duration_seconds"] = self.duration_seconds
        data["total_tokens"] = self.total_tokens
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionMeta":
        """Rebuild from a cache entry, ignoring derived and unknown keys."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ParsedMessage:
    """One renderable entry of a conversation, as handed to the viewer.

    ``kind`` is one of ``user``, ``assistant``, ``tool_result``, ``system``
    or ``attachment``; ``blocks`` hold ``text``, ``thinking``, ``tool_use``,
    ``tool_result`` and ``image`` dicts.  ``line`` locates the message in
    its source file (the line for JSONL, the message index for JSON), which
    is what search hits jump to.
    """

    index: int
    line: int
    uuid: str | None
    parent_uuid: str | None
    kind: str
    role: str
    timestamp: str | None
    model: str | None
    is_sidechain: bool
    is_meta: bool
    is_error: bool
    blocks: List[Dict[str, Any]]
    usage: Dict[str, int] | None = None
    subtype: str | None = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the HTTP API."""
        return {
            "index": self.index,
            "line": self.line,
            "uuid": self.uuid,
            "parent_uuid": self.parent_uuid,
            "kind": self.kind,
            "role": self.role,
            "timestamp": self.timestamp,
            "model": self.model,
            "is_sidechain": self.is_sidechain,
            "is_meta": self.is_meta,
            "is_error": self.is_error,
            "blocks": self.blocks,
            "usage": self.usage,
            "subtype": self.subtype,
            "extra": self.extra,
        }


@dataclass
class ProjectInfo:
    """A working directory and the sessions recorded in it.

    One project can hold sessions from several providers: the same
    repository worked on with Claude Code and with Codex is one project.
    ``key`` identifies it: the real path when known, otherwise the
    provider's own directory name prefixed with the provider id.
    """

    dir_name: str
    path: str
    exists: bool
    decoded_guess: bool
    key: str = ""
    session_count: int = 0
    message_count: int = 0
    total_tokens: int = 0
    file_size: int = 0
    last_activity: str | None = None
    has_claude_md: bool = False
    instruction_files: List[str] = field(default_factory=list)
    providers: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the sidebar."""
        return {
            "dir_name": self.dir_name,
            "key": self.key or self.dir_name,
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
            "instruction_files": self.instruction_files,
            "providers": self.providers,
        }


# Provider-neutral aliases, matching the names used in the documentation.
Session = SessionMeta
Message = ParsedMessage
Project = ProjectInfo
