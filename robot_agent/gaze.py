"""Regard actif: awake, with the setting on, the head follows one face.

A thread (`Tracker`) reads the head camera, finds faces with YuNet and tells the motion thread where the one it
follows is, in the robot's frame; the mixer's `Aim` turns the head there, under the mode's animation. Three facts
shape it:

- YuNet sees no face under ~30 px and costs by the pixel: it is handed small views in which a face is still big
  (three search views in turn, then a 112 px crop around the face it follows).
- The camera is in the head: a face is placed against the head pose sent when its frame was taken, or the head
  chases its own motion.
- A turning head is as loud in the robot's microphone as a voice: the aim is quick only while the comfort noise
  covers it (the robot talks, its body turns); otherwise it holds still until the face has really moved, then turns
  at the pace of an idle animation.

Without OpenCV, the model or a camera, the thread says so once and the head keeps to its animations.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

logger = logging.getLogger(__name__)

# YuNet 2023mar, OpenCV Zoo (MIT), sha256 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4.
MODEL = Path(__file__).resolve().parent / 'assets' / 'face_detection_yunet_2023mar.onnx'

# ----- Where the head may point, and how it gets there -----
# Off the body. The daemon turns the body itself near 60°: under every animation these fit its IK, and at their
# corners the wobble at full voice does not always (0.5 % of a sweep), which the mixer's reach check catches.
MAX_YAW = math.radians(40.0)
MAX_UP, MAX_DOWN = math.radians(30.0), math.radians(15.0)  # people sit above a desk robot's eyes
KEEP = 0.3  # the share of the animation left under a full gaze: a nod still reads, a lifted head keeps its reach
SMOOTH_S = 0.15  # the head following a face: detection noise never reaches the motors
SPEED = math.radians(150.0)  # under the comfort noise
QUIET_SPEED = math.radians(15.0)  # the microphone open: the pace of the quickest idle animation's glance
DEAD, REST = math.radians(5.0), math.radians(1.0)  # quiet, a face this far off starts a turn, which ends this near
HOLD_S = 0.5  # a sighting older than this is no sighting: the tracker stopped saying where the face is
RISE_S, FALL_S = 0.35, 0.8  # settling onto a face, letting go of it (longer when the turn would outpace the speed)
WIDEN_S, NARROW = 2.0, 1.0 / 16.0  # the share of gaze the reach allows: back in 2 s, given up a sixteenth at a time

# ----- How it looks -----
SEARCH_WIDTH, WING_WIDTH = 320, 200  # the views YuNet is handed, in pixels: a 30 px face is ~1.8 m away in them
CROP, CROP_FACES, CROP_MIN = 112, 2.5, 64  # the crop around the face followed: the face ~45 px, from 0.4 to 3 m
FIND_SCORE, KEEP_SCORE = 0.75, 0.6  # convincing to be found, merely plausible to be kept
TRACK_HZ, SEARCH_HZ, IDLE_HZ, IDLE_AFTER_S = 15.0, 6.0, 2.0, 10.0
LOST_S = 1.5  # a blink, a hand before the lens: how long the face followed is looked for where it was
SAME = math.radians(15.0)  # meanwhile, a face found this near where it was is that face
FRAME_WAIT_S, NO_FRAME_S = 0.1, 0.5
FRAME_AGE_S = 0.08  # exposure, the pipeline, up to a frame period in the appsink: estimated, the smoothing absorbs it
FACE_M, NEAR_M, FAR_M = 0.16, 0.3, 3.0  # a face's width gives its distance: the parallax of a camera above the head
VIEW_X, VIEW_Y = math.tan(math.radians(44.0)), math.tan(math.radians(33.0))  # half the field of view
NICE = 10  # the thread's scheduling priority: the audio and the conversation win the core
COSTED = 200  # detections averaged before the one cost line of a run


def _clamp(x: float, low: float, high: float) -> float:
    return low if x < low else high if x > high else x


def aimed(head: np.ndarray, yaw: float, pitch: float, weight: float) -> np.ndarray:
    """An animation's head pose (in the body's frame) turned to (yaw, pitch): +yaw left, +pitch down, no roll.
    `weight` is the share of gaze, 0 the animation untouched. The animation is damped rather than dropped, its
    rotation and its translation alike, and its translation turns with the gaze: leaning in is leaning towards you."""
    if weight <= 0.0:
        return head
    keep = 1.0 - weight * (1.0 - KEEP)
    cy, sy = math.cos(weight * yaw), math.sin(weight * yaw)
    cp, sp = math.cos(weight * pitch), math.sin(weight * pitch)
    x, y, z = keep * head[:3, 3]
    out = np.eye(4)
    out[:3, :3] = np.array([[cy * cp, -sy, cy * sp], [sy * cp, cy, sy * sp], [-sp, 0.0, cp]]) @ _damped(head, keep)
    out[:3, 3] = (cy * x - sy * y, sy * x + cy * y, z)
    return out


def _damped(head: np.ndarray, keep: float) -> np.ndarray:
    """A pose's rotation about the same axis, `keep` times its angle. Plain numpy: at each frame of the motion thread,
    where a scipy Rotation costs the robot 1.8 ms."""
    r = head[:3, :3]
    angle = math.acos(_clamp((r[0, 0] + r[1, 1] + r[2, 2] - 1.0) / 2.0, -1.0, 1.0))
    if angle < 1e-9:
        return np.eye(3)
    a, b, c = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]]) / (2.0 * math.sin(angle))
    skew = np.array([[0.0, -c, b], [c, 0.0, -a], [-b, a, 0.0]])
    return np.eye(3) + math.sin(keep * angle) * skew + (1.0 - math.cos(keep * angle)) * (skew @ skew)


def direction(yaw: float, pitch: float) -> np.ndarray:
    """The unit vector (yaw, pitch) points along, in the robot's frame."""
    return np.array([math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), -math.sin(pitch)])


