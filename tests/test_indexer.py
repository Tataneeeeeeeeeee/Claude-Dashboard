"""Tests for the session index, its disk cache and full-text search."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agentboard.indexer import SessionIndex
from agentboard.paths import cache_path


@pytest.fixture
def index(fake_claude_home) -> SessionIndex:
    """An index over the fake ``~/.claude`` built from the fixtures."""
    built = SessionIndex(fake_claude_home / "projects")
    built.build()
    return built


def test_build_finds_every_transcript(index):
    assert len(index.sessions) == 2
    stats = index.stats()
    assert stats["session_count"] == 2
    assert stats["project_count"] == 1
    assert stats["corrupt_lines"] == 3          # all from messy.jsonl


def test_projects_resolve_from_the_recorded_cwd_not_the_folder_name(index):
    project = index.projects[0]
    assert project.dir_name == "-home-tester-demo"
    # The encoding is lossy, so the cwd inside the transcript is authoritative.
    assert project.path == "/home/tester/demo"
    assert project.decoded_guess is False
    assert project.session_count == 2


def test_project_path_falls_back_to_a_decode_when_no_cwd_is_readable(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "-srv-app"
    root.mkdir(parents=True)
    (root / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl").write_text(
        '{"type": "mode", "mode": "normal"}\n', encoding="utf-8"
    )
    built = SessionIndex(tmp_path / "projects")
    built.build()
    project = built.projects[0]
    assert project.path == "/srv/app"
    assert project.decoded_guess is True
    assert project.exists is False


def test_missing_projects_directory_is_an_empty_index_not_a_crash(tmp_path):
    built = SessionIndex(tmp_path / "nothing-here")
    built.build()
    assert built.sessions == []
    assert built.stats()["projects_dir_exists"] is False


def test_cache_is_reused_when_files_are_unchanged(fake_claude_home):
    first = SessionIndex(fake_claude_home / "projects")
    first.build()
    assert first.progress.scanned == 2
    assert first.progress.from_cache == 0
    assert cache_path().is_file()

    second = SessionIndex(fake_claude_home / "projects")
    second.build()
    assert second.progress.from_cache == 2
    assert second.progress.scanned == 0
    assert len(second.sessions) == 2


def test_cache_is_bypassed_when_a_file_changes(fake_claude_home):
    first = SessionIndex(fake_claude_home / "projects")
    first.build()

    target = next((fake_claude_home / "projects").rglob("*.jsonl"))
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "ai-title", "aiTitle": "Renamed"}) + "\n")
    # mtime granularity on some filesystems is coarse; make the change visible.
    future = time.time() + 5
    import os

    os.utime(target, (future, future))

    second = SessionIndex(fake_claude_home / "projects")
    second.build()
    assert second.progress.scanned == 1
    assert second.progress.from_cache == 1


def test_force_rebuild_ignores_the_cache(fake_claude_home):
    SessionIndex(fake_claude_home / "projects").build()
    forced = SessionIndex(fake_claude_home / "projects")
    forced.build(force=True)
    assert forced.progress.scanned == 2
    assert forced.progress.from_cache == 0


def test_a_corrupt_cache_file_is_ignored(fake_claude_home):
    cache_path().write_text("{ not json", encoding="utf-8")
    built = SessionIndex(fake_claude_home / "projects")
    built.build()
    assert len(built.sessions) == 2


def test_refresh_path_updates_and_removes(index, fake_claude_home):
    target = next((fake_claude_home / "projects").rglob("*.jsonl"))
    assert index.refresh_path(target) is not None
    target.unlink()
    assert index.refresh_path(target) is None
    assert len(index.sessions) == 1


def test_progress_reports_percent_and_counts(index):
    progress = index.progress.to_dict()
    assert progress["running"] is False
    assert progress["total"] == 2 and progress["done"] == 2
    assert progress["percent"] == 100.0
    assert progress["error"] is None


# --------------------------------------------------------------- filtering

def test_filter_by_project_model_and_tool(index):
    assert len(index.filter(project="-home-tester-demo")) == 2
    assert len(index.filter(project="/home/tester/demo")) == 2
    assert len(index.filter(project="nope")) == 0
    assert len(index.filter(model="claude-opus-5")) == 1
    assert len(index.filter(tool="Read")) == 1
    assert len(index.filter(tool="Bash")) == 0
    assert len(index.filter(branch="main")) == 2


def test_filter_by_date_range_and_message_count(index):
    assert len(index.filter(since="2026-03-03")) == 1
    assert len(index.filter(until="2026-03-02T23:59:59Z")) == 1
    assert len(index.filter(min_messages=3)) == 1
    assert len(index.filter(max_messages=2)) == 1


def test_filter_by_favorites_and_tags(index):
    ids = [m.session_id for m in index.sessions]
    favorites = [ids[0]]
    assert len(index.filter(only_favorites=True, favorites=favorites)) == 1
    tags = {ids[0]: ["keep", "demo"]}
    assert len(index.filter(tag="keep", tags=tags)) == 1
    assert len(index.filter(tag="absent", tags=tags)) == 0


def test_sorting_orders_are_distinct(index):
    recent = [m.session_id for m in index.filter(sort="recent")]
    oldest = [m.session_id for m in index.filter(sort="oldest")]
    assert recent == list(reversed(oldest))
    expensive = index.filter(sort="expensive")
    assert expensive[0].total_tokens >= expensive[-1].total_tokens
    assert index.filter(sort="size")[0].file_size >= index.filter(sort="size")[-1].file_size


def test_distinct_values_feed_the_filter_dropdowns(index):
    distinct = index.distinct()
    assert "claude-opus-5" in distinct["models"]
    assert distinct["tools"] == ["Read"]
    assert distinct["branches"] == ["main"]


# ------------------------------------------------------------------ search

def test_search_finds_text_in_prose_thinking_and_tool_io(index):
    assert index.search("health check endpoint")["hits"]
    assert index.search("Need to read the router")["hits"]      # thinking block
    assert index.search("/srv/router.py")["hits"]               # tool_use input
    assert index.search("from flask import")["hits"]            # tool_result body


def test_search_results_carry_jump_targets(index):
    hit = index.search("Added `/health`")["hits"][0]
    assert hit["session_id"]
    assert hit["line"] > 0
    assert hit["uuid"] == "s4"
    assert "Added" in hit["snippet"]
    assert hit["match_length"] == len("Added `/health`")


def test_search_is_case_insensitive_by_default(index):
    assert index.search("HEALTH CHECK")["hits"]
    assert index.search("HEALTH CHECK", case_sensitive=True)["hits"] == []


def test_search_supports_regular_expressions(index):
    assert index.search(r"/health|/status", regex=True)["hits"]
    bad = index.search("(unclosed", regex=True)
    assert bad["hits"] == [] and "invalid pattern" in bad["error"]


def test_search_respects_the_limit_and_flags_truncation(index):
    result = index.search("e", limit=2)
    assert len(result["hits"]) == 2
    assert result["truncated"] is True


def test_search_skips_files_that_cannot_match(index):
    result = index.search("health check endpoint")
    assert result["scanned_files"] == 2
    assert result["matched_files"] == 1          # the byte-level prefilter works


def test_empty_search_returns_nothing_quietly(index):
    assert index.search("")["hits"] == []
    assert index.search("   ")["hits"] == []


def test_search_finds_nothing_for_an_absent_term(index):
    result = index.search("kubernetes")
    assert result["hits"] == [] and result["matched_files"] == 0


# ------------------------------------------------------------- maintenance

def test_maintenance_helpers_select_the_right_sessions(index):
    assert len(index.older_than(1)) == 2         # fixtures are dated 2026-03
    assert len(index.older_than(100_000)) == 0
    assert len(index.smaller_than(3)) == 1
    assert len(index.orphaned()) == 2            # /home/tester/demo does not exist


def test_symlinked_transcripts_are_not_indexed(fake_claude_home, tmp_path):
    """A link cannot be deleted, so it must never appear as a session."""
    outsider = tmp_path / "outside.jsonl"
    outsider.write_text('{"type":"mode"}\n', encoding="utf-8")
    project = fake_claude_home / "projects" / "-home-tester-demo"
    (project / "linked.jsonl").symlink_to(outsider)

    built = SessionIndex(fake_claude_home / "projects")
    built.build()

    assert len(built.sessions) == 2
    # Compare file names: the temporary directory itself contains "linked".
    names = {Path(m.path).name for m in built.sessions}
    assert "linked.jsonl" not in names


def test_a_symlinked_project_directory_is_skipped(fake_claude_home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "sneaky.jsonl").write_text('{"type":"mode"}\n', encoding="utf-8")
    (fake_claude_home / "projects" / "-linked").symlink_to(elsewhere, target_is_directory=True)

    built = SessionIndex(fake_claude_home / "projects")
    built.build()

    assert len(built.sessions) == 2
    names = {Path(m.path).name for m in built.sessions}
    assert "sneaky.jsonl" not in names
