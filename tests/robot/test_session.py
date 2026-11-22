"""The conversation against a scripted Gemini Live: turns, tools, reconnection, audio and idle sleep."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import TypeAdapter
from websockets import exceptions as ws
from websockets.datastructures import Headers
from websockets.frames import Close
from websockets.http11 import Response

from mac_server import contracts
from robot_agent import apps, config, notes
from robot_agent import audio as audio_module
from robot_agent import session as session_module
from robot_agent.audio import ComfortNoise
from robot_agent.state import SessionState
from robot_agent.tools import SILENT, WHEN_IDLE
from scripts import contract_examples
from tests.robot.conftest import (
    FakeAudio,
    FakeLive,
    FakeMac,
    Rig,
    call,
    content,
    heard,
    resumable,
    settings_view,
    silent_peer,
    speech,
    until,
    vad,
)

S = SessionState
ROOT = Path(__file__).resolve().parents[2]


def run(scenario) -> None:
    asyncio.run(asyncio.wait_for(scenario(), 10.0))


def _loud_speech(n_samples: int) -> dict[str, Any]:
    """Model audio that is not silence: a 764 Hz tone at 24 kHz, 0.8 full scale."""
    pcm = (0.8 * np.sin(np.arange(n_samples) * 0.2) * 32767).astype(np.int16).tobytes()
    part = types.Part(inline_data=types.Blob(data=pcm, mime_type='audio/pcm;rate=24000'))
    return content(model_turn=types.Content(role='model', parts=[part]))


def test_a_spoken_turn_moves_the_state_and_the_body() -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**vad('ACTIVITY_START'))
        await until(lambda: rig.session.state is S.LISTENING)
        live.push(**heard('Bonjour'))
        live.push(**vad('ACTIVITY_END'))
        await until(lambda: rig.session.state is S.THINKING)
        live.push(**speech(2400))  # 0.1 s
        await until(lambda: rig.session.state is S.SPEAKING)
        live.push(**content(turn_complete=True))
        await until(lambda: rig.session.state is S.IDLE)
        assert rig.phases() == ['connecting', 'idle', 'listening', 'thinking', 'speaking', 'idle']
        assert rig.body.modes == ['idle', 'listening', 'listening', 'talking', 'idle']  # thinking looks attentive
        live.push(**vad('ACTIVITY_START'))  # receive() ended with the interaction: the session calls it again
        await until(lambda: rig.session.state is S.LISTENING)
        assert live.receives == 2 and len(rig.client.connections) == 1
        assert await rig.stop() == 'dashboard'

    run(scenario)


@pytest.mark.parametrize(
    'frames',
    [
        [content(interrupted=True)],
        [{**vad('ACTIVITY_START'), **content(interrupted=True)}],
        [vad('ACTIVITY_START'), content(interrupted=True)],
    ],
    ids=['interrupted', 'with-the-vad-start', 'after-the-vad-start'],
)
def test_barge_in_flushes_the_speaker_and_listens(frames: list[dict[str, Any]]) -> None:
    """Gemini may report the user's speech before `interrupted`: the robot is then listening already, and the
    speaker must be flushed all the same."""

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**_loud_speech(24000))  # 1 s of the robot's voice queued
        await until(lambda: rig.session.state is S.SPEAKING)
        for frame in frames:
            live.push(**frame)
        await until(lambda: rig.audio.cleared == 1)
        assert rig.session.state is S.LISTENING
        assert rig.body.modes[-1] == 'listening'
        assert rig.session._playback_end <= time.monotonic()  # the playback clock forgets the dropped audio
        await until(lambda: rig.audio.sound_stops == 1)  # an emotion's sound stops with the voice
        dropped = len(rig.audio.pushed)
        live.push(**speech(2400))  # the next answer opens on silence...
        await until(lambda: len(rig.audio.pushed) > dropped)
        assert np.max(np.abs(rig.audio.pushed[dropped])) < 1e-6  # ...with nothing of the dropped voice in it
        await rig.stop()

    run(scenario)


@pytest.mark.parametrize(
    'frames',
    [
        [vad('ACTIVITY_START'), vad('ACTIVITY_END'), content(interrupted=True)],
        [{**vad('ACTIVITY_END'), **content(interrupted=True)}],
        ['say', content(interrupted=True)],
    ],
    ids=['start-end-interrupted', 'end-and-interrupted', 'say-then-interrupted'],
)
def test_an_interruption_after_the_users_turn_flushes_and_waits_for_the_reply(monkeypatch, frames: list) -> None:
    """The user's turn is over (VAD end, a typed say) when `interrupted` comes: the robot must not listen to
    nobody, and the reply it is owed (or proactive audio declining it) brings it back to rest."""
    monkeypatch.setattr(config, 'REPLY_TIMEOUT_S', 0.2)

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**_loud_speech(24000))  # 1 s of the robot's voice queued
        await until(lambda: rig.session.state is S.SPEAKING)
        for frame in frames:
            if frame == 'say':
                rig.session.say('Stop, parle-moi de la météo.')
            else:
                live.push(**frame)
        await until(lambda: rig.audio.cleared == 1)
        assert rig.session.state is S.THINKING
        await until(lambda: rig.session.state is S.IDLE)  # no reply came: not stuck listening
        await rig.stop()

    run(scenario)


def test_interaction_status_closes_the_interaction_when_it_is_sent() -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**heard('Quelle heure est-il ?'), **vad('ACTIVITY_END'))
        await until(lambda: rig.session.state is S.THINKING)
        live.push(**content(turn_complete=True, interaction_status=types.InteractionStatus.IN_PROGRESS))
        await asyncio.sleep(0.05)
        assert rig.session.state is S.THINKING  # the turn ended, the interaction did not
        live.push(**content(turn_complete=True, interaction_status=types.InteractionStatus.IDLE))
        await until(lambda: rig.session.state is S.IDLE)
        await rig.stop()

    run(scenario)


def test_no_reply_returns_to_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'REPLY_TIMEOUT_S', 0.1)

    async def scenario() -> None:
        rig = await Rig().start()
        rig.client.live.push(**vad('ACTIVITY_START'))
        rig.client.live.push(**vad('ACTIVITY_END'))
        await until(lambda: rig.session.state is S.THINKING)
        await until(lambda: rig.session.state is S.IDLE)
        await rig.stop()

    run(scenario)


@pytest.mark.parametrize('go_away', [False, True], ids=['alone', 'a go_away pending'])
def test_a_say_typed_while_thinking_gets_its_own_reply_time(monkeypatch: pytest.MonkeyPatch, go_away: bool) -> None:
    """The model has not answered a spoken turn yet; most of its reply time gone, the owner types a question. The
    typed turn gets a reply time of its own: the robot thinks on, and a go_away waiting for a quiet point does not
    reconnect under the answer still owed."""
    monkeypatch.setattr(config, 'REPLY_TIMEOUT_S', 0.5)

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        first = rig.client.live
        first.push(**heard('Hmm…'))
        first.push(**vad('ACTIVITY_END'))
        if go_away:
            first.push(go_away=types.LiveServerGoAway(time_left='60s'))
        await until(lambda: rig.session.state is S.THINKING)
        await asyncio.sleep(0.4)
        rig.session.say('Quelle heure est-il ?')
        await until(lambda: first.turns)
        await asyncio.sleep(0.3)  # past the spoken turn's reply time, within the typed turn's
        assert len(rig.client.connections) == 1 and not first.closed
        assert rig.session.state is S.THINKING
        await until(lambda: rig.session.state is not S.THINKING)  # no reply to it either, in its own time
        await rig.stop()

    run(scenario)


@pytest.mark.parametrize('declined', ['no reply', 'an empty turn'])
def test_a_turn_left_unanswered_stops_the_latency_clock(monkeypatch: pytest.MonkeyPatch, declined: str) -> None:
    """The model declines a spoken turn (it may keep quiet): no reply comes, or an empty turn. When the robot later
    speaks to a note, no latency is reported: its audio is no reply to the human's turn."""
    monkeypatch.setattr(config, 'REPLY_TIMEOUT_S', 0.2)

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        live = rig.client.live
        live.push(**vad('ACTIVITY_START'))
        live.push(**heard('Pas pour toi.'))
        live.push(**vad('ACTIVITY_END'))
        await until(lambda: rig.session.state is S.THINKING)
        if declined == 'an empty turn':
            live.push(**content(turn_complete=True))
        await until(lambda: rig.session.state is S.IDLE)
        assert [e['text'] for e in rig.kinds('user_end')] == ['Pas pour toi.']
        rig.session.notify("L'app « Snake » est prête.")
        await until(lambda: live.turns)
        live.push(**speech(2400, said='Ton Snake est prêt.'))
        live.push(**content(turn_complete=True))
        await until(lambda: rig.kinds('robot_end'))
        assert rig.kinds('latency') == []
        await rig.stop()

    run(scenario)


def test_a_gesture_first_turn_times_its_spoken_reply() -> None:
    """The model answers with a gesture alone, its turn complete, and speaks once the gesture is answered: the
    latency runs from the end of the human's turn to that first word."""

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        live = rig.client.live
        live.push(**vad('ACTIVITY_START'))
        live.push(**heard('Tu es content ?'))
        live.push(**vad('ACTIVITY_END'))
        live.push(**call('do_gesture', 'g1', name='nod'))
        live.push(**content(turn_complete=True))
        await until(lambda: live.responses)
        await asyncio.sleep(0.05)
        assert rig.session.state is S.THINKING and rig.kinds('latency') == []
        live.push(**speech(2400, said='Oui !'))
        await until(lambda: rig.kinds('latency'))
        assert len(rig.kinds('latency')) == 1 and rig.kinds('latency')[0]['ms'] >= 50
        await rig.stop()

    run(scenario)


@pytest.mark.parametrize(
    ('typed_over', 'old_end'),
    [
        ('speech', [content(interrupted=True), content(turn_complete=True)]),
        ('speech', [content(turn_complete=True)]),
        ('a spoken turn', [content(interrupted=True), content(turn_complete=True)]),
    ],
    ids=['speech interrupted', 'speech played out', 'a spoken turn interrupted'],
)
def test_a_say_typed_over_an_open_answer_waits_for_its_own_reply(typed_over: str, old_end: list) -> None:
    """The owner types while the robot speaks, or before it answered a spoken turn. The generation the say cuts into
    ends after the say went out (sdk:types.py:20326, `interrupted > turn_complete`, or a turn_complete once played
    out): that end is not the say's. The robot thinks on, and the say's reply carries its latency."""

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        live = rig.client.live
        live.push(**heard('Raconte une histoire.'))
        live.push(**vad('ACTIVITY_END'))
        await until(lambda: rig.session.state is S.THINKING)
        if typed_over == 'speech':
            live.push(**_loud_speech(24000))  # 1 s of the robot's voice
            await until(lambda: rig.kinds('latency'))
        latencies = len(rig.kinds('latency'))
        rig.session.say('Stop, quelle heure est-il ?')
        await until(lambda: live.turns)
        for frame in old_end:
            live.push(**frame)
        await asyncio.sleep(0.1)
        assert rig.session.state is S.THINKING  # the say's answer is still owed
        live.push(**speech(2400, said='Il est midi.'))
        await until(lambda: len(rig.kinds('latency')) == latencies + 1)
        await rig.stop()

    run(scenario)


