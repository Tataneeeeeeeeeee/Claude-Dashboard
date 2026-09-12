"""Streaming reader for Claude Code session transcripts.

Every function here is defensive by construction, because the on-disk format
is an open set: ``SCHEMA.md`` records 17 distinct ``type`` values on the
reference install, several undocumented, and new Claude Code releases add
more.  Unknown shapes are counted and skipped, never fatal.

Two entry points matter:

:func:`scan_session`
    One streaming pass that produces :class:`SessionMeta` - everything the
    list view needs (title, counts, tokens, daily buckets, tool usage) without
    ever holding the file in memory.  The largest transcript on the reference
    install is 30 MB.

:func:`parse_conversation`
    The full message list for the viewer.  Only ever called for one session at
    a time, in response to an explicit click.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

__all__ = [
    "SessionMeta",
    "ParsedMessage",
    "iter_lines",
    "iter_entries",
    "scan_session",
    "parse_conversation",
    "block_list",
    "text_of_blocks",
    "is_human_turn",
    "parse_timestamp",
    "STATE_TYPES",
    "MESSAGE_TYPES",
    "EMPTY_USAGE",
    "EMPTY_MODEL_TOTALS",
    "EMPTY_DAY_TOTALS",
    "api_message_key",
    "file_signature",
]

#: Entry types that are session-level state markers, rewritten repeatedly
#: through the file.  The last occurrence wins.
STATE_TYPES = frozenset(
    {
        "mode",
        "permission-mode",
        "atis-latch",
        "bridge-session",
        "ai-title",
        "last-prompt",
        "summary",
        "cost-state",
    }
)

#: Entry types that carry conversation content.
MESSAGE_TYPES = frozenset({"user", "assistant", "system", "attachment"})

#: Read transcripts in chunks this large when scanning raw bytes for a term.
_SEARCH_CHUNK = 4 << 20

#: The token counters carried through every aggregation level.
_TOKEN_KEYS = ("input", "output", "cache_write", "cache_write_5m", "cache_write_1h", "cache_read")

#: Zero rows for the per-model and per-day buckets on :class:`SessionMeta`.
EMPTY_MODEL_TOTALS: Dict[str, int] = {**{k: 0 for k in _TOKEN_KEYS}, "messages": 0}
EMPTY_DAY_TOTALS: Dict[str, int] = {**{k: 0 for k in _TOKEN_KEYS}, "messages": 0, "tool_calls": 0}


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a transcript timestamp into an aware UTC ``datetime``.

    Transcripts use ISO-8601 with a ``Z`` suffix; ``history.jsonl`` and
    ``cost-state.startTime`` use epoch milliseconds.  Both are accepted.
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


def iter_lines(path: Path | str) -> Iterator[Tuple[int, str]]:
    """Yield ``(line_number, text)`` for a transcript, tolerating bad encodings.

    The file is opened with ``errors="replace"`` so a truncated multi-byte
    sequence degrades to U+FFFD instead of raising.
    """
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace", newline="")
    except OSError:
        return
    with handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if stripped:
                yield number, stripped


def iter_entries(path: Path | str) -> Iterator[Tuple[int, Dict[str, Any], str | None]]:
    """Yield ``(line_number, entry, error)`` for every line of a transcript.

    On a malformed line *entry* is ``{}`` and *error* holds a short reason, so
    callers can surface corruption counts instead of silently dropping data.
    A truncated final line - common while a session is still being written -
    produces exactly one error.
    """
    for number, text in iter_lines(path):
        try:
            obj = json.loads(text)
        except ValueError as exc:
            yield number, {}, f"invalid JSON: {exc.args[0] if exc.args else exc}"
            continue
        if not isinstance(obj, dict):
            yield number, {}, f"expected object, got {type(obj).__name__}"
            continue
        yield number, obj, None


def block_list(message: Any) -> List[Dict[str, Any]]:
    """Normalise ``message.content`` to a list of block dicts.

    ``content`` is a plain string on 361 of 4 338 user turns and a list
    elsewhere (``SCHEMA.md`` 3.2).  List elements are not guaranteed to be
    dicts, so non-dict entries are wrapped as text blocks.
    """
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    blocks: List[Dict[str, Any]] = []
    for item in content:
        if isinstance(item, dict):
            blocks.append(item)
        elif isinstance(item, str):
            blocks.append({"type": "text", "text": item})
        else:
            blocks.append({"type": "text", "text": str(item)})
    return blocks


def text_of_blocks(blocks: List[Dict[str, Any]]) -> str:
    """Concatenate the human-readable text of *blocks*, ignoring the rest."""
    parts: List[str] = []
    for block in blocks:
        if block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def is_human_turn(entry: Dict[str, Any]) -> bool:
    """True when a ``user`` entry is a real prompt typed by the person.

    A ``user`` line is also how tool results and injected notices are stored
    (91% of them carry ``toolUseResult``).  Counting those as messages turns a
    three-turn session into two hundred - see ``SCHEMA.md`` 8.4.
    """
    if entry.get("type") != "user":
        return False
    if entry.get("toolUseResult") is not None:
        return False
    if entry.get("isMeta"):
        return False
    blocks = block_list(entry.get("message"))
    if not blocks:
        return False
    if all(block.get("type") == "tool_result" for block in blocks):
        return False
    text = text_of_blocks(blocks).strip()
    if not text:
        return any(block.get("type") not in {"tool_result"} for block in blocks)
    # Slash-command echoes and system reminders are not human prose.
    return not text.startswith(("<local-command-", "<command-name>"))


#: Zero-valued usage record, the shape every consumer can rely on.
EMPTY_USAGE: Dict[str, int] = {
    "input": 0,
    "output": 0,
    "cache_write": 0,
    "cache_write_5m": 0,
    "cache_write_1h": 0,
    "cache_read": 0,
    "thinking": 0,
}


def _usage_of(entry: Dict[str, Any]) -> Dict[str, int]:
    """Extract the billable counters from an assistant entry.

    Cache creation is additionally split by TTL from ``usage.cache_creation``.
    The split matters for cost: the 1-hour tier is charged at twice the input
    rate against 1.25x for the 5-minute tier, and on the reference install
    every one of the 9.6 million cache-creation tokens used the 1-hour tier.
    """
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return dict(EMPTY_USAGE)

    def _int(source: Dict[str, Any], key: str) -> int:
        value = source.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    details = usage.get("output_tokens_details")
    thinking = _int(details, "thinking_tokens") if isinstance(details, dict) else 0

    creation = usage.get("cache_creation")
    creation = creation if isinstance(creation, dict) else {}
    write_total = _int(usage, "cache_creation_input_tokens")
    write_5m = _int(creation, "ephemeral_5m_input_tokens")
    write_1h = _int(creation, "ephemeral_1h_input_tokens")
    if write_5m + write_1h != write_total:
        # Trust the top-level total; attribute anything unaccounted for to the
        # cheaper 5-minute tier so estimates never overstate cost.
        write_5m = max(0, write_total - write_1h)

    return {
        "input": _int(usage, "input_tokens"),
        "output": _int(usage, "output_tokens"),
        "cache_write": write_total,
        "cache_write_5m": write_5m,
        "cache_write_1h": write_1h,
        "cache_read": _int(usage, "cache_read_input_tokens"),
        "thinking": thinking,
    }


def api_message_key(entry: Dict[str, Any]) -> str:
    """Stable identity for the API response an assistant entry belongs to.

    Claude Code writes **one line per content block**, and every one of those
    lines repeats the *same* ``message.usage``.  On the reference install 156
    assistant lines represented only 86 API responses, so summing usage line
    by line overstated tokens - and cost - by 57%.  Deduplicating on this key
    brings the estimate to within a fraction of a percent of the ``costUSD``
    Claude Code records itself.  See ``SCHEMA.md`` 3.5.
    """
    message = entry.get("message")
    if isinstance(message, dict):
        identifier = message.get("id")
        if isinstance(identifier, str) and identifier:
            return identifier
    for key in ("requestId", "uuid"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return repr(sorted(entry.items())[:3])


def _truncate(text: str, limit: int) -> str:
    """Collapse whitespace in *text* and cut it to *limit* characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass
