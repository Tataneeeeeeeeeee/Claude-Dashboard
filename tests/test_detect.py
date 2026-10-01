"""Provider detection, as run by the installers."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from agentboard import detect
from agentboard.config import load_config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def _rows(report):
    return {row["id"]: row for row in report["providers"]}


def test_every_builtin_is_reported_with_its_state(fake_claude_home, fake_codex_home):
    report = detect.detect_and_configure()
    rows = _rows(report)
    assert rows["claude"]["data_found"] and rows["claude"]["sessions"] == 2
    assert rows["codex"]["data_found"] and rows["codex"]["sessions"] == 2
    assert not rows["gemini"]["data_found"]
    assert all(row["enabled"] for row in rows.values()), "detection never disables"


def test_environment_only_homes_are_remembered(fake_codex_home, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-elsewhere"))
    report = detect.detect_and_configure()
    providers = load_config(force=True)["providers"]
    assert providers["codex"]["path"] == str(fake_codex_home)
    assert providers["claude"]["path"] == str(tmp_path / "claude-elsewhere")
    assert providers["gemini"]["path"] == str(tmp_path / "no-gemini" / ".gemini")
    assert any("CODEX_HOME" in change for change in report["changes"])


def test_a_configured_path_is_never_overwritten(fake_codex_home):
    from agentboard.config import update_config

    update_config({"providers": {"codex": {"path": "/somewhere/else"}}})
    detect.detect_and_configure()
    assert load_config(force=True)["providers"]["codex"]["path"] == "/somewhere/else"


def test_a_detected_example_tool_is_added_once(tmp_path, monkeypatch):
    home = tmp_path / "continue-home"
    shutil.copytree(FIXTURES / "generic" / "json-tool", home)
    examples = tmp_path / "examples"
    examples.mkdir()
    spec = json.loads((ROOT / "examples" / "providers" / "continue.json").read_text())
    (examples / "continue.json").write_text(json.dumps({**spec, "home": str(home)}))
    (examples / "absent.json").write_text(json.dumps({**spec, "id": "absent", "home": str(tmp_path / "x")}))

    first = detect.detect_and_configure(examples=examples)
    assert _rows(first)["continue"]["sessions"] == 1
    assert "absent" not in _rows(first)
    second = detect.detect_and_configure(examples=examples)
    assert not second["changes"], "running it again changes nothing"
    ids = [s["id"] for s in load_config(force=True)["custom_providers"]]
    assert ids == ["continue"]


def test_a_dry_run_writes_nothing(fake_codex_home, isolated_app_home):
    report = detect.detect_and_configure(write=False)
    assert report["changes"] and not report["written"]
    config = json.loads((isolated_app_home / "config.json").read_text())
    assert config["providers"]["codex"]["path"] == ""


def test_the_report_reads_well():
    text = detect.format_report({
        "providers": [
            {"id": "a", "name": "Alpha", "enabled": True, "installed": True, "data_found": True,
             "sessions": 3, "root": "/a", "reason": ""},
            {"id": "b", "name": "Beta", "enabled": False, "installed": False, "data_found": False,
             "sessions": 0, "root": "/b", "reason": ""},
        ],
        "changes": ["did a thing"],
    })
    assert "+ Alpha" in text and "3 session files" in text
    assert "- Beta" in text and "not found" in text and "(disabled)" in text
    assert "* did a thing" in text


def test_the_cli_flag_prints_the_report(fake_claude_home, isolated_app_home, tmp_path):
    import os

    env = {**os.environ, "AGENTBOARD_HOME": str(isolated_app_home),
           "AGENTBOARD_CLAUDE_HOME": str(fake_claude_home),
           "CODEX_HOME": str(tmp_path / "none"), "GEMINI_CLI_HOME": str(tmp_path / "none")}
    result = subprocess.run([sys.executable, str(ROOT / "run_app.py"), "--detect-providers"],
                            capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "Claude Code" in result.stdout and "Codex CLI" in result.stdout