def test_a_late_chunk_of_the_answer_a_say_cut_into_leaves_the_says_reply_owed() -> None:
    """The answer streams while the robot speaks: one more chunk of it was on the wire when the say went out. It
    plays, the old turn ends, and the robot must still wait for the say's reply: a pending go_away is no reason to
    reconnect under it."""

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        live = rig.client.live
        live.push(go_away=types.LiveServerGoAway(time_left='60s'))
        live.push(**_loud_speech(4800))  # 0.2 s of an answer still streaming
        await until(lambda: rig.session.state is S.SPEAKING)
        rig.session.say('Stop, quelle heure est-il ?')
        await until(lambda: live.turns)
        live.push(**speech(2400))  # the old answer's next chunk, sent before the server read the say
        live.push(**content(turn_complete=True))  # the old answer's end, not the say's
        await asyncio.sleep(0.6)  # the old audio has played out
        assert len(rig.client.connections) == 1 and rig.session.state is S.THINKING, rig.phases()
        live.push(**speech(2400, said='Il est midi.'))
        live.push(**content(turn_complete=True))
        await until(lambda: len(rig.client.connections) == 2)  # the say answered: the quiet point comes
        await rig.stop()

    run(scenario)


def test_a_late_chunk_then_the_interruption_of_the_answer_a_say_cut_into_is_no_barge_in(monkeypatch) -> None:
    """As above, but the server cuts the old answer: its `interrupted` follows the late chunk. Nobody spoke, so the
    robot thinks on, and comes back to rest when no reply comes."""
    monkeypatch.setattr(config, 'REPLY_TIMEOUT_S', 0.2)

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        live = rig.client.live
        live.push(**_loud_speech(24000))  # 1 s of an answer still streaming
        await until(lambda: rig.session.state is S.SPEAKING)
        rig.session.say('Stop, parle-moi de la météo.')
        await until(lambda: live.turns)
        live.push(**speech(2400))  # the old answer's next chunk, sent before the server read the say
        live.push(**content(interrupted=True))
        await until(lambda: rig.audio.cleared == 1)
        assert rig.session.state is S.THINKING, rig.phases()
        await until(lambda: rig.session.state is S.IDLE)  # no reply came
        await rig.stop()

    run(scenario)


def test_a_turn_lost_with_its_connection_gives_no_later_answer_its_latency() -> None:
    """The human speaks; the socket is lost before any reply. After the reconnect the robot answers a note: that
    answer is no reply to the human's turn, which went with its connection."""

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        first = rig.client.live
        first.push(**heard('Tu es là ?'))
        first.push(**vad('ACTIVITY_END'))
        await until(lambda: rig.session.state is S.THINKING)
        first.inbox.put_nowait(ConnectionError('socket lost'))
        await until(lambda: len(rig.client.connections) == 2 and rig.session.state is S.IDLE)
        rig.session.notify("L'app « Snake » est prête.")
        live = rig.client.live
        await until(lambda: live.turns)
        live.push(**speech(2400, said='Ton Snake est prêt.'))
        live.push(**content(turn_complete=True))
        await until(lambda: rig.kinds('robot_end'))
        assert rig.kinds('latency') == []
        await rig.stop()

    run(scenario)


def test_a_tool_is_refused_without_speech_then_runs_after_a_typed_say(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**call('do_gesture', 'c1', name='nod'))
        await until(lambda: len(live.responses) == 1)
        refused = live.responses[0]
        assert (refused.id, refused.response['status'], refused.scheduling) == ('c1', 'refused', SILENT)
        assert refused.will_continue is None
        said = "Personne n'a parlé ces dix dernières secondes\xa0: cet appel vient d'un bruit, il n'a pas été exécuté."
        assert refused.response['message'] == said and rig.kinds('tool_result')[0]['message'] == said
        assert rig.body.gestures == []
        rig.session.say('Fais oui de la tête.')
        await until(lambda: len(live.turns) == 1)
        assert live.turns[0][0].parts[0].text == 'Fais oui de la tête.'
        live.push(**call('do_gesture', 'c2', name='nod'))
        await until(lambda: len(live.responses) == 2)
        assert live.responses[1].response == {'status': 'ok', 'message': 'Geste «\xa0nod\xa0» joué.'}
        assert rig.body.gestures == ['nod']
        assert [e['typed'] for e in rig.kinds('user_end')] == [True]
        await rig.stop()

    with caplog.at_level(logging.WARNING):
        run(scenario)
    assert caplog.text.count('[PHANTOM] do_gesture') == 1  # the refused call, not the one after the say


@pytest.mark.parametrize('typed', [False, True], ids=['spoken', 'typed'])
def test_a_human_turn_authorises_tools_only_for_the_grace_window(monkeypatch: pytest.MonkeyPatch, typed: bool) -> None:
    monkeypatch.setattr(config, 'HUMAN_GRACE_S', 0.3)

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        if typed:
            rig.session.say('Fais oui de la tête.')
        else:
            live.push(**heard('Fais oui de la tête.'))
        live.push(**call('do_gesture', 'c1', name='nod'))
        await until(lambda: len(live.responses) == 1)
        await asyncio.sleep(0.4)  # the human turn is now older than the grace window
        live.push(**call('do_gesture', 'c2', name='nod'))
        await until(lambda: len(live.responses) == 2)
        assert [r.response['status'] for r in live.responses] == ['ok', 'refused']
        assert rig.body.gestures == ['nod']
        await rig.stop()

    run(scenario)


@pytest.mark.parametrize(('ago', 'status'), [(9.9, 'ok'), (10.1, 'refused')])
def test_the_human_grace_window_is_ten_seconds(ago: float, status: str) -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        rig.session._last_human_at = time.monotonic() - ago  # the clock, set back: the last human turn
        rig.client.live.push(**call('do_gesture', 'c1', name='nod'))
        await until(lambda: rig.client.live.responses)
        assert rig.client.live.responses[0].response['status'] == status
        await rig.stop()

    run(scenario)


def test_an_interim_transcription_authorises_tools_but_only_the_committed_one_is_the_turns_text() -> None:
    """Interim words are the human's own speech: a call right after them runs. They never reach the transcript,
    or the committed transcription would repeat them."""

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**content(interim_input_transcription=types.Transcription(text='Fais oui')))
        live.push(**call('do_gesture', 'c1', name='nod'))
        await until(lambda: live.responses)
        assert live.responses[0].response['status'] == 'ok' and rig.body.gestures == ['nod']
        said = types.Transcription(text='Fais oui de la tête.')
        live.push(**content(input_transcription=said, turn_complete=True))
        await until(lambda: rig.kinds('user_end'))
        assert [e['text'] for e in rig.kinds('user_delta')] == ['Fais oui de la tête.']
        assert [e['text'] for e in rig.kinds('user_end')] == ['Fais oui de la tête.']
        await rig.stop()

    run(scenario)


class _BrokenCamera:
    """A camera whose pull raises: the `look` handler crashes rather than returning a result."""

    def read_jpeg(self) -> bytes:
        raise RuntimeError('appsink gone')


def test_every_call_is_answered_exactly_once_and_never_continued() -> None:
    mac = FakeMac(
        answers={
            '/api/volume': {'volume': 40},
            '/api/spotify/play': {
                'track': {'title': 'One More Time', 'artist': 'Daft Punk', 'uri': None},
                'device': 'Mac',
            },
        }
    )

    async def scenario() -> None:
        rig = await Rig(mac=mac, camera=_BrokenCamera()).start()
        live = rig.client.live
        live.push(**heard('Plein de choses, merci.'))
        live.push(**speech(said="D'accord."))  # the robot spoke: its physical results need no words
        calls = [
            call('set_volume', 'v', level=40),
            call('turn_body', 't', angle='à gauche'),
            call('play_music', 'm', query='Daft Punk'),
            call('teleport', 'x'),
            call('play_emotion', 'e', name='cheerful1'),
            call('look', 'k'),  # its handler raises
        ]
        for message in calls:
            live.push(**message)
        await until(lambda: len(live.responses) >= len(calls))
        await asyncio.sleep(0.05)
        assert sorted(r.id for r in live.responses) == ['e', 'k', 'm', 't', 'v', 'x']
        assert all(r.will_continue is None for r in live.responses)
        by_id = {r.id: r for r in live.responses}
        assert (by_id['v'].response['status'], by_id['v'].scheduling) == ('ok', SILENT)
        assert (by_id['t'].response['status'], by_id['t'].scheduling) == ('error', WHEN_IDLE)
        assert (by_id['m'].response['status'], by_id['m'].scheduling) == ('ok', WHEN_IDLE)
        assert (by_id['x'].response['status'], by_id['x'].scheduling) == ('error', WHEN_IDLE)
        assert (by_id['e'].response['status'], by_id['e'].scheduling) == ('ok', SILENT)
        assert (by_id['k'].response['status'], by_id['k'].scheduling) == ('error', WHEN_IDLE)
        assert rig.audio.sounds == ['/sounds/cheerful1.wav']
        await rig.stop()

    run(scenario)


@pytest.mark.parametrize(
    ('performing', 'instruction'), [(False, 'Dis-le simplement.'), (True, "N'en parle pas.")], ids=['crash', 'scene']
)
def test_the_dashboard_gets_what_happened_to_a_call_and_only_the_model_what_to_do(
    performing: bool, instruction: str
) -> None:
    async def scenario() -> None:
        rig = await Rig(camera=_BrokenCamera(), performing=lambda: performing).start()
        live = rig.client.live
        live.push(**heard('Regarde-moi.'))
        live.push(**call('look', 'k'))  # its handler raises
        await until(lambda: rig.kinds('tool_result'))
        shown, told = rig.kinds('tool_result')[0]['message'], live.responses[0].response['message']
        assert shown and instruction not in shown and 'look' not in shown and told == f'{shown} {instruction}'
        await rig.stop()

    run(scenario)


