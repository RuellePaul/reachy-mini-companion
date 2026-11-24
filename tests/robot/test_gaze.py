"""Regard actif: where a face is, how the head turns to it and when it does not, the thread that looks."""

from __future__ import annotations

import itertools
import math
import threading
import time
from typing import Any

import numpy as np
import pytest

from robot_agent import animations, config, gaze
from robot_agent.motion import SLEEP_HEAD, Mixer, MotionRunner, rehearse, rot_z

DEG = math.radians(1.0)
# The SDK's camera, as a pinhole: 1280x720, ±44° across, 4.4 cm ahead of the head's centre and 5.1 cm above it,
# looking along the head's x (its z), its x to the head's right, its y down.
K = np.array([[663.0, 0.0, 640.0], [0.0, 663.0, 360.0], [0.0, 0.0, 1.0]])
MOUNT = np.array([[0.0, 0.0, 1.0, 0.0437], [-1.0, 0.0, 0.0, 0.0], [0.0, -1.0, 0.0, 0.0512], [0.0, 0.0, 0.0, 1.0]])


def _points_at(head: np.ndarray) -> tuple[float, float]:
    """The (yaw, pitch) a head pose looks along: +yaw left, +pitch down."""
    x = head[:3, 0]
    return math.atan2(x[1], x[0]), math.atan2(-x[2], math.hypot(x[0], x[1]))


# ----- geometry -----


def test_the_head_is_aimed_where_it_is_told_and_keeps_a_share_of_its_animation() -> None:
    rest = np.eye(4)
    assert gaze.aimed(rest, 0.5, -0.3, 0.0) is rest  # no gaze: the animation untouched
    yaw, pitch = _points_at(gaze.aimed(rest, 30 * DEG, -20 * DEG, 1.0))
    assert (yaw, pitch) == pytest.approx((30 * DEG, -20 * DEG))  # left, and up
    assert gaze.aimed(rest, 30 * DEG, -20 * DEG, 1.0)[:3, 0] == pytest.approx(gaze.direction(30 * DEG, -20 * DEG))
    assert _points_at(gaze.aimed(rest, 30 * DEG, -20 * DEG, 0.5))[0] == pytest.approx(15 * DEG)

    nod = animations.pose(pitch=10.0, x=0.01, z=0.02)  # an animation looking down, leaning in, lifted
    under = gaze.aimed(nod, 90 * DEG, 0.0, 1.0)
    assert under[:3, :3] @ under[:3, :3].T == pytest.approx(np.eye(3))  # a rigid pose
    assert _points_at(under)[1] == pytest.approx(gaze.KEEP * 10 * DEG)  # the nod still reads, damped
    # Its translation is damped too, and turned with the gaze: leaning in is leaning towards the face.
    assert under[:3, 3] == pytest.approx([0.0, gaze.KEEP * 0.01, gaze.KEEP * 0.02], abs=1e-9)


def test_a_face_is_placed_in_the_robots_frame_whatever_the_head_does() -> None:
    lens = gaze.Lens(K, np.zeros(5))
    where = 1.2 * gaze.direction(25 * DEG, -15 * DEG)  # 1.2 m away, to the left, above
    for head in (np.eye(4), rot_z(20 * DEG) @ animations.pose(pitch=-12.0, roll=4.0, z=0.01), rot_z(-10 * DEG)):
        cam = np.linalg.inv(head @ MOUNT) @ np.append(where, 1.0)
        u, v = lens.pixel(cam[:3])
        half = 663.0 * gaze.FACE_M * gaze._stretch(cam[0] / cam[2], cam[1] / cam[2]) / np.linalg.norm(cam[:3]) / 2
        (x, y), (left, _), (right, _) = lens.plane([(u, v), (u - half, v), (u + half, v)])
        yaw, pitch, metres = gaze.locate(x, y, right - left, head @ MOUNT)
        assert (yaw, pitch, metres) == pytest.approx((25 * DEG, -15 * DEG, 1.2), abs=1e-3)


# ----- the aim -----


