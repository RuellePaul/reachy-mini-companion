"""The catalogue and the handlers, with fakes for the body, the camera and the Mac."""

from __future__ import annotations

import asyncio
import re
import time
from typing import get_args

import pytest
from google.genai import types

from mac_server.contracts import MusicAction, PlayKind
from robot_agent import config
from robot_agent.emotions import CURATED, DANCES, DATASET, Emotions
from robot_agent.tools import SILENT, WHEN_IDLE, ToolResult, Tools, declarations
from tests.robot.conftest import FakeAudio, FakeBody, FakeCamera, FakeEmotions, FakeMac


def _tools(**fakes) -> Tools:
    return Tools(
        body=fakes.get('body', FakeBody()),
        audio=fakes.get('audio', FakeAudio()),
        camera=fakes.get('camera', FakeCamera()),
        mac=fakes.get('mac', FakeMac()),
        emotions=fakes.get('emotions', FakeEmotions()),
    )


def _run(tools: Tools, tool: str, /, **args) -> ToolResult:
    sent: list[bytes] = []

    async def send_image(jpeg: bytes) -> None:
        sent.append(jpeg)

    return asyncio.run(tools.run(tool, args, send_image))


def test_the_catalogue_is_non_blocking_and_conditional() -> None:
    base = {d.name for d in declarations(emotions=[], camera=False)}
    assert base == {'do_gesture', 'turn_body', 'set_volume', 'goto_sleep', 'play_music', 'music'}
    full = declarations(emotions=list(CURATED), camera=True)
    assert {d.name for d in full} == base | {'play_emotion', 'look'}
    assert all(d.behavior is types.Behavior.NON_BLOCKING for d in full)
    emotion = next(d for d in full if d.name == 'play_emotion')
    assert emotion.parameters.properties['name'].enum == list(CURATED) and len(CURATED) == 25
    play = next(d for d in full if d.name == 'play_music')
    assert play.parameters.required == [] and play.parameters.properties['kind'].enum == list(get_args(PlayKind))
    music = next(d for d in full if d.name == 'music')
    assert music.parameters.required == ['action']
    assert music.parameters.properties['action'].enum == list(get_args(MusicAction))


def test_scheduling_silent_for_physical_successes_and_refusals_when_idle_for_the_rest() -> None:
    assert ToolResult('ok', '').response('c', 'turn_body').scheduling is SILENT
    assert ToolResult('ok', '').response('c', 'look').scheduling is SILENT
    assert ToolResult('ok', '').response('c', 'turn_body', spoke=False).scheduling is WHEN_IDLE
    assert ToolResult('ok', '').response('c', 'look', spoke=False).scheduling is SILENT
    assert ToolResult('refused', '').response('c', 'do_gesture', spoke=False).scheduling is SILENT
    assert ToolResult('ok', '').response('c', 'play_music').scheduling is WHEN_IDLE
    assert ToolResult('ok', '', {'volume': 20}).response('c', 'music').scheduling is WHEN_IDLE  # Spotify's, said
    assert ToolResult('refused', '').response('c', 'music').scheduling is SILENT
    assert ToolResult('ok', '', silent=True).response('c', 'play_scene', spoke=False).scheduling is SILENT
    assert ToolResult('error', '', silent=True).response('c', 'play_scene').scheduling is WHEN_IDLE  # a failure is said
    assert ToolResult('error', '').response('c', 'turn_body').scheduling is WHEN_IDLE
    assert ToolResult('refused', '').response('c', 'play_music').scheduling is SILENT
    response = ToolResult('ok', 'fait', {'volume': 3}).response('c', 'set_volume')
    assert response.response == {'status': 'ok', 'message': 'fait'} and response.will_continue is None


def test_music_results_carry_the_track_as_a_field() -> None:
    mac = FakeMac(answers={'/api/spotify/play': {'track': {'title': 'Around the World', 'artist': 'Daft Punk'}}})
    result = _run(_tools(mac=mac), 'play_music', query='Daft Punk', kind='artist')
    assert (result.status, result.message) == ('ok', 'Lecture lancée\xa0: Around the World, de Daft Punk.')
    assert result.data == {'track': {'title': 'Around the World', 'artist': 'Daft Punk', 'uri': None}}
    assert mac.calls == [('POST', '/api/spotify/play', {'query': 'Daft Punk', 'kind': 'artist'})]
    _run(_tools(mac=mac), 'play_music', query='calme', kind='ambiance')
    assert mac.calls[-1][2]['kind'] == 'track'


