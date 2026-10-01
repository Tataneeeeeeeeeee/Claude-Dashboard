"""Tests for trash, restore, purge and the terminal launcher."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentboard import actions
from agentboard.actions import (
    SafetyError,
    build_terminal_commands,
    delete_batch_permanently,
    list_trash,
    move_to_trash,
    purge_trash,
    restore_batch,
    resume_command_string,
)
from agentboard.paths import trash_dir


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A projects tree with three transcripts to move around."""
    home = tmp_path / "dot-claude"
    project = home / "projects" / "-home-me-app"
    project.mkdir(parents=True)
    files = []
    for index in range(3):
        path = project / f"{index}aaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        path.write_text('{"type":"mode"}\n' * (index + 1), encoding="utf-8")
        files.append(path)
    monkeypatch.setenv("AGENTBOARD_CLAUDE_HOME", str(home))
    return {"home": home, "project": project, "files": files}


# ---------------------------------------------------------------- deletion

def test_deleting_moves_rather_than_unlinks(tree):
    source = tree["files"][0]
    original_bytes = source.read_bytes()

    batch = move_to_trash([source])

    assert not source.exists(), "the transcript should have left the projects tree"
    assert len(batch.items) == 1
    stored = Path(batch.items[0]["stored_path"])
    assert stored.is_file()
    assert stored.read_bytes() == original_bytes, "content must survive intact"
    assert batch.total_bytes == len(original_bytes)


def test_a_batch_records_enough_to_restore_and_to_display(tree):
    batch = move_to_trash(
        [tree["files"][0]],
        metadata={tree["files"][0].stem: {"title": "A session", "project_path": "/home/me/app"}},
    )
    item = batch.items[0]
    assert item["original_path"] == str(tree["files"][0])
    assert item["project_dir"] == "-home-me-app"
    assert item["title"] == "A session"
    assert item["project_path"] == "/home/me/app"
    assert item["size"] > 0


def test_bulk_deletion_keeps_everything_in_one_batch(tree):
    batch = move_to_trash(tree["files"])
    assert len(batch.items) == 3
    assert all(not f.exists() for f in tree["files"])
    assert len(list_trash()) == 1


def test_duplicate_paths_are_collapsed(tree):
    source = tree["files"][0]
    batch = move_to_trash([source, source, str(source)])
    assert len(batch.items) == 1


def test_deleting_nothing_is_an_error(tree):
    with pytest.raises(SafetyError, match="Nothing selected"):
        move_to_trash([])


def test_one_bad_path_aborts_the_whole_batch(tree, tmp_path):
    """Validation happens up front, so nothing moves if anything is unsafe."""
    outsider = tmp_path / "outside.jsonl"
    outsider.write_text("{}", encoding="utf-8")

    with pytest.raises(SafetyError):
        move_to_trash([tree["files"][0], outsider])

    assert all(f.exists() for f in tree["files"]), "no file should have moved"
    assert outsider.exists()
    assert list_trash() == []


def test_a_failure_midway_rolls_back(tree, monkeypatch):
    """If a move fails partway, the files already moved are put back."""
    calls = {"n": 0}
    real_move = actions.shutil.move

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_move(src, dst)

    monkeypatch.setattr(actions.shutil, "move", flaky)
    with pytest.raises(SafetyError, match="rolled back"):
        move_to_trash(tree["files"])

    assert all(f.exists() for f in tree["files"]), "rollback should restore everything"
    assert list_trash() == []


# ----------------------------------------------------------------- restore

def test_restore_puts_the_file_back(tree):
    source = tree["files"][0]
    content = source.read_bytes()
    batch = move_to_trash([source])

    result = restore_batch(batch.batch_id)

    assert result["restored"] == [source.stem]
    assert result["skipped"] == []
    assert source.is_file() and source.read_bytes() == content
    assert list_trash() == [], "an emptied batch should disappear"


