"""The scene performer on fake hardware and a fake clock, and a scene played through a live session."""

from __future__ import annotations

import asyncio
import io
import math
import struct
import threading
import time
import wave
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from google.genai import types
from pydantic import TypeAdapter

from mac_server import contracts, scene_text
from mac_server.scenes import ASSETS
from robot_agent import config, scene
from robot_agent.antennas import HOLD_SAMPLES, AntennaWiggle
from robot_agent.mac import MacError
from robot_agent.main import Robot
from robot_agent.motion import SLEEP_ANTENNAS, SLEEP_HEAD, MotionRunner
from robot_agent.session import _PERFORMING
from robot_agent.state import SessionState
from robot_agent.tools import SILENT
from tests.robot.conftest import (
    FakeAudio,
    FakeBody,
    FakeMac,
    Rig,
    call,
    content,
    heard,
    settings_view,
    speech,
    until,
    vad,
)
from tests.robot.test_main import FakeMacServer, _robot, robot_parts  # noqa: F401 — the fixture, reused

S = SessionState


def tone_wav(seconds: float, rate: int = 24000) -> bytes:
    samples = [int(0.3 * 32767 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(int(seconds * rate))]
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack(f'<{len(samples)}h', *samples))
    return out.getvalue()


def cue(at_ms: int, kind: str, line: int, **fields: Any) -> dict[str, Any]:
    base = {'key': None, 'duration_ms': None, 'character': None, 'text': None, 'name': None, 'degrees': None}
    return {'at_ms': at_ms, 'kind': kind, 'line': line, **base, **fields}


TIMELINE = {
    'scene_id': 'courte',
    'title': 'Courte',
    'duration_ms': 7100,
    'cues': [
        cue(0, 'audio', 4, key='k1', duration_ms=1700, character='NARRATEUR', text='Il était une fois.'),
        cue(300, 'emotion', 6, name='sad1'),
        cue(1700, 'audio', 6, key='k2', duration_ms=1700, character='MARCO', text='Ciao.'),
        cue(3400, 'turn', 6, degrees=35.0),
        cue(5400, 'audio', 8, key='k1', duration_ms=1700, character='NARRATEUR', text='Il était une fois.'),
        cue(5700, 'gesture', 8, name='nod'),
    ],
}


class SceneMac(FakeMac):
    """The Mac's scene routes: the timeline found by title, and each segment's WAV."""

    def __init__(self, timeline: dict[str, Any], seconds: float = 1.7) -> None:
        super().__init__({'/api/scenes/find': {'timeline': timeline, 'titles': []}})
        self.wav = tone_wav(seconds)
        self.fetched: list[str] = []
        self.fail: str | None = None
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def get_bytes(self, path: str) -> bytes:
        self.fetched.append(path)
        if self.fail:
            raise MacError(self.fail)
        return self.wav