def test_play_music_names_an_artist_only_when_there_is_one() -> None:
    artist = {'track': {'title': 'Daft Punk', 'artist': '', 'uri': 'spotify:artist:4tZ'}}  # the Mac's, for an artist
    result = _run(
        _tools(mac=FakeMac(answers={'/api/spotify/play': artist})), 'play_music', query='Daft Punk', kind='artist'
    )
    assert (result.status, result.message) == ('ok', 'Lecture lancée\xa0: Daft Punk.')


@pytest.mark.parametrize(
    ('args', 'sent'),
    [
        ({}, {'query': '', 'kind': 'liked'}),  # « mets ma musique »: nothing to search, the liked tracks
        ({'query': '  ', 'kind': 'liked'}, {'query': '', 'kind': 'liked'}),
        ({'query': 'Le Petit Prince', 'kind': 'audiobook'}, {'query': 'Le Petit Prince', 'kind': 'audiobook'}),
        ({'query': 'Journal de 8 h', 'kind': 'podcast'}, {'query': 'Journal de 8 h', 'kind': 'podcast'}),
    ],
)
def test_play_music_starts_every_kind(args: dict, sent: dict) -> None:
    mac = FakeMac(answers={'/api/spotify/play': {'track': {'title': 'T', 'artist': 'A', 'uri': 'spotify:x:1'}}})
    result = _run(_tools(mac=mac), 'play_music', **args)
    assert mac.calls == [('POST', '/api/spotify/play', sent)]
    assert result.status == 'ok' and result.data == {'track': {'title': 'T', 'artist': 'A', 'uri': 'spotify:x:1'}}
    assert result.message.startswith('Lecture de tes titres likés' if sent['kind'] == 'liked' else 'Lecture lancée')


def test_play_music_needs_something_to_search_but_for_the_liked_tracks() -> None:
    mac = FakeMac()
    result = _run(_tools(mac=mac), 'play_music', query=' ', kind='album')
    assert result.status == 'error' and mac.calls == []


@pytest.mark.parametrize(
    ('args', 'body', 'answer', 'data'),
    [
        ({'action': 'pause'}, {'action': 'pause', 'value': '', 'amount': None}, {'message': 'Lecture en pause.'}, {}),
        (
            {'action': 'volume', 'amount': '30'},
            {'action': 'volume', 'value': '', 'amount': 30.0},
            {'message': 'Volume de Spotify réglé à 30 sur 100.', 'volume': 30},
            {'volume': 30},
        ),
        (
            {'action': 'now'},
            {'action': 'now', 'value': '', 'amount': None},
            {'message': 'En cours : Get Lucky, de Daft Punk.', 'track': {'title': 'Get Lucky', 'artist': 'Daft Punk'}},
            {'track': {'title': 'Get Lucky', 'artist': 'Daft Punk', 'uri': None}},
        ),
        (
            {'action': 'device', 'value': ' salon ', 'amount': 'beaucoup'},
            {'action': 'device', 'value': 'salon', 'amount': None},
            {'message': 'Lecture passée sur Salon.', 'device': 'Salon', 'volume': None, 'track': None},
            {},
        ),
    ],
)
def test_music_sends_the_action_and_says_the_macs_answer(args: dict, body: dict, answer: dict, data: dict) -> None:
    mac = FakeMac(answers={'/api/spotify/control': answer})
    result = _run(_tools(mac=mac), 'music', **args)
    assert mac.calls == [('POST', '/api/spotify/control', body)]
    assert (result.status, result.message, result.data) == ('ok', answer['message'], data)
    assert result.response('c', 'music').scheduling is WHEN_IDLE


def test_music_refuses_an_unknown_action_without_asking_the_mac() -> None:
    mac = FakeMac()
    result = _run(_tools(mac=mac), 'music', action='rewind')
    assert result.status == 'error' and mac.calls == []


def test_a_mac_failure_is_said_in_the_macs_words() -> None:
    result = _run(_tools(mac=FakeMac(error="Aucun appareil Spotify n'est disponible.")), 'music', action='pause')
    assert (result.status, result.message) == ('error', "Aucun appareil Spotify n'est disponible.")


def test_volume_is_clamped_and_reported() -> None:
    mac = FakeMac(answers={'/api/volume': {'volume': 100}})
    result = _run(_tools(mac=mac), 'set_volume', level=180)
    assert mac.calls == [('PUT', '/api/volume', {'volume': 100})]
    assert result.data == {'volume': 100}
    assert _run(_tools(), 'set_volume', level='fort').status == 'error'


