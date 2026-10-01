"""OpenAI Codex CLI: rollouts under ``~/.codex/sessions``.

Codex writes one JSONL "rollout" per session at
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl``.
Current versions wrap every line in an envelope::

    {"timestamp": "...", "type": "session_meta",  "payload": {"id", "cwd", "cli_version", "git": {...}}}
    {"timestamp": "...", "type": "turn_context",  "payload": {"cwd", "model", ...}}
    {"timestamp": "...", "type": "response_item", "payload": {"type": "message", "role", "content": [...]}}
    {"timestamp": "...", "type": "response_item", "payload": {"type": "function_call", "name", "arguments", "call_id"}}
    {"timestamp": "...", "type": "response_item", "payload": {"type": "function_call_output", "call_id", "output"}}
    {"timestamp": "...", "type": "event_msg",     "payload": {"type": "token_count", "info": {"total_token_usage": {...}}}}

Older versions wrote the payloads bare, with a first line holding the
session ``id``; both shapes are read.

Two details matter for correct numbers:

* ``token_count`` events carry a running ``total_token_usage``, and the
  same total can be emitted more than once.  Usage is charged as the
  *increase* between consecutive totals, so repeats add nothing.
* OpenAI counts cached prompt tokens inside ``input_tokens``.  They are
  split out into ``cache_read`` so they can be priced at the cached rate.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from ..model import EMPTY_DAY_TOTALS, EMPTY_MODEL_TOTALS, ParsedMessage, SessionMeta, empty_usage, parse_timestamp
from .base import Capabilities, ProviderAdapter
from .common import clean_tool_result_content, iter_entries, truncate

__all__ = ["CodexAdapter", "INJECTED_PREFIXES"]

#: User-role messages that Codex injects rather than the person typing them.
INJECTED_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "# AGENTS.md instructions",
    "<user_shell_command>",
    "<turn_aborted>",
)

#: Response item types that are a tool invocation.
TOOL_CALL_TYPES = frozenset({"function_call", "custom_tool_call", "local_shell_call", "web_search_call"})

#: Response item types that are a tool's answer.
TOOL_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})

_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)


def _envelope(entry: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Return ``(kind, payload)`` for either line shape.

    A bare legacy item is reported as a ``response_item``; the legacy first
    line (an ``id`` with no ``type``) as ``session_meta``.
    """
    kind = entry.get("type")
    payload = entry.get("payload")
    if isinstance(payload, dict) and isinstance(kind, str):
        return kind, payload
    if kind in {"message", "reasoning"} or kind in TOOL_CALL_TYPES or kind in TOOL_OUTPUT_TYPES:
        return "response_item", entry
    if kind is None and "id" in entry and "record_type" not in entry:
        return "session_meta", entry
    return str(kind or "unknown"), entry