def _run(aim: gaze.Aim, seconds: float, start: float = 0.0, **step: Any) -> list[tuple[float, float]]:
    """What the head is turned by, frame after frame, at the motion thread's pace."""
    out = []
    for n in range(round(seconds * 30) + 1):
        yaw, pitch, share = aim.step(start + n / 30, 0.0, **step)
        out.append((share * yaw, share * pitch))
    return out


def _top_speed(turns: list[tuple[float, float]]) -> float:
    return max(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in itertools.pairwise(turns)) * 30


def test_the_microphone_open_the_head_turns_at_an_idle_pace_then_holds_still() -> None:
    aim = gaze.Aim()

    def follow(yaw: float, pitch: float, start: float, seconds: float) -> list[tuple[float, float]]:
        out = []
        for n in range(round(seconds * 30)):
            aim.see(yaw, pitch, start + n / 30)
            out.extend(_run(aim, 0.0, start + n / 30))
        return out

    turns = follow(30 * DEG, -10 * DEG, 0.0, 6.0)
    assert turns[-1] == pytest.approx((30 * DEG, -10 * DEG), abs=1e-3)  # there
    assert _top_speed(turns) <= gaze.QUIET_SPEED * 1.02  # and never quicker than an idle animation's glance
    held = follow(33 * DEG, -8 * DEG, 6.0, 2.0)  # the face shifts a little: nothing moves
    assert held[-1] == turns[-1]
    moved = follow(38 * DEG, -10 * DEG, 8.0, 3.0)  # it really moved: a turn, at the same pace, then rest again
    assert moved[-1][0] == pytest.approx(38 * DEG, abs=gaze.REST)
    assert _top_speed(moved) <= gaze.QUIET_SPEED * 1.02
    assert follow(38.5 * DEG, -10 * DEG, 11.0, 1.0)[-1] == moved[-1]


def test_under_the_comfort_noise_the_head_is_quick_and_follows_every_move() -> None:
    aim = gaze.Aim()
    turns: list[tuple[float, float]] = []
    for n in range(60):
        aim.see(35 * DEG, 0.0, n / 30)
        turns += _run(aim, 0.0, n / 30, quick=True)
    assert turns[20][0] == pytest.approx(35 * DEG, abs=0.5 * DEG)  # there in well under a second
    assert gaze.QUIET_SPEED * 3 < _top_speed(turns) <= gaze.SPEED * 1.02
    for n in range(60, 90):  # a small shift is followed at once: no dead zone under the noise
        aim.see(37 * DEG, 0.0, n / 30)
        turns += _run(aim, 0.0, n / 30, quick=True)
    assert turns[-1][0] == pytest.approx(37 * DEG, abs=0.1 * DEG)


def test_a_face_no_longer_said_is_let_go_and_limits_and_the_body_are_minded() -> None:
    aim, yaw, pitch, share = gaze.Aim(), 0.0, 0.0, 0.0
    for n in range(300):  # a face far behind, far above
        aim.see(170 * DEG, -80 * DEG, n / 30)
        yaw, pitch, share = aim.step(n / 30, 0.0, quick=True)
    assert (yaw, pitch, share) == pytest.approx((gaze.MAX_YAW, -gaze.MAX_UP, 1.0))
    aim.see(30 * DEG, 5 * DEG, 10.0)
    for n in range(60):
        aim.see(30 * DEG, 5 * DEG, 10 + n / 30)
        yaw, _, _ = aim.step(10 + n / 30, 50 * DEG, quick=True)  # the body is turned 50° left: the face is to its right
    assert yaw == pytest.approx(-20 * DEG, abs=0.1 * DEG)
    shares = [aim.step(12 + n / 30, 50 * DEG)[2] for n in range(120)]  # nothing said any more
    assert shares[round(gaze.HOLD_S * 30) - 2] == 1.0 and shares[-1] == 0.0  # held half a second, then let go
    assert all(b <= a for a, b in itertools.pairwise(shares))
    aim.see(math.nan, 0.0, 20.0)  # never smoothed into the frames after it
    assert aim.step(20.0, 0.0)[2] == 0.0


