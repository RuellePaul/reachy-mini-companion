"""Across the process boundary, offline: the robot's real Live setup built on the Mac's real prompt, and the
robot's real Mac client against the real Mac app under uvicorn, over localhost HTTP and the dashboard's socket."""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import json
import logging
import queue
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import numpy as np
import pytest
import requests
import uvicorn
from google.genai import types
from PIL import Image
from pydantic import TypeAdapter
from websockets.sync.client import ClientConnection, connect

from mac_server import app_build, contracts, health, pricing, prompts, spotify, studio
from mac_server import apps as mac_apps
from mac_server import catalog as mac_catalog
from mac_server.config import Config
from mac_server.routes import NOT_IN_CONVERSATION, VOLUME_NOT_TAKEN
from mac_server.server import create_app
from robot_agent import apps, config, ear, live, manette, poses, scene, take
from robot_agent.adapters import Camera
from robot_agent.adapters.robot import RobotCamera
from robot_agent.animations import GESTURES, VARIANTS
from robot_agent.emotions import CURATED, Emotions
from robot_agent.mac import Mac, MacError
from robot_agent.state import SessionState
from robot_agent.tools import MUSIC_ACTIONS, PLAY_KINDS, WHEN_IDLE, ToolResult, Tools, declarations
from robot_agent.transcript import Transcript
from scripts import contract_examples
from tests.robot.conftest import FakeAudio, FakeBody, FakeCamera, FakeEmotions, FakeMac

KEY = 'cross-process-key'
SETUP_BUDGET = 10_000  # characters re-billed every turn: system instruction + tool declarations
EVENTS = TypeAdapter(contracts.RobotEvent)
WS_MESSAGES = TypeAdapter(contracts.WsMessage)
HANDSHAKE = {
    'Connection': 'Upgrade',
    'Upgrade': 'websocket',
    'Sec-WebSocket-Version': '13',
    'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',  # RFC 6455's sample nonce, no secret: gitleaks:allow
}
ENGLISH = re.compile(
    r'\b(the|not|bad|error|failed|internal|server|unavailable|unauthorized|forbidden|gateway|required|missing|'
    r'field|timeout)\b',
    re.IGNORECASE,
)
USAGE = types.UsageMetadata(
    prompt_token_count=2308,
    response_token_count=120,
    total_token_count=2428,
    prompt_tokens_details=[
        types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=1800),
        types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=250),
        types.ModalityTokenCount(modality=types.MediaModality.IMAGE, token_count=258),
    ],
    response_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=120)],
)


@pytest.fixture(autouse=True)
def _localhost_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may reach beyond this machine: any other connection fails loudly."""
    real_connect = socket.socket.connect

    def guarded(sock: socket.socket, address: Any) -> None:
        try:
            loopback = ipaddress.ip_address(address[0]).is_loopback
        except (TypeError, ValueError, IndexError):
            loopback = False
        if not loopback:
            raise OSError(f'offline test: no connection to {address!r}')
        real_connect(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)


def robot_extras(mac: Any) -> dict[str, Any]:
    """The subjects' tools, as main.Robot gives them to Tools."""
    performer = scene.Performer(mac, FakeAudio(), FakeBody(), FakeEmotions())
    return {**apps.tools(mac, apps.Builds(mac, notify=lambda _text: None)), **scene.tools(mac, performer, lambda: None)}


# ----- 1. The setup budget, whole ------------------------------------------------------------------


class PollenLibrary:
    """The emotion library as the robot loads it: every curated emotion, and some the robot does not offer."""

    def __init__(self, dataset: str) -> None:
        self.dataset = dataset

    def list_moves(self) -> list[str]:
        return [*CURATED, 'furious1', 'electric1', 'dance3']

    def get(self, name: str) -> str:
        return f'move:{name}'


def _loaded_emotions() -> Emotions:
    emotions = Emotions(PollenLibrary)
    emotions.start()
    _until(emotions.names, timeout=2.0)
    return emotions


def _longest_date() -> date:
    """The day with the longest French date ('mercredi 1er septembre'), over a whole calendar cycle."""
    days = (date(2026, 1, 1) + timedelta(days=n) for n in range(28 * 366))
    return max(days, key=lambda day: len(prompts.french_date(day)))