@pytest.mark.parametrize(
    ('angle', 'asked', 'message'),
    [
        (45, 45.0, 'Rotation vers 45° lancée.'),
        ('-30', -30.0, 'Rotation vers -30° lancée.'),
        (170, 170.0, 'Rotation vers 100° lancée (limitée à 100° de chaque côté).'),
    ],
)
def test_turn_body_tells_the_model_where_the_body_goes(angle, asked: float, message: str) -> None:
    body = FakeBody()
    result = _run(_tools(body=body), 'turn_body', angle=angle)
    assert (result.status, result.message, body.turns) == ('ok', message, [asked])


def test_look_without_a_picture_says_so() -> None:
    assert _run(_tools(camera=FakeCamera(jpeg=None)), 'look').status == 'error'
    assert _run(_tools(camera=None), 'look').status == 'error'


def test_a_photo_is_claimed_only_once_it_reached_the_model() -> None:
    class FirstPullEmpty:
        reads = 0

        def read_jpeg(self) -> bytes | None:
            self.reads += 1
            return None if self.reads == 1 else b'\xff\xd8jpeg'

    sent: list[bytes] = []

    async def send(jpeg: bytes) -> None:
        sent.append(jpeg)

    async def no_connection(_jpeg: bytes) -> None:
        raise ConnectionError('no Live connection')

    async def scenario() -> None:
        tools = _tools(camera=FirstPullEmpty())
        # The same ask as two calls, and the first finds no frame: the second waits for it, then takes its own.
        first, second = await asyncio.gather(tools.run('look', {}, send), tools.run('look', {}, send))
        assert (first.status, second.status) == ('error', 'ok') and second.data['image_base64'] and sent
        tools = _tools()
        with pytest.raises(ConnectionError):
            await tools.run('look', {}, no_connection)
        retry = await tools.run('look', {}, send)  # within LOOK_REPEAT_S, yet nothing was ever sent: a new photo
        assert retry.status == 'ok' and retry.data['image_base64']
        duplicate = await tools.run('look', {}, send)
        assert (duplicate.status, duplicate.data) == ('ok', {}) and 'déjà prise' in duplicate.message

    asyncio.run(scenario())


def test_an_emotion_that_is_not_offered_is_an_error() -> None:
    body = FakeBody()
    assert _run(_tools(body=body, emotions=FakeEmotions(())), 'play_emotion', name='cheerful1').status == 'error'
    assert body.moves == []


def test_an_unknown_tool_is_an_error() -> None:
    assert _run(_tools(), 'fly').status == 'error'


# What only the model is told to do: the transcript shows a failed call's message to the owner, never this.
FOR_THE_MODEL = re.compile(r"Dis-le|dis-le|N'en parle|n'invente|Choisis|[Pp]récise|^Tu |\btu\b")


@pytest.mark.parametrize(
    ('tool', 'args', 'fakes', 'lent'),
    [
        ('do_gesture', {'name': 'moonwalk'}, {}, None),
        ('look', {}, {'camera': None}, None),
        ('look', {}, {'camera': FakeCamera(jpeg=None)}, None),
        ('play_music', {'query': ' ', 'kind': 'album'}, {}, None),
        ('do_gesture', {'name': 'nod'}, {}, 'volant'),
    ],
    ids=['unknown-gesture', 'no-camera', 'no-image', 'nothing-to-search', 'lent'],
)
def test_a_failed_call_says_what_happened_and_only_the_model_is_told_what_to_do(
    tool: str, args: dict, fakes: dict, lent: str | None
) -> None:
    tools = Tools(
        body=FakeBody(), audio=FakeAudio(), camera=fakes.get('camera', FakeCamera()), mac=FakeMac(),
        emotions=FakeEmotions(), lent=lambda: lent,
    )  # fmt: skip
    result = _run(tools, tool, **args)
    told = result.response('c', tool).response['message']
    assert result.status == 'error' and result.message and not FOR_THE_MODEL.search(result.message)
    assert told.startswith(result.message) and FOR_THE_MODEL.search(told)  # the model still gets its instruction


