"""Pollen's wobble and breathing while the robot talks: the port against their own numbers, the timing on the playback
clock, the mixer and the motion thread around it, the speaker's tap."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from robot_agent import animations, config, wobble
from robot_agent.motion import BLEND_S, Mixer, MotionRunner
from robot_agent.wobble import FADE_S, HOP_S, SWAY, SwayRollRT, Tap, Wobble
from tests.robot.conftest import FakeAudio

RATE = 16000
# reachy_mini 1.10.0's motion/speech_tapper.py, fed _speech() in 333-sample chunks: its loud * env per hop, read back
# from its pitch, yaw and roll outputs (each divided by its own oscillator). Loud, near silence (the detector lets go
# after 5 hops), between its thresholds (it stays off, the envelope dies), then a quieter voice.
POLLEN_GAINS = [
    *[0.0] * 4,
    *[0.946724, 1.301634, 1.454407, 1.484042, 1.494415, 1.498045, 1.450655, 1.249831, 1.499916, 1.499971],
    *[1.49999, 1.499996, 1.499999, 1.338902],
    *[0.0] * 10,
    *[0.009689, 0.002655, 0.000368, 0.0, 6.6e-05, 4.4e-05, 1.8e-05, 6e-06, 1e-06, 0.0, 0.0, 0.0],
    *[0.715571, 1.016031, 1.014671, 0.781252, 0.455319, 0.640233, 0.972127, 1.136888, 1.142722, 0.990784],
]
# Their oscillators' phases, read from their SwayRollRT (seed 7).
POLLEN_PHASES = [3.927590651355011, 5.637360571650786, 4.873776931938056, 1.4150185072200883, 1.8860003910648933]


def _speech(seconds: float = 2.5) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    level = np.select([t < 0.2, t < 0.9, t < 1.4, t < 2.0], [0.0, 0.3, 0.002, 0.02], 0.05)
    return (level * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)) * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _voice(seconds: float, level: float = 0.3) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _chunks(x: np.ndarray, size: int) -> list[np.ndarray]:
    return [x[i : i + size] for i in range(0, x.size, size)]


def _angle(pose: np.ndarray) -> float:
    return float(np.degrees(np.linalg.norm(Rotation.from_matrix(pose[:3, :3]).as_rotvec())))


def test_the_tapper_gives_pollens_own_gains() -> None:
    tapper = SwayRollRT(RATE)
    gains = [g for chunk in _chunks(_speech(), 333) for g in tapper.feed(chunk)]
    assert gains == pytest.approx(POLLEN_GAINS, abs=2e-6)
    assert [wobble._PHASES[key] for key in ('pitch', 'yaw', 'roll', 'x', 'y')] == pytest.approx(POLLEN_PHASES)
    assert SWAY == {
        'pitch': (2.2, 4.5),
        'yaw': (0.6, 7.5),
        'roll': (1.3, 2.25),
        'x': (0.35, 4.5),
        'y': (0.45, 3.75),
        'z': (0.25, 2.25),
    }


def test_each_hop_moves_the_head_as_pollens_oscillators_do_when_it_plays() -> None:
    w = Wobble()
    w.hear(_voice(1.0), 100.0, RATE)
    gains = SwayRollRT(RATE).feed(_voice(1.0))
    for k in (3, 9, 15):
        now = 100.0 + k * HOP_S
        v = {
            key: gains[k] * amp * math.sin(2 * math.pi * hz * now + wobble._PHASES[key])
            for key, (hz, amp) in SWAY.items()
        }
        expected = Rotation.from_euler('xyz', [v['roll'], v['pitch'], v['yaw']], degrees=True).as_matrix()
        offset = w.offset(now)
        assert offset is not None
        assert np.allclose(offset[:3, :3], expected)
        assert np.allclose(offset[:3, 3], [v['x'] / 1000, v['y'] / 1000, v['z'] / 1000])


def test_a_loud_voice_wobbles_the_head_as_much_as_pollens() -> None:
    w = Wobble()
    w.hear(_voice(6.0), 0.0, RATE)
    offsets = [o for n in range(15, 180) if (o := w.offset(n / 30)) is not None]
    assert len(offsets) == 165
    pitch, yaw, roll = np.degrees(np.array([Rotation.from_matrix(o[:3, :3]).as_euler('xyz') for o in offsets])).T[
        [1, 2, 0]
    ]
    assert 6.0 < np.max(np.abs(pitch)) <= 4.5 * 1.5 + 1e-6  # their SWAY_MASTER over each amplitude
    assert 9.0 < np.max(np.abs(yaw)) <= 7.5 * 1.5 + 1e-6
    assert 2.5 < np.max(np.abs(roll)) <= 2.25 * 1.5 + 1e-6


def test_silence_moves_nothing() -> None:
    w = Wobble()
    w.hear(np.zeros(RATE, dtype=np.float32), 0.0, RATE)
    assert all(w.offset(n / 30) is None for n in range(40))


def test_how_the_audio_is_chunked_changes_nothing() -> None:
    whole, chunked = Wobble(), Wobble()
    voice = _speech()
    whole.hear(voice, 5.0, RATE)
    at = 5.0
    for chunk in _chunks(voice, 333):
        chunked.hear(chunk, at, RATE)
        at += chunk.size / RATE
    for n in range(80):
        a, b = whole.offset(5.0 + n / 30), chunked.offset(5.0 + n / 30)
        assert (a is None and b is None) or (a is not None and b is not None and np.allclose(a, b))


def test_past_the_last_sound_it_fades_out_within_its_release() -> None:
    w = Wobble()
    w.hear(_voice(1.0), 0.0, RATE)
    last = 1.0 - HOP_S
    assert w.gain(last + HOP_S) == pytest.approx(w.gain(last))  # held for its 50 ms
    gains = [w.gain(last + HOP_S + i * 0.01) for i in range(26)]
    assert all(b <= a for a, b in itertools.pairwise(gains)) and gains[-1] == 0.0
    assert w.until() == pytest.approx(last + FADE_S - HOP_S)
    assert w.offset(last + FADE_S) is None


def test_a_voice_after_a_silence_rises_from_rest_and_leaves_the_fade_before_it_as_drawn() -> None:
    times = [0.9 + i / 100 for i in range(160)]
    alone = Wobble()
    alone.hear(_voice(1.0), 0.0, RATE)
    faded = [alone.gain(t) for t in times]
    both = Wobble()
    both.hear(_voice(1.0), 0.0, RATE)
    both.hear(_voice(1.0), 2.5, RATE)  # queued long before it plays
    drawn = [both.gain(t) for t in times]
    assert drawn[:155] == pytest.approx(faded[:155])  # to 2.45, the hop before the next voice
    assert both.gain(2.5) > 1.0  # risen in one hop
    late = Wobble()
    late.hear(_voice(1.0), 0.0, RATE)
    first = [late.gain(t) for t in times[:30]]  # the motion thread already into the fade when the next one comes
    late.hear(_voice(1.0), 2.5, RATE)
    assert first + [late.gain(t) for t in times[30:]] == pytest.approx(drawn)


def test_a_cut_drops_what_was_queued_and_fades_from_where_it_is() -> None:
    w = Wobble()
    w.hear(_voice(3.0), 0.0, RATE)
    at_cut = w.gain(1.0)
    w.cut(1.0)
    assert w.gain(1.0) == pytest.approx(at_cut)
    assert w.gain(1.0 + FADE_S - HOP_S) == 0.0 and w.offset(2.0) is None
    w.hear(_voice(0.5), 3.0, RATE)  # the next voice starts afresh
    assert w.gain(3.0 + 4 * HOP_S) > 0.0


def test_the_wobble_and_the_breathing_end_while_the_comfort_noise_still_covers_them() -> None:
    # The wobble: a hop plays SPEAKER_DELAY_S after its push and fades out FADE_S later. The breathing: talking ends
    # one watchdog tick after the playback clock, then blends into the rest in BLEND_S.
    assert config.SPEAKER_DELAY_S + FADE_S < config.SELF_NOISE_TAIL_S
    assert config.WATCHDOG_TICK_S + BLEND_S < config.SELF_NOISE_TAIL_S


def test_talking_breathes_as_pollens_robot_does_under_the_comfort_noise_only() -> None:
    def antennas(speech: bool) -> np.ndarray:
        mixer = Mixer(0.0, speech=speech)
        mixer.set_variant('talking', 'still', 0.0)
        mixer.set_mode('talking', 0.0, blend_s=0.0)
        return np.array([mixer.sample(n / 30)[1] for n in range(120)])

    swing = antennas(True)
    assert np.degrees(swing[:, 0].max() - swing[:, 0].min()) == pytest.approx(30.0, abs=0.5)
    assert np.allclose(swing[:, 0], -swing[:, 1])  # both alike, about their joints' zero
    assert np.degrees(swing[:, 0].mean()) == pytest.approx(0.0, abs=1.5)
    still = antennas(False)
    assert np.allclose(still, animations.NEUTRAL)


def test_the_mixer_wobbles_over_any_move_through_its_blends_and_remembers_the_pose_without_it() -> None:
    def frames(voice: bool) -> list[np.ndarray]:
        mixer = Mixer(0.0)
        mixer.set_mode('talking', 0.0, blend_s=0.0)
        if voice:
            mixer.hear(_voice(4.0), 0.0, RATE)
        out = []
        for n in range(1, 120):
            if n == 30:
                mixer.play_move(animations.GESTURES['nod'], n / 30)  # blended in, played, blended out
            out.append(mixer.sample(n / 30)[0])
            assert voice or np.allclose(out[-1], mixer.head)
            if n == 50:  # the blend into the nod over
                assert np.allclose(mixer.head, animations.GESTURES['nod'].evaluate(20 / 30)[0])
        return out

    plain, wobbled = frames(False), frames(True)
    w = Wobble()
    w.hear(_voice(4.0), 0.0, RATE)
    for n, (a, b) in enumerate(zip(plain, wobbled, strict=True), start=1):
        offset = w.offset(n / 30)
        assert offset is not None and np.allclose(b, wobble.over(a, offset))  # the wobble, whatever is under it


def test_asleep_or_with_the_microphone_live_it_does_not_wobble() -> None:
    asleep = Mixer(0.0)
    asleep.hear(_voice(1.0), 0.0, RATE)
    assert asleep.wobble.offset(0.5) is None
    live = Mixer(0.0, speech=False)
    live.set_mode('talking', 0.0)
    live.hear(_voice(1.0), 0.0, RATE)
    assert live.wobble.offset(0.5) is None
    falling = Mixer(0.0)
    falling.set_mode('talking', 0.0)
    falling.hear(_voice(3.0), 0.0, RATE)
    falling.set_mode('sleep', 1.0)  # the sleep move: what moves fades out, nothing more is heard
    assert falling.wobble.offset(1.0 + FADE_S) is None
    falling.hear(_voice(1.0), 2.0, RATE)
    assert falling.wobble.offset(2.5) is None


class _Mini:
    def set_target_head_pose(self, _pose: np.ndarray) -> None: ...
    def set_target_antenna_joint_positions(self, _antennas: list[float]) -> None: ...
    def set_target_body_yaw(self, _yaw: float) -> None: ...


def test_the_motion_thread_drops_the_wobble_while_an_app_holds_the_body() -> None:
    now = [0.0]
    motion = MotionRunner(_Mini(), clock=lambda: now[0])
    motion.set_mode('talking')
    motion.wobble(_voice(2.0), 0.0, RATE)
    now[0] = 0.5
    assert motion.mixer.wobble.offset(0.5) is not None
    motion.lend(True)
    motion.wobble(_voice(2.0), 2.0, RATE)
    assert motion.mixer.wobble.offset(0.5 + FADE_S) is None and motion.mixer.wobble.offset(2.5) is None
    motion.lend(False)
    motion.wobble(_voice(1.0), 3.0, RATE)
    assert motion.mixer.wobble.offset(3.5) is not None


def test_a_failing_wobble_never_reaches_the_speaker(monkeypatch: pytest.MonkeyPatch) -> None:
    motion = MotionRunner(_Mini())
    motion.set_mode('talking')
    monkeypatch.setattr(motion.mixer, 'hear', lambda *_: 1 / 0)
    motion.wobble(_voice(0.1), 0.0, RATE)
    assert motion.errors == 1


class _Body:
    def __init__(self) -> None:
        self.heard: list[tuple[int, float, int]] = []
        self.stops = 0

    def wobble(self, samples: np.ndarray, start: float, rate: int) -> None:
        self.heard.append((samples.size, start, rate))

    def stop_wobble(self) -> None:
        self.stops += 1


def test_the_tap_hands_the_body_what_the_speaker_plays_when_it_plays() -> None:
    fake_audio = FakeAudio()
    now = [10.0]
    body = _Body()
    tap = Tap(fake_audio, body, clock=lambda: now[0])
    tap.push_audio_sample(_voice(0.5))
    tap.push_audio_sample(_voice(0.25))  # queued behind the first
    delay = config.SPEAKER_DELAY_S
    assert body.heard == [(8000, 10.0 + delay, RATE), (4000, pytest.approx(10.5 + delay), RATE)]
    now[0] = 20.0  # drained: the next plays at once
    tap.push_audio_sample(_voice(0.1))
    assert body.heard[-1][1] == pytest.approx(20.0 + delay)
    tap.push_audio_sample(np.zeros(0, dtype=np.float32))
    assert len(body.heard) == 3 and len(fake_audio.pushed) == 4  # the speaker gets it all, as before
    tap.clear_output()
    assert body.stops == 1 and fake_audio.cleared == 1
    tap.push_audio_sample(_voice(0.1))
    assert body.heard[-1][1] == pytest.approx(20.0 + delay)  # a flush empties its clock
    tap.play_sound('confused.wav')
    assert fake_audio.sounds == ['confused.wav'] and tap.get_input_samplerate() == fake_audio.mic_rate
