"""What a hand made the robot do, played back as it was: the tape of a hold, a take cut from it between two times of
the robot's clock, and what the mixer is given: the head where it was in the room, its translation included, the
antennas, the base no faster than its motor takes, a seam between two loops."""

from __future__ import annotations

import math
from itertools import pairwise

import numpy as np
import pytest

from robot_agent import animations, config, take
from robot_agent.motion import rot_z

T0 = 1_794_000_000.0  # the robot's clock, epoch seconds
EDGE = 1e-3  # a cut's bounds are taken a millisecond wide: at this epoch a float holds a quarter of a microsecond


def swayed(y: float, yaw: float = 0.0, pitch: float = 0.0) -> np.ndarray:
    """A head pose in the world: turned, and carried `y` metres toward a shoulder."""
    pose = animations.pose(pitch=pitch, yaw=yaw)
    pose[1, 3] = y
    return pose


def taped(seconds: float = 2.0, hz: float = 50.0, body_deg_s: float = 0.0) -> take.Tape:
    """A hand swaying the head from shoulder to shoulder (2 cm, once a second), looking ahead all along, one antenna
    pushed and let go, the base turned at `body_deg_s`."""
    tape = take.Tape()
    for n in range(int(seconds * hz) + 1):
        t = n / hz
        head = swayed(0.02 * math.sin(2 * math.pi * t))
        antennas = animations.NEUTRAL + np.array([0.1 * math.sin(math.pi * t), 0.0])
        body = math.radians(body_deg_s * t)
        tape.add(T0 + t, (rot_z(body) @ head, antennas, body))
    return tape


def world(frame: tuple[np.ndarray, np.ndarray, float]) -> np.ndarray:
    """Where the head is in the room: what the motion thread writes (motion.MotionRunner._step)."""
    head, _, body = frame
    return rot_z(body) @ head


def test_a_take_is_the_tape_between_two_times_of_the_robots_clock() -> None:
    tape = taped(2.0)
    cut = tape.take(T0 + 0.5 - EDGE, T0 + 1.5 + EDGE)
    assert cut is not None and cut.seconds == pytest.approx(1.0, abs=1e-5) and cut.duration == cut.seconds
    assert tape.take(T0 + 0.5, T0 + 0.6) is None  # too short to be a gesture
    assert tape.take(T0 + 5.0, T0 + 9.0) is None  # nothing was taped then
    assert tape.take(T0, T0 + 2.0, loops=99).loops == take.LOOPS_MAX
    tape.clear()
    assert tape.take(T0, T0 + 2.0) is None


def test_the_head_is_played_where_it_was_carried_not_only_how_it_was_turned() -> None:
    """The sway from shoulder to shoulder, looking ahead: no angle changes, the translation is the whole gesture."""
    cut = taped(2.0).take(T0 - EDGE, T0 + 2.0 + EDGE)
    assert cut is not None
    for t in (0.0, 0.25, 0.5, 0.75, 1.25, 1.99):
        head, antennas, body = cut.evaluate(t)
        assert head[1, 3] == pytest.approx(0.02 * math.sin(2 * math.pi * t), abs=2e-4)  # between two samples too
        assert np.allclose(head[:3, :3], np.eye(3), atol=1e-9) and body == 0.0  # it looks ahead all along
        assert antennas[0] == pytest.approx(animations.NEUTRAL[0] + 0.1 * math.sin(math.pi * t), abs=1e-3)
        assert antennas[1] == pytest.approx(animations.NEUTRAL[1])
    exact = cut.evaluate(0.5)  # a sample's own time: the sample
    assert np.allclose(world(exact), swayed(0.02 * math.sin(math.pi)), atol=1e-7)


def test_the_base_turns_as_it_was_turned_at_a_pace_its_motor_takes() -> None:
    slow = taped(2.0, body_deg_s=30.0).take(T0 - EDGE, T0 + 2.0 + EDGE)
    assert slow is not None
    assert math.degrees(slow.evaluate(1.0)[2]) == pytest.approx(30.0, abs=0.1)  # as the hand turned it
    assert np.allclose(world(slow.evaluate(1.0)), rot_z(math.radians(30.0)) @ swayed(0.0), atol=1e-6)
    fast = taped(2.0, body_deg_s=300.0)  # spun by hand: past the pace, and past the limit
    cut = fast.take(T0 - EDGE, T0 + 2.0 + EDGE)
    assert cut is not None
    bodies = [math.degrees(cut.evaluate(n / 50)[2]) for n in range(100)]
    assert max(b - a for a, b in pairwise(bodies)) <= config.BODY_TURN_SPEED_DEG_S / 50 + 1e-4
    assert max(bodies) <= config.BODY_YAW_LIMIT_DEG + 1e-6
    lagging = cut.evaluate(0.2)  # the base is behind the hand: the head is still where it was in the room
    assert np.allclose(world(lagging), rot_z(math.radians(300.0 * 0.2)) @ swayed(0.02 * math.sin(0.4 * math.pi)))


def test_loops_are_joined_by_a_seam_back_to_where_the_take_starts() -> None:
    """From one shoulder to the other: the take does not end where it starts."""
    cut = taped(2.0, hz=100.0).take(T0 + 0.25 - EDGE, T0 + 0.75 + EDGE, loops=3)
    assert cut is not None and cut.seconds == pytest.approx(0.5, abs=1e-5)
    lap = cut.seconds + take.SEAM_S
    assert cut.duration == pytest.approx(3 * 0.5 + 2 * take.SEAM_S, abs=1e-4)
    first, last = cut.evaluate(0.0)[0][1, 3], cut.evaluate(cut.seconds)[0][1, 3]
    assert first == pytest.approx(0.02, abs=1e-6) and last == pytest.approx(-0.02, abs=1e-6)
    middle = cut.evaluate(cut.seconds + take.SEAM_S / 2)[0][1, 3]
    assert middle == pytest.approx(0.0, abs=1e-6)  # half way back, eased
    for loop in (1, 2):
        assert cut.evaluate(loop * lap)[0][1, 3] == pytest.approx(first, abs=1e-6)  # each loop starts as the take
        assert cut.evaluate(loop * lap + 0.2)[0][1, 3] == pytest.approx(cut.evaluate(0.2)[0][1, 3], abs=1e-9)
    assert cut.evaluate(cut.duration)[0][1, 3] == pytest.approx(last, abs=1e-6)  # no seam after the last one
