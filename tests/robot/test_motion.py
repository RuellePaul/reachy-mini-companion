"""The motion thread, the animations and the gestures, against a fake robot."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from robot_agent import animation_names, animations, config, studio
from robot_agent.motion import SLEEP_ANTENNAS, SLEEP_HEAD, Mixer, MotionRunner, blend_pose, rehearse, rot_z


class FakeMini:
    """Records the targets; its head command raises `error` `failures` times first."""

    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.failures, self.error = failures, error or RuntimeError('daemon busy')
        self.heads: list[np.ndarray] = []
        self.antennas: list[list[float]] = []
        self.body: list[float] = []

    def set_target_head_pose(self, pose: np.ndarray) -> None:
        if self.failures:
            self.failures -= 1
            raise self.error
        self.heads.append(pose)

    def set_target_antenna_joint_positions(self, antennas: list[float]) -> None:
        self.antennas.append(antennas)

    def set_target_body_yaw(self, yaw: float) -> None:
        self.body.append(yaw)


class LaggingMini(FakeMini):
    """A daemon slower than the frame period, as a loaded machine makes it: each frame reaches it well after it was
    sampled, so the last one sent lags the clock."""

    def set_target_head_pose(self, pose: np.ndarray) -> None:
        time.sleep(0.05)
        super().set_target_head_pose(pose)


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, 'MOTION_HZ', 200)
    monkeypatch.setattr(config, 'BODY_TURN_MIN_S', 0.05)
    monkeypatch.setattr(config, 'BODY_TURN_SPEED_DEG_S', 4000.0)
    created: list[MotionRunner] = []

    def make(mini: FakeMini) -> MotionRunner:
        created.append(MotionRunner(mini))
        created[-1].start()
        return created[-1]

    yield make
    for motion in created:
        motion.stop()


def _wait(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'condition not met in time'
        time.sleep(0.005)


def _settled_frame(mini: FakeMini, motion: MotionRunner) -> tuple[np.ndarray, list[float], float]:
    """The first frame sampled once the motion settled. Settled is a time: the frame on its way when it is read may
    have been sampled before it, the next one was not."""
    _wait(motion.settled)
    sent = len(mini.body)  # the body goes last in a frame
    _wait(lambda: len(mini.body) >= sent + 2)
    return mini.heads[sent + 1], mini.antennas[sent + 1], mini.body[sent + 1]


@pytest.mark.parametrize(
    'error', [RuntimeError('daemon busy'), ValueError('non-finite pose'), TimeoutError('no answer'), KeyError('x')]
)
def test_the_thread_survives_a_failing_cycle(runner, error: Exception) -> None:
    mini = FakeMini(failures=5, error=error)
    motion = runner(mini)
    _wait(lambda: len(mini.heads) >= 10)
    assert motion.errors == 5


def test_the_thread_survives_a_move_that_cannot_be_sampled(runner) -> None:
    class Corrupt:  # a recorded move whose frames cannot be read
        duration = 0.2

        def evaluate(self, t: float) -> tuple[np.ndarray, np.ndarray]:
            raise ValueError('corrupt frame')

    mini = FakeMini()
    motion = runner(mini)
    _wait(lambda: len(mini.heads) >= 3)
    motion.play_move(Corrupt())
    time.sleep(0.3)  # the move's 0.2 s are over: it is dropped and the animation takes over again
    frames = len(mini.heads)
    _wait(lambda: len(mini.heads) >= frames + 10)
    assert motion.errors > 0


def test_the_loop_keeps_its_pace_when_the_wall_clock_jumps_back(runner, monkeypatch: pytest.MonkeyPatch) -> None:
    mini = FakeMini()
    runner(mini)
    _wait(lambda: len(mini.heads) >= 5)
    monkeypatch.setattr(time, 'time', lambda: 0.0)  # an NTP step back to 1970
    frames = len(mini.heads)
    _wait(lambda: len(mini.heads) >= frames + 20)  # the loop paces on the monotonic clock: frames keep coming


def test_a_blend_started_by_a_new_mode_survives_the_end_of_the_one_it_replaced() -> None:
    mini = FakeMini()
    motion = MotionRunner(mini)  # not started: the test steps it by hand
    t0 = time.monotonic()
    motion.set_mode('idle', blend_s=0.1)  # out of the sleep pose
    motion._step(t0 + 0.05)
    halfway = list(mini.antennas[-1])
    motion.set_mode('listening', blend_s=60.0)  # a new blend, from where the robot is
    motion._step(t0 + 1.0)  # past the end of the first blend
    assert motion.mixer.blend is not None and motion.mixer.blend.duration == 60.0
    assert mini.antennas[-1] == pytest.approx(halfway, abs=0.01)  # the new blend applies, barely begun


def test_a_blend_started_while_a_frame_is_sent_is_not_erased() -> None:
    """The motion thread sends a frame (lock released) while the session changes the mode: the frame that found
    the old blend over must not throw the new one away (the head would snap to the new pose)."""

    class ModeChangeMidFrame(FakeMini):
        def set_target_head_pose(self, pose: np.ndarray) -> None:
            super().set_target_head_pose(pose)
            if len(self.heads) == 1:
                motion.set_mode('listening', blend_s=60.0)

    motion = MotionRunner(ModeChangeMidFrame())  # not started: the test steps it by hand
    t0 = time.monotonic()
    motion.set_mode('idle', blend_s=0.01)
    motion._step(t0 + 1.0)  # this frame finds the idle blend over, and the mode changes while it is sent
    assert motion.mixer.blend is not None and motion.mixer.blend.duration == 60.0


def test_new_settings_during_the_wake_move_do_not_hurry_it() -> None:
    """This wake's animation arrives mid-move: the antennas still swing at the wake move's pace (8 rad/s is
    their joint's limit)."""
    motion = MotionRunner(FakeMini())
    motion.set_mode('idle', blend_s=config.WAKE_S)
    t0 = motion.mixer.blend.t0
    motion._step(t0 + 0.05)
    motion.set_variant('idle', 'still')  # a fetched variant other than the default
    times = np.arange(t0 + 0.1, t0 + config.WAKE_S + 0.2, 1.0 / 30)
    antennas = []
    for t in times:
        motion._step(t)
        antennas.append(motion.mixer.antennas.copy())
    speed = np.abs(np.diff(antennas, axis=0)).max(axis=1) / np.diff(times)
    assert speed.max() < 8.0, speed.max()


def test_moving_until_is_the_end_of_the_move_or_of_the_turn() -> None:
    motion = MotionRunner(FakeMini())  # not started: only what is planned is read
    assert motion.moving_until() <= time.monotonic()  # nothing under way
    motion.gesture('nod')  # 1 s, asleep: once out of the sleep pose
    assert motion.moving_until() == pytest.approx(time.monotonic() + config.WAKE_S + 1.0, abs=0.05)
    motion = MotionRunner(FakeMini())
    motion.set_mode('idle', blend_s=0.0)  # awake
    motion.gesture('nod')
    assert motion.moving_until() == pytest.approx(time.monotonic() + 1.0, abs=0.05)
    motion.turn_body(100)  # at least BODY_TURN_MIN_S, 1.5 s: it outlasts the nod
    assert motion.moving_until() == pytest.approx(time.monotonic() + config.BODY_TURN_MIN_S, abs=0.05)


def test_turn_body_is_clamped_and_reached(runner) -> None:
    mini = FakeMini()
    motion = runner(mini)
    assert motion.turn_body(150) == 100.0
    assert _settled_frame(mini, motion)[2] == pytest.approx(math.radians(100))
    assert motion.turn_body(-500) == -100.0


def test_the_body_turn_is_never_quicker_than_the_motor_takes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BODY_TURN_MIN_S', 1.5)
    motion = MotionRunner(FakeMini())
    motion.turn_body(0)
    assert motion.settled()  # already facing front: nothing to wait for
    motion.turn_body(5)
    assert not motion.settled()  # a 5° turn still takes 1.5 s
    ramp = motion.mixer.turn
    motion.turn_body(5)
    assert motion.mixer.turn is ramp  # on its way already: the same turn, not a new one


def test_the_shipped_turn_pace_spares_the_body_motor() -> None:
    def pace(speed: float) -> float:  # a full swing, -100° to +100°: the fastest average the pace allows
        mixer = Mixer(0.0, (rot_z(math.radians(-100)), animations.NEUTRAL, math.radians(-100)))
        mixer.turn_body(100, 0.0, speed)
        return 200.0 / mixer.turn.duration

    assert pace(1.0) <= 90.0 + 1e-9  # a conversation's turn
    assert pace(config.SCENE_TURN_SPEED) <= 1.2 * 90.0 + 1e-9  # a scene's: at most 20 % faster


@pytest.mark.parametrize('daemon', [FakeMini, LaggingMini])
def test_the_head_follows_the_body_and_sleep_is_reached(runner, daemon: type[FakeMini]) -> None:
    mini = daemon()
    motion = runner(mini)
    motion.set_mode('idle', blend_s=0.05)
    motion.set_variant('idle', 'still')
    motion.turn_body(90)
    head, _, _ = _settled_frame(mini, motion)
    assert np.allclose(head, rot_z(math.radians(90)), atol=1e-6)  # still: neutral, turned with the body
    motion.set_mode('sleep', blend_s=0.05)
    motion.turn_body(0)
    head, antennas, _ = _settled_frame(mini, motion)
    assert np.allclose(head, SLEEP_HEAD, atol=1e-2)
    assert antennas == pytest.approx([-3.05, 3.05])


def test_the_last_frame_sent_at_a_stop_is_the_pose_reached() -> None:
    """Settled is a time: the thread may not have sampled it yet when it stops, so the stop sends that frame."""
    now = [0.0]
    mini = FakeMini()
    motion = MotionRunner(mini, clock=lambda: now[0])
    motion.start((np.eye(4), np.zeros(2), 0.0))  # off the sleep pose: it eases into it
    _wait(lambda: mini.heads)
    now[0] = 10.0  # the ease is over, and the thread has not sampled it
    motion.stop()
    assert np.allclose(mini.heads[-1], SLEEP_HEAD) and mini.antennas[-1] == pytest.approx([-3.05, 3.05])


def test_a_gesture_plays_then_hands_back_to_the_animation(runner) -> None:
    mini = FakeMini()
    motion = runner(mini)
    motion.set_mode('listening', blend_s=0.01)
    motion.set_variant('listening', 'still')
    assert not motion.gesture('moonwalk')
    assert motion.gesture('nod')
    time.sleep(0.3)
    pitches = [Rotation.from_matrix(h[:3, :3]).as_euler('xyz', degrees=True)[1] for h in mini.heads[-30:]]
    assert max(pitches) > 5.0  # nodding
    motion.set_mode('talking')  # during the gesture: applies after it
    _wait(motion.settled, 3.0)
    time.sleep(0.02)
    assert motion.settled()


@pytest.mark.parametrize('waking', [False, True])
def test_a_gesture_out_of_the_sleep_pose_plays_whole_as_the_preview_shows(waking: bool) -> None:
    """« Essayer » on a nod with the robot asleep (main._try), or a scene played asleep opening on one (the talking
    mode's wake blend under way): the robot reaches the nod's first frame, then nods as the 3D preview shows, rather
    than spending the nod inside the slow blend out of the sleep pose."""
    nod, hz, at = animations.GESTURES['nod'], config.MOTION_HZ, 0.3
    preview = rehearse(
        [(0.0, lambda m, t: m.set_variant('idle', 'still', t)), (at, lambda m, t: m.play_move(nod, t))], 3
    )
    mixer, frames, started = Mixer(0.0), [], 0.0  # asleep, settled in the sleep pose
    for n in range(4 * hz + 1):
        if waking and n == round((at - 0.15) * hz):
            mixer.set_mode('talking', n / hz, config.WAKE_S)
        if n == round(at * hz):
            mixer.play_move(nod, n / hz)
            started = mixer.moving_until() - nod.duration
        frames.append(mixer.sample(n / hz))

    def swing(frames: list, start: float) -> float:  # from the dip (-4° at 0.5 s) to the next key (+8° at 0.75 s)
        pitch = np.array([_angles(head)[0] for head, _, _ in frames])
        dip, up = (pitch[round((start + a) * hz) : round((start + b) * hz) + 1] for a, b in ((0.4, 0.6), (0.6, 0.9)))
        return float(up.max() - dip.min())

    assert swing(frames, started) >= 0.9 * swing(preview, at) > 8.0
    assert np.allclose(frames[round(started * hz)][0], np.eye(4), atol=1e-6)  # the nod's first frame, reached
    assert waking or np.allclose(frames[-1][0], SLEEP_HEAD)  # and back to sleep


def test_a_move_out_of_the_sleep_pose_waits_at_its_first_frame() -> None:
    """An animation tried asleep (studio.AnimationMove, defined before its start too): the blend heads for its first
    frame, not for where the animation would be before it starts."""
    move = studio.AnimationMove('idle', 'observer', animations.Tuning(), 3.0)
    mixer = Mixer(0.0)
    mixer.play_move(move, 0.0)
    head, antennas, _ = mixer.sample(config.WAKE_S / 2)
    first, alpha = move.evaluate(0.0), animations.smoothstep(0.5)
    assert np.allclose(head, blend_pose(SLEEP_HEAD, first[0], alpha), atol=1e-9)
    assert np.allclose(antennas, (1 - alpha) * SLEEP_ANTENNAS + alpha * np.asarray(first[1]), atol=1e-9)


def test_blends_are_rigid_poses() -> None:
    a = animations.pose(yaw=50.0, z=0.01)
    b = animations.pose(pitch=-20.0, roll=10.0)
    for alpha in (0.0, 0.3, 0.7, 1.0):
        rotation = blend_pose(a, b, alpha)[:3, :3]
        assert np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-9)
    assert np.allclose(blend_pose(a, b, 0.0), a) and np.allclose(blend_pose(a, b, 1.0), b)


