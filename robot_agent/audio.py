"""Audio helpers: the streaming resampler, PCM conversion, the comfort-noise stand-in, the mic drain and clock."""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections.abc import Callable
from math import gcd

import numpy as np
from scipy.signal import firwin, lfilter, resample_poly

from robot_agent import config
from robot_agent.adapters import Audio

# Above this max(up, down) the stateful filter costs more than the chunk lasts (44.1 kHz ratios are in the
# hundreds): such streams are resampled chunk by chunk.
_MAX_STREAM_RATIO = 12


logger = logging.getLogger(__name__)


class StreamResampler:
    """Resample a stream chunk by chunk with no seam between chunks; one instance per stream.

    It keeps resample_poly's filter state across chunks: resampling each chunk alone clicks at every
    16 ms seam, in the very audio Gemini's VAD listens to. A tiny chunk can come out empty.
    """

    def __init__(self, sr_in: int, sr_out: int) -> None:
        g = gcd(sr_in, sr_out)
        self._up, self._down = sr_out // g, sr_in // g
        self._passthrough = self._up == self._down
        self._per_chunk = max(self._up, self._down) > _MAX_STREAM_RATIO
        width = max(self._up, self._down)
        # resample_poly's own design: a Kaiser low-pass at the narrower Nyquist, gain `up` for the zeros.
        streaming = not (self._passthrough or self._per_chunk)
        self._h = firwin(20 * width + 1, 1.0 / width, window=('kaiser', 5.0)) * self._up if streaming else None
        self.reset()

    def reset(self) -> None:
        """Forget the filter's history: after a flush, its tail belongs to audio that was dropped."""
        self._zi = np.zeros(self._h.size - 1) if self._h is not None else None
        self._phase = 0  # index, in the next chunk's upsampled samples, of the first one to keep

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self._passthrough or x.size == 0:
            return x
        if self._h is None:
            return resample_poly(x, self._up, self._down).astype(np.float32)
        stuffed = np.zeros(x.size * self._up)
        stuffed[:: self._up] = x
        y, self._zi = lfilter(self._h, 1.0, stuffed, zi=self._zi)
        out = y[self._phase :: self._down]
        self._phase = (self._phase - stuffed.size) % self._down
        return out.astype(np.float32)


def pcm_to_float(pcm: bytes) -> np.ndarray:
    """Little-endian int16 PCM to float32 in [-1, 1]; a trailing odd byte is dropped."""
    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0


def float_to_pcm(samples: np.ndarray) -> bytes:
    return np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16).tobytes()


def mono_mix(samples: np.ndarray) -> np.ndarray:
    """A (frames, channels) array averaged to mono; a 1-D array as it is."""
    return samples.mean(axis=1) if samples.ndim == 2 else samples


class ComfortNoise:
    """What Gemini hears instead of the microphone while the robot itself is the noise.

    Not silence: digital silence lowers the VAD's threshold, and the motors coming back then sound like
    someone starting to speak. It plays the room's own level, learned while the robot is quiet (20th
    percentile of the last 2 s in 100 ms frames, so a word does not count), as brown noise with 50 ms fades.
    One instance per stream.
    """

    _FRAME_S = 0.1
    _HISTORY_S = 2.0
    _PERCENTILE = 20.0
    _FADE_S = 0.05
    _DEFAULT_RMS = 10 ** (-60 / 20)  # before anything is learned
    _MAX_RMS = 10 ** (-40 / 20)  # a room inflated by someone talking must not become a loud hiss
    _CORNER_HZ = 40.0

    def __init__(self, sr: int, seed: int | None = None) -> None:
        self._sr = sr
        self._rng = np.random.default_rng(seed)
        self._frame = max(1, int(sr * self._FRAME_S))
        self._pending: list[np.ndarray] = []
        self._pending_n = 0
        self._levels: list[float] = []
        self._max_levels = int(self._HISTORY_S / self._FRAME_S)
        self.level = self._DEFAULT_RMS
        self._mix: float | None = None  # 0 = the microphone, 1 = the stand-in; None before the first chunk
        self._pole = float(np.exp(-2 * np.pi * self._CORNER_HZ / sr))
        self._state = 0.0

    def process(self, chunk: np.ndarray, *, robot_audible: bool) -> np.ndarray:
        """The chunk to send: the microphone, the stand-in, or a fade between the two. A stream that opens on an
        audible robot starts on the stand-in: a fade from the microphone would send it the robot's own noise."""
        if chunk.size == 0:
            return chunk
        if self._mix is None:
            self._mix = 1.0 if robot_audible else 0.0
        if not robot_audible and self._mix == 0.0:
            self._learn(chunk)
            return chunk
        step = chunk.size / (self._FADE_S * self._sr)
        end = min(1.0, self._mix + step) if robot_audible else max(0.0, self._mix - step)
        ramp = np.linspace(self._mix, end, chunk.size, dtype=np.float32)
        self._mix = end
        return (chunk * (1.0 - ramp) + self._noise(chunk.size) * ramp).astype(np.float32)

    def _learn(self, chunk: np.ndarray) -> None:
        self._pending.append(chunk)
        self._pending_n += chunk.size
        if self._pending_n < self._frame:
            return
        joined = np.concatenate(self._pending)
        whole = joined.size // self._frame * self._frame
        frames = joined[:whole].reshape(-1, self._frame).astype(np.float64)
        rest = joined[whole:]
        self._pending = [rest] if rest.size else []
        self._pending_n = rest.size
        self._levels.extend(np.sqrt((frames**2).mean(axis=1)).tolist())
        del self._levels[: -self._max_levels]
        self.level = min(float(np.percentile(self._levels, self._PERCENTILE)), self._MAX_RMS)

    def _noise(self, n: int) -> np.ndarray:
        # A leaky integrator over white noise: unit variance once scaled by sqrt(1 - p²).
        out, _ = lfilter([1.0], [1.0, -self._pole], self._rng.standard_normal(n), zi=[self._state * self._pole])
        self._state = float(out[-1])
        return (out * np.sqrt(1.0 - self._pole**2) * self.level).astype(np.float32)


