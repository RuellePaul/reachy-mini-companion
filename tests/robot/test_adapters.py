"""The Reachy Mini adapters, on a fake SDK object: the speaker guard and the camera's frame and JPEG."""

from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
from PIL import Image

from robot_agent import config, speaker
from robot_agent.adapters.robot import RobotAudio, RobotCamera, read_pose


def test_an_empty_array_never_reaches_the_sdk() -> None:
    pushed: list[np.ndarray] = []
    audio = RobotAudio(SimpleNamespace(media=SimpleNamespace(push_audio_sample=pushed.append)))
    audio.push_audio_sample(np.zeros(0, dtype=np.float32))
    audio.push_audio_sample(np.zeros(3, dtype=np.float32))
    assert [a.size for a in pushed] == [3]


def test_an_emotions_sound_goes_through_the_daemon_which_can_stop_it(monkeypatch) -> None:
    posted: list[tuple[str, object]] = []
    monkeypatch.setattr(speaker._HTTP, 'post', lambda url, json, timeout: posted.append((url, json)))
    audio = RobotAudio(SimpleNamespace(media=SimpleNamespace()))  # mini.media's own playbin cannot be stopped
    audio.play_sound('/cache/emotions/loving1.wav')
    audio.stop_sound()
    assert posted == [
        ('http://localhost:8000/api/media/play_sound', {'file': '/cache/emotions/loving1.wav'}),
        ('http://localhost:8000/api/media/stop_sound', None),
    ]


def _camera(frames: list) -> RobotCamera:
    def read():
        return frames.pop(0) if frames else None

    return RobotCamera(SimpleNamespace(media=SimpleNamespace(camera=SimpleNamespace(read=read))))


def test_a_frame_is_awaited_past_empty_pulls_then_sent_as_a_jpeg_rgb_768() -> None:
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame[..., 2] = 255  # red, in BGR
    jpeg = _camera([None, None, np.zeros(0), frame]).read_jpeg()
    image = Image.open(io.BytesIO(jpeg))
    assert (image.format, image.size) == ('JPEG', (768, 432))
    red, green, blue = image.getpixel((10, 10))
    assert red > 200 and green < 50 and blue < 50


def test_no_frame_before_the_deadline_is_no_picture(monkeypatch) -> None:
    monkeypatch.setattr(config, 'LOOK_WAIT_S', 0.05)
    assert _camera([]).read_jpeg() is None


def test_the_pose_is_read_in_the_world_and_a_failed_read_is_none() -> None:
    mini = SimpleNamespace(
        get_current_head_pose=lambda: np.eye(4),
        get_current_joint_positions=lambda: ([0.5, 0, 0, 0, 0, 0, 0], [-3.05, 3.05]),
    )
    head, antennas, body = read_pose(mini)
    assert np.allclose(head, np.eye(4)) and antennas.tolist() == [-3.05, 3.05] and body == 0.5
    assert read_pose(SimpleNamespace()) is None