def test_the_variants_are_exactly_the_shared_names() -> None:
    assert {mode: set(variants) for mode, variants in animations.VARIANTS.items()} == {
        mode: set(variants) for mode, variants in animation_names.VARIANTS.items()
    }


ALL_VARIANTS = [(m, k) for m in animations.VARIANTS for k in animations.VARIANTS[m]]
LOUDEST = {
    m: animations.Tuning(amplitude=animation_names.AMPLITUDE_RANGE[m][1], speed=animation_names.SPEED_RANGE[1])
    for m in animation_names.MODES
}
# Pollen's ranges for the head off neutral.
LIMITS = {'pitch': 22.0, 'yaw': 20.0, 'roll': 15.0, 'z_mm': (-25.0, 20.0), 'antennas_rad': 1.5}


def _angles(head: np.ndarray) -> np.ndarray:
    roll, pitch, yaw = Rotation.from_matrix(head[:3, :3]).as_euler('xyz', degrees=True)
    return np.array([pitch, yaw, roll])


def _within_limits(head: np.ndarray, antennas: np.ndarray) -> bool:
    pitch, yaw, roll = np.abs(_angles(head))
    z_mm = head[2, 3] * 1000.0
    low, high = LIMITS['z_mm']
    return (
        pitch <= LIMITS['pitch'] + 1e-6
        and yaw <= LIMITS['yaw'] + 1e-6
        and roll <= LIMITS['roll'] + 1e-6
        and low <= z_mm <= high
        and bool(np.all(np.abs(antennas - animations.NEUTRAL) <= LIMITS['antennas_rad']))
    )