class Clock:
    """A clock that a sleep moves forward at once; `at` runs a callback once the clock passes a time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.at: tuple[float, Any] | None = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay
        if self.at and self.now >= self.at[0]:
            _, job, self.at = *self.at, None
            job()
        await asyncio.sleep(0)


class Stage:
    def __init__(self, clock: Clock) -> None:
        self.clock, self.playback_end, self.pushes, self.flushes = clock, 0.0, [], 0

    def stage_audio(self, samples: np.ndarray, seconds: float) -> None:
        self.pushes.append((round(self.clock() - 1000.0, 3), samples))
        self.playback_end = max(self.playback_end, self.clock()) + seconds

    def flush(self) -> None:
        self.flushes += 1


class TimedBody(FakeBody):
    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock, self.log, self.speeds, self.cuts = clock, [], [], []

    def turn_body(self, degrees: float, speed: float = 1.0) -> float:
        turned, target = len(self.turns), super().turn_body(degrees, speed)
        if len(self.turns) > turned:  # else it already faces there: nothing happens
            self.log.append((round(self.clock() - 1000.0, 3), 'turn', degrees))
            self.speeds.append(speed)
        return target

    def stop_moves(self) -> None:
        self.cuts.append(round(self.clock() - 1000.0, 3))
        super().stop_moves()

    def gesture(self, name: str) -> bool:
        self.log.append((round(self.clock() - 1000.0, 3), 'gesture', name))
        return super().gesture(name)

    def play_move(self, move: Any) -> None:
        self.log.append((round(self.clock() - 1000.0, 3), 'move', move.name))
        super().play_move(move)


class Emotions:
    def move(self, name: str) -> Any:
        return SimpleNamespace(name=name, duration=1.0)


def performer(clock: Clock, mac: SceneMac | None = None) -> tuple[scene.Performer, TimedBody, list[dict[str, Any]]]:
    body, mac = TimedBody(clock), mac or SceneMac(TIMELINE)
    return scene.Performer(mac, FakeAudio(), body, Emotions(), clock, clock.sleep), body, mac.events


def test_the_voices_and_moves_play_on_the_timeline() -> None:
    clock = Clock()
    player, body, events = performer(clock)
    stage = Stage(clock)
    result = asyncio.run(player.perform(TIMELINE, stage, 's1'))
    assert result.status == 'ok' and 'jouée' in result.message and not player.busy
    starts = sorted({at for at, _ in stage.pushes})
    assert starts == [0.0, 1.4, 5.4]  # back to back: queued LEAD_S early; after the silence: on time
    assert all(samples.size for _, samples in stage.pushes)  # never an empty push
    assert sum(s.size for _, s in stage.pushes) == pytest.approx(3 * 1.7 * 16000, abs=30)  # resampled to the speaker
    assert max(s.size for _, s in stage.pushes) <= scene.CHUNK_S * 16000
    assert scene.SPEAKER_LATENCY_S == 0.15  # each move: its time on the timeline, plus the speaker's latency
    assert body.log[:3] == [(0.45, 'move', 'sad1'), (3.55, 'turn', 35.0), (5.85, 'gesture', 'nod')]
    assert body.log[3][1:] == ('turn', 0.0)  # it ends facing the audience
    assert body.speeds[0] == config.SCENE_TURN_SPEED > 1.0  # a scene turns faster than a conversation
    assert body.cuts == [1.7, 5.4]  # MARCO, then the narrator again: the move before ends as the other speaks
    assert [(e['state'], e.get('line')) for e in events] == [
        ('started', None),
        ('line', 4),
        ('line', 6),
        ('line', 8),
        ('ended', None),
    ]
    for event in events:
        contracts.SceneEvent.model_validate(event)
    # Each segment once, at the speaker's rate: the Mac resamples, the robot only plays.
    assert sorted(player._mac.fetched) == ['/api/scenes/audio/k1?rate=16000', '/api/scenes/audio/k2?rate=16000']


def test_the_first_line_plays_before_the_other_voices_are_downloaded() -> None:
    """The first word waits for one download, not for all of them (a long scene is tens of MB over Wi-Fi)."""
    clock, mac = Clock(), SceneMac(TIMELINE)
    later, fetch, downloaded = threading.Event(), mac.get_bytes, []

    def get_bytes(path: str) -> bytes:
        if 'k2' in path:
            later.wait(2.0)  # the second voice comes only once the first line has started
            downloaded.append(path)
        return fetch(path)

    mac.get_bytes = get_bytes  # type: ignore[method-assign]
    player, _body, events = performer(clock, mac)
    stage, first = Stage(clock), []
    push = stage.stage_audio

    def staged(samples: np.ndarray, seconds: float) -> None:
        first.append(bool(downloaded))  # was the next voice already downloaded when the first line started?
        later.set()
        push(samples, seconds)

    stage.stage_audio = staged  # type: ignore[method-assign]
    assert asyncio.run(asyncio.wait_for(player.perform(TIMELINE, stage, 's1'), 10.0)).status == 'ok'
    assert first[0] is False
    assert [e.get('line') for e in events if e['state'] == 'line'] == [4, 6, 8]


def test_it_waits_for_audio_already_queued() -> None:
    clock = Clock()
    player, _, _ = performer(clock)
    stage = Stage(clock)
    stage.playback_end = clock.now + 2.0  # the model was still finishing a sentence
    asyncio.run(player.perform(TIMELINE, stage, 's1'))
    assert stage.pushes[0][0] == 2.0


def test_a_stop_cuts_the_scene_at_once() -> None:
    clock = Clock()
    player, body, events = performer(clock)
    stage = Stage(clock)
    clock.at = (1001.0, lambda: player.stop('dashboard'))
    result = asyncio.run(player.perform(TIMELINE, stage, 's1'))
    assert stage.flushes == 1 and body.stopped == 1
    assert {at for at, _ in stage.pushes} == {0.0}  # nothing after the stop
    assert all(kind != 'turn' for _, kind, _ in body.log)  # no turn happened: none to undo
    assert events[-1] == {
        'kind': 'scene',
        'session_id': 's1',
        'scene_id': 'courte',
        'title': 'Courte',
        'state': 'stopped',
        'message': 'Arrêtée depuis le tableau de bord.',
    }
    assert (result.status, result.message, result.silent) == (
        'ok',
        'Scène «\xa0Courte\xa0» arrêtée depuis le tableau de bord.',
        True,  # the model says nothing about it, nor plays it again
    )


def _replaced(clock: Clock, player: scene.Performer, timeline: dict[str, Any], at: float) -> Any:
    """`timeline` played until `at` (seconds on its clock), then replaced, as by a jump."""
    clock.at = (clock.now + at, lambda: player.stop('replaced'))
    return player.perform(timeline, Stage(clock), 's1')


@pytest.mark.parametrize('moves', [True, False])
def test_a_scene_that_replaces_a_turned_one_starts_and_ends_facing_the_audience(moves: bool) -> None:
    async def scenario() -> None:
        await _replaced(clock, player, TIMELINE, 4.0)  # past the turn to 35°
        assert body.turns == [35.0]  # replaced: the next scene turns the body where it starts
        await player.perform({**SHORT, 'moves': moves}, stage, 's1')  # it writes no turn: said facing the audience
        assert body.turns == [35.0, 0.0] and body.log[-1][0] <= stage.pushes[0][0]

    clock = Clock()
    player, body, _ = performer(clock)
    stage = Stage(clock)
    asyncio.run(scenario())


def test_a_scene_after_the_conversation_turned_the_body_plays_and_ends_facing_the_audience() -> None:
    """« Tourne-toi à gauche » (turn_body 90°), then « joue la scène », one with no turn of its own: from its first
    word to after its curtain, the body faces the audience. The real motion engine, not started: its targets."""
    body, clock = MotionRunner(SimpleNamespace()), Clock()
    body.turn_body(90.0)
    timeline = {**TIMELINE, 'cues': [cue for cue in TIMELINE['cues'] if cue['kind'] != 'turn']}
    stage, facing = Stage(clock), []
    push = stage.stage_audio

    def staged(samples: np.ndarray, seconds: float) -> None:
        facing.append(math.degrees(body.mixer.turn.end))
        push(samples, seconds)

    stage.stage_audio = staged  # type: ignore[method-assign]
    player = scene.Performer(SceneMac(timeline), FakeAudio(), body, Emotions(), clock, clock.sleep)
    assert asyncio.run(player.perform(timeline, stage, 's1')).status == 'ok'
    assert (set(facing), math.degrees(body.mixer.turn.end)) == ({0.0}, 0.0)


def test_a_scene_with_no_emotion_ends_facing_the_audience_too() -> None:
    clock = Clock()
    timeline = {**TIMELINE, 'cues': [cue for cue in TIMELINE['cues'] if cue['kind'] != 'emotion']}
    player, body, _ = performer(clock, SceneMac(timeline))
    assert asyncio.run(player.perform(timeline, Stage(clock), 's1')).status == 'ok'
    assert body.turns == [35.0, 0.0]


def test_a_still_play_never_turns_the_body_the_conversation_turned() -> None:
    """« Tourne-toi à gauche », then a play « Sans mouvements expressifs » (contracts.SceneOptions: no turn), replaced
    by a jump, then the jump played out: the body stays where the conversation left it, curtains included."""

    async def scenario() -> None:
        clock.at = (clock.now + 4.0, lambda: player.stop('replaced'))  # a jump
        await player.perform(still, stage, 's1')
        await player.perform(still, stage, 's1')

    body, clock = MotionRunner(SimpleNamespace()), Clock()
    body.turn_body(90.0)
    still, stage, facing = {**TIMELINE, 'moves': False}, Stage(clock), []
    push = stage.stage_audio

    def staged(samples: np.ndarray, seconds: float) -> None:
        facing.append(round(math.degrees(body.mixer.turn.end), 6))
        push(samples, seconds)

    stage.stage_audio = staged  # type: ignore[method-assign]
    player = scene.Performer(SceneMac(still), FakeAudio(), body, Emotions(), clock, clock.sleep)
    asyncio.run(scenario())
    assert facing and (set(facing), round(math.degrees(body.mixer.turn.end), 6)) == ({90.0}, 90.0)


@pytest.mark.parametrize('winding_down', [False, True])
def test_a_jump_stopped_before_it_starts_faces_the_audience_and_leaves_nothing_owed(winding_down: bool) -> None:
    """« Jouer » on another scene, then « Arrêter » before it starts (main voids it), once the turned scene it replaced
    ended or as it winds down: the body faces the audience then; later, after « tourne-toi à gauche », a play « Sans
    mouvements expressifs » turns nothing."""

    async def scenario() -> None:
        voided = lambda: (player.stop('replaced'), player.stop('dashboard'))  # noqa: E731 — the same poll
        clock.at = (clock.now + 4.0, voided if winding_down else lambda: player.stop('replaced'))  # past its 35°
        await player.perform(TIMELINE, Stage(clock), 's1')
        if not winding_down:
            player.stop('dashboard')
        assert round(math.degrees(body.mixer.turn.end), 6) == 0.0
        body.turn_body(90.0)
        await player.perform(still, stage, 's1')

    body, clock = MotionRunner(SimpleNamespace()), Clock()
    still, stage, facing = {**TIMELINE, 'moves': False}, Stage(clock), []
    push = stage.stage_audio

    def staged(samples: np.ndarray, seconds: float) -> None:
        facing.append(round(math.degrees(body.mixer.turn.end), 6))
        push(samples, seconds)

    stage.stage_audio = staged  # type: ignore[method-assign]
    player = scene.Performer(SceneMac(TIMELINE), FakeAudio(), body, Emotions(), clock, clock.sleep)
    asyncio.run(scenario())
    assert facing and (set(facing), round(math.degrees(body.mixer.turn.end), 6)) == ({90.0}, 90.0)


def _sea() -> contracts.SceneTimeline:
    parsed = scene_text.parse((ASSETS / 'voir-la-mer.txt').read_text(encoding='utf-8'), 'Kore')
    return scene_text.timeline('voir-la-mer', 'Mer', parsed, lambda key: scene_text.Voice(1.7))


@pytest.mark.parametrize(('line', 'turns'), [(6, [0.0]), (14, [])])
def test_a_jump_faces_where_the_text_puts_the_body_at_its_line(line: int, turns: list[float]) -> None:
    """« Le robot qui voulait voir la mer » turned to the seagull (line 12); a click on line 6 (the narrator, before
    any turn) faces the audience; one on line 14 (the seagull) stays turned: no turn at all, never back between."""

    async def scenario() -> None:
        await _replaced(clock, player, laid.model_dump(), seagull.at_ms / 1000 + 1.0)
        assert body.turns == [-35.0]
        body.log.clear()
        clock.at = (clock.now + 0.5, lambda: player.stop('dashboard'))  # once its first word is out
        await player.perform(scene_text.starting(laid, line).model_dump(), stage, 's1')

    clock, laid = Clock(), _sea()
    stage = Stage(clock)
    player, body, _ = performer(clock)
    seagull = next(c for c in laid.cues if c.kind == 'turn')
    asyncio.run(scenario())
    heard = stage.pushes[0][0] + scene.SPEAKER_LATENCY_S  # its first word out of the speaker
    assert [deg for at, kind, deg in body.log if kind == 'turn' and at <= heard] == turns


def test_a_cancelled_scene_stops_and_says_nothing() -> None:
    async def scenario() -> None:
        mac = SceneMac(TIMELINE)
        mac.events = events
        player = scene.Performer(mac, FakeAudio(), FakeBody(), Emotions())
        stage = scene.Standalone(FakeAudio())
        task = asyncio.create_task(player.perform(TIMELINE, stage, 's1'))
        await until(lambda: any(e['state'] == 'line' for e in events))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events[-1]['state'] == 'stopped' and not player.busy

    events: list[dict[str, Any]] = []
    asyncio.run(scenario())


def test_a_missing_voice_fails_in_french() -> None:
    clock = Clock()
    mac = SceneMac(TIMELINE)
    mac.fail = "Cette réplique n'est pas encore rendue."
    player, body, events = performer(clock, mac)
    result = asyncio.run(player.perform(TIMELINE, Stage(clock), 's1'))
    assert result.status == 'error' and "Cette réplique n'est pas encore rendue." in result.message
    assert events == [
        {'kind': 'scene', 'session_id': 's1', 'scene_id': 'courte', 'title': 'Courte', 'state': 'started'},
        {
            'kind': 'scene',
            'session_id': 's1',
            'scene_id': 'courte',
            'title': 'Courte',
            'state': 'failed',
            'message': "La scène n'a pas pu être jouée\xa0: Cette réplique n'est pas encore rendue.",
        },
    ]
    assert body.log == [] and not player.busy


def test_the_standalone_stage_keeps_its_own_clock() -> None:
    audio = FakeAudio()
    stage = scene.Standalone(audio)
    stage.stage_audio(np.ones(1600, dtype=np.float32), 0.1)
    assert stage.playback_end == pytest.approx(time.monotonic() + 0.1, abs=0.05)
    stage.flush()
    assert audio.cleared == 1 and stage.playback_end <= time.monotonic()


# ----- through a session -----

SHORT = {
    'scene_id': 'courte',
    'title': 'Courte',
    'duration_ms': 2300,
    'cues': [
        cue(0, 'audio', 4, key='k1', duration_ms=300, character='NARRATEUR', text='Un.'),
        cue(2000, 'audio', 6, key='k2', duration_ms=300, character='MARCO', text='Deux.'),  # after a 1.7 s silence
    ],
}


class SceneRig(Rig):
    def __init__(self) -> None:
        loud = np.full(256, 0.5, dtype=np.float32)
        holder: dict[str, scene.Performer] = {}
        super().__init__(
            audio=FakeAudio(lambda: loud), mac=SceneMac(SHORT, 0.3), performing=lambda: holder['performer'].busy
        )
        self.mac.events = self.events  # the session's events and the scene's, in one list
        self.performer = holder['performer'] = scene.Performer(self.mac, self.audio, self.body, Emotions())
        self.tools._extra.update(scene.tools(self.mac, self.performer, lambda: self.session))


def _rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    return float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0


def test_a_scene_asked_by_voice_plays_through_the_session() -> None:
    """Session start, the call, the scene's audio (the model muted, other tools refused, the microphone replaced
    even in a silence), then its one answer, silent (the scene was the answer), and the model speaking again."""

    async def scenario() -> None:
        rig = await SceneRig().start()
        live = rig.client.live
        live.push(**heard('Joue la scène courte.'))
        live.push(**vad('ACTIVITY_END'))
        live.push(**call('play_scene', 'c1', title='courte'))
        await until(lambda: rig.performer.busy)
        assert rig.session.state in (S.THINKING, S.SPEAKING)
        live.push(**speech(2400, said='Je lance la scène'))  # the model talking over the scene: dropped
        live.push(**heard('Mets de la musique.'))
        live.push(**call('play_music', 'c2', query='jazz'))
        await until(lambda: any(r.id == 'c2' for r in live.responses))
        refused = next(r for r in live.responses if r.id == 'c2')
        assert refused.response['status'] == 'refused' and refused.scheduling == SILENT
        assert ('POST', '/api/spotify/play', {'query': 'jazz', 'kind': 'track'}) not in rig.mac.calls
        started = time.monotonic()
        await until(lambda: rig.session.state is S.SPEAKING)
        await until(lambda: any(r.id == 'c1' for r in live.responses), timeout=6.0)
        silence = [pcm for at, pcm in live.audio if started + 1.4 <= at <= started + 1.9]  # past the voice's tail
        assert silence and max(map(_rms, silence)) < 0.02  # the comfort noise, not the loud room
        answer = [r for r in live.responses if r.id == 'c1']
        assert len(answer) == 1 and answer[0].scheduling == SILENT
        assert answer[0].response['status'] == 'ok' and "jouée jusqu'au bout" in answer[0].response['message']
        said = [e['text'] for e in rig.events if e['kind'] in ('robot_delta', 'robot_end')]
        assert all('Je lance' not in text for text in said)
        pushed = sum(s.size for s in rig.audio.pushed)
        assert pushed == pytest.approx(2 * 0.3 * 16000, abs=20)  # the scene's audio alone
        assert rig.session.state is S.THINKING or rig.session.state is S.SPEAKING
        live.push(**speech(2400, said='Voilà !'))  # the model speaks again
        await until(lambda: sum(s.size for s in rig.audio.pushed) > pushed)
        live.push(**content(turn_complete=True))
        await until(lambda: rig.session.state is S.IDLE)
        phases = rig.phases()
        assert phases[:2] == ['connecting', 'idle'] and 'speaking' in phases and phases[-1] == 'idle'
        states = [e['state'] for e in rig.events if e['kind'] == 'scene']
        assert states == ['started', 'line', 'line', 'ended']
        await rig.stop()

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_a_scene_ends_unanswered_when_the_robot_goes_to_sleep() -> None:
    async def scenario() -> None:
        rig = await SceneRig().start()
        live = rig.client.live
        live.push(**heard('Joue la scène.'))
        live.push(**call('play_scene', 'c1'))
        await until(lambda: any(e.get('state') == 'line' for e in rig.events))
        await rig.stop('dashboard')
        assert [r for r in live.responses if r.id == 'c1'] == []
        scenes = [(e['state'], e.get('message')) for e in rig.events if e['kind'] == 'scene']
        assert scenes[-1] == ('stopped', 'Arrêtée par la mise en veille.')
        assert not rig.performer.busy

    asyncio.run(asyncio.wait_for(scenario(), 10.0))


def test_a_scene_call_the_server_withdraws_stops_saying_so_and_the_conversation_goes_on() -> None:
    """The human talks on as the scene starts, and the server withdraws the play_scene call (a cancelled
    call gets no answer): the scene stops, unanswered, and the dashboard is not told the robot sleeps."""

    async def scenario() -> None:
        rig = await SceneRig().start()
        live = rig.client.live
        live.push(**heard('Joue la scène courte.'))
        live.push(**vad('ACTIVITY_END'))
        live.push(**call('play_scene', 'c1', title='courte'))
        await until(lambda: rig.performer.busy)
        live.push(tool_call_cancellation=types.LiveServerToolCallCancellation(ids=['c1']))
        await until(lambda: not rig.performer.busy)
        stopped = [e['message'] for e in rig.events if e['kind'] == 'scene' and e['state'] == 'stopped']
        assert stopped == ['Arrêtée par la conversation, qui a annulé la demande.']
        assert [r for r in live.responses if r.id == 'c1'] == [] and rig.session.state is not S.CLOSING
        await rig.stop()

    asyncio.run(asyncio.wait_for(scenario(), 10.0))


class _SlowVoiceMac(SceneMac):
    def get_bytes(self, path: str) -> bytes:
        time.sleep(0.5)  # off the loop: the first voice still on its way
        return super().get_bytes(path)


@pytest.mark.parametrize('lag_s', [0.0, 0.12])  # 0.12: a loaded machine, the test's own steps as late
def test_a_dashboard_scene_stopped_before_its_first_line_leaves_the_idle_delay_after_it(lag_s: float) -> None:
    """The owner edits a script in silence, plays it from the dashboard (no tool call), and « Arrêter » before its
    first line: the idle sleep counts from the curtain, not from the last word heard before the scene. Both delays
    are read on the session's own clocks: a late step of the test changes neither."""

    async def scenario() -> None:
        seen: list[float] = []  # each time the session saw the scene play: the last one is its curtain

        def performing() -> bool:
            if busy := player.busy:
                seen.append(time.monotonic())
            return busy

        rig = Rig(settings_view(idle_sleep_minutes=idle_s / 60), performing=performing)
        player = scene.Performer(_SlowVoiceMac(SHORT), FakeAudio(), rig.body, Emotions())
        await rig.start()
        time.sleep(lag_s)  # a slow start
        await asyncio.sleep(rig.session._last_activity_at + idle_s * 0.75 - time.monotonic())  # editing, quiet
        play = asyncio.ensure_future(player.perform(SHORT, rig.session, rig.session.session_id))
        play.add_done_callback(lambda _: time.sleep(lag_s))  # the test resumes late after the curtain
        await asyncio.sleep(idle_s * 0.5)
        assert not rig.task.done()  # past the idle delay, held off by the scene
        player.stop('dashboard')
        await asyncio.wait_for(play, 2.0)
        assert await asyncio.wait_for(rig.task, 2.0) == 'idle'
        assert time.monotonic() - seen[-1] >= idle_s

    idle_s = 0.4
    asyncio.run(asyncio.wait_for(scenario(), 10.0))