def _stretch(x: float, y: float) -> float:
    """How much wider a face shows on the camera's z = 1 plane at (x, y) than dead ahead, at the same range (1.9 at
    the edge of this lens)."""
    return math.sqrt((1.0 + x * x) * (1.0 + x * x + y * y))


def locate(x: float, y: float, width: float, eye: np.ndarray) -> tuple[float, float, float]:
    """Where a face is, (yaw, pitch, metres) from the head's centre in the robot's frame, from its nose (x, y) and its
    width on the camera's z = 1 plane. `eye` is the camera's pose in the robot's frame when the frame was taken."""
    metres = _clamp(FACE_M * _stretch(x, y) / max(width, 1e-6), NEAR_M, FAR_M)
    ray = np.array([x, y, 1.0])
    dx, dy, dz = (eye @ np.append(ray / np.linalg.norm(ray) * metres, 1.0))[:3]
    return math.atan2(dy, dx), math.atan2(-dz, math.hypot(dx, dy)), float(math.sqrt(dx * dx + dy * dy + dz * dz))


class Aim:
    """Where the head looks, stepped by the mixer at each frame with the time it is given. Not thread-safe: the
    motion thread's lock is around every call."""

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.yaw = self.pitch = self.weight = 0.0  # off the body, and how much of it shows (eased by `step`)
        self.room = 1.0  # the share of it the robot's reach allows
        self._seen: tuple[float, float, float] | None = None  # the face: yaw in the robot's frame, pitch, when
        self._at: float | None = None
        self._turning = False

    def see(self, yaw: float, pitch: float, now: float) -> None:
        if math.isfinite(yaw) and math.isfinite(pitch):  # a NaN here would be smoothed into every frame after it
            self._seen = (yaw, pitch, now)

    def lose(self) -> None:
        self._seen = None

    def step(
        self,
        now: float,
        body: float,
        quick: bool = False,
        fits: Callable[[float, float, float], bool] | None = None,
    ) -> tuple[float, float, float]:
        """One frame on: (yaw off the body, pitch, share of gaze) to aim the animation with. `quick`: the comfort
        noise covers the motors. `fits(yaw, pitch, share)`: whether the robot can take that pose as it is."""
        dt = 0.0 if self._at is None else _clamp(now - self._at, 0.0, 0.1)
        self._at = now
        speed = SPEED if quick else QUIET_SPEED
        seen = self._seen
        if seen is not None and now - seen[2] < HOLD_S:
            yaw = _clamp(math.remainder(seen[0] - body, math.tau), -MAX_YAW, MAX_YAW)
            pitch = _clamp(seen[1], -MAX_UP, MAX_DOWN)
            if self.weight <= 0.0:
                self.yaw, self.pitch, self._turning = yaw, pitch, False  # the rising weight is the easing
            off = math.hypot(yaw - self.yaw, pitch - self.pitch)
            self._turning = off > 0.0 and (quick or off > (REST if self._turning else DEAD))
            if self._turning:
                step = min(1.0 - math.exp(-dt / SMOOTH_S), speed * dt / off)  # smoothed, and never past the speed
                self.yaw += (yaw - self.yaw) * step
                self.pitch += (pitch - self.pitch) * step
            self.weight = min(1.0, self.weight + dt / self._ease(RISE_S, speed))
        else:
            self.weight = max(0.0, self.weight - dt / self._ease(FALL_S, speed))
        share = self.weight * self.weight * (3.0 - 2.0 * self.weight)
        if share <= 0.0:
            self.room = 1.0
        elif fits is not None:
            wider = min(1.0, self.room + dt / WIDEN_S)
            if fits(self.yaw, self.pitch, share * wider):
                self.room = wider
            else:
                while self.room > 0.0 and not fits(self.yaw, self.pitch, share * self.room):
                    self.room = max(0.0, self.room - NARROW)
        return self.yaw, self.pitch, share * self.room

    def _ease(self, floor_s: float, speed: float) -> float:
        """How long the weight takes: never so short that its steepest moment turns the head past `speed`."""
        return max(floor_s, 1.5 * math.hypot(self.yaw, self.pitch) / speed)