def test_the_gaze_gives_way_to_the_robots_reach_and_comes_back() -> None:
    aim, yaw, share = gaze.Aim(), 0.0, 0.0
    wall = 20 * DEG  # the robot takes no pose turned further than this

    def fits(yaw: float, pitch: float, share: float) -> bool:
        return share * yaw <= wall

    for n in range(150):
        aim.see(40 * DEG, 0.0, n / 30)
        yaw, _, share = aim.step(n / 30, 0.0, quick=True, fits=fits)
        assert share * yaw <= wall  # never a pose the robot refuses
    assert share * yaw > wall - 40 * DEG * gaze.NARROW  # and as near the face as its reach allows
    wall = 90 * DEG  # the animation under it moved on: there is room again
    for n in range(150, 240):
        aim.see(40 * DEG, 0.0, n / 30)
        yaw, _, share = aim.step(n / 30, 0.0, quick=True, fits=fits)
    assert share * yaw == pytest.approx(40 * DEG, abs=0.1 * DEG)


# ----- in the mixer -----


def _awake(mode: str = 'idle', **mixer: Any) -> Mixer:
    out = Mixer(0.0, **mixer)
    out.mode = mode
    out.sample(0.0)
    return out


def test_no_face_no_change_the_preview_and_a_robot_without_the_setting_move_as_before() -> None:
    plain = rehearse([], 3.0, mode='listening')
    looked = _awake('listening')
    frames = [looked.sample(n / config.MOTION_HZ) for n in range(len(plain))]
    assert all(np.array_equal(a[0], b[0]) for a, b in zip(plain, frames, strict=True))


def test_the_animation_is_aimed_at_the_face_and_a_move_takes_the_head_whole() -> None:
    mixer = _awake('idle', speech=True)
    mixer.set_variant('idle', 'still', 0.0)
    for n in range(1, 200):
        mixer.aim.see(25 * DEG, -15 * DEG, n / 30)
        head = mixer.sample(n / 30)[0]
    assert _points_at(head) == pytest.approx((25 * DEG, -15 * DEG), abs=0.2 * DEG)
    assert np.array_equal(mixer.head, head)  # remembered: the next blend starts from where it looks

    mixer.play_move(animations.GESTURES['nod'], 200 / 30)
    during = [mixer.sample(200 / 30 + n / 30)[0] for n in range(1, 30)]
    for n in range(30):
        mixer.aim.see(25 * DEG, -15 * DEG, 200 / 30 + n / 30)
    assert abs(_points_at(during[-1])[0]) < 1 * DEG  # the nod is played as it is written: straight ahead
    end = 200 / 30 + animations.GESTURES['nod'].duration
    for n in range(1, 90):
        mixer.aim.see(25 * DEG, -15 * DEG, end + n / 30)
        head = mixer.sample(end + n / 30)[0]
    assert _points_at(head) == pytest.approx((25 * DEG, -15 * DEG), abs=0.2 * DEG)  # then back to the face

    mixer.set_mode('sleep', end + 3.0, blend_s=1.0)
    asleep = [mixer.sample(end + 3.0 + n / 30)[0] for n in range(1, 45)]
    assert np.allclose(asleep[-1], SLEEP_HEAD) and mixer.aim.weight == 0.0  # asleep, it looks at nobody


@pytest.mark.parametrize(
    ('mode', 'speech', 'turning', 'quick'),
    [
        ('listening', True, False, False),  # the microphone listens
        ('talking', True, False, True),  # the comfort noise covers the motors
        ('talking', False, False, False),  # MIC_MUTE_WHILE_TALKING=0: the microphone is never covered
        ('idle', True, True, True),  # the body turns: a tool's move, covered too
    ],
)
def test_the_head_is_quick_only_under_the_comfort_noise(mode: str, speech: bool, turning: bool, quick: bool) -> None:
    mixer = _awake(mode, speech=speech)
    mixer.set_variant(mode, 'still', 0.0)
    if turning:
        mixer.turn_body(-60.0, 0.0)
    heads = []
    for n in range(1, 45):
        mixer.aim.see(35 * DEG, 0.0, n / 30)
        head, _, body = mixer.sample(n / 30)
        heads.append(_points_at(rot_z(body) @ head)[0] - body)
    speed = max(abs(b - a) for a, b in itertools.pairwise(heads)) * 30
    assert (speed > gaze.QUIET_SPEED * 2) is quick