# ----- the robot's commands -----


def test_the_dashboard_plays_a_scene_on_a_sleeping_robot(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._mac.get_bytes = SceneMac(SHORT, 0.3).get_bytes
        armed: list[str] = []
        robot._antennas.arm, robot._antennas.disarm = lambda: armed.append('arm'), lambda: armed.append('disarm')
        robot._command({'action': 'scene', 'scene': SHORT})
        await until(lambda: robot._performer.busy)
        assert robot._motion.mixer.mode != 'sleep'
        await until(lambda: not robot._performer.busy and armed[-1:] == ['arm'], timeout=6.0)
        assert armed == ['disarm', 'arm'] and robot._motion.mixer.mode == 'sleep'
        events = [e for e in robot_parts.events if e['kind'] == 'scene']
        assert [e['state'] for e in events] == ['started', 'line', 'line', 'ended']
        assert events[0]['session_id'].startswith('scene-')
        robot._command({'action': 'scene', 'scene': SHORT})
        await until(lambda: robot._performer.busy)
        robot._command({'action': 'scene_stop'})
        await until(lambda: [e['state'] for e in robot_parts.events if e['kind'] == 'scene'][-1] == 'stopped')

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_a_scene_asleep_shows_the_robot_speaking_only_while_a_voice_plays(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """As the session's states show it awake: talking while a line plays, idle through the silence between two lines
    ([pause] or not), then back to sleep."""

    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._mac.get_bytes = SceneMac(SHORT, 0.3).get_bytes
        set_mode = robot._motion.set_mode
        robot._motion.set_mode = lambda mode, **kw: (modes.append(mode), set_mode(mode, **kw))[1]
        robot._command({'action': 'scene', 'scene': SHORT})
        await until(lambda: not robot._background, timeout=8.0)
        shown = [mode for n, mode in enumerate(modes) if not n or modes[n - 1] != mode]
        assert shown[:4] == ['idle', 'talking', 'idle', 'talking'] and shown[-1] == 'sleep'

    modes: list[str] = []
    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_the_standalone_stage_never_takes_the_robot_out_of_a_sleep_pose_asked_meanwhile() -> None:
    """A Ctrl-C during a scene asleep: the sleep move may come before the scene's end stops the stage showing it."""

    async def scenario() -> None:
        stage.stage_audio(np.ones(1600, dtype=np.float32), 1.0)  # a line playing
        showing = asyncio.ensure_future(stage.show(body))
        await asyncio.sleep(3 * config.WATCHDOG_TICK_S)
        showing.cancel()

    body, stage = MotionRunner(SimpleNamespace()), scene.Standalone(FakeAudio())  # asleep
    asyncio.run(scenario())
    assert body.mixer.mode == 'sleep'


def test_the_heartbeat_says_whether_a_wiggle_wakes_the_sleeping_robot(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """At rest, a wiggled antenna wakes it; while a scene plays asleep, it does not (held, it stops the scene). The
    dashboard's header reads which from the heartbeat, and the scene on stage, as its events said it, from each beat:
    an event lost on the way is put right by the next."""

    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._mac.get_bytes = SceneMac(SHORT, 0.3).get_bytes
        robot._antennas.arm()
        assert robot._beat() == {'awake': False, 'session_id': None, 'antenna_wake': True, 'scene': None}
        robot._command({'action': 'scene', 'scene': SHORT})
        await until(lambda: robot._performer.busy)
        assert robot._beat()['antenna_wake'] is False
        await until(lambda: any(e.get('state') == 'started' for e in robot_parts.events))
        assert robot._beat()['scene'] == 'courte'
        robot._command({'action': 'scene_stop'})
        await until(lambda: not robot._background, timeout=8.0)
        assert robot._beat()['antenna_wake'] is True  # at rest again, listening
        assert robot._beat()['scene'] is None and robot_parts.events[-1]['state'] == 'stopped'

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def _starts(events: list[dict[str, Any]]) -> list[str]:
    return [e['scene_id'] for e in events if e['kind'] == 'scene' and e['state'] == 'started']


def _awake_and_lent(robot: Robot) -> asyncio.Event:
    """In conversation, the head lent to an app; the event lets it go (the head still, taken back)."""
    stage = scene.Standalone(robot._audio)
    stage.session_id = 'awake'  # type: ignore[attr-defined]
    robot._session, robot._session_id, robot._manette.profile = stage, 'awake', 'tete'  # type: ignore[assignment]
    back = asyncio.Event()

    async def release(why: str) -> None:
        await back.wait()
        robot._manette.profile = None

    robot._manette._release = release  # type: ignore[method-assign]
    return back


def test_a_scene_asked_by_voice_is_refused_once_an_app_took_the_body_while_it_was_found(robot_parts) -> None:  # noqa: F811
    """« Joue la scène courte » as an app asks for the robot as its controller: « Autoriser » lands while the Mac finds
    the scene. The scene never plays over the hold: refused, as asked while the body is lent."""

    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        mini = robot_parts.mini
        mini.get_current_head_pose = lambda: mini.heads[-1] if mini.heads else SLEEP_HEAD  # it goes where it is sent
        found = SceneMac(SHORT, 0.3)
        found.delay = 0.3  # the Mac's /api/scenes/find, in a thread
        robot._mac.request, robot._mac.get_bytes = found.request, found.get_bytes  # type: ignore[method-assign]
        robot._motion.start(None)
        robot._motion.set_mode('idle', blend_s=0.01)
        stage = scene.Standalone(robot._audio)
        stage.session_id = 's1'  # type: ignore[attr-defined]
        robot._session, robot._session_id = stage, 's1'  # type: ignore[assignment]
        asked = asyncio.create_task(robot._tools.run('play_scene', {'title': 'courte'}, None))
        await asyncio.sleep(0.05)  # past the tool's check: nothing is lent yet
        robot._command({'action': 'manette', 'manette': 'gachettes', 'grant': True})  # « Autoriser »
        result = await asyncio.wait_for(asked, 5.0)
        assert (result.status, _starts(robot_parts.events), robot._manette.on) == ('error', [], True)
        assert 'manette' in result.message
        await robot._manette.leave('app')
        robot._motion.stop()

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


@pytest.mark.parametrize('then', ['scene_stop', 'scene'])
def test_a_play_waiting_for_the_body_is_stopped_or_replaced(robot_parts: SimpleNamespace, then: str) -> None:  # noqa: F811
    """« Jouer » while an app gives the head back (up to manette.RETURN_WAIT_S): « Arrêter » meanwhile (the button
    shows from the moment the play is queued) plays nothing and says so, for the dashboard to show « Jouer » again;
    another « Jouer » plays only the latest, which answers for both."""

    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._mac.get_bytes = SceneMac(SHORT, 0.3).get_bytes
        back = _awake_and_lent(robot)
        robot._command({'action': 'scene', 'scene': SHORT})
        await asyncio.sleep(0.1)
        robot._command({'action': then, 'scene': {**SHORT, 'scene_id': 'autre'}})
        await asyncio.sleep(0.1)
        back.set()
        await until(lambda: not robot._background, timeout=8.0)
        assert _starts(robot_parts.events) == ([] if then == 'scene_stop' else ['autre'])
        dropped = [(e['state'], e['message']) for e in robot_parts.events if e.get('scene_id') == 'courte']
        assert dropped == ([('stopped', 'Arrêtée depuis le tableau de bord.')] if then == 'scene_stop' else [])

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


@pytest.mark.parametrize(('then', 'started'), [('scene', ['courte', 'saut-2']), ('scene_stop', ['courte'])])
def test_of_two_commands_at_once_the_latest_wins(robot_parts: SimpleNamespace, then: str, started: list[str]) -> None:  # noqa: F811
    """A line number clicked, then another one or « Arrêter », as the same poll brings them."""

    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._mac.get_bytes = SceneMac(SHORT, 0.3).get_bytes
        robot._command({'action': 'scene', 'scene': SHORT})
        await until(lambda: any(e.get('state') == 'line' for e in robot_parts.events))
        robot._command({'action': 'scene', 'scene': {**SHORT, 'scene_id': 'saut-1'}})
        robot._command({'action': then, 'scene': {**SHORT, 'scene_id': 'saut-2'}})
        await until(lambda: not robot._background, timeout=8.0)
        assert _starts(robot_parts.events) == started

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_a_jump_asleep_goes_on_from_where_the_scene_was(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """Asleep, a line number clicked: the new scene takes over at once, never heading back to the sleep pose or the
    front between the two; the last one to end puts the robot back to sleep, listening."""

    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._mac.get_bytes = SceneMac(SHORT, 0.3).get_bytes
        set_mode, turn = robot._motion.set_mode, robot._motion.turn_body
        robot._motion.set_mode = lambda mode, **kw: (calls.append(f'mode {mode}'), set_mode(mode, **kw))[1]
        robot._motion.turn_body = lambda deg, *a, **kw: (calls.append(f'turn {deg:g}'), turn(deg, *a, **kw))[1]
        turned = {**SHORT, 'cues': [cue(0, 'turn', 4, degrees=60.0), *SHORT['cues']]}
        robot._command({'action': 'scene', 'scene': turned})
        await until(lambda: 'turn 60' in calls)
        calls.clear()
        robot._command({'action': 'scene', 'scene': {**turned, 'scene_id': 'saut'}})  # turned where the scene was
        await until(lambda: 'saut' in _starts(robot_parts.events))
        assert 'mode sleep' not in calls and 'turn 0' not in calls
        await until(lambda: not robot._background, timeout=8.0)
        assert calls[-2:] == ['mode sleep', 'turn 0'] and robot._antennas._armed

    calls: list[str] = []
    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_a_jump_asleep_stopped_before_it_starts_leaves_the_robot_asleep(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._mac.get_bytes = SceneMac(SHORT, 0.3).get_bytes
        robot._command({'action': 'scene', 'scene': SHORT})
        await until(lambda: any(e.get('state') == 'line' for e in robot_parts.events))
        robot._command({'action': 'scene', 'scene': {**SHORT, 'scene_id': 'saut'}})
        robot._command({'action': 'scene_stop'})
        await until(lambda: not robot._background, timeout=8.0)
        assert _starts(robot_parts.events) == ['courte']
        assert robot._motion.mixer.mode == 'sleep' and robot._antennas._armed
        dropped = [(e['state'], e['message']) for e in robot_parts.events if e.get('scene_id') == 'saut']
        assert dropped == [('stopped', 'Arrêtée depuis le tableau de bord.')]
        for event in robot_parts.events:  # the Mac takes every one, the robot asleep too
            TypeAdapter(contracts.RobotEvent).validate_python(event)

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_in_conversation_a_jump_stopped_before_it_starts_faces_the_audience(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """Awake, a turned scene, a line number clicked and « Arrêter » in the same poll: the body faces the audience."""

    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._mac.get_bytes = SceneMac(SHORT, 0.3).get_bytes
        stage = scene.Standalone(robot._audio)
        stage.session_id = 'awake'  # type: ignore[attr-defined]
        robot._session, robot._session_id = stage, 'awake'  # type: ignore[assignment]
        robot._command(
            {'action': 'scene', 'scene': {**SHORT, 'cues': [cue(0, 'turn', 4, degrees=60.0), *SHORT['cues']]}}
        )
        await until(lambda: round(math.degrees(robot._motion.mixer.turn.end)) == 60)
        robot._command({'action': 'scene', 'scene': {**SHORT, 'scene_id': 'saut'}})
        robot._command({'action': 'scene_stop'})
        await until(lambda: not robot._background, timeout=8.0)
        assert _starts(robot_parts.events) == ['courte'] and math.degrees(robot._motion.mixer.turn.end) == 0.0
        assert not robot._performer._owed

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


MOVING = {  # gestures that swing the antennas well past ANTENNA_WAKE_RAD
    'scene_id': 'mouvante',
    'title': 'Mouvante',
    'duration_ms': 3000,
    'cues': [
        cue(i * 1000, 'audio', 4 + i, key=f'k{i}', duration_ms=1000, character='NARRATEUR', text='x') for i in range(3)
    ]
    + [cue(500, 'gesture', 5, name='nod'), cue(1500, 'gesture', 6, name='wiggle')],
}


def _running_robot(parts: SimpleNamespace) -> tuple[Any, Any, FakeMacServer]:
    """A whole Robot.run() on fakes whose antenna motors follow their command; the scene voices last 1 s."""
    parts.mini.follow = True
    mac = FakeMacServer(settings_view())
    robot = Robot(parts.mini, parts.client, camera=None)
    robot._mac._send = mac.send
    robot._mac.get_bytes = SceneMac(MOVING, 1.0).get_bytes
    return robot, asyncio.create_task(robot.run()), mac


def test_the_dashboard_plays_a_scene_in_conversation_on_the_sessions_stage(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """« Jouer » in a conversation: its events are the conversation's (the Mac's tally starts over on another id) and
    its voices move the session's clock (the robot shows it speaking, a sleep flushes them)."""

    async def scenario() -> None:
        robot, task, mac = _running_robot(robot_parts)
        await until(lambda: robot._antennas._armed)
        robot._trigger('dashboard')
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        session = robot._session
        mac.commands.put({'id': 'c1', 'action': 'scene', 'scene': MOVING, 'queued_at': time.time()})
        await until(lambda: any(e.get('state') == 'line' for e in mac.events), timeout=5.0)
        assert {e['session_id'] for e in mac.events if e['kind'] == 'scene'} == {session.session_id}
        assert session.playback_end > time.monotonic() and session.state is S.SPEAKING
        robot._shutdown()
        await asyncio.wait_for(task, 8.0)

    asyncio.run(asyncio.wait_for(scenario(), 20.0))


def test_a_scene_played_as_the_robot_falls_asleep_never_wakes_it(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """« Jouer sur le robot » while the robot still falls asleep (after « Veille », or at boot): the end of the sleep
    move must not let the antennas listen while the scene moves them."""

    async def scenario() -> None:
        robot, task, mac = _running_robot(robot_parts)
        await asyncio.sleep(0)  # the sleep move under way
        robot._command({'action': 'scene', 'scene': MOVING})
        await until(lambda: robot._performer.busy)
        await until(lambda: robot._session_id is not None or not robot._performer.busy, timeout=8.0)
        await until(lambda: robot._session_id is not None or robot._antennas._armed, timeout=8.0)
        assert robot._session_id is None and 'session_start' not in [e['kind'] for e in mac.events]
        assert [e['state'] for e in mac.events if e['kind'] == 'scene'][-1] == 'ended'
        robot._shutdown()
        await asyncio.wait_for(task, 5.0)

    asyncio.run(asyncio.wait_for(scenario(), 20.0))


def test_an_antenna_held_to_stop_a_scene_asleep_wakes_nothing_once_let_go(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """Held on past the fold back into the sleep pose, the antenna is still in the hand when the wake re-arms."""

    async def scenario() -> None:
        robot, task, mac = _running_robot(robot_parts)
        await until(lambda: robot._antennas._armed and robot._antennas._baseline is not None, timeout=8.0)
        robot._command({'action': 'scene', 'scene': MOVING})
        await until(lambda: any(e.get('state') == 'line' for e in mac.events), timeout=8.0)
        mini.follow, mini.antennas = False, [1.5, -1.5]  # a hand holds both antennas off their command
        await until(lambda: any(e.get('state') == 'stopped' for e in mac.events if e['kind'] == 'scene'))
        await until(lambda: robot._antennas._armed and robot._antennas._baseline is not None, timeout=8.0)
        mini.follow, mini.antennas = True, list(SLEEP_ANTENNAS)  # let go: the motors bring them back
        await asyncio.sleep(0.5)
        assert robot._session_id is None and not robot._wake.is_set() and robot._antennas._armed
        robot._shutdown()
        await asyncio.wait_for(task, 8.0)

    mini = robot_parts.mini
    asyncio.run(asyncio.wait_for(scenario(), 30.0))


def test_ctrl_c_during_a_scene_asleep_ends_in_the_sleep_pose(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    async def scenario() -> None:
        robot, task, mac = _running_robot(robot_parts)
        await until(lambda: robot._antennas._armed)
        robot._command({'action': 'scene', 'scene': MOVING})
        await until(lambda: any(e.get('state') == 'line' for e in mac.events))
        await asyncio.sleep(1.2)  # in the talking pose
        robot._shutdown()  # Ctrl-C, or `make run`'s SSH session dropped
        await asyncio.wait_for(task, 8.0)
        assert robot._motion.mixer.mode == 'sleep'
        assert np.allclose(robot_parts.mini.heads[-1], SLEEP_HEAD) and np.allclose(
            robot_parts.mini.antennas, SLEEP_ANTENNAS
        )

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def _scene_ends(mac: FakeMacServer) -> list[tuple[str, str | None]]:
    return [(e['state'], e.get('message')) for e in mac.events if e['kind'] == 'scene' and e['state'] != 'line']


def test_a_wake_stops_a_scene_played_asleep_at_once(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """« Réveiller » while the dashboard's scene plays on the sleeping robot: the scene stops at once and says why, or
    it would mute the new conversation and refuse its tools until its last line."""

    async def scenario() -> None:
        robot, task, mac = _running_robot(robot_parts)
        await until(lambda: robot._antennas._armed)
        robot._command({'action': 'scene', 'scene': MOVING})
        await until(lambda: any(e.get('state') == 'line' for e in mac.events))
        robot._command({'action': 'wake'})
        await until(lambda: not robot._performer.busy, timeout=0.5)  # the scene has 3 s to go
        assert _scene_ends(mac) == [('started', None), ('stopped', 'Arrêtée par le réveil du robot.')]
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        robot._shutdown()
        await asyncio.wait_for(task, 8.0)

    asyncio.run(asyncio.wait_for(scenario(), 20.0))


def test_a_conversations_end_stops_the_scene_the_dashboard_started_in_it(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """« Jouer » in a conversation, then « Endormir » mid-scene: the scene stops with the conversation instead of
    playing on over the sleep pose with the wake antennas deaf."""

    async def scenario() -> None:
        robot, task, mac = _running_robot(robot_parts)
        await until(lambda: robot._antennas._armed)
        robot._command({'action': 'wake'})
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        robot._command({'action': 'scene', 'scene': MOVING})
        await until(lambda: any(e.get('state') == 'line' for e in mac.events))
        robot._command({'action': 'sleep'})
        await until(lambda: robot._session_id is None)
        await until(lambda: not robot._performer.busy, timeout=0.5)
        assert _scene_ends(mac) == [('started', None), ('stopped', 'Arrêtée par la mise en veille.')]
        robot._shutdown()
        await asyncio.wait_for(task, 8.0)

    asyncio.run(asyncio.wait_for(scenario(), 20.0))


def test_the_conversation_the_robot_opens_knows_a_dashboard_scene_plays(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    """« Jouer » in a conversation: the session Robot opened refuses the model's tools until the curtain, a human's
    words just heard included (the same session, built by hand, is tested in SceneRig's tests)."""

    async def scenario() -> None:
        robot, task, mac = _running_robot(robot_parts)
        await until(lambda: robot._antennas._armed)
        robot._command({'action': 'wake'})
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        robot._command({'action': 'scene', 'scene': MOVING})
        await until(lambda: any(e.get('state') == 'line' for e in mac.events))
        live = robot_parts.client.live
        live.push(**heard('Tourne-toi.'))
        live.push(**call('turn_body', 'c1', angle=40))
        await until(lambda: any(r.id == 'c1' for r in live.responses))
        said = f"{_PERFORMING} N'en parle pas."
        assert [r.response for r in live.responses if r.id == 'c1'] == [{'status': 'refused', 'message': said}]
        assert robot._performer.busy  # still playing: the refusal came from the scene
        robot._shutdown()
        await asyncio.wait_for(task, 8.0)

    asyncio.run(asyncio.wait_for(scenario(), 20.0))


def test_a_scene_that_breaks_still_ends_asleep_and_listening(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))

        async def perform(*_args: Any) -> None:
            raise RuntimeError('a bug')

        robot._performer.perform = perform
        with pytest.raises(RuntimeError, match='a bug'):
            robot._scenes += 1  # as `_command` counts it
            await robot._scene(SHORT, robot._performer.stops)
        assert robot._motion.mixer.mode == 'sleep' and robot._antennas._armed

    asyncio.run(asyncio.wait_for(scenario(), 10.0))


def test_a_typed_say_during_the_voice_download_stops_the_scene_at_once() -> None:
    """The stop does not wait for the download: `busy` clears, and the reply to the typed turn is heard."""

    async def scenario() -> None:
        rig = await SceneRig().start()
        downloaded = threading.Event()
        fetch = rig.mac.get_bytes
        rig.mac.get_bytes = lambda path: (downloaded.wait(5.0), fetch(path))[1]  # a slow Wi-Fi download
        live = rig.client.live
        live.push(**heard('Joue la scène.'))
        live.push(**call('play_scene', 'c1', title='courte'))
        await until(lambda: any(e.get('state') == 'started' for e in rig.events))  # its moves loaded: it started
        rig.performer.stop('say')  # main._command('say'): the scene stops, then the typed turn
        rig.session.say('Arrête, dis-moi plutôt bonjour.')
        await until(lambda: not rig.performer.busy, timeout=1.0)
        live.push(**speech(4800, said='Bonjour !'))
        await until(lambda: rig.audio.pushed)
        assert [(e['state'], e.get('message')) for e in rig.events if e['kind'] == 'scene'] == [
            ('started', None),  # at once: the first voice is still on its way when the stop comes
            ('stopped', 'Arrêtée par un message écrit.'),
        ]
        downloaded.set()
        await rig.stop()

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_a_typed_turn_stops_the_scene_first(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._performer = SimpleNamespace(stop=calls.append, busy=True)
        robot._session = SimpleNamespace(say=lambda text: calls.append(f'say:{text}'))
        robot._command({'action': 'say', 'text': 'Stop !'})

    calls: list[str] = []
    asyncio.run(scenario())
    assert calls == ['say', 'say:Stop !']


def test_an_antenna_held_by_hand_completes_a_hold_and_a_lagging_motor_does_not() -> None:
    watch = AntennaWiggle(SimpleNamespace(), lambda: None)
    commanded, lagging, hand = (-0.17, 0.17), (-0.6, 0.17), (-1.0, 0.17)  # off by 0.43 rad, then 0.83 rad
    assert not any(watch.held(lagging, commanded) for _ in range(3 * HOLD_SAMPLES))
    assert [watch.held(hand, commanded) for _ in range(HOLD_SAMPLES)] == [False] * (HOLD_SAMPLES - 1) + [True]
    watch.held(hand, commanded)
    watch.held(commanded, commanded)  # let go: it starts over
    assert not any(watch.held(hand, commanded) for _ in range(HOLD_SAMPLES - 1))


def test_the_antenna_thread_stops_a_playing_scene(robot_parts: SimpleNamespace) -> None:  # noqa: F811
    async def scenario() -> None:
        robot = _robot(robot_parts, RuntimeError('unused'))
        robot._performer = SimpleNamespace(stop=stops.append, busy=True)
        robot_parts.mini.antennas = [-1.2, 0.17]  # held back by hand, far from what the motion thread commands
        robot._antennas.start()
        await until(lambda: stops == ['antenna'])
        robot._antennas.stop()

    stops: list[str] = []
    asyncio.run(asyncio.wait_for(scenario(), 5.0))
