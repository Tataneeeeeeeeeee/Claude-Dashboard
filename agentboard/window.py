"""Native window state: size, position and maximised flag across launches.

Kept separate from :mod:`agentboard.app` so it can be unit tested
without a display attached.  Geometry is validated before being restored: a
window remembered on a monitor that is no longer connected must not reopen
off-screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from .config import load_config, update_config

__all__ = ["WindowState", "load_window_state", "save_window_state"]

#: Hard floor for a restored window, independent of the configured minimum.
ABSOLUTE_MIN = (640, 480)

#: Anything larger than this is treated as a stale or corrupt value.
ABSOLUTE_MAX = (20000, 20000)


@dataclass
class WindowState:
    """Geometry handed to pywebview when the window is created."""

    width: int = 1400
    height: int = 900
    x: int | None = None
    y: int | None = None
    maximized: bool = False
    min_size: Tuple[int, int] = (1024, 640)

    def to_patch(self) -> Dict[str, Any]:
        """Shape this state as a config patch for :func:`save_window_state`."""
        return {
            "window": {
                "width": self.width,
                "height": self.height,
                "x": self.x,
                "y": self.y,
                "maximized": self.maximized,
                "min_width": self.min_size[0],
                "min_height": self.min_size[1],
            }
        }


def _clamp(value: Any, low: int, high: int, fallback: int) -> int:
    """Coerce *value* to an int inside ``[low, high]``, else *fallback*."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return fallback
    number = int(value)
    if number < low or number > high:
        return fallback
    return number


def load_window_state(config: Dict[str, Any] | None = None) -> WindowState:
    """Read the stored geometry, discarding anything implausible.

    Off-screen or negative coordinates are dropped rather than corrected, so
    the window manager places the window itself - which is the right default
    when the monitor layout has changed since the last run.
    """
    settings = (config or load_config()).get("window", {})
    if not isinstance(settings, dict):
        settings = {}

    min_width = _clamp(settings.get("min_width"), ABSOLUTE_MIN[0], ABSOLUTE_MAX[0], 1024)
    min_height = _clamp(settings.get("min_height"), ABSOLUTE_MIN[1], ABSOLUTE_MAX[1], 640)
    width = _clamp(settings.get("width"), min_width, ABSOLUTE_MAX[0], 1400)
    height = _clamp(settings.get("height"), min_height, ABSOLUTE_MAX[1], 900)

    def coordinate(key: str) -> int | None:
        """Accept a saved coordinate only if it is a sane on-screen value."""
        value = settings.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        number = int(value)
        # A small negative offset is legitimate on multi-monitor setups.
        if number < -ABSOLUTE_MAX[0] or number > ABSOLUTE_MAX[0]:
            return None
        return number

    return WindowState(
        width=width,
        height=height,
        x=coordinate("x"),
        y=coordinate("y"),
        maximized=bool(settings.get("maximized")),
        min_size=(min_width, min_height),
    )


def save_window_state(state: WindowState) -> None:
    """Persist geometry to the config file, ignoring write failures."""
    try:
        update_config(state.to_patch())
    except OSError:
        pass