def test_a_frame_the_robot_would_refuse_is_never_sent() -> None:
    limit = 20 * DEG  # further than the wobble alone ever turns the head (11.25° at full voice)
    reach = lambda head, body: abs(_points_at(head)[0] - body) <= limit  # noqa: E731
    mixer = _awake('talking', speech=True, reach=reach)
    mixer.set_variant('talking', 'still', 0.0)
    mixer.hear(0.5 * np.sin(np.arange(16000 * 6) * 0.05).astype(np.float32), 0.0, 16000)  # it talks: the head wobbles
    yaws = []
    for n in range(1, 180):
        mixer.aim.see(35 * DEG, 0.0, n / 30)
        head, _, body = mixer.sample(n / 30)
        yaws.append(_points_at(rot_z(body) @ head)[0])
    assert limit - 5 * DEG < max(yaws) <= limit + 1e-9  # the wobble included, and right up to the edge


class FakeMini:
    def __init__(self) -> None:
        self.heads: list[np.ndarray] = []

    def set_target_head_pose(self, pose: np.ndarray) -> None:
        self.heads.append(pose)

    def set_target_antenna_joint_positions(self, antennas: list[float]) -> None: ...
    def set_target_body_yaw(self, yaw: float) -> None: ...


def test_the_motion_thread_remembers_the_poses_it_sent_and_says_none_for_a_head_in_a_hand() -> None:
    now = [100.0]
    motion = MotionRunner(FakeMini(), clock=lambda: now[0])
    assert motion.head_at(100.0) is None  # nothing sent yet
    for n in range(40):
        now[0] = 100.0 + n / 30
        motion._step(now[0])
    assert np.array_equal(motion.head_at(100.5), motion._mini.heads[15])  # the frame nearest the time asked
    assert motion.head_at(now[0] + 0.3) is None and motion.head_at(98.0) is None  # too far from any frame
    motion.look(0.4, -0.2)
    assert motion.mixer.aim._seen == (0.4, -0.2, now[0])
    motion.lend(True)  # an app holds the body: no face to look at
    assert motion.mixer.aim._seen is None
    motion.look(0.4, -0.2)
    assert motion.mixer.aim._seen is None
    motion.freeze()
    assert motion.head_at(now[0]) is None


# ----- the thread that looks -----