class Face(NamedTuple):
    """One face, in a frame's pixels: its nose (where to look: the box's middle is over the eyes), its box's centre
    and width."""

    x: float
    y: float
    cx: float
    cy: float
    w: float

    def moved(self, dx: float, dy: float) -> Face:
        return Face(self.x + dx, self.y + dy, self.cx + dx, self.cy + dy, self.w)


Detect = Callable[[str, np.ndarray, int, float], list[Face]]  # (a view's name, the view, its width for YuNet, score)


class Lens:
    """The camera's optics, pixels to its z = 1 plane and back: OpenCV's model with the SDK's calibration, a plain
    pinhole without OpenCV."""

    def __init__(self, k: np.ndarray, d: np.ndarray, cv: Any = None) -> None:
        self._k, self._d, self._cv = np.asarray(k, dtype=np.float64), np.asarray(d, dtype=np.float64), cv
        self.focal = float(self._k[0, 0])

    def plane(self, pixels: Sequence[tuple[float, float]]) -> np.ndarray:
        points = np.asarray(pixels, dtype=np.float64)
        if self._cv is not None:
            return self._cv.undistortPoints(points.reshape(-1, 1, 2), self._k, self._d).reshape(-1, 2)
        return (points - self._k[:2, 2]) / np.array([self._k[0, 0], self._k[1, 1]])

    def pixel(self, point: np.ndarray) -> tuple[float, float]:
        if self._cv is not None:
            found, _ = self._cv.projectPoints(point.reshape(1, 1, 3), np.zeros(3), np.zeros(3), self._k, self._d)
            return float(found[0, 0, 0]), float(found[0, 0, 1])
        u, v = self._k[:2, :2] @ (point[:2] / point[2]) + self._k[:2, 2]
        return float(u), float(v)


