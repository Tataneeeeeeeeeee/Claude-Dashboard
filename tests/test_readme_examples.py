"""The README's "How to add a new AI provider" examples must keep working."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentboard.indexer import SessionIndex
from agentboard.providers import registry
from agentboard.providers.generic import GenericAdapter, validate_spec

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
SECTION = README[README.index("## How to add a new AI provider"):README.index("## How it works")]


def _block(language: str) -> str:
    match = re.search(rf"```{language}\n(.*?)\n```", SECTION, re.S)
    assert match, f"no {language} block in the section"
    return match.group(1)


def test_the_spec_example_is_valid():
    spec = validate_spec(json.loads(_block("json")))
    adapter = GenericAdapter(spec)
    assert adapter.capabilities.usage and adapter.capabilities.resume


@pytest.fixture
def plainlog(tmp_path, monkeypatch):
    """Run the README adapter, then undo its registration."""
    saved = dict(registry.BUILTIN)
    namespace: dict = {}
    exec(compile(_block("python"), "README.md", "exec"), namespace)
    cls = namespace["PlainLogAdapter"]
    yield cls
    registry.BUILTIN.clear()
    registry.BUILTIN.update(saved)


def test_the_adapter_example_works_end_to_end(plainlog, tmp_path):
    home = tmp_path / ".plainlog"
    home.mkdir()
    records = [
        {"role": "user", "text": "hello there", "time": "2026-09-30T10:00:00Z"},
        {"role": "assistant", "text": "hi", "time": "2026-09-30T10:00:02Z",
         "model": "pl-1", "in": 1_000_000, "out": 500_000},
    ]
    (home / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    adapter = plainlog({"path": str(home)})
    assert "plainlog" in registry.BUILTIN, "@register makes it a built-in"
    meta = adapter.scan(home / "s1.jsonl")
    assert meta.title == "hello there" and meta.message_count == 2
    assert adapter.parse(home / "s1.jsonl")["messages"][1]["role"] == "assistant"

    index = SessionIndex(adapters=[adapter])
    index.build(force=True)
    assert index.search("hello")["hits"][0]["provider"] == "plainlog"
    usage = adapter.get_usage(index.sessions, {"pricing": {}})
    assert usage["totals"]["estimated_cost"] == pytest.approx(2.0)