def test_the_emotion_library_loads_in_the_background_and_offers_only_the_curated_names(monkeypatch) -> None:
    monkeypatch.setattr(config, 'EMOTIONS_RETRY_S', (0.01, 0.02))
    attempts: list[int] = []

    class Library:
        def __init__(self, tag: str, moves: list[str]) -> None:
            self.tag, self.moves = tag, moves

        def list_moves(self) -> list[str]:
            return self.moves

        def get(self, name: str) -> str:
            return f'{self.tag}:{name}'

    def load(dataset: str) -> Library:
        attempts.append(dataset)
        if attempts.count(dataset) < 3:
            raise OSError('no network yet')  # each library is retried until it loads
        if dataset == DANCES:
            return Library('dance', ['chicken_peck', 'dance1'])  # a scene's dances, loaded after the emotions
        return Library('move', ['cheerful1', 'dance1', 'furious1'])  # furious1 is not offered

    emotions = Emotions(load)
    assert emotions.names() == [] and emotions.get('cheerful1') is None  # nothing until it has loaded
    emotions.start()
    deadline = time.monotonic() + 2.0
    while not emotions.names() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert emotions.names() == ['cheerful1', 'dance1'] and attempts.count(DATASET) == 3
    assert emotions.get('dance1') == 'move:dance1' and emotions.get('furious1') is None
    while emotions.move('chicken_peck') is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert [emotions.move(name) for name in ('furious1', 'chicken_peck', 'dance1', 'sad1')] == [
        'move:furious1',  # a scene plays every emotion and every dance; an emotion wins over a dance of its name
        'dance:chicken_peck',
        'move:dance1',
        None,
    ]


def test_the_same_move_asked_twice_at_once_plays_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """One ask can come as several identical calls (seen on the robot: two tilt_left 0.4 s apart): the robot
    moves once; another move, or the same one later, plays."""
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    body = FakeBody()
    tools = _tools(body=body)
    assert _run(tools, 'do_gesture', name='tilt_left').status == 'ok'
    clock[0] += 0.4
    again = _run(tools, 'do_gesture', name='tilt_left')
    assert again.status == 'ok' and 'Déjà fait' in again.message
    _run(tools, 'do_gesture', name='nod')  # another move is not a repeat
    clock[0] += 2.6
    _run(tools, 'do_gesture', name='tilt_left')  # asked again later: it plays
    assert body.gestures == ['tilt_left', 'nod', 'tilt_left']


@pytest.mark.parametrize(
    ('tool', 'args', 'fakes'),
    [
        ('do_gesture', {'name': 'wave'}, {}),
        ('turn_body', {'angle': 'gauche'}, {}),
        ('play_emotion', {'name': 'cheerful1'}, {'emotions': FakeEmotions(())}),  # the library not loaded yet
    ],
    ids=['unknown gesture', 'angle not a number', 'emotion not loaded'],
)
def test_a_move_that_did_not_play_is_no_repeat(tool: str, args: dict, fakes: dict) -> None:
    """Asked again at once, a move that failed fails again: only a move that played is « déjà fait »."""
    body = FakeBody()
    tools = _tools(body=body, **fakes)
    first, again = _run(tools, tool, **args), _run(tools, tool, **args)
    assert (first.status, again.status) == ('error', 'error') and again.message == first.message
    assert body.gestures == body.turns == body.moves == []


def test_the_same_emotion_asked_twice_in_one_message_plays_once_or_fails_twice() -> None:
    """Two identical calls in one message run side by side: the second waits for the first one's outcome."""

    async def twice(tools: Tools) -> list[ToolResult]:
        args = {'name': 'cheerful1'}
        return await asyncio.gather(
            tools.run('play_emotion', args, _no_image), tools.run('play_emotion', args, _no_image)
        )

    body = FakeBody()
    played = asyncio.run(twice(_tools(body=body)))
    assert [r.status for r in played] == ['ok', 'ok'] and 'Déjà fait' in played[1].message
    assert [move.name for move in body.moves] == ['cheerful1']
    failed = asyncio.run(twice(_tools(body=body, emotions=FakeEmotions(()))))
    assert [r.status for r in failed] == ['error', 'error']


async def _no_image(_jpeg: bytes) -> None:
    raise AssertionError('no picture for a move')


def test_only_the_move_under_way_is_a_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Left, front, left within 2.5 s are three asks: the robot ends where it was last asked. A gesture and an
    emotion share the head's move, the body turn is its own: an ask sent twice as a pair still plays once."""
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    body = FakeBody()
    tools = _tools(body=body)
    for angle in (100, 0, 100):
        assert _run(tools, 'turn_body', angle=angle).message == f'Rotation vers {angle}° lancée.'
        clock[0] += 0.5
    assert body.turns == [100.0, 0.0, 100.0]
    for _ in range(2):  # « regarde à gauche en hochant la tête », sent twice
        _run(tools, 'turn_body', angle=-60)
        _run(tools, 'do_gesture', name='nod')
    assert body.turns[3:] == [-60.0] and body.gestures == ['nod']
    _run(tools, 'play_emotion', name='cheerful1')  # it replaces the nod: a nod now is a new move
    assert 'Déjà fait' not in _run(tools, 'do_gesture', name='nod').message
    assert body.gestures == ['nod', 'nod'] and [move.name for move in body.moves] == ['cheerful1']