def drain_mic(read: Callable[[], object]) -> int:
    """Pull and drop the microphone's backlog; returns how many chunks. Blocking: call it in a thread.

    A running microphone never returns None, it hands over the next period: the backlog is gone once a pull
    costs real time. The budget and the chunk cap are safety nets.
    """
    deadline = time.monotonic() + config.MIC_DRAIN_BUDGET_S
    dropped = 0
    with contextlib.suppress(Exception):
        while dropped < config.MIC_DRAIN_MAX_CHUNKS and time.monotonic() < deadline:
            started = time.monotonic()
            if read() is None:
                break
            dropped += 1
            if time.monotonic() - started >= config.MIC_DRAIN_CAUGHT_UP_S:
                break
    return dropped


class MicClock:
    """The microphone, and when its last chunk was captured: a pull hands over bytes, no timestamp. A pull that
    waited for its period is fresh, captured now; one that did not comes from a backlog (the pump fell behind: a
    slow send, a starved pool), captured one chunk after the one before, but never before the oldest chunk the
    appsink holds: a full one drops its oldest."""

    def __init__(self, audio: Audio) -> None:
        self._read, self._sr = audio.get_audio_sample, audio.get_input_samplerate()
        self.captured_at = time.monotonic()  # the end of the last chunk pulled

    @classmethod
    def drained(cls, audio: Audio) -> MicClock:
        """Blocking: drop the backlog (`drain_mic`); the stream is then caught up, and the clock starts."""
        logger.debug('mic: %d stale chunks dropped', drain_mic(audio.get_audio_sample))
        return cls(audio)

    def pull(self) -> np.ndarray | None:
        """Blocking: the next chunk (None: none came), with `captured_at` moved to its end."""
        started = time.monotonic()
        sample = self._read()
        now = time.monotonic()
        if sample is not None:
            fresh, length = now - started >= config.MIC_DRAIN_CAUGHT_UP_S, len(sample) / self._sr
            oldest = now - config.MIC_APPSINK_BUFFERS * length  # the end of the oldest chunk a full appsink holds
            self.captured_at = now if fresh else min(now, max(oldest, self.captured_at + length))
        return sample


class MicMeter:
    """MIC_LEVEL_LOG: the raw microphone's level every few seconds, to tell the room from the robot's own noise."""

    def __init__(self, every_s: float = 2.0) -> None:
        self._every_s = every_s
        self._reset(time.monotonic())

    def _reset(self, now: float) -> None:
        self._since, self._squares, self._samples, self._peak, self._audible = now, 0.0, 0, 0.0, 0

    def add(self, chunk: np.ndarray, *, audible: bool, state: str) -> None:
        if chunk.size:
            self._squares += float(np.dot(chunk, chunk))
            self._samples += chunk.size
            self._peak = max(self._peak, float(np.abs(chunk).max()))
            self._audible += chunk.size if audible else 0
        now = time.monotonic()
        if now - self._since < self._every_s or not self._samples:
            return
        rms = math.sqrt(self._squares / self._samples)
        logger.info(
            '[MIC] rms %.1f dBFS, peak %.1f dBFS, robot audible %d%%, %s',
            20 * math.log10(rms + 1e-12),
            20 * math.log10(self._peak + 1e-12),
            round(100 * self._audible / self._samples),
            state,
        )
        self._reset(now)
