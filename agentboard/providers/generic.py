"""A provider described by a config file instead of code.

Most AI tools log a session as either one JSON document holding a list of
messages, or a JSONL file with one record per line.  This adapter reads
both, using dotted paths from a small spec to find each field, so adding a
tool means writing a file rather than a class.

Specs come from two places, merged:

* the ``custom_providers`` list in ``~/.agentboard/config.json``;
* one file per provider in ``~/.agentboard/providers/`` (``.json``, or
  ``.yaml``/``.yml`` when PyYAML is installed).

A minimal spec::

    {
      "id": "mytool",
      "name": "My Tool",
      "home": "~/.mytool",
      "glob": "sessions/**/*.jsonl",
      "format": "jsonl",
      "fields": {
        "role": "message.role",
        "text": "message.content",
        "timestamp": "timestamp",
        "model": "message.model",
        "input_tokens": "usage.input_tokens",
        "output_tokens": "usage.output_tokens"
      }
    }

See ``README.md`` ("How to add a new AI provider") for every key.  A bad
spec is reported in the settings page and skipped; it never stops the
application.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

from ..model import EMPTY_DAY_TOTALS, EMPTY_MODEL_TOTALS, ParsedMessage, SessionMeta, empty_usage, parse_timestamp
from ..paths import app_home
from .base import Capabilities, Detection, ProviderAdapter
from .common import clean_tool_result_content, dig, iter_entries, truncate

__all__ = [
    "GenericAdapter",
    "SpecError",
    "validate_spec",
    "load_custom_adapters",
    "custom_providers_dir",
    "DEFAULT_ROLES",
]

#: Role values recognised out of the box.  A spec's ``roles`` adds to these.
DEFAULT_ROLES: Dict[str, List[str]] = {
    "user": ["user", "human"],
    "assistant": ["assistant", "ai", "model", "bot", "gemini"],
    "tool": ["tool", "function", "tool_result"],
    "system": ["system", "info", "error", "warning"],
}

#: Field names a spec may map, and what each one means.
FIELD_NAMES = (
    "role", "text", "timestamp", "model", "message_id",
    "tool_name", "tool_input", "tool_output",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "cost",
)

#: Session-level values, read from the document (json) or the first record
#: that has them (jsonl).
SESSION_NAMES = ("id", "cwd", "title", "branch")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class SpecError(ValueError):
    """A provider spec that cannot be used, with a message for the user."""


def validate_spec(spec: Any, taken: set[str] = frozenset()) -> Dict[str, Any]:
    """Check a provider spec and return it normalised.

    :raises SpecError: naming the first problem found.
    """
    if not isinstance(spec, dict):
        raise SpecError("a provider spec must be a JSON object")
    provider_id = spec.get("id")
    if not isinstance(provider_id, str) or not _ID_RE.match(provider_id):
        raise SpecError("`id` must be lowercase letters, digits, '-' or '_' (max 40)")
    if provider_id in taken:
        raise SpecError(f"`id` {provider_id!r} is already used by another provider")
    for key in ("home", "glob"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise SpecError(f"`{key}` is required")
    if ".." in Path(spec["glob"]).parts or Path(spec["glob"]).is_absolute():
        raise SpecError("`glob` must be relative to `home` and must not contain '..'")
    fmt = spec.get("format", "jsonl")
    if fmt not in {"jsonl", "json"}:
        raise SpecError("`format` must be 'jsonl' or 'json'")
    fields = spec.get("fields") or {}
    if not isinstance(fields, dict):
        raise SpecError("`fields` must be an object of dotted paths")
    unknown = sorted(set(fields) - set(FIELD_NAMES))
    if unknown:
        raise SpecError(f"unknown field(s) {', '.join(unknown)}; known: {', '.join(FIELD_NAMES)}")
    if "text" not in fields and "role" not in fields:
        raise SpecError("`fields` must map at least `role` and `text`")
    session = spec.get("session") or {}
    if not isinstance(session, dict) or set(session) - set(SESSION_NAMES):
        raise SpecError(f"`session` may only map {', '.join(SESSION_NAMES)}")
    roles = spec.get("roles") or {}
    if not isinstance(roles, dict) or any(not isinstance(v, list) for v in roles.values()):
        raise SpecError("`roles` must map a role to a list of values")
    pricing = spec.get("pricing") or {}
    if not isinstance(pricing, dict):
        raise SpecError("`pricing` must map model ids to price rows")
    color = spec.get("color", "#8a8f98")
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise SpecError("`color` must be a #rrggbb hex colour")
    return {
        **spec,
        "format": fmt,
        "fields": fields,
        "session": session,
        "roles": roles,
        "pricing": pricing,
        "color": color,
    }


def _text(value: Any) -> str:
    """Readable text from a string, a list of parts, or a part object."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if isinstance(value.get(key), (str, list)):
                return _text(value[key])
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "\n".join(t for t in (_text(item) for item in value) if t)
    return str(value)


