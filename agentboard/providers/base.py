"""The contract every AI-tool adapter implements.

An adapter knows three things about one tool: where its history lives
(:meth:`ProviderAdapter.session_files`), how to summarise one session in a
single streaming pass (:meth:`~ProviderAdapter.scan`), and how to turn one
session into renderable messages (:meth:`~ProviderAdapter.parse`).
Everything else - indexing, caching, search, usage, the HTTP API, the UI -
is shared and works on the normalized model in :mod:`agentboard.model`.

What a tool cannot do is declared in :class:`Capabilities` rather than
discovered by failure, so the interface can say "not supported by Gemini
CLI" instead of showing a zero or an error.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional, Sequence, Set

from ..model import SessionMeta
from ..paths import is_within

__all__ = ["Capabilities", "Detection", "SearchEntry", "ProviderAdapter", "merge_pricing"]


@dataclass(frozen=True)
class Capabilities:
    """Features a provider's data supports.  The UI degrades on each one."""

    #: Token counts are recorded per response.
    usage: bool = True
    #: Tokens can be priced from the pricing table.
    cost: bool = True
    #: Prompt-cache reads and writes are recorded separately from input.
    cache_tokens: bool = False
    #: Tool / function calls are recorded.
    tool_calls: bool = True
    #: Reasoning text or token counts are recorded.
    thinking: bool = False
    #: History is updated while a session runs, so live watching is useful.
    live: bool = True
    #: Full-text search over message bodies.
    search: bool = True
    #: A session can be reopened in the tool from a terminal.
    resume: bool = False
    #: Sessions can be moved to the dashboard's trash.
    delete: bool = False
    #: The tool records its own cost figure alongside usage.
    reported_cost: bool = False
    #: Per-project instruction files (``CLAUDE.md``, ``AGENTS.md``...).
    instructions: bool = False
    #: A provider-specific configuration and asset browser.
    extras: bool = False

    def to_dict(self) -> Dict[str, bool]:
        """Serialise for the HTTP API."""
        return asdict(self)


