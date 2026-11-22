"""The robot's ear lent to an app (the manette's « voix »): how loud its microphone is over the room's own quiet, a
number from 0 to 1 handed on about 20 times a second while it is lent. No sound leaves the robot. Fed by the
session's microphone pump, on the event loop: a sum of squares per chunk."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable

import numpy as np

from robot_agent import config

FRAME_S = 0.05  # one level per frame
HISTORY_S = 5.0  # the quiet is the 20th percentile of this much: a word said meanwhile does not count
PERCENTILE = 20.0
DEFAULT_DB, MAX_QUIET_DB = -60.0, -40.0  # before anything is learnt; never louder (someone talked through it all)
MARGIN_DB, SPAN_DB = 6.0, 30.0  # level 0 up to 6 dB over the quiet, 1 from 36 dB over it


class Ear:
    def __init__(self, send: Callable[[float], None], sr: int = config.GEMINI_INPUT_SR) -> None:
        self._send, self._frame = send, max(1, int(sr * FRAME_S))
        self._squares, self._samples = 0.0, 0
        self._quiets: deque[float] = deque(maxlen=int(HISTORY_S / FRAME_S))
        self.quiet_db = DEFAULT_DB
        self.lent = False

    def lend(self, on: bool) -> None:
        """Lent, the quiet learnt so far stays as it is: a hum held ten seconds is no new quiet."""
        self.lent = on

    def hear(self, chunk: np.ndarray, own: bool) -> None:
        """A microphone chunk (mono, at the rate given), `own` when the robot itself was audible as it was captured:
        its voice and its moves teach no quiet. Lent, everything is told, the robot's own noise included."""
        self._squares += float(np.dot(chunk, chunk))
        self._samples += chunk.size
        if self._samples < self._frame:
            return
        db = 10 * math.log10(self._squares / self._samples + 1e-12)
        self._squares, self._samples = 0.0, 0
        if self.lent:
            self._send(round(min(1.0, max(0.0, (db - self.quiet_db - MARGIN_DB) / SPAN_DB)), 3))
        elif not own:
            self._quiets.append(db)
            self.quiet_db = min(float(np.percentile(self._quiets, PERCENTILE)), MAX_QUIET_DB)
