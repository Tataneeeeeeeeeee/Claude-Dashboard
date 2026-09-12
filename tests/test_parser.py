"""Unit tests for the streaming JSONL parser."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_dashboard.parser import (
    api_message_key,
    block_list,
    is_human_turn,
    iter_entries,
    parse_conversation,
    parse_timestamp,
    scan_session,
    text_of_blocks,
)


# --------------------------------------------------------------- primitives

def test_parse_timestamp_accepts_iso_epoch_seconds_and_millis():
    iso = parse_timestamp("2026-03-02T09:00:00.000Z")
    assert iso == datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    assert parse_timestamp(1772442000000) == parse_timestamp(1772442000)
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("not a date") is None


def test_block_list_normalises_every_content_shape():
    assert block_list({"content": "hi"}) == [{"type": "text", "text": "hi"}]
    assert block_list({"content": [{"type": "text", "text": "a"}]})[0]["text"] == "a"
    # Non-dict list elements are wrapped rather than dropped.
    assert block_list({"content": ["raw", 7]}) == [
        {"type": "text", "text": "raw"},
        {"type": "text", "text": "7"},
    ]
    assert block_list({"content": None}) == []
    assert block_list(None) == []
    assert block_list("not a message") == []


def test_text_of_blocks_ignores_non_text_blocks():
    blocks = [
        {"type": "text", "text": "visible"},
        {"type": "thinking", "thinking": "hidden"},
        {"type": "tool_use", "name": "Bash"},
    ]
    assert text_of_blocks(blocks) == "visible"


def test_is_human_turn_rejects_tool_results_and_meta():
    human = {"type": "user", "message": {"role": "user", "content": "do the thing"}}
    assert is_human_turn(human)

    tool_result = {
        "type": "user",
        "toolUseResult": {"stdout": "ok"},
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
    }
    assert not is_human_turn(tool_result)

    meta = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "x"}}
    assert not is_human_turn(meta)

    caveat = {
        "type": "user",
        "message": {"role": "user", "content": "<local-command-caveat>x</local-command-caveat>"},
    }
    assert not is_human_turn(caveat)
    assert not is_human_turn({"type": "assistant", "message": {"content": "x"}})


def test_api_message_key_prefers_message_id_then_request_then_uuid():
    assert api_message_key({"message": {"id": "msg_A"}, "requestId": "r", "uuid": "u"}) == "msg_A"
    assert api_message_key({"message": {}, "requestId": "r", "uuid": "u"}) == "r"
    assert api_message_key({"uuid": "u"}) == "u"


# ------------------------------------------------------------- iter_entries

def test_iter_entries_reports_corruption_without_stopping(fixtures):
    results = list(iter_entries(fixtures / "messy.jsonl"))
    errors = [r for r in results if r[2] is not None]
    good = [r for r in results if r[2] is None]
    # Truncated JSON, a bare string, and a truncated final line all fail.
    assert len(errors) == 3
    # Everything after the corruption is still parsed.
    assert len(good) == 5
    # Line numbers are 1-based and strictly increasing.
    numbers = [r[0] for r in results]
    assert numbers == sorted(numbers) and numbers[0] >= 1


def test_iter_entries_on_empty_and_missing_files(fixtures, tmp_path):
    assert list(iter_entries(fixtures / "empty.jsonl")) == []
    assert list(iter_entries(tmp_path / "does-not-exist.jsonl")) == []


# ------------------------------------------------------------ scan_session

def test_scan_session_deduplicates_usage_across_split_lines(fixtures):
    """Three assistant lines sharing one ``message.id`` bill once, not three."""
    meta = scan_session(fixtures / "basic.jsonl")

    assert meta.assistant_messages == 2          # msg_A and msg_B, not 4 lines
    assert meta.user_messages == 1
    assert meta.message_count == 3
    assert meta.tool_calls == 1

    # msg_A: 10/100/2000(1h)/5000 counted once.  msg_B: 5/50/800(5m)/7000.
    assert meta.tokens["input"] == 15
    assert meta.tokens["output"] == 150
    assert meta.tokens["cache_write"] == 2800
    assert meta.tokens["cache_write_1h"] == 2000
    assert meta.tokens["cache_write_5m"] == 800
    assert meta.tokens["cache_read"] == 12000
    assert meta.tokens["thinking"] == 40
    assert meta.total_tokens == 15 + 150 + 2800 + 12000


def test_scan_session_reads_metadata_and_title(fixtures):
    meta = scan_session(fixtures / "basic.jsonl")
    # The filename stem ("basic") is not uuid-shaped, so the recorded
    # sessionId wins - the real install always names files by session id.
    assert meta.session_id == "11111111-2222-3333-4444-555555555555"
    assert meta.project_path == "/home/tester/demo"
    assert meta.git_branch == "main"
    assert meta.versions == ["2.1.263"]
    assert meta.title == "Add a health check endpoint"
    assert meta.title_source == "ai-title"
    assert meta.first_timestamp == "2026-03-02T09:00:00.000Z"
    assert meta.last_timestamp == "2026-03-02T09:00:13.000Z"
    assert meta.duration_seconds == pytest.approx(13.0)
    assert meta.tools == {"Read": 1}
    assert meta.reported_cost_usd == pytest.approx(0.034825)
    assert meta.corrupt_lines == 0


def test_scan_session_buckets_by_day_and_heatmap(fixtures):
    meta = scan_session(fixtures / "basic.jsonl")
    assert set(meta.daily) == {"2026-03-02"}
    day = meta.daily["2026-03-02"]
    assert day["messages"] == 2            # deduplicated API responses
    assert day["tool_calls"] == 1
    assert day["output"] == 150
    # 2026-03-02 is a Monday; entries land at 09:00 UTC.
    assert meta.heatmap["0-9"] > 0
    assert sum(meta.heatmap.values()) == 8


def test_scan_session_survives_corruption_and_odd_shapes(fixtures):
    meta = scan_session(fixtures / "messy.jsonl")
    assert meta.corrupt_lines == 3
    assert meta.user_messages == 1                    # the isMeta caveat excluded
    assert meta.assistant_messages == 1
    assert "<synthetic>" in meta.models
    assert meta.title == "Plain string content"
    assert meta.title_source == "first-message"


def test_scan_session_on_empty_file_is_not_an_error(fixtures):
    meta = scan_session(fixtures / "empty.jsonl")
    assert meta.message_count == 0
    assert meta.total_tokens == 0
    assert meta.title == "empty"
    assert meta.title_source == "session-id"


def test_scan_session_on_missing_file_returns_empty_meta(tmp_path):
    meta = scan_session(tmp_path / "nope.jsonl")
    assert meta.message_count == 0
    assert meta.file_size == 0


def test_session_meta_round_trips_through_the_cache(fixtures):
    from claude_dashboard.parser import SessionMeta

    original = scan_session(fixtures / "basic.jsonl")
    restored = SessionMeta.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()


# ------------------------------------------------------- parse_conversation

def test_parse_conversation_classifies_every_entry(fixtures):
    parsed = parse_conversation(fixtures / "basic.jsonl")
    kinds = [m["kind"] for m in parsed["messages"]]

    assert kinds.count("user") == 1
    assert kinds.count("tool_result") == 1
    assert kinds.count("attachment") == 1
    assert kinds.count("system") == 1
    assert parsed["info"]["title"] == "Add a health check endpoint"
    assert parsed["info"]["cwd"] == "/home/tester/demo"
    assert parsed["errors"] == []


def test_parse_conversation_merges_one_response_split_across_lines(fixtures):
    """The three lines of msg_A become one turn carrying all three blocks."""
    parsed = parse_conversation(fixtures / "basic.jsonl")
    assistants = [m for m in parsed["messages"] if m["kind"] == "assistant"]

    assert len(assistants) == 2               # msg_A and msg_B, not four lines
    first = assistants[0]
    assert [b["type"] for b in first["blocks"]] == ["thinking", "text", "tool_use"]
    assert first["extra"]["merged_uuids"] == ["s2", "s3"]
    assert first["uuid"] == "s1"              # identity of the first line
    # Usage is the response's, recorded once rather than three times.
    assert first["usage"]["output"] == 100


def test_merging_never_spans_a_tool_result(fixtures):
    """Two responses separated by a tool result stay separate turns."""
    parsed = parse_conversation(fixtures / "basic.jsonl")
    kinds = [m["kind"] for m in parsed["messages"]]
    assert kinds.index("tool_result") < kinds.index("assistant", kinds.index("tool_result"))


def test_parse_conversation_links_tool_results_to_their_tool_name(fixtures):
    parsed = parse_conversation(fixtures / "basic.jsonl")
    results = [b for m in parsed["messages"] for b in m["blocks"] if b["type"] == "tool_result"]
    assert results[0]["name"] == "Read"
    assert results[0]["tool_use_id"] == "toolu_1"


def test_parse_conversation_truncates_long_tool_output(tmp_path):
    import json

    path = tmp_path / "big.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "x",
                "toolUseResult": {"stdout": "y"},
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": "y" * 5000}
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    parsed = parse_conversation(path, tool_output_limit=100)
    content = parsed["messages"][0]["blocks"][0]["content"]
    assert len(content) < 200
    assert "truncated" in content


def test_parse_conversation_replaces_inline_images_with_descriptors(fixtures):
    parsed = parse_conversation(fixtures / "messy.jsonl")
    blocks = [b for m in parsed["messages"] for b in m["blocks"] if b["type"] == "tool_result"]
    inner = blocks[0]["content"][0]
    assert inner["type"] == "image"
    assert inner["media_type"] == "image/png"
    assert inner["bytes"] == 512          # the base64 payload itself is dropped
    assert "data" not in inner


def test_parse_conversation_records_corrupt_lines(fixtures):
    parsed = parse_conversation(fixtures / "messy.jsonl")
    assert len(parsed["errors"]) == 3
    assert all("line" in e and "error" in e for e in parsed["errors"])
    assert parsed["info"]["corrupt_lines"] == 3
