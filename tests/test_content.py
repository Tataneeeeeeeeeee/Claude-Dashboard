"""Tests for the instruction-file editor, asset browser, todos and backup."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

from claude_dashboard import content
from claude_dashboard.actions import SafetyError
from claude_dashboard.content import (
    backup_projects,
    find_claude_md,
    list_assets,
    list_claude_md_backups,
    read_asset,
    read_todos,
    save_claude_md,
    validate_claude_md_path,
)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A ~/.claude with instructions, assets, todos and a project."""
    home = tmp_path / "dot-claude"
    (home / "projects" / "-home-me-app").mkdir(parents=True)
    (home / "CLAUDE.md").write_text("# Global rules\n", encoding="utf-8")

    (home / "commands").mkdir()
    (home / "commands" / "deploy.md").write_text(
        "---\nname: deploy\ndescription: Ship the current branch\n---\n\nRun the deploy.\n",
        encoding="utf-8",
    )
    (home / "agents").mkdir()
    (home / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews a diff\n---\n\nBody.\n", encoding="utf-8"
    )
    skill_dir = home / "plugins" / "cache" / "market" / "myplugin" / "1.0" / "skills" / "charting"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: charting\ndescription: "Draws charts"\n---\n\n# Charting\n', encoding="utf-8"
    )

    project = tmp_path / "app"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# Project rules\n", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_DASHBOARD_CLAUDE_HOME", str(home))
    return {"home": home, "project": project, "tmp": tmp_path}


# ----------------------------------------------------------- discovery

def test_finds_global_and_project_instruction_files(tree):
    files = find_claude_md([str(tree["project"])])
    scopes = [f["scope"] for f in files]
    assert scopes == ["global", "project"], "global comes first"
    assert files[0]["path"] == str(tree["home"] / "CLAUDE.md")
    assert files[1]["project"] == str(tree["project"])
    assert all(f["size"] > 0 and f["modified"] for f in files)


def test_a_project_that_no_longer_exists_is_skipped(tree):
    files = find_claude_md([str(tree["tmp"] / "gone")])
    assert [f["scope"] for f in files] == ["global"]


def test_no_instruction_files_is_an_empty_list_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_DASHBOARD_CLAUDE_HOME", str(tmp_path / "empty"))
    assert find_claude_md([]) == []


# ------------------------------------------------------------ the guard

def test_the_editor_accepts_only_instruction_files(tree):
    good = validate_claude_md_path(tree["home"] / "CLAUDE.md", [])
    assert good == (tree["home"] / "CLAUDE.md").resolve()

    other = tree["home"] / "settings.json"
    other.write_text("{}", encoding="utf-8")
    with pytest.raises(SafetyError, match="Only CLAUDE"):
        validate_claude_md_path(other, [])


def test_the_editor_refuses_traversal_and_relative_paths(tree):
    with pytest.raises(SafetyError, match=r"\.\."):
        validate_claude_md_path(tree["home"] / ".." / "CLAUDE.md", [])
    with pytest.raises(SafetyError, match="absolute"):
        validate_claude_md_path("CLAUDE.md", [])


def test_the_editor_refuses_a_file_outside_every_known_place(tree):
    stray = tree["tmp"] / "elsewhere" / "CLAUDE.md"
    stray.parent.mkdir()
    stray.write_text("# nope\n", encoding="utf-8")
    with pytest.raises(SafetyError, match="not in ~/.claude"):
        validate_claude_md_path(stray, [str(tree["project"])])


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privileges")
def test_the_editor_refuses_a_symlink(tree):
    victim = tree["tmp"] / "victim.md"
    victim.write_text("keep", encoding="utf-8")
    link = tree["home"] / "CLAUDE.md"
    link.unlink()
    link.symlink_to(victim)
    with pytest.raises(SafetyError, match="symlink"):
        validate_claude_md_path(link, [])
    assert victim.read_text(encoding="utf-8") == "keep"


# -------------------------------------------------------------- saving

