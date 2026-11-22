"""What Pollen's robots do while they talk, ported: the head wobbles with the voice, over their breathing.

`SwayRollRT` is reachy_mini's `motion/speech_tapper.py` (1.10.0, the robot's daemon, the same in 1.14), itself ported
from pollen-robotics/reachy_mini_conversation_app (both Apache-2.0): every 50 ms of the audio the robot plays, its
loudness, through a voice detector with hysteresis and an envelope, gates six fixed oscillators (pitch 2.2 Hz, yaw
0.6 Hz, roll 1.3 Hz, and the head's x, y, z). Their tunables and their arithmetic are kept as they wrote them. Only the
timing is ours: `Wobble` places each 50 ms where it plays, on the playback clock, and the mixer reads it at any
instant, the gain interpolated and the oscillators run on that clock: the 30 Hz motion thread sends it smooth where
their daemon steps it every 50 ms. `breathing` is their BreathingMove (the app's moves.py), what the wobble rides on.
Both move the robot only while its microphone is replaced by the comfort noise (MIC_MUTE_WHILE_TALKING:
its motors are not in the echo canceller's reference).
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation

from robot_agent import animations, config
from robot_agent.audio import mono_mix

# speech_tapper.py's tunables, as Pollen set them.
FRAME_MS = 20
HOP_MS = 50
SWAY_MASTER = 1.5
SENS_DB_OFFSET = 4.0
VAD_DB_ON = -35.0
VAD_DB_OFF = -45.0
VAD_ATTACK_MS = 40
VAD_RELEASE_MS = 250
ENV_FOLLOW_GAIN = 0.65
SWAY_DB_LOW = -46.0
SWAY_DB_HIGH = -18.0
LOUDNESS_GAMMA = 0.9
SWAY_ATTACK_MS = 50
SWAY_RELEASE_MS = 250
ATTACK_FR = max(1, int(VAD_ATTACK_MS / HOP_MS))
RELEASE_FR = max(1, int(VAD_RELEASE_MS / HOP_MS))
SWAY_ATTACK_FR = max(1, int(SWAY_ATTACK_MS / HOP_MS))
SWAY_RELEASE_FR = max(1, int(SWAY_RELEASE_MS / HOP_MS))
# Their oscillators, in the order their phases are drawn (seed 7): (Hz, degrees for an angle, millimetres otherwise).
SWAY = {
    'pitch': (2.2, 4.5),
    'yaw': (0.6, 7.5),
    'roll': (1.3, 2.25),
    'x': (0.35, 4.5),
    'y': (0.45, 3.75),
    'z': (0.25, 2.25),
}
_PHASES = dict(zip(SWAY, np.random.default_rng(7).random(len(SWAY)) * 2.0 * math.pi, strict=True))
HOP_S = HOP_MS / 1000.0
RELEASE_S = SWAY_RELEASE_MS / 1000.0  # past the last 50 ms heard, the wobble fades out over this
FADE_S = 2 * HOP_S + RELEASE_S  # from the start of the last 50 ms heard to rest


def _rms_dbfs(x: np.ndarray) -> float:
    rms = np.sqrt(np.mean(x * x, dtype=np.float32) + 1e-12, dtype=np.float32)
    return float(20.0 * math.log10(float(rms) + 1e-12))


def _loudness_gain(db: float, offset: float = SENS_DB_OFFSET) -> float:
    t = min(max((db + offset - SWAY_DB_LOW) / (SWAY_DB_HIGH - SWAY_DB_LOW), 0.0), 1.0)
    return t**LOUDNESS_GAMMA


class SwayRollRT:
    """Pollen's speech tapper: fed float32 mono audio, one gain per 50 ms hop (their `loud * env`, 0 to SWAY_MASTER),
    what scales every oscillator. Their `feed` multiplies the oscillators in; `Wobble.offset` does, at any instant."""

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = int(sample_rate)
        self.frame = int(self.sample_rate * FRAME_MS / 1000)
        self.hop = int(self.sample_rate * HOP_MS / 1000)
        self.reset()

    def reset(self) -> None:
        self.carry = np.zeros(0, dtype=np.float32)
        self.vad_on, self.vad_above, self.vad_below = False, 0, 0
        self.sway_env, self.sway_up, self.sway_down = 0.0, 0, 0

    def feed(self, pcm: np.ndarray) -> list[float]:
        self.carry = np.concatenate([self.carry, pcm]) if self.carry.size else pcm
        out = []
        while self.carry.size >= self.hop:
            hop, self.carry = self.carry[: self.hop], self.carry[self.hop :]
            db = _rms_dbfs(hop[-self.frame :])  # the hop's last 20 ms: theirs, HOP_MS being over FRAME_MS
            if db >= VAD_DB_ON:
                self.vad_above, self.vad_below = self.vad_above + 1, 0
                self.vad_on = self.vad_on or self.vad_above >= ATTACK_FR
            elif db <= VAD_DB_OFF:
                self.vad_below, self.vad_above = self.vad_below + 1, 0
                self.vad_on = self.vad_on and self.vad_below < RELEASE_FR
            if self.vad_on:
                self.sway_up, self.sway_down = min(SWAY_ATTACK_FR, self.sway_up + 1), 0
                target = self.sway_up / SWAY_ATTACK_FR
            else:
                self.sway_down, self.sway_up = min(SWAY_RELEASE_FR, self.sway_down + 1), 0
                target = 1.0 - self.sway_down / SWAY_RELEASE_FR
            self.sway_env = min(max(self.sway_env + ENV_FOLLOW_GAIN * (target - self.sway_env), 0.0), 1.0)
            out.append(_loudness_gain(db) * SWAY_MASTER * self.sway_env)
        return out


class Wobble:
    """The voice's gains on the playback clock, and the head's offset they make at any instant. Pure: given times."""

    def __init__(self) -> None:
        self._tapper: SwayRollRT | None = None
        self._gains: deque[tuple[float, float]] = deque()  # (when a hop starts playing, its gain), in time order

    def hear(self, samples: np.ndarray, start: float, rate: int) -> None:
        """Audio playing from `start`. A hop begun in the chunk before plays from that much earlier. After a silence,
        the last hop's fade is written out as points (the same curve `gain` drew) and the wobble rises from rest over
        the hop before the first."""
        if self._tapper is None or self._tapper.sample_rate != rate:
            self._tapper = SwayRollRT(rate)
        tapper, gains = self._tapper, self._gains
        begun = tapper.carry.size
        for i, gain in enumerate(tapper.feed(mono_mix(np.asarray(samples, dtype=np.float32)))):
            when = start + (i * tapper.hop - begun) / rate
            if not gains:
                gains.append((when - HOP_S, 0.0))
            elif when - gains[-1][0] > FADE_S:
                t, g = gains[-1]
                gains.extend([(t + HOP_S, g), (t + HOP_S + RELEASE_S, 0.0), (when - HOP_S, 0.0)])
            gains.append((when, gain))

    def gain(self, now: float) -> float:
        """Between two hops, from one to the next; past the last, held for its 50 ms, then faded out linearly."""
        gains = self._gains
        while len(gains) > 1 and gains[1][0] <= now:
            gains.popleft()
        if not gains or now < gains[0][0]:
            return 0.0
        t0, g0 = gains[0]
        if len(gains) > 1:
            t1, g1 = gains[1]
            return g0 + (g1 - g0) * (now - t0) / max(t1 - t0, 1e-9)
        return g0 * (1.0 - min(max((now - t0 - HOP_S) / RELEASE_S, 0.0), 1.0))

    def offset(self, now: float) -> np.ndarray | None:
        """The head's offset at `now` (a 4x4: their create_head_pose), None while the voice adds nothing."""
        gain = self.gain(now)
        if gain <= 0.0:
            return None
        v = {key: gain * amp * math.sin(2.0 * math.pi * hz * now + _PHASES[key]) for key, (hz, amp) in SWAY.items()}
        out = np.eye(4)
        out[:3, :3] = Rotation.from_euler('xyz', [v['roll'], v['pitch'], v['yaw']], degrees=True).as_matrix()
        out[:3, 3] = (v['x'] / 1000.0, v['y'] / 1000.0, v['z'] / 1000.0)
        return out

    def cut(self, now: float) -> None:
        """Nothing more plays (a flush, the sleep, the body lent): what moves now fades out."""
        self._gains = deque([(now, self.gain(now))])
        if self._tapper is not None:
            self._tapper.reset()

    def until(self) -> float:
        """When the wobble is at rest, if nothing more is heard."""
        return self._gains[-1][0] + HOP_S + RELEASE_S if self._gains else -math.inf


