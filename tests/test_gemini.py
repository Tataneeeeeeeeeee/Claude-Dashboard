"""The Gemini CLI adapter, against chat documents in ``~/.gemini/tmp``."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentboard.indexer import SessionIndex
from agentboard.providers.claude import ClaudeAdapter
from agentboard.providers.gemini import GeminiAdapter, project_hash
from agentboard.usage import session_cost

DEMO_HASH = project_hash("/home/tester/demo")


@pytest.fixture
def adapter(fake_gemini_home) -> GeminiAdapter:
    return GeminiAdapter()


def _chat(adapter: GeminiAdapter, needle: str) -> Path:
    return next(p for p in adapter.session_files() if needle in p.name)


def test_gemini_home_follows_the_cli_override(adapter, fake_gemini_home):
    assert adapter.home == fake_gemini_home
    assert adapter.root == fake_gemini_home / "tmp"
    assert adapter.detect().data_found


def test_every_chat_file_is_found(adapter):
    assert len(adapter.session_files()) == 3


def test_a_chat_is_summarised(adapter):
    meta = adapter.scan(_chat(adapter, "5b0c7e2a"))
    assert meta.provider == "gemini"
    assert meta.session_id == "5b0c7e2a-1d3f-4e5a-9b8c-7d6e5f4a3b2c"
    assert meta.project_dir == DEMO_HASH
    assert meta.title == "Add a docstring to router.py"
    assert meta.first_timestamp == "2026-09-22T14:05:00.000Z"
    assert meta.last_timestamp == "2026-09-22T14:07:30.000Z"


def test_slash_commands_are_not_prompts(adapter):
    meta = adapter.scan(_chat(adapter, "5b0c7e2a"))
    assert meta.user_messages == 1
    assert meta.assistant_messages == 3
    assert meta.message_count == 4


def test_tokens_are_rearranged_into_the_normalized_counters(adapter):
    meta = adapter.scan(_chat(adapter, "5b0c7e2a"))
    # input includes cached tokens; thoughts are billed as output.
    assert meta.tokens["input"] == 4_500
    assert meta.tokens["cache_read"] == 15_000
    assert meta.tokens["output"] == 270
    assert meta.tokens["thinking"] == 80
    assert meta.models["gemini-2.5-pro"]["messages"] == 2
    assert meta.models["gemini-2.5-flash"]["messages"] == 1


def test_tools_errors_and_files_are_recorded(adapter):
    meta = adapter.scan(_chat(adapter, "5b0c7e2a"))
    assert meta.tools == {"read_file": 1, "replace": 1}
    assert meta.error_messages == 1
    assert meta.files_touched == ["/home/tester/demo/router.py"]


def test_cost_uses_gemini_prices(adapter):
    meta = adapter.scan(_chat(adapter, "5b0c7e2a"))
    table = adapter.pricing_table({"pricing": {}})
    assert session_cost(meta, table) == pytest.approx(0.009175)


def test_a_broken_document_is_reported_not_fatal(adapter):
    meta = adapter.scan(_chat(adapter, "broken"))
    assert meta.corrupt_lines == 1
    parsed = adapter.parse(_chat(adapter, "broken"))
    assert parsed["messages"] == [] and parsed["errors"]


def test_a_summary_and_a_project_root_marker_are_used(adapter):
    meta = adapter.scan(_chat(adapter, "-ok"))
    assert meta.title == "Explain the build"
    assert meta.project_path == "/home/tester/other"
    assert list(meta.models) == ["gemini"], "a model-less reply is still counted"


def test_the_conversation_is_rebuilt(adapter):
    parsed = adapter.parse(_chat(adapter, "5b0c7e2a"))
    kinds = [m["kind"] for m in parsed["messages"]]
    assert kinds == ["user", "assistant", "tool_result", "assistant", "tool_result",
                     "system", "user", "assistant"]
    first = parsed["messages"][1]
    assert [b["type"] for b in first["blocks"]] == ["thinking", "text", "tool_use"]
    assert first["model"] == "gemini-2.5-pro"
    assert first["usage"]["cache_read"] == 6_000
    read, failed = parsed["messages"][2], parsed["messages"][4]
    assert "redirect('/login')" in read["blocks"][0]["content"]
    assert failed["blocks"][0]["is_error"] is True
    assert failed["blocks"][0]["content"] == "Permission denied"
    assert parsed["messages"][6]["is_meta"] is True, "a slash command is marked meta"


def test_lines_are_one_based_so_search_can_jump(adapter):
    path = _chat(adapter, "5b0c7e2a")
    lines = [m["line"] for m in adapter.parse(path)["messages"]]
    assert min(lines) == 1
    hits = [e for e in adapter.search_entries(path) if "docstring" in e.text]
    assert hits and hits[0].line == 1


def test_the_project_is_recovered_from_another_providers_path(fake_claude_home, fake_gemini_home):
    index = SessionIndex(adapters=[ClaudeAdapter(), GeminiAdapter()])
    index.build(force=True)
    gemini = [m for m in index.sessions if m.session_id.startswith("5b0c7e2a")][0]
    assert gemini.project_path == "/home/tester/demo"
    demo = next(p for p in index.projects if p.path == "/home/tester/demo")
    assert demo.providers == {"claude": 2, "gemini": 1}


def test_an_unknown_hash_stays_a_hash(adapter):
    index = SessionIndex(adapters=[adapter])
    index.build(force=True)
    keys = {p.key for p in index.projects}
    assert f"gemini:{DEMO_HASH}" in keys


def test_gemini_is_read_only_and_cannot_resume_by_default(adapter):
    assert adapter.resume_command("x") is None
    assert not adapter.capabilities.delete
    configured = GeminiAdapter({"resume_command": "gemini --resume {session_id}"})
    assert configured.resume_command("x") == "gemini --resume x"


def test_only_chat_files_are_owned(adapter, fake_gemini_home):
    root = fake_gemini_home / "tmp"
    assert adapter.owns(root / DEMO_HASH / "chats" / "session-x.json")
    assert not adapter.owns(root / DEMO_HASH / "logs.json")
    assert not adapter.owns(fake_gemini_home / "settings.json")
