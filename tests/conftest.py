"""Shared fixtures.

Every test runs against a throwaway ``AGENTBOARD_HOME`` so the suite
never reads or writes the real ``~/.agentboard``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    """Directory holding the sample transcripts."""
    return FIXTURES


@pytest.fixture(autouse=True)
def isolated_app_home(tmp_path, monkeypatch):
    """Point the app's config and cache at a temporary directory, and every
    provider's data directory somewhere empty, so the suite never reads the
    real history of any tool installed on the machine."""
    home = tmp_path / "app-home"
    home.mkdir()
    monkeypatch.setenv("AGENTBOARD_HOME", str(home))
    monkeypatch.delenv("CLAUDE_DASHBOARD_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_DASHBOARD_CLAUDE_HOME", raising=False)
    monkeypatch.setenv("AGENTBOARD_CLAUDE_HOME", str(tmp_path / "no-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "no-gemini"))
    from agentboard import config
    from agentboard.providers import registry

    monkeypatch.setattr(config, "_CACHED", None, raising=False)
    monkeypatch.setattr(registry, "_REGISTRY", None, raising=False)
    yield home
    monkeypatch.setattr(config, "_CACHED", None, raising=False)
    monkeypatch.setattr(registry, "_REGISTRY", None, raising=False)


@pytest.fixture
def fake_claude_home(tmp_path, monkeypatch) -> Path:
    """A ``~/.claude`` replica containing the fixture transcripts."""
    home = tmp_path / "dot-claude"
    project = home / "projects" / "-home-tester-demo"
    project.mkdir(parents=True)
    shutil.copy(FIXTURES / "basic.jsonl", project / "11111111-2222-3333-4444-555555555555.jsonl")
    shutil.copy(FIXTURES / "messy.jsonl", project / "99999999-8888-7777-6666-555555555555.jsonl")
    (home / "settings.json").write_text('{"model": "opus[1m]", "theme": "dark"}', encoding="utf-8")
    monkeypatch.setenv("AGENTBOARD_CLAUDE_HOME", str(home))
    return home


@pytest.fixture
def fake_codex_home(tmp_path, monkeypatch) -> Path:
    """A ``~/.codex`` replica holding the fixture rollouts in dated folders."""
    home = tmp_path / "dot-codex"
    for source in sorted((FIXTURES / "codex").glob("rollout-*.jsonl")):
        year, month, day = source.name[8:12], source.name[13:15], source.name[16:18]
        folder = home / "sessions" / year / month / day
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, folder / source.name)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def fake_gemini_home(tmp_path, monkeypatch) -> Path:
    """A ``~/.gemini`` replica: ``$GEMINI_CLI_HOME/.gemini/tmp/<hash>/chats``."""
    base = tmp_path / "gemini-user"
    shutil.copytree(FIXTURES / "gemini", base / ".gemini" / "tmp")
    monkeypatch.setenv("GEMINI_CLI_HOME", str(base))
    return base / ".gemini"