def test_only_the_withdrawn_call_is_cancelled_and_it_is_never_answered() -> None:
    async def scenario() -> None:
        rig = await Rig(mac=FakeMac(answers={'/api/volume': {'volume': 30}}, delay=0.3)).start()
        live = rig.client.live
        live.push(**heard('Mets du jazz, et baisse le son.'))
        live.push(**call('play_music', 'c1', query='jazz'))
        live.push(**call('set_volume', 'c2', level=30))
        await until(lambda: len(rig.mac.calls) == 2)  # both running
        live.push(tool_call_cancellation=types.LiveServerToolCallCancellation(ids=['c1']))
        await until(lambda: live.responses)
        await asyncio.sleep(0.1)
        assert [r.id for r in live.responses] == ['c2']
        assert {e['call_id']: e['status'] for e in rig.kinds('tool_result')} == {'c1': 'cancelled', 'c2': 'ok'}
        await rig.stop()

    run(scenario)


def _calls(*calls: types.FunctionCall) -> dict[str, Any]:
    """One tool_call message carrying several function calls."""
    return {'tool_call': types.LiveServerToolCall(function_calls=list(calls))}


def test_a_call_id_received_again_runs_and_is_answered_once() -> None:
    async def scenario() -> None:
        rig = await Rig(mac=FakeMac(answers={'/api/volume': {'volume': 30}})).start()
        live = rig.client.live
        live.push(**heard('Baisse le son.'))
        volume = types.FunctionCall(id='dup', name='set_volume', args={'level': 30})
        live.push(**_calls(volume, volume))  # twice in one message...
        await until(lambda: live.responses)
        live.push(**_calls(volume))  # ...and once more after its answer
        await asyncio.sleep(0.1)
        assert [r.id for r in live.responses] == ['dup'] and len(rig.mac.calls) == 1
        assert [e['call_id'] for e in rig.kinds('tool_call')] == ['dup']
        await rig.stop()

    run(scenario)


def test_a_call_id_received_twice_is_withdrawn_whole() -> None:
    async def scenario() -> None:
        rig = await Rig(mac=FakeMac(delay=0.3)).start()
        live = rig.client.live
        live.push(**heard('Mets du jazz.'))
        music = types.FunctionCall(id='dup', name='play_music', args={'query': 'jazz'})
        live.push(**_calls(music, music))
        await until(lambda: rig.mac.calls)
        live.push(tool_call_cancellation=types.LiveServerToolCallCancellation(ids=['dup']))
        await asyncio.sleep(0.4)  # past the Mac's answer
        assert live.responses == [] and len(rig.mac.calls) == 1
        assert [e['status'] for e in rig.kinds('tool_result')] == ['cancelled']
        await rig.stop()

    run(scenario)


def test_calls_sent_with_goto_sleep_do_nothing_once_it_ran() -> None:
    """« Fais un câlin, baisse le son et dors »: an emotion still waiting for its move, and calls not started
    yet, act on nothing once the robot was told to sleep: no move, no sound, no Mac."""

    async def scenario() -> None:
        rig = await Rig(mac=FakeMac(answers={'/api/volume': {'volume': 10}})).start()
        live = rig.client.live
        live.push(**heard('Fais un câlin, baisse le son, tourne-toi, puis dors.'))
        await until(lambda: rig.kinds('user_delta'))
        live.push(
            **_calls(
                types.FunctionCall(id='e', name='play_emotion', args={'name': 'loving1'}),  # suspends on its lookup
                types.FunctionCall(id='s', name='goto_sleep', args={}),
                types.FunctionCall(id='g', name='do_gesture', args={'name': 'nod'}),
                types.FunctionCall(id='v', name='set_volume', args={'level': 10}),
                types.FunctionCall(id='t', name='turn_body', args={'angle': 90}),
            )
        )
        assert await asyncio.wait_for(rig.task, 3.0) == 'goto_sleep'
        await asyncio.sleep(0.05)
        assert (rig.body.moves, rig.body.gestures, rig.body.turns, rig.audio.sounds) == ([], [], [], [])
        assert rig.mac.calls == [] and live.responses == []
        results = {e['call_id']: e['status'] for e in rig.kinds('tool_result')}
        assert results == {'s': 'ok', 'e': 'cancelled', 'g': 'cancelled', 'v': 'cancelled', 't': 'cancelled'}

    run(scenario)


def test_a_sleep_that_lands_before_a_calls_first_step_cancels_it() -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        rig.client.live.push(**heard('Tourne-toi.'))
        await until(lambda: rig.kinds('user_delta'))
        rig.session._on_message(types.LiveServerMessage(**call('turn_body', 't', angle=90)))  # its task is created
        rig.session.request_sleep('dashboard')  # and the dashboard's sleep runs before its first step
        assert await asyncio.wait_for(rig.task, 3.0) == 'dashboard'
        assert rig.body.turns == [] and rig.client.live.responses == []
        assert [(e['call_id'], e['status']) for e in rig.kinds('tool_result')] == [('t', 'cancelled')]

    run(scenario)


def test_sleep_cancels_the_calls_still_running() -> None:
    async def scenario() -> None:
        rig = await Rig(mac=FakeMac(delay=0.3)).start()
        live = rig.client.live
        live.push(**heard('Mets ma musique.'))
        live.push(**call('play_music', 'c1', kind='liked'))
        await until(lambda: rig.mac.calls)
        assert await rig.stop() == 'dashboard'
        await asyncio.sleep(0.4)
        assert live.responses == []
        assert [e['status'] for e in rig.kinds('tool_result')] == ['cancelled']
        assert rig.body.stopped == 1

    run(scenario)


def test_a_call_still_on_the_wire_does_nothing_once_asleep() -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**heard('Fais oui, puis dors.'))
        await until(lambda: rig.kinds('user_delta'))  # someone spoke: the call would pass the guard
        live.push(**call('do_gesture', 'c1', name='nod'))
        rig.session.request_sleep('dashboard')  # in the same tick: the call is read after the order
        assert await asyncio.wait_for(rig.task, 3.0) == 'dashboard'
        assert rig.body.gestures == [] and live.responses == [] and rig.kinds('tool_call') == []

    run(scenario)


def test_a_call_is_answered_on_its_own_connection_only() -> None:
    async def scenario() -> None:
        rig = await Rig(mac=FakeMac(delay=0.2)).start()
        first = rig.client.live
        first.push(**heard('Mets du jazz.'))
        first.push(**call('play_music', 'c1', query='jazz'))
        await until(lambda: rig.mac.calls)
        first.inbox.put_nowait(ConnectionError('socket closed'))  # lost while the Mac works
        await until(lambda: len(rig.client.connections) == 2 and rig.kinds('tool_result'))
        await asyncio.sleep(0.05)
        assert first.responses == [] and rig.client.live.responses == []  # the new socket never issued c1
        assert rig.kinds('tool_result')[0]['status'] == 'ok'  # the dashboard still sees what happened
        await rig.stop()

    run(scenario)


def test_goto_sleep_ends_the_session_silently() -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**heard('Dors.'))
        live.push(**speech(24000, said='Bonne nuit'))
        await until(lambda: rig.session.state is S.SPEAKING)
        live.push(**call('goto_sleep', 'c1'))
        assert await asyncio.wait_for(rig.task, 3.0) == 'goto_sleep'
        pushed = len(rig.audio.pushed)
        assert rig.audio.cleared >= 1 and live.responses == []  # goto_sleep itself is never answered
        await until(lambda: rig.audio.sound_stops >= 1)  # nor does an emotion's sound play on
        assert rig.session.state is S.CLOSING
        rig.session._play(b'\x00\x00' * 2400)  # whatever arrives after the order stays unheard
        assert len(rig.audio.pushed) == pushed
        # ...but the generation that asked for the sleep is still billed on the dashboard.
        rig.session._on_message(types.LiveServerMessage(usage_metadata=types.UsageMetadata(total_token_count=1900)))
        assert [e['total_tokens'] for e in rig.kinds('usage')] == [1900]
        # The turns the sleep cut short still end: the dashboard finalises their rows.
        assert [e['text'] for e in rig.kinds('user_end')] == ['Dors.']
        assert [e['text'] for e in rig.kinds('robot_end')] == ['Bonne nuit']

    run(scenario)


def test_a_turn_ends_with_its_connection() -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        rig.client.live.push(**heard('Allô ?'))
        await until(lambda: rig.kinds('user_delta'))
        rig.client.live.inbox.put_nowait(ConnectionError('dropped'))
        await until(lambda: len(rig.client.connections) == 2)
        rig.client.live.push(**heard('Tu es là ?'))
        await until(lambda: len(rig.kinds('user_delta')) == 2)
        await rig.stop()
        ends = rig.kinds('user_end')
        assert [e['text'] for e in ends] == ['Allô ?', 'Tu es là ?']  # two rows, not one merged across the gap
        assert ends[0]['turn_id'] != ends[1]['turn_id']

    run(scenario)


def test_look_sends_the_picture_as_its_own_turn() -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**heard('Regarde.'))
        live.push(**call('look', 'c1'))
        live.push(**call('look', 'c2'))  # the same ask, twice in one message
        await until(lambda: len(live.responses) == 2)
        assert len(live.turns) == 1
        image = live.turns[0][0].parts[0].inline_data
        assert (image.mime_type, image.data) == ('image/jpeg', b'\xff\xd8jpeg')
        assert {r.scheduling for r in live.responses} == {SILENT}
        assert 'image_base64' not in str(live.responses[0].response)
        results = {e['call_id']: e for e in rig.kinds('tool_result')}
        assert results['c1']['data']['image_base64'] and results['c2']['data'] == {}
        await rig.stop()

    run(scenario)


def test_go_away_reconnects_with_the_handle_at_a_quiet_point(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (5.0, 5.0, 5.0))  # a planned reconnect waits no backoff

    async def scenario() -> None:
        rig = await Rig().start()
        first = rig.client.live
        first.push(**content(turn_complete=True))  # an exchange done: the connection has proven itself
        first.push(**resumable('h0'))
        first.push(**resumable('h1'))  # the latest handle holds the latest context
        first.push(**speech(4800))  # 0.2 s of audio still to play...
        first.push(**content(turn_complete=True))  # ...of an answer that is over
        first.push(go_away=types.LiveServerGoAway(time_left='60s'))
        await until(lambda: rig.session.state is S.SPEAKING)
        assert len(rig.client.connections) == 1  # not while it talks
        await until(lambda: len(rig.client.connections) == 2)
        assert first.closed and rig.client.live.handle == 'h1'
        await until(lambda: rig.session.state is S.IDLE)
        assert await rig.stop() == 'dashboard'

    run(scenario)


def test_a_go_away_at_every_setup_runs_out_of_reconnects() -> None:
    async def scenario() -> None:
        rig = Rig()
        rig.client.on_open = lambda live: live.push(go_away=types.LiveServerGoAway(time_left='0s'))
        rig.task = asyncio.create_task(rig.session.run())
        assert await asyncio.wait_for(rig.task, 3.0) == 'connection_lost'
        assert len(rig.client.connections) == 4  # the first connection, then the 3 of the budget

    run(scenario)


