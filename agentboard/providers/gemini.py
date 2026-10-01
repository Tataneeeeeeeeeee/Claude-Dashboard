"""Google Gemini CLI: chats under ``~/.gemini/tmp/<project-hash>/chats``.

Gemini CLI records each conversation as one JSON document, rewritten in
place as the chat grows::

    {
      "sessionId": "...", "projectHash": "<sha256 of the cwd>",
      "startTime": "...", "lastUpdated": "...", "summary": "...",
      "messages": [
        {"id", "timestamp", "type": "user",   "content": "..."},
        {"id", "timestamp", "type": "gemini", "content": "...", "model": "gemini-2.5-pro",
         "thoughts": [{"subject", "description"}],
         "tokens": {"input", "output", "cached", "thoughts", "tool", "total"},
         "toolCalls": [{"id", "name", "args", "result", "status"}]},
        {"type": "info" | "error" | "warning", "content": "..."}
      ]
    }

The working directory is only stored as a hash.  It is recovered by hashing
the directories other sessions recorded (or a ``.project_root`` file, which
some versions write next to ``chats/``); otherwise the project is shown by
its hash.

``tokens.input`` includes cached tokens, and thoughts are billed as output,
so both are rearranged to fit the normalized counters.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Set

from ..model import EMPTY_DAY_TOTALS, EMPTY_MODEL_TOTALS, ParsedMessage, SessionMeta, empty_usage, parse_timestamp
from .base import Capabilities, ProviderAdapter
from .common import clean_tool_result_content, truncate

__all__ = ["GeminiAdapter", "project_hash"]

#: Gemini tool inputs that name a file.
FILE_ARGS = ("file_path", "absolute_path", "path")

#: At most this many distinct paths are kept per session.
_MAX_FILES = 500


def project_hash(path: str) -> str:
    """The hash Gemini CLI uses for a project directory."""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _parts_text(content: Any) -> str:
    """Text of a message's ``content``: a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    parts: List[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "\n".join(parts)


def _result_text(result: Any) -> str:
    """Readable text of a tool call's ``result`` parts."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    items = result if isinstance(result, list) else [result]
    parts: List[str] = []
    for item in items:
        if isinstance(item, dict):
            response = item.get("functionResponse")
            if isinstance(response, dict):
                payload = response.get("response")
                if isinstance(payload, dict) and isinstance(payload.get("output"), str):
                    parts.append(payload["output"])
                elif payload is not None:
                    parts.append(json.dumps(payload, ensure_ascii=False))
            elif isinstance(item.get("text"), str):
                parts.append(item["text"])
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _usage(tokens: Any) -> Dict[str, int]:
    """Normalized usage from a message's ``tokens`` record."""
    usage = empty_usage()
    if not isinstance(tokens, dict):
        return usage

    def value(key: str) -> int:
        raw = tokens.get(key)
        return int(raw) if isinstance(raw, (int, float)) else 0

    cached = value("cached")
    usage["input"] = max(0, value("input") - cached) + value("tool")
    usage["cache_read"] = cached
    usage["output"] = value("output") + value("thoughts")
    usage["thinking"] = value("thoughts")
    return usage


