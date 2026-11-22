"""Recorded moves: our interpolation is reachy_mini's, so the robot and the 3D preview play the same frames."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from robot_agent import animations
from robot_agent.recorded import Recorded

# reachy_mini's frames for _move(), kept here: CI installs no reachy_mini, whose dependencies are the robot's (its motor
# controller, GStreamer, PyGObject).
REFERENCE = Path(__file__).with_name('recorded_reference.json')


def _move() -> dict:
    frames = []
    for n, t in enumerate(np.arange(0.0, 1.0, 0.02)):
        head = animations.pose(pitch=10 * np.sin(4 * t), yaw=20 * t, roll=-5 * t, z=0.004 * t)
        frames.append({'head': head.tolist(), 'antennas': [0.1 * n, -0.05 * n], 'body_yaw': 0.01 * n})
    return {'description': 'test', 'time': list(np.arange(0.0, 1.0, 0.02)), 'set_target_data': frames}


def test_frames_are_interpolated_and_held_at_the_ends() -> None:
    move = Recorded(_move())
    assert move.duration == pytest.approx(0.98)
    head, antennas, body = move.evaluate(0.005)  # a quarter of the way from the first frame to the second
    assert antennas == pytest.approx([0.025, -0.0125])
    assert body == pytest.approx(0.0025)
    assert np.allclose(move.evaluate(5.0)[0], np.asarray(_move()['set_target_data'][-1]['head']))
    rotation = head[:3, :3]
    assert np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-9)


def test_the_head_is_slerped_between_two_frames() -> None:
    """A quarter of the way, not halfway: there a frame played backwards, or always at its middle, looks right."""
    data = _move()
    a, b = (np.asarray(data['set_target_data'][i]['head']) for i in (0, 1))
    head = Recorded(data).evaluate(0.005)[0]

    def angle(m: np.ndarray, n: np.ndarray) -> float:
        return float(Rotation.from_matrix(m[:3, :3].T @ n[:3, :3]).magnitude())

    assert angle(a, b) > 1e-3 and not np.allclose(a[:3, 3], b[:3, 3], atol=1e-6)
    assert np.allclose(head[:3, 3], a[:3, 3] + (b[:3, 3] - a[:3, 3]) / 4, atol=1e-12)
    assert angle(a, head) == pytest.approx(angle(a, b) / 4, rel=1e-6)
    assert angle(head, b) == pytest.approx(angle(a, b) * 3 / 4, rel=1e-6)  # on the shortest way from a to b


def _reachy_minis_frames() -> list[list[float]]:
    """[t, head (4x4, row by row), antennas, body yaw] as reachy_mini's RecordedMove plays _move()."""
    from reachy_mini.motion.recorded_move import RecordedMove  # noqa: PLC0415 — optional, local only

    move, frames = RecordedMove(_move()), []
    for t in np.arange(0.0, 0.97, 0.013):
        head, antennas, body = move.evaluate(float(t))
        frames.append([float(t), *head.ravel().tolist(), *antennas.tolist(), float(body)])
    return frames


def test_it_plays_the_frames_reachy_minis_recorded_move_plays() -> None:
    ours = Recorded(_move())
    for t, *frame in json.loads(REFERENCE.read_text())['frames']:
        head, antennas, body = ours.evaluate(t)
        assert np.allclose(head.ravel(), frame[:16], atol=1e-9), t
        assert np.allclose(antennas, frame[16:18], atol=1e-9) and body == pytest.approx(frame[18]), t


@pytest.mark.skipif(importlib.util.find_spec('reachy_mini') is None, reason='reachy_mini is not installed here')
def test_the_kept_frames_are_the_ones_reachy_mini_plays() -> None:
    """Else Pollen changed RecordedMove: see what changed in it, then write REFERENCE again with
    `PYTHONPATH=. .venv/bin/python tests/robot/test_recorded.py`."""
    assert np.allclose(json.loads(REFERENCE.read_text())['frames'], _reachy_minis_frames(), rtol=0, atol=1e-12)


if __name__ == '__main__':  # REFERENCE written again from the reachy_mini installed here
    source = f'reachy_mini {importlib.metadata.version("reachy-mini")}: RecordedMove.evaluate of _move()'
    frames = ',\n'.join(f'    {json.dumps(frame)}' for frame in _reachy_minis_frames())
    REFERENCE.write_text(f'{{\n  "source": {json.dumps(source)},\n  "frames": [\n{frames}\n  ]\n}}\n')