def test_the_worst_setup_the_product_allows_fits_the_budget() -> None:
    settings = contracts.Settings(
        name='N' * contracts.NAME_MAX, personality='P' * contracts.PERSONALITY_MAX, web_search=True
    )
    view = json.loads(prompts.settings_view(settings, _longest_date()).model_dump_json())  # GET /api/settings
    mac = FakeMac()
    extras = robot_extras(mac)
    tools = Tools(
        body=FakeBody(), audio=FakeAudio(), camera=FakeCamera(), mac=mac, emotions=_loaded_emotions(), extra=extras
    )
    setup = live.build_config(view, tools.declarations(), handle=None, search=True)

    instruction = ''.join(part.text or '' for part in setup.system_instruction.parts)
    declared = json.dumps([tool.model_dump(mode='json', exclude_none=True) for tool in setup.tools], ensure_ascii=False)
    assert settings.name in instruction and settings.personality in instruction  # nothing was cut
    assert instruction.startswith(view['system_prompt']) and instruction.endswith(view['web_prompt'])
    assert len(instruction) + len(declared) <= SETUP_BUDGET, f'{len(instruction)} + {len(declared)} characters'

    functions = [decl for tool in setup.tools for decl in tool.function_declarations or []]
    every_tool = {decl.name for decl in declarations(emotions=[], camera=False)} | {'play_emotion', 'look', *extras}
    assert {decl.name for decl in functions} >= every_tool
    assert any(tool.google_search is not None for tool in setup.tools)
    assert all(decl.behavior is types.Behavior.NON_BLOCKING for decl in functions)
    emotion = next(decl for decl in functions if decl.name == 'play_emotion')
    offered = emotion.parameters.properties['name'].enum
    assert offered == list(CURATED) and len(offered) <= 25


# ----- 2. The wire, robot to Mac, for real -----------------------------------------------------


