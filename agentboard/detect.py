"""Detect installed AI tools and configure their adapters.

Run by both installers after installing (``agentboard --detect-providers``)
and available from the Settings page as "Detect again".  It:

* reports, for every built-in and custom provider, whether the tool is
  installed and where its history is;
* records a data folder that is only known through an environment variable
  (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``, ``GEMINI_CLI_HOME``), because an
  app started from the desktop menu does not inherit the shell's variables;
* adds a bundled example spec (``examples/providers``) as a custom provider
  when that tool's history is found and no provider with its id exists.

Nothing is ever disabled: a provider that is not detected stays enabled, so
it appears by itself once the tool is installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .config import load_config, save_config
from .providers.generic import GenericAdapter, SpecError, validate_spec
from .providers.registry import BUILTIN, Registry

__all__ = ["EXAMPLES_DIR", "ENV_HOMES", "detect_and_configure", "format_report"]

#: Example specs shipped with the application.
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "providers"

#: Environment variables that move a built-in tool's data directory.
ENV_HOMES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "gemini": "GEMINI_CLI_HOME"}


def _example_specs(folder: Path) -> List[Dict[str, Any]]:
    """Every readable JSON example spec."""
    if not folder.is_dir():
        return []
    specs = []
    for path in sorted(folder.glob("*.json")):
        try:
            specs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return specs


def detect_and_configure(write: bool = True, examples: Path | None = None) -> Dict[str, Any]:
    """Detect every provider, update the config, and return a report."""
    config = load_config(force=True)
    providers = {k: dict(v) for k, v in (config.get("providers") or {}).items() if isinstance(v, dict)}
    custom = [s for s in (config.get("custom_providers") or []) if isinstance(s, dict)]
    changes: List[str] = []

    registry = Registry(config)
    taken = {adapter.id for adapter in registry.adapters} | set(BUILTIN)
    for spec in _example_specs(examples or EXAMPLES_DIR):
        try:
            clean = validate_spec(spec, taken)
        except SpecError:
            continue
        if GenericAdapter(clean).session_files():
            custom.append(spec)
            taken.add(clean["id"])
            changes.append(f"added the {clean.get('name', clean['id'])} provider from its example spec")

    for provider_id, variable in ENV_HOMES.items():
        value = os.environ.get(variable)
        entry = providers.setdefault(provider_id, {})
        if value and not entry.get("path"):
            home = str(Path(value).expanduser())
            if provider_id == "gemini":
                home = str(Path(home) / ".gemini")   # GEMINI_CLI_HOME replaces $HOME
            entry["path"] = home
            changes.append(f"remembered {variable}={value} for {provider_id}")

    updated = {**config, "providers": providers, "custom_providers": custom}
    if write and changes:
        save_config(updated)

    rows = []
    for adapter in Registry(updated).adapters:
        found = adapter.detect()
        sessions = len(adapter.session_files()) if found.data_found else 0
        rows.append({
            "id": adapter.id,
            "name": adapter.name,
            "enabled": adapter.enabled,
            "installed": found.installed,
            "data_found": found.data_found,
            "sessions": sessions,
            "root": found.root,
            "reason": found.reason,
        })
    return {"providers": rows, "changes": changes, "written": bool(write and changes)}


def format_report(report: Dict[str, Any]) -> str:
    """The report as aligned terminal text."""
    lines = []
    width = max((len(row["name"]) for row in report["providers"]), default=10)
    for row in report["providers"]:
        if row["data_found"]:
            state = f"{row['sessions']} session file{'s' if row['sessions'] != 1 else ''}"
        elif row["installed"]:
            state = "installed, no history yet"
        else:
            state = "not found"
        mark = "+" if row["data_found"] else ("~" if row["installed"] else "-")
        off = "" if row["enabled"] else "  (disabled)"
        lines.append(f"  {mark} {row['name']:<{width}}  {state:<26} {row['root']}{off}")
    for change in report["changes"]:
        lines.append(f"  * {change}")
    return "\n".join(lines)
