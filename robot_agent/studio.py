"""Moves the dashboard's studio asks the robot to play at once (« Essayer sur le robot »), awake or asleep.

A try changes no setting: an animation variant plays as a move of its own for a few seconds, then the robot
blends back to what it was doing (or to the sleep pose). Everything goes through the one motion thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robot_agent import animation_names, animations


@dataclass(frozen=True)
class AnimationMove:
    """A mode's variant played for `duration` seconds as a move (contracts.TryMove, kind 'animation')."""

    mode: str
    key: str
    tuning: animations.Tuning
    duration: float

    def evaluate(self, t: float) -> animations.Pose:
        return animations.animate(self.mode, self.key, self.tuning)(t)


def build(spec: dict[str, Any], emotions: Any) -> Any | None:
    """The move a contracts.TryMove names, or None. Blocking (an emotion may be read from disk): in a thread."""
    kind, name = spec.get('kind'), str(spec.get('name', ''))
    if kind == 'gesture':
        return animations.GESTURES.get(name)
    if kind == 'emotion':
        return emotions.get(name)
    mode = spec.get('mode')
    if kind != 'animation' or mode not in animation_names.MODES or name not in animations.VARIANTS[mode]:
        return None
    tuning = spec.get('tuning') if isinstance(spec.get('tuning'), dict) else {}
    try:
        factors = animations.Tuning(float(tuning.get('amplitude', 1.0)), float(tuning.get('speed', 1.0)))
        seconds = float(spec.get('seconds', 6.0))
    except (TypeError, ValueError):
        return None
    return AnimationMove(mode, name, factors, min(max(seconds, 1.0), 20.0))
