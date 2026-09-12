"""User configuration, persisted to ``~/.claude-dashboard/config.json``.

The file is created from :data:`DEFAULT_CONFIG` on first run and merged
key-by-key on every load, so a config written by an older version keeps
working when new settings appear.

Nothing here is ever read from the network.  The pricing table in particular
is a *local estimate* maintained by the user: the dashboard has no access to
real billing data and the UI says so wherever a cost is displayed.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict

from .paths import config_path

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_PRICING",
    "load_config",
    "save_config",
    "update_config",
    "normalise_model",
    "price_for_model",
    "cost_of",
]

# Prices are US dollars per *million* tokens.  Cache writes are charged by
# TTL: the 5-minute tier costs 1.25x the input rate, the 1-hour tier 2x.
#
# These are LOCAL ESTIMATES the user is expected to edit - see the "Pricing"
# section of README.md.  The starting values were derived by least-squares
# fitting the four counters against the `costUSD` figures Claude Code itself
# recorded in 32 `cost-state` entries on the reference install; the fit for
# claude-opus-5 reproduces those figures exactly.  They are still not billing
# data and the UI never presents them as such.
DEFAULT_PRICING: Dict[str, Dict[str, float]] = {
    "claude-opus-5":    {"input": 5.00, "output": 25.00, "cache_write_5m": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50},
    "claude-opus-4-8":  {"input": 5.00, "output": 25.00, "cache_write_5m": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50},
    "claude-sonnet-5":  {"input": 3.00, "output": 15.00, "cache_write_5m": 3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
    "claude-fable-5-1": {"input": 3.00, "output": 15.00, "cache_write_5m": 3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.00, "output":  5.00, "cache_write_5m": 1.25, "cache_write_1h":  2.00, "cache_read": 0.10},
    "default":          {"input": 3.00, "output": 15.00, "cache_write_5m": 3.75, "cache_write_1h":  6.00, "cache_read": 0.30},
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "window": {
        "width": 1400,
        "height": 900,
        "x": None,
        "y": None,
        "maximized": False,
        "min_width": 1024,
        "min_height": 640,
    },
    "theme": "system",               # system | light | dark
    "minimize_to_tray": False,
    "auto_refresh": True,            # watchdog-driven live updates
    "trash_retention_days": 30,
    "confirm_deletions": True,
    "pricing": DEFAULT_PRICING,
    "terminal": {
        # ``{cwd}``, ``{session_id}`` and ``{command}`` are substituted.
        # An empty list means "use the built-in per-platform detection".
        "windows": [],
        "darwin": [],
        "linux": [],
        "resume_command": "claude --resume {session_id}",
    },
    "editor_command": "code",        # used for "open in VS Code"
    "search": {
        "max_results": 400,
        "context_chars": 160,
    },
    "favorites": [],                 # session ids
    "tags": {},                      # session id -> [tag, ...]
    "notes": {},                     # session id -> markdown string
    "last_view": "sessions",
}

_LOCK = threading.RLock()
_CACHED: Dict[str, Any] | None = None


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Merge *override* into a copy of *base*, recursing into plain dicts."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(force: bool = False) -> Dict[str, Any]:
    """Return the configuration, reading from disk at most once per process.

    A corrupt or unreadable file is ignored in favour of the defaults rather
    than crashing the application; the next :func:`save_config` rewrites it.
    """
    global _CACHED
    with _LOCK:
        if _CACHED is not None and not force:
            return _CACHED
        path = config_path()
        stored: Dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    stored = raw
            except (OSError, ValueError):
                stored = {}
        _CACHED = _deep_merge(DEFAULT_CONFIG, stored)
        if not path.exists():
            _write(_CACHED, path)
        return _CACHED


def _write(data: Dict[str, Any], path: Path) -> None:
    """Atomically write *data* as pretty JSON to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a complete configuration object and return it."""
    global _CACHED
    with _LOCK:
        merged = _deep_merge(DEFAULT_CONFIG, data)
        _write(merged, config_path())
        _CACHED = merged
        return merged


def update_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge *patch* into the stored configuration and persist it."""
    with _LOCK:
        return save_config(_deep_merge(load_config(), patch))


_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")


def normalise_model(model: str | None) -> str:
    """Strip the context-window suffix from a model id.

    ``cost-state`` records ``claude-opus-5[1m]`` while ``message.usage``
    records ``claude-opus-5``; the pricing table is keyed on the latter.
    Dated ids such as ``claude-haiku-4-5-20251001`` fall back to their
    undated prefix when that has a price entry.
    """
    if not model:
        return "unknown"
    return _SUFFIX_RE.sub("", model.strip())


def price_for_model(model: str | None, pricing: Dict[str, Any] | None = None) -> Dict[str, float]:
    """Look up the price row for *model*, falling back progressively.

    Exact id, then the id with a trailing ``-YYYYMMDD`` date removed, then any
    table key that prefixes the id, then the ``default`` row.
    """
    table = pricing if pricing is not None else load_config()["pricing"]
    raw = (model or "").strip()
    if raw and raw in table:
        # An explicit row for a suffixed id such as ``claude-opus-5[1m]`` wins.
        return table[raw]
    name = normalise_model(model)
    if name in table:
        return table[name]
    undated = re.sub(r"-\d{8}$", "", name)
    if undated in table:
        return table[undated]
    best = ""
    for key in table:
        if key != "default" and name.startswith(key) and len(key) > len(best):
            best = key
    if best:
        return table[best]
    return table.get("default", DEFAULT_PRICING["default"])


def _rate(row: Dict[str, Any], key: str, fallback_key: str = "", factor: float = 1.0) -> float:
    """Read a rate from a pricing row, tolerating older table layouts.

    Tables written before the cache TTL split only had ``cache_write``; that
    value is reused, scaled by *factor*, when the TTL-specific key is absent.
    """
    value = row.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    if fallback_key:
        legacy = row.get(fallback_key)
        if isinstance(legacy, (int, float)):
            return float(legacy) * factor
    return 0.0


def cost_of(
    model: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    pricing: Dict[str, Any] | None = None,
    cache_write_5m_tokens: int | None = None,
    cache_write_1h_tokens: int | None = None,
) -> float:
    """Estimated US-dollar cost of one usage record.

    When the 5-minute / 1-hour split is known it is priced per tier, which
    matters: on the reference install 100% of cache writes used the 1-hour
    tier, and pricing them at the 5-minute rate understates cost by a third.
    When only the combined ``cache_write_tokens`` is known it is charged at
    the 5-minute rate.

    Synthetic assistant entries (``model == "<synthetic>"``) are local error
    placeholders and never cost anything - see ``SCHEMA.md`` 3.3.
    """
    if model == "<synthetic>":
        return 0.0
    row = price_for_model(model, pricing)
    rate_5m = _rate(row, "cache_write_5m", "cache_write", 1.0)
    rate_1h = _rate(row, "cache_write_1h", "cache_write", 1.6)

    if cache_write_5m_tokens is None and cache_write_1h_tokens is None:
        write_cost = cache_write_tokens * rate_5m
    else:
        write_cost = (cache_write_5m_tokens or 0) * rate_5m + (cache_write_1h_tokens or 0) * rate_1h

    return (
        input_tokens * _rate(row, "input")
        + output_tokens * _rate(row, "output")
        + write_cost
        + cache_read_tokens * _rate(row, "cache_read")
    ) / 1_000_000.0