@dataclass
class Detection:
    """Whether a tool is present on this machine, and why we think so."""

    installed: bool
    data_found: bool
    binary: str | None
    home: str
    root: str
    reason: str

    @property
    def detected(self) -> bool:
        """Present in any form: a binary on PATH or a data directory."""
        return self.installed or self.data_found

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the HTTP API."""
        return {
            "installed": self.installed,
            "data_found": self.data_found,
            "detected": self.detected,
            "binary": self.binary,
            "home": self.home,
            "root": self.root,
            "reason": self.reason,
        }


class SearchEntry(NamedTuple):
    """One searchable message: where it is and the text to match."""

    line: int
    role: str
    text: str
    uuid: str | None
    timestamp: str | None


def merge_pricing(
    adapter_defaults: Dict[str, Dict[str, float]],
    global_table: Dict[str, Dict[str, float]],
    overrides: Dict[str, Dict[str, float]] | None = None,
) -> Dict[str, Dict[str, float]]:
    """Combine the three pricing layers for one provider.

    Later layers win per model: the adapter's built-in rows, then the
    user's global table, then the provider-specific overrides.  The
    ``default`` row, used for unknown models, is the exception: a
    provider's own default beats the global one, so an unknown OpenAI model
    is not priced at an Anthropic rate.
    """
    table: Dict[str, Dict[str, float]] = {}
    table.update(adapter_defaults or {})
    table.update(global_table or {})
    if "default" in (adapter_defaults or {}):
        table["default"] = adapter_defaults["default"]
    table.update(overrides or {})
    return table


class ProviderAdapter(ABC):
    """Base class for one AI tool.  Subclasses set the class attributes and
    implement the three abstract methods."""

    #: Stable identifier used in URLs, config keys and the cache.
    id: str = ""
    #: Display name.
    name: str = ""
    #: One or two characters drawn in the provider badge.
    monogram: str = "?"
    #: Brand accent, used only for this provider's badges and chart series.
    color: str = "#8a8f98"
    #: One-line description for the settings page.
    description: str = ""
    #: Executables whose presence on PATH means the tool is installed.
    binary_names: Sequence[str] = ()
    #: File suffixes that hold sessions, used by the watcher.
    file_suffixes: Sequence[str] = (".jsonl",)
    #: Instruction files this tool reads from a project root.
    instruction_file_names: Sequence[str] = ()
    #: Command template that reopens a session; ``{session_id}`` and
    #: ``{cwd}`` are substituted.  Empty when the tool cannot resume by id.
    default_resume_command: str = ""
    #: Price rows (US dollars per million tokens) for this tool's models.
    default_pricing: Dict[str, Dict[str, float]] = {}
    capabilities: Capabilities = Capabilities()
    #: True for adapters built from a user's config file rather than code.
    custom: bool = False

    def __init__(self, settings: Dict[str, Any] | None = None) -> None:
        self.settings: Dict[str, Any] = dict(settings or {})

    # ------------------------------------------------------------ location

    @abstractmethod
    def default_home(self) -> Path:
        """The tool's data directory when no path is configured."""

    def data_root(self, home: Path) -> Path:
        """Where sessions live inside *home*.  Defaults to *home* itself."""
        return home

    @property
    def home(self) -> Path:
        """The tool's data directory, honouring a configured path."""
        configured = self.settings.get("path")
        if isinstance(configured, str) and configured.strip():
            return Path(configured.strip()).expanduser()
        return self.default_home()

    @property
    def root(self) -> Path:
        """The directory sessions are read from."""
        return self.data_root(self.home)

    @property
    def enabled(self) -> bool:
        """Whether the user has this provider switched on (default: yes)."""
        return self.settings.get("enabled", True) is not False

    # ----------------------------------------------------------- detection

    def detect(self) -> Detection:
        """Is the tool installed, and does it have any history here?"""
        binary = None
        for name in self.binary_names:
            found = shutil.which(name)
            if found:
                binary = found
                break
        root = self.root
        data_found = root.is_dir()
        if data_found and binary:
            reason = f"{Path(binary).name} on PATH and history in {root}"
        elif data_found:
            reason = f"history found in {root}"
        elif binary:
            reason = f"{Path(binary).name} on PATH, but no history in {root} yet"
        else:
            reason = f"not installed, and nothing at {root}"
        return Detection(
            installed=binary is not None,
            data_found=data_found,
            binary=binary,
            home=str(self.home),
            root=str(root),
            reason=reason,
        )

    # ------------------------------------------------------- the contract

    @abstractmethod
    def session_files(self) -> List[Path]:
        """Every file holding a session, sorted.  Symlinks are skipped."""

    @abstractmethod
    def scan(self, path: Path) -> SessionMeta:
        """Summarise one session file in a single streaming pass."""

    @abstractmethod
    def parse(
        self,
        path: Path,
        tool_output_limit: int = 20_000,
        include_attachments: bool = True,
    ) -> Dict[str, Any]:
        """The full conversation: ``{"info", "messages", "errors"}``."""

    # ------------------------------------------------------------ optional

    def search_entries(self, path: Path) -> Iterator[SearchEntry]:
        """Searchable text per message.

        The default parses the session and flattens each message, which
        works for any adapter.  Line-oriented formats override it to skip
        the full parse.
        """
        parsed = self.parse(path, tool_output_limit=1_000_000, include_attachments=False)
        for message in parsed.get("messages", []):
            if message.get("kind") not in {"user", "assistant", "tool_result"}:
                continue
            parts: List[str] = []
            for block in message.get("blocks", []):
                kind = block.get("type")
                if kind in {"text", "thinking"} and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif kind == "tool_use":
                    parts.append(str(block.get("name") or ""))
                    payload = block.get("input")
                    if isinstance(payload, dict):
                        parts.extend(v for v in payload.values() if isinstance(v, str))
                    elif isinstance(payload, str):
                        parts.append(payload)
                elif kind == "tool_result":
                    content = block.get("content")
                    if isinstance(content, str):
                        parts.append(content)
                    elif isinstance(content, list):
                        parts.extend(
                            item["text"] for item in content
                            if isinstance(item, dict) and isinstance(item.get("text"), str)
                        )
            text = "\n".join(p for p in parts if p)
            if text:
                yield SearchEntry(
                    line=int(message.get("line") or 0),
                    role=message.get("role") or message.get("kind") or "",
                    text=text,
                    uuid=message.get("uuid"),
                    timestamp=message.get("timestamp"),
                )

    def owns(self, path: Path | str) -> bool:
        """Whether *path* is a session file this adapter reads."""
        candidate = Path(path)
        if not str(candidate).endswith(tuple(self.file_suffixes)):
            return False
        return is_within(candidate, self.root)

    def watch_roots(self) -> List[Path]:
        """Directories to watch for live updates."""
        return [self.root] if self.root.is_dir() else []

    def guess_project_path(self, meta: SessionMeta, known_paths: Set[str]) -> str:
        """Recover a working directory the format does not record.

        Called for sessions whose ``project_path`` is empty, with every path
        other sessions recorded.  Most adapters have nothing to add.
        """
        return ""

    def resume_command(self, session_id: str, config: Dict[str, Any] | None = None) -> str | None:
        """The shell command that reopens *session_id*, or ``None``."""
        template = self.settings.get("resume_command") or self.default_resume_command
        if not template or not self.capabilities.resume:
            return None
        return str(template).replace("{session_id}", session_id)

    def pricing_table(self, config: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        """This provider's effective price table."""
        overrides = self.settings.get("pricing")
        return merge_pricing(
            self.default_pricing,
            config.get("pricing") or {},
            overrides if isinstance(overrides, dict) else None,
        )

    # ------------------------------------------- convenience entry points

    def list_sessions(self) -> List[SessionMeta]:
        """Scan every session this provider has, uncached."""
        return [self.scan(path) for path in self.session_files()]

    def get_session(self, session_id: str) -> Dict[str, Any] | None:
        """Parse the session with this id, or ``None`` if there is none."""
        for path in self.session_files():
            if path.stem == session_id or self.scan(path).session_id == session_id:
                return self.parse(path)
        return None

    def get_usage(
        self,
        sessions: Sequence[SessionMeta] | None = None,
        config: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Token, cost and activity rollup over this provider's sessions."""
        from ..config import load_config
        from ..usage import aggregate

        metas = [m for m in (sessions if sessions is not None else self.list_sessions())
                 if m.provider == self.id]
        return aggregate(metas, "day", self.pricing_table(config or load_config()))

    def watch(self, on_change: Callable[[Set[str]], None]) -> List[Any]:
        """Start live watchers over :meth:`watch_roots`.  Returns them so
        the caller can stop them; an empty list means no live updates."""
        from ..watcher import start_watcher

        watchers = []
        for root in self.watch_roots():
            watcher = start_watcher(root, on_change, suffixes=tuple(self.file_suffixes))
            if watcher is not None:
                watchers.append(watcher)
        return watchers

    def describe(self, detection: Optional[Detection] = None) -> Dict[str, Any]:
        """Everything the UI needs to present this provider."""
        found = detection or self.detect()
        return {
            "id": self.id,
            "name": self.name,
            "monogram": self.monogram,
            "color": self.color,
            "description": self.description,
            "custom": self.custom,
            "enabled": self.enabled,
            "capabilities": self.capabilities.to_dict(),
            "detection": found.to_dict(),
            "settings": {
                "path": self.settings.get("path") or "",
                "resume_command": self.settings.get("resume_command") or "",
                "pricing": self.settings.get("pricing") or {},
            },
            "default_home": str(self.default_home()),
            "default_resume_command": self.default_resume_command,
            "default_pricing": self.default_pricing,
            "instruction_files": list(self.instruction_file_names),
        }
