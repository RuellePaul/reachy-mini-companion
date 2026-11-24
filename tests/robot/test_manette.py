"""The robot as an app's controller: it holds still and writes nothing, frees only its profile's motors once it stands
where it was told, and takes them back (targets pinned to the present pose) only once let go within the IK's reach,
however it ends. The real motion thread, a robot that records what it is sent and reports the pose it was given (or
the one a hand left it in)."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from google.genai import types

from robot_agent import animations, apps, config, manette, take
from robot_agent.emotions import CURATED
from robot_agent.motion import SLEEP_ANTENNAS, SLEEP_HEAD, MotionRunner, rot_z
from robot_agent.tools import ToolResult, Tools
from tests.robot.conftest import FakeAudio, FakeBody, FakeCamera, FakeEmotions, FakeMac, until


class Mini:
    """Records, in order, every target written and every torque change; its pose is the last one written, or the
    one `hand` holds it in."""

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.lock = threading.Lock()
        self.head, self.antennas, self.body = animations.pose(), animations.NEUTRAL, 0.0
        self.hand: tuple[np.ndarray, np.ndarray, float] | None = None
        self.stuck = False  # the head does not reach its target (a hand, a lagging motor)

    def _add(self, kind: str, value: Any) -> None:
        with self.lock:
            self.log.append((kind, value))

    def set_target_head_pose(self, pose: np.ndarray) -> None:
        self._add('head', pose)
        if not self.stuck:
            self.head = pose

    def set_target_antenna_joint_positions(self, antennas: list[float]) -> None:
        self._add('antennas', antennas)
        self.antennas = np.asarray(antennas)

    def set_target_body_yaw(self, yaw: float) -> None:
        self._add('body', yaw)
        self.body = yaw

    def disable_motors(self, ids: list[str] | None = None) -> None:
        self._add('free', ids)

    def enable_motors(self, ids: list[str] | None = None) -> None:
        self._add('take', ids)

    def pose(self) -> tuple[np.ndarray, np.ndarray, float]:
        return self.hand or (self.head, self.antennas, self.body)

    def kinds(self) -> list[str]:
        with self.lock:
            return [kind for kind, _ in self.log]


def turned(base_deg: float, head_deg: float = 0.0, head: np.ndarray | None = None) -> tuple:
    """A pose a hand left: the base turned `base_deg`, the head `head_deg` off it."""
    local = (head if head is not None else animations.pose()) @ rot_z(math.radians(head_deg))
    return rot_z(math.radians(base_deg)) @ local, animations.NEUTRAL, math.radians(base_deg)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, 'MOTION_HZ', 200)
    monkeypatch.setattr(config, 'BODY_TURN_MIN_S', 0.02)
    monkeypatch.setattr(config, 'SLEEP_S', 0.05)
    monkeypatch.setattr(config, 'WAKE_S', 0.05)
    for name, value in (('SETTLE_S', 0.05), ('TAKEN_BACK_S', 0.01), ('STILL_S', 0.05), ('RETURN_POLL_S', 0.01)):
        monkeypatch.setattr(manette, name, value)
    mini = Mini()
    motion = MotionRunner(mini)
    motion.start((animations.pose(), animations.NEUTRAL, 0.0))
    motion.set_mode('idle', blend_s=0.01)
    events: list[dict[str, Any]] = []
    refusal: list[str | None] = [None]
    ear: list[bool] = []  # the ear lent (True) and given back (False), in order
    sent: list[tuple] = []  # the lent body's measures handed on: (at, joints, head, antennas)
    held = manette.Manette(
        mini, motion, events.append, lambda m: m.pose(), refusal=lambda: refusal[0], ear=ear.append,
        pose=lambda *measure: sent.append(measure),
    )  # fmt: skip
    yield SimpleNamespace(mini=mini, motion=motion, events=events, manette=held, refusal=refusal, ear=ear, sent=sent)
    motion.stop()


def _between(log: list[tuple[str, Any]], first: str, last: str) -> list[str]:
    """The targets written between the first `first` and the last `last`."""
    kinds = [kind for kind, _ in log]
    start, end = kinds.index(first), len(kinds) - 1 - kinds[::-1].index(last)
    return [kind for kind in kinds[start:end] if kind in ('head', 'antennas', 'body')]


def test_gachettes_hold_still_write_nothing_and_free_nothing(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('gachettes', grant=True)
        assert rig.manette.on and rig.motion.frozen
        assert rig.events == [{'kind': 'manette', 'state': 'on', 'profile': 'gachettes'}]
        written = len(rig.mini.log)
        await asyncio.sleep(0.1)
        assert len(rig.mini.log) == written  # nothing written while it is a controller
        assert rig.motion.moving_until() <= time.monotonic()  # once still, its mic is not covered
        await rig.manette.command(None)
        assert not rig.manette.on and not rig.motion.frozen
        assert rig.events[-1] == {'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'app'}
        assert 'free' not in rig.mini.kinds() and 'take' not in rig.mini.kinds()
        await until(lambda: len(rig.mini.log) > written)  # it moves again

    asyncio.run(scenario())


def test_voix_alone_lends_the_ear_and_nothing_of_the_body(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('voix', grant=True)
        assert rig.manette.on and not rig.manette.lent and not rig.motion.frozen
        assert rig.events == [{'kind': 'manette', 'state': 'on', 'profile': 'voix'}] and rig.ear == [True]
        rig.motion.play_move(apps.Hold(animations.pose(yaw=20), animations.NEUTRAL))  # the app's look: it plays
        assert isinstance(rig.motion.current_move(), apps.Hold)
        written = len(rig.mini.log)
        await until(lambda: len(rig.mini.log) > written)  # the robot goes on moving while it listens
        await rig.manette.command('voix')  # the lease, renewed
        assert rig.events[-1] == {'kind': 'manette', 'state': 'on', 'profile': 'voix'} and rig.ear == [True]
        await rig.manette.command(None)
        assert not rig.manette.on and rig.ear == [True, False]
        assert rig.events[-1] == {'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'app'}
        assert 'free' not in rig.mini.kinds() and 'take' not in rig.mini.kinds()
        assert isinstance(rig.motion.current_move(), apps.Hold)  # giving the ear back cuts no move

    asyncio.run(scenario())


def test_a_body_that_listens_is_held_as_that_body_and_lends_the_ear_from_its_on_to_its_end(rig) -> None:
    async def scenario() -> None:
        assert [manette.body_of(p) for p in ('tete', 'tete+voix', 'voix', None)] == ['tete', 'tete', None, None]
        await rig.manette.command('volant+voix', grant=True)
        assert rig.manette.lent and rig.motion.frozen and ('free', ['body_rotation']) in rig.mini.log
        assert rig.events[-1] == {'kind': 'manette', 'state': 'on', 'profile': 'volant+voix'} and rig.ear == [True]
        rig.mini.hand = turned(30)
        await rig.manette.leave('app')
        assert rig.ear == [True, False] and rig.mini.kinds().count('take') == 1
        assert rig.events[-1] == {'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'app'}
        rig.mini.hand = None
        await rig.manette.command('gachettes', grant=True)
        assert rig.ear == [True, False, False]  # a profile that does not listen lends no ear
        await rig.manette.command('voix+tete', grant=True)  # no such profile: ignored
        assert rig.manette.profile == 'gachettes'
        await rig.manette.leave('app')

    asyncio.run(scenario())


def test_the_lease_of_an_ear_alone_runs_out_like_any_other(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manette, 'LEASE_S', 0.1)
    monkeypatch.setattr(manette, 'WATCH_S', 0.02)

    async def scenario() -> None:
        await rig.manette.command('voix', grant=True)
        await until(lambda: not rig.manette.on, timeout=2.0)  # the dashboard closed: nobody renews
        assert rig.ear == [True, False]
        assert rig.events[-1] == {'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'lease'}

    asyncio.run(scenario())


def test_what_a_hand_does_with_the_body_is_taped_and_played_back_as_it_was(rig) -> None:
    """Pollen's marionette, through the one motion thread: the head swayed from shoulder to shoulder, looking ahead."""

    def carried(seconds: float) -> tuple:
        head = np.array(SLEEP_HEAD)
        head[1, 3] += 0.02 * math.sin(2 * math.pi * seconds)  # no angle changes: only where the head is
        return head, animations.NEUTRAL, 0.0

    async def scenario() -> None:
        await rig.manette.command('tete', grant=True)
        began = time.time()
        while time.time() - began < 1.0:  # a hand, one second
            rig.mini.hand = carried(time.time() - began)
            await asyncio.sleep(0.005)
        ended = time.time()
        rig.mini.hand = carried(0.0)
        await rig.manette.leave('app')
        rig.mini.hand = None
        cut = rig.manette.tape.take(began, ended)
        assert cut is not None and 0.8 <= cut.seconds <= 1.0  # the hold's whole second, a pose every 20 ms or so
        sways = [cut.evaluate(n * cut.seconds / 40)[0][1, 3] - SLEEP_HEAD[1, 3] for n in range(41)]
        assert max(sways) == pytest.approx(0.02, abs=0.003) and min(sways) == pytest.approx(-0.02, abs=0.003)
        # Played by a motion thread stepped by hand, on a clock of the test's own: what it writes, to the frame.
        clock = [100.0]
        played = Mini()
        runner = MotionRunner(played, clock=lambda: clock[0])
        starts = runner.play_take(cut, take.LEAD_IN_S)
        assert starts == pytest.approx(100.0 + take.LEAD_IN_S)  # the take waits to be met at its first pose
        for part in (0.0, 0.2, 0.5, 0.8, 0.95):  # (its last 10 ms are the mixer's way out of any move)
            clock[0] = starts + part * cut.seconds
            runner._step(clock[0])
            head, antennas, body = cut.evaluate(part * cut.seconds)
            assert np.allclose(played.head, rot_z(body) @ head, atol=1e-9)  # where the hand had it, as it had it
            assert np.allclose(played.head[:3, :3], SLEEP_HEAD[:3, :3], atol=1e-6)  # turned not at all: carried
            assert np.allclose(played.antennas, antennas) and played.body == body

    asyncio.run(scenario())


