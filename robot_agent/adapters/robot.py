"""The adapters on a real Reachy Mini, and the only module that imports `reachy_mini`.

The daemon lends the camera to any `ReachyMini(media_backend='no_media')` client and takes it back only in
`__exit__`: bench scripts on the robot must always use `with ReachyMini(...) as mini:`, or the camera stays
dark until the daemon restarts (the camera is lent by the daemon).
"""

from __future__ import annotations

import io
import logging
import math
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import numpy as np
import requests

from robot_agent import config, speaker

try:
    from PIL import Image
except ImportError:  # no JPEG encoder: no `look` tool
    Image = None

logger = logging.getLogger(__name__)


@contextmanager
def connect() -> Iterator[Any]:
    """The robot, motors on, its audio pipelines opened once for the life of the process."""
    from reachy_mini import ReachyMini  # noqa: PLC0415 — native SDK, present on the robot only

    with ReachyMini(media_backend='local') as mini:
        mini.enable_motors()
        mini.media.start_recording()
        mini.media.start_playing()
        try:
            yield mini
        finally:
            with suppress(Exception):
                mini.media.stop_recording()
            with suppress(Exception):
                mini.media.stop_playing()


def emotion_library(dataset: str) -> Any:
    """One of Pollen's move libraries: from the Hugging Face cache, else a download. Blocking: the loader's thread."""
    from reachy_mini.motion.recorded_move import RecordedMoves  # noqa: PLC0415 — native SDK, robot only

    return RecordedMoves(dataset)


def read_pose(mini: Any) -> tuple[np.ndarray, np.ndarray, float] | None:
    """The pose the robot is in (head, antennas, body yaw), so the motion thread starts without a snap."""
    try:
        head = np.asarray(mini.get_current_head_pose(), dtype=np.float64)
        joints, antennas = mini.get_current_joint_positions()
        return head, np.asarray(antennas, dtype=np.float64), float(joints[0])
    except Exception:
        logger.exception('could not read the pose: the motion starts from the sleep pose')
        return None


