"""The config-driven adapter: specs, both formats, and loading."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agentboard.indexer import SessionIndex
from agentboard.providers import Registry
from agentboard.providers.generic import GenericAdapter, SpecError, validate_spec
from agentboard.usage import session_cost

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "generic"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "providers"

JSONL_SPEC = {
    "id": "mytool",
    "name": "My Tool",
    "color": "#e11d48",
    "home": str(FIXTURES / "jsonl-tool"),
    "glob": "logs/**/*.jsonl",
    "format": "jsonl",
    "session": {"id": "session_id", "cwd": "cwd"},
    "fields": {
        "role": "role", "text": "content", "timestamp": "ts", "model": "model",
        "message_id": "response_id", "tool_name": "tool.name", "tool_input": "tool.args",
        "tool_output": "tool.result", "input_tokens": "usage.prompt_tokens",
        "output_tokens": "usage.completion_tokens", "cache_read_tokens": "usage.cached_tokens",
        "cost": "cost_usd",
    },
    "input_includes_cached": True,
    "pricing": {"my-model-large": {"input": 2.0, "output": 8.0, "cache_read": 0.5}},
    "resume_command": "mytool --resume {session_id}",
}


def _continue_spec(home: Path) -> dict:
    spec = json.loads((EXAMPLES / "continue.json").read_text())
    return {**spec, "home": str(home)}


@pytest.fixture
def jsonl_adapter() -> GenericAdapter:
    return GenericAdapter(validate_spec(JSONL_SPEC))


@pytest.fixture
def json_adapter() -> GenericAdapter:
    return GenericAdapter(validate_spec(_continue_spec(FIXTURES / "json-tool")))


# ------------------------------------------------------------- validation

@pytest.mark.parametrize("broken, message", [
    ({**JSONL_SPEC, "id": "Bad Id"}, "`id`"),
    ({**JSONL_SPEC, "glob": "../escape/*.jsonl"}, "'..'"),
    ({**JSONL_SPEC, "format": "xml"}, "`format`"),
    ({**JSONL_SPEC, "fields": {"colour": "x"}}, "unknown field"),
    ({**JSONL_SPEC, "fields": {"model": "m"}}, "`role` and `text`"),
    ({**JSONL_SPEC, "color": "red"}, "`color`"),
    ({k: v for k, v in JSONL_SPEC.items() if k != "home"}, "`home`"),
    ("not an object", "JSON object"),
])
def test_bad_specs_are_rejected_with_a_reason(broken, message):
    with pytest.raises(SpecError, match=message.replace("(", r"\(")):
        validate_spec(broken)


def test_a_spec_cannot_take_a_builtin_id():
    with pytest.raises(SpecError, match="already used"):
        validate_spec({**JSONL_SPEC, "id": "claude"}, taken={"claude"})


def test_the_shipped_examples_are_valid():
    validate_spec(json.loads((EXAMPLES / "continue.json").read_text()))
    yaml = pytest.importorskip("yaml")
    validate_spec(yaml.safe_load((EXAMPLES / "mytool.yaml").read_text()))


# ------------------------------------------------------------------ jsonl

def test_capabilities_follow_the_mapped_fields(jsonl_adapter, json_adapter):
    caps = jsonl_adapter.capabilities
    assert caps.usage and caps.cost and caps.cache_tokens and caps.tool_calls
    assert caps.resume and caps.reported_cost and not caps.delete
    plain = json_adapter.capabilities
    assert not plain.usage and not plain.cost and not plain.tool_calls and not plain.resume


def test_jsonl_sessions_are_found_and_summarised(jsonl_adapter):
    files = jsonl_adapter.session_files()
    assert [f.name for f in files] == ["mt-001.jsonl"]
    meta = jsonl_adapter.scan(files[0])
    assert meta.provider == "mytool"
    assert meta.session_id == "mt-001"
    assert meta.project_path == "/home/tester/demo"
    assert meta.title == "summarise the changelog"
    assert meta.corrupt_lines == 1


def test_a_response_split_over_lines_is_counted_once(jsonl_adapter):
    meta = jsonl_adapter.scan(jsonl_adapter.session_files()[0])
    assert meta.user_messages == 1
    assert meta.assistant_messages == 2
    # input_includes_cached: 5000-4000 and 6000-5000.
    assert meta.tokens["input"] == 2_000
    assert meta.tokens["cache_read"] == 9_000
    assert meta.tokens["output"] == 250
    assert meta.tools == {"read": 1}


def test_reported_cost_and_spec_pricing(jsonl_adapter):
    meta = jsonl_adapter.scan(jsonl_adapter.session_files()[0])
    assert meta.reported_cost_usd == pytest.approx(0.006)
    table = jsonl_adapter.pricing_table({"pricing": {}})
    assert session_cost(meta, table) == pytest.approx(0.0105)


def test_jsonl_parse_builds_turns_and_tool_results(jsonl_adapter):
    parsed = jsonl_adapter.parse(jsonl_adapter.session_files()[0])
    kinds = [m["kind"] for m in parsed["messages"]]
    assert kinds == ["user", "assistant", "tool_result", "assistant", "system"]
    first = parsed["messages"][1]
    assert [b["type"] for b in first["blocks"]] == ["text", "tool_use", "text"]
    assert parsed["messages"][2]["blocks"][0]["content"].startswith("## 1.0")
    assert parsed["info"]["session_id"] == "mt-001"
    assert parsed["errors"][0]["line"] == 6


def test_the_resume_command_comes_from_the_spec(jsonl_adapter):
    assert jsonl_adapter.resume_command("mt-001") == "mytool --resume mt-001"


def test_ownership_is_matched_without_walking(jsonl_adapter):
    home = FIXTURES / "jsonl-tool"
    assert jsonl_adapter.owns(home / "logs" / "2026" / "x.jsonl")
    assert jsonl_adapter.owns(home / "logs" / "x.jsonl"), "** may match no directory"
    assert not jsonl_adapter.owns(home / "other" / "x.jsonl")
    assert not jsonl_adapter.owns(home / "logs" / "x.json")


# ------------------------------------------------------------------- json

def test_json_documents_use_the_messages_path(json_adapter):
    meta = json_adapter.scan(json_adapter.session_files()[0])
    assert meta.session_id == "cont-42"
    assert meta.title == "Explain the router"
    assert meta.project_path == "/home/tester/demo"
    assert meta.user_messages == 1 and meta.assistant_messages == 1
    assert meta.corrupt_lines == 1, "a non-object entry is reported"


def test_a_format_without_timestamps_falls_back_to_the_file_time(json_adapter):
    meta = json_adapter.scan(json_adapter.session_files()[0])
    assert meta.last_timestamp is not None


def test_part_lists_are_flattened_to_text(json_adapter):
    parsed = json_adapter.parse(json_adapter.session_files()[0])
    reply = parsed["messages"][1]
    assert reply["blocks"][0]["text"] == "It maps URLs to handlers."
    assert parsed["info"]["title"] == "Explain the router"


# ---------------------------------------------------------------- loading

def test_specs_load_from_config_and_from_the_providers_folder(isolated_app_home, tmp_path):
    home = tmp_path / "continue-home"
    shutil.copytree(FIXTURES / "json-tool", home)
    folder = isolated_app_home / "providers"
    folder.mkdir()
    (folder / "continue.json").write_text(json.dumps(_continue_spec(home)))
    (folder / "broken.json").write_text("{not json")
    (folder / "notes.txt").write_text("ignored")

    registry = Registry({"pricing": {}, "custom_providers": [JSONL_SPEC, {"id": "x"}]})
    ids = [a.id for a in registry.adapters]
    assert "mytool" in ids and "continue" in ids
    sources = {e["source"] for e in registry.errors}
    assert any("broken.json" in s for s in sources)
    assert "custom_providers[1]" in sources


def test_a_duplicate_custom_id_is_reported_once(isolated_app_home):
    registry = Registry({"pricing": {}, "custom_providers": [JSONL_SPEC, JSONL_SPEC]})
    assert [a.id for a in registry.adapters].count("mytool") == 1
    assert any("already used" in e["error"] for e in registry.errors)


def test_custom_providers_honour_settings_and_can_be_disabled(isolated_app_home, tmp_path):
    moved = tmp_path / "moved"
    shutil.copytree(FIXTURES / "jsonl-tool", moved)
    registry = Registry({
        "pricing": {},
        "custom_providers": [JSONL_SPEC],
        "providers": {"mytool": {"path": str(moved), "enabled": False}},
    })
    adapter = registry.get("mytool")
    assert adapter.home == moved and adapter.session_files()
    assert adapter not in registry.enabled()


def test_a_custom_provider_is_indexed_and_searched(jsonl_adapter, json_adapter):
    index = SessionIndex(adapters=[jsonl_adapter, json_adapter])
    index.build(force=True)
    assert sorted(m.provider for m in index.sessions) == ["continue", "mytool"]
    hits = index.search("first release")["hits"]
    assert hits and hits[0]["provider"] == "mytool"


def test_describe_exposes_the_spec_for_the_settings_page(jsonl_adapter):
    described = jsonl_adapter.describe()
    assert described["custom"] is True
    assert described["spec"]["glob"] == "logs/**/*.jsonl"
