"""The Codex CLI adapter, against rollouts in both on-disk formats."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentboard.indexer import SessionIndex
from agentboard.providers.claude import ClaudeAdapter
from agentboard.providers.codex import CodexAdapter
from agentboard.usage import session_cost

CURRENT = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
LEGACY = "11111111-aaaa-bbbb-cccc-222222222222"


@pytest.fixture
def adapter(fake_codex_home) -> CodexAdapter:
    return CodexAdapter()


def _file(adapter: CodexAdapter, session_id: str) -> Path:
    return next(p for p in adapter.session_files() if session_id in p.name)


def test_codex_home_follows_the_environment(adapter, fake_codex_home):
    assert adapter.home == fake_codex_home
    assert adapter.root == fake_codex_home / "sessions"
    assert adapter.detect().data_found


def test_rollouts_are_found_in_dated_folders(adapter):
    names = [p.name for p in adapter.session_files()]
    assert len(names) == 2
    assert all(name.startswith("rollout-") for name in names)


def test_the_session_identity_comes_from_session_meta(adapter):
    meta = adapter.scan(_file(adapter, CURRENT))
    assert meta.provider == "codex"
    assert meta.session_id == CURRENT
    assert meta.project_path == "/home/tester/demo"
    assert meta.git_branch == "feature/login"
    assert meta.versions == ["0.42.0"]
    assert meta.title == "Fix the login redirect bug in router.py"


def test_injected_context_is_not_counted_as_a_message(adapter):
    meta = adapter.scan(_file(adapter, CURRENT))
    assert meta.user_messages == 2
    assert meta.assistant_messages == 2
    assert meta.message_count == 4


def test_usage_is_charged_once_per_increase_and_cache_is_split_out(adapter):
    meta = adapter.scan(_file(adapter, CURRENT))
    # Totals end at 40 000 input of which 28 000 cached, and 1 000 output.
    # A repeated token_count with unchanged totals must add nothing.
    assert meta.tokens["input"] == 12_000
    assert meta.tokens["cache_read"] == 28_000
    assert meta.tokens["output"] == 1_000
    assert meta.tokens["thinking"] == 256
    assert meta.total_tokens == 41_000
    assert set(meta.models) == {"gpt-5-codex"}
    assert meta.models["gpt-5-codex"]["messages"] == 2


def test_usage_is_bucketed_by_day(adapter):
    meta = adapter.scan(_file(adapter, CURRENT))
    assert sorted(meta.daily) == ["2026-09-20", "2026-09-21"]
    assert meta.daily["2026-09-21"]["input"] == 2_000
    assert meta.daily["2026-09-20"]["tool_calls"] == 3


def test_tool_calls_and_patched_files_are_recorded(adapter):
    meta = adapter.scan(_file(adapter, CURRENT))
    assert meta.tools == {"shell": 2, "apply_patch": 1}
    assert meta.files_touched == ["router.py"]


def test_a_truncated_final_line_is_counted_not_fatal(adapter):
    assert adapter.scan(_file(adapter, CURRENT)).corrupt_lines == 1


def test_cost_uses_openai_prices_including_the_cached_rate(adapter):
    meta = adapter.scan(_file(adapter, CURRENT))
    table = adapter.pricing_table({"pricing": {}})
    # 12k x $1.25 + 28k x $0.125 + 1k x $10, per million.
    assert session_cost(meta, table) == pytest.approx(0.0285)


def test_an_unknown_openai_model_uses_the_openai_default_not_claude(adapter):
    table = adapter.pricing_table({"pricing": {"default": {"input": 99, "output": 99}}})
    assert table["default"]["input"] == 1.25


def test_the_conversation_is_rebuilt_into_turns(adapter):
    parsed = adapter.parse(_file(adapter, CURRENT))
    kinds = [m["kind"] for m in parsed["messages"]]
    assert kinds[:4] == ["attachment", "user", "assistant", "tool_result"]
    assert kinds.count("user") == 2
    assert parsed["info"]["session_id"] == CURRENT
    assert parsed["info"]["cwd"] == "/home/tester/demo"
    assert parsed["errors"] and parsed["errors"][0]["line"] == 20


def test_reasoning_and_tool_calls_live_in_the_assistant_turn(adapter):
    messages = adapter.parse(_file(adapter, CURRENT))["messages"]
    first = next(m for m in messages if m["kind"] == "assistant")
    types = [b["type"] for b in first["blocks"]]
    assert types == ["thinking", "tool_use"]
    assert first["blocks"][1]["input"]["command"] == "bash -lc grep -n redirect router.py"
    assert first["usage"]["cache_read"] == 8_000
    assert first["model"] == "gpt-5-codex"


def test_tool_results_carry_their_name_and_failure(adapter):
    results = [m for m in adapter.parse(_file(adapter, CURRENT))["messages"]
               if m["kind"] == "tool_result"]
    assert [r["blocks"][0]["name"] for r in results] == ["shell", "apply_patch", "shell"]
    assert [r["blocks"][0]["is_error"] for r in results] == [False, False, True]
    assert "redirect('/login')" in results[0]["blocks"][0]["content"]


def test_the_legacy_bare_format_is_read(adapter):
    path = _file(adapter, LEGACY)
    meta = adapter.scan(path)
    assert meta.session_id == LEGACY
    assert meta.user_messages == 1 and meta.assistant_messages == 1
    assert meta.tools == {"shell": 1}
    kinds = [m["kind"] for m in adapter.parse(path)["messages"]]
    assert kinds == ["user", "assistant", "tool_result", "assistant"]


def test_search_finds_text_in_tool_output_and_jumps_to_the_line(adapter):
    path = _file(adapter, CURRENT)
    entries = [e for e in adapter.search_entries(path) if "redirect('/login')" in e.text]
    assert entries
    message_lines = {m["line"] for m in adapter.parse(path)["messages"]}
    assert all(entry.line in message_lines for entry in entries)


def test_codex_can_resume_by_id(adapter):
    assert adapter.resume_command(CURRENT) == f"codex resume {CURRENT}"
    assert adapter.capabilities.resume and not adapter.capabilities.delete


def test_codex_and_claude_share_a_project(fake_claude_home, fake_codex_home):
    index = SessionIndex(adapters=[ClaudeAdapter(), CodexAdapter()])
    index.build(force=True)
    demo = next(p for p in index.projects if p.path == "/home/tester/demo")
    assert demo.providers == {"claude": 2, "codex": 1}
    assert index.search("redirect", provider="codex")["hits"]


def test_a_new_rollout_is_picked_up_live(adapter, fake_codex_home):
    index = SessionIndex(adapters=[adapter])
    index.build(force=True)
    source = _file(adapter, LEGACY)
    copy = source.with_name(source.name.replace(LEGACY, "22222222-aaaa-bbbb-cccc-222222222222"))
    copy.write_text(source.read_text().replace(LEGACY, "22222222-aaaa-bbbb-cccc-222222222222"))
    assert index.refresh_path(copy).provider == "codex"
    assert len(index.sessions) == 3
