"""Tests for window-state persistence and the app entry point.

None of these need a display: the geometry logic and the CLI are separated
from pywebview on purpose.
"""

from __future__ import annotations

import pytest

from agentboard.app import main
from agentboard.config import load_config
from agentboard.window import WindowState, load_window_state, save_window_state


def test_defaults_match_the_requested_geometry():
    state = load_window_state({})
    assert (state.width, state.height) == (1400, 900)
    assert state.min_size == (1024, 640)
    assert state.x is None and state.y is None
    assert state.maximized is False


def test_stored_geometry_is_restored():
    state = load_window_state(
        {"window": {"width": 1600, "height": 1000, "x": 120, "y": 60, "maximized": True}}
    )
    assert (state.width, state.height, state.x, state.y) == (1600, 1000, 120, 60)
    assert state.maximized is True


def test_a_size_below_the_minimum_falls_back_to_the_default():
    state = load_window_state({"window": {"width": 50, "height": 20}})
    assert (state.width, state.height) == (1400, 900)


def test_absurd_or_wrong_typed_values_are_discarded():
    state = load_window_state(
        {"window": {"width": "wide", "height": None, "x": 10 ** 9, "y": True}}
    )
    assert (state.width, state.height) == (1400, 900)
    assert state.x is None
    assert state.y is None


def test_a_small_negative_offset_is_kept_for_multi_monitor_setups():
    state = load_window_state({"window": {"x": -1920, "y": 0}})
    assert state.x == -1920 and state.y == 0


def test_a_non_dict_window_section_does_not_crash():
    assert load_window_state({"window": "corrupt"}).width == 1400


def test_window_state_round_trips_through_the_config_file():
    save_window_state(WindowState(width=1234, height=777, x=10, y=20, maximized=False))
    restored = load_window_state(load_config(force=True))
    assert (restored.width, restored.height, restored.x, restored.y) == (1234, 777, 10, 20)


def test_check_reports_backend_availability(capsys):
    code = main(["--check"])
    output = capsys.readouterr().out
    assert "pywebview" in output
    assert code in (0, 1)               # 1 simply means no backend on this box


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    from agentboard import __version__

    assert __version__ in capsys.readouterr().out


def test_position_is_not_persisted_on_wayland(monkeypatch):
    """Wayland clients cannot know their own position, so none is saved."""
    import importlib

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    import agentboard.app as app_module

    importlib.reload(app_module)
    assert app_module.POSITION_IS_KNOWABLE is False

    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    importlib.reload(app_module)
    assert app_module.POSITION_IS_KNOWABLE is True
