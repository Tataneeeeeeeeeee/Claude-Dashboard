"""Tests for the coalescing filesystem watcher."""

from __future__ import annotations

import time

import pytest

from agentboard.watcher import FileWatcher, start_watcher


@pytest.fixture
def project(tmp_path):
    """A projects directory with one transcript in it."""
    root = tmp_path / "projects" / "-home-me-app"
    root.mkdir(parents=True)
    (root / "aaaa.jsonl").write_text('{"type":"mode"}\n', encoding="utf-8")
    return tmp_path / "projects"


def test_only_transcripts_are_queued(project):
    seen = []
    watcher = FileWatcher(project, seen.append, settle_seconds=0.05)
    watcher.note("/x/a.jsonl")
    watcher.note("/x/notes.txt")
    watcher.note("/x/b.jsonl")
    assert set(watcher._pending) == {"/x/a.jsonl", "/x/b.jsonl"}
    assert watcher.events_seen == 2


def test_repeated_writes_coalesce_into_one_refresh(project):
    """An active session appends constantly; it must be re-scanned once."""
    batches = []
    watcher = FileWatcher(project, batches.append, settle_seconds=0.2)
    watcher._stop.clear()

    import threading

    worker = threading.Thread(target=watcher._drain, daemon=True)
    worker.start()
    try:
        for _ in range(25):
            watcher.note("/x/busy.jsonl")
            time.sleep(0.01)
        deadline = time.time() + 5
        while time.time() < deadline and not batches:
            time.sleep(0.05)
    finally:
        watcher._stop.set()
        worker.join(timeout=2)

    assert batches, "the watcher never fired"
    assert batches[0] == {"/x/busy.jsonl"}
    assert watcher.events_seen == 25, "every event was seen"
    assert len(batches) == 1, f"25 writes should coalesce into one refresh, got {len(batches)}"


def test_a_callback_failure_does_not_kill_the_watcher(project):
    calls = {"n": 0}

    def explode(_paths):
        calls["n"] += 1
        raise RuntimeError("boom")

    watcher = FileWatcher(project, explode, settle_seconds=0.05)
    import threading

    worker = threading.Thread(target=watcher._drain, daemon=True)
    worker.start()
    try:
        watcher.note("/x/a.jsonl")
        time.sleep(0.5)
        watcher.note("/x/b.jsonl")
        time.sleep(0.5)
    finally:
        watcher._stop.set()
        worker.join(timeout=2)

    assert calls["n"] >= 2, "the worker stopped after the first failure"


def test_start_returns_false_for_a_missing_directory(tmp_path):
    watcher = FileWatcher(tmp_path / "nothing", lambda _p: None)
    assert watcher.start() is False


def test_start_watcher_detects_a_real_change(project):
    """An end-to-end check against the actual watchdog observer."""
    pytest.importorskip("watchdog")
    batches = []
    watcher = start_watcher(project, batches.append)
    if watcher is None:
        pytest.skip("no usable watchdog backend on this platform")
    try:
        (project / "-home-me-app" / "bbbb.jsonl").write_text('{"type":"mode"}\n', encoding="utf-8")
        deadline = time.time() + 10
        while time.time() < deadline and not batches:
            time.sleep(0.1)
    finally:
        watcher.stop()

    assert batches, "the observer saw no events"
    assert any(path.endswith("bbbb.jsonl") for batch in batches for path in batch)


def test_stop_is_safe_to_call_twice(project):
    watcher = FileWatcher(project, lambda _p: None)
    watcher.stop()
    watcher.stop()
