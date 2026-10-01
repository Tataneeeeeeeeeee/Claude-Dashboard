"""Adversarial tests for the deletion guard.

These are the tests that matter most in the whole suite: everything else
only reads, while this code moves files. Each case is an attempt to reach
something outside ``~/.claude/projects``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agentboard.actions import (
    PROTECTED_NAMES,
    SafetyError,
    validate_transcript_path,
)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A realistic ``~/.claude`` plus an outsider file to aim at."""
    home = tmp_path / "dot-claude"
    projects = home / "projects" / "-home-me-app"
    projects.mkdir(parents=True)
    transcript = projects / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    transcript.write_text('{"type":"mode"}\n', encoding="utf-8")

    (home / "settings.json").write_text("{}", encoding="utf-8")
    (home / "CLAUDE.md").write_text("# global", encoding="utf-8")

    outside = tmp_path / "precious.txt"
    outside.write_text("do not touch", encoding="utf-8")

    monkeypatch.setenv("AGENTBOARD_CLAUDE_HOME", str(home))
    return {
        "home": home,
        "root": home / "projects",
        "project": projects,
        "transcript": transcript,
        "outside": outside,
        "tmp": tmp_path,
    }


# ------------------------------------------------------------- happy path

def test_a_real_transcript_is_accepted(tree):
    resolved = validate_transcript_path(tree["transcript"])
    assert resolved == tree["transcript"].resolve()


def test_a_relative_path_resolves_against_the_projects_root(tree):
    relative = Path("-home-me-app") / tree["transcript"].name
    assert validate_transcript_path(relative) == tree["transcript"].resolve()


# --------------------------------------------------------------- traversal

def test_dot_dot_is_refused_outright(tree):
    attack = tree["project"] / ".." / ".." / "settings.json"
    with pytest.raises(SafetyError, match=r"\.\."):
        validate_transcript_path(attack)


def test_deep_traversal_out_of_the_tree_is_refused(tree):
    attack = tree["project"] / ".." / ".." / ".." / "precious.txt"
    with pytest.raises(SafetyError):
        validate_transcript_path(attack)


def test_an_absolute_path_outside_the_tree_is_refused(tree):
    with pytest.raises(SafetyError, match="outside"):
        validate_transcript_path(tree["outside"])


@pytest.mark.parametrize("target", ["/etc/passwd", "/etc/hosts"])
def test_system_files_are_refused(tree, target):
    if not Path(target).exists():
        pytest.skip(f"{target} not present")
    with pytest.raises(SafetyError):
        validate_transcript_path(target)


# ---------------------------------------------------------------- symlinks

@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privileges on Windows")
def test_a_symlink_pointing_outside_is_refused(tree):
    link = tree["project"] / "escape.jsonl"
    link.symlink_to(tree["outside"])
    with pytest.raises(SafetyError, match="symlink"):
        validate_transcript_path(link)
    assert tree["outside"].exists(), "the target must be untouched"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privileges on Windows")
def test_a_symlink_pointing_inside_is_still_refused(tree):
    """Links are rejected on principle, not only when they escape."""
    link = tree["project"] / "alias.jsonl"
    link.symlink_to(tree["transcript"])
    with pytest.raises(SafetyError, match="symlink"):
        validate_transcript_path(link)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privileges on Windows")
def test_a_symlinked_project_directory_is_refused(tree):
    """A linked directory must not redirect the move out of the tree."""
    elsewhere = tree["tmp"] / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "victim.jsonl"
    victim.write_text("{}", encoding="utf-8")

    linked = tree["root"] / "-linked-project"
    linked.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(SafetyError, match="symlink"):
        validate_transcript_path(linked / "victim.jsonl")
    assert victim.exists()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="hardlink behaviour differs")
def test_a_hard_link_inside_the_tree_is_accepted(tree):
    """A hard link is indistinguishable from a file and stays in the tree."""
    link = tree["project"] / "copy.jsonl"
    os.link(tree["transcript"], link)
    assert validate_transcript_path(link) == link.resolve()


# ------------------------------------------------------------ shape checks

def test_a_directory_is_refused(tree):
    with pytest.raises(SafetyError):
        validate_transcript_path(tree["project"])


def test_a_missing_file_is_refused(tree):
    with pytest.raises(SafetyError, match="No such transcript"):
        validate_transcript_path(tree["project"] / "ghost.jsonl")


def test_a_non_jsonl_file_is_refused(tree):
    other = tree["project"] / "notes.txt"
    other.write_text("hello", encoding="utf-8")
    with pytest.raises(SafetyError, match=r"\.jsonl"):
        validate_transcript_path(other)


def test_a_transcript_at_the_wrong_depth_is_refused(tree):
    """Transcripts live at <projects>/<project>/<id>.jsonl, nowhere else."""
    stray = tree["root"] / "loose.jsonl"
    stray.write_text("{}", encoding="utf-8")
    with pytest.raises(SafetyError, match="Expected"):
        validate_transcript_path(stray)

    nested = tree["project"] / "sub" / "deep.jsonl"
    nested.parent.mkdir()
    nested.write_text("{}", encoding="utf-8")
    with pytest.raises(SafetyError, match="Expected"):
        validate_transcript_path(nested)


@pytest.mark.parametrize("name", sorted(PROTECTED_NAMES))
def test_protected_names_are_refused_even_inside_the_tree(tree, name):
    planted = tree["project"] / name
    planted.write_text("{}", encoding="utf-8")
    with pytest.raises(SafetyError):
        validate_transcript_path(planted)


def test_the_config_files_cannot_be_reached(tree):
    for target in (tree["home"] / "settings.json", tree["home"] / "CLAUDE.md"):
        with pytest.raises(SafetyError):
            validate_transcript_path(target)
        assert target.exists()


# ------------------------------------------------------------------ oddity

def test_an_empty_or_nonsense_path_is_refused(tree):
    for value in ("", ".", "..", "~"):
        with pytest.raises(SafetyError):
            validate_transcript_path(value)


def test_a_missing_projects_directory_is_reported_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTBOARD_CLAUDE_HOME", str(tmp_path / "nothing"))
    with pytest.raises(SafetyError, match="unreadable"):
        validate_transcript_path(tmp_path / "x.jsonl")