def yunet(cv: Any) -> Detect:
    """YuNet on one OpenCV thread. A view is halved while it is twice the size asked (OpenCV's fast path), then
    resized: a straight area resize to an odd factor costs 12 ms on the robot."""
    cv.setNumThreads(1)
    nets: dict[str, Any] = {}
    sizes: dict[str, tuple[int, int]] = {}

    def detect(name: str, view: np.ndarray, width: int, score: float) -> list[Face]:
        height, wide = view.shape[:2]
        size = (width, max(1, round(width * height / wide)))
        if name not in nets:
            nets[name] = cv.FaceDetectorYN.create(str(MODEL), '', size, score)
        if sizes.get(name) != size:  # the input size is baked into a net: one per view, resized only when it must
            nets[name].setInputSize(size)
            sizes[name] = size
        small = view
        while small.shape[1] >= 2 * size[0] and small.shape[0] >= 2 * size[1]:
            small = cv.resize(small, (small.shape[1] // 2, small.shape[0] // 2), interpolation=cv.INTER_AREA)
        if (small.shape[1], small.shape[0]) != size:
            small = cv.resize(small, size, interpolation=cv.INTER_LINEAR)
        _, rows = nets[name].detect(small)
        sx, sy = wide / size[0], height / size[1]
        # A row: the box (x, y, w, h), five landmarks (the eyes, the nose tip, the mouth's corners), the score.
        return [] if rows is None else [
            Face(float(r[8]) * sx, float(r[9]) * sy, float(r[0] + r[2] / 2) * sx, float(r[1] + r[3] / 2) * sy,
                 float(r[2]) * sx)
            for r in rows
        ]  # fmt: skip

    return detect


class Tracker:
    """The thread that looks. `camera` has `read_frame(wait_s)` and `geometry()` (K, D, the camera in the head);
    `body` is the motion thread (`look`, `head_at`); `wanted()` says whether there is a conversation to look in."""

    def __init__(
        self,
        camera: Any,
        body: Any,
        wanted: Callable[[], bool],
        *,
        detect: Detect | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._camera, self._body, self._wanted, self._detect, self._clock = camera, body, wanted, detect, clock
        self._cv: Any = None
        self._on = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._optics: tuple[Lens, np.ndarray] | None = None  # the lens, and the camera's pose in the head
        self._face: tuple[float, float, float, float] | None = None  # the one followed: yaw, pitch, metres, when
        self._view = 0  # the search view to look in next
        self._seen_at = -math.inf
        self._costs: dict[str, list[float]] | None = {'search': [], 'follow': []}

    def enable(self, on: bool) -> None:
        """The setting, as each wake reads it. The thread starts with the first wake that wants it."""
        self._on = on
        if on and self._thread is None:
            self._thread = threading.Thread(target=self._run, name='gaze', daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        try:
            if self._detect is None:
                import cv2  # noqa: PLC0415 — on the robot only: nothing else here needs it

                if not MODEL.is_file():
                    raise FileNotFoundError(MODEL.name)
                self._cv, self._detect = cv2, yunet(cv2)
            if self._lens() is None:
                raise RuntimeError('the camera gives no calibration')
        except Exception as exc:  # noqa: BLE001 — no OpenCV, no model, no lens: the conversation never notices
            logger.warning('[GAZE] cannot look (%r): the head keeps to its animations', exc)
            return
        with contextlib.suppress(AttributeError, OSError):  # this thread alone, where the system can (Linux)
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)
        looking, failed = False, 0
        while not self._stop.is_set():
            began, period = self._clock(), 0.5
            try:
                if self._on and self._wanted():
                    if not looking:
                        looking, self._seen_at = True, began  # just woken: someone is likely there
                        logger.info('[GAZE] on')
                    period = self.tick()
                elif looking:
                    looking, self._face = False, None
                    self._body.look(None)
                failed = 0
            except Exception:  # one bad frame must not end the gaze
                failed += 1
                if failed == 1 or failed % 100 == 0:
                    logger.exception('[GAZE] a look failed (%d in a row)', failed)
            self._stop.wait(max(0.0, began + period - self._clock()))

    def tick(self) -> float:
        """One look: the face followed where it should be, else a search. Returns the period it asks for."""
        frame = self._camera.read_frame(FRAME_WAIT_S)
        optics = self._optics = self._optics or self._lens()
        if frame is None or optics is None:
            return NO_FRAME_S
        now = self._clock()  # once the frame is there: it may have been waited for
        head = self._body.head_at(now - FRAME_AGE_S)
        if head is None:  # the head was not the motion thread's (lent, frozen): nowhere to place a face from
            return 1.0 / TRACK_HZ
        lens, eye = optics[0], head @ optics[1]
        started, found, face = time.perf_counter(), None, self._face
        if face is not None and (guess := self._expected(face, eye, lens, frame.shape)) is not None:
            near = self._crop(frame, guess)
            if near:
                found = self._place(min(near, key=lambda f: (f.cx - guess.cx) ** 2 + (f.cy - guess.cy) ** 2), eye, lens)
        followed = found is not None
        if found is None:
            placed = [self._place(f, eye, lens) for f in self._search(frame)]
            if face is not None:  # still looked for: only a face where it was is that face
                was = direction(face[0], face[1])
                placed = [p for p in placed if float(direction(p[0], p[1]) @ was) > math.cos(SAME)]
            found = min(placed, key=lambda p: p[2], default=None)  # the nearest
        self._cost('follow' if followed else 'search', time.perf_counter() - started)
        if found is not None:
            if face is None:
                logger.info('[GAZE] a face at %+.0f°, %.1f m: following it', math.degrees(found[0]), found[2])
            face = self._face = (*found, now)
            self._seen_at = now
        elif face is not None and now - face[3] > LOST_S:
            face = self._face = None
            self._body.look(None)
            logger.info('[GAZE] nobody in view')
        if face is None:
            return 1.0 / (SEARCH_HZ if now - self._seen_at < IDLE_AFTER_S else IDLE_HZ)
        self._body.look(face[0], face[1])  # where it was, while it is looked for
        return 1.0 / TRACK_HZ

    def _lens(self) -> tuple[Lens, np.ndarray] | None:
        geometry = self._camera.geometry()
        return None if geometry is None else (Lens(geometry[0], geometry[1], self._cv), np.asarray(geometry[2]))

    def _place(self, face: Face, eye: np.ndarray, lens: Lens) -> tuple[float, float, float]:
        half = face.w / 2.0
        (x, y), (left, _), (right, _) = lens.plane(
            [(face.x, face.y), (face.cx - half, face.cy), (face.cx + half, face.cy)]
        )
        return locate(float(x), float(y), abs(float(right - left)), eye)

    def _expected(self, face: tuple[float, ...], eye: np.ndarray, lens: Lens, shape: tuple[int, ...]) -> Face | None:
        """Where the face followed should show in this frame, given where the head was; None out of view."""
        cam = np.linalg.inv(eye) @ np.append(face[2] * direction(face[0], face[1]), 1.0)
        if cam[2] <= 0.05 or abs(cam[0] / cam[2]) > VIEW_X or abs(cam[1] / cam[2]) > VIEW_Y:
            return None
        u, v = lens.pixel(cam[:3])
        if not (0.0 <= u < shape[1] and 0.0 <= v < shape[0]):
            return None
        width = lens.focal * FACE_M * _stretch(cam[0] / cam[2], cam[1] / cam[2]) / float(np.linalg.norm(cam[:3]))
        return Face(u, v, u, v, width)

    def _crop(self, frame: np.ndarray, guess: Face) -> list[Face]:
        height, width = frame.shape[:2]
        side = int(min(max(CROP_FACES * guess.w, CROP_MIN), height, width))
        x0, y0 = int(_clamp(guess.cx - side / 2, 0, width - side)), int(_clamp(guess.cy - side / 2, 0, height - side))
        return [f.moved(x0, y0) for f in self._found('crop', frame[y0 : y0 + side, x0 : x0 + side], CROP, KEEP_SCORE)]

    def _search(self, frame: np.ndarray) -> list[Face]:
        """Every face of one view, in turn: the upper middle (where someone at the desk is), the upper sides, then
        the whole frame at a quarter (anyone within ~0.9 m, low down included)."""
        height, width = frame.shape[:2]
        view, self._view = self._view, (self._view + 1) % 3
        top = height * 2 // 3
        if view == 0:
            x0 = width // 4
            return [f.moved(x0, 0) for f in self._found('middle', frame[:top, x0 : x0 + width // 2], SEARCH_WIDTH)]
        if view == 1:
            wing = width * 5 // 16  # they overlap the middle by a face
            right = [f.moved(width - wing, 0) for f in self._found('side', frame[:top, width - wing :], WING_WIDTH)]
            return self._found('side', frame[:top, :wing], WING_WIDTH) + right
        return self._found('whole', frame, SEARCH_WIDTH)

    def _found(self, name: str, view: np.ndarray, width: int, score: float = FIND_SCORE) -> list[Face]:
        assert self._detect is not None
        return self._detect(name, view, width, score)

    def _cost(self, kind: str, seconds: float) -> None:
        """What a look costs on this robot, logged once a run."""
        costs = self._costs
        if costs is not None:
            costs[kind].append(seconds * 1000.0)
            if sum(len(v) for v in costs.values()) >= COSTED:
                self._costs = None
                said = ', '.join(f'{k} {sum(v) / len(v):.1f} ms x{len(v)}' for k, v in costs.items() if v)
                logger.info('[GAZE] a look costs: %s', said)