def over(head: np.ndarray, offset: np.ndarray) -> np.ndarray:
    """Their compose_world_offset, in the body's frame: the offset turns the head about its own centre and moves it."""
    out = head.copy()
    out[:3, :3] = offset[:3, :3] @ head[:3, :3]
    out[:3, 3] = head[:3, 3] + offset[:3, 3]
    return out


def breathing(t: float) -> animations.Channels:
    """Their BreathingMove past its first second: the head rises and falls 5 mm at 0.1 Hz, the antennas swing 15° at
    0.5 Hz both alike, about their joints' zero (10° forward of our neutral)."""
    return animations.ch(z=0.005 * math.sin(0.2 * math.pi * t), forward=10.0 + 15.0 * math.sin(math.pi * t))


class Tap:
    """The speaker, the audio it is given handed to the body as it is queued, as Pollen's daemon taps its output
    pipeline: the session's voice and a scene's lines alike. Its own FIFO clock, as the session's,
    the speaker SPEAKER_DELAY_S behind it. Everything else goes to the speaker as is."""

    def __init__(self, audio: Any, body: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self._audio, self._body, self._clock = audio, body, clock
        self._rate = int(audio.get_output_samplerate())
        self._end = 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._audio, name)

    def push_audio_sample(self, samples: np.ndarray) -> None:
        self._audio.push_audio_sample(samples)
        if samples.size:
            start = max(self._end, self._clock())
            self._end = start + len(samples) / self._rate
            self._body.wobble(samples, start + config.SPEAKER_DELAY_S, self._rate)

    def clear_output(self) -> None:
        self._audio.clear_output()
        self._end = min(self._end, self._clock())
        self._body.stop_wobble()