def test_a_pending_go_away_waits_for_the_reply_to_a_tool_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'REPLY_TIMEOUT_S', 0.5)
    play = {'track': {'title': 'So What', 'artist': 'Miles Davis'}, 'device': 'Mac'}
    mac = FakeMac(answers={'/api/spotify/play': play}, delay=0.6)  # slower than the reply timeout

    async def scenario() -> None:
        rig = await Rig(mac=mac).start()
        live = rig.client.live
        live.push(**heard('Mets du jazz'), **vad('ACTIVITY_END'))
        live.push(**call('play_music', 'c1', query='jazz'))
        live.push(**content(turn_complete=True))  # the plain model: the turn ends with the call made
        live.push(go_away=types.LiveServerGoAway(time_left='50s'))
        await until(lambda: live.responses)  # the track is out (WHEN_IDLE): the model is about to say it
        await asyncio.sleep(0.1)
        assert rig.session.state is S.THINKING and len(rig.client.connections) == 1  # no quiet point yet
        live.push(**speech(2400, said='Voici So What, de Miles Davis.'))
        live.push(**content(turn_complete=True))
        await until(lambda: len(rig.client.connections) == 2)  # said, played out: now the reconnect
        await rig.stop()

    run(scenario)


def test_a_pending_go_away_waits_through_a_pause_inside_the_answer() -> None:
    """The answer pauses mid-turn (a server-side search, jitter) longer than its audio lasts: the speaker drains,
    yet the answer is not over, so it is no quiet point."""

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**content(turn_complete=True))  # an exchange done: the connection has proven itself
        live.push(**heard('Quel temps fera-t-il demain ?'), **vad('ACTIVITY_END'))
        live.push(go_away=types.LiveServerGoAway(time_left='60s'))
        live.push(**speech(2400, said='Je regarde ça.'))  # 0.1 s, then nothing while Google searches
        await until(lambda: rig.session.state is S.SPEAKING)
        await asyncio.sleep(0.3)
        assert rig.session.state is S.THINKING and len(rig.client.connections) == 1
        live.push(**speech(2400, said='Demain, douze degrés.'))
        live.push(**content(turn_complete=True))
        await until(lambda: len(rig.client.connections) == 2)  # said and played out: now the reconnect
        await rig.stop()

    run(scenario)


def test_a_pending_go_away_waits_for_the_reply_owed_to_a_fast_tool() -> None:
    """A gesture-first turn: the tool's answer goes out (WHEN_IDLE), then the tool step's turn ends. The spoken
    reply is still owed: no quiet point until it has been said."""

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**heard('Fais oui de la tête.'), **vad('ACTIVITY_END'))
        live.push(go_away=types.LiveServerGoAway(time_left='50s'))
        live.push(**call('do_gesture', 'g', name='nod'))
        await until(lambda: live.responses)
        assert live.responses[0].scheduling == WHEN_IDLE
        live.push(**content(turn_complete=True))  # read after the answer went out
        await asyncio.sleep(0.1)
        assert rig.session.state is S.THINKING and len(rig.client.connections) == 1
        live.push(**speech(2400, said="Voilà, c'est fait."))
        live.push(**content(turn_complete=True))
        await until(lambda: len(rig.client.connections) == 2)
        await rig.stop()

    run(scenario)


@pytest.mark.parametrize('time_left', ['3s', None, 'soon'], ids=['3 s', 'none', 'unreadable'])
def test_go_away_with_little_time_left_reconnects_at_once(time_left: str | None) -> None:
    """A time left that cannot be read is taken as none left."""

    async def scenario() -> None:
        rig = await Rig().start()
        rig.client.live.push(
            session_resumption_update=types.LiveServerSessionResumptionUpdate(resumable=True, new_handle='h2')
        )
        rig.client.live.push(**heard('Une longue histoire ?'), **vad('ACTIVITY_START'))
        await until(lambda: rig.session.state is S.LISTENING)
        rig.client.live.push(go_away=types.LiveServerGoAway(time_left=time_left))
        await until(lambda: len(rig.client.connections) == 2)
        assert rig.client.live.handle == 'h2'
        await rig.stop()

    run(scenario)


def test_a_refused_handle_opens_a_fresh_session_and_is_never_offered_again(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        closed = ws.ConnectionClosedError(Close(1008, 'session not found'), None)
        rig.client.refuse = lambda cfg: closed if cfg.session_resumption.handle else None
        rig.client.live.push(**resumable('stale'))
        rig.client.live.push(go_away=types.LiveServerGoAway(time_left='0s'))
        await until(lambda: len(rig.client.connections) == 2)
        assert [a.session_resumption.handle for a in rig.client.attempts] == [None, 'stale', None]
        rig.client.live.inbox.put_nowait(ConnectionError('dropped'))  # lost before it handed out a handle
        await until(lambda: len(rig.client.connections) == 3)
        assert [a.session_resumption.handle for a in rig.client.attempts] == [None, 'stale', None, None]
        await rig.stop()

    with caplog.at_level(logging.WARNING):
        run(scenario)
    assert '[RESUME] handle refused' in caplog.text


@pytest.mark.parametrize(
    'errors_on_the_way',
    [
        [ConnectionRefusedError('no route'), socket.gaierror(-3, 'Temporary failure in name resolution')],
        [OSError(101, 'Network is unreachable'), genai_errors.APIError(1006, {'message': 'Abnormal closure.'})],
    ],
    ids=['no-network', 'reset-during-the-setup'],
)
def test_a_handle_survives_reconnects_that_never_got_through(errors_on_the_way: list[Exception]) -> None:
    """A Wi-Fi blip of a few seconds: the connects fail on the way, which says nothing about the handle, so the
    try that gets through still resumes the conversation."""

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**content(turn_complete=True))  # proven
        live.push(**resumable('h1'))
        await until(lambda: rig.session._handle == 'h1')
        rig.client.fail = list(errors_on_the_way)
        live.inbox.put_nowait(ConnectionError('dropped'))
        await until(lambda: len(rig.client.connections) == 2)
        assert [a.session_resumption.handle for a in rig.client.attempts] == [None, 'h1', 'h1', 'h1']
        assert rig.client.live.handle == 'h1'
        await rig.stop()

    run(scenario)


def test_a_connect_that_never_completes_is_a_failed_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'CONNECT_TIMEOUT_S', 0.05)

    async def scenario() -> None:
        rig = Rig()
        rig.client.stall = True  # the server takes the socket and never completes the setup
        rig.task = asyncio.create_task(rig.session.run())
        assert await asyncio.wait_for(rig.task, 3.0) == 'connection_lost'  # not awake and deaf for good
        assert len(rig.client.attempts) == 4

    run(scenario)


@pytest.mark.parametrize('waiting', ['connect', 'settings', 'backoff'])
def test_a_sleep_never_waits_on_the_network(monkeypatch: pytest.MonkeyPatch, waiting: str) -> None:
    """Asked while a connect hangs, while an open socket waits for this wake's settings, or during the backoff after
    a failed connect: the sleep ends the session at once, with no connect begun after it."""
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (5.0, 5.0, 5.0))  # CONNECT_TIMEOUT_S stays 10 s

    async def scenario() -> None:
        rig = Rig(settled=waiting != 'settings')
        rig.client.stall = waiting == 'connect'
        rig.client.fail = [ConnectionError('no route')] if waiting == 'backoff' else []
        rig.task = asyncio.create_task(rig.session.run())
        await until(lambda: rig.client.attempts)
        await asyncio.sleep(0.05)  # it is waiting
        assert rig.session.state is S.CONNECTING and not rig.task.done()
        assert await rig.stop() == 'dashboard'  # within 3 s
        assert len(rig.client.attempts) == 1
        assert len(rig.client.connections) == (waiting == 'settings') and all(c.closed for c in rig.client.connections)
        assert rig.phases()[-1] == 'closing'

    run(scenario)


def test_a_sleep_asked_while_a_silent_peer_holds_the_connect_waits_at_most_the_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real SDK, its socket upgraded and then never answered (CONNECT_TIMEOUT_S stays 10 s): the connect given
    up closes its socket, a handshake nobody answers either, bounded by CLOSE_TIMEOUT_S like a connected session's."""
    monkeypatch.setattr(config, 'CLOSE_TIMEOUT_S', 0.3)

    async def scenario() -> float:
        async with silent_peer(monkeypatch) as (client, upgraded):
            rig = Rig()
            rig.session._client = client
            rig.task = asyncio.create_task(rig.session.run())
            await until(lambda: upgraded)
            await asyncio.sleep(0.1)  # the setup sent, never answered
            assert rig.session.state is S.CONNECTING
            started = time.monotonic()
            rig.session.request_sleep('dashboard')
            assert await rig.task == 'dashboard'
            return time.monotonic() - started

    took = asyncio.run(asyncio.wait_for(scenario(), 30.0))
    assert took < config.CLOSE_TIMEOUT_S + 0.5, f'the sleep waited {took:.1f} s'


def test_a_handle_whose_connections_die_at_once_is_dropped_for_the_last_try() -> None:
    async def scenario() -> None:
        rig = await Rig().start()

        def accept_then_close(live: FakeLive) -> None:
            if live.handle:  # the server takes the setup, then closes (the SDK's APIError): it refuses the handle
                live.inbox.put_nowait(genai_errors.APIError(1008, {'message': 'session not found'}))

        rig.client.on_open = accept_then_close
        rig.client.live.push(
            session_resumption_update=types.LiveServerSessionResumptionUpdate(resumable=True, new_handle='gone')
        )
        rig.client.live.inbox.put_nowait(ConnectionError('dropped'))
        await until(lambda: len(rig.client.connections) == 4)
        assert [c.handle for c in rig.client.connections] == [None, 'gone', 'gone', None]
        await until(lambda: rig.session.state is S.IDLE)
        assert not rig.task.done()
        await rig.stop()

    run(scenario)


def test_a_handle_is_kept_when_its_resumed_connection_is_lost_to_the_network() -> None:
    """The Wi-Fi flaps in a proven conversation: the resumed connection serves a while (under RECONNECT_PROVEN_S,
    with no turn), then dies with no close frame (1006), and the next connect does not get through. A loss to the
    network says nothing about the handle: the last try still resumes."""

    async def scenario() -> None:
        rig = await Rig().start()
        first = rig.client.live
        first.push(**content(turn_complete=True))  # proven
        first.push(**resumable('h1'))
        await until(lambda: rig.session._handle == 'h1')
        first.inbox.put_nowait(genai_errors.APIError(1006, {'message': 'Abnormal closure.'}))
        await until(lambda: len(rig.client.connections) == 2)
        second = rig.client.live
        await asyncio.sleep(0.3)  # it serves: the human could talk
        rig.client.fail = [OSError(101, 'Network is unreachable')]
        second.inbox.put_nowait(genai_errors.APIError(1006, {'message': 'Abnormal closure.'}))
        await until(lambda: len(rig.client.connections) == 3)
        assert [a.session_resumption.handle for a in rig.client.attempts] == [None, 'h1', 'h1', 'h1']
        await rig.stop()

    run(scenario)


