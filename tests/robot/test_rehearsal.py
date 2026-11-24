"""Rehearsals on the robot: the performer waits silent and still for the human, until a tap, on a fake clock;
the antenna's tap."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from mac_server import contracts
from robot_agent import animations, scene
from robot_agent.antennas import HOLD_SAMPLES, STEADY_S, AntennaWiggle
from robot_agent.config import ANTENNA_POLL_HZ
from robot_agent.motion import MotionRunner
from tests.robot.conftest import FakeAudio, until
from tests.robot.test_main import _robot, robot_parts  # noqa: F401 — the fixture, reused
from tests.robot.test_scene import TIMELINE, Clock, Emotions, SceneMac, Stage, TimedBody, cue

NARRATION = {'key': 'k1', 'duration_ms': 1700, 'character': 'NARRATEUR', 'text': 'Il était une fois.'}
REHEARSAL = {
    'scene_id': 'courte',
    'title': 'Courte',
    'duration_ms': 5400,
    'moves': True,
    'rehearse': 'MARCO',
    'cues': [
        cue(0, 'audio', 4, **NARRATION),
        cue(300, 'emotion', 4, name='cheerful1'),  # the narrator's: played
        cue(1700, 'audio', 6, duration_ms=0, character='MARCO', text='Ciao. <rire>'),
        cue(1700, 'emotion', 6, name='sad1'),  # MARCO's, in his line: the robot holds still
        cue(1700, 'audio', 6, duration_ms=0, character='MARCO', text='Je pars.'),  # the same turn, split by a move
        cue(1700, 'turn', 6, degrees=35.0),
        cue(3700, 'audio', 8, **NARRATION),  # after a 2 s pause
        cue(4000, 'gesture', 8, name='nod'),
    ],
}


class Script(Clock):
    """The fake clock, running each (seconds from the start, job) once it passes it."""

    def __init__(self, *jobs: tuple[float, Any]) -> None:
        super().__init__()
        self.jobs = sorted(jobs, key=lambda job: job[0])

    async def sleep(self, delay: float) -> None:
        self.now = round(self.now + delay, 6)  # no drift over hundreds of polls
        while self.jobs and self.now >= 1000.0 + self.jobs[0][0]:
            self.jobs.pop(0)[1]()
        await asyncio.sleep(0)


class Body(TimedBody):
    def play_move(self, move: Any) -> None:
        if move is scene.STILL:
            self.log.append((round(self.clock() - 1000.0, 2), 'still', None))
            self.playing = move
        else:
            super().play_move(move)

    def stop_moves(self) -> None:
        self.log.append((round(self.clock() - 1000.0, 2), 'stop', None))
        super().stop_moves()


def play(timeline: dict[str, Any], *jobs: tuple[float, Any]) -> Any:
    """The timeline performed on the fake clock; each job (seconds, job(performer)) runs once the clock passes it."""
    held: list[scene.Performer] = []
    clock = Script(*((at, lambda job=job: job(held[0])) for at, job in jobs))
    body, mac, stage = Body(clock), SceneMac(timeline), Stage(clock)
    held.append(scene.Performer(mac, FakeAudio(), body, Emotions(), clock, clock.sleep))
    result = asyncio.run(held[0].perform(timeline, stage, 's1'))
    return SimpleNamespace(result=result, body=body, stage=stage, mac=mac, player=held[0])


def starts(stage: Stage) -> list[float]:
    return sorted({round(at, 2) for at, _ in stage.pushes})


def states(mac: SceneMac) -> list[tuple[str, Any, Any]]:
    for event in mac.events:
        contracts.SceneEvent.model_validate(event)
    return [(e['state'], e.get('line'), e.get('message')) for e in mac.events]


def test_a_rehearsal_waits_silent_and_still_then_goes_on_as_written_at_a_tap() -> None:
    seen: list[bool] = []
    run = play(REHEARSAL, (10.0, lambda p: (seen.append(p.waiting), p.tap())))
    assert run.result.status == 'ok' and not run.player.waiting
    assert seen == [True]  # waiting for a tap
    assert starts(run.stage) == [0.0, 12.0]  # silent while the human speaks; after the tap, the 2 s pause, the voice
    assert run.body.log == [
        (0.45, 'move', 'cheerful1'),
        (1.85, 'still', None),  # held still: neither sad1 nor the turn of MARCO's line
        (10.0, 'stop', None),
        (12.45, 'gesture', 'nod'),  # as long after the tap as written after MARCO's line
    ]
    assert states(run.mac) == [
        ('started', None, None),
        ('line', 4, None),
        ('waiting', 6, 'Ciao. <rire> Je pars.'),  # one turn for MARCO's line, split by a move
        ('line', 8, None),
        ('ended', None, None),
    ]
    assert sorted(set(run.mac.fetched)) == ['/api/scenes/audio/k1?rate=16000']  # no audio for MARCO


def test_a_stop_ends_a_wait_at_once() -> None:
    run = play(REHEARSAL, (5.0, lambda p: p.stop('antenna')))
    assert 'arrêtée à la main' in run.result.message and run.stage.flushes == 1 and starts(run.stage) == [0.0]
    assert states(run.mac)[-1] == ('stopped', None, 'Arrêtée à la main, par une antenne.')
    assert [kind for at, kind, _ in run.body.log if at >= 1.85] == ['still', 'stop', 'stop']  # still, then stopped
    assert not run.player.busy and not run.player.waiting


def test_a_try_over_a_humans_turn_ends_in_the_still_hold_again() -> None:
    """« Essayer » in the studio while the robot waits for the human's line (main._try plays it over the hold): the
    try plays, then the robot holds still again until the tap. The real motion engine, stepped by hand."""
    seen: list[Any] = []
    held: list[scene.Performer] = []
    clock = Script(
        (5.0, lambda: body.play_move(animations.GESTURES['nod'])),  # the try, 1 s
        (7.0, lambda: body.mixer.sample(clock())),  # the motion thread's frame, past its end
        (7.5, lambda: seen.append(body.current_move())),
        (9.0, lambda: held[0].tap()),
    )
    body = MotionRunner(SimpleNamespace(), clock=clock)
    body.set_mode('idle', blend_s=0.0)  # awake
    held.append(scene.Performer(SceneMac(REHEARSAL), FakeAudio(), body, Emotions(), clock, clock.sleep))
    assert asyncio.run(held[0].perform(REHEARSAL, Stage(clock), 's1')).status == 'ok'
    assert seen == [scene.STILL]


def test_without_moves_the_robot_only_speaks() -> None:
    run = play({**TIMELINE, 'moves': False})
    assert run.result.status == 'ok' and run.body.log == []  # no emotion, gesture or turn, nor a turn back
    assert starts(run.stage) == [0.0, 1.4, 5.4]
    assert [s for s, _, _ in states(run.mac)] == ['started', 'line', 'line', 'line', 'ended']


# ----- the antenna's tap -----

STEADY = round(STEADY_S * ANTENNA_POLL_HZ)


def _samples(watch: AntennaWiggle, readings: list[tuple[float, float]], command: tuple[float, float], t0: float):
    """Each reading against `command`, one poll apart from `t0`: the times it ended a tap at."""
    period = 1.0 / ANTENNA_POLL_HZ
    return [round(t0 + n * period, 3) for n, p in enumerate(readings) if watch.tapped(p, command, t0 + n * period)]


def test_a_flick_is_a_tap_once_the_command_rests_and_a_hold_never_is() -> None:
    watch = AntennaWiggle(SimpleNamespace(), lambda: None)
    rest, flick, hand = (-0.17, 0.17), (-0.17, 0.27), (-0.17, 1.2)
    assert _samples(watch, [rest] * 3 + [flick] * 3 + [rest] * 3, rest, 0.0) == []  # too soon: not settled
    readings = [rest] * STEADY + [flick] * 3 + [rest] * 3 + [flick] * 2 + [rest]
    tapped = _samples(watch, readings, rest, 1.0)
    assert len(tapped) == 2  # on each release, once
    assert _samples(watch, [hand] * (HOLD_SAMPLES + 2) + [rest] * 2, rest, 3.0) == []  # a hold is no tap


def test_a_lagging_motor_is_no_tap() -> None:
    watch = AntennaWiggle(SimpleNamespace(), lambda: None)
    period = 1.0 / ANTENNA_POLL_HZ
    taps = []
    for n in range(60):  # the command blends 0.3 rad in 0.5 s, the motor 0.08 rad behind, then catching up
        command = (-0.17, 0.17 + min(0.3, 0.02 * n))
        lag = 0.08 if n < 15 else max(0.0, 0.08 - 0.02 * (n - 15))
        taps.append(watch.tapped((command[0], command[1] - lag), command, n * period))
    assert not any(taps)


def test_wake_hold_and_tap_each_call_their_own() -> None:
    calls: list[str] = []
    state = {'holding': True, 'tapping': True}
    rest = (-0.17, 0.17)
    watch = AntennaWiggle(
        SimpleNamespace(),
        lambda: calls.append('wake'),
        holding=lambda: state['holding'],
        commanded=lambda: rest,
        on_hold=lambda: calls.append('hold'),
        tapping=lambda: state['tapping'],
        on_tap=lambda: calls.append('tap'),
    )
    clock = iter(n / ANTENNA_POLL_HZ for n in range(1000))
    for position in [rest] * STEADY + [(-0.17, 0.27)] * 2 + [rest]:
        watch.sample(position, next(clock))
    assert calls == ['tap']
    for position in [(-0.17, 1.2)] * HOLD_SAMPLES + [rest] * 2:
        watch.sample(position, next(clock))
    assert calls == ['tap', 'hold']
    state['tapping'] = False  # a scene, no human's turn: a flick is nothing
    for position in [rest] * STEADY + [(-0.17, 0.27)] * 2 + [rest]:
        watch.sample(position, next(clock))
    assert calls == ['tap', 'hold']
    state['holding'] = False
    watch.arm()
    for position in [rest, (-0.17, 0.27)]:
        watch.sample(position, next(clock))
    assert calls == ['tap', 'hold', 'wake']


def test_the_antenna_thread_taps_a_waiting_scene(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._performer = SimpleNamespace(stop=calls.append, tap=lambda: calls.append('tap'), busy=True, waiting=True)
        robot._antennas.start()
        await asyncio.sleep(STEADY_S + 0.2)  # the command at rest
        robot_parts.mini.antennas = [robot_parts.mini.antennas[0], robot_parts.mini.antennas[1] + 0.1]
        await asyncio.sleep(0.1)
        robot_parts.mini.antennas = [robot_parts.mini.antennas[0], robot_parts.mini.antennas[1] - 0.1]
        await until(lambda: calls == ['tap'])
        robot._antennas.stop()

    calls: list[str] = []
    asyncio.run(asyncio.wait_for(scenario(), 5.0))