def test_saving_takes_a_backup_first(tree):
    target = tree["home"] / "CLAUDE.md"
    original = target.read_text(encoding="utf-8")

    result = save_claude_md(target, "# Replaced\n", [])

    assert target.read_text(encoding="utf-8") == "# Replaced\n"
    assert result["backup"], "a backup path should be reported"
    assert Path(result["backup"]).read_text(encoding="utf-8") == original

    backups = list_claude_md_backups()
    assert len(backups) == 1
    assert backups[0]["original_name"] == "CLAUDE.md"


def test_saving_a_new_file_reports_no_backup(tree):
    target = tree["project"] / "CLAUDE.local.md"
    result = save_claude_md(target, "# New\n", [str(tree["project"])])
    assert result["backup"] is None
    assert target.read_text(encoding="utf-8") == "# New\n"


def test_two_projects_do_not_overwrite_each_others_backups(tree, tmp_path):
    second = tmp_path / "other"
    second.mkdir()
    (second / "CLAUDE.md").write_text("# second\n", encoding="utf-8")

    save_claude_md(tree["project"] / "CLAUDE.md", "a", [str(tree["project"])])
    save_claude_md(second / "CLAUDE.md", "b", [str(second)])

    names = {Path(entry["path"]).name for entry in list_claude_md_backups()}
    assert len(names) == 2, f"backups collided: {names}"


def test_saving_rejects_non_text_content(tree):
    with pytest.raises(SafetyError, match="text"):
        save_claude_md(tree["home"] / "CLAUDE.md", {"not": "text"}, [])


def test_a_failed_save_leaves_no_temporary_file(tree):
    target = tree["home"] / "CLAUDE.md"
    save_claude_md(target, "fine", [])
    leftovers = list(target.parent.glob("*.dashboard-tmp"))
    assert leftovers == []


# -------------------------------------------------------------- assets

def test_assets_are_found_in_user_folders_and_plugins(tree):
    result = list_assets()
    assert result["counts"] == {"command": 1, "agent": 1, "skill": 1}

    by_kind = {a["kind"]: a for a in result["assets"]}
    assert by_kind["command"]["name"] == "deploy"
    assert by_kind["command"]["description"] == "Ship the current branch"
    assert by_kind["command"]["source"] == "user"
    assert by_kind["skill"]["name"] == "charting"
    assert by_kind["skill"]["source"].startswith("plugin:")


def test_an_asset_without_frontmatter_falls_back_to_its_first_line(tree):
    path = tree["home"] / "commands" / "bare.md"
    path.write_text("# Heading\n\nThe first real line.\n", encoding="utf-8")
    assets = {a["name"]: a for a in list_assets()["assets"]}
    assert assets["bare"]["description"] == "The first real line."


def test_quoted_frontmatter_values_are_unquoted(tree):
    path = tree["home"] / "commands" / "quoted.md"
    path.write_text('---\nname: "quoted"\ndescription: \'Has quotes\'\n---\nBody\n', encoding="utf-8")
    assets = {a["name"]: a for a in list_assets()["assets"]}
    assert assets["quoted"]["description"] == "Has quotes"