def test_the_lent_body_is_sent_on_as_measures_thirty_times_a_second_until_it_is_given_back(rig) -> None:
    """What the dashboard makes its pads from: the robot's own measures, each once, never the daemon's stream."""

    async def scenario() -> None:
        await rig.manette.command('volant', grant=True)
        rig.mini.hand = turned(40)  # a hand turns the base
        rig.sent.clear()
        began = time.time()
        await asyncio.sleep(0.5)
        during = list(rig.sent)
        assert 10 <= len(during) <= 17  # 30 a second, the pace of the stream it stands for
        at, joints, head, antennas = during[-1]
        assert began < at <= time.time() and [m[0] for m in during] == sorted({m[0] for m in during})  # each once
        assert len(joints) == 7 and joints[0] == pytest.approx(math.radians(40))  # the base's yaw first
        assert len(head) == 16 and np.allclose(np.asarray(head).reshape(4, 4), turned(40)[0])  # the head in the room
        assert list(antennas) == pytest.approx(list(animations.NEUTRAL))
        rig.mini.hand = None
        await rig.manette.leave('app')
        rig.sent.clear()
        await asyncio.sleep(0.15)
        assert rig.sent == []  # given back: nothing more is said of it
        await rig.manette.command('voix', grant=True)  # the ear alone lends no body: no measure
        await asyncio.sleep(0.15)
        await rig.manette.command(None)
        assert rig.sent == []

    asyncio.run(scenario())