@pytest.mark.parametrize('key', list(animations.VARIANTS['talking']))
def test_talking_moves_the_head_little_and_the_antennas_not_at_all(key: str) -> None:
    for tuning in (animations.Tuning(), LOUDEST['talking']):
        poses = [animations.animate('talking', key, tuning)(t) for t in np.arange(0.0, 20.0, 0.05)]
        angles = [np.degrees(Rotation.from_matrix(h[:3, :3]).magnitude()) for h, _ in poses]
        assert max(angles) <= 3.5
        assert all(np.allclose(a, poses[0][1]) for _, a in poses)


@pytest.mark.parametrize(('mode', 'key'), ALL_VARIANTS)
def test_antennas_stay_slow(mode: str, key: str) -> None:
    def top_speed(tuning: animations.Tuning) -> float:
        antennas = np.array([animations.animate(mode, key, tuning)(t)[1] for t in np.arange(0.0, 30.0, 0.02)])
        return float(np.degrees(np.abs(np.diff(antennas, axis=0)).max()) / 0.02)

    assert top_speed(animations.Tuning()) <= 4.0  # °/s: an antenna at 12°/s already peaks at -30 dBFS in the mic
    assert top_speed(LOUDEST[mode]) <= 9.0  # the loudest settings the dashboard allows stay well under that


