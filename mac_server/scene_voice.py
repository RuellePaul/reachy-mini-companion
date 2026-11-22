"""A scene's voices: each segment rendered once by Gemini's speech model, then served from the cache forever.

One Interactions request per segment (voice and style per request; a Live connection has one voice), at most three
at a time across every scene, the same key never twice at once. The WAV is stored as received (24 kHz mono s16le)
with a sidecar: its duration, where the voice starts and stops and where it pauses (an RMS pass), which the timeline
is laid on.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import re
import threading
import time
import wave
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from scipy.signal import resample_poly

from mac_server import gemini
from mac_server.gemini import GeminiError
from mac_server.scene_text import TTS_MODEL

log = logging.getLogger(__name__)

FALLBACK_MODEL = 'gemini-3.8-flash-lite-tts'  # only once the day's quota of TTS_MODEL is spent
CONCURRENCY = 3  # three at once held; more got 429
TIMEOUT_S = 60.0
QUOTA_WAIT_S = 120.0  # per segment, waiting out per-minute 429s
SERVER_ATTEMPTS = 3
GAP_MIN_S = 0.15  # a quieter run this long inside the voiced span is a pause (between words: shorter)
_KEY = re.compile(r'^[0-9a-f]{40}$')
_MEASURED = ('duration', 'onset', 'offset', 'gaps')
_PACIFIC = ZoneInfo('America/Los_Angeles')  # where Google's daily quotas reset, at midnight
NO_KEY = 'GEMINI_API_KEY manque dans le .env du Mac\xa0: les voix ne peuvent pas être rendues.'


def measure(wav: bytes) -> dict[str, Any]:
    """Duration; the first and last 20 ms window louder than max(0.01, 10 % of the loudest); and the pauses between
    them (quieter runs of GAP_MIN_S or more), in seconds."""
    with wave.open(io.BytesIO(wav)) as reader:
        rate, frames = reader.getframerate(), reader.readframes(reader.getnframes())
    samples = np.frombuffer(frames, dtype='<i2').astype(np.float64) / 32768.0
    window = max(1, rate // 50)
    count = samples.size // window
    rms = np.sqrt(np.mean(samples[: count * window].reshape(count, window) ** 2, axis=1))
    loud = np.flatnonzero(rms > max(0.01, 0.1 * float(rms.max(initial=0.0))))
    at = [round(int(n) * window / rate, 3) for n in loud]  # when each loud window starts
    end = round(samples.size / rate, 3)
    gaps = [
        [at[i] + window / rate, at[i + 1]] for i in np.flatnonzero((np.diff(loud) - 1) * window >= GAP_MIN_S * rate)
    ]
    onset, offset = (at[0], min(round(at[-1] + window / rate, 3), end)) if at else (0.0, end)
    return {'duration': end, 'onset': onset, 'offset': offset, 'gaps': [[round(a, 3), b] for a, b in gaps]}


def _write(path: Path, data: bytes) -> None:
    """Atomically: a reader sees no file or the whole file."""
    tmp = path.with_name(f'.{path.name}.{threading.get_ident()}.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


class Voices:
    def __init__(
        self,
        cache_dir: Path,
        api_key: str,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.dir = cache_dir
        self._api_key = api_key
        self._sleep, self._wall = sleep, wall
        self._lock = threading.Lock()
        self._inflight: dict[str, Future[None]] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._spent: dict[str, float] = {}  # model -> when its daily quota comes back (wall clock)
        self._refused: dict[str, str] = {}  # key -> why Gemini refused it last (a 4xx): asking again may not help

    def path(self, key: str) -> Path | None:
        """The segment's WAV, when it and its sidecar are on disk; never a path from a malformed key."""
        path = self.dir / f'{key}.wav' if _KEY.match(key) else None
        return path if path is not None and path.is_file() and (self.dir / f'{key}.json').is_file() else None

    def meta(self, key: str) -> dict[str, Any] | None:
        """The sidecar, None when the segment is not rendered: what decides it, for playing and for rendering. One
        written before a measure existed (no onset; no gaps) is measured again and completed; one unreadable is a
        segment to render again."""
        wav = self.path(key)
        if wav is None:
            return None
        sidecar = self.dir / f'{key}.json'
        try:
            meta = json.loads(sidecar.read_text(encoding='utf-8'))
            if isinstance(meta, dict) and not set(_MEASURED) <= meta.keys():
                meta |= measure(wav.read_bytes())
                _write(sidecar, json.dumps(meta).encode())
        except (OSError, ValueError, wave.Error) as exc:
            meta = exc
        numbers = isinstance(meta, dict) and all(isinstance(meta.get(k), int | float) for k in _MEASURED[:3])
        if numbers and isinstance(meta, dict) and isinstance(meta.get('gaps'), list):
            return meta
        log.warning('scene audio %s unreadable (%s): to render again', key, meta)
        return None

    def render(self, key: str, voice: str, style: str, said: str) -> Future[None]:
        """Render one segment in the background; the same key already on its way shares its future."""
        with self._lock:
            future = self._inflight.get(key)
            if future is not None:
                return future
            self._pool = self._pool or ThreadPoolExecutor(CONCURRENCY, thread_name_prefix='scene-voice')
            future = self._inflight[key] = self._pool.submit(self._render, key, voice, style, said)
        future.add_done_callback(lambda _: self._forget(key))  # unlocked: a future already done calls it at once
        return future

    def failing(self, keys: list[str]) -> str | None:
        """Why these segments cannot be rendered now, in French: no key, every model's daily quota spent, or Gemini
        refusing one of them the last time it was asked; None while a render may work."""
        if not self._api_key:
            return NO_KEY
        if all(self._spent.get(model, 0.0) > self._wall() for model in (TTS_MODEL, FALLBACK_MODEL)):
            return self._spent_until()
        return next(filter(None, map(self._refused.get, keys)), None)

    def close(self) -> None:
        with self._lock:
            pool = self._pool
        if pool is not None:  # unlocked: each future it cancels forgets itself in this thread
            pool.shutdown(wait=False, cancel_futures=True)

    def _forget(self, key: str) -> None:
        with self._lock:
            self._inflight.pop(key, None)

    def _render(self, key: str, voice: str, style: str, said: str) -> None:
        if self.meta(key) is not None:
            return
        started = time.monotonic()
        try:
            wav, model = self._synthesize(voice, style, said)
        except GeminiError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                self._refused[key] = str(exc)
            raise
        self._refused.pop(key, None)
        meta = {**measure(wav), 'model': model, 'voice': voice, 'rendered_at': round(self._wall())}
        self.dir.mkdir(parents=True, exist_ok=True)
        _write(self.dir / f'{key}.wav', wav)
        _write(self.dir / f'{key}.json', json.dumps(meta).encode())  # last: its presence means rendered
        log.info(
            'scene voice %s rendered by %s: %.1f s in %.1f s',
            voice,
            model,
            meta['duration'],
            time.monotonic() - started,
        )

    def _synthesize(self, voice: str, style: str, said: str) -> tuple[bytes, str]:
        if not self._api_key:
            raise GeminiError(NO_KEY, 503)
        text: dict[str, Any] = {'type': 'text', 'text': said}
        if style:  # obeyed, never read aloud (the speech model reads its text verbatim)
            text['annotations'] = [{'type': 'speech_metadata', 'style': style}]
        for model in (TTS_MODEL, FALLBACK_MODEL):
            if self._spent.get(model, 0.0) > self._wall():
                continue
            body = {
                'model': model,
                'input': [{'type': 'user_input', 'content': [text]}],
                'response_format': {'type': 'audio', 'mime_type': 'audio/wav'},
                'generation_config': {'speech_config': [{'voice': voice}]},
            }
            try:
                return self._attempts(body), model
            except GeminiError as exc:
                if not exc.per_day:
                    raise
                self._spent[model] = self._next_reset()
                log.warning('scene voices: the daily quota of %s is spent', model)
        raise GeminiError(self._spent_until(), 429, per_day=True)

    def _spent_until(self) -> str:
        back = datetime.fromtimestamp(min(self._spent.values())).strftime('%H h %M')
        return f'Le quota de voix Gemini du jour est épuisé\xa0: il revient vers {back}. Les répliques déjà rendues restent.'

    def _attempts(self, body: dict[str, Any]) -> bytes:
        """One segment from one model: per-minute 429s waited out (up to QUOTA_WAIT_S), 5xx and network retried."""
        waited, failures = 0.0, 0
        while True:
            try:
                answer = gemini.post(self._api_key, 'interactions', body, TIMEOUT_S)
            except GeminiError as exc:
                if exc.status == 429 and not exc.per_day and waited < QUOTA_WAIT_S:
                    delay = min(exc.retry_after_s or 8.0, 30.0, QUOTA_WAIT_S - waited)
                elif exc.status >= 500 and failures < SERVER_ATTEMPTS - 1:
                    failures += 1
                    delay = 2.0 * failures
                else:
                    raise
                waited += delay if exc.status == 429 else 0.0
                self._sleep(delay)
                continue
            audio = [
                part
                for step in answer.get('steps') or []
                if isinstance(step, dict) and step.get('type') == 'model_output'
                for part in step.get('content') or []
                if isinstance(part, dict) and part.get('type') == 'audio' and part.get('data')
            ]
            if not audio:
                raise GeminiError("Gemini a répondu sans voix\xa0: la réplique n'a pas été rendue.")
            wav = base64.b64decode(audio[-1]['data'])
            if not wav.startswith(b'RIFF'):  # asked for audio/wav
                raise GeminiError('Gemini a renvoyé une voix illisible.')
            return wav

    def _next_reset(self) -> float:
        now = datetime.fromtimestamp(self._wall(), _PACIFIC)
        return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def at_rate(wav: bytes, rate: int | None) -> bytes:
    """A mono s16le WAV resampled to `rate` (polyphase, as the robot's StreamResampler); the same bytes at its own."""
    with wave.open(io.BytesIO(wav)) as reader:
        source, frames = reader.getframerate(), reader.readframes(reader.getnframes())
    if rate is None or rate == source:
        return wav
    common = math.gcd(rate, source)
    samples = resample_poly(np.frombuffer(frames, dtype='<i2').astype(np.float32), rate // common, source // common)
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(np.clip(np.round(samples), -32768, 32767).astype('<i2').tobytes())
    return out.getvalue()