def test_an_ear_alone_tapes_nothing_and_a_new_hold_starts_a_new_tape(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('gachettes', grant=True)
        began = time.time()
        await asyncio.sleep(0.4)
        await rig.manette.command(None)
        first = rig.manette.tape.take(began, time.time())
        assert first is not None  # kept after the hold, for the app's replay
        await rig.manette.command('voix', grant=True)
        await asyncio.sleep(0.1)
        await rig.manette.command(None)
        assert rig.manette.tape.take(began, time.time()) is not None  # no body lent: the tape is the last hold's
        await rig.manette.command('gachettes', grant=True)
        await asyncio.sleep(0.05)
        assert rig.manette.tape.take(began, began + 0.4) is None  # a body lent again: its own tape
        await rig.manette.command(None)

    asyncio.run(scenario())


def test_volant_frees_the_base_alone_and_takes_it_back_where_the_hand_left_it(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('volant', grant=True)
        assert ('free', ['body_rotation']) in rig.mini.log
        rig.mini.hand = turned(40)  # a quarter of a wheel turn, let go
        await rig.manette.leave('app')
        log = list(rig.mini.log)
        assert _between(log, 'free', 'take') == []  # a freed motor is given no target
        assert log[[kind for kind, _ in log].index('take')][1] is None  # global: every target pinned where it is
        rig.mini.hand = None
        await until(lambda: any(kind == 'head' for kind, _ in rig.mini.log[len(log) :]))
        first_head = next(value for kind, value in rig.mini.log[len(log) :] if kind == 'head')
        assert np.allclose(first_head, turned(40)[0], atol=0.02)  # it starts again from where it is: no snap
        assert rig.motion.mixer.turn.start == pytest.approx(math.radians(40)) and rig.motion.mixer.turn.end == 0.0
        assert rig.motion.mixer.turn.duration >= 40 / config.BODY_TURN_SPEED_DEG_S  # never faster than a usual turn

    asyncio.run(scenario())


def test_paume_frees_the_base_alone_from_a_pose_with_the_antennas_lowered_as_in_sleep(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the palm: the base turns by hand as a wheel does, the antennas lie where they do in sleep, under torque,
    for the fingers of that hand; the head stays upright, held."""
    monkeypatch.setattr(config, 'SLEEP_S', 0.3)  # the antennas lowered at the sleep move's pace

    async def scenario() -> None:
        assert manette.BODIES['paume'] == manette.BODIES['volant'] == ('body_rotation',)
        assert 'paume+voix' in manette.PROFILES and manette.body_of('paume+voix') == 'paume'
        entering = asyncio.create_task(rig.manette.command('paume', grant=True))
        await asyncio.sleep(0.1)  # « Reachy se met en place… »: its antennas on their way down
        still = rig.motion.current_move()
        assert isinstance(still, apps.Hold) and np.allclose(still.antennas, SLEEP_ANTENNAS)
        assert np.allclose(still.head, animations.pose())  # the head stays upright
        assert rig.motion.mixer.blend.duration == pytest.approx(0.3)  # at the sleep move's pace: ~165° to swing
        assert 'free' not in rig.mini.kinds() and not np.allclose(rig.mini.antennas, SLEEP_ANTENNAS, atol=0.1)
        await entering
        assert [ids for kind, ids in rig.mini.log if kind == 'free'] == [['body_rotation']]
        freed_at = rig.mini.kinds().index('free')
        lowered = [value for kind, value in rig.mini.log[:freed_at] if kind == 'antennas'][-1]
        assert np.allclose(lowered, SLEEP_ANTENNAS, atol=1e-3)  # lowered before the base is freed, and held there
        last_head = [value for kind, value in rig.mini.log[:freed_at] if kind == 'head'][-1]
        assert np.allclose(last_head, animations.pose(), atol=1e-3)
        assert rig.events == [{'kind': 'manette', 'state': 'on', 'profile': 'paume'}]
        rig.mini.hand = (turned(40)[0], SLEEP_ANTENNAS, math.radians(40))  # turned by a palm, then let go
        await rig.manette.leave('app')
        log = list(rig.mini.log)
        assert _between(log, 'free', 'take') == []  # a freed motor is given no target, the antennas none either
        assert log[[kind for kind, _ in log].index('take')][1] is None  # global: every target pinned where it is
        assert rig.events[-1] == {'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'app'}
        rig.mini.hand = None
        await until(lambda: any(kind == 'antennas' for kind, _ in rig.mini.log[len(log) :]))
        first = next(value for kind, value in rig.mini.log[len(log) :] if kind == 'antennas')
        assert np.allclose(first, SLEEP_ANTENNAS, atol=0.3)  # they start again from where they lie: no snap
        await until(lambda: np.allclose(rig.mini.antennas, animations.NEUTRAL, atol=0.3), timeout=3.0)  # then come up
        assert rig.motion.mixer.turn.start == pytest.approx(math.radians(40)) and rig.motion.mixer.turn.end == 0.0

    asyncio.run(scenario())


def test_the_torque_waits_for_the_base_brought_back_within_reach_and_let_go(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('volant', grant=True)
        rig.mini.hand = turned(170)  # past what the IK takes: it would jump the base
        leaving = asyncio.create_task(rig.manette.leave('app'))
        await until(lambda: rig.events[-1]['state'] == 'returning')
        await asyncio.sleep(0.1)
        assert 'take' not in rig.mini.kinds() and not leaving.done()  # still freed: it waits
        for degrees in (120, 90, 60):  # brought back by hand, still moving
            rig.mini.hand = turned(degrees)
            await asyncio.sleep(0.02)
            assert 'take' not in rig.mini.kinds()
        await asyncio.wait_for(leaving, 2.0)  # let go at 60°: taken back
        assert rig.mini.kinds().count('take') == 1 and rig.events[-1]['state'] == 'off'

    asyncio.run(scenario())


@pytest.mark.parametrize(('base', 'head'), [(10, 80), (140, 50)], ids=['head-off-its-base', 'world-yaw'])
def test_the_torque_waits_for_the_head_brought_back_within_reach_and_let_go(rig, base: float, head: float) -> None:
    async def scenario() -> None:
        await rig.manette.command('tete', grant=True)
        rig.mini.hand = turned(base, head, SLEEP_HEAD)  # the head past what the IK takes, on its base or in the world
        leaving = asyncio.create_task(rig.manette.leave('app'))
        await until(lambda: rig.events[-1]['state'] == 'returning')
        await asyncio.sleep(0.1)
        assert 'take' not in rig.mini.kinds() and not leaving.done()  # still freed: it waits
        rig.mini.hand = turned(10, 20, SLEEP_HEAD)
        await asyncio.wait_for(leaving, 2.0)
        assert rig.mini.kinds().count('take') == 1 and rig.events[-1]['state'] == 'off'

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ('base', 'head'),
    [(165, 0), (10, 66), (60, -66), (140, 45), (-165, 0), (-10, -66), (-60, 66), (-140, -45)],
)
def test_the_torque_never_comes_back_where_the_ik_would_jump(base: float, head: float) -> None:
    """The SDK's safe IK clamps the base at 160° and the head at 65° off it, and swings
    the long way past 180° of world yaw."""
    assert not manette.returnable(turned(base, head))
    assert manette.returnable(turned(base * 0.7, head * 0.7))  # brought back part of the way: within reach


def test_tete_frees_the_head_and_the_base_from_the_sleep_pose(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('tete', grant=True)
        freed = next(ids for kind, ids in rig.mini.log if kind == 'free')
        assert freed == ['body_rotation', *(f'stewart_{n}' for n in range(1, 7))]
        last_head = [value for kind, value in rig.mini.log if kind == 'head'][-1]
        assert np.allclose(last_head, SLEEP_HEAD, atol=1e-3)  # lowered before it is freed
        rig.mini.hand = turned(10, 30, SLEEP_HEAD)
        await rig.manette.leave('sleep')
        assert rig.events[-1]['why'] == 'sleep'

    asyncio.run(scenario())


@pytest.mark.parametrize('held', ['head', 'base'])
def test_nothing_is_freed_unless_the_robot_stands_where_it_was_told(rig, held: str) -> None:
    async def scenario() -> None:
        if held == 'head':
            rig.mini.stuck = True  # the head stays upright: a hand, or a lagging motor
        else:
            rig.mini.hand = turned(30, 0, SLEEP_HEAD)  # the head lowered as told, on a base held 30° off the front
        await rig.manette.command('tete', grant=True)
        assert 'free' not in rig.mini.kinds() and not rig.manette.on and not rig.motion.frozen
        assert rig.events[-1] == {'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'pose'}

    asyncio.run(scenario())


def test_an_entry_that_cannot_read_the_pose_gives_the_body_back_and_says_so(rig) -> None:
    async def scenario() -> None:
        reads = rig.mini.pose
        rig.mini.pose = lambda: None  # the daemon's state is not read (read_pose's None)
        await rig.manette.command('volant', grant=True)
        assert not rig.manette.on and not rig.motion.frozen and 'free' not in rig.mini.kinds()
        assert rig.events[-1] == {'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'pose'}
        rig.mini.pose = reads
        rig.motion.play_move(apps.Hold(animations.pose(z=0.01), animations.NEUTRAL))  # no longer lent: it plays
        assert rig.motion.current_move() is not None

    asyncio.run(scenario())


def test_an_entry_whose_exit_fails_is_given_back_once_nobody_renews(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manette, 'LEASE_S', 0.2)
    monkeypatch.setattr(manette, 'WATCH_S', 0.02)
    monkeypatch.setattr(manette, 'RETURN_WAIT_S', 0.1)

    async def scenario() -> None:
        reads = rig.mini.pose

        def refuse(ids: list[str]) -> None:  # frozen, then the daemon fails: no motor freed, no pose read
            rig.mini.pose = lambda: None
            raise RuntimeError('daemon busy')

        rig.mini.disable_motors = refuse
        with pytest.raises(RuntimeError):
            await rig.manette.command('volant', grant=True)
        assert rig.manette.on and rig.motion.frozen  # nothing written: held until an exit passes
        rig.mini.pose = reads
        await until(lambda: not rig.manette.on, timeout=2.0)  # the dashboard closed: given back
        assert not rig.motion.frozen and rig.events[-1]['why'] == 'pose'  # why the entry failed

    asyncio.run(scenario())


def test_an_entry_that_failed_is_never_said_on_nor_kept_by_renewals(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard still shows « Reachy se met en place… » and renews: no on for a motor maybe never freed, and the
    failed exit is tried again once the entry's lease runs out."""
    monkeypatch.setattr(manette, 'LEASE_S', 1.2)  # past the entry (~0.7 s): its first renewal comes within the lease
    monkeypatch.setattr(manette, 'WATCH_S', 0.02)
    monkeypatch.setattr(manette, 'RETURN_WAIT_S', 0.1)

    async def scenario() -> None:
        reads = rig.mini.pose

        def refuse(ids: list[str]) -> None:  # frozen, then the daemon fails: no motor freed, no pose read
            rig.mini.pose = lambda: None
            raise RuntimeError('daemon busy')

        rig.mini.disable_motors = refuse
        with pytest.raises(RuntimeError):
            await rig.manette.command('volant', grant=True)
        rig.mini.pose = reads
        await rig.manette.command('volant')  # renewed
        assert rig.events == []

        async def renew() -> None:
            while True:
                await asyncio.sleep(0.05)
                await rig.manette.command('volant')

        renewing = asyncio.create_task(renew())
        await until(lambda: not rig.manette.on, timeout=3.0)  # renewed all along, given back all the same
        renewing.cancel()
        assert not rig.motion.frozen
        assert rig.events[0] == {'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'pose'}
        assert all(event['state'] == 'off' for event in rig.events)  # the renewals after it: off (lease)

    asyncio.run(scenario())


def test_a_lease_exit_whose_torque_fails_is_tried_again_until_it_passes(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manette, 'LEASE_S', 0.2)
    monkeypatch.setattr(manette, 'WATCH_S', 0.02)
    monkeypatch.setattr(manette, 'RETURN_WAIT_S', 0.1)

    async def scenario() -> None:
        failures, take = [manette.TAKE_BACK_ATTEMPTS], rig.mini.enable_motors

        def flaky(ids: list[str] | None = None) -> None:  # every attempt of the first exit fails, then it passes
            if failures[0]:
                failures[0] -= 1
                rig.mini._add('fail', ids)
                raise RuntimeError('daemon busy')
            take(ids)

        rig.mini.enable_motors = flaky
        await rig.manette.command('volant', grant=True)  # never renewed: the lease runs out
        await until(lambda: rig.mini.kinds().count('fail') == manette.TAKE_BACK_ATTEMPTS, timeout=2.0)
        assert rig.manette.on and rig.motion.frozen  # the exit failed: still held, frozen
        await until(lambda: not rig.manette.on, timeout=3.0)  # the watcher tries again a second later
        assert rig.mini.kinds().count('take') == 1 and _between(rig.mini.log, 'free', 'take') == []
        assert rig.events[-1]['why'] == 'lease' and not rig.motion.frozen

    asyncio.run(scenario())


def test_a_move_on_its_way_when_the_app_takes_the_body_is_dropped(rig, caplog: pytest.LogCaptureFixture) -> None:
    """Dropped, not merely outlasted by the entry's second attempt: the still pose is never replaced."""

    async def scenario() -> None:
        entering = asyncio.create_task(rig.manette.command('tete', grant=True))
        await asyncio.sleep(0.01)  # it is lowering its head
        assert rig.manette.on  # the model's moves are refused from now (Tools.lent)
        still = rig.motion.current_move()
        assert isinstance(still, apps.Hold) and np.allclose(still.head, SLEEP_HEAD)  # its still pose, on its way
        rig.motion.play_move(apps.Hold(animations.pose(z=0.01), animations.NEUTRAL))  # an app move in flight
        rig.motion.turn_body(60.0)  # a turn in flight
        rig.motion.stop_moves()  # a conversation ending in flight
        assert rig.motion.current_move() is still and rig.motion.mixer.turn.end == 0.0
        await entering
        last_head = [value for kind, value in rig.mini.log if kind == 'head'][-1]
        assert np.allclose(last_head, SLEEP_HEAD, atol=1e-3) and rig.mini.body == pytest.approx(0.0)
        assert 'not reached' not in caplog.text  # in place at the first attempt
        await rig.manette.leave('app')
        nod = animations.GESTURES['nod']
        rig.motion.play_move(nod)  # let go: the body is no longer lent
        assert rig.motion.current_move() is nod

    asyncio.run(scenario())


def test_a_renewal_never_takes_the_body_again_only_a_grant_does(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manette, 'LEASE_S', 0.3)
    monkeypatch.setattr(manette, 'WATCH_S', 0.02)

    async def scenario() -> None:
        await rig.manette.command('volant')  # a renewal with nothing in force: the hold ended here
        assert not rig.manette.on
        assert rig.events == [{'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'lease'}]
        await rig.manette.command('volant', grant=True)
        for _ in range(4):  # renewed: it holds
            await asyncio.sleep(0.15)
            await rig.manette.command('volant')
        assert rig.manette.on and rig.mini.kinds().count('free') == 1  # entered once
        started = time.monotonic()
        await until(lambda: rig.events[-1]['state'] == 'off', timeout=2.0)
        assert time.monotonic() - started >= 0.2 and rig.events[-1]['why'] == 'lease'
        await rig.manette.command('volant')  # a late renewal: no second hold
        assert not rig.manette.on and rig.mini.kinds().count('free') == 1

    asyncio.run(scenario())


def test_the_hold_in_force_is_said_again_at_each_renewal_and_grant(rig) -> None:
    """An on lost on its way, or a dashboard that forgot its hold and asks the human again, is put right by the
    dashboard's next word: the robot answers it with the hold it has, and takes nothing again."""

    async def scenario() -> None:
        on = {'kind': 'manette', 'state': 'on', 'profile': 'volant'}
        await rig.manette.command('volant', grant=True)
        rig.events.clear()  # that on never reached the Mac
        await rig.manette.command('volant')  # renewed
        assert rig.events == [on]
        await rig.manette.command('volant', grant=True)  # granted anew by a dashboard that forgot it held
        assert rig.events == [on, on]
        assert rig.mini.kinds().count('free') == 1 and 'take' not in rig.mini.kinds()  # held all along
        await rig.manette.leave('app')

    asyncio.run(scenario())


def test_a_switch_that_outlasts_a_lease_keeps_the_new_profile(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The base back to the front at half pace, then the new still pose: longer than a lease, the dashboard's renewals
    waiting meanwhile behind the switch."""
    monkeypatch.setattr(config, 'BODY_TURN_MIN_S', 0.1)
    monkeypatch.setattr(manette, 'LEASE_S', 0.15)
    monkeypatch.setattr(manette, 'WATCH_S', 0.02)

    async def scenario() -> None:
        dashboard = ['volant']

        async def renew() -> None:  # each renewal spawned, as Robot._command does
            while True:
                await asyncio.sleep(0.05)
                asyncio.ensure_future(rig.manette.command(dashboard[0]))

        await rig.manette.command('volant', grant=True)
        renewing = asyncio.create_task(renew())
        rig.mini.body = math.radians(10)  # the wheel turned by hand and let go
        dashboard[0] = 'tete'
        started = time.monotonic()
        await rig.manette.command('tete', grant=True)
        assert time.monotonic() - started > manette.LEASE_S  # the switch outlasted a lease
        await asyncio.sleep(0.3)
        renewing.cancel()
        assert rig.manette.profile == 'tete'
        said = [(e['state'], e.get('profile'), e.get('why')) for e in rig.events]  # each renewal answered: on again
        changes = [word for i, word in enumerate(said) if not i or word != said[i - 1]]
        assert changes == [('on', 'volant', None), ('off', None, 'profile'), ('on', 'tete', None)]
        await rig.manette.leave('app')

    asyncio.run(scenario())


def test_a_grant_while_the_robot_waits_to_be_let_go_holds(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manette, 'LEASE_S', 0.15)
    monkeypatch.setattr(manette, 'WATCH_S', 0.02)

    async def scenario() -> None:
        await rig.manette.command('volant', grant=True)
        rig.mini.hand = turned(170)  # « Rendre la main », the base past the IK's reach
        leaving = asyncio.create_task(rig.manette.command(None))
        await until(lambda: rig.events[-1]['state'] == 'returning')
        granting = asyncio.create_task(rig.manette.command('gachettes', grant=True))  # asked again, said yes
        await asyncio.sleep(0.3)  # the old lease runs out while both wait
        rig.mini.hand = None
        await asyncio.wait_for(asyncio.gather(leaving, granting), 2.0)
        await asyncio.sleep(0.05)  # < LEASE_S: the new hold is in force
        assert rig.manette.profile == 'gachettes' and rig.events[-1]['state'] == 'on'
        await rig.manette.leave('app')

    asyncio.run(scenario())


@pytest.mark.parametrize('why', ['app', 'scene', 'sleep'])
def test_a_let_go_during_the_entry_frees_nothing(rig, monkeypatch: pytest.MonkeyPatch, why: str) -> None:
    monkeypatch.setattr(config, 'SLEEP_S', 0.3)  # the head lowered at the sleep move's pace

    async def scenario() -> None:
        entering = asyncio.create_task(rig.manette.command('tete', grant=True))
        await asyncio.sleep(0.05)  # « Reachy se met en place… », « Rendre la main » (or a scene, a sleep)
        leaving = asyncio.create_task(rig.manette.command(None) if why == 'app' else rig.manette.leave(why))
        await asyncio.wait_for(asyncio.gather(entering, leaving), 3.0)
        assert 'free' not in rig.mini.kinds() and not rig.manette.on and not rig.motion.frozen
        assert rig.events == [{'kind': 'manette', 'state': 'off', 'profile': None, 'why': why}]
        await rig.manette.command('gachettes', grant=True)  # asked after it: a let-go withdraws only what came before
        assert rig.manette.on
        await rig.manette.leave('app')

    asyncio.run(scenario())


def test_a_let_go_during_a_switch_never_starts_the_new_profile(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('volant', grant=True)
        rig.mini.hand = turned(170)  # the wheel past reach: the switch waits to be let go
        switching = asyncio.create_task(rig.manette.command('tete', grant=True))
        await until(lambda: rig.events[-1]['state'] == 'returning')
        leaving = asyncio.create_task(rig.manette.command(None))
        rig.mini.hand = None
        written = len(rig.mini.log)
        await asyncio.wait_for(asyncio.gather(switching, leaving), 3.0)
        assert [(e['state'], e.get('why')) for e in rig.events] == [
            ('on', None), ('returning', None), ('off', 'profile'), ('off', 'app'),
        ]  # fmt: skip
        heads = [value for kind, value in rig.mini.log[written:] if kind == 'head']
        assert not any(np.allclose(head, SLEEP_HEAD, atol=1e-3) for head in heads)  # the head never lowered
        assert rig.mini.kinds().count('free') == 1 and not rig.manette.on

    asyncio.run(scenario())


def test_the_renewals_of_a_switch_let_go_end_nothing_more(rig) -> None:
    """Renewals of the new profile wait behind the switch; the let-go that withdraws it says its end: they add none,
    or the dashboard would take one for the end of the grant that follows."""

    async def scenario() -> None:
        await rig.manette.command('volant', grant=True)
        rig.mini.hand = turned(170)  # the wheel past reach: the switch waits to be let go
        tasks = [asyncio.create_task(rig.manette.command('tete', grant=True))]
        await until(lambda: rig.events[-1]['state'] == 'returning')
        for _ in range(2):  # renewed every second meanwhile
            tasks.append(asyncio.create_task(rig.manette.command('tete')))
            await asyncio.sleep(0.02)
        tasks.append(asyncio.create_task(rig.manette.command(None)))  # « Rendre la main »
        await asyncio.sleep(0.02)
        tasks.append(asyncio.create_task(rig.manette.command('gachettes', grant=True)))  # « Rejouer »
        await asyncio.sleep(0.02)
        rig.mini.hand = None
        await asyncio.wait_for(asyncio.gather(*tasks), 5.0)
        assert [(e['state'], e.get('profile'), e.get('why')) for e in rig.events] == [
            ('on', 'volant', None), ('returning', 'volant', None), ('off', None, 'profile'), ('off', None, 'app'),
            ('on', 'gachettes', None),
        ]  # fmt: skip
        await rig.manette.leave('app')

    asyncio.run(scenario())


def test_no_hold_while_a_scene_plays_or_the_conversation_ends(rig) -> None:
    async def scenario() -> None:
        for why in ('scene', 'sleep'):
            rig.refusal[0] = why
            await rig.manette.command('gachettes', grant=True)
            assert not rig.manette.on
            assert rig.events[-1] == {'kind': 'manette', 'state': 'refused', 'profile': None, 'why': why}

    asyncio.run(scenario())


def test_another_profile_takes_the_body_back_first(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('volant', grant=True)
        await rig.manette.command('gachettes', grant=True)
        kinds = rig.mini.kinds()
        assert kinds.index('take') > kinds.index('free') and rig.manette.profile == 'gachettes'
        assert [e['state'] for e in rig.events] == ['on', 'off', 'on'] and rig.events[1]['why'] == 'profile'
        await rig.manette.leave('app')

    asyncio.run(scenario())


def test_a_torque_that_will_not_come_back_keeps_everything_frozen(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('volant', grant=True)
        failures = [RuntimeError('daemon busy')] * 3

        def refuse(ids: list[str] | None = None) -> None:
            if failures:
                raise failures.pop()
            rig.mini._add('take', ids)

        rig.mini.enable_motors = refuse
        with pytest.raises(RuntimeError):
            await rig.manette.leave('app')
        assert rig.manette.on and rig.motion.frozen  # nothing written onto a freed base
        assert 'take' not in rig.mini.kinds()
        await rig.manette.leave('sleep')  # the next exit tries again
        assert not rig.manette.on and not rig.motion.frozen and rig.mini.kinds().count('take') == 1

    asyncio.run(scenario())


def test_the_agents_end_puts_the_torque_back(rig) -> None:
    async def scenario() -> None:
        await rig.manette.command('volant', grant=True)
        rig.manette.take_back_now()  # Robot.run's last word, whatever ended it
        assert rig.mini.kinds()[-1] == 'take'

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ('name', 'args'),
    [
        ('do_gesture', {'name': 'nod'}),
        ('play_emotion', {'name': CURATED[0]}),
        ('turn_body', {'angle': 45}),
        ('play_scene', {'title': 'La mer'}),  # a subject's tool, through `extra`
    ],
)
def test_its_moves_are_refused_while_it_is_a_controller(name: str, args: dict[str, Any]) -> None:
    async def scenario() -> None:
        profile: list[str | None] = ['volant']
        body, audio, scenes = FakeBody(), FakeAudio(), list[dict[str, Any]]()

        async def play_scene(asked: dict[str, Any], _send: object) -> ToolResult:
            scenes.append(asked)
            return ToolResult('ok', 'Scène jouée.')

        tools = Tools(
            body=body, audio=audio, camera=FakeCamera(), mac=FakeMac(), emotions=FakeEmotions(),
            extra={'play_scene': (types.FunctionDeclaration(name='play_scene'), play_scene)}, lent=lambda: profile[0],
        )  # fmt: skip
        refused = await tools.run(name, args, None)
        assert refused.status == 'error' and 'manette' in refused.message
        assert (body.gestures, body.moves, body.turns, audio.sounds, scenes) == ([], [], [], [], [])
        profile[0] = None
        assert (await tools.run(name, args, None)).status == 'ok'  # let go, the same call plays
        assert [*body.gestures, *body.moves, *body.turns, *scenes]

    asyncio.run(scenario())