@pytest.mark.parametrize(('mode', 'key'), ALL_VARIANTS)
def test_every_variant_stays_in_range_and_moves_continuously(mode: str, key: str) -> None:
    for tuning in (animations.Tuning(), LOUDEST[mode]):
        play = animations.animate(mode, key, tuning)
        frames = [play(t) for t in np.arange(0.0, 60.0, 1.0 / 30)]
        assert all(_within_limits(head, antennas) for head, antennas in frames)
        heads = np.array([_angles(h) for h, _ in frames])
        assert np.abs(np.diff(heads, axis=0)).max() <= 2.0  # degrees per 30 Hz frame: no jump


def test_tuning_scales_the_variant_as_written() -> None:
    plain = animations.animate('idle', 'curious')
    tuned = animations.animate('idle', 'curious', animations.Tuning(amplitude=0.5, speed=1.5))
    for t in (0.0, 1.3, 7.9):
        assert _angles(tuned(t)[0]) == pytest.approx(
            0.5 * animations.VARIANTS['idle']['curious'](1.5 * t)[:3], abs=1e-6
        )
        assert _angles(plain(t)[0]) == pytest.approx(animations.VARIANTS['idle']['curious'](t)[:3], abs=1e-6)


def test_every_gesture_has_a_name_a_label_and_a_sentence() -> None:
    assert set(animations.GESTURES) == set(animation_names.GESTURES)
    assert all(label and sentence for label, sentence in animation_names.GESTURES.values())


