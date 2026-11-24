"""The wake button: an antenna moved by hand while the robot sleeps, read from its encoder. In a scene, the stop
button (an antenna held off where the motion thread puts it) and, in a rehearsal, the go-on button (a tap)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from robot_agent import config

logger = logging.getLogger(__name__)

HOLD_RAD = 0.6  # generous: a motor lagging a fast move stays well under it, a hand holding the antenna does not
HOLD_SAMPLES = round(0.3 * config.ANTENNA_POLL_HZ)  # held this long (0.3 s)
TAP_RAD = 0.05  # a flick under torque deflects an antenna 5-6°
STEADY_RAD = 0.005  # the command moving more than this between two samples: the motor may lag it, no tap is read
STEADY_S = 0.5  # nor until the command has rested this long


class AntennaWiggle:
    """Polls the antenna encoders on its own thread. Armed, a deflection off `commanded()` from where they were when
    it was armed calls `on_wiggle` once (from this thread) and disarms. While `holding()` (a scene plays), an antenna
    kept HOLD_RAD off `commanded()` for HOLD_SAMPLES calls `on_hold` once; while `tapping()` too (a rehearsal waits
    for the human), a tap calls `on_tap`."""

    def __init__(
        self,
        mini: Any,
        on_wiggle: Callable[[], None],
        holding: Callable[[], bool] = lambda: False,
        commanded: Callable[[], Sequence[float]] = lambda: (),
        on_hold: Callable[[], None] = lambda: None,
        tapping: Callable[[], bool] = lambda: False,
        on_tap: Callable[[], None] = lambda: None,
    ) -> None:
        self._mini = mini
        self._on_wiggle = on_wiggle
        self._holding, self._commanded, self._on_hold = holding, commanded, on_hold
        self._tapping, self._on_tap = tapping, on_tap
        self._held = 0  # consecutive samples off the command
        self._push: float | None = None  # the deepest deflection of a push under way
        self._last: tuple[float, ...] = ()  # the command at the last sample, at rest since `_steady_since`
        self._steady_since = 0.0
        self._lock = threading.Lock()
        self._armed = False
        self._baseline: tuple[float, ...] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name='antennas', daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    @property
    def armed(self) -> bool:
        """Whether a wiggle would wake the robot now."""
        return self._armed

    def arm(self) -> None:
        with self._lock:
            self._armed, self._baseline = True, None

    def disarm(self) -> None:
        with self._lock:
            self._armed = False

    def check(self, positions: tuple[float, ...]) -> bool:
        """Whether these positions fire the wake (and disarm it): an antenna ANTENNA_WAKE_RAD further off its command
        than when armed. Only a hand pushes it away from where its motor holds it: one coming back toward its command
        (a hand letting go of it) moves the baseline instead. With no command known, any way counts."""
        with self._lock:
            if not self._armed:
                return False
            if self._baseline is None:
                self._baseline = positions
                return False
            command = tuple(self._commanded())
            rest = command if len(command) == len(positions) else self._baseline
            moves = list(zip(positions, self._baseline, rest, strict=True))
            if max(abs(p - c) - abs(b - c) for p, b, c in moves) <= config.ANTENNA_WAKE_RAD:
                self._baseline = tuple(p if abs(p - c) < abs(b - c) else b for p, b, c in moves)
                return False
            self._armed = False
            return True

    def held(self, positions: tuple[float, ...], commanded: Sequence[float]) -> bool:
        """Whether these positions complete a hold (and reset it): HOLD_SAMPLES in a row off the command."""
        self._held = self._held + 1 if _off(positions, commanded) > HOLD_RAD else 0
        if self._held < HOLD_SAMPLES:
            return False
        self._held = 0
        return True

    def tapped(self, positions: tuple[float, ...], commanded: Sequence[float], now: float) -> bool:
        """Whether these positions end a tap: an antenna pushed TAP_RAD off a command at rest, then let go. One
        that reached HOLD_RAD is a hold, never a tap. Each `tapping()` spell waits STEADY_S before its first."""
        command = tuple(commanded)
        if len(command) != len(self._last) or _off(command, self._last) > STEADY_RAD:
            self._steady_since, self._push = now, None
        self._last = command
        off = _off(positions, command)
        if now - self._steady_since < STEADY_S:
            return False
        if off > TAP_RAD:
            self._push = max(off, self._push or 0.0)
            return False
        push, self._push = self._push, None
        return push is not None and push < HOLD_RAD

    def sample(self, positions: tuple[float, ...], now: float) -> None:  # one reading: a wake, a hold or a tap
        if self.check(positions):
            logger.info('[WAKE] antenna moved')
            self._on_wiggle()
            return
        if not self._holding():
            self._held, self._push, self._last = 0, None, ()
            return
        commanded = list(self._commanded())
        if self.held(positions, commanded):
            logger.info('[SCENE] antenna held')
            self._on_hold()
        elif not self._tapping():
            self._push, self._last = None, ()
        elif self.tapped(positions, commanded, now):
            logger.info('[SCENE] antenna tapped')
            self._on_tap()

    def _loop(self) -> None:
        while not self._stop.wait(1.0 / config.ANTENNA_POLL_HZ):
            try:
                right, left = self._mini.get_present_antenna_joint_positions()[:2]
                self.sample((float(right), float(left)), time.monotonic())
            except Exception as exc:  # noqa: BLE001 — a failed read is a missed sample, not a dead button
                logger.debug('antenna read failed: %s', exc)


def _off(positions: Sequence[float], commanded: Sequence[float]) -> float:  # how far from the command (0: none)
    if len(commanded) != len(positions):
        return 0.0
    return max(abs(p - c) for p, c in zip(positions, commanded, strict=True))