def _unreachable(tried: list[float]):
    """A FakeClient.refuse that refuses every connect, noting when each was tried."""

    def refuse(_config: types.LiveConnectConfig) -> BaseException:
        tried.append(time.monotonic())
        return ConnectionError('no route')

    return refuse


def test_a_lost_connection_runs_out_of_reconnects_and_the_session_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (0.1, 0.2, 0.3))

    async def scenario() -> None:
        rig = await Rig().start()
        tried: list[float] = []
        rig.client.refuse = _unreachable(tried)
        rig.client.live.push(**heard('Allô ?'))
        lost_at = time.monotonic()
        rig.client.live.inbox.put_nowait(ConnectionError('socket closed'))
        assert await asyncio.wait_for(rig.task, 3.0) == 'connection_lost'
        assert len(rig.client.attempts) == 4  # the first connection, then 3 retries
        waits = np.diff([lost_at, *tried])
        assert all(wait >= backoff * 0.9 for wait, backoff in zip(waits, (0.1, 0.2, 0.3), strict=True)), waits
        assert rig.phases()[-2:] == ['reconnecting', 'closing']
        assert rig.body.stopped == 1

    run(scenario)


@pytest.mark.parametrize(
    'refusal',
    [
        genai_errors.APIError(
            1011, {'message': 'Your prepayment credits are depleted.', 'status': 'RESOURCE_EXHAUSTED'}
        ),
        genai_errors.APIError(1007, {'message': 'Request contains an invalid argument.', 'status': 'INVALID_ARGUMENT'}),
        ws.InvalidStatus(Response(403, 'Forbidden', Headers())),
    ],
    ids=['credits-depleted', 'setup-rejected', 'http-403'],
)
def test_a_gemini_that_refuses_every_connect_ends_the_conversation_refused_not_lost(refusal: Exception) -> None:
    """Gemini itself says no (its close frame, an HTTP 4xx): the budget is spent as for a lost network, but the
    conversation ends `refused`, never « connexion à Gemini perdue »."""

    async def scenario() -> None:
        rig = Rig()
        rig.client.refuse = lambda _config: refusal
        rig.task = asyncio.create_task(rig.session.run())
        assert await asyncio.wait_for(rig.task, 3.0) == 'refused'
        assert len(rig.client.attempts) == 4  # the first connect, then the 3 of the budget
        assert rig.phases() == ['connecting', 'closing']

    run(scenario)


def test_the_last_connect_decides_whether_gemini_refused_or_the_network_failed() -> None:
    async def scenario() -> None:
        rig = Rig()
        refused = genai_errors.APIError(1011, {'message': 'Internal error encountered.', 'status': 'INTERNAL'})
        rig.client.fail = [refused, refused, refused, ConnectionError('no route')]
        rig.task = asyncio.create_task(rig.session.run())
        assert await asyncio.wait_for(rig.task, 3.0) == 'connection_lost'

    run(scenario)


def test_a_reconnect_mid_conversation_is_no_wake_for_the_dashboard() -> None:
    """Gemini lost mid-conversation: the robot is awake and reconnecting, and says so (`reconnecting`), not that it
    wakes up again (`connecting`, the wake's phase). A wake whose first connects fail is still waking."""

    async def scenario() -> None:
        rig = await Rig().start()
        rig.client.live.push(**content(turn_complete=True))
        await asyncio.sleep(0.05)
        rig.client.fail = [OSError(101, 'Network is unreachable')] * 2
        rig.client.live.inbox.put_nowait(genai_errors.APIError(1006, {'message': 'Abnormal closure.'}))
        await until(lambda: len(rig.client.connections) == 2 and rig.session.state is S.IDLE)
        assert rig.phases() == ['connecting', 'idle', 'reconnecting', 'idle']
        assert rig.body.modes == ['idle', 'idle', 'idle']  # it waits as it does at the wake
        await rig.stop()
        waking = Rig()
        waking.client.fail = [OSError(101, 'Network is unreachable')]
        waking.task = asyncio.create_task(waking.session.run())
        await until(lambda: waking.session.state is S.IDLE)
        assert waking.phases() == ['connecting', 'idle']
        await waking.stop()

    run(scenario)


def test_a_proven_connection_lost_waits_its_first_backoff_and_keeps_three_retries(monkeypatch) -> None:
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (0.15, 0.01, 0.01))

    async def scenario() -> None:
        rig = await Rig().start()
        tried: list[float] = []
        rig.client.refuse = _unreachable(tried)
        rig.client.live.push(**content(turn_complete=True))  # an exchange done: the connection proved itself
        await asyncio.sleep(0.02)
        lost_at = time.monotonic()
        rig.client.live.inbox.put_nowait(ConnectionError('socket closed'))
        assert await asyncio.wait_for(rig.task, 3.0) == 'connection_lost'
        assert len(tried) == 3 and tried[0] - lost_at >= 0.15 * 0.9, (len(tried), tried[0] - lost_at)

    run(scenario)


def test_a_task_that_crashes_ends_its_connection_like_a_lost_socket(monkeypatch, caplog) -> None:
    """A bug in one of a connection's tasks is logged and ends that connection as a dropped socket would: once the
    connection has proven itself, it reconnects as often as it takes, more often than the budget."""
    crash: list[asyncio.Event] = []

    async def pump(self: session_module.Session) -> str:
        await crash[0].wait()
        crash[0].clear()
        raise RuntimeError('a bug in the pump')

    monkeypatch.setattr(session_module.Session, '_pump_mic', pump)

    async def scenario() -> None:
        crash.append(asyncio.Event())
        rig = await Rig().start()
        for n in range(1, 6):
            rig.client.live.push(**content(turn_complete=True))  # an exchange done: the connection proved itself
            await asyncio.sleep(0.02)
            crash[0].set()
            await until(lambda n=n: len(rig.client.connections) == n + 1)
            await until(lambda: rig.session.state is S.IDLE)
        assert not rig.task.done()
        assert await rig.stop() == 'dashboard'

    with caplog.at_level(logging.ERROR):
        run(scenario)
    assert caplog.text.count('[LIVE] mic failed') == 5


def test_a_cancelled_conversation_still_tears_down() -> None:
    """Cancelled where it stands (a second Ctrl-C ends the event loop): it closes all the same, its socket closed
    and its moves stopped."""

    async def scenario() -> None:
        rig = await Rig().start()
        rig.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rig.task
        assert (rig.session.state, rig.session.end_reason) == (S.CLOSING, 'error')
        assert rig.phases()[-1] == 'closing' and rig.client.live.closed and rig.body.stopped == 1

    run(scenario)


def test_a_proven_connection_resets_the_reconnect_budget() -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        for n in range(1, 6):  # more losses than the budget, each after a completed turn
            rig.client.live.push(**content(turn_complete=True))
            await asyncio.sleep(0.02)
            rig.client.live.inbox.put_nowait(ConnectionError('dropped'))
            await until(lambda n=n: len(rig.client.connections) == n + 1)
            await until(lambda: rig.session.state is S.IDLE)
        assert not rig.task.done()
        await rig.stop()

    run(scenario)


def test_a_connection_that_lived_long_enough_resets_the_reconnect_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'RECONNECT_PROVEN_S', 0.1)

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        for n in range(1, 5):  # more losses than the budget, and not one turn completed
            await asyncio.sleep(0.15)  # each connection outlives RECONNECT_PROVEN_S
            rig.client.live.inbox.put_nowait(ConnectionError('dropped'))
            await until(lambda n=n: len(rig.client.connections) == n + 1)
            await until(lambda: rig.session.state is S.IDLE)
        assert not rig.task.done()
        await rig.stop()

    run(scenario)


def test_the_robot_sleeps_when_nothing_happens() -> None:
    async def scenario() -> None:
        started = time.monotonic()
        rig = await Rig(settings_view(idle_sleep_minutes=0.3 / 60)).start()
        assert await asyncio.wait_for(rig.task, 2.0) == 'idle'
        assert time.monotonic() - started >= 0.3  # the delay is in minutes

    run(scenario)


def test_a_build_it_asked_for_holds_the_idle_sleep_off() -> None:
    async def scenario() -> None:
        building = [True]
        rig = await Rig(settings_view(idle_sleep_minutes=0.2 / 60), held=lambda: building[0]).start()
        await asyncio.sleep(0.5)
        assert not rig.task.done()
        building[0] = False
        assert await asyncio.wait_for(rig.task, 2.0) == 'idle'

    run(scenario)


def _held_past_the_idle_sleep(hook: str, lag_s: float) -> None:
    """Held through `hook` longer than the idle delay, then let go: no idle sleep while held, then a whole idle delay
    from the last time the session saw the hold. Read on the session's own clock: a late step of the test changes
    nothing."""

    async def scenario() -> None:
        holding, seen = [True], []  # each time the session saw the hold: the last one is its end

        def probe() -> bool:
            if holding[0]:
                seen.append(time.monotonic())
            return holding[0]

        rig = await Rig(settings_view(idle_sleep_minutes=0.2 / 60), **{hook: probe}).start()
        await asyncio.sleep(0.5)  # longer than the idle sleep: no voice, no human, no tool call
        time.sleep(lag_s)  # the loop given back late
        assert not rig.task.done()
        holding[0] = False
        assert await asyncio.wait_for(rig.task, 2.0) == 'idle'
        assert time.monotonic() - seen[-1] >= 0.2

    run(scenario)


@pytest.mark.parametrize('lag_s', [0.0, 0.05])  # 0.05: a loaded machine, the test's own step as late
def test_a_manette_hold_longer_than_the_idle_sleep_holds_it_off_to_its_end(lag_s: float) -> None:
    """A silent game with the body lent as a controller: no idle sleep while held, then a whole idle delay from the
    let-go (« Rendre la main »), so « Rejouer » finds the robot awake."""
    _held_past_the_idle_sleep('held', lag_s)


@pytest.mark.parametrize('lag_s', [0.0, 0.05])
def test_a_scene_longer_than_the_idle_sleep_holds_it_off(lag_s: float) -> None:
    """No idle sleep during a scene: between its voices nothing pushes the playback clock, and under the comfort
    noise no human speech reaches the VAD. The idle sleep then counts from its end."""
    _held_past_the_idle_sleep('performing', lag_s)


