"""The single motion engine: a pure `Mixer` that says what the robot shows at any instant, and the 30 Hz thread
that samples it and is the only thing commanding the head, the antennas and the body.

The mixer samples one source, the mode's animation or a scripted move (a gesture, an emotion), in the body's
frame, and blends from the last pose sampled by slerping the rotation and lerping the translation and the
antennas. While it talks, Pollen's breathing goes into the talking animation and their wobble over whatever plays,
following the audio the speaker is given (`wobble.py`). With a face to look at, the mode's animation is aimed at it
(`gaze.py`): a move takes the head whole. The body yaw follows `turn_body` at a pace its motor takes.
Waking and falling asleep are mode changes with longer blends. The mixer never reads a clock: the thread passes the
monotonic time, and the dashboard's 3D preview (`rehearse`, run on the Mac) passes the times of its script, so the
preview is the robot's own motion.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation

from robot_agent import animation_names, animations, config, gaze, wobble

logger = logging.getLogger(__name__)

BLEND_S = 0.5
POSE_AGE_S = 0.1  # a head pose sent further than this from the time asked was not the one held then
# The SDK's sleep pose (reachy_mini.reachy_mini SLEEP_HEAD_POSE and SLEEP_ANTENNAS_JOINT_POSITIONS).
SLEEP_HEAD = np.array(
    [[0.911, 0.004, 0.413, -0.021], [-0.004, 1.0, -0.001, 0.001], [-0.413, -0.001, 0.911, -0.044], [0, 0, 0, 1.0]]
)
SLEEP_ANTENNAS = np.array([-3.05, 3.05])
Frame = tuple[np.ndarray, np.ndarray, float]  # head in the body's frame, antennas, body yaw (rad)
Reach = Callable[[np.ndarray, float], bool]  # whether the robot takes a head pose (its frame) with this body yaw


def blend_pose(a: np.ndarray, b: np.ndarray, alpha: float) -> np.ndarray:
    """`alpha` of the way from pose `a` to `b`. A lerped 4x4 matrix is not a rigid pose: slerp the rotation."""
    out = np.eye(4)
    turn = Rotation.from_matrix(a[:3, :3].T @ b[:3, :3]).as_rotvec()
    out[:3, :3] = a[:3, :3] @ Rotation.from_rotvec(alpha * turn).as_matrix()
    out[:3, 3] = (1.0 - alpha) * a[:3, 3] + alpha * b[:3, 3]
    return out


def rot_z(yaw: float) -> np.ndarray:
    out = np.eye(4)
    out[:2, :2] = [[math.cos(yaw), -math.sin(yaw)], [math.sin(yaw), math.cos(yaw)]]
    return out


@dataclass(frozen=True)
class _Ramp:
    """A value going from `start` to `end` over [t0, t0 + duration], eased; poses for a blend."""

    start: Any
    end: Any
    t0: float
    duration: float

    def alpha(self, now: float) -> float:
        return 1.0 if self.duration <= 0 else animations.smoothstep((now - self.t0) / self.duration)


class Mixer:
    """What the robot shows, as a function of the time it is given. Not thread-safe: MotionRunner locks it."""

    def __init__(
        self,
        now: float,
        current: Frame | None = None,
        ease_s: float = 0.0,
        speech: bool = config.MIC_MUTE_WHILE_TALKING,
        reach: Reach | None = None,
    ) -> None:
        self.mode, self.mode_t0 = 'sleep', now
        self.speech = speech  # Pollen's breathing and wobble while it talks: only if the comfort noise covers them
        self.wobble = wobble.Wobble()
        self.aim = gaze.Aim()  # the face to look at, if any: the mode's animation is aimed at it
        self.reach = reach  # asked of each aimed pose: the gaze gives way rather than send one the robot refuses
        self.variants = dict(animation_names.DEFAULTS)
        self.tunings = {mode: animations.Tuning() for mode in animation_names.MODES}
        self.move: Any = None  # a gesture or a recorded emotion: .duration and .evaluate(t)
        self.move_t0 = 0.0
        self.head, self.antennas, self.body = SLEEP_HEAD, SLEEP_ANTENNAS, 0.0  # last sampled, head in body frame
        if current is not None:
            head, self.antennas, self.body = current
            self.head = rot_z(-self.body) @ head
        self.turn = _Ramp(self.body, self.body, now, 0.0)  # body yaw, rad
        self.blend: _Ramp | None = _Ramp((self.head, self.antennas), None, now, ease_s) if ease_s > 0 else None

    def set_mode(self, mode: str, now: float, blend_s: float = BLEND_S) -> None:
        """'sleep' or one of animation_names.MODES. Asleep, the head rests on the body: no wobble."""
        if mode != self.mode:
            self.mode, self.mode_t0 = mode, now
            if mode == 'sleep':
                self.wobble.cut(now)
                self.aim.clear()
            if self.move is None:
                self._start_blend(now, blend_s)

    def hear(self, samples: np.ndarray, start: float, rate: int) -> None:
        """Audio the speaker plays from `start`: the head wobbles with it."""
        if self.speech and self.mode != 'sleep':
            self.wobble.hear(samples, start, rate)

    def set_variant(self, mode: str, key: str, now: float, tuning: animations.Tuning | None = None) -> bool:
        if key not in animations.VARIANTS.get(mode, {}):
            return False
        tuning = tuning or self.tunings[mode]
        if (self.variants[mode], self.tunings[mode]) != (key, tuning):
            self.variants[mode], self.tunings[mode] = key, tuning
            if self.mode == mode and self.move is None:
                self._start_blend(now, BLEND_S)
        return True

    def turn_body(self, degrees: float, now: float, speed: float = 1.0) -> float:
        """Turn the body to an absolute yaw, clamped to ±BODY_YAW_LIMIT_DEG, `speed` times as fast as the usual pace.
        Returns the target in degrees."""
        target = max(-config.BODY_YAW_LIMIT_DEG, min(config.BODY_YAW_LIMIT_DEG, float(degrees)))
        if not math.isclose(self.turn.end, math.radians(target), abs_tol=1e-6):  # else there, or on its way
            span = abs(target - math.degrees(self.body)) / config.BODY_TURN_SPEED_DEG_S
            self.turn = _Ramp(self.body, math.radians(target), now, max(config.BODY_TURN_MIN_S, span) / speed)
        return target

    def play_move(self, move: Any, now: float, blend_s: float | None = None) -> None:
        """A blend slower than BLEND_S (out of the sleep pose, the wake's under way) holds the move's clock until it
        ends: the robot reaches the move's first frame, then plays it whole, as the preview shows it."""
        took = self._start_blend(now, self._blend_s() if blend_s is None else blend_s)
        self.move, self.move_t0 = move, now + (took if took > BLEND_S else 0.0)

    def stop_moves(self, now: float) -> None:
        if self.move is not None:
            self.move = None
            self._start_blend(now, self._blend_s())

    def reseat(self, current: Frame, now: float, ease_s: float = BLEND_S) -> None:
        """Start again from the pose the robot is in (read after its motors were freed and taken back): no move, the
        body where it was left, a blend into the mode's animation."""
        head, self.antennas, self.body = current
        self.head, self.move = rot_z(-self.body) @ head, None
        self.turn = _Ramp(self.body, self.body, now, 0.0)
        self.blend = _Ramp((self.head, self.antennas), None, now, ease_s)
        self.aim.clear()

    def moving_until(self) -> float:
        """When the move (a gesture, an emotion) and the body turn under way end."""
        move_end = self.move_t0 + self.move.duration if self.move is not None else 0.0
        return max(move_end, self.turn.t0 + self.turn.duration)

    def posed_at(self, now: float) -> float:
        """When the blend and the body turn under way end: the pose asked for is reached (a move may go on)."""
        blend_end = self.blend.t0 + self.blend.duration if self.blend is not None else now
        return max(now, blend_end, self.turn.t0 + self.turn.duration)

    def settled(self, now: float) -> bool:
        """No move playing, no blend, no turn under way."""
        blend_done = self.blend is None or now >= self.blend.t0 + self.blend.duration
        return self.move is None and blend_done and now >= self.turn.t0 + self.turn.duration

    def sample(self, now: float) -> Frame:
        """The frame at `now` (never earlier than the last one), remembered as the start of the next blend; the wobble
        goes over it and is not remembered: it runs on through a blend. The mode's animation is aimed at the face to
        look at, as far as the robot's reach lets the whole frame go."""
        move = self.move
        if move is not None and now - self.move_t0 >= move.duration:
            self.move = move = None
            self._start_blend(now, self._blend_s())
        turn = self.turn
        body = turn.start + (turn.end - turn.start) * turn.alpha(now)
        aiming = False
        if move is not None:
            played = move.evaluate(min(max(0.0, now - self.move_t0), max(0.0, move.duration - 0.01)))
            head, antennas = played[:2]
        elif self.mode == 'sleep':
            head, antennas = SLEEP_HEAD, SLEEP_ANTENNAS
        else:
            mode, t = self.mode, now - self.mode_t0
            channels = animations.channels(mode, self.variants[mode], self.tunings[mode], t)
            if mode == 'talking' and self.speech:
                channels = channels + wobble.breathing(t)
            head, antennas = animations.to_pose(channels)
            aiming = True
        head, antennas = np.asarray(head, dtype=np.float64), np.asarray(antennas, dtype=np.float64)
        blend, start, alpha = self.blend, None, 1.0
        if blend is not None:
            alpha = blend.alpha(now)
            if alpha >= 1.0:
                self.blend = None
            else:
                start = blend.start[0]
                antennas = (1.0 - alpha) * blend.start[1] + alpha * antennas
        offset = self.wobble.offset(now)

        def shown(source: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            """A source pose as it is remembered (blended from where the blend started) and sent (the wobble over)."""
            posed = source if start is None else blend_pose(start, source, alpha)
            return posed, (posed if offset is None else wobble.over(posed, offset))

        # Quick only under the comfort noise: the robot talks, or its body turns (a tool's move).
        quick = self.speech and (self.mode == 'talking' or now < turn.t0 + turn.duration)
        if aiming:
            source, reach = head, self.reach
            fits = reach and (lambda y, p, w: reach(rot_z(body) @ shown(gaze.aimed(source, y, p, w))[1], body))
            head = gaze.aimed(source, *self.aim.step(now, body, quick, fits or None))
        else:
            self.aim.step(now, body, quick)  # a move takes the head whole: the aim runs on, back once it ends
        self.head, sent = shown(head)
        self.antennas, self.body = antennas, body
        if move is not None and getattr(move, 'turns', False) and now >= self.move_t0:  # a take: the base as taped
            self.body = float(played[2])
            self.turn = _Ramp(self.body, self.body, now, 0.0)  # a turn asked after it starts from there
        return sent, antennas, self.body

    def _blend_s(self) -> float:
        """Into or out of a move: asleep, the antennas swing ~2.9 rad from the sleep pose, at the wake's pace."""
        return config.WAKE_S if self.mode == 'sleep' else BLEND_S

    def _start_blend(self, now: float, duration: float) -> float:
        """From the pose last sampled; never ending before the blend under way, or a long move (the wake, the
        sleep) would be finished in BLEND_S, its antennas past their joint speed. Returns its duration."""
        if self.blend is not None:
            duration = max(duration, self.blend.t0 + self.blend.duration - now)
        self.blend = _Ramp((self.head, self.antennas), None, now, duration)
        return duration


Cue = tuple[float, Callable[[Mixer, float], object]]  # (seconds from the start, what happens then)


def rehearse(cues: Iterable[Cue], seconds: float, hz: float = config.MOTION_HZ, mode: str = 'idle') -> list[Frame]:
    """The frames the motion thread would send for a script of cues, from rest in `mode`: the 3D preview.

    The mixer starts settled in the mode (no wake blend), and every cue runs at its time, before that frame."""
    mixer = Mixer(0.0)
    mixer.mode = mode
    mixer.sample(0.0)  # at rest in the mode: what a blend starts from
    pending = sorted(cues, key=lambda cue: cue[0])
    frames = []
    for n in range(int(seconds * hz) + 1):
        now = n / hz
        while pending and pending[0][0] <= now:
            at, cue = pending.pop(0)
            cue(mixer, at)
        frames.append(mixer.sample(now))
    return frames


class MotionRunner:
    """Owns the robot's pose. Callers say what to play, from any thread; every call returns at once."""

    def __init__(self, mini: Any, clock: Callable[[], float] = time.monotonic, reach: Reach | None = None) -> None:
        self._mini = mini
        self._clock = clock
        self._reach = reach
        self._sent: deque[tuple[float, np.ndarray]] = deque(maxlen=config.MOTION_HZ)  # a second of head poses sent
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._frozen = threading.Event()  # set: nothing is written, a freed motor must not be given a target
        self._lent = False  # an app holds the body (the manette): only its own moves play
        self._writing = threading.Lock()  # held while a frame is sent: `freeze` returns once none is in flight
        self._thread = threading.Thread(target=self._loop, name='motion', daemon=True)
        self.mixer = Mixer(clock(), reach=reach)
        self.errors = 0
        self._logged_at = -math.inf

    def start(self, current: Frame | None = None) -> None:
        """Run from the pose the robot is in, read beforehand: it eases into the sleep pose, no snap."""
        with self._lock:
            self.mixer = Mixer(self._clock(), current, ease_s=config.SLEEP_S, reach=self._reach)
        self._thread.start()

    def freeze(self) -> None:
        """Write nothing from now on: the daemon holds the last targets. A target written to a freed motor is where it
        snaps when its torque returns."""
        self._frozen.set()
        with self._writing:  # a frame already on its way is sent before this returns
            pass

    def thaw(self, current: Frame) -> None:
        """Write again, from `current` (read once the motors are taken back), blending into the mode's animation."""
        with self._lock:
            self.mixer.reseat(current, self._clock(), config.WAKE_S)  # a head moved far by hand eases back slowly
        self._frozen.clear()

    @property
    def frozen(self) -> bool:
        return self._frozen.is_set()

    def stop(self) -> None:
        """Stop the thread, then send the pose reached: settled is a time, the last frame may be one short of it."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
            if not self._thread.is_alive() and not self.frozen:
                with contextlib.suppress(Exception):  # the shutdown goes on
                    self._step(self._clock())

    def set_mode(self, mode: str, blend_s: float = BLEND_S) -> None:
        with self._lock:
            self.mixer.set_mode(mode, self._clock(), blend_s)

    def set_variant(self, mode: str, key: str, tuning: animations.Tuning | None = None) -> None:
        with self._lock:
            known = self.mixer.set_variant(mode, key, self._clock(), tuning)
        if not known:
            logger.warning('unknown %s animation %r: keeping %r', mode, key, self.mixer.variants.get(mode))

    def wobble(self, samples: np.ndarray, start: float, rate: int) -> None:
        """The audio the speaker plays from `start` (monotonic): the head wobbles with it. From the event loop: a few
        dozen operations per 50 ms of audio. Not while an app holds the body."""
        try:
            with self._lock:
                if not self._lent:
                    self.mixer.hear(samples, start, rate)
        except Exception as exc:  # noqa: BLE001 — the voice plays on; only its wobble is lost
            self._failed(exc, 'the wobble')

    def stop_wobble(self) -> None:
        """Nothing queued plays any more (a flush): the wobble fades out."""
        with self._lock:
            self.mixer.wobble.cut(self._clock())

    def lend(self, lent: bool) -> None:
        """While lent (an app holds the body), every move but the lender's own (`own=True`) is dropped: one already
        on its way when the app took the body would replace its still pose. The wobble stops too."""
        with self._lock:
            self._lent = lent
            if lent:
                self.mixer.wobble.cut(self._clock())
                self.mixer.aim.lose()

    def look(self, yaw: float | None, pitch: float = 0.0) -> None:
        """The face to look at, in the robot's frame (rad: +yaw left, +pitch down); None: nobody. From the gaze's
        thread. Not while an app holds the body."""
        with self._lock:
            if yaw is None or self._lent:
                self.mixer.aim.lose()
            else:
                self.mixer.aim.see(yaw, pitch, self._clock())

    def head_at(self, t: float) -> np.ndarray | None:
        """The head pose sent nearest `t` (this thread's clock), in the robot's frame: where the camera was when a
        frame was taken. None when none was sent about then (frozen: the head is in someone's hand)."""
        with self._lock:
            if self._frozen.is_set() or not self._sent:
                return None
            at, head = min(self._sent, key=lambda sent: abs(sent[0] - t))
        return head if abs(at - t) <= POSE_AGE_S else None

    def turn_body(self, degrees: float, speed: float = 1.0, *, own: bool = False) -> float:
        with self._lock:
            if self._lent and not own:
                return math.degrees(self.mixer.turn.end)
            return self.mixer.turn_body(degrees, self._clock(), speed)

    def gesture(self, name: str) -> bool:
        move = animations.GESTURES.get(name)
        if move is not None:
            self.play_move(move)
        return move is not None

    def play_move(self, move: Any, *, own: bool = False, blend_s: float | None = None) -> None:
        with self._lock:
            if self._lent and not own:
                logger.info('a move while the body is lent: dropped')
                return
            self.mixer.play_move(move, self._clock(), blend_s)

    def play_take(self, take: Any, lead_in_s: float) -> float | None:
        """A take (take.Take), from its first pose: the base turns to where the take starts while the head blends
        there, the take's clock held meanwhile. Returns when (on this clock) the take itself starts; None if dropped."""
        with self._lock:
            if self._lent:
                return None
            now = self._clock()
            self.mixer.turn_body(math.degrees(take.evaluate(0.0)[2]), now)
            turned = self.mixer.turn.t0 + self.mixer.turn.duration - now
            self.mixer.play_move(take, now, max(lead_in_s, turned))
            return self.mixer.move_t0

    def stop_moves(self, *, own: bool = False) -> None:
        with self._lock:
            if not (self._lent and not own):
                self.mixer.stop_moves(self._clock())

    def moving_until(self) -> float:
        with self._lock:
            return self.mixer.moving_until()

    def posed_at(self) -> float:
        with self._lock:
            return self.mixer.posed_at(self._clock())

    def current_move(self) -> Any:
        """The move playing now (a gesture, an emotion, an app's pose), or None."""
        with self._lock:
            return self.mixer.move

    def settled(self) -> bool:
        with self._lock:
            return self.mixer.settled(self._clock())

    async def settled_within(self, timeout: float) -> bool:
        """Whether it settles within `timeout` seconds, polled on the event loop: waiting holds no thread."""
        deadline = time.monotonic() + timeout
        while not self.settled():
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def _loop(self) -> None:
        period = 1.0 / config.MOTION_HZ
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                with self._writing:
                    if not self._frozen.is_set():
                        self._step(self._clock())
            except Exception as exc:  # noqa: BLE001 — one bad frame must not freeze the head for good
                self._failed(exc, 'motion cycle')
            next_at += period
            now = time.monotonic()
            if next_at < now - period:  # fell behind: resync rather than fire a burst of frames
                next_at = now
            self._stop.wait(max(0.0, next_at - now))

    def _failed(self, exc: Exception, what: str) -> None:
        self.errors += 1
        if time.monotonic() - self._logged_at >= config.ERROR_LOG_EVERY_S:
            self._logged_at = time.monotonic()
            logger.warning('%s failed (%d motion errors so far): %s', what, self.errors, exc)

    def _step(self, now: float) -> None:
        with self._lock:
            head, antennas, body = self.mixer.sample(now)
            world = rot_z(body) @ head
            self._sent.append((now, world))
        # Three commands, as the SDK's play_move sends them: the combined set_target may miss the body motor.
        self._mini.set_target_head_pose(world)
        self._mini.set_target_antenna_joint_positions(list(antennas))
        self._mini.set_target_body_yaw(body)