def read_frame(mini: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """The pose the robot is in, whole (head, antennas, the seven head joints: the base's yaw, then the platform's
    six motors), in one read: what the manette tapes and sends on. None, without a word, when it cannot be read."""
    try:
        head = np.asarray(mini.get_current_head_pose(), dtype=np.float64)
        joints, antennas = mini.get_current_joint_positions()
        return head, np.asarray(antennas, dtype=np.float64), np.asarray(joints, dtype=np.float64)
    except Exception:  # noqa: BLE001 — the SDK's own failures: the next read comes 20 ms later
        return None


BODY_MARGIN = math.radians(3.0)  # kept from where the daemon turns the body itself: a turn it has not received yet
BODY_MOVED = 1e-4  # rad: the IK's body yaw further than this from the one given is the daemon moving the body


def reach() -> Callable[[np.ndarray, float], bool] | None:
    """Whether the daemon would take a head pose (in the robot's frame) as it is, the body where it is: its own IK
    solves it without turning the body (past ~60° off it, it does), BODY_MARGIN further included, and the platform's
    six motors stay in the URDF's ranges, which the IK does not know. For the motion thread alone. None when the
    SDK's kinematics cannot be had: the gaze's own limits stand alone."""
    try:
        import reachy_mini  # noqa: PLC0415 — native SDK, present on the robot only
        from reachy_mini.kinematics import AnalyticalKinematics  # noqa: PLC0415

        solver = AnalyticalKinematics(automatic_body_yaw=True)
        urdf = Path(reachy_mini.__file__).resolve().parent / 'descriptions' / 'reachy_mini' / 'urdf' / 'robot.urdf'
        joints = re.findall(
            r'<joint name="stewart_\d".*?<limit[^>]*lower="([^"]+)"[^>]*upper="([^"]+)"', urdf.read_text(), re.S
        )
        limits = np.array(joints, dtype=np.float64) if len(joints) == 6 else None
    except Exception as exc:  # noqa: BLE001 — another SDK, another layout: the gaze keeps its own limits
        logger.warning('[GAZE] no reach check (%r): the gaze keeps to its own limits', exc)
        return None

    def fits(head: np.ndarray, body: float) -> bool:
        if not np.all(np.isfinite(head)):
            return False
        solved = solver.ik(head, body_yaw=body)
        if solved is None or np.any(np.isnan(solved)) or abs(solved[0] - body) > BODY_MOVED:
            return False
        if limits is not None and not np.all((solved[1:] >= limits[:, 0]) & (solved[1:] <= limits[:, 1])):
            return False
        further = np.eye(4)  # the same pose a little further off the body must not move it either
        heading = math.remainder(math.atan2(head[1, 0], head[0, 0]) - body, math.tau)
        c, s = math.cos(BODY_MARGIN), math.copysign(math.sin(BODY_MARGIN), heading)
        further[:2, :2] = [[c, -s], [s, c]]
        spare = solver.ik(further @ head, body_yaw=body)
        return spare is not None and abs(spare[0] - body) <= BODY_MOVED

    return fits


def camera_released() -> bool:
    """Whether the daemon lent the camera to another client that never gave it back."""
    try:
        status = requests.get(f'{config.DAEMON_URL}/api/media/status', timeout=2.0).json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('[CAMERA] media status unreadable: %s', exc)
        return False
    return isinstance(status, dict) and bool(status.get('released'))


class RobotAudio:
    """`mini.media`, read at each call: the SDK may replace its MediaManager."""

    def __init__(self, mini: Any) -> None:
        self._mini = mini

    def get_input_samplerate(self) -> int:
        return int(self._mini.media.get_input_audio_samplerate())

    def get_output_samplerate(self) -> int:
        return int(self._mini.media.get_output_audio_samplerate())

    def get_audio_sample(self) -> np.ndarray | None:
        sample = self._mini.media.get_audio_sample()
        return None if sample is None else np.asarray(sample)

    def push_audio_sample(self, samples: np.ndarray) -> None:
        # The SDK wraps empty bytes in a GstBuffer with no memory: GStreamer-CRITICALs, then a segfault.
        if samples.size:
            self._mini.media.push_audio_sample(samples)

    def clear_output(self) -> None:
        self._mini.media.audio.clear_player()  # MediaManager does not re-export the flush

    def play_sound(self, path: str) -> None:
        speaker.play(path)  # through the daemon, which can stop it; mini.media's own playbin cannot be stopped

    def stop_sound(self) -> None:
        speaker.stop()


class RobotCamera:
    """The head camera: one JPEG on demand (`look`), frames for the gaze. The appsink keeps the latest frame and
    each pull takes it: the two readers take turns, a frame or two apart at 30 fps."""

    PULL_FLOOR_S = 0.005  # a dead pipeline answers at once: without a floor the retry loop spins

    def __init__(self, mini: Any) -> None:
        self._mini = mini

    @staticmethod
    def available(mini: Any) -> bool:
        return getattr(mini.media, 'camera', None) is not None and Image is not None

    def geometry(self) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """The lens (K at the resolution it runs at, D) and the camera's pose in the head, from the SDK; None when it
        does not give them."""
        camera = self._mini.media.camera
        k, d, mount = getattr(camera, 'K', None), getattr(camera, 'D', None), getattr(self._mini, 'T_head_cam', None)
        if k is None or d is None or mount is None:
            return None
        return np.asarray(k, dtype=np.float64), np.asarray(d, dtype=np.float64), np.asarray(mount, dtype=np.float64)

    def read_frame(self, wait_s: float = config.LOOK_WAIT_S) -> np.ndarray | None:
        """A BGR frame. A pull waits 20 ms and frames come every 33 ms, so one ask often comes back empty."""
        deadline = time.monotonic() + wait_s
        while True:
            pulled_at = time.monotonic()
            frame = self._mini.media.camera.read()
            if frame is not None and getattr(frame, 'size', 0):
                return frame
            now = time.monotonic()
            if now >= deadline:
                return None
            if now - pulled_at < self.PULL_FLOOR_S:
                time.sleep(self.PULL_FLOOR_S)

    def read_jpeg(self) -> bytes | None:
        try:
            frame = self.read_frame()
            if frame is None or frame.ndim != 3 or frame.shape[2] != 3:
                logger.warning('[CAMERA] no usable frame in %.1f s', config.LOOK_WAIT_S)
                return None
            image = Image.fromarray(np.ascontiguousarray(frame[:, :, ::-1]))  # BGR to RGB
            scale = config.LOOK_MAX_EDGE / max(image.size)
            if scale < 1.0:
                image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=config.LOOK_JPEG_QUALITY)
            return buffer.getvalue()
        except Exception:
            logger.exception('[CAMERA] could not produce a frame')
            return None