class _ReadyOnCue(FakeMac):
    """The Mac lists the app as building until `done`; its « ready » then waits for `go`, so the test picks the loop
    turn the answer lands in."""

    BUILDING = {'id': 'a1', 'title': 'Snake', 'status': 'building'}

    def __init__(self) -> None:
        super().__init__({'/api/apps': {'apps': [self.BUILDING]}})
        self.done, self.asked, self.go, self.answered = (threading.Event() for _ in range(4))

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.done.is_set():
            return super().request(method, path, body)
        self.asked.set()
        self.go.wait(5.0)
        self.answered.set()
        return {'apps': [{**self.BUILDING, 'status': 'ready', 'error': None}]}


def test_a_build_longer_than_the_idle_sleep_is_announced_before_any_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The build's hold ends in the step that queues its note, even when the Mac's « ready » and a due watchdog tick
    land in one loop turn: the note itself keeps the robot awake until it is answered."""
    monkeypatch.setattr(apps, 'BUILD_POLL_S', 0.02)

    async def scenario() -> None:
        mac = _ReadyOnCue()
        rig = Rig(settings_view(idle_sleep_minutes=0.2 / 60), held=lambda: builds.pending())
        builds = apps.Builds(mac, rig.session.notify)  # wired as in main.py
        await rig.start()
        builds.watch(mac.BUILDING)
        await asyncio.sleep(0.5)  # longer than the idle sleep, in silence: held by the build
        assert not rig.task.done()
        mac.done.set()
        await until(mac.asked.is_set)
        mac.go.set()
        mac.answered.wait(5.0)
        time.sleep(0.05)  # the loop busy: the answer is queued, a watchdog tick falls due behind it
        await until(lambda: rig.client.live.turns or rig.task.done())
        assert rig.client.live.turns, f'asleep without a word: {rig.task.result() if rig.task.done() else ""}'
        await asyncio.sleep(0.12)  # a watchdog tick or more
        assert not rig.task.done()  # awake to say it
        rig.client.live.push(**_loud_speech(2400))
        await until(lambda: rig.session.state is S.SPEAKING)
        await rig.stop()

    run(scenario)


def test_a_note_waits_for_a_scene_to_end_and_for_a_connection() -> None:
    async def scenario() -> None:
        playing = [True]
        rig = await Rig(settings_view(idle_sleep_minutes=0), performing=lambda: playing[0]).start()
        rig.session.notify('fini')
        await asyncio.sleep(0.3)
        assert rig.client.live.turns == []  # the model's audio is muted during a scene: it would be lost
        playing[0] = False
        await until(lambda: rig.client.live.turns)
        await rig.stop()

    run(scenario)


def test_what_the_model_does_after_a_note_is_no_humans_ask() -> None:
    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        live = rig.client.live
        rig.session.say('Merci Reachy.')  # a human, a moment ago
        await until(lambda: live.turns)
        live.push(**content(turn_complete=True))
        await until(lambda: rig.session.state is S.IDLE)
        rig.session.notify("L'app « Snake » n'a pas pu être construite.")
        await until(lambda: len(live.turns) == 2)
        live.push(**call('app', 'a1', action='create', request='un Snake'))  # the model retries on its own
        await until(lambda: rig.kinds('tool_result'))
        refused = rig.kinds('tool_result')[0]
        assert refused['status'] == 'refused'
        assert 'note' in refused['message'] and 'Personne' not in refused['message']  # the human spoke just now
        rig.session.say('Oui, réessaie.')  # a human asks again: tools run again
        live.push(**call('do_gesture', 'g1', name='nod'))
        await until(lambda: len(rig.kinds('tool_result')) == 2)
        assert rig.kinds('tool_result')[1]['status'] == 'ok'
        await rig.stop()

    run(scenario)


def test_a_note_waits_until_the_robot_is_quiet() -> None:
    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        live = rig.client.live
        live.push(**heard('Raconte-moi une histoire.'))
        live.push(**_loud_speech(int(0.4 * config.GEMINI_OUTPUT_SR)))
        await until(lambda: rig.session.state is S.SPEAKING)
        rig.session.notify("L'app « Snake » est prête.")
        await asyncio.sleep(0.15)
        assert live.turns == []  # the robot is talking: the note waits
        live.push(**content(turn_complete=True))
        await until(lambda: live.turns)
        note = "(Note du tableau de bord, personne ne parle.) L'app « Snake » est prête."
        assert live.turns[0][0].parts[0].text == note
        assert rig.session.state is S.THINKING  # the model has something to answer
        await rig.stop()

    run(scenario)


def test_a_note_authorises_no_tool_and_its_answer_is_no_phantom(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        live = rig.client.live
        rig.session.notify("L'app « Snake » est prête.")
        await until(lambda: live.turns)
        live.push(**call('do_gesture', 'g1', name='nod'))
        await until(lambda: rig.kinds('tool_result'))
        assert rig.kinds('tool_result')[0]['status'] == 'refused'  # nobody spoke: a note is not a human
        live.push(**_loud_speech(2400))
        await until(lambda: rig.session.state is S.SPEAKING)
        assert 'the robot answers, but nobody spoke' not in caplog.text
        await rig.stop()

    run(scenario)


@pytest.mark.parametrize('typed', [False, True], ids=['spoken', 'typed'])
def test_a_human_talking_holds_the_idle_sleep_off(typed: bool) -> None:
    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0.3 / 60)).start()
        for _ in range(6):  # 0.6 s of someone talking
            if typed:
                rig.session.say('et puis')
            else:
                rig.client.live.push(**heard('et puis'))
            await asyncio.sleep(0.1)
        assert not rig.task.done()
        assert await asyncio.wait_for(rig.task, 2.0) == 'idle'

    run(scenario)


def test_the_robots_own_audio_holds_the_idle_sleep_off() -> None:
    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0.6 / 60)).start()
        rig.client.live.push(**speech(16800))  # 0.7 s of the robot talking: longer than the idle delay
        await until(lambda: rig.session.state is S.SPEAKING)
        await asyncio.sleep(rig.session._playback_end + 0.3 - time.monotonic())  # played out 0.3 s ago
        assert not rig.task.done()  # the delay counts from the end of its audio
        assert await asyncio.wait_for(rig.task, 2.0) == 'idle'

    run(scenario)


def test_the_idle_clock_never_runs_back_to_an_event_during_the_robots_speech() -> None:
    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0.4 / 60)).start()
        live = rig.client.live
        live.push(**heard('Raconte-moi une histoire.'))
        live.push(**speech(19200, said='Il était une fois…'))  # 0.8 s queued at once
        live.push(**call('do_gesture', 'g', name='nod'))  # a quick tool while it plays
        await until(lambda: live.responses)
        await asyncio.sleep(rig.session._playback_end + 0.2 - time.monotonic())
        assert not rig.task.done()  # 0.2 s after its last word: the delay counts from there
        assert await asyncio.wait_for(rig.task, 2.0) == 'idle'

    run(scenario)


def test_a_user_who_starts_talking_holds_the_idle_sleep_off() -> None:
    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0.5 / 60)).start()
        started = time.monotonic()
        await asyncio.sleep(0.35)
        rig.client.live.push(**vad('ACTIVITY_START'))  # no transcription yet
        await asyncio.sleep(started + 0.6 - time.monotonic())  # past the delay counted from the wake
        assert not rig.task.done()
        assert await asyncio.wait_for(rig.task, 2.0) == 'idle'

    run(scenario)


def test_a_tool_still_running_holds_the_idle_sleep_off() -> None:
    mac = FakeMac(answers={'/api/spotify/play': {'track': {'title': 'Get Lucky', 'artist': 'Daft Punk'}}}, delay=0.8)

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0.3 / 60), mac=mac).start()
        rig.client.live.push(**heard('Mets ma musique.'))
        rig.client.live.push(**call('play_music', 'c1', kind='liked'))
        await asyncio.sleep(0.5)  # the Mac is still at it
        assert not rig.task.done()
        await until(lambda: rig.client.live.responses)
        assert await asyncio.wait_for(rig.task, 2.0) == 'idle'

    run(scenario)


def test_an_idle_sleep_of_zero_never_sleeps() -> None:
    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        await asyncio.sleep(0.5)
        assert not rig.task.done()
        await rig.stop()

    run(scenario)


def _voice(live: FakeLive) -> str:
    return live.config.speech_config.voice_config.prebuilt_voice_config.voice_name


@pytest.mark.parametrize('voice', ['Achird', 'Kore'], ids=['unchanged', 'changed'])
def test_a_connection_on_the_last_wakes_settings_serves_only_once_this_wakes_are_in(monkeypatch, voice: str) -> None:
    """Opened on the last wake's settings (`settled` False): nobody is heard on it before `adopt`, and it is
    reopened, once and without its handle, if what it bakes in changed."""
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (5.0, 5.0, 5.0))  # the reopen waits no backoff

    async def scenario() -> None:
        rig = Rig(audio=FakeAudio(_tone), settled=False)
        rig.task = asyncio.create_task(rig.session.run())
        await until(lambda: rig.client.connections)
        first = rig.client.live
        first.push(**heard('Bonjour !'))  # the server has something to say before this wake's settings are in
        rig.session.say('Tu es là ?')
        await asyncio.sleep(0.2)
        assert rig.session.state is S.CONNECTING and first.receives == 0 and first.audio == [] and not first.turns
        rig.session.adopt(settings_view(voice=voice))
        await until(lambda: rig.session.listening.is_set() and rig.client.live.turns)
        reopened = ['Achird'] if voice == 'Achird' else ['Achird', voice]
        assert [_voice(live) for live in rig.client.connections] == reopened
        assert rig.phases()[:2] == ['connecting', 'idle']
        if voice == 'Kore':
            assert first.closed and first.receives == 0 and not first.turns  # never served
        await rig.stop()

    run(scenario)


def test_settings_adopted_during_the_connect_never_serve_the_stale_setup(monkeypatch) -> None:
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (5.0, 5.0, 5.0))

    async def scenario() -> None:
        rig = Rig(settled=False)

        def first_setup(live: FakeLive) -> None:
            if len(rig.client.connections) == 1:  # the settings fetch answers while the socket opens
                rig.session.adopt(settings_view(voice='Kore'))

        rig.client.on_open = first_setup
        rig.task = asyncio.create_task(rig.session.run())
        await until(lambda: rig.session.state is S.IDLE)
        assert [_voice(live) for live in rig.client.connections] == ['Achird', 'Kore']
        assert rig.client.connections[0].closed and rig.client.connections[0].receives == 0
        assert rig.phases() == ['connecting', 'idle']  # the stale socket was never served
        await rig.stop()

    run(scenario)


def _tone() -> np.ndarray:
    return (0.5 * np.sin(np.arange(256) * 0.3)).astype(np.float32)  # 16 ms at 16 kHz, RMS 0.35


def _rms(samples: np.ndarray | bytes) -> float:
    if isinstance(samples, bytes):
        samples = np.frombuffer(samples, dtype=np.int16) / 32768.0
    return float(np.sqrt(np.mean(np.asarray(samples, dtype=np.float64) ** 2)))


def _stand_in_decisions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[bool, float]]:
    """Every chunk the session hands the comfort noise: (robot_audible, RMS of what goes to Gemini)."""
    decisions: list[tuple[bool, float]] = []
    process = ComfortNoise.process

    def recording(self: ComfortNoise, chunk: np.ndarray, *, robot_audible: bool) -> np.ndarray:
        sent = process(self, chunk, robot_audible=robot_audible)
        decisions.append((robot_audible, _rms(sent)))
        return sent

    monkeypatch.setattr(ComfortNoise, 'process', recording)
    return decisions


def _stretches(decisions: list[tuple[bool, float]]) -> list[bool]:
    """The robot_audible flags with repeats collapsed: [False, True, False] is one audible stretch."""
    return [flag for flag, _ in itertools.groupby(audible for audible, _ in decisions)]


@pytest.mark.parametrize(('mute', 'pull_s'), [(True, 0.016), (False, 0.016), (True, 0.065)])
def test_while_the_robot_is_audible_gemini_hears_the_comfort_noise(
    monkeypatch: pytest.MonkeyPatch, mute: bool, pull_s: float
) -> None:
    """Judged on what reaches the socket, by the time each chunk was sent: while the robot's audio plays, then
    for SELF_NOISE_TAIL_S, the stand-in; the microphone (RMS 0.35) before and after. Each 16 ms chunk comes
    `pull_s` after the last: 0.065 is a starved scheduler."""
    tail = 0.3
    monkeypatch.setattr(config, 'SELF_NOISE_TAIL_S', tail)

    async def scenario() -> None:
        audio = FakeAudio(_tone)
        audio.pull_s = pull_s
        rig = await Rig(audio=audio, mic_mute=mute).start()
        sent = rig.client.live.audio
        # A 50 ms fade spans 4 chunks of 16 ms however late they come, and the chunk read just before the robot
        # starts or stops being audible may be sent just after: the first 5 chunks sent after each change are left out.
        fade = 5
        await until(lambda: len(sent) >= 10)
        rig.client.live.push(**speech(4800))  # 0.2 s
        await until(lambda: rig.session.state is S.SPEAKING)
        end = rig.session._playback_end
        start = end - 0.2  # on the session's own clock: when the robot became audible
        await until(lambda: sum(at >= end + tail for at, _ in sent) >= fade + 10)
        await rig.stop()

        def levels(since: float, before: float) -> list[tuple[float, float]]:
            return [(at, _rms(pcm)) for at, pcm in sent if since <= at < before]

        mic = [level for _, level in levels(-math.inf, start) + levels(end + tail, math.inf)[fade:]]
        audible = levels(start, end + tail - 0.01)[fade:]
        heard, tail_only = [level for _, level in audible], [level for at, level in audible if at >= end]
        assert len(mic) >= 20 and min(mic) > 0.3
        if not mute:  # switched off: the microphone, whatever the robot does
            assert heard and min(heard) > 0.3
            return
        # The stand-in: the room's level, capped at -40 dBFS (a 16 ms chunk of it can peak to twice that).
        assert heard and max(heard) < 0.05
        assert tail_only and max(tail_only) < 0.05  # the tail after the robot's audio included

    run(scenario)


class _Appsink(FakeAudio):
    """A running microphone: a capture thread queues a 16 ms chunk every 16 ms of the clock, the newest 200 kept;
    a pull waits 20 ms at most. Each chunk is filled with `level()` at its capture."""

    def __init__(self, level: Callable[[], float]) -> None:
        super().__init__()
        self.level, self.chunks, self.on = level, queue.Queue(), threading.Event()
        self.on.set()
        threading.Thread(target=self._capture, daemon=True).start()

    def _capture(self) -> None:
        due = time.monotonic()
        while self.on.is_set():
            due += 0.016
            time.sleep(max(0.0, due - time.monotonic()))
            if self.chunks.qsize() >= 200:
                self.chunks.get_nowait()
            self.chunks.put(np.full((256, 2), self.level(), dtype=np.float32))

    def get_audio_sample(self) -> np.ndarray | None:
        try:
            return self.chunks.get(timeout=0.02)
        except queue.Empty:
            return None


def test_the_robots_voice_read_late_from_the_mic_is_still_the_stand_in() -> None:
    """One send held 2.5 s (a congested uplink, a starved pool) while the robot speaks for 1 s: the microphone banks
    its voice and the pump reads it after the robot's tail. Each chunk is judged by when it was captured: the voice
    (0.5) never reaches Gemini past the 50 ms fade, and the human who speaks after the tail (0.2) does."""
    timeline: dict[str, float] = {}

    def level() -> float:
        now = time.monotonic()
        if now < timeline.get('robot_until', 0.0):
            return 0.5
        return 0.2 if now >= timeline.get('human_from', math.inf) else 0.0

    mic = _Appsink(level)

    async def scenario() -> list[float]:
        rig = await Rig(audio=mic, mic_mute=True).start()
        live = rig.client.live
        await asyncio.sleep(0.5)
        sent, send = len(live.audio), live.send_realtime_input

        async def held_once(*, audio: types.Blob) -> None:
            if not timeline.get('held'):
                timeline['held'] = 1.0
                await asyncio.sleep(2.5)
            await send(audio=audio)

        live.send_realtime_input = held_once
        live.push(**heard('Raconte-moi une histoire'))
        live.push(**speech(24000))  # 1 s of the robot's voice
        await until(lambda: rig.session.state is S.SPEAKING)
        timeline['robot_until'] = rig.session.playback_end
        timeline['human_from'] = rig.session.playback_end + config.SELF_NOISE_TAIL_S + 0.2
        await asyncio.sleep(4.0)
        mic.on.clear()
        await rig.stop()
        return [np.abs(np.frombuffer(pcm, dtype=np.int16)).max() / 32768 for _, pcm in live.audio[sent:]]

    peaks = asyncio.run(asyncio.wait_for(scenario(), 15.0))
    voice, human = sum(p > 0.3 for p in peaks), sum(0.15 < p < 0.3 for p in peaks)
    assert voice <= 3, f'{voice} chunks of the robot own voice reached Gemini'
    assert human >= 60, f'only {human} chunks of the human were sent'


def test_a_scene_sends_the_comfort_noise_even_with_the_mic_mute_switch_off() -> None:
    """MIC_MUTE_WHILE_TALKING=0 trades barge-in back, scenes excepted: for a whole scene, the stand-in."""

    async def scenario() -> None:
        rig = await Rig(audio=FakeAudio(_tone), mic_mute=False, performing=lambda: True).start()
        sent = rig.client.live.audio
        await until(lambda: len(sent) >= 30)
        await rig.stop()
        levels = [_rms(pcm) for _, pcm in sent[10:]]  # past the first fade
        assert max(levels) < 0.05  # never the microphone (RMS 0.35)

    run(scenario)


@pytest.mark.parametrize(
    ('value', 'on'),
    [('0', False), ('false', False), ('No', False), (' OFF ', False), ('1', True), ('true', True), ('', True)],
)
def test_the_mic_mute_switch_is_off_in_any_spelling(monkeypatch: pytest.MonkeyPatch, value: str, on: bool) -> None:
    monkeypatch.setenv('MIC_MUTE_WHILE_TALKING', value)
    assert config._flag('MIC_MUTE_WHILE_TALKING', default=True) is on


def test_mic_mute_while_talking_0_reaches_the_session() -> None:
    """Read at import, and the robot never passes mic_mute: the session's default must be the switch."""
    code = (
        f'import inspect, sys; sys.path.insert(0, {str(ROOT)!r}); from robot_agent import config, session; '
        'print(config.MIC_MUTE_WHILE_TALKING, inspect.signature(session.Session).parameters["mic_mute"].default)'
    )
    env = {**os.environ, 'MIC_MUTE_WHILE_TALKING': '0'}
    done = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, check=True, timeout=60)
    assert done.stdout.split() == ['False', 'False']


