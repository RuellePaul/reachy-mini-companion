"""The lent body's pose as a datagram (robot_agent/poses.py): what the robot packs, the Mac unpacks."""

from __future__ import annotations

import math

import pytest

from robot_agent import poses

JOINTS = [0.5, -0.1, 0.2, -0.3, 0.4, -0.5, 0.6]
HEAD = [float(n) / 10 for n in range(16)]
ANTENNAS = [-0.1745, 0.1745]


def test_a_measure_comes_out_as_it_went_in() -> None:
    data = poses.pack('key', 1794751821.8925, JOINTS, HEAD, ANTENNAS)
    assert len(data) == poses.SIZE == 116  # one small datagram, 30 a second
    at, joints, head, antennas = poses.unpack('key', data) or (0.0, [], [], [])
    assert at == 1794751821.8925  # the robot's clock, whole: a replay is cut by it
    assert joints == pytest.approx(JOINTS, abs=1e-6) and len(joints) == 7
    assert head == pytest.approx(HEAD, abs=1e-6) and len(head) == 16
    assert antennas == pytest.approx(ANTENNAS, abs=1e-6)


def test_a_datagram_of_another_key_or_cut_short_is_no_measure() -> None:
    data = poses.pack('key', 12.0, JOINTS, HEAD, ANTENNAS)
    assert poses.unpack('other', data) is None
    assert poses.unpack('key', data[:-1]) is None and poses.unpack('key', data + b'\0') is None
    assert poses.unpack('key', b'') is None and poses.unpack('key', b'x' * poses.SIZE) is None
    flipped = data[:-1] + bytes([data[-1] ^ 1])  # one bit of the measure changed on the way
    assert poses.unpack('key', flipped) is None


def test_no_key_still_tells_a_measure_from_a_stray_datagram() -> None:
    data = poses.pack('', math.pi, JOINTS, HEAD, ANTENNAS)
    assert poses.unpack('', data) is not None and poses.unpack('', b'x' * poses.SIZE) is None
