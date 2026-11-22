"""Resampling, PCM, the comfort noise, the microphone drain and its clock."""

from __future__ import annotations

import itertools
import logging
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.signal import resample_poly

from robot_agent import audio, config
from robot_agent.audio import ComfortNoise, StreamResampler, drain_mic, float_to_pcm, mono_mix, pcm_to_float


def test_the_stream_resampler_matches_resample_poly_with_no_seams() -> None:
    signal = np.sin(np.arange(4800) * 0.05).astype(np.float32)  # 0.1 s at 48 kHz
    resampler = StreamResampler(48000, 16000)
    streamed = np.concatenate([resampler.process(chunk) for chunk in np.split(signal, 30)])
    whole = resample_poly(signal, 1, 3)
    delay = 10  # half the filter, at the output rate
    assert np.allclose(streamed[delay:], whole[: streamed.size - delay], atol=1e-3)


def test_tiny_chunks_can_come_out_empty_and_a_reset_forgets_the_tail() -> None:
    resampler = StreamResampler(24000, 16000)
    sizes = [resampler.process(np.ones(1, dtype=np.float32)).size for _ in range(30)]
    assert 0 in sizes and sum(sizes) == 20
    resampler.reset()
    assert resampler.process(np.zeros(3, dtype=np.float32)).size == 2


def test_pcm_round_trip_and_mono() -> None:
    samples = np.array([0.0, 0.5, -0.5, 0.999], dtype=np.float32)
    assert np.allclose(pcm_to_float(float_to_pcm(samples)), samples, atol=1e-4)
    assert pcm_to_float(b'\x01').size == 0  # an odd byte is dropped, not an error
    assert np.allclose(mono_mix(np.array([[1.0, 0.0], [0.0, 1.0]])), [0.5, 0.5])


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def test_comfort_noise_stands_in_at_the_rooms_level_and_lets_the_mic_back() -> None:
    noise = ComfortNoise(16000, seed=1)
    room = (0.003 * np.random.default_rng(2).standard_normal(16000 * 2)).astype(np.float32)
    for chunk in np.split(room, 125):
        assert noise.process(chunk, robot_audible=False) is chunk  # quiet robot: the microphone, untouched
    assert 0.002 < noise.level < 0.004
    voice = np.full(1600, 0.5, dtype=np.float32)  # the robot's own motors, loud
    stand_in = np.concatenate([noise.process(voice, robot_audible=True) for _ in range(10)])
    assert _rms(stand_in[1600:]) < 0.01  # past the 50 ms fade, only the stand-in
    back = [noise.process(voice, robot_audible=False) for _ in range(3)]
    assert np.array_equal(back[-1], voice)


def test_a_stream_that_opens_on_an_audible_robot_starts_on_the_stand_in() -> None:
    loud = np.full(256, 0.5, dtype=np.float32)  # the robot's own motors, from the very first chunk
    assert _rms(ComfortNoise(16000, seed=4).process(loud, robot_audible=True)) < 0.01  # no fade from the mic
    assert ComfortNoise(16000, seed=4).process(loud, robot_audible=False) is loud  # a quiet start: the mic


def test_comfort_noise_is_capped() -> None:
    noise = ComfortNoise(16000, seed=3)
    for _ in range(40):
        noise.process(np.full(1600, 0.5, dtype=np.float32), robot_audible=False)
    assert noise.level <= 0.01


def test_the_drain_stops_once_a_pull_costs_real_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Judged pull by pull: a long backlog of cheap pulls is drained whole, the first live period ends it."""
    clock = [1000.0]
    monkeypatch.setattr(audio, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    pulls = itertools.count(1)

    def read() -> np.ndarray:
        clock[0] += 0.001 if next(pulls) <= 30 else 0.016  # 30 queued periods, cheap but not free; then live
        return np.zeros(256)

    assert drain_mic(read) == 31


def test_the_drain_stops_at_once_on_an_empty_or_a_failing_pull() -> None:
    pulls: list[str] = []

    def empty() -> None:
        pulls.append('empty')

    def broken() -> np.ndarray:
        pulls.append('broken')
        raise OSError('appsink gone')

    assert drain_mic(empty) == 0 and drain_mic(broken) == 0
    assert pulls == ['empty', 'broken']  # one pull each


def test_the_mic_clock_dates_a_backlog_by_the_stream_and_a_live_period_by_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five 10 ms chunks banked while the pump was held 0.2 s: each dated one chunk after the one before, not when
    read. A pull that waits for its period is dated when it returns, and no chunk is dated in the future."""
    clock = [1000.0]
    monkeypatch.setattr(audio, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    costs = iter([0.0001] * 5 + [0.016, 0.0001])

    def read() -> np.ndarray:
        clock[0] += next(costs)
        return np.zeros((160, 2))  # 10 ms at 16 kHz

    mic = audio.MicClock(SimpleNamespace(get_audio_sample=read, get_input_samplerate=lambda: 16000))
    clock[0] += 0.2  # the pump held
    dated = []
    for _ in range(7):
        mic.pull()
        dated.append(mic.captured_at - 1000.0)
    assert dated == pytest.approx([0.01, 0.02, 0.03, 0.04, 0.05, 0.2165, 0.2166])


def test_the_mic_clock_never_dates_a_chunk_older_than_the_appsink_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pump slower than the microphone for long (each 16 ms chunk sent in 24 ms, a congested uplink): the appsink
    fills, then drops its oldest, and no pull waits. The chunk read is the oldest one it holds, 3.2 s old at most:
    dated one chunk after the one before, the date would fall further behind with each chunk dropped."""
    clock = [1000.0]
    monkeypatch.setattr(audio, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def read() -> np.ndarray:
        clock[0] += 0.0001  # a backlog: no pull waits
        return np.zeros((256, 2))  # 16 ms at 16 kHz

    mic = audio.MicClock(SimpleNamespace(get_audio_sample=read, get_input_samplerate=lambda: 16000))
    behind = []
    for _ in range(1000):  # 24 s
        clock[0] += 0.024 - 0.0001
        mic.pull()
        behind.append(clock[0] - mic.captured_at)
    assert behind[199] == pytest.approx(200 * 0.008)  # the backlog grows by 8 ms a chunk: not full yet
    assert behind[-1] == pytest.approx(config.MIC_APPSINK_BUFFERS * 0.016)  # full: its oldest chunk, no older


def test_the_mic_meter_logs_the_raw_level_once_per_period(caplog, monkeypatch) -> None:
    clock = iter([0.0, 0.5, 1.0, 2.5, 2.6])
    monkeypatch.setattr(audio.time, 'monotonic', lambda: next(clock))
    meter = audio.MicMeter(every_s=2.0)
    tone = np.full(1600, 0.1, dtype=np.float32)  # -20 dBFS
    with caplog.at_level(logging.INFO, logger='robot_agent.audio'):
        meter.add(tone, audible=False, state='idle')
        meter.add(tone, audible=True, state='idle')
        meter.add(tone, audible=False, state='listening')
        meter.add(tone, audible=False, state='listening')
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('[MIC]')]
    assert lines == ['[MIC] rms -20.0 dBFS, peak -20.0 dBFS, robot audible 33%, listening']
