"""A recorded move (Pollen's emotions) played from its JSON frames, interpolated as reachy_mini's RecordedMove does
(bisect, antennas and body yaw lerped, the head slerped), but in our code: the robot plays it and the Mac's 3D
preview samples it from the same JSON, so both show the same frames."""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any

import numpy as np

from robot_agent.motion import blend_pose


class Recorded:
    def __init__(self, data: dict[str, Any], sound_path: Path | str | None = None) -> None:
        self.times = [float(t) for t in data['time']]
        self.frames = data['set_target_data']
        self.description = str(data.get('description', ''))
        self.sound_path = sound_path

    @property
    def duration(self) -> float:
        return self.times[-1] - self.times[0]  # reachy_mini: len(frames) * (last - first) / len(frames)

    def evaluate(self, t: float) -> tuple[np.ndarray, np.ndarray, float]:
        """Head pose (4x4), antennas [right, left] and body yaw at `t`, held at the ends."""
        index = bisect.bisect_right(self.times, t)
        prev = max(index - 1, 0)
        nxt = index if index < len(self.times) else prev
        span = self.times[nxt] - self.times[prev]
        alpha = 0.0 if span == 0 else (t - self.times[prev]) / span
        a, b = self.frames[prev], self.frames[nxt]
        head = blend_pose(np.asarray(a['head'], dtype=np.float64), np.asarray(b['head'], dtype=np.float64), alpha)
        antennas = (1.0 - alpha) * np.asarray(a['antennas'], dtype=np.float64) + alpha * np.asarray(b['antennas'])
        body = (1.0 - alpha) * float(a.get('body_yaw', 0.0)) + alpha * float(b.get('body_yaw', 0.0))
        return head, antennas, body
