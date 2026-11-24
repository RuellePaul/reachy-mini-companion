"""The robot's ear lent to an app (the manette's « voix »): the room's quiet learnt before the hold, then one level
per 50 ms from that quiet up, whatever the size of the microphone's chunks."""

from __future__ import annotations

import numpy as np
import pytest

from robot_agent import ear

SR = 16000


def noise(db: float, samples: int = SR // 20, seed: int = 1) -> np.ndarray:
    """White noise at `db` dBFS (RMS)."""
    x = np.random.default_rng(seed).standard_normal(samples).astype(np.float32)
    return x / np.sqrt(np.mean(x**2)) * 10 ** (db / 20)


def learnt(db: float = -55.0) -> tuple[ear.Ear, list[float]]:
    sent: list[float] = []
    lent = ear.Ear(sent.append, SR)
    for _ in range(40):
        lent.hear(noise(db), own=False)
    return lent, sent


def test_it_learns_the_rooms_quiet_while_it_is_not_lent_and_a_word_said_does_not_count() -> None:
    sent: list[float] = []
    lent = ear.Ear(sent.append, SR)
    assert lent.quiet_db == ear.DEFAULT_DB
    for n in range(100):  # a quiet room, someone speaking one frame in ten
        lent.hear(noise(-25.0 if n % 10 == 0 else -55.0), own=False)
    assert lent.quiet_db == pytest.approx(-55.0, abs=0.5)
    assert sent == []  # nothing is told unless it is lent


def test_the_robots_own_noise_teaches_no_quiet_and_a_room_talked_through_is_capped() -> None:
    lent, _ = learnt(-55.0)
    for _ in range(100):
        lent.hear(noise(-30.0), own=True)  # its voice, its motors
    assert lent.quiet_db == pytest.approx(-55.0, abs=0.5)
    for _ in range(100):
        lent.hear(noise(-20.0), own=False)  # people talking through the whole history
    assert lent.quiet_db == ear.MAX_QUIET_DB


def test_lent_it_tells_how_loud_the_room_is_over_its_quiet() -> None:
    lent, sent = learnt(-55.0)
    lent.lend(True)
    for db in (-55.0, -49.5, -34.0, -19.0, -5.0):
        lent.hear(noise(db), own=False)
    assert sent[:2] == [0.0, 0.0]  # the quiet, and up to 6 dB over it
    assert sent[2] == pytest.approx(0.5, abs=0.02)  # 21 dB over the quiet
    assert sent[3:] == [1.0, 1.0]  # 36 dB over it, and more


def test_lent_the_quiet_stays_as_it_was_and_the_robots_own_noise_is_told_too() -> None:
    lent, sent = learnt(-55.0)
    lent.lend(True)
    for _ in range(200):  # a hum held ten seconds
        lent.hear(noise(-30.0), own=False)
    assert lent.quiet_db == pytest.approx(-55.0, abs=0.5)
    assert sent[0] == pytest.approx(sent[-1]) == pytest.approx((25 - ear.MARGIN_DB) / ear.SPAN_DB, abs=0.02)
    lent.hear(noise(-30.0), own=True)
    assert len(sent) == 201  # its motors are heard as the rest: the app allows for them
    lent.lend(False)
    lent.hear(noise(-30.0), own=False)
    assert len(sent) == 201


def test_one_level_per_frame_whatever_the_chunks() -> None:
    lent, sent = learnt()
    lent.lend(True)
    for _ in range(25):  # 16 ms chunks: 0.4 s
        lent.hear(noise(-30.0, samples=256), own=False)
    assert len(sent) == 6  # a level each time 50 ms are in: every fourth chunk
    lent.hear(np.zeros(0, dtype=np.float32), own=False)  # a chunk the resampler left empty
    assert len(sent) == 6
