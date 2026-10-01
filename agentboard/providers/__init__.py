"""Provider adapters: one per AI coding tool.

See :mod:`agentboard.providers.base` for the interface and ``README.md``
("How to add a new AI provider") for a worked example.
"""

from .base import Capabilities, Detection, ProviderAdapter, SearchEntry, merge_pricing
from .registry import BUILTIN, Registry, get_registry, register, reload_registry

__all__ = [
    "Capabilities",
    "Detection",
    "ProviderAdapter",
    "SearchEntry",
    "merge_pricing",
    "BUILTIN",
    "Registry",
    "get_registry",
    "register",
    "reload_registry",
]
