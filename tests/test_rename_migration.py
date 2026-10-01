"""The rename from Claude Code Dashboard to Agentboard must lose nothing.

Old environment variables keep working, the old data directory is moved
into place on first run, and trash batches recorded under the old path can
still be restored.
"""

from __future__ import annotations

import json
import shutil

from agentboard import actions, paths
from agentboard.actions import move_to_trash, restore_batch


def test_legacy_environment_variables_are_still_honoured(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTBOARD_CLAUDE_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_DASHBOARD_CLAUDE_HOME", str(tmp_path / "old-style"))
    assert paths.claude_home() == tmp_path / "old-style"


def test_the_new_variable_wins_over_the_legacy_one(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_DASHBOARD_CLAUDE_HOME", str(tmp_path / "old"))
    monkeypatch.setenv("AGENTBOARD_CLAUDE_HOME", str(tmp_path / "new"))
    assert paths.claude_home() == tmp_path / "new"


def test_the_legacy_data_directory_is_moved_not_copied(tmp_path):
    legacy = tmp_path / ".claude-dashboard"
    (legacy / "trash").mkdir(parents=True)
    (legacy / "config.json").write_text('{"favorites": ["abc"]}', encoding="utf-8")
    target = tmp_path / ".agentboard"

    assert paths.migrate_legacy_home(target, legacy) is True
    assert not legacy.exists()
    assert json.loads((target / "config.json").read_text())["favorites"] == ["abc"]
    assert (target / "trash").is_dir()


def test_migration_never_overwrites_an_existing_directory(tmp_path):
    legacy = tmp_path / ".claude-dashboard"
    legacy.mkdir()
    (legacy / "config.json").write_text("{}", encoding="utf-8")
    target = tmp_path / ".agentboard"
    target.mkdir()

    assert paths.migrate_legacy_home(target, legacy) is False
    assert (legacy / "config.json").is_file(), "the old data must be left alone"


def test_migration_ignores_a_symlinked_legacy_directory(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    legacy = tmp_path / ".claude-dashboard"
    legacy.symlink_to(real, target_is_directory=True)
    assert paths.migrate_legacy_home(tmp_path / ".agentboard", legacy) is False


def test_a_batch_trashed_before_the_move_can_still_be_restored(tmp_path, monkeypatch):
    home = tmp_path / "dot-claude"
    project = home / "projects" / "-home-me-app"
    project.mkdir(parents=True)
    source = project / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    source.write_text('{"type":"mode"}\n', encoding="utf-8")
    monkeypatch.setenv("AGENTBOARD_CLAUDE_HOME", str(home))

    old_home = tmp_path / "old-app-home"
    monkeypatch.setenv("AGENTBOARD_HOME", str(old_home))
    batch = move_to_trash([source])
    assert str(old_home) in batch.items[0]["stored_path"]

    # The data directory moves; the manifest still names the old location.
    new_home = tmp_path / "new-app-home"
    shutil.move(str(old_home), str(new_home))
    monkeypatch.setenv("AGENTBOARD_HOME", str(new_home))

    result = restore_batch(batch.batch_id)
    assert result["restored"] == [source.stem]
    assert source.is_file()
    assert actions.list_trash() == []