@pytest.mark.parametrize('name', list(animations.GESTURES))
def test_gestures_start_and_end_at_rest_and_stay_smooth_and_in_range(name: str) -> None:
    gesture = animations.GESTURES[name]
    for t in (0.0, gesture.duration, gesture.duration + 1.0):
        head, antennas = gesture.evaluate(t)
        assert np.allclose(head, np.eye(4)) and np.allclose(antennas, animations.NEUTRAL)
    frames = [gesture.evaluate(t) for t in np.arange(0.0, gesture.duration, 1.0 / 30)]
    assert all(_within_limits(head, antennas) for head, antennas in frames)
    heads = np.array([_angles(h) for h, _ in frames])
    antennas = np.array([a for _, a in frames])
    assert np.abs(np.diff(heads, axis=0)).max() <= 5.0  # degrees per frame
    assert np.degrees(np.abs(np.diff(antennas, axis=0)).max()) * 30 <= 300.0  # under the 8 rad/s joint limit


def test_the_preview_is_the_frames_the_motion_thread_sends() -> None:
    """The dashboard's 3D preview (`rehearse`, on the Mac) and the robot's thread run the same mixer: stepped on
    the same clock with the same cues, they send the same frames."""
    hz = 30
    cues = [
        (0.0, lambda m, t: m.set_variant('idle', 'observer', t, animations.Tuning(amplitude=1.2, speed=0.8))),
        (1.0, lambda m, t: m.play_move(animations.GESTURES['dance'], t)),
        (2.5, lambda m, t: m.set_mode('talking', t)),
        (3.0, lambda m, t: m.turn_body(40, t)),
        (6.0, lambda m, t: m.set_mode('listening', t)),
    ]
    preview = rehearse(cues, seconds=8.0, hz=hz)

    clock = [0.0]
    mini = FakeMini()
    motion = MotionRunner(mini, clock=lambda: clock[0])  # not started: stepped by hand, as the thread would
    motion.set_mode('idle', blend_s=0.0)
    motion._step(0.0)  # settled in idle, like the preview's start
    mini.heads.clear(), mini.antennas.clear(), mini.body.clear()
    actions = {
        0.0: lambda: motion.set_variant('idle', 'observer', animations.Tuning(amplitude=1.2, speed=0.8)),
        1.0: lambda: motion.play_move(animations.GESTURES['dance']),
        2.5: lambda: motion.set_mode('talking'),
        3.0: lambda: motion.turn_body(40),
        6.0: lambda: motion.set_mode('listening'),
    }
    for n in range(len(preview)):
        clock[0] = n / hz
        if (act := actions.get(round(clock[0], 6))) is not None:
            act()
        motion._step(clock[0])
    assert len(mini.heads) == len(preview)
    for sent, antennas, body, (head, want_antennas, want_body) in zip(
        mini.heads, mini.antennas, mini.body, preview, strict=True
    ):
        assert np.allclose(sent, rot_z(want_body) @ head, atol=1e-9)
        assert np.allclose(antennas, want_antennas, atol=1e-9) and body == pytest.approx(want_body, abs=1e-9)