class SessionMeta:
    """Everything the session list, filters and dashboard need for one file.

    Produced by a single streaming pass and cached on disk keyed by
    ``(path, mtime, size)``.
    """

    session_id: str
    path: str
    project_dir: str
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
        """Sum of the four billable token counters.

        ``cache_write_5m`` / ``cache_write_1h`` are a breakdown of
        ``cache_write`` and are deliberately not added again.
        """
        return sum(
            int(self.tokens.get(key, 0))
            for key in ("input", "output", "cache_write", "cache_read")
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the cache file and the HTTP API."""
        data = {
            "session_id": self.session_id,
            "path": self.path,
            "project_dir": self.project_dir,
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


def scan_session(path: Path | str) -> SessionMeta:
    """Stream one transcript and return its :class:`SessionMeta`.

    The file is read line by line and never materialised.  Titles are resolved
    with the precedence documented in ``SCHEMA.md`` 3.8: a legacy ``summary``
    entry, then ``ai-title``, then the first human prompt, then the UUID.
    ``ai-title`` appears late in the file, which is why the whole stream is
    consumed rather than only the head.
    """
    file_path = Path(path)
    meta = SessionMeta(
        session_id=file_path.stem,
        path=str(file_path),
        project_dir=file_path.parent.name,
    )
    try:
        stat = file_path.stat()
        meta.file_size = stat.st_size
        meta.mtime = stat.st_mtime
    except OSError:
        return meta

    tokens = dict(EMPTY_USAGE)
    counted_api_messages: set[str] = set()
    versions: List[str] = []
    summary_title = ""
    ai_title = ""
    first_prompt = ""
    last_prompt_entry = ""

    for _, entry, error in iter_entries(file_path):
        meta.line_count += 1
        if error is not None:
            meta.corrupt_lines += 1
            continue

        kind = entry.get("type")

        session_id = entry.get("sessionId") or entry.get("session_id")
        if isinstance(session_id, str) and session_id and meta.session_id != session_id:
            # The filename is authoritative; record the mismatch only if the
            # stem is not a plausible uuid.
            if len(meta.session_id) < 8:
                meta.session_id = session_id

        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd and not meta.project_path:
            meta.project_path = cwd
        branch = entry.get("gitBranch")
        if isinstance(branch, str) and branch:
            meta.git_branch = branch
        version = entry.get("version")
        if isinstance(version, str) and version and version not in versions:
            versions.append(version)

        stamp = entry.get("timestamp")
        if isinstance(stamp, str) and stamp:
            if meta.first_timestamp is None or stamp < meta.first_timestamp:
                meta.first_timestamp = stamp
            if meta.last_timestamp is None or stamp > meta.last_timestamp:
                meta.last_timestamp = stamp

        if kind == "summary":
            value = entry.get("summary")
            if isinstance(value, str) and value.strip():
                summary_title = value.strip()
            continue
        if kind == "ai-title":
            value = entry.get("aiTitle")
            if isinstance(value, str) and value.strip():
                ai_title = value.strip()
            continue
        if kind == "last-prompt":
            value = entry.get("lastPrompt")
            if isinstance(value, str) and value.strip():
                last_prompt_entry = value.strip()
            continue
        if kind == "cost-state":
            cost = entry.get("totalCostUSD")
            if isinstance(cost, (int, float)):
                meta.reported_cost_usd = float(cost)
            duration = entry.get("totalDuration")
            if isinstance(duration, (int, float)):
                meta.reported_duration_ms = int(duration)
            continue
        if kind in STATE_TYPES or kind not in MESSAGE_TYPES:
            continue

        if entry.get("isSidechain"):
            meta.sidechain_messages += 1

        day = stamp[:10] if isinstance(stamp, str) and len(stamp) >= 10 else None
        when = parse_timestamp(stamp)
        if when is not None:
            slot = f"{when.weekday()}-{when.hour}"
            meta.heatmap[slot] = meta.heatmap.get(slot, 0) + 1

        if kind == "user":
            if is_human_turn(entry):
                meta.user_messages += 1
                meta.message_count += 1
                if not first_prompt:
                    first_prompt = text_of_blocks(block_list(entry.get("message")))
        elif kind == "assistant":
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            model = message.get("model") or "unknown"
            if entry.get("isApiErrorMessage") or entry.get("error"):
                meta.error_messages += 1

            # One API response spans several lines; count and charge it once.
            api_key = api_message_key(entry)
            first_line_of_response = api_key not in counted_api_messages
            if first_line_of_response:
                counted_api_messages.add(api_key)
                meta.assistant_messages += 1
                meta.message_count += 1
                usage = _usage_of(entry)
                for key, value in usage.items():
                    tokens[key] += value
                slot_model = meta.models.setdefault(model, dict(EMPTY_MODEL_TOTALS))
                slot_model["messages"] += 1
                for key in _TOKEN_KEYS:
                    slot_model[key] += usage[key]
                if day:
                    bucket = meta.daily.setdefault(day, dict(EMPTY_DAY_TOTALS))
                    bucket["messages"] += 1
                    for key in _TOKEN_KEYS:
                        bucket[key] += usage[key]
            elif day:
                meta.daily.setdefault(day, dict(EMPTY_DAY_TOTALS))

            for block in block_list(message):
                if block.get("type") == "tool_use":
                    name = block.get("name")
                    name = name if isinstance(name, str) and name else "unknown"
                    meta.tools[name] = meta.tools.get(name, 0) + 1
                    meta.tool_calls += 1
                    if day:
                        meta.daily[day]["tool_calls"] += 1

    meta.tokens = tokens
    meta.versions = versions
    if summary_title:
        meta.title, meta.title_source = _truncate(summary_title, 160), "summary"
    elif ai_title:
        meta.title, meta.title_source = _truncate(ai_title, 160), "ai-title"
    elif first_prompt.strip():
        meta.title, meta.title_source = _truncate(first_prompt, 120), "first-message"
    elif last_prompt_entry:
        meta.title, meta.title_source = _truncate(last_prompt_entry, 120), "last-prompt"
    else:
        meta.title, meta.title_source = meta.session_id, "session-id"
    meta.preview = _truncate(first_prompt or last_prompt_entry or "", 240)
    if not meta.project_path:
        meta.project_path = ""
    return meta


@dataclass
class ParsedMessage:
    """One renderable entry of a conversation, as handed to the viewer."""

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


def _clean_tool_result_content(content: Any, limit: int) -> Any:
    """Shrink a ``tool_result`` payload for transport to the browser.

    Long stdout is truncated with a marker, and inline image blocks are
    replaced by a small descriptor so a screenshot-heavy session does not
    push megabytes of base64 through the API.
    """
    if isinstance(content, str):
        if len(content) > limit:
            return content[:limit] + f"\n… [truncated, {len(content):,} characters total]"
        return content
    if isinstance(content, list):
        out = []
        for item in content:
            if not isinstance(item, dict):
                out.append({"type": "text", "text": str(item)[:limit]})
                continue
            if item.get("type") == "image":
                source = item.get("source") if isinstance(item.get("source"), dict) else {}
                data = source.get("data")
                out.append(
                    {
                        "type": "image",
                        "media_type": source.get("media_type", "image/png"),
                        "bytes": len(data) if isinstance(data, str) else 0,
                    }
                )
            elif item.get("type") == "text":
                text = item.get("text")
                out.append(
                    {
                        "type": "text",
                        "text": text[:limit] if isinstance(text, str) else str(text)[:limit],
                    }
                )
            else:
                out.append(item)
        return out
    return content


def parse_conversation(
    path: Path | str,
    tool_output_limit: int = 20_000,
    include_attachments: bool = True,
) -> Dict[str, Any]:
    """Parse a whole transcript into renderable messages.

    Called for one session at a time.  Tool outputs are truncated to
    *tool_output_limit* characters and inline images reduced to descriptors,
    which keeps even a 30 MB transcript inside a few megabytes of JSON.
    """
    file_path = Path(path)
    messages: List[ParsedMessage] = []
    errors: List[Dict[str, Any]] = []
    tool_names: Dict[str, str] = {}
    info: Dict[str, Any] = {
        "session_id": file_path.stem,
        "path": str(file_path),
        "title": "",
        "cwd": "",
        "git_branch": "",
        "versions": [],
        "cost_state": None,
    }

    index = 0
    last_api_key: str | None = None
    for number, entry, error in iter_entries(file_path):
        if error is not None:
            errors.append({"line": number, "error": error})
            continue
        kind = entry.get("type")

        if not info["cwd"] and isinstance(entry.get("cwd"), str):
            info["cwd"] = entry["cwd"]
        if isinstance(entry.get("gitBranch"), str) and entry["gitBranch"]:
            info["git_branch"] = entry["gitBranch"]
        version = entry.get("version")
        if isinstance(version, str) and version not in info["versions"]:
            info["versions"].append(version)

        if kind == "summary" and isinstance(entry.get("summary"), str):
            info["title"] = entry["summary"]
            continue
        if kind == "ai-title" and isinstance(entry.get("aiTitle"), str):
            if not info["title"]:
                info["title"] = entry["aiTitle"]
            continue
        if kind == "cost-state":
            info["cost_state"] = entry
            continue
        if kind in STATE_TYPES:
            continue

        if kind == "attachment":
            if not include_attachments:
                continue
            attachment = entry.get("attachment")
            sub = attachment.get("type") if isinstance(attachment, dict) else None
            rendered = entry.get("rendered")
            messages.append(
                ParsedMessage(
                    index=index,
                    line=number,
                    uuid=entry.get("uuid"),
                    parent_uuid=entry.get("parentUuid"),
                    kind="attachment",
                    role="system",
                    timestamp=entry.get("timestamp"),
                    model=None,
                    is_sidechain=bool(entry.get("isSidechain")),
                    is_meta=True,
                    is_error=False,
                    blocks=[],
                    subtype=sub if isinstance(sub, str) else "unknown",
                    extra={
                        "attachment": attachment if isinstance(attachment, dict) else {},
                        "rendered": rendered if isinstance(rendered, str) else None,
                    },
                )
            )
            index += 1
            continue

        if kind == "system":
            content = entry.get("content")
            messages.append(
                ParsedMessage(
                    index=index,
                    line=number,
                    uuid=entry.get("uuid"),
                    parent_uuid=entry.get("parentUuid"),
                    kind="system",
                    role="system",
                    timestamp=entry.get("timestamp"),
                    model=None,
                    is_sidechain=bool(entry.get("isSidechain")),
                    is_meta=bool(entry.get("isMeta")),
                    is_error=entry.get("level") in {"error", "warning"},
                    blocks=[{"type": "text", "text": content}] if isinstance(content, str) else [],
                    subtype=entry.get("subtype") if isinstance(entry.get("subtype"), str) else None,
                    extra={
                        k: entry[k]
                        for k in ("durationMs", "messageCount", "level", "compactMetadata",
                                  "originalModel", "fallbackModel", "apiRefusalCategory")
                        if k in entry
                    },
                )
            )
            index += 1
            continue

        if kind not in {"user", "assistant"}:
            continue

        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        blocks = block_list(message)
        clean: List[Dict[str, Any]] = []
        for block in blocks:
            btype = block.get("type")
            if btype == "tool_use":
                name = block.get("name") if isinstance(block.get("name"), str) else "unknown"
                tool_id = block.get("id")
                if isinstance(tool_id, str):
                    tool_names[tool_id] = name
                caller = block.get("caller") if isinstance(block.get("caller"), dict) else {}
                clean.append(
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": block.get("input"),
                        "caller": caller.get("type", "direct"),
                    }
                )
            elif btype == "tool_result":
                tool_id = block.get("tool_use_id")
                clean.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "name": tool_names.get(tool_id) if isinstance(tool_id, str) else None,
                        "is_error": bool(block.get("is_error")),
                        "content": _clean_tool_result_content(block.get("content"), tool_output_limit),
                    }
                )
            elif btype == "thinking":
                thinking = block.get("thinking")
                clean.append(
                    {"type": "thinking", "text": thinking if isinstance(thinking, str) else ""}
                )
            elif btype == "text":
                text = block.get("text")
                clean.append({"type": "text", "text": text if isinstance(text, str) else str(text)})
            elif btype == "image":
                source = block.get("source") if isinstance(block.get("source"), dict) else {}
                data = source.get("data")
                clean.append(
                    {
                        "type": "image",
                        "media_type": source.get("media_type", "image/png"),
                        "bytes": len(data) if isinstance(data, str) else 0,
                    }
                )
            else:
                clean.append({"type": btype or "unknown", "raw": block})

        if kind == "user":
            role = "user"
            is_tool_result = entry.get("toolUseResult") is not None or (
                bool(clean) and all(b["type"] == "tool_result" for b in clean)
            )
            entry_kind = "tool_result" if is_tool_result else "user"
        else:
            role = "assistant"
            entry_kind = "assistant"

        usage = None
        if kind == "assistant":
            usage = _usage_of(entry)
            # Claude Code writes one line per content block of a single API
            # response (SCHEMA.md 3.5.1).  Merge those back into one turn so
            # the viewer shows 86 assistant messages rather than 156, and so
            # the usage figures are not repeated per block.
            api_key = api_message_key(entry)
            if messages and messages[-1].kind == "assistant" and last_api_key == api_key:
                previous = messages[-1]
                previous.blocks.extend(clean)
                if entry.get("timestamp"):
                    previous.extra["last_timestamp"] = entry["timestamp"]
                previous.extra.setdefault("merged_uuids", []).append(entry.get("uuid"))
                previous.is_error = previous.is_error or bool(
                    entry.get("isApiErrorMessage") or entry.get("error")
                )
                continue
            last_api_key = api_key
        else:
            last_api_key = None

        messages.append(
            ParsedMessage(
                index=index,
                line=number,
                uuid=entry.get("uuid"),
                parent_uuid=entry.get("parentUuid"),
                kind=entry_kind,
                role=role,
                timestamp=entry.get("timestamp"),
                model=message.get("model") if isinstance(message.get("model"), str) else None,
                is_sidechain=bool(entry.get("isSidechain")),
                is_meta=bool(entry.get("isMeta")),
                is_error=bool(entry.get("isApiErrorMessage") or entry.get("error")),
                blocks=clean,
                usage=usage,
                extra={
                    k: entry[k]
                    for k in ("requestId", "effort", "stop_reason", "sourceToolAssistantUUID")
                    if k in entry
                },
            )
        )
        index += 1

    if not info["title"]:
        for message_obj in messages:
            if message_obj.kind == "user":
                info["title"] = _truncate(text_of_blocks(message_obj.blocks), 120)
                break
    if not info["title"]:
        info["title"] = file_path.stem

    try:
        info["file_size"] = file_path.stat().st_size
    except OSError:
        info["file_size"] = 0
    info["message_count"] = len(messages)
    info["corrupt_lines"] = len(errors)

    return {
        "info": info,
        "messages": [m.to_dict() for m in messages],
        "errors": errors,
    }


def file_signature(path: Path | str) -> Tuple[str, float, int] | None:
    """Return ``(path, mtime, size)`` used as the cache key, or ``None``."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return str(path), stat.st_mtime, stat.st_size
