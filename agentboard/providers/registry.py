"""Which providers exist, which are switched on, and their settings.

Built-in adapters register themselves in :data:`BUILTIN`.  User-defined ones
come from the generic adapter, configured either in ``custom_providers`` in
the dashboard config or as files in ``~/.agentboard/providers/``.

Per-provider settings live under ``providers.<id>`` in the config::

    "providers": {
      "codex": {"enabled": true, "path": "", "resume_command": "", "pricing": {}}
    }

An empty ``path`` means the tool's default location.  The registry is
rebuilt whenever the configuration changes.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Type

from .base import ProviderAdapter

__all__ = ["BUILTIN", "register", "Registry", "get_registry", "reload_registry"]

#: Adapter classes shipped with the application, in display order.
BUILTIN: Dict[str, Type[ProviderAdapter]] = {}


def register(cls: Type[ProviderAdapter]) -> Type[ProviderAdapter]:
    """Add a built-in adapter class.  Usable as a decorator."""
    if not cls.id:
        raise ValueError(f"{cls.__name__} has no id")
    BUILTIN[cls.id] = cls
    return cls


def _load_builtins() -> None:
    """Import the built-in adapters so they register themselves."""
    from .claude import ClaudeAdapter
    from .codex import CodexAdapter

    for cls in (ClaudeAdapter, CodexAdapter):
        BUILTIN.setdefault(cls.id, cls)


class Registry:
    """Every known adapter, built from one configuration snapshot."""

    def __init__(self, config: Dict[str, Any]) -> None:
        _load_builtins()
        settings = config.get("providers") if isinstance(config.get("providers"), dict) else {}
        self.adapters: List[ProviderAdapter] = []
        self.errors: List[Dict[str, str]] = []
        for provider_id, cls in BUILTIN.items():
            own = settings.get(provider_id) if isinstance(settings.get(provider_id), dict) else {}
            self.adapters.append(cls(own))
        for adapter in _custom_adapters(config, settings, self.errors):
            if any(a.id == adapter.id for a in self.adapters):
                self.errors.append({
                    "source": adapter.id,
                    "error": f"a provider with id {adapter.id!r} already exists",
                })
                continue
            self.adapters.append(adapter)

    def enabled(self) -> List[ProviderAdapter]:
        """Adapters the user has not switched off."""
        return [adapter for adapter in self.adapters if adapter.enabled]

    def get(self, provider_id: str) -> Optional[ProviderAdapter]:
        """The adapter with this id, enabled or not."""
        for adapter in self.adapters:
            if adapter.id == provider_id:
                return adapter
        return None


def _custom_adapters(
    config: Dict[str, Any],
    settings: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> List[ProviderAdapter]:
    """Generic adapters described by the user.  Filled in by the generic
    adapter module once it exists; a bad definition is reported, never
    fatal."""
    try:
        from .generic import load_custom_adapters
    except ImportError:
        return []
    return load_custom_adapters(config, settings, errors)


_LOCK = threading.Lock()
_REGISTRY: Optional[Registry] = None


def get_registry() -> Registry:
    """The process-wide registry, built from the current config on first use."""
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is None:
            from ..config import load_config

            _REGISTRY = Registry(load_config())
        return _REGISTRY


def reload_registry(config: Dict[str, Any] | None = None) -> Registry:
    """Rebuild the registry, after a settings change."""
    global _REGISTRY
    from ..config import load_config

    fresh = Registry(config if config is not None else load_config())
    with _LOCK:
        _REGISTRY = fresh
    return fresh