def test_the_suite_ignores_a_mic_mute_switch_left_in_the_shell() -> None:
    """A shell that exported MIC_MUTE_WHILE_TALKING=0 for a hardware test: the tests still run on the default, the
    stand-in while the robot moves (a session the robot opens takes the switch, read at import)."""
    test = 'tests/robot/test_main.py::test_a_wake_asked_for_during_the_last_sleep_move_still_masks_its_own_move'
    env = {**os.environ, 'MIC_MUTE_WHILE_TALKING': '0'}
    command = [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', test]
    done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=120)
    assert done.returncode == 0, done.stdout[-1500:]


def test_each_connection_drains_the_mic_backlog_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    drains: list[int] = []  # the thread each drain ran on
    drain = audio_module.drain_mic

    def spy(read) -> int:
        drains.append(threading.get_ident())
        return drain(read)

    monkeypatch.setattr(audio_module, 'drain_mic', spy)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        rig = await Rig(audio=FakeAudio(_tone)).start()
        await until(lambda: len(rig.client.live.audio) >= 3)
        rig.client.live.inbox.put_nowait(ConnectionError('dropped'))
        await until(lambda: len(rig.client.connections) == 2 and len(rig.client.live.audio) >= 3)
        await rig.stop()
        assert len(drains) == 2 and loop_thread not in drains  # at each connect, never on the event loop

    run(scenario)


def test_the_robots_own_moves_bring_the_stand_in_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """From the wake move's first chunk: a stream opened on an audible robot starts on the stand-in, no fade from
    the microphone (RMS 0.35); then a gesture, an emotion or a body turn brings it back."""
    monkeypatch.setattr(config, 'WAKE_S', 0.4)
    monkeypatch.setattr(config, 'SELF_NOISE_TAIL_S', 0.05)
    decisions = _stand_in_decisions(monkeypatch)

    def quiet_again() -> bool:
        return [flag for flag, _ in decisions[-5:]] == [False] * 5

    async def scenario() -> None:
        rig = await Rig(audio=FakeAudio(_tone), woke_at=time.monotonic() - 2.0, moved_at=time.monotonic()).start()
        await until(quiet_again)
        assert _stretches(decisions) == [True, False]  # the stream opened while the head was still waking up
        assert max(level for audible, level in decisions if audible) < 0.05  # the first chunk included
        rig.body.moving_end = time.monotonic() + 0.2  # a gesture, an emotion or a body turn under way
        await until(lambda: _stretches(decisions) == [True, False, True, False] and quiet_again())
        await rig.stop()

    run(scenario)


def test_the_mic_is_never_read_on_the_event_loop() -> None:
    async def scenario() -> None:
        rig = await Rig(audio=FakeAudio(_tone)).start()
        await until(lambda: len(rig.client.live.audio) >= 5)
        await rig.stop()
        assert rig.audio.pull_threads and threading.get_ident() not in rig.audio.pull_threads

    run(scenario)


