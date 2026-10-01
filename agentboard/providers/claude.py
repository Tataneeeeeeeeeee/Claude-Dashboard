"""Claude Code: transcripts under ``~/.claude/projects``.

This module is the Claude Code adapter.  The format notes it relies on are
in ``SCHEMA.md``.

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

from pathlib import Path
from typing import Any, Dict, Iterator, List

from ..model import (
    EMPTY_DAY_TOTALS,
    EMPTY_MODEL_TOTALS,
    EMPTY_USAGE,
    TOKEN_KEYS as _TOKEN_KEYS,
    ParsedMessage,
    SessionMeta,
    parse_timestamp,
)
from ..paths import claude_home, claude_projects_dir
from .base import Capabilities, ProviderAdapter, SearchEntry
from .common import (
    block_list,
    clean_tool_result_content,
    file_signature,
    iter_entries,
    iter_lines,
    text_of_blocks,
    truncate,
)

__all__ = [
    "ClaudeAdapter",
    "searchable_text",
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
                    note_file_touched(meta, block.get("input"))

    meta.tokens = tokens
    meta.versions = versions
    if summary_title:
        meta.title, meta.title_source = truncate(summary_title, 160), "summary"
    elif ai_title:
        meta.title, meta.title_source = truncate(ai_title, 160), "ai-title"
    elif first_prompt.strip():
        meta.title, meta.title_source = truncate(first_prompt, 120), "first-message"
    elif last_prompt_entry:
        meta.title, meta.title_source = truncate(last_prompt_entry, 120), "last-prompt"
    else:
        meta.title, meta.title_source = meta.session_id, "session-id"
    meta.preview = truncate(first_prompt or last_prompt_entry or "", 240)
    if not meta.project_path:
        meta.project_path = ""
    return meta


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
                        "content": clean_tool_result_content(block.get("content"), tool_output_limit),
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
                info["title"] = truncate(text_of_blocks(message_obj.blocks), 120)
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




#: Tool input keys that name a file the tool read or changed.
FILE_INPUT_KEYS = ("file_path", "notebook_path")

#: At most this many distinct paths are kept per session.
MAX_FILES_TOUCHED = 500


def note_file_touched(meta: SessionMeta, tool_input: Any) -> None:
    """Record the file a ``Read``/``Edit``/``Write``-style call worked on."""
    if not isinstance(tool_input, dict) or len(meta.files_touched) >= MAX_FILES_TOUCHED:
        return
    for key in FILE_INPUT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value and value not in meta.files_touched:
            meta.files_touched.append(value)


def searchable_text(entry: Dict[str, Any]) -> str:
    """Flatten one transcript entry into the text search should look at.

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


class ClaudeAdapter(ProviderAdapter):
    """Anthropic's Claude Code CLI."""

    id = "claude"
    name = "Claude Code"
    monogram = "C"
    color = "#d97757"
    description = "Anthropic's terminal coding agent. Reads ~/.claude/projects."
    binary_names = ("claude",)
    file_suffixes = (".jsonl",)
    instruction_file_names = ("CLAUDE.md", "CLAUDE.local.md")
    default_resume_command = "claude --resume {session_id}"
    # Claude's rows are the global pricing table itself (it predates other
    # providers), so nothing is layered underneath it.
    default_pricing: Dict[str, Dict[str, float]] = {}
    capabilities = Capabilities(
        cache_tokens=True,
        thinking=True,
        resume=True,
        delete=True,
        reported_cost=True,
        instructions=True,
        extras=True,
    )

    def __init__(self, settings: Dict[str, Any] | None = None, projects_dir: Path | None = None) -> None:
        super().__init__(settings)
        self._projects_dir = Path(projects_dir) if projects_dir is not None else None

    def default_home(self) -> Path:
        return claude_home()

    def data_root(self, home: Path) -> Path:
        return home / "projects"

    @property
    def root(self) -> Path:
        if self._projects_dir is not None:
            return self._projects_dir
        configured = self.settings.get("path")
        if isinstance(configured, str) and configured.strip():
            return self.data_root(Path(configured.strip()).expanduser())
        return claude_projects_dir()

    def session_files(self) -> List[Path]:
        """Every ``.jsonl`` directly inside a project directory, sorted.

        Symlinks are skipped, both files and directories.  The deletion
        guard refuses them anyway, so indexing one would put a session in
        the list that can never be deleted, and a "select all" would fail
        as a whole because deletion is all-or-nothing.
        """
        root = self.root
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

    def scan(self, path: Path) -> SessionMeta:
        return scan_session(path)

    def parse(
        self,
        path: Path,
        tool_output_limit: int = 20_000,
        include_attachments: bool = True,
    ) -> Dict[str, Any]:
        return parse_conversation(
            path, tool_output_limit=tool_output_limit, include_attachments=include_attachments
        )

    def search_entries(self, path: Path) -> Iterator[SearchEntry]:
        """Line-by-line, so a hit never pays for parsing the whole file."""
        for line_no, entry, error in iter_entries(path):
            if error is not None:
                continue
            kind = entry.get("type")
            if kind not in {"user", "assistant"}:
                continue
            text = searchable_text(entry)
            if text:
                yield SearchEntry(line_no, kind, text, entry.get("uuid"), entry.get("timestamp"))

    def owns(self, path: Path | str) -> bool:
        candidate = Path(path)
        return candidate.suffix == ".jsonl" and candidate.parent.parent == self.root

    def resume_command(self, session_id: str, config: Dict[str, Any] | None = None) -> str | None:
        """``claude --resume <id>``, honouring both the provider setting and
        the older ``terminal.resume_command`` key."""
        legacy = ((config or {}).get("terminal") or {}).get("resume_command")
        template = self.settings.get("resume_command") or legacy or self.default_resume_command
        return str(template).replace("{session_id}", session_id)