def _message_text(payload: Dict[str, Any]) -> str:
    """The text of a ``message`` item, whose content is a list of parts."""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    parts: List[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
    return "\n".join(parts)


def _is_injected(text: str) -> bool:
    """Whether a user-role message was injected by Codex itself."""
    return text.lstrip().startswith(INJECTED_PREFIXES)


def _tool_input(payload: Dict[str, Any]) -> Any:
    """Decode a tool call's arguments into something displayable."""
    kind = payload.get("type")
    if kind == "custom_tool_call":
        return {"input": payload.get("input")}
    if kind == "local_shell_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        return {"command": action.get("command")}
    if kind == "web_search_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        return {"query": action.get("query")}
    raw = payload.get("arguments")
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {"arguments": raw}
    else:
        decoded = raw
    if isinstance(decoded, dict) and isinstance(decoded.get("command"), list):
        # Shell calls arrive as argv; a joined line reads and searches better.
        decoded = {**decoded, "command": " ".join(str(part) for part in decoded["command"])}
    return decoded


def _tool_name(payload: Dict[str, Any]) -> str:
    """A tool call's display name."""
    name = payload.get("name")
    if isinstance(name, str) and name:
        return name
    return {"local_shell_call": "shell", "web_search_call": "web_search"}.get(
        str(payload.get("type")), "unknown"
    )


def _tool_output(payload: Dict[str, Any]) -> tuple[str, bool]:
    """``(text, is_error)`` for a tool's output, unwrapping Codex's JSON."""
    raw = payload.get("output")
    if isinstance(raw, dict):
        text = raw.get("content") if isinstance(raw.get("content"), str) else json.dumps(raw)
        return text, raw.get("success") is False
    if not isinstance(raw, str):
        return ("" if raw is None else str(raw)), False
    try:
        decoded = json.loads(raw)
    except ValueError:
        return raw, False
    if isinstance(decoded, dict) and "output" in decoded:
        metadata = decoded.get("metadata") if isinstance(decoded.get("metadata"), dict) else {}
        exit_code = metadata.get("exit_code")
        return str(decoded.get("output")), isinstance(exit_code, int) and exit_code != 0
    return raw, False


def _files_from_call(payload: Dict[str, Any]) -> List[str]:
    """Files named by an ``apply_patch`` call."""
    text = ""
    if payload.get("type") == "custom_tool_call":
        text = payload.get("input") if isinstance(payload.get("input"), str) else ""
    else:
        arguments = _tool_input(payload)
        if isinstance(arguments, dict):
            for value in arguments.values():
                if isinstance(value, str) and "*** Begin Patch" in value:
                    text = value
                    break
    return [match.strip() for match in _PATCH_FILE_RE.findall(text or "")]


def _usage_delta(previous: Dict[str, int] | None, current: Dict[str, Any]) -> Dict[str, int]:
    """Normalized usage for the increase from *previous* to *current* totals."""
    def value(source: Dict[str, Any] | None, key: str) -> int:
        if not isinstance(source, dict):
            return 0
        raw = source.get(key)
        return int(raw) if isinstance(raw, (int, float)) else 0

    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    if previous is not None and any(value(current, f) < value(previous, f) for f in fields):
        previous = None  # the counter was reset; count from zero
    delta = {f: value(current, f) - value(previous, f) for f in fields}

    usage = empty_usage()
    cached = max(0, delta["cached_input_tokens"])
    usage["input"] = max(0, delta["input_tokens"] - cached)
    usage["cache_read"] = cached
    usage["output"] = max(0, delta["output_tokens"])
    usage["thinking"] = max(0, delta["reasoning_output_tokens"])
    return usage


def _usage_of_last(info: Dict[str, Any]) -> Dict[str, int] | None:
    """Normalized usage of the most recent request in a ``token_count``."""
    last = info.get("last_token_usage")
    if not isinstance(last, dict):
        return None
    return _usage_delta(None, last)


class CodexAdapter(ProviderAdapter):
    """OpenAI's Codex CLI."""

    id = "codex"
    name = "Codex CLI"
    monogram = "Cx"
    assistant_label = "Codex"
    color = "#10a37f"
    description = "OpenAI's terminal coding agent. Reads ~/.codex/sessions."
    binary_names = ("codex",)
    file_suffixes = (".jsonl",)
    instruction_file_names = ("AGENTS.md",)
    default_resume_command = "codex resume {session_id}"
    # US dollars per million tokens. Local estimates, editable in Settings.
    default_pricing = {
        "gpt-5":             {"input": 1.25, "output": 10.00, "cache_read": 0.125},
        "gpt-5-codex":       {"input": 1.25, "output": 10.00, "cache_read": 0.125},
        "gpt-5-mini":        {"input": 0.25, "output":  2.00, "cache_read": 0.025},
        "gpt-5-nano":        {"input": 0.05, "output":  0.40, "cache_read": 0.005},
        "gpt-4.1":           {"input": 2.00, "output":  8.00, "cache_read": 0.50},
        "o3":                {"input": 2.00, "output":  8.00, "cache_read": 0.50},
        "o4-mini":           {"input": 1.10, "output":  4.40, "cache_read": 0.275},
        "codex-mini-latest": {"input": 1.50, "output":  6.00, "cache_read": 0.375},
        "default":           {"input": 1.25, "output": 10.00, "cache_read": 0.125},
    }
    capabilities = Capabilities(cache_tokens=True, thinking=True, resume=True)

    def default_home(self) -> Path:
        configured = os.environ.get("CODEX_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".codex"

    def data_root(self, home: Path) -> Path:
        return home / "sessions"

    def session_files(self) -> List[Path]:
        root = self.root
        if not root.is_dir():
            return []
        found: List[Path] = []
        try:
            for path in root.rglob("*.jsonl"):
                if path.is_symlink() or not path.is_file():
                    continue
                if any(part.is_symlink() for part in path.parents if root in part.parents):
                    continue
                found.append(path)
        except OSError:
            return []
        return sorted(found)

    # ---------------------------------------------------------------- scan

    def scan(self, path: Path) -> SessionMeta:
        file_path = Path(path)
        match = _UUID_RE.search(file_path.name)
        meta = SessionMeta(
            session_id=match.group(1) if match else file_path.stem,
            path=str(file_path),
            project_dir=file_path.parent.name,
            provider=self.id,
        )
        try:
            stat = file_path.stat()
            meta.file_size, meta.mtime = stat.st_size, stat.st_mtime
        except OSError:
            return meta

        tokens = empty_usage()
        model = "unknown"
        previous_total: Dict[str, int] | None = None
        first_prompt = ""
        versions: List[str] = []

        for _, entry, error in iter_entries(file_path):
            meta.line_count += 1
            if error is not None:
                meta.corrupt_lines += 1
                continue
            kind, payload = _envelope(entry)
            stamp = entry.get("timestamp") or payload.get("timestamp")
            if isinstance(stamp, str) and stamp:
                if meta.first_timestamp is None or stamp < meta.first_timestamp:
                    meta.first_timestamp = stamp
                if meta.last_timestamp is None or stamp > meta.last_timestamp:
                    meta.last_timestamp = stamp
            day = stamp[:10] if isinstance(stamp, str) and len(stamp) >= 10 else None

            if kind == "session_meta":
                identifier = payload.get("id")
                if isinstance(identifier, str) and identifier:
                    meta.session_id = identifier
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    meta.project_path = cwd
                version = payload.get("cli_version")
                if isinstance(version, str) and version and version not in versions:
                    versions.append(version)
                git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
                if isinstance(git.get("branch"), str) and git["branch"]:
                    meta.git_branch = git["branch"]
                continue
            if kind == "turn_context":
                if isinstance(payload.get("model"), str) and payload["model"]:
                    model = payload["model"]
                if not meta.project_path and isinstance(payload.get("cwd"), str):
                    meta.project_path = payload["cwd"]
                continue
            if kind == "event_msg":
                if payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
                    total = payload["info"].get("total_token_usage")
                    if not isinstance(total, dict):
                        continue
                    usage = _usage_delta(previous_total, total)
                    previous_total = {k: v for k, v in total.items() if isinstance(v, (int, float))}
                    if not any(usage.values()):
                        continue
                    for key, value in usage.items():
                        tokens[key] += value
                    row = meta.models.setdefault(model, dict(EMPTY_MODEL_TOTALS))
                    for key in EMPTY_MODEL_TOTALS:
                        if key != "messages":
                            row[key] += usage.get(key, 0)
                    if day:
                        bucket = meta.daily.setdefault(day, dict(EMPTY_DAY_TOTALS))
                        for key in EMPTY_MODEL_TOTALS:
                            if key != "messages":
                                bucket[key] += usage.get(key, 0)
                elif payload.get("type") in {"error", "stream_error"}:
                    meta.error_messages += 1
                continue
            if kind != "response_item":
                continue

            item_type = payload.get("type")
            when = parse_timestamp(stamp)
            if item_type == "message":
                role = payload.get("role")
                text = _message_text(payload)
                if role == "user":
                    if _is_injected(text) or not text.strip():
                        continue
                    meta.user_messages += 1
                    meta.message_count += 1
                    if not first_prompt:
                        first_prompt = text
                elif role == "assistant":
                    meta.assistant_messages += 1
                    meta.message_count += 1
                    meta.models.setdefault(model, dict(EMPTY_MODEL_TOTALS))["messages"] += 1
                    if day:
                        meta.daily.setdefault(day, dict(EMPTY_DAY_TOTALS))["messages"] += 1
                else:
                    continue
                if when is not None:
                    slot = f"{when.weekday()}-{when.hour}"
                    meta.heatmap[slot] = meta.heatmap.get(slot, 0) + 1
            elif item_type in TOOL_CALL_TYPES:
                name = _tool_name(payload)
                meta.tools[name] = meta.tools.get(name, 0) + 1
                meta.tool_calls += 1
                if day:
                    meta.daily.setdefault(day, dict(EMPTY_DAY_TOTALS))["tool_calls"] += 1
                for touched in _files_from_call(payload):
                    if touched not in meta.files_touched and len(meta.files_touched) < 500:
                        meta.files_touched.append(touched)

        meta.tokens = tokens
        meta.versions = versions
        if first_prompt.strip():
            meta.title, meta.title_source = truncate(first_prompt, 120), "first-message"
        else:
            meta.title, meta.title_source = meta.session_id, "session-id"
        meta.preview = truncate(first_prompt, 240)
        # Rollouts are filed by date, not project, so the folder name says
        # nothing; the recorded working directory is the project.
        meta.project_dir = meta.project_path or "unknown"
        return meta

    # --------------------------------------------------------------- parse

    def parse(
        self,
        path: Path,
        tool_output_limit: int = 20_000,
        include_attachments: bool = True,
    ) -> Dict[str, Any]:
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
        model: str | None = None
        current: ParsedMessage | None = None  # the assistant turn being built

        def push(message: ParsedMessage) -> None:
            message.index = len(messages)
            messages.append(message)

        def flush() -> None:
            nonlocal current
            if current is not None and current.blocks:
                push(current)
            current = None

        def assistant(number: int, stamp: Any) -> ParsedMessage:
            nonlocal current
            if current is None:
                current = ParsedMessage(
                    index=0, line=number, uuid=None, parent_uuid=None, kind="assistant",
                    role="assistant", timestamp=stamp, model=model, is_sidechain=False,
                    is_meta=False, is_error=False, blocks=[],
                )
            return current

        for number, entry, error in iter_entries(file_path):
            if error is not None:
                errors.append({"line": number, "error": error})
                continue
            kind, payload = _envelope(entry)
            stamp = entry.get("timestamp") or payload.get("timestamp")

            if kind == "session_meta":
                match = _UUID_RE.search(file_path.name)
                info["session_id"] = payload.get("id") or (match.group(1) if match else file_path.stem)
                info["cwd"] = payload.get("cwd") or info["cwd"]
                git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
                info["git_branch"] = git.get("branch") or ""
                if isinstance(payload.get("cli_version"), str):
                    info["versions"].append(payload["cli_version"])
                continue
            if kind == "turn_context":
                model = payload.get("model") if isinstance(payload.get("model"), str) else model
                info["cwd"] = info["cwd"] or payload.get("cwd") or ""
                continue
            if kind == "event_msg":
                event = payload.get("type")
                if event == "token_count" and isinstance(payload.get("info"), dict):
                    usage = _usage_of_last(payload["info"])
                    target = current or next(
                        (m for m in reversed(messages) if m.kind == "assistant"), None
                    )
                    if target is not None and usage is not None and target.usage is None:
                        target.usage = usage
                elif event in {"error", "stream_error", "turn_aborted"}:
                    flush()
                    push(ParsedMessage(
                        index=0, line=number, uuid=None, parent_uuid=None, kind="system",
                        role="system", timestamp=stamp, model=None, is_sidechain=False,
                        is_meta=False, is_error=event != "turn_aborted",
                        blocks=[{"type": "text", "text": str(payload.get("message") or payload.get("reason") or event)}],
                        subtype=event,
                    ))
                continue
            if kind != "response_item":
                continue

            item_type = payload.get("type")
            if item_type == "message":
                text = _message_text(payload)
                role = payload.get("role")
                if role == "assistant":
                    assistant(number, stamp).blocks.append({"type": "text", "text": text})
                    continue
                flush()
                if role == "user" and _is_injected(text):
                    if include_attachments:
                        subtype = text.lstrip().split(">", 1)[0].lstrip("<#").strip() or "context"
                        push(ParsedMessage(
                            index=0, line=number, uuid=None, parent_uuid=None, kind="attachment",
                            role="system", timestamp=stamp, model=None, is_sidechain=False,
                            is_meta=True, is_error=False, blocks=[], subtype=subtype[:40],
                            extra={"attachment": {"type": subtype[:40]}, "rendered": text},
                        ))
                    continue
                if role == "user":
                    push(ParsedMessage(
                        index=0, line=number, uuid=None, parent_uuid=None, kind="user",
                        role="user", timestamp=stamp, model=None, is_sidechain=False,
                        is_meta=False, is_error=False, blocks=[{"type": "text", "text": text}],
                    ))
                    if not info["title"] and text.strip():
                        info["title"] = truncate(text, 120)
                continue
            if item_type == "reasoning":
                summary = payload.get("summary")
                parts = [
                    part.get("text", "") for part in summary
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                ] if isinstance(summary, list) else []
                assistant(number, stamp).blocks.append({"type": "thinking", "text": "\n\n".join(parts)})
                continue
            if item_type in TOOL_CALL_TYPES:
                call_id = payload.get("call_id") or payload.get("id")
                name = _tool_name(payload)
                if isinstance(call_id, str):
                    tool_names[call_id] = name
                assistant(number, stamp).blocks.append({
                    "type": "tool_use", "id": call_id, "name": name,
                    "input": _tool_input(payload), "caller": "direct",
                })
                continue
            if item_type in TOOL_OUTPUT_TYPES:
                flush()
                call_id = payload.get("call_id")
                text, failed = _tool_output(payload)
                push(ParsedMessage(
                    index=0, line=number, uuid=None, parent_uuid=None, kind="tool_result",
                    role="user", timestamp=stamp, model=None, is_sidechain=False,
                    is_meta=False, is_error=False,
                    blocks=[{
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "name": tool_names.get(call_id) if isinstance(call_id, str) else None,
                        "is_error": failed,
                        "content": clean_tool_result_content(text, tool_output_limit),
                    }],
                ))
        flush()

        if not info["title"]:
            info["title"] = info["session_id"]
        try:
            info["file_size"] = file_path.stat().st_size
        except OSError:
            info["file_size"] = 0
        info["message_count"] = len(messages)
        info["corrupt_lines"] = len(errors)
        return {"info": info, "messages": [m.to_dict() for m in messages], "errors": errors}