def _number(value: Any) -> int:
    """An integer token count, or zero."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


class GenericAdapter(ProviderAdapter):
    """An adapter whose every detail comes from a spec."""

    custom = True

    def __init__(self, spec: Dict[str, Any], settings: Dict[str, Any] | None = None) -> None:
        super().__init__(settings)
        self.spec = spec
        self.id = spec["id"]
        self.name = spec.get("name") or spec["id"]
        self.monogram = (spec.get("monogram") or self.name[:2]).strip()[:2] or "?"
        self.color = spec["color"]
        self.description = spec.get("description") or f"Custom provider reading {spec['home']}/{spec['glob']}"
        binary = spec.get("binary") or []
        self.binary_names = [binary] if isinstance(binary, str) else list(binary)
        suffix = Path(spec["glob"]).suffix
        self.file_suffixes = (suffix,) if suffix and "*" not in suffix else (".json", ".jsonl")
        self.instruction_file_names = list(spec.get("instruction_files") or [])
        self.default_resume_command = spec.get("resume_command") or ""
        self.default_pricing = spec.get("pricing") or {}
        fields = spec["fields"]
        self.capabilities = Capabilities(
            usage=any(k in fields for k in ("input_tokens", "output_tokens")),
            cost=any(k in fields for k in ("input_tokens", "output_tokens", "cost")),
            cache_tokens=any(k in fields for k in ("cache_read_tokens", "cache_write_tokens")),
            tool_calls="tool_name" in fields,
            thinking="reasoning_tokens" in fields,
            resume=bool(self.default_resume_command),
            reported_cost="cost" in fields,
            instructions=False,
        )
        roles = {k: list(v) for k, v in DEFAULT_ROLES.items()}
        for role, values in spec["roles"].items():
            roles.setdefault(role, [])
            roles[role] = [str(v) for v in values] + roles[role]
        self._roles = {value.lower(): role for role, values in roles.items() for value in values}

    def default_home(self) -> Path:
        return Path(self.spec["home"]).expanduser()

    def detect(self) -> Detection:
        found = super().detect()
        found.data_found = bool(self.session_files())
        if not found.data_found and Path(found.root).is_dir():
            found.reason = f"{found.root} exists but nothing matches {self.spec['glob']}"
        return found

    def session_files(self) -> List[Path]:
        home = self.home
        if not home.is_dir():
            return []
        found: List[Path] = []
        try:
            for path in home.glob(self.spec["glob"]):
                if path.is_file() and not path.is_symlink():
                    found.append(path)
        except (OSError, ValueError):
            return []
        return sorted(found)

    def owns(self, path: Path | str) -> bool:
        """Matched textually, so a watcher event never walks the tree."""
        candidate = Path(path)
        try:
            relative = candidate.relative_to(self.home).as_posix()
        except ValueError:
            return False
        pattern = self.spec["glob"]
        # fnmatch's "*" already crosses "/", but "a/**/b" must also match
        # "a/b", as glob's "**" may stand for no directory at all.
        return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(
            relative, pattern.replace("**/", "")
        )

    # ------------------------------------------------------------- records

    def _records(self, path: Path) -> Tuple[Dict[str, Any], Iterator[Tuple[int, Any, str | None]]]:
        """``(document, records)`` for either format.

        For JSONL the document is empty and session values are taken from
        the records themselves.  Each record comes with its 1-based
        position and an error message when it could not be read.
        """
        if self.spec["format"] == "jsonl":
            return {}, iter_entries(path)
        try:
            document = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            return {}, iter([(1, {}, f"invalid JSON: {exc}")])
        messages_path = self.spec.get("messages_path")
        items = dig(document, messages_path) if messages_path else document
        if not isinstance(items, list):
            return document if isinstance(document, dict) else {}, iter(
                [(1, {}, f"no message list at {messages_path or 'the top level'!r}")]
            )
        doc = document if isinstance(document, dict) else {}
        return doc, iter(
            (i, item, None if isinstance(item, dict) else "message is not an object")
            for i, item in enumerate(items, start=1)
        )

    def _field(self, record: Any, name: str) -> Any:
        return dig(record, self.spec["fields"].get(name))

    def _role(self, record: Any) -> str:
        raw = self._field(record, "role")
        if raw is None and self._field(record, "tool_name") is not None:
            return "assistant"
        return self._roles.get(str(raw).lower(), "other") if raw is not None else "other"

    def _usage(self, record: Any) -> Dict[str, int]:
        usage = empty_usage()
        cached = _number(self._field(record, "cache_read_tokens"))
        raw_input = _number(self._field(record, "input_tokens"))
        if self.spec.get("input_includes_cached"):
            raw_input = max(0, raw_input - cached)
        usage["input"] = raw_input
        usage["cache_read"] = cached
        usage["cache_write"] = _number(self._field(record, "cache_write_tokens"))
        usage["cache_write_5m"] = usage["cache_write"]
        usage["output"] = _number(self._field(record, "output_tokens"))
        usage["thinking"] = _number(self._field(record, "reasoning_tokens"))
        return usage

    def _session_value(self, document: Dict[str, Any], record: Any, name: str) -> Any:
        path = self.spec["session"].get(name)
        if not path:
            return None
        value = dig(document, path) if document else None
        return value if value is not None else dig(record, path)

    # ---------------------------------------------------------------- scan

    def scan(self, path: Path) -> SessionMeta:
        file_path = Path(path)
        meta = SessionMeta(
            session_id=file_path.stem, path=str(file_path),
            project_dir=file_path.parent.name, provider=self.id,
        )
        try:
            stat = file_path.stat()
            meta.file_size, meta.mtime = stat.st_size, stat.st_mtime
        except OSError:
            return meta

        document, records = self._records(file_path)
        tokens = empty_usage()
        seen_ids: set[str] = set()
        first_prompt = ""
        title = ""
        reported = 0.0
        has_reported = False

        for _, record, error in records:
            meta.line_count += 1
            if error is not None:
                meta.corrupt_lines += 1
                continue
            for name in SESSION_NAMES:
                value = self._session_value(document, record, name)
                if not isinstance(value, str) or not value:
                    continue
                if name == "id" and meta.session_id == file_path.stem:
                    meta.session_id = value
                elif name == "cwd" and not meta.project_path:
                    meta.project_path = value
                elif name == "title" and not title:
                    title = value
                elif name == "branch" and not meta.git_branch:
                    meta.git_branch = value

            stamp = self._field(record, "timestamp")
            when = parse_timestamp(stamp)
            iso = when.isoformat().replace("+00:00", "Z") if when else None
            if iso:
                if meta.first_timestamp is None or iso < meta.first_timestamp:
                    meta.first_timestamp = iso
                if meta.last_timestamp is None or iso > meta.last_timestamp:
                    meta.last_timestamp = iso
            day = iso[:10] if iso else None

            role = self._role(record)
            tool = self._field(record, "tool_name")
            if isinstance(tool, str) and tool:
                meta.tools[tool] = meta.tools.get(tool, 0) + 1
                meta.tool_calls += 1
                if day:
                    meta.daily.setdefault(day, dict(EMPTY_DAY_TOTALS))["tool_calls"] += 1

            cost = self._field(record, "cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                reported += float(cost)
                has_reported = True

            if role == "user":
                text = _text(self._field(record, "text"))
                if not text.strip():
                    continue
                meta.user_messages += 1
                meta.message_count += 1
                first_prompt = first_prompt or text
            elif role == "assistant":
                message_id = self._field(record, "message_id")
                key = str(message_id) if message_id is not None else None
                if key is not None and key in seen_ids:
                    continue  # another line of a response already counted
                if key is not None:
                    seen_ids.add(key)
                meta.assistant_messages += 1
                meta.message_count += 1
                model = self._field(record, "model")
                model = model if isinstance(model, str) and model else "unknown"
                usage = self._usage(record)
                for k, v in usage.items():
                    tokens[k] += v
                row = meta.models.setdefault(model, dict(EMPTY_MODEL_TOTALS))
                row["messages"] += 1
                bucket = meta.daily.setdefault(day, dict(EMPTY_DAY_TOTALS)) if day else None
                if bucket is not None:
                    bucket["messages"] += 1
                for k in EMPTY_MODEL_TOTALS:
                    if k != "messages":
                        row[k] += usage.get(k, 0)
                        if bucket is not None:
                            bucket[k] += usage.get(k, 0)
            else:
                continue
            if when is not None:
                slot = f"{when.weekday()}-{when.hour}"
                meta.heatmap[slot] = meta.heatmap.get(slot, 0) + 1

        if meta.last_timestamp is None and meta.mtime:
            # Formats without per-message times still sort and chart by the
            # last time the file was written.
            when = parse_timestamp(meta.mtime)
            meta.first_timestamp = meta.last_timestamp = (
                when.isoformat().replace("+00:00", "Z") if when else None
            )
        meta.tokens = tokens
        if has_reported:
            meta.reported_cost_usd = reported
        if title:
            meta.title, meta.title_source = truncate(title, 160), "summary"
        elif first_prompt.strip():
            meta.title, meta.title_source = truncate(first_prompt, 120), "first-message"
        else:
            meta.title, meta.title_source = meta.session_id, "session-id"
        meta.preview = truncate(first_prompt, 240)
        if meta.project_path:
            meta.project_dir = meta.project_path
        return meta

    # --------------------------------------------------------------- parse

    def parse(
        self,
        path: Path,
        tool_output_limit: int = 20_000,
        include_attachments: bool = True,
    ) -> Dict[str, Any]:
        file_path = Path(path)
        document, records = self._records(file_path)
        info: Dict[str, Any] = {
            "session_id": file_path.stem, "path": str(file_path), "title": "",
            "cwd": "", "git_branch": "", "versions": [], "cost_state": None,
        }
        messages: List[ParsedMessage] = []
        errors: List[Dict[str, Any]] = []

        for position, record, error in records:
            if error is not None:
                errors.append({"line": position, "error": error})
                continue
            for name, key in (("id", "session_id"), ("cwd", "cwd"), ("title", "session_title"),
                              ("branch", "git_branch")):
                value = self._session_value(document, record, name)
                if not isinstance(value, str) or not value:
                    continue
                if key == "session_id":
                    if info["session_id"] == file_path.stem:
                        info["session_id"] = value
                elif not info.get(key):
                    info[key] = value
            role = self._role(record)
            stamp = self._field(record, "timestamp")
            when = parse_timestamp(stamp)
            iso = when.isoformat().replace("+00:00", "Z") if when else None
            text = _text(self._field(record, "text"))
            tool = self._field(record, "tool_name")
            message_id = self._field(record, "message_id")
            uuid = str(message_id) if message_id is not None else None

            if role == "tool":
                output = _text(self._field(record, "tool_output")) or text
                kind, blocks = "tool_result", [{
                    "type": "tool_result", "tool_use_id": None,
                    "name": tool if isinstance(tool, str) else None, "is_error": False,
                    "content": clean_tool_result_content(output, tool_output_limit),
                }]
                msg_role = "user"
            elif role in {"user", "assistant"}:
                blocks = [{"type": "text", "text": text}] if text else []
                if isinstance(tool, str) and tool:
                    blocks.append({"type": "tool_use", "id": None, "name": tool,
                                   "input": self._field(record, "tool_input"), "caller": "direct"})
                kind, msg_role = role, role
                previous = next((m for m in reversed(messages) if m.kind == "assistant"), None)
                if role == "assistant" and uuid and previous is not None and previous.uuid == uuid:
                    # Another line of the same response, as message_id says.
                    previous.blocks.extend(blocks)
                    continue
                output = self._field(record, "tool_output")
                if output is not None and isinstance(tool, str) and tool:
                    messages.append(self._message(len(messages), position, uuid, kind, msg_role,
                                                  iso, record, blocks, role))
                    kind, msg_role = "tool_result", "user"
                    blocks = [{"type": "tool_result", "tool_use_id": None, "name": tool,
                               "is_error": False,
                               "content": clean_tool_result_content(_text(output), tool_output_limit)}]
                    uuid = None
            elif role == "system":
                kind, msg_role = "system", "system"
                blocks = [{"type": "text", "text": text}] if text else []
            else:
                continue
            if not blocks:
                continue
            messages.append(self._message(len(messages), position, uuid, kind, msg_role,
                                          iso, record, blocks, role))
            if kind == "user" and not info["title"] and text.strip():
                info["title"] = truncate(text, 120)

        info["title"] = info.pop("session_title", "") or info["title"] or info["session_id"]
        try:
            info["file_size"] = file_path.stat().st_size
        except OSError:
            info["file_size"] = 0
        info["message_count"] = len(messages)
        info["corrupt_lines"] = len(errors)
        return {"info": info, "messages": [m.to_dict() for m in messages], "errors": errors}

    def describe(self, detection: Detection | None = None) -> Dict[str, Any]:
        described = super().describe(detection)
        described["source"] = getattr(self, "source", "")
        described["spec"] = self.spec
        return described

    def _message(self, index: int, line: int, uuid: str | None, kind: str, role: str,
                 timestamp: str | None, record: Any, blocks: List[Dict[str, Any]],
                 source_role: str) -> ParsedMessage:
        model = self._field(record, "model") if source_role == "assistant" else None
        usage = self._usage(record) if kind == "assistant" and self.capabilities.usage else None
        return ParsedMessage(
            index=index, line=line, uuid=uuid, parent_uuid=None, kind=kind, role=role,
            timestamp=timestamp, model=model if isinstance(model, str) else None,
            is_sidechain=False, is_meta=False, is_error=False, blocks=blocks, usage=usage,
        )


# -------------------------------------------------------------------- loading

def custom_providers_dir() -> Path:
    """Where one-file-per-provider specs are read from."""
    return app_home() / "providers"


def _read_spec_file(path: Path) -> Any:
    """Parse one spec file, JSON always and YAML when PyYAML is present."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SpecError("YAML specs need PyYAML: pip install pyyaml (or use .json)") from exc
    return yaml.safe_load(text)


def load_custom_adapters(
    config: Dict[str, Any],
    settings: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> List[GenericAdapter]:
    """Build an adapter for every valid spec; report the rest in *errors*."""
    from .registry import BUILTIN

    taken = set(BUILTIN)
    sources: List[Tuple[str, Any]] = []
    for position, spec in enumerate(config.get("custom_providers") or []):
        sources.append((f"custom_providers[{position}]", spec))
    folder = custom_providers_dir()
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.suffix not in {".json", ".yaml", ".yml"} or not path.is_file():
                continue
            try:
                sources.append((str(path), _read_spec_file(path)))
            except (OSError, ValueError, SpecError) as exc:
                errors.append({"source": str(path), "error": str(exc)})

    adapters: List[GenericAdapter] = []
    for source, raw in sources:
        try:
            spec = validate_spec(raw, taken)
        except SpecError as exc:
            errors.append({"source": source, "error": str(exc)})
            continue
        taken.add(spec["id"])
        own = settings.get(spec["id"]) if isinstance(settings.get(spec["id"]), dict) else {}
        adapter = GenericAdapter(spec, own)
        adapter.source = source  # type: ignore[attr-defined]
        adapters.append(adapter)
    return adapters