def _until(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'condition not met in time'
        time.sleep(0.01)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture
def mac_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The real Mac app under uvicorn, configured as __main__ runs it, on a free localhost port."""
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')  # requests goes straight to the server
    mac_config = Config(
        api_key=KEY,
        reachy_host='127.0.0.1',
        reachy_port=_closed_port(),  # no daemon: its probe fails at once
        settings_path=tmp_path / 'settings.json',
        spotify_token_path=tmp_path / 'spotify.json',  # and no Spotify credentials
        apps_dir=tmp_path / 'apps',
    )
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(mac_config),
            log_level='warning',
            proxy_headers=True,
            forwarded_allow_ips='127.0.0.1',
            ws='websockets-sansio',
            timeout_graceful_shutdown=1,
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        _until(lambda: server.started or not thread.is_alive(), timeout=10.0)
        assert server.started, 'uvicorn did not start'
        yield f'http://127.0.0.1:{listener.getsockname()[1]}'
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()


@pytest.fixture
def robot(mac_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Mac]:
    """The robot's real Mac client, with the same key; its threads are gone before the server stops."""
    monkeypatch.setattr(config, 'HEARTBEAT_S', 0.05)
    monkeypatch.setattr(config, 'COMMAND_WAIT_S', 0.2)  # a parked long-poll comes back soon
    before = set(threading.enumerate())
    link = Mac(url=mac_url, api_key=KEY)
    yield link
    link.close()
    for thread in set(threading.enumerate()) - before:
        if thread.name.startswith('mac-'):
            thread.join(5)


class Dashboard:
    """The dashboard's side of the Mac: HTTP with the key (the Vite proxy adds it), and the WebSocket."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.http = requests.Session()
        self.http.headers['X-API-Key'] = KEY

    def call(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        return self.http.request(method, f'{self.url}{path}', timeout=5, **kwargs)

    def state(self) -> dict[str, Any]:
        response = self.call('GET', '/api/state')
        assert response.status_code == 200, response.text
        return contracts.DashboardState.model_validate(response.json()).model_dump(mode='json')

    def socket(self) -> ClientConnection:
        url = f'ws{self.url.removeprefix("http")}/api/ws'
        return connect(url, additional_headers={'X-API-Key': KEY}, proxy=None, open_timeout=5)


@pytest.fixture
def dashboard(mac_url: str) -> Iterator[Dashboard]:
    board = Dashboard(mac_url)
    yield board
    board.http.close()


def _receive(ws: ClientConnection, channel: str, count: int = 1, timeout: float = 5.0) -> list[Any]:
    """The data of the next `count` messages on `channel`; every message on the socket must be a WsMessage."""
    deadline = time.monotonic() + timeout
    found: list[Any] = []
    while len(found) < count:
        try:
            message = json.loads(ws.recv(timeout=max(0.0, deadline - time.monotonic())))
        except TimeoutError:
            arrived = [data.get('kind', data) for data in found]
            raise AssertionError(f'{len(found)} of {count} {channel} messages arrived: {arrived}') from None
        WS_MESSAGES.validate_python(message)
        if message['channel'] == channel:
            found.append(message['data'])
    return found


def _beat(session_id: str | None, at_rest: bool = True, scene: str | None = None) -> dict[str, Any]:
    """The heartbeat's body, as main.Robot._beat builds it: awake, or asleep and moving, no antenna wakes it."""
    awake, rest = session_id is not None, at_rest and not session_id and not scene
    return {'awake': awake, 'session_id': session_id, 'antenna_wake': rest, 'scene': scene}


def _tools(mac: Mac, camera: Camera | None = None) -> Tools:
    """The robot's real tool handlers, reaching the Mac through the robot's real client; the hardware is faked."""
    return Tools(body=FakeBody(), audio=FakeAudio(), camera=camera, mac=mac, emotions=FakeEmotions(()))


def _run(tools: Tools, name: str, /, **args: Any) -> ToolResult:
    async def show(_jpeg: bytes) -> None:
        """Session._send_image: the picture goes to Gemini as a turn of its own."""

    return asyncio.run(tools.run(name, args, show))


def _camera() -> RobotCamera:
    """The robot's real camera adapter and JPEG encoder, on a 1080p frame of noise: the largest picture it sends."""
    frame = np.random.default_rng(0).integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
    return RobotCamera(SimpleNamespace(media=SimpleNamespace(camera=SimpleNamespace(read=lambda: frame))))


def test_the_settings_the_robot_fetches_are_settings_it_accepts(mac_url: str, robot: Mac, dashboard: Dashboard) -> None:
    changed = {'name': 'Robby', 'voice': 'Kore', 'personality': 'Tu adores le jazz.', 'web_search': True}
    patched = dashboard.call('PATCH', '/api/settings', json=changed)
    assert patched.status_code == 200, patched.text
    view = robot.fetch_settings()  # the robot's own check: it raises on settings a wake would refuse
    assert contracts.SettingsView.model_validate(view).settings.model_dump() == patched.json()['settings']
    setup = live.build_config(view, declarations(emotions=[], camera=False), handle=None, search=True)
    assert setup.speech_config.voice_config.prebuilt_voice_config.voice_name == 'Kore'
    instruction = setup.system_instruction.parts[0].text
    assert instruction.startswith(view['system_prompt']) and instruction.endswith(view['web_prompt'])
    assert instruction.startswith('Tu es Robby') and 'Tu adores le jazz.' in instruction
    with pytest.raises(requests.HTTPError, match='401'):  # another key: refused, and that wake goes back to sleep
        Mac(url=mac_url, api_key='not-the-key').fetch_settings()


def test_the_robots_heartbeat_is_the_session_the_dashboard_sees(robot: Mac, dashboard: Dashboard) -> None:
    beat = [_beat(None)]
    robot.start(on_command=lambda _command: None, beat=lambda: beat[0])
    _until(lambda: dashboard.state()['robot_status'] == 'live')
    assert dashboard.state()['session']['awake'] is False
    beat[0] = _beat('0a1b2c3d4e5f')
    _until(lambda: dashboard.state()['session']['awake'])
    awake = {
        'awake': True,
        'session_id': '0a1b2c3d4e5f',
        'phase': None,
        'cost_usd': 0.0,
        'last_latency_ms': None,
        'manette': None,
        'hushed': False,
        'antenna_wake': True,  # asleep only: awake, the snapshot leaves it at its default
        'scene': None,
    }
    assert dashboard.state()['session'] == awake
    beat[0] = _beat(None)
    _until(lambda: not dashboard.state()['session']['awake'])
    assert dashboard.state()['session']['antenna_wake'] is True
    beat[0] = _beat(None, at_rest=False)  # a try or a scene played asleep: the header asks for no wiggle
    _until(lambda: dashboard.state()['session']['antenna_wake'] is False)
    beat[0] = _beat(None, scene='fete-surprise')  # a scene whose `started` was lost: the beat says it
    _until(lambda: dashboard.state()['session']['scene'] == 'fete-surprise')
    beat[0] = _beat(None)  # and its end lost too
    _until(lambda: dashboard.state()['session']['scene'] is None)


def test_two_beats_lost_never_show_a_talking_robot_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """docs/CONTRACTS.md's numbers, written out: the robot beats every 3 s, the Mac calls it gone after 10 s without
    a beat. The robot's real beat loop and the Mac's real Health on one clock: two beats lost in a row, each waiting
    out its whole timeout, leave the robot live; an unplugged one is gone 10 s after its last beat."""
    assert (config.HEARTBEAT_S, config.EVENT_TIMEOUT_S, health.HEARTBEAT_STALE_S) == (3.0, 2.0, 10.0)
    now = [0.0]
    mac_side = health.Health('robot', 8000, lambda _message: None, lambda: 100, clock=lambda: now[0])
    answers = iter(['taken', 'lost', 'lost', 'taken'])
    landed: list[tuple[float, str]] = []  # when each beat reached the Mac, and the robot's status it found there

    def send(_method: str, _path: str, *, json: dict[str, Any], timeout: float) -> SimpleNamespace:
        if next(answers) == 'lost':
            now[0] += timeout  # no answer: the beat waits out its timeout
            raise requests.Timeout('lost')
        landed.append((now[0], mac_side.status()))
        mac_side.beat(contracts.Heartbeat.model_validate(json))
        return SimpleNamespace(status_code=204)

    def wait(seconds: float) -> bool:
        now[0] += seconds
        return len(landed) == 2  # stopped once the beat after the lost ones has landed

    robot = Mac(url='http://mac.invalid')
    robot._send = send
    robot._stop = SimpleNamespace(wait=wait)
    monkeypatch.setattr('robot_agent.mac.time', SimpleNamespace(monotonic=lambda: now[0]))
    robot._heartbeat(lambda: _beat('0a1b2c3d4e5f'))
    assert landed == [(0.0, 'off'), (9.0, 'live')]  # three periods start to start, whatever the lost ones waited
    now[0] = landed[-1][0] + health.HEARTBEAT_STALE_S
    assert (mac_side.status(), mac_side.session().awake) == ('live', True)
    now[0] += 0.001
    assert (mac_side.status(), mac_side.session().awake) == ('off', False)


def test_one_event_of_each_kind_reaches_the_dashboard_in_order(
    robot: Mac, dashboard: Dashboard, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger='robot_agent.mac')
    # The keyless handshake over plain HTTP: websockets' sync client may sit out its 10 s close timeout on a refusal.
    refused = requests.get(f'{dashboard.url}/api/ws', headers=HANDSHAKE, timeout=5)
    assert refused.status_code == 403  # closed before accept: the key is the socket's whole authentication
    session_id = '5e5510000a1b'
    sent: list[dict[str, Any]] = []

    def report(event: dict[str, Any]) -> None:  # Session._emit adds the session id; main.Robot puts it itself
        sent.append({**event, 'session_id': session_id})
        robot.emit(sent[-1])

    transcript = Transcript(report)
    with dashboard.socket() as ws:
        robot.start(on_command=lambda _command: None, beat=lambda: _beat(session_id))
        report({'kind': 'session_start'})  # main.Robot._cycle
        report({'kind': 'phase', 'phase': SessionState.LISTENING.value})  # Session._on_transition
        transcript.heard('Regarde autour de toi.')
        transcript.user_done(at=100.0)
        report({'kind': 'tool_call', 'call_id': 'c1', 'name': 'look', 'args': {}})  # Session._run_call
        look = _run(_tools(robot, _camera()), 'look')
        result = {'status': look.status, 'message': look.message, 'data': look.data}
        report({'kind': 'tool_result', 'call_id': 'c1', 'name': 'look', **result})  # Session._result
        transcript.audio(now=100.64)  # the reply's first audio
        transcript.said('Je vois un bureau.')
        report(live.usage_event(USAGE))
        report({'kind': 'scene', 'scene_id': 'fete-surprise', 'title': 'La fête surprise', 'state': 'started'})
        report({'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'app'})  # manette.Manette._release
        report({'kind': 'replay', 'state': 'started', 'seconds': 2.48, 'loops': 4})  # main.Robot._replay
        report({'kind': 'hush', 'on': False, 'why': 'antenna'})  # hush.Hush: an antenna held, the speech taken back
        transcript.close()
        report({'kind': 'session_end', 'reason': 'goto_sleep'})  # main.Robot._cycle
        received = _receive(ws, 'conversation', count=len(sent))

    assert sorted(event['kind'] for event in sent) == sorted(
        {e['kind'] for e in contract_examples.examples()['RobotEvent']}
    )
    expected = [EVENTS.dump_python(EVENTS.validate_python(event), mode='json') for event in sent]
    usage = next(event for event in expected if event['kind'] == 'usage')
    cost = round(pricing.cost_usd(contracts.Usage.model_validate(usage)), 6)
    usage.update(cost_usd=cost, session_cost_usd=cost)
    assert received == expected  # each one accepted and relayed once, in the robot's order, the usage costed
    assert cost > 0
    image = next(event for event in received if event['kind'] == 'tool_result')['data']['image_base64']
    assert max(Image.open(io.BytesIO(base64.b64decode(image))).size) == config.LOOK_MAX_EDGE
    lost = [r.getMessage() for r in caplog.records if r.name == 'robot_agent.mac' and 'event' in r.getMessage()]
    assert lost == []
    _until(lambda: dashboard.state()['session']['awake'])
    tally = {
        'awake': True,
        'session_id': session_id,
        'phase': 'listening',
        'cost_usd': cost,
        'last_latency_ms': 640,
        'manette': None,
        'hushed': False,
        'antenna_wake': True,
        'scene': None,
    }
    assert dashboard.state()['session'] == tally


def test_dashboard_commands_reach_the_robots_long_poll(robot: Mac, dashboard: Dashboard) -> None:
    refused = dashboard.call('POST', '/api/session/say', json={'text': 'Personne ?'})
    assert (refused.status_code, refused.json()) == (409, {'detail': NOT_IN_CONVERSATION})
    taken: queue.Queue[dict[str, Any]] = queue.Queue()
    handled = threading.Event()

    def on_command(command: dict[str, Any]) -> None:  # on the long-poll thread, which polls again once it returns
        taken.put(command)
        if command['action'] == 'say':
            handled.wait(5)  # meanwhile nothing polls: what the dashboard sends waits on the Mac

    robot.start(on_command=on_command, beat=lambda: _beat('c0ffee000001'))
    _until(lambda: dashboard.state()['session']['awake'])  # the Mac takes a say only for an awake robot
    assert dashboard.call('POST', '/api/session/say', json={'text': 'Bonjour Reachy'}).status_code == 204
    say = contracts.Command.model_validate(taken.get(timeout=5))
    assert (say.action, say.text) == ('say', 'Bonjour Reachy')
    for action in ('wake', 'sleep'):
        assert dashboard.call('POST', f'/api/session/{action}').status_code == 204
    handled.set()
    sleep = contracts.Command.model_validate(taken.get(timeout=5))
    assert (sleep.action, sleep.text) == ('sleep', None)
    with pytest.raises(queue.Empty):  # the wake was replaced: the polls that follow bring nothing
        taken.get(timeout=0.5)


def test_a_restart_wakes_the_robot_once_its_session_end_arrives(robot: Mac, dashboard: Dashboard) -> None:
    session_id = 'feed00000001'
    taken: queue.Queue[dict[str, Any]] = queue.Queue()
    robot.start(on_command=taken.put, beat=lambda: _beat(session_id))  # the beat stays awake: the event decides
    _until(lambda: dashboard.state()['session']['awake'])
    assert dashboard.call('POST', '/api/session/restart').status_code == 204
    assert taken.get(timeout=5)['action'] == 'sleep'
    with pytest.raises(queue.Empty):
        taken.get(timeout=0.3)
    robot.emit({'kind': 'session_end', 'session_id': session_id, 'reason': 'dashboard'})  # main.Robot._cycle
    assert taken.get(timeout=5)['action'] == 'wake'


def test_the_robots_set_volume_is_the_dashboards_volume(
    robot: Mac, dashboard: Dashboard, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert dashboard.state()['volume'] == 100
    with dashboard.socket() as ws:
        result = _run(_tools(robot), 'set_volume', level=35)  # no daemon: stored and shown, but not called done
        assert _receive(ws, 'volume') == [{'volume': 35}]
    assert (result.status, result.message, result.data) == ('error', VOLUME_NOT_TAKEN.format(volume=35), {})
    assert dashboard.state()['volume'] == 35
    pushed: list[int] = []
    monkeypatch.setattr(health, 'probe', lambda host, port: True)  # the daemon comes back
    monkeypatch.setattr(health, 'push_volume', lambda host, port, volume: pushed.append(volume) or True)
    _until(lambda: pushed == [35])  # the stored volume, at the next probe
    result = _run(_tools(robot), 'set_volume', level=60)
    assert (result.status, result.data, pushed) == ('ok', {'volume': 60}, [35, 60])


def test_an_unconfigured_spotify_comes_back_to_the_robot_in_french(robot: Mac) -> None:
    with pytest.raises(MacError) as refused:  # the Mac's own French detail, as the robot's client reads it
        robot.request('POST', '/api/spotify/play', {'query': 'Daft Punk', 'kind': 'track'})
    assert str(refused.value) == spotify.NOT_CONFIGURED
    for tool, args in (
        ('play_music', {'query': 'Daft Punk', 'kind': 'artist'}),
        ('play_music', {'kind': 'liked'}),
        ('music', {'action': 'pause'}),
        ('music', {'action': 'volume', 'amount': 20}),
    ):
        result = _run(_tools(robot), tool, **args)
        assert (result.status, result.message, result.data) == ('error', spotify.NOT_CONFIGURED, {}), tool
        assert not ENGLISH.search(result.message), result.message
        answer = result.response('c1', tool)  # what the model gets: said once it is idle, never filed away
        assert answer.response == {'status': 'error', 'message': spotify.NOT_CONFIGURED}
        assert answer.scheduling is WHEN_IDLE


@pytest.mark.parametrize('status', ['ready', 'failed'])
def test_a_build_cancelled_on_the_dashboard_is_nothing_the_robot_tells(status: str) -> None:
    """The owner's « Annuler » on a build asked for by voice: the robot knows the cancel by the Mac's own error (its
    copy cannot import the Mac's), and tells the model nothing, neither a failure nor a ready app."""
    assert apps.CANCELLED == mac_apps.CANCELLED
    assert apps.built({'title': 'Pong', 'status': status, 'error': mac_apps.CANCELLED}) is None


# ----- 3. The catalogue covers what the robot does --------------------------------------------------


def test_every_tool_and_animation_of_the_robot_is_in_the_catalogue() -> None:
    """The palette's single source: every tool the model can call (subjects' tools included) and every gesture,
    emotion and animation variant has an entry; a new tool or variant without one fails here."""
    mac = FakeMac()
    tools = Tools(
        body=FakeBody(),
        audio=FakeAudio(),
        camera=FakeCamera(),
        mac=mac,
        emotions=_loaded_emotions(),
        extra=robot_extras(mac),
    )
    declared = {decl.name for decl in tools.declarations()}
    catalog = mac_catalog.build()
    assert declared <= {entry.tool for entry in catalog.entries}, declared - {e.tool for e in catalog.entries}
    ids = {entry.id for entry in catalog.entries}
    every_move = (
        {f'gesture:{name}' for name in GESTURES}
        | {f'emotion:{name}' for name in CURATED}
        | {f'animation:{mode}:{key}' for mode in VARIANTS for key in VARIANTS[mode]}
    )
    assert every_move <= ids, every_move - ids


def test_every_label_the_mac_sends_has_its_english(dashboard: Dashboard, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard's English of the Mac's labels (frontend/src/mac.en.json) is keyed by the label itself: a label
    reworded or added here fails until its English follows, and a key the Mac no longer sends is dropped. A scene's
    or an app's title is its author's, and stays as written."""
    root = Path(__file__).resolve().parent.parent
    monkeypatch.setattr(mac_apps, 'ASSETS', root / 'mac_server' / 'assets' / 'apps')  # the apps shipped: their entries
    english = json.loads((root / 'frontend' / 'src' / 'mac.en.json').read_text(encoding='utf-8'))
    catalog = dashboard.call('GET', '/api/catalog').json()
    options = dashboard.call('GET', '/api/settings').json()['options']
    vocabulary = dashboard.call('GET', '/api/scenes/vocabulary').json()
    sent = {group['label'] for group in catalog['groups']}
    sent |= {entry['description'] for entry in catalog['entries']}
    sent |= {entry['label'] for entry in catalog['entries'] if not entry['id'].startswith(('scene:', 'app:'))}
    sent |= {option['label'] for mode in options['animations'].values() for option in mode}
    sent |= {voice['descriptor'] for voice in options['voices']}
    sent |= {word[key] for words in vocabulary.values() for word in words for key in ('label', 'family')} - {''}
    assert sorted(sent - english.keys()) == [], 'a label of the Mac without its English'
    assert sorted(english.keys() - sent) == [], 'an English for a label the Mac no longer sends'
    assert all(english.values())


def test_every_music_action_and_play_kind_has_its_own_entry() -> None:
    """« reprends » finds « Reprendre la musique », not the audiobook: each value the model can give `music` or
    `play_music` has its entry, and a new one without an entry fails here."""
    entries = mac_catalog.build().entries
    assert {e.id for e in entries if e.tool == 'music'} == {f'spotify:{action}' for action in MUSIC_ACTIONS}
    assert {e.id for e in entries if e.tool == 'play_music'} == {f'spotify:play:{kind}' for kind in PLAY_KINDS}


def test_the_palette_never_sends_a_made_up_example() -> None:
    """Only a sentence without a parameter is sent as is. What needs a choice (what to play, where to turn, which
    scene) leaves its start in the composer for the user to finish; « Créer une app » opens the Apps form."""
    actions = {e.id: e.action for e in mac_catalog.build().entries}
    sent = {key for key, action in actions.items() if action.kind == 'say'}
    assert sent == {
        'session:look',
        *(f'spotify:play:{kind}' for kind in ('liked', 'audiobook', 'podcast')),
        *(f'spotify:{action}' for action in ('pause', 'resume', 'next', 'previous', 'now', 'like')),
    }
    for key in ('session:turn', 'scenes:play', 'apps:update', 'spotify:play:track', 'spotify:queue', 'spotify:device'):
        action = actions[key]
        assert action.kind == 'compose' and action.text.endswith(' '), key  # the user writes what follows
    assert actions['apps:create'] == contracts.AppAction(kind='app', app_id=None)
    with pytest.raises(ValueError, match='literal_error'):
        contracts.SettingsAction.model_validate({'kind': 'settings', 'field': 'volum'})  # the drawer has no such field


def test_a_leftover_agent_is_killed_only_once_it_would_be_done() -> None:
    """scripts/stop_agent.sh (`make up`, `make run`) stops a leftover agent, then kills it. Never before the agent is
    done by its own bounds: a hold's wait to be let go within reach included, or the motors it freed stay freed until
    the next agent takes them all."""
    script = (Path(__file__).parents[1] / 'scripts' / 'stop_agent.sh').read_text()
    loop = re.search(
        r"seq (\d+)\); do pgrep -f '\$AGENT' >/dev/null \|\| exit 0; sleep ([\d.]+); done; pkill -KILL", script
    )
    assert loop is not None, 'the leftover agent is no longer waited for this way: update this test'
    # Its shutdown: the conversation closed (2 x CLOSE_TIMEOUT_S), the sleep move waited for (SLEEP_S + 3 s, main.py),
    # the last events sent to the Mac (Mac.close, 2 s).
    usual = 2 * config.CLOSE_TIMEOUT_S + config.SLEEP_S + 3.0 + 2.0
    held = manette.RETURN_WAIT_S + manette.TAKE_BACK_ATTEMPTS * 1.0  # waiting to be let go, then the torque back
    assert int(loop[1]) * float(loop[2]) >= usual + held


def test_the_documented_timings_are_the_running_ones() -> None:
    """CLAUDE.md's numbers, written out: the other tests patch them or follow them (the
    command, app move and human grace windows have their own)."""
    assert config.RECONNECT_BACKOFF_S == (0.5, 2.0, 5.0)  # one wait per attempt: at most 3
    assert (config.SETTINGS_TIMEOUT_S, config.SELF_NOISE_TAIL_S) == (2.0, 1.0)
    assert (contracts.TRY_TTL_S, contracts.SCENE_STOP_TTL_S, contracts.MANETTE_TTL_S) == (10.0, 10.0, 3.0)


def test_the_three_processes_know_the_same_manette_profiles_and_bodies() -> None:
    """A profile one side does not know is dropped on its way: the Mac refuses it, the dashboard ignores what carries
    it, the robot answers nothing. A body the dashboard or the toolbox does not know has no rest, no words, no time
    to be taken back."""
    assert get_args(contracts.ManetteProfile) == manette.PROFILES
    root = Path(__file__).resolve().parent.parent
    pad = (root / 'frontend/src/apps/pad.ts').read_text(encoding='utf-8')
    kit = (root / 'mac_server/assets/kit.js').read_text(encoding='utf-8')
    known = re.search(r'export type Body = ([^;]+);', pad)
    assert known and tuple(re.findall(r"'(\w+)'", known.group(1))) == tuple(manette.BODIES)
    said = (root / 'frontend/src/apps/words.ts').read_text(encoding='utf-8')
    for table in ('asks', 'held'):  # what the human is told of each body, before and while it is held
        tables = re.findall(rf'^        {table}: {{(.*?)^        }}', said, re.S | re.M)
        assert len(tables) == 2, table  # in French, in English
        for words in tables:
            assert set(re.findall(r'^            (\w+):', words, re.M)) == set(manette.BODIES), table
    for table, more in (('REPRISE_MS', set()), ('TENUE', {manette.EAR})):  # the toolbox's own, by body
        keys = re.search(rf'const {table} = {{([^}}]*)}}', kit)
        assert keys and set(re.findall(r'(\w+): ', keys.group(1))) == set(manette.BODIES) | more, table
    for body in manette.BODIES:  # and the builder is told each body a page may ask for
        assert f"\n- '{body}' : " in app_build.PROMPT, body


def test_the_bench_shows_the_manette_as_it_runs() -> None:
    """« Banc de manette » draws the antennas' thresholds, says what each body frees, bounds a replay and expects a
    pace of pads: each one the dashboard's, the robot's or the Mac's own, never a number of the page's."""
    root = Path(__file__).resolve().parent.parent
    bench = (root / 'mac_server/assets/apps/banc-de-manette.html').read_text(encoding='utf-8')
    pad = (root / 'frontend/src/apps/pad.ts').read_text(encoding='utf-8')

    def said(name: str, text: str = bench) -> float:
        found = re.search(rf'\b{name} = ([\d.]+)', text)
        assert found, name
        return float(found.group(1))

    assert (said('SEUIL_APPUI'), said('SEUIL_RELACHE')) == (said('PRESS_DEG', pad), said('RELEASE_DEG', pad))
    assert (said('PASSAGE_MIN'), said('PASSAGE_MAX'), said('TOURS_MAX')) == (take.MIN_S, take.MAX_S, take.LOOPS_MAX)
    assert said('COUTURE_S') == take.SEAM_S
    pace = re.search(r'CADENCE = \{ corps: (\d+), voix: (\d+) \}', bench)
    relayed = re.search(r'frequency=(\d+)', studio.STATE_STREAM)
    assert pace and relayed and (int(pace[1]), int(pace[2])) == (int(relayed[1]), round(1 / ear.FRAME_S))
    assert int(relayed[1]) == poses.HZ  # the robot's own datagrams go at the pace of the stream they stand for
    motors = {'base': {'body_rotation'}, 'tete': set(manette.HEAD)}  # what the page calls a part: the motors freed
    freed = dict(re.findall(r'^  (\w+): \{\n    libres: \[([^\]]*)\]', bench, re.M))
    assert {
        body: set().union(*(motors[part] for part in re.findall(r"'(\w+)'", parts))) for body, parts in freed.items()
    } == {body: set(ids) for body, ids in manette.BODIES.items()}


def test_the_robot_holds_the_manette_through_a_stall_of_its_link() -> None:
    """A dashboard that closes renews nothing: the Mac lets another app take the manette once MANETTE_TTL_S has passed
    (a renewal older than that is not handed on), and the robot gives the body back on its own later (the other tests
    patch its lease): its link to the Mac stalls for seconds, so it waits several renewals more than the Mac keeps
    one, and no longer than the Mac waits for a heartbeat before it says the robot is gone."""
    assert 3 * contracts.MANETTE_TTL_S <= manette.LEASE_S <= health.HEARTBEAT_STALE_S