def test_missing_asset_directories_produce_an_empty_result(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_DASHBOARD_CLAUDE_HOME", str(tmp_path / "nothing"))
    result = list_assets()
    assert result["assets"] == []
    assert result["counts"] == {}
    assert result["searched"], "it should still say where it looked"


def test_reading_an_asset_outside_the_tree_is_refused(tree, tmp_path):
    outsider = tmp_path / "outside.md"
    outsider.write_text("secret", encoding="utf-8")
    with pytest.raises(SafetyError, match="outside"):
        read_asset(outsider)
    with pytest.raises(SafetyError, match=r"\.\."):
        read_asset(tree["home"] / ".." / "x.md")


def test_reading_an_asset_returns_its_content_and_frontmatter(tree):
    path = tree["home"] / "commands" / "deploy.md"
    result = read_asset(path)
    assert "Run the deploy." in result["content"]
    assert result["frontmatter"]["name"] == "deploy"
    assert result["truncated"] is False


def test_a_huge_asset_is_truncated_rather_than_streamed(tree, monkeypatch):
    monkeypatch.setattr(content, "MAX_TEXT_BYTES", 100)
    path = tree["home"] / "commands" / "big.md"
    path.write_text("x" * 5000, encoding="utf-8")
    result = read_asset(path)
    assert result["truncated"] is True
    assert len(result["content"]) == 100


# --------------------------------------------------------------- todos

def test_a_missing_todos_directory_reports_where_it_looked(tree):
    result = read_todos()
    assert result["exists"] is False
    assert result["total"] == 0
    assert result["path"].endswith("todos")


def test_todos_are_parsed_and_linked_to_their_session(tree):
    todos = tree["home"] / "todos"
    todos.mkdir()
    session = "11111111-2222-3333-4444-555555555555"
    (todos / f"{session}-agent-x.json").write_text(json.dumps([
        {"content": "Write the parser", "status": "completed", "id": "1"},
        {"content": "Ship it", "status": "in_progress", "id": "2"},
        {"content": "Rest", "status": "pending", "id": "3"},
    ]), encoding="utf-8")

    result = read_todos({session: "The session title"})
    assert result["exists"] is True and result["total"] == 3
    entry = result["lists"][0]
    assert entry["session_id"] == session
    assert entry["session_title"] == "The session title"
    # In-progress sorts first, completed last.
    assert [i["status"] for i in entry["items"]] == ["in_progress", "pending", "completed"]
    assert entry["counts"] == {"in_progress": 1, "pending": 1, "completed": 1}


@pytest.mark.parametrize("payload,expected", [
    ('[{"content": "a", "status": "pending"}]', 1),
    ('{"todos": [{"task": "b"}]}', 1),
    ('["plain string task"]', 1),
    ('[]', 0),
    ('{"unexpected": true}', 0),
])
def test_todo_shapes_are_all_tolerated(tree, payload, expected):
    todos = tree["home"] / "todos"
    todos.mkdir(exist_ok=True)
    (todos / "list.json").write_text(payload, encoding="utf-8")
    assert read_todos()["total"] == expected


def test_a_corrupt_todo_file_is_reported_not_hidden(tree):
    todos = tree["home"] / "todos"
    todos.mkdir()
    (todos / "broken.json").write_text("{ not json", encoding="utf-8")
    entry = read_todos()["lists"][0]
    assert entry["error"] == "unreadable JSON"
    assert entry["items"] == []


# -------------------------------------------------------------- backup

def test_backup_archives_every_transcript(tree):
    project = tree["home"] / "projects" / "-home-me-app"
    for index in range(3):
        (project / f"{index}.jsonl").write_text('{"type":"mode"}\n' * 20, encoding="utf-8")

    result = backup_projects(tree["tmp"])

    archive = Path(result["path"])
    assert archive.is_file() and archive.suffix == ".zip"
    assert result["files"] == 3
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert len(names) == 3
        assert all(name.startswith("projects/") for name in names)
        assert zf.read(names[0]).decode().startswith('{"type":"mode"}')


def test_backup_accepts_an_explicit_filename(tree):
    (tree["home"] / "projects" / "-home-me-app" / "a.jsonl").write_text("{}", encoding="utf-8")
    result = backup_projects(tree["tmp"] / "my-backup")
    assert Path(result["path"]).name == "my-backup.zip"


def test_backup_leaves_no_partial_file_behind(tree):
    (tree["home"] / "projects" / "-home-me-app" / "a.jsonl").write_text("{}", encoding="utf-8")
    backup_projects(tree["tmp"])
    assert list(tree["tmp"].glob("*.part")) == []


def test_backup_without_a_projects_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_DASHBOARD_CLAUDE_HOME", str(tmp_path / "nothing"))
    with pytest.raises(SafetyError, match="Nothing to back up"):
        backup_projects(tmp_path)