class Room:
    """A camera, the motion thread and YuNet, for one room: faces stand where they are put, in the robot's frame."""

    def __init__(self) -> None:
        self.faces: list[tuple[float, float, float]] = []  # yaw, pitch, metres
        self.head: np.ndarray | None = np.eye(4)
        self.said: list[tuple[float, float] | None] = []
        self.views: list[str] = []
        self.frames = 0
        self.now = 0.0
        self.lens = gaze.Lens(K, np.zeros(5))

    def read_frame(self, wait_s: float) -> np.ndarray | None:
        self.frames += 1
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        for yaw, pitch, metres in self.faces if self.head is not None else []:
            cam = np.linalg.inv(self.head @ MOUNT) @ np.append(metres * gaze.direction(yaw, pitch), 1.0)
            if cam[2] > 0.05:
                u, v = self.lens.pixel(cam[:3])
                width = 663.0 * gaze.FACE_M * gaze._stretch(cam[0] / cam[2], cam[1] / cam[2]) / np.linalg.norm(cam[:3])
                if 0 <= u < 1280 and 0 <= v < 720:
                    frame[int(v), int(u)] = (255, min(255, int(width) // 2), 0)  # a face: its nose, half its width
        return frame

    def geometry(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return K, np.zeros(5), MOUNT

    def head_at(self, t: float) -> np.ndarray | None:
        return self.head

    def look(self, yaw: float | None, pitch: float = 0.0) -> None:
        self.said.append(None if yaw is None else (yaw, pitch))

    def detect(self, name: str, view: np.ndarray, width: int, score: float) -> list[gaze.Face]:
        self.views.append(name)
        ys, xs = np.nonzero(view[:, :, 0])
        return [gaze.Face(x + 0.5, y + 0.5, x + 0.5, y + 0.5, 2.0 * view[y, x, 1]) for y, x in zip(ys, xs, strict=True)]


def _tracker(room: Room) -> gaze.Tracker:
    return gaze.Tracker(room, room, lambda: True, detect=room.detect, clock=lambda: room.now)


def _ticks(tracker: gaze.Tracker, room: Room, seconds: float) -> None:
    until = room.now + seconds
    while room.now < until:
        room.now += tracker.tick()


def test_a_face_is_found_then_followed_in_a_crop_wherever_the_head_points() -> None:
    room = Room()
    room.faces = [(20 * DEG, -12 * DEG, 0.9)]
    tracker = _tracker(room)
    _ticks(tracker, room, 1.0)
    assert room.said[-1] == pytest.approx((20 * DEG, -12 * DEG), abs=1.5 * DEG)
    assert room.views[0] == 'middle' and set(room.views[2:]) == {'crop'}  # searched, then followed: the cheap view
    room.head = rot_z(18 * DEG) @ animations.pose(pitch=-10.0)  # the head has turned to it: the face has not moved
    _ticks(tracker, room, 1.0)
    assert room.said[-1] == pytest.approx((20 * DEG, -12 * DEG), abs=1.5 * DEG)
    assert set(room.views[2:]) == {'crop'}


def test_the_nearest_face_is_the_one_and_it_is_kept_while_it_shows() -> None:
    room = Room()
    room.faces = [(-15 * DEG, -10 * DEG, 2.0), (10 * DEG, -10 * DEG, 0.8)]
    tracker = _tracker(room)
    _ticks(tracker, room, 1.0)
    assert room.said[-1][0] == pytest.approx(10 * DEG, abs=1.5 * DEG)  # the nearer of the two
    room.faces.append((-5 * DEG, -10 * DEG, 0.5))  # someone leans in, nearer still: the one followed is kept
    _ticks(tracker, room, 2.0)
    assert room.said[-1][0] == pytest.approx(10 * DEG, abs=1.5 * DEG)


def test_a_face_hidden_is_looked_for_where_it_was_then_given_up_for_the_nearest_one() -> None:
    room = Room()
    room.faces = [(10 * DEG, -10 * DEG, 0.8)]
    tracker = _tracker(room)
    _ticks(tracker, room, 1.0)
    room.faces = [(-25 * DEG, -5 * DEG, 1.0)]  # a hand before the first face; someone else, elsewhere
    _ticks(tracker, room, gaze.LOST_S - 0.3)
    assert room.said[-1] == pytest.approx((10 * DEG, -10 * DEG), abs=1.5 * DEG)  # still said where it was
    _ticks(tracker, room, 1.5)
    assert None in room.said  # given up
    assert room.said[-1][0] == pytest.approx(-25 * DEG, abs=1.5 * DEG)  # then the one that is there
    room.faces = []
    before = len(room.said)
    _ticks(tracker, room, gaze.LOST_S + 1.0)
    assert room.said[-1] is None and room.said[before:].count(None) == 1  # nobody: said once


def test_no_frame_no_pose_nothing_is_said_and_the_camera_is_asked_less() -> None:
    room = Room()
    room.faces = [(0.0, -10 * DEG, 0.8)]
    tracker = _tracker(room)
    room.head = None  # the head is in someone's hand: a face cannot be placed
    assert tracker.tick() == pytest.approx(1 / gaze.TRACK_HZ) and room.said == []
    room.read_frame = lambda wait_s: None  # type: ignore[method-assign]
    assert tracker.tick() == gaze.NO_FRAME_S and room.said == []
    empty = Room()
    quiet = _tracker(empty)
    quiet._seen_at = 0.0  # as the thread sets it when a conversation starts: someone is likely there
    assert quiet.tick() == pytest.approx(1 / gaze.SEARCH_HZ)  # nobody yet: a brisk search
    empty.now = gaze.IDLE_AFTER_S + 1.0
    assert quiet.tick() == pytest.approx(1 / gaze.IDLE_HZ)  # then a lazy one


def test_the_thread_looks_only_in_a_conversation_with_the_setting_on() -> None:
    room, wanted = Room(), threading.Event()
    room.faces = [(0.0, -10 * DEG, 0.8)]
    tracker = gaze.Tracker(room, room, wanted.is_set, detect=room.detect)
    try:
        tracker.enable(False)
        assert tracker._thread is None  # the setting off: no thread at all
        tracker.enable(True)
        time.sleep(0.2)
        assert room.frames == 0  # asleep: the camera is not read
        wanted.set()
        deadline = time.monotonic() + 3.0
        while not room.said and time.monotonic() < deadline:
            time.sleep(0.01)
        assert room.said and room.said[-1] is not None
        wanted.clear()  # the robot falls asleep, a scene starts: the face is let go at once
        deadline = time.monotonic() + 3.0
        while room.said[-1] is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert room.said[-1] is None
        read = room.frames
        time.sleep(0.3)
        assert room.frames == read
    finally:
        tracker.close()
    assert tracker._thread is not None and not tracker._thread.is_alive()


# ----- against the robot's own kinematics and detector, where they are installed -----


def test_the_gaze_fits_the_robots_reach_under_every_animation_and_never_sends_what_it_refuses() -> None:
    pytest.importorskip('reachy_mini')
    from robot_agent.adapters.robot import reach  # noqa: PLC0415

    fits = reach()
    assert fits is not None
    assert fits(np.eye(4), 0.0) and not fits(animations.pose(yaw=70.0), 0.0)  # past ~60° the daemon turns the body
    assert not fits(rot_z(100 * DEG) @ animations.pose(yaw=70.0), 100 * DEG)
    corners = [(y * gaze.MAX_YAW, p) for y in (-1, -0.5, 0, 0.5, 1) for p in (-gaze.MAX_UP, 0.0, gaze.MAX_DOWN)]
    for mode, variants in animations.VARIANTS.items():  # the microphone open or not, no wobble: the whole range fits
        for key in variants:
            play = animations.animate(mode, key)
            for t in np.arange(0.0, 30.0, 1.5):
                assert all(fits(gaze.aimed(play(t)[0], yaw, pitch, 1.0), 0.0) for yaw, pitch in corners), (mode, key)

    # Talking at full voice to a face at the corner of the range: the wobble does not always fit, the gaze gives way.
    mixer = _awake('talking', speech=True, reach=fits)
    rng = np.random.default_rng(7)
    mixer.hear((0.8 * rng.standard_normal(16000 * 12)).astype(np.float32), 0.0, 16000)
    refused = aimed = 0
    for n in range(1, 360):
        mixer.aim.see(gaze.MAX_YAW, -gaze.MAX_UP, n / 30)
        head, _, body = mixer.sample(n / 30)
        refused += not fits(rot_z(body) @ head, body)
        aimed += mixer.aim.weight > 0.9 and mixer.aim.room > 0.5
    assert refused == 0 and aimed > 200


def test_yunet_loads_and_finds_nobody_in_an_empty_frame() -> None:
    cv2 = pytest.importorskip('cv2')
    assert gaze.MODEL.is_file()
    detect = gaze.yunet(cv2)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert detect('middle', frame[:480, 320:960], gaze.SEARCH_WIDTH, gaze.FIND_SCORE) == []
    assert detect('crop', frame[:200, :200], gaze.CROP, gaze.KEEP_SCORE) == []
    lens = gaze.Lens(K, np.array([0.1, -0.05, 0.0, 0.0, 0.0]), cv2)  # OpenCV's model: there and back
    (x, y), = lens.plane([(900.0, 200.0)])  # fmt: skip
    assert lens.pixel(np.array([x, y, 1.0])) == pytest.approx((900.0, 200.0), abs=0.5)
