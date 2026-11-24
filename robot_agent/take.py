"""What a hand made the robot do, kept to be played back as it was (the bridge's `replay`). While an app holds the
body, the manette tapes the measured pose ~50 times a second; a take is a stretch of that tape between two times of
the robot's own clock (the pads carry them), played by the one motion thread once the body is the robot's again: the
head where it was in the room, its translation included, the antennas, and the base no faster than its motor takes.
Pollen's marionette records and replays the same three readings."""

from __future__ import annotations

import bisect
import math
from collections import deque
from typing import Any

import numpy as np

from robot_agent import animations, config
from robot_agent.motion import blend_pose, rot_z

TAPE_HZ = 50.0  # the daemon's own loop: reading faster tells nothing more
TAPE_S = 120.0  # what the tape keeps of a hold
MIN_S, MAX_S = 0.3, 30.0  # a take's length
LOOPS_MAX = 8
LEAD_IN_S = 0.8  # to the take's first pose from wherever the robot is: longer than the mixer's blend, the take waits
SEAM_S = 0.4  # between two loops, from the last pose back to the first

Sample = tuple[float, np.ndarray, np.ndarray, float]  # the robot's clock (s), head in the world, antennas, base (rad)


def paced(bodies: list[float], at: list[float]) -> list[float]:
    """The base's yaw as it was turned, within its limits and no faster than its usual pace: a fast turn overloads
    its motor. The head stays where it was in the room all the same."""
    limit, pace = math.radians(config.BODY_YAW_LIMIT_DEG), math.radians(config.BODY_TURN_SPEED_DEG_S)
    out = [max(-limit, min(limit, bodies[0]))]
    for k in range(1, len(bodies)):
        step = pace * (at[k] - at[k - 1])
        wanted = max(-limit, min(limit, bodies[k]))
        out.append(out[-1] + max(-step, min(step, wanted - out[-1])))
    return out


class Take:
    """A move for the mixer (`duration`, `evaluate`): `seconds` of tape, `loops` times, a seam between two."""

    turns = True  # its third value, the base's yaw, is played too (motion.Mixer.sample)

    def __init__(self, samples: list[Sample], loops: int = 1) -> None:
        self._at = [at - samples[0][0] for at, *_ in samples]
        self._heads = [head for _, head, _, _ in samples]
        self._antennas = [antennas for _, _, antennas, _ in samples]
        self._bodies = paced([body for *_, body in samples], self._at)
        self.seconds, self.loops = self._at[-1], loops
        self.duration = loops * self.seconds + (loops - 1) * SEAM_S

    def evaluate(self, t: float) -> tuple[np.ndarray, np.ndarray, float]:
        """(head in the base's frame, antennas, base yaw) at `t`: the tape as it was, then a seam back to its start."""
        lap = self.seconds + SEAM_S
        u = t - min(self.loops - 1, int(t // lap)) * lap
        if u <= self.seconds:
            k = max(0, min(len(self._at) - 2, bisect.bisect_right(self._at, u) - 1))
            span = self._at[k + 1] - self._at[k]
            head, antennas, body = self._between(k, k + 1, (u - self._at[k]) / span if span > 0 else 0.0)
        else:
            head, antennas, body = self._between(-1, 0, animations.smoothstep((u - self.seconds) / SEAM_S))
        return rot_z(-body) @ head, antennas, body

    def _between(self, a: int, b: int, alpha: float) -> tuple[np.ndarray, np.ndarray, float]:
        alpha = max(0.0, min(1.0, alpha))
        return (
            blend_pose(self._heads[a], self._heads[b], alpha),
            (1.0 - alpha) * self._antennas[a] + alpha * self._antennas[b],
            (1.0 - alpha) * self._bodies[a] + alpha * self._bodies[b],
        )


class Tape:
    """The measured poses of the hold under way, or of the last one."""

    def __init__(self) -> None:
        self._samples: deque[Sample] = deque(maxlen=int(TAPE_S * TAPE_HZ))

    def clear(self) -> None:
        self._samples.clear()

    def add(self, at: float, pose: Any) -> None:
        """A pose as the robot reads it (adapters.robot.read_pose), at `at` on its clock (time.time())."""
        head, antennas, body = pose
        self._samples.append((at, np.array(head, dtype=np.float64), np.array(antennas, dtype=np.float64), float(body)))

    def take(self, start: float, end: float, loops: int = 1) -> Take | None:
        """What was taped between `start` and `end`, as a move; None when the tape holds too little of it."""
        samples = [sample for sample in self._samples if start <= sample[0] <= end]
        if len(samples) < 2 or not MIN_S <= samples[-1][0] - samples[0][0] <= MAX_S:
            return None
        return Take(samples, max(1, min(LOOPS_MAX, loops)))