def test_restoring_a_subset_keeps_the_rest_in_the_trash(tree):
    batch = move_to_trash(tree["files"])
    wanted = tree["files"][1].stem

    result = restore_batch(batch.batch_id, [wanted])

    assert result["restored"] == [wanted]
    assert tree["files"][1].exists()
    assert not tree["files"][0].exists()
    remaining = list_trash()
    assert len(remaining) == 1 and len(remaining[0].items) == 2


def test_restore_refuses_to_overwrite_an_existing_file(tree):
    source = tree["files"][0]
    batch = move_to_trash([source])
    source.write_text("something new", encoding="utf-8")

    result = restore_batch(batch.batch_id)

    assert result["restored"] == []
    assert result["skipped"][0]["reason"] == "a file already exists at the original path"
    assert source.read_text(encoding="utf-8") == "something new"


def test_a_tampered_manifest_cannot_write_outside_the_tree(tree, tmp_path):
    """The restore path is validated again, not trusted from the manifest."""
    batch = move_to_trash([tree["files"][0]])
    manifest_path = trash_dir() / batch.batch_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"][0]["original_path"] = str(tmp_path / "pwned.jsonl")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = restore_batch(batch.batch_id)

    assert result["restored"] == []
    assert "outside the projects tree" in result["skipped"][0]["reason"]
    assert not (tmp_path / "pwned.jsonl").exists()


def test_restoring_an_unknown_batch_is_an_error(tree):
    with pytest.raises(SafetyError, match="valid trash batch id"):
        restore_batch("../../etc")
    with pytest.raises(SafetyError, match="No manifest"):
        restore_batch("20260101-000000-abcdef")


# ------------------------------------------------------------------- purge

def test_purge_removes_only_old_batches(tree):
    fresh = move_to_trash([tree["files"][0]])
    old = move_to_trash([tree["files"][1]])

    # Backdate one batch by rewriting its manifest.
    stale = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    manifest_path = trash_dir() / old.batch_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["deleted_at"] = stale
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = purge_trash(days=30)

    assert result["purged"] == [old.batch_id]
    remaining = [b.batch_id for b in list_trash()]
    assert remaining == [fresh.batch_id]


def test_purge_can_be_disabled(tree):
    move_to_trash([tree["files"][0]])
    result = purge_trash(days=0)
    assert result["disabled"] is True
    assert len(list_trash()) == 1


def test_permanent_deletion_only_touches_the_trash(tree):
    batch = move_to_trash([tree["files"][0]])
    result = delete_batch_permanently(batch.batch_id)
    assert result["removed_files"] == 1
    assert list_trash() == []
    with pytest.raises(SafetyError):
        delete_batch_permanently(batch.batch_id)


def test_a_batch_with_an_unreadable_manifest_is_still_listed(tree):
    batch = move_to_trash([tree["files"][0]])
    (trash_dir() / batch.batch_id / "manifest.json").write_text("{ broken", encoding="utf-8")
    listed = list_trash()
    assert len(listed) == 1
    assert listed[0].items[0]["title"] == "(manifest lost)"


def test_listing_an_absent_trash_directory_is_empty(tree):
    assert list_trash() == []


# ---------------------------------------------------------------- terminal

def test_resume_command_uses_the_configured_template():
    assert resume_command_string("abc-123") == "claude --resume abc-123"
    config = {"terminal": {"resume_command": "claude --resume {session_id} --verbose"}}
    assert resume_command_string("abc-123", config).endswith("--verbose")


@pytest.mark.parametrize("bad", ["", "../etc", "a b", "x;rm -rf /", "$(whoami)", "a" * 200])
def test_an_unsafe_session_id_is_refused(bad):
    with pytest.raises(SafetyError, match="unsafe session id"):
        resume_command_string(bad)


def test_windows_prefers_windows_terminal_then_falls_back():
    commands = build_terminal_commands("C:\\work\\app", "abc", {}, platform="win32")
    assert commands[0][0] == "wt.exe"
    assert "-d" in commands[0] and "C:\\work\\app" in commands[0]
    assert commands[1][0] == "cmd.exe"
    assert any("start" in part for part in commands[1])