def test_a_failing_mic_pull_is_survived() -> None:
    pulls = iter(range(1000))

    def flaky() -> np.ndarray:
        if next(pulls) % 3 == 0:
            raise OSError('appsink hiccup')
        return _tone()

    async def scenario() -> None:
        rig = await Rig(audio=FakeAudio(flaky)).start()
        await until(lambda: len(rig.client.live.audio) >= 5)
        await rig.stop()

    run(scenario)


def test_the_speaker_never_gets_an_empty_array() -> None:
    async def scenario() -> None:
        rig = await Rig(audio=FakeAudio(speaker_rate=16000)).start()
        for _ in range(30):
            rig.client.live.push(**speech(1))  # one sample at 24 kHz: two thirds of a sample at 16 kHz
        await until(lambda: 'speaking' in rig.phases())
        assert rig.audio.empty_pushes == 0
        assert 15 <= sum(chunk.size for chunk in rig.audio.pushed) <= 20
        await rig.stop()

    run(scenario)


def test_a_reply_with_nobody_speaking_is_logged_as_a_phantom(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        rig = await Rig().start()
        rig.client.live.push(**speech())
        await until(lambda: rig.session.state is S.SPEAKING)
        await rig.stop()

    with caplog.at_level(logging.WARNING):
        run(scenario)
    assert '[PHANTOM] the robot answers' in caplog.text


def test_usage_is_split_by_modality() -> None:
    usage = types.UsageMetadata(
        prompt_token_count=150,
        response_token_count=30,
        total_token_count=192,
        thoughts_token_count=5,
        tool_use_prompt_token_count=7,
        prompt_tokens_details=[
            types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=100),
            types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=40),
            types.ModalityTokenCount(modality=types.MediaModality.IMAGE, token_count=10),
        ],
        response_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=30)],
    )

    async def scenario() -> None:
        rig = await Rig().start()
        rig.client.live.push(usage_metadata=usage)
        await until(lambda: rig.kinds('usage'))
        event = rig.kinds('usage')[0]
        assert event['prompt'] == {'text': 107, 'audio': 40, 'image': 10}
        assert event['response'] == {'text': 5, 'audio': 30, 'image': 0}
        assert event['total_tokens'] == 192
        await rig.stop()

    run(scenario)


def test_every_event_matches_the_contracts() -> None:
    mac = FakeMac(answers={'/api/spotify/play': {'track': {'title': 'Get Lucky', 'artist': 'Daft Punk'}}})
    adapter = TypeAdapter(contracts.RobotEvent)

    async def scenario() -> None:
        rig = await Rig(mac=mac).start()
        live = rig.client.live
        live.push(**vad('ACTIVITY_START'))
        live.push(**heard('Mets ma musique'))
        live.push(**vad('ACTIVITY_END'))
        live.push(**call('play_music', 'c1', kind='liked'))
        live.push(**call('look', 'c2'))
        live.push(**call('turn_body', 'c3', angle=None))
        live.push(**speech(said="C'est parti !"))
        live.push(**content(turn_complete=True), usage_metadata=types.UsageMetadata(total_token_count=10))
        await until(lambda: len(live.responses) == 3 and rig.kinds('usage') and rig.kinds('robot_end'))
        rig.session.say('Merci !')
        await rig.stop('goto_sleep')
        kinds = {event['kind'] for event in rig.events}
        # session_start and session_end are the robot's main loop's to send (replay too), scene its scene player's,
        # manette and hush their own
        others = {'session_start', 'session_end', 'scene', 'manette', 'replay', 'hush'}
        assert kinds == {e['kind'] for e in contract_examples.examples()['RobotEvent']} - others
        for event in rig.events:
            adapter.validate_python(event)
        latency = rig.kinds('latency')[0]
        assert latency['turn_id'] == rig.kinds('robot_end')[0]['turn_id'] and latency['ms'] >= 0
        assert rig.kinds('user_end')[0]['text'] == 'Mets ma musique'

    run(scenario)


def test_a_gesture_that_was_the_whole_answer_asks_the_model_to_speak() -> None:
    """Gemini 3.8 Live can answer « comment vas-tu ? » with a nod alone; a SILENT result left it mute."""

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**heard('Comment vas-tu ?'))
        live.push(**call('do_gesture', 'g1', name='nod'))  # the whole answer: no audio
        await until(lambda: len(live.responses) == 1)
        live.push(**heard('Et là ?'))
        live.push(**speech(said='Très bien.'))
        live.push(**call('do_gesture', 'g2', name='nod'))  # this one accompanies words
        await until(lambda: len(live.responses) == 2)
        by_id = {r.id: r.scheduling for r in live.responses}
        assert by_id == {'g1': WHEN_IDLE, 'g2': SILENT}
        await rig.stop()

    run(scenario)


def test_a_failed_move_asked_twice_is_an_error_twice() -> None:
    """The model sends the same call twice for one ask, and it cannot play: it is told so both times, never
    that the second one was « déjà fait »."""

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**heard('Tourne-toi à gauche.'))
        live.push(**call('turn_body', 'c1', angle='gauche'))
        live.push(**call('turn_body', 'c2', angle='gauche'))
        await until(lambda: len(live.responses) == 2)
        told = [(r.id, r.response['status'], r.scheduling) for r in live.responses]
        assert told == [('c1', 'error', WHEN_IDLE), ('c2', 'error', WHEN_IDLE)]
        assert [e['status'] for e in rig.kinds('tool_result')] == ['error', 'error'] and rig.body.turns == []
        await rig.stop()

    run(scenario)


def test_a_say_typed_during_a_long_reconnect_is_sent_on_the_new_socket_and_authorises_its_tools(monkeypatch) -> None:
    """The socket is lost and two connects fail: the way back is longer than LIVE_WAIT_S and the grace window.
    The typed turn, shown on the dashboard at once, still reaches the model, and the tool it asks for runs."""
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (0.2, 0.2, 0.2))  # 0.6 s back, LIVE_WAIT_S is 0.3 s
    monkeypatch.setattr(config, 'HUMAN_GRACE_S', 0.4)

    async def scenario() -> None:
        rig = await Rig().start()
        rig.client.fail = [OSError('network down'), OSError('network down')]
        rig.client.live.inbox.put_nowait(ConnectionError('socket lost'))
        await until(lambda: rig.session.state is S.RECONNECTING)
        rig.session.say('Fais oui de la tête.')
        assert [(e['text'], e['typed']) for e in rig.kinds('user_end')] == [('Fais oui de la tête.', True)]
        await until(lambda: len(rig.client.connections) == 2 and rig.client.live.turns)
        live = rig.client.live
        assert [turn[0].parts[0].text for turn in live.turns] == ['Fais oui de la tête.']
        live.push(**call('do_gesture', 'c1', name='nod'))
        await until(lambda: live.responses)
        assert live.responses[0].response['status'] == 'ok' and rig.body.gestures == ['nod']
        await rig.stop()

    run(scenario)


def test_says_typed_during_a_reconnect_reach_the_model_in_the_order_typed(monkeypatch) -> None:
    """Two says typed 30 ms apart while the socket is down; it is back 30 ms after the second, between the
    second's next look for it and the first's. They reach the model in the order typed, the dashboard's."""
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (0.0, 0.0, 0.0))

    async def scenario() -> None:
        rig = await Rig().start()
        back, connect = asyncio.Event(), rig.client.aio.live.connect

        @contextlib.asynccontextmanager
        async def held(**kwargs: Any):
            await back.wait()
            async with connect(**kwargs) as connection:
                yield connection

        rig.client.aio.live.connect = held
        rig.client.live.inbox.put_nowait(ConnectionError('socket lost'))
        await until(lambda: rig.session.state is S.RECONNECTING)
        rig.session.say('Mets de la musique.')
        await asyncio.sleep(0.03)
        rig.session.say('Du jazz.')
        await asyncio.sleep(0.03)
        back.set()
        await until(lambda: len(rig.client.connections) == 2 and len(rig.client.live.turns) == 2)
        assert [turn[0].parts[0].text for turn in rig.client.live.turns] == ['Mets de la musique.', 'Du jazz.']
        await rig.stop()

    run(scenario)


def test_a_note_never_goes_out_behind_a_say_that_waited_for_the_socket(monkeypatch) -> None:
    """A note and a say both wait out a reconnect; the note looks for its quiet point far more often than the say for
    its socket. The say goes first and alone: the model answers the human, and a call it makes then runs. The note
    waits for the next quiet point, so it is never taken for the human's turn, nor the human's for it."""
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (0.0, 0.0, 0.0))
    monkeypatch.setattr(notes, 'POLL_S', 0.001)

    async def scenario() -> None:
        rig = await Rig(settings_view(idle_sleep_minutes=0)).start()
        back, connect = asyncio.Event(), rig.client.aio.live.connect

        @contextlib.asynccontextmanager
        async def held(**kwargs: Any):
            await back.wait()
            async with connect(**kwargs) as connection:
                yield connection

        rig.client.aio.live.connect = held
        rig.client.live.inbox.put_nowait(ConnectionError('socket lost'))
        await until(lambda: rig.session.state is S.RECONNECTING)
        rig.session.notify("L'app « Snake » est prête.")
        rig.session.say('Ouvre la musique.')
        await asyncio.sleep(0.03)
        back.set()
        await until(lambda: len(rig.client.connections) == 2 and rig.client.live.turns)
        live = rig.client.live
        await asyncio.sleep(0.05)
        assert [turn[0].parts[0].text for turn in live.turns] == ['Ouvre la musique.']
        live.push(**call('do_gesture', 'g1', name='nod'))
        await until(lambda: live.responses)
        assert live.responses[0].response['status'] == 'ok'
        live.push(**speech(2400, said="C'est parti."))
        live.push(**content(turn_complete=True))
        await until(lambda: len(live.turns) == 2)  # the answer is over: the note's quiet point
        assert 'Snake' in live.turns[1][0].parts[0].text
        await rig.stop()

    run(scenario)


def test_turned_left_back_and_left_again_the_robot_ends_left() -> None:
    """Three asks in one breath: the third is no repeat of the second, the robot turns back to the left."""

    async def scenario() -> None:
        rig = await Rig().start()
        live = rig.client.live
        live.push(**heard('Regarde à gauche, reviens, puis encore à gauche.'))
        for call_id, angle in (('c1', 100), ('c2', 0), ('c3', 100)):
            live.push(**call('turn_body', call_id, angle=angle))
        await until(lambda: len(live.responses) == 3)
        assert [r.response['message'] for r in live.responses][-1] == 'Rotation vers 100° lancée.'
        assert rig.body.turns == [100.0, 0.0, 100.0]
        await rig.stop()

    run(scenario)
