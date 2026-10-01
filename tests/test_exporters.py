"""Tests for the Markdown, HTML and JSON conversation exports."""

from __future__ import annotations

import json
import re

import pytest

from agentboard import exporters
from agentboard.parser import parse_conversation


@pytest.fixture
def parsed(fixtures):
    """The basic fixture conversation, with session metadata attached."""
    data = parse_conversation(fixtures / "basic.jsonl")
    data["meta"] = {
        "first_timestamp": "2026-03-02T09:00:00.000Z",
        "last_timestamp": "2026-03-02T09:00:13.000Z",
        "message_count": 3,
        "tool_calls": 1,
        "models": {"claude-opus-5": {}},
        "total_tokens": 14965,
        "estimated_cost": 0.034825,
        "reported_cost_usd": 0.034825,
    }
    return data


# ------------------------------------------------------------- markdown

def test_markdown_has_a_title_and_front_matter(parsed):
    text = exporters.to_markdown(parsed)
    assert text.startswith("# Add a health check endpoint")
    assert "- **Project:** /home/tester/demo" in text
    assert "- **Git branch:** main" in text
    assert "local estimate" in text


def test_markdown_renders_each_role(parsed):
    text = exporters.to_markdown(parsed)
    assert "## You" in text
    assert "## Claude" in text
    assert "Add a health check endpoint" in text
    assert "Added `/health`." in text


def test_markdown_folds_thinking_and_tools(parsed):
    text = exporters.to_markdown(parsed)
    assert "<summary>Thinking</summary>" in text
    assert "<summary>Tool call: Read</summary>" in text
    assert "<summary>Result from Read</summary>" in text
    assert "/srv/router.py" in text


def test_markdown_can_omit_thinking_and_tools(parsed):
    text = exporters.to_markdown(parsed, include_thinking=False, include_tools=False)
    # The front matter still reports the tool-call count; what must be gone
    # are the folded blocks themselves.
    assert "<summary>Thinking</summary>" not in text
    assert "<summary>Tool call:" not in text
    assert "<summary>Result from" not in text
    assert "Added `/health`." in text


def test_markdown_excludes_attachments_by_default(parsed):
    assert "Injected context" not in exporters.to_markdown(parsed)
    assert "Injected context" in exporters.to_markdown(parsed, include_attachments=True)


def test_markdown_ends_with_a_single_newline(parsed):
    text = exporters.to_markdown(parsed)
    assert text.endswith("\n") and not text.endswith("\n\n")


# ----------------------------------------------------------------- html

def test_html_is_self_contained(parsed):
    text = exporters.to_html(parsed)
    assert text.startswith("<!DOCTYPE html>")
    assert "<style>" in text
    # Nothing may be fetched at runtime.
    assert "http://" not in text.replace("http://www.w3.org", "")
    assert "<script" not in text
    assert 'src="' not in text


def test_html_escapes_transcript_content(tmp_path):
    path = tmp_path / "xss.jsonl"
    path.write_text(
        json.dumps({
            "type": "user", "uuid": "u1", "timestamp": "2026-03-02T09:00:00.000Z",
            "message": {"role": "user", "content": "<script>alert('x')</script>"},
        }) + "\n",
        encoding="utf-8",
    )
    text = exporters.to_html(parse_conversation(path))
    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text


def test_html_marks_errors_and_sidechains(tmp_path):
    path = tmp_path / "odd.jsonl"
    path.write_text(
        json.dumps({
            "type": "assistant", "uuid": "a1", "isSidechain": True,
            "isApiErrorMessage": True, "timestamp": "2026-03-02T09:00:00.000Z",
            "message": {"id": "m", "role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "boom"}]},
        }) + "\n",
        encoding="utf-8",
    )
    text = exporters.to_html(parse_conversation(path))
    assert "sidechain" in text
    assert "error" in text
    assert "sub-agent" in text


# ----------------------------------------------------------------- json

def test_json_round_trips(parsed):
    payload = json.loads(exporters.to_json(parsed))
    assert payload["exported_by"] == "Agentboard"
    assert payload["info"]["cwd"] == "/home/tester/demo"
    assert len(payload["messages"]) == len(parsed["messages"])
    assert "local estimates" in payload["note"]


# ------------------------------------------------------------ filenames

@pytest.mark.parametrize(
    "fmt,extension", [("markdown", ".md"), ("html", ".html"), ("json", ".json")]
)
def test_suggested_filename_uses_the_title(parsed, fmt, extension):
    name = exporters.suggested_filename(parsed, fmt)
    assert name == "Add-a-health-check-endpoint" + extension


def test_suggested_filename_strips_unsafe_characters():
    data = {"info": {"title": "fix: ../../etc/passwd <bad>", "session_id": "x"}}
    name = exporters.suggested_filename(data, "markdown")
    assert "/" not in name and ".." not in name
    assert re.fullmatch(r"[A-Za-z0-9\-]+\.md", name)


def test_suggested_filename_falls_back_when_the_title_is_unusable():
    data = {"info": {"title": "///", "session_id": "abc"}}
    assert exporters.suggested_filename(data, "json") == "conversation.json"


# ------------------------------------------------------------- robustness

def test_exports_survive_a_messy_transcript(fixtures):
    data = parse_conversation(fixtures / "messy.jsonl")
    assert "Plain string content" in exporters.to_markdown(data)
    assert "<!DOCTYPE html>" in exporters.to_html(data)
    assert json.loads(exporters.to_json(data))["errors"]


def test_exports_handle_an_empty_conversation(fixtures):
    data = parse_conversation(fixtures / "empty.jsonl")
    assert exporters.to_markdown(data).startswith("# empty")
    assert "<!DOCTYPE html>" in exporters.to_html(data)
    assert json.loads(exporters.to_json(data))["messages"] == []


def test_long_tool_output_is_truncated_in_exports(tmp_path):
    path = tmp_path / "big.jsonl"
    path.write_text(
        json.dumps({
            "type": "user", "uuid": "u", "toolUseResult": {"stdout": "x"},
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "y" * 100_000}]},
        }) + "\n",
        encoding="utf-8",
    )
    data = parse_conversation(path, tool_output_limit=500)
    text = exporters.to_markdown(data)
    assert "truncated" in text
    assert len(text) < 5000


def test_images_become_descriptors_not_base64(fixtures):
    text = exporters.to_markdown(parse_conversation(fixtures / "messy.jsonl"))
    assert "[image" in text
    assert "A" * 200 not in text