def test_macos_uses_applescript_for_terminal_then_iterm():
    commands = build_terminal_commands("/Users/me/app", "abc", {}, platform="darwin")
    assert all(c[0] == "osascript" for c in commands)
    assert 'tell application "Terminal"' in commands[0][2]
    assert 'tell application "iTerm"' in commands[1][2]


def test_linux_tries_the_documented_order_first():
    commands = build_terminal_commands("/home/me/app", "abc", {}, platform="linux")
    programs = [c[0] for c in commands]
    assert programs[:3] == ["x-terminal-emulator", "gnome-terminal", "konsole"]


def test_a_configured_terminal_overrides_the_defaults():
    config = {"terminal": {"linux": [["myterm", "--cd", "{cwd}", "-e", "{command}"]],
                           "resume_command": "claude --resume {session_id}"}}
    commands = build_terminal_commands("/home/me/app", "s1", config, platform="linux")
    assert commands == [["myterm", "--cd", "/home/me/app", "-e", "claude --resume s1"]]


def test_a_single_configured_argv_is_accepted_without_nesting():
    config = {"terminal": {"linux": ["myterm", "{cwd}"]}}
    commands = build_terminal_commands("/home/me/app", "s1", config, platform="linux")
    assert commands == [["myterm", "/home/me/app"]]


def test_directories_with_spaces_and_quotes_stay_one_argument():
    """Nothing goes through a shell, so odd names cannot split or inject."""
    nasty = "/home/me/my app; rm -rf ~/'x'"
    commands = build_terminal_commands(nasty, "abc", {}, platform="linux")
    gnome = next(c for c in commands if c[0] == "gnome-terminal")
    assert nasty in gnome, "the path must be passed as a single argv element"
    # In the sh -c payload it must be quoted, not interpolated bare.
    payload = gnome[-1]
    assert "'/home/me/my app; rm -rf ~/'\\''x'\\''" in payload


def test_launching_into_a_missing_folder_is_refused(tmp_path):
    with pytest.raises(SafetyError, match="no longer exists"):
        actions.launch_terminal(str(tmp_path / "gone"), "abc")


def test_launch_reports_what_it_tried_when_nothing_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(actions.shutil, "which", lambda _name: None)
    with pytest.raises(SafetyError, match="Could not open a terminal"):
        actions.launch_terminal(str(tmp_path), "abc")


def test_launch_uses_the_first_program_that_exists(tmp_path, monkeypatch):
    started = {}

    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/" + name
                        if name == "konsole" else None)

    def fake_popen(argv, **kwargs):
        started["argv"] = argv
        started["cwd"] = kwargs.get("cwd")
        return object()

    monkeypatch.setattr(actions.subprocess, "Popen", fake_popen)
    result = actions.launch_terminal(str(tmp_path), "abc-123")

    assert result["launched"] is True
    assert result["program"] == "konsole"
    assert started["cwd"] == str(tmp_path)
    assert "claude --resume abc-123" in " ".join(started["argv"])
    # The two programs ahead of konsole were reported as unavailable.
    assert [a["program"] for a in result["attempts"]] == [
        "x-terminal-emulator", "gnome-terminal"
    ]


# -------------------------------------------------------------- open paths

def test_open_folder_refuses_a_missing_directory(tmp_path):
    with pytest.raises(SafetyError, match="no longer exists"):
        actions.open_folder(str(tmp_path / "gone"))


def test_open_in_editor_reports_a_missing_editor(tmp_path, monkeypatch):
    monkeypatch.setattr(actions.shutil, "which", lambda _n: None)
    with pytest.raises(SafetyError, match="not on your PATH"):
        actions.open_in_editor(str(tmp_path), {"editor_command": "code"})


def test_open_in_editor_starts_the_configured_command(tmp_path, monkeypatch):
    started = {}
    monkeypatch.setattr(actions.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(actions.subprocess, "Popen",
                        lambda argv, **k: started.setdefault("argv", argv))
    result = actions.open_in_editor(str(tmp_path), {"editor_command": "codium --new-window"})
    assert result["program"] == "codium"
    assert started["argv"] == ["codium", "--new-window", str(tmp_path)]