def _load(path: Path) -> Dict[str, Any] | None:
    """Read one chat file, or ``None`` if it is not a usable document."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class GeminiAdapter(ProviderAdapter):
    """Google's Gemini CLI."""

    id = "gemini"
    name = "Gemini CLI"
    monogram = "G"
    assistant_label = "Gemini"
    color = "#4285f4"
    description = "Google's terminal coding agent. Reads ~/.gemini/tmp/*/chats."
    binary_names = ("gemini",)
    file_suffixes = (".json",)
    instruction_file_names = ("GEMINI.md",)
    # US dollars per million tokens. Local estimates, editable in Settings.
    default_pricing = {
        "gemini-2.5-pro":        {"input": 1.25, "output": 10.00, "cache_read": 0.125},
        "gemini-2.5-flash":      {"input": 0.30, "output":  2.50, "cache_read": 0.03},
        "gemini-2.5-flash-lite": {"input": 0.10, "output":  0.40, "cache_read": 0.01},
        "gemini-3-pro":          {"input": 2.00, "output": 12.00, "cache_read": 0.20},
        "default":               {"input": 1.25, "output": 10.00, "cache_read": 0.125},
    }
    capabilities = Capabilities(cache_tokens=True, thinking=True)

    def default_home(self) -> Path:
        # GEMINI_CLI_HOME stands in for the home directory, as in the CLI.
        override = os.environ.get("GEMINI_CLI_HOME")
        base = Path(override).expanduser() if override else Path.home()
        return base / ".gemini"

    def data_root(self, home: Path) -> Path:
        return home / "tmp"

    def session_files(self) -> List[Path]:
        root = self.root
        if not root.is_dir():
            return []
        found: List[Path] = []
        try:
            for project in sorted(root.iterdir()):
                chats = project / "chats"
                if project.is_symlink() or chats.is_symlink() or not chats.is_dir():
                    continue
                for path in sorted(chats.glob("*.json")):
                    if path.is_file() and not path.is_symlink():
                        found.append(path)
        except OSError:
            return []
        return found

    def owns(self, path: Path | str) -> bool:
        candidate = Path(path)
        return (
            candidate.suffix == ".json"
            and candidate.parent.name == "chats"
            and candidate.parent.parent.parent == self.root
        )

    # ---------------------------------------------------------------- scan

    def scan(self, path: Path) -> SessionMeta:
        file_path = Path(path)
        project_dir = file_path.parent.parent.name
        meta = SessionMeta(
            session_id=file_path.stem,
            path=str(file_path),
            project_dir=project_dir,
            provider=self.id,
        )
        try:
            stat = file_path.stat()
            meta.file_size, meta.mtime = stat.st_size, stat.st_mtime
        except OSError:
            return meta
        data = _load(file_path)
        if data is None:
            meta.corrupt_lines = 1
            meta.title, meta.title_source = file_path.stem, "session-id"
            return meta

        if isinstance(data.get("sessionId"), str) and data["sessionId"]:
            meta.session_id = data["sessionId"]
        if isinstance(data.get("projectHash"), str) and data["projectHash"]:
            meta.project_dir = data["projectHash"]
        meta.project_path = self._recorded_root(file_path)

        tokens = empty_usage()
        first_prompt = ""
        raw_messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        meta.line_count = len(raw_messages)
        for message in raw_messages:
            if not isinstance(message, dict):
                meta.corrupt_lines += 1
                continue
            kind = message.get("type")
            stamp = message.get("timestamp")
            if isinstance(stamp, str) and stamp:
                if meta.first_timestamp is None or stamp < meta.first_timestamp:
                    meta.first_timestamp = stamp
                if meta.last_timestamp is None or stamp > meta.last_timestamp:
                    meta.last_timestamp = stamp
            day = stamp[:10] if isinstance(stamp, str) and len(stamp) >= 10 else None
            when = parse_timestamp(stamp)

            if kind == "user":
                text = _parts_text(message.get("content"))
                if not text.strip() or text.lstrip().startswith("/"):
                    continue  # slash commands are not prompts
                meta.user_messages += 1
                meta.message_count += 1
                if not first_prompt:
                    first_prompt = text
            elif kind == "gemini":
                meta.assistant_messages += 1
                meta.message_count += 1
                model = message.get("model") if isinstance(message.get("model"), str) else "gemini"
                usage = _usage(message.get("tokens"))
                for key, value in usage.items():
                    tokens[key] += value
                row = meta.models.setdefault(model, dict(EMPTY_MODEL_TOTALS))
                row["messages"] += 1
                bucket = meta.daily.setdefault(day, dict(EMPTY_DAY_TOTALS)) if day else None
                if bucket is not None:
                    bucket["messages"] += 1
                for key in EMPTY_MODEL_TOTALS:
                    if key == "messages":
                        continue
                    row[key] += usage.get(key, 0)
                    if bucket is not None:
                        bucket[key] += usage.get(key, 0)
                for call in message.get("toolCalls") or []:
                    if not isinstance(call, dict):
                        continue
                    name = call.get("name") if isinstance(call.get("name"), str) else "unknown"
                    meta.tools[name] = meta.tools.get(name, 0) + 1
                    meta.tool_calls += 1
                    if bucket is not None:
                        bucket["tool_calls"] += 1
                    if call.get("status") == "error":
                        meta.error_messages += 1
                    args = call.get("args") if isinstance(call.get("args"), dict) else {}
                    for key in FILE_ARGS:
                        value = args.get(key)
                        if (isinstance(value, str) and value and value not in meta.files_touched
                                and len(meta.files_touched) < _MAX_FILES):
                            meta.files_touched.append(value)
            elif kind == "error":
                meta.error_messages += 1
                continue
            else:
                continue
            if when is not None:
                slot = f"{when.weekday()}-{when.hour}"
                meta.heatmap[slot] = meta.heatmap.get(slot, 0) + 1

        meta.first_timestamp = meta.first_timestamp or data.get("startTime")
        meta.last_timestamp = meta.last_timestamp or data.get("lastUpdated")
        meta.tokens = tokens
        summary = data.get("summary")
        if isinstance(summary, str) and summary.strip():
            meta.title, meta.title_source = truncate(summary, 160), "summary"
        elif first_prompt.strip():
            meta.title, meta.title_source = truncate(first_prompt, 120), "first-message"
        else:
            meta.title, meta.title_source = meta.session_id, "session-id"
        meta.preview = truncate(first_prompt, 240)
        return meta

    @staticmethod
    def _recorded_root(chat_file: Path) -> str:
        """The project path from a ``.project_root`` marker, if one exists."""
        marker = chat_file.parent.parent / ".project_root"
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return text if text.startswith(("/", "\\")) or (len(text) > 2 and text[1] == ":") else ""

    def guess_project_path(self, meta: SessionMeta, known_paths: Set[str]) -> str:
        """Find the known directory whose hash names this project."""
        for candidate in known_paths:
            if project_hash(candidate) == meta.project_dir:
                return candidate
        home = str(Path.home())
        if project_hash(home) == meta.project_dir:
            return home
        return ""

    # --------------------------------------------------------------- parse

    def parse(
        self,
        path: Path,
        tool_output_limit: int = 20_000,
        include_attachments: bool = True,
    ) -> Dict[str, Any]:
        file_path = Path(path)
        data = _load(file_path)
        info: Dict[str, Any] = {
            "session_id": file_path.stem,
            "path": str(file_path),
            "title": "",
            "cwd": self._recorded_root(file_path),
            "git_branch": "",
            "versions": [],
            "cost_state": None,
        }
        messages: List[ParsedMessage] = []
        errors: List[Dict[str, Any]] = []
        if data is None:
            errors.append({"line": 1, "error": "not a readable JSON document"})
            info["title"] = file_path.stem
            return {"info": info, "messages": [], "errors": errors}

        info["session_id"] = data.get("sessionId") or info["session_id"]
        raw = data.get("messages") if isinstance(data.get("messages"), list) else []

        def push(message: ParsedMessage) -> None:
            message.index = len(messages)
            messages.append(message)

        for position, message in enumerate(raw, start=1):
            if not isinstance(message, dict):
                errors.append({"line": position, "error": "message is not an object"})
                continue
            kind = message.get("type")
            stamp = message.get("timestamp")
            uuid = message.get("id") if isinstance(message.get("id"), str) else None
            text = _parts_text(message.get("content"))

            if kind == "user":
                push(ParsedMessage(
                    index=0, line=position, uuid=uuid, parent_uuid=None, kind="user",
                    role="user", timestamp=stamp, model=None, is_sidechain=False,
                    is_meta=text.lstrip().startswith("/"), is_error=False,
                    blocks=[{"type": "text", "text": text}],
                ))
                if not info["title"] and text.strip() and not text.lstrip().startswith("/"):
                    info["title"] = truncate(text, 120)
            elif kind == "gemini":
                blocks: List[Dict[str, Any]] = []
                for thought in message.get("thoughts") or []:
                    if isinstance(thought, dict):
                        subject = thought.get("subject") or ""
                        body = thought.get("description") or ""
                        joined = f"**{subject}**\n\n{body}".strip() if subject else str(body)
                        blocks.append({"type": "thinking", "text": joined})
                if text:
                    blocks.append({"type": "text", "text": text})
                calls = [c for c in (message.get("toolCalls") or []) if isinstance(c, dict)]
                for call in calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": call.get("name") or "unknown",
                        "input": call.get("args"),
                        "caller": "direct",
                    })
                model = message.get("model") if isinstance(message.get("model"), str) else None
                push(ParsedMessage(
                    index=0, line=position, uuid=uuid, parent_uuid=None, kind="assistant",
                    role="assistant", timestamp=stamp, model=model, is_sidechain=False,
                    is_meta=False, is_error=False, blocks=blocks,
                    usage=_usage(message.get("tokens")) if message.get("tokens") else None,
                ))
                for call in calls:
                    output = _result_text(call.get("result"))
                    if not output and isinstance(call.get("resultDisplay"), str):
                        output = call["resultDisplay"]
                    push(ParsedMessage(
                        index=0, line=position, uuid=None, parent_uuid=uuid, kind="tool_result",
                        role="user", timestamp=call.get("timestamp") or stamp, model=None,
                        is_sidechain=False, is_meta=False, is_error=False,
                        blocks=[{
                            "type": "tool_result",
                            "tool_use_id": call.get("id"),
                            "name": call.get("name"),
                            "is_error": call.get("status") in {"error", "cancelled"},
                            "content": clean_tool_result_content(output, tool_output_limit),
                        }],
                    ))
            elif kind in {"info", "error", "warning"}:
                push(ParsedMessage(
                    index=0, line=position, uuid=uuid, parent_uuid=None, kind="system",
                    role="system", timestamp=stamp, model=None, is_sidechain=False,
                    is_meta=False, is_error=kind == "error",
                    blocks=[{"type": "text", "text": text}] if text else [], subtype=kind,
                ))

        summary = data.get("summary")
        if isinstance(summary, str) and summary.strip():
            info["title"] = summary.strip()
        info["title"] = info["title"] or info["session_id"]
        try:
            info["file_size"] = file_path.stat().st_size
        except OSError:
            info["file_size"] = 0
        info["message_count"] = len(messages)
        info["corrupt_lines"] = len(errors)
        return {"info": info, "messages": [m.to_dict() for m in messages], "errors": errors}
