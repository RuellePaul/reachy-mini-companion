"""What the robot sends: events relayed and costed, the heartbeat behind status and session, the volume."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter

from mac_server import health as health_module
from mac_server import pricing
from mac_server.contracts import Heartbeat, RobotEvent, SessionSnapshot, TokenCounts, Usage
from mac_server.health import HEARTBEAT_STALE_S, Health
from mac_server.routes import VOLUME_NOT_TAKEN
from mac_server.server import Mac
from scripts.contract_examples import examples
from tests.mac.conftest import KEY, longest_stall

EVENTS = TypeAdapter(RobotEvent)


def _usage(sid: str, prompt: dict, response: dict) -> dict:
    zero = {'text': 0, 'audio': 0, 'image': 0}
    total = sum(prompt.values()) + sum(response.values())
    return {
        'kind': 'usage',
        'session_id': sid,
        'prompt': zero | prompt,
        'response': zero | response,
        'total_tokens': total,
    }


def test_pricing_per_modality() -> None:
    def cost(**counts: int) -> float:
        prompt = {k.removeprefix('in_'): v for k, v in counts.items() if k.startswith('in_')}
        response = {k.removeprefix('out_'): v for k, v in counts.items() if k.startswith('out_')}
        usage = Usage(
            kind='usage', session_id='s', prompt=TokenCounts(**prompt), response=TokenCounts(**response), total_tokens=0
        )
        return pricing.cost_usd(usage)

    million = 1_000_000
    assert cost(in_text=million) == pytest.approx(0.75)
    assert cost(in_audio=million) == pytest.approx(3.00)
    assert cost(in_image=million) == pytest.approx(1.00)
    assert cost(out_text=million) == pytest.approx(4.50)
    assert cost(out_audio=million) == pytest.approx(12.00)
    assert cost(in_text=1800, in_audio=250, out_audio=120) == pytest.approx(
        (1800 * 0.75 + 250 * 3 + 120 * 12) / million
    )
    assert pricing.MODEL == 'gemini-3.8-live'
    assert pricing.SOURCE == 'https://ai.google.dev/gemini-api/docs/pricing'


def test_the_ears_level_goes_as_it_comes_to_the_sockets_that_asked_for_it(client: TestClient, app: FastAPI) -> None:
    broker = app.state.mac.broker

    def until(check: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 3
        while not check():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    with client.websocket_connect('/api/ws') as listening, client.websocket_connect('/api/ws') as other:
        listening.send_json({'subscribe': ['voice']})
        until(lambda: len(broker._voices) == 1)
        for level in (0.0, 0.42, 1.0):
            assert client.post('/api/robot/voice', json={'level': level}).status_code == 204
        heard = [listening.receive_json() for _ in range(3)]
        assert heard == [{'channel': 'voice', 'data': {'level': level}} for level in (0.0, 0.42, 1.0)]
        assert client.post('/api/robot/voice', json={'level': 1.5}).status_code == 422  # a level, nothing else
        listening.send_json({'subscribe': []})
        until(lambda: not broker._voices)
        assert client.post('/api/robot/voice', json={'level': 0.5}).status_code == 204
        news = {'kind': 'phase', 'session_id': 's1', 'phase': 'idle'}
        assert client.post('/api/robot/events', json=news).status_code == 204
        assert other.receive_json()['channel'] == 'conversation'  # the one that never asked got no level
        assert listening.receive_json()['channel'] == 'conversation'  # nor the one that asked no more
    assert not broker._voices


def test_events_are_relayed_in_order_and_usage_is_costed(client: TestClient) -> None:
    sent = [e for e in examples()['RobotEvent'] if e['kind'] != 'usage']
    with client.websocket_connect('/api/ws') as ws:
        for event in sent:
            assert client.post('/api/robot/events', json=event).status_code == 204
        received = [ws.receive_json() for _ in sent]
        assert received == [{'channel': 'conversation', 'data': e} for e in sent]

        first = _usage('s1', {'text': 1000, 'audio': 2000}, {'audio': 500})
        second = _usage('s1', {'text': 4000}, {'text': 100, 'audio': 1000})
        other = _usage('s2', {'audio': 1000}, {})
        costs = []
        for usage in (first, second, other):
            client.post('/api/robot/events', json=usage)
            data = ws.receive_json()['data']
            assert {k: v for k, v in data.items() if not k.endswith('cost_usd')} == usage
            costs.append((data['cost_usd'], data['session_cost_usd']))
    c1, c2, c3 = (0.00075 + 0.006 + 0.006), (0.003 + 0.00045 + 0.012), 0.003
    assert costs == [
        (pytest.approx(c1), pytest.approx(c1)),
        (pytest.approx(c2), pytest.approx(c1 + c2)),
        (pytest.approx(c3), pytest.approx(c3)),
    ]


def test_a_malformed_event_is_refused(client: TestClient) -> None:
    for body in ({'kind': 'nope', 'session_id': 's'}, {'kind': 'phase', 'session_id': 's', 'phase': 'dancing'}, {}):
        assert client.post('/api/robot/events', json=body).status_code == 422


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[BaseModel] = []

    def __call__(self, message: BaseModel) -> None:
        self.messages.append(message)

    def take(self) -> list[tuple[str, object]]:
        taken = [(m.channel, m.data) for m in self.messages]  # type: ignore[attr-defined]
        self.messages.clear()
        return taken


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


def test_status_off_idle_live_from_probe_and_heartbeat(new_clock, recorder: _Recorder) -> None:
    clock = new_clock()
    health = Health('robot', 8000, recorder, lambda: 100, clock=clock)
    assert health.status() == 'off'
    health.daemon_ok = True
    health.publish_changes()
    assert health.status() == 'idle'
    health.beat(Heartbeat(awake=False))
    assert health.status() == 'live'
    health.daemon_ok = False  # a fresh heartbeat says the robot is there, whatever the probe saw
    assert health.status() == 'live'
    clock.now += HEARTBEAT_STALE_S + 0.1
    assert health.status() == 'off'
    health.daemon_ok = True
    assert health.status() == 'idle'
    health.publish_changes()
    statuses = [data.status for channel, data in recorder.take() if channel == 'robot_status']
    assert statuses == ['idle', 'live', 'idle']  # on change only: nothing for the unseen 'off' in between
    health.publish_changes()
    assert recorder.take() == []


def test_the_session_follows_the_heartbeat(new_clock, recorder: _Recorder) -> None:
    clock = new_clock()
    health = Health('robot', 8000, recorder, lambda: 100, clock=clock)
    for event in (
        {'kind': 'phase', 'session_id': 's1', 'phase': 'listening'},
        {'kind': 'latency', 'session_id': 's1', 'turn_id': 'u1', 'ms': 640},
        _usage('s1', {'text': 1_000_000}, {}),
    ):
        health.record(EVENTS.validate_python(event))
    assert health.session() == SessionSnapshot(awake=False)  # events alone do not make it awake
    health.beat(Heartbeat(awake=True, session_id='s1'))
    awake = SessionSnapshot(awake=True, session_id='s1', phase='listening', cost_usd=0.75, last_latency_ms=640)
    assert health.session() == awake
    health.beat(Heartbeat(awake=True, session_id='s2'))  # a new session: none of s1's figures
    assert health.session() == SessionSnapshot(awake=True, session_id='s2')
    health.beat(Heartbeat(awake=True, session_id='s1'))
    clock.now += HEARTBEAT_STALE_S + 0.1
    assert health.session() == SessionSnapshot(awake=False)  # a stale heartbeat is a sleeping robot
    health.publish_changes()
    sessions = [data for channel, data in recorder.take() if channel == 'session']
    assert sessions == [awake, SessionSnapshot(awake=True, session_id='s2'), awake, SessionSnapshot(awake=False)]


def test_the_session_says_whether_a_wiggle_wakes_the_sleeping_robot(new_clock, recorder: _Recorder) -> None:
    health = Health('robot', 8000, recorder, lambda: 100, clock=new_clock())
    health.beat(Heartbeat(awake=False))
    assert health.session().antenna_wake  # an agent that does not say: at rest, as before
    health.beat(Heartbeat(awake=False, antenna_wake=False))  # a try or a scene played asleep
    assert health.session() == SessionSnapshot(awake=False, antenna_wake=False)
    health.beat(Heartbeat(awake=False, antenna_wake=True))
    sessions = [data.antenna_wake for channel, data in recorder.take() if channel == 'session']
    assert sessions == [False, True]  # news on each change


def test_an_ended_conversation_is_not_one_a_wiggle_wakes_until_the_robot_says_so(
    new_clock, recorder: _Recorder
) -> None:
    """The robot ends a conversation, then makes the sleep move: its antennas, deaf since the wake, listen again only
    once it rests, which its next heartbeat (up to 3 s later) says."""
    health = Health('robot', 8000, recorder, lambda: 100, clock=new_clock())
    health.beat(Heartbeat(awake=True, session_id='s1', antenna_wake=False))
    recorder.take()
    health.record(EVENTS.validate_python({'kind': 'session_end', 'session_id': 's1', 'reason': 'goto_sleep'}))
    assert health.session() == SessionSnapshot(awake=False, antenna_wake=False)
    health.publish_changes()
    health.beat(Heartbeat(awake=False, antenna_wake=True))  # at rest in the sleep pose
    sessions = [(data.awake, data.antenna_wake) for channel, data in recorder.take() if channel == 'session']
    assert sessions == [(False, False), (False, True)]


def test_a_start_or_an_end_is_news_at_once_and_the_heartbeat_says_whether_anyone_is_there(
    new_clock, recorder: _Recorder
) -> None:
    clock = new_clock()
    health = Health('robot', 8000, recorder, lambda: 100, clock=clock)
    start = EVENTS.validate_python({'kind': 'session_start', 'session_id': 's1'})
    health.record(start)
    assert health.session() == SessionSnapshot(awake=False)  # no heartbeat yet: no robot
    health.beat(Heartbeat(awake=False))
    health.record(start)  # the heartbeat says so up to 3 s later
    assert (health.session(), health.takes_say()) == (SessionSnapshot(awake=True, session_id='s1'), True)
    health.record(EVENTS.validate_python({'kind': 'session_end', 'session_id': 's1', 'reason': 'error'}))
    asleep = SessionSnapshot(awake=False, antenna_wake=False)  # the sleep move: deaf antennas, until a beat says
    assert (health.session(), health.takes_say()) == (asleep, False)
    health.beat(Heartbeat(awake=True, session_id='s1'))  # sent before the end, arrived after it
    assert not health.takes_say()
    health.beat(Heartbeat(awake=True, session_id='s2'))  # the next conversation, its start lost on the way
    assert (health.session(), health.takes_say()) == (SessionSnapshot(awake=True, session_id='s2'), True)
    health.record(EVENTS.validate_python({'kind': 'phase', 'session_id': 's2', 'phase': 'closing'}))
    assert (health.session().awake, health.session().phase, health.takes_say()) == (True, 'closing', False)
    health.record(EVENTS.validate_python({'kind': 'session_start', 'session_id': 's3'}))
    assert health.session().session_id == 's3'
    clock.now += HEARTBEAT_STALE_S + 0.1  # a robot that stopped beating is gone, whatever it said last
    assert (health.session(), health.takes_say()) == (SessionSnapshot(awake=False), False)


def test_the_session_says_which_scene_the_robot_plays_awake_or_asleep(new_clock, recorder: _Recorder) -> None:
    clock = new_clock()
    health = Health('robot', 8000, recorder, lambda: 100, clock=clock)
    health.beat(Heartbeat(awake=False))
    played = {'kind': 'scene', 'session_id': 'scene-1a2b', 'scene_id': 'fete-surprise', 'title': 'La fête surprise'}
    # a rehearsal waiting for its human's line still plays: the dashboard keeps its stop
    playing = (('started', None, 'fete-surprise'), ('line', 4, 'fete-surprise'), ('waiting', 7, 'fete-surprise'))
    for state, line, on_stage in (*playing, ('ended', None, None)):
        health.record(EVENTS.validate_python(played | {'state': state, 'line': line}))
        assert health.session() == SessionSnapshot(awake=False, scene=on_stage)
    for end in ('stopped', 'failed'):
        health.record(EVENTS.validate_python(played | {'state': 'waiting', 'line': 7}))
        health.record(EVENTS.validate_python(played | {'state': end, 'message': 'Arrêtée depuis le dashboard.'}))
        assert health.session().scene is None
    health.record(EVENTS.validate_python({'kind': 'session_start', 'session_id': 's1'}))
    health.record(EVENTS.validate_python(played | {'session_id': 's1', 'state': 'started'}))
    assert health.session().scene == 'fete-surprise'  # in a conversation too
    health.record(EVENTS.validate_python({'kind': 'session_end', 'session_id': 's1', 'reason': 'goto_sleep'}))
    assert health.session().scene is None  # the sleep stops it
    health.record(EVENTS.validate_python(played | {'state': 'started'}))
    clock.now += HEARTBEAT_STALE_S + 0.1
    assert health.session().scene is None  # a robot gone plays nothing


def test_the_session_holds_the_manette_from_its_on_to_its_off(new_clock, recorder: _Recorder) -> None:
    """The dashboard takes a session without `manette` for the end of the hold it saw on (its off lost): the wait to
    be let go is not that end, nor the refusal of another grant while it holds."""
    health = Health('robot', 8000, recorder, lambda: 100, clock=new_clock())
    health.beat(Heartbeat(awake=True, session_id='s1'))
    recorder.take()
    said = {'kind': 'manette', 'session_id': 's1'}
    for state, profile, why in (('on', 'volant', None), ('returning', 'volant', None), ('refused', None, 'sleep')):
        health.record(EVENTS.validate_python(said | {'state': state, 'profile': profile, 'why': why}))
        health.publish_changes()
        assert health.session().manette == 'volant', state
    health.record(EVENTS.validate_python(said | {'state': 'off', 'why': 'app'}))
    health.publish_changes()
    assert [data.manette for channel, data in recorder.take() if channel == 'session'] == ['volant', None]


def test_the_heartbeat_puts_right_a_scene_whose_events_were_lost(new_clock, recorder: _Recorder) -> None:
    """The robot sends an event once (no retry): what it plays is said again in each beat, which the Mac follows."""
    clock = new_clock()
    health = Health('robot', 8000, recorder, lambda: 100, clock=clock)
    health.beat(Heartbeat(awake=False))
    played = {'kind': 'scene', 'session_id': 'scene-1a2b', 'scene_id': 'fete-surprise', 'title': 'La fête surprise'}
    health.record(EVENTS.validate_python(played | {'state': 'started'}))
    for _ in range(5):  # its `ended` lost on the way, the robot beats on, playing nothing
        clock.now += 3
        health.beat(Heartbeat(awake=False))
        assert health.session() == SessionSnapshot(awake=False)
    health.record(EVENTS.validate_python(played | {'state': 'started'}))
    clock.now += HEARTBEAT_STALE_S + 0.1  # the agent gone mid-scene, its `stopped` never sent
    assert health.session().scene is None
    health.beat(Heartbeat(awake=False))  # a new agent, asleep, playing nothing
    assert health.session() == SessionSnapshot(awake=False)
    for awake in (False, True):  # a `started` lost: the next beat says it plays, asleep or in a conversation
        beat = {'awake': awake, 'session_id': 's1' if awake else None, 'scene': 'fete-surprise'}
        health.beat(Heartbeat.model_validate(beat))
        assert health.session().scene == 'fete-surprise'


def test_a_dashboard_that_missed_a_scene_event_reads_the_scene_on_stage_back(client: TestClient) -> None:
    client.post('/api/robot/heartbeat', json={'awake': False})
    played = {'kind': 'scene', 'session_id': 'scene-1a2b', 'scene_id': 'fete-surprise', 'title': 'La fête surprise'}
    with client.websocket_connect('/api/ws') as ws:
        client.post('/api/robot/events', json=played | {'state': 'started'})
        assert ws.receive_json()['channel'] == 'conversation'
        assert ws.receive_json() == {
            'channel': 'session',
            'data': SessionSnapshot(awake=False, scene='fete-surprise').model_dump(),
        }
    assert client.get('/api/state').json()['session']['scene'] == 'fete-surprise'  # a socket opened since reads it
    client.post('/api/robot/events', json=played | {'state': 'ended'})  # no dashboard connected
    assert client.get('/api/state').json()['session']['scene'] is None


def test_the_dashboard_learns_of_a_wake_and_a_sleep_with_the_events(client: TestClient) -> None:
    client.post('/api/robot/heartbeat', json={'awake': False})
    with client.websocket_connect('/api/ws') as ws:
        client.post('/api/robot/events', json={'kind': 'session_start', 'session_id': 's1'})
        assert ws.receive_json()['channel'] == 'conversation'
        assert ws.receive_json() == {
            'channel': 'session',
            'data': {
                'awake': True,
                'session_id': 's1',
                'phase': None,
                'cost_usd': 0.0,
                'last_latency_ms': None,
                'manette': None,
                'hushed': False,
                'antenna_wake': True,
                'scene': None,
            },
        }
        client.post('/api/robot/events', json={'kind': 'session_end', 'session_id': 's1', 'reason': 'goto_sleep'})
        assert ws.receive_json()['channel'] == 'conversation'
        asleep = SessionSnapshot(awake=False, antenna_wake=False)  # its sleep move: no wiggle wakes it yet
        assert ws.receive_json() == {'channel': 'session', 'data': asleep.model_dump()}


def test_the_session_cost_adds_up_to_the_micro_dollar(client: TestClient) -> None:
    """The header shows the snapshot to three decimals: two usages of 0.0231 $ make 0.0462 $, not 0.05 $."""
    client.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's1'})
    usage = _usage('s1', {'text': 12_000, 'audio': 1_500}, {'audio': 800})  # 0.009 + 0.0045 + 0.0096 $
    for _ in range(2):
        assert client.post('/api/robot/events', json=usage).status_code == 204
    assert client.get('/api/state').json()['session']['cost_usd'] == pytest.approx(0.0462)


def test_session_and_status_reach_the_dashboard(client: TestClient) -> None:
    with client.websocket_connect('/api/ws') as ws:
        client.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's1'})
        assert ws.receive_json() == {'channel': 'robot_status', 'data': {'status': 'live'}}
        assert ws.receive_json()['data'] == {
            'awake': True,
            'session_id': 's1',
            'phase': None,
            'cost_usd': 0.0,
            'last_latency_ms': None,
            'manette': None,
            'hushed': False,
            'antenna_wake': True,
            'scene': None,
        }
        client.post('/api/robot/events', json={'kind': 'phase', 'session_id': 's1', 'phase': 'speaking'})
        assert ws.receive_json()['channel'] == 'conversation'
        assert ws.receive_json() == {
            'channel': 'session',
            'data': {
                'awake': True,
                'session_id': 's1',
                'phase': 'speaking',
                'cost_usd': 0.0,
                'last_latency_ms': None,
                'manette': None,
                'hushed': False,
                'antenna_wake': True,
                'scene': None,
            },
        }
    state = client.get('/api/state').json()
    assert state == {'robot_status': 'live', 'session': state['session'], 'volume': 100}
    assert state['session']['phase'] == 'speaking'


def test_the_stored_volume_is_pushed_when_the_daemon_comes_back(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    probes = iter([False, True, True, False, True])
    pushed: list[tuple[str, int, int]] = []
    monkeypatch.setattr(health_module, 'probe', lambda host, port: next(probes))
    monkeypatch.setattr(
        health_module, 'push_volume', lambda host, port, volume: pushed.append((host, port, volume)) or True
    )
    health = Health('robot', 8000, recorder, lambda: 35)

    async def five_probes() -> None:
        for _ in range(5):
            await health.probe_once()

    asyncio.run(five_probes())
    assert pushed == [('robot', 8000, 35), ('robot', 8000, 35)]
    assert [data.status for _, data in recorder.take()] == ['idle', 'off', 'idle']


def test_the_daemon_is_looked_for_where_the_robots_heartbeat_comes_from(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, app: FastAPI
) -> None:
    """The robot's lease changed (unplugged, another network): its heartbeats now come from another address, and
    the daemon's probe, the volume and the pose stream follow them. A local client's address moves nothing."""
    probed: list[str] = []
    monkeypatch.setattr(health_module, 'probe', lambda host, port: probed.append(host) or True)
    monkeypatch.setattr(health_module, 'push_volume', lambda host, port, volume: probed.append(host) or True)
    health = Health('192.168.1.50', 8000, recorder, lambda: 35)
    for origin in (None, 'testclient', '127.0.0.1', '192.168.1.51'):
        health.beat(Heartbeat(awake=False), origin)
    asyncio.run(health.probe_once())
    assert health.host == '192.168.1.51' and probed == ['192.168.1.51', '192.168.1.51']
    with TestClient(app, headers={'X-API-Key': KEY}, client=('192.168.1.52', 50000)) as http:
        assert http.post('/api/robot/heartbeat', json={'awake': False}).status_code == 204
    assert app.state.mac.health.host == '192.168.1.52'


def test_the_health_probe_never_blocks_the_event_loop(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    """An offline robot's name can take seconds to fail: the probe, the volume read and its push run in threads."""
    monkeypatch.setattr(health_module, 'probe', lambda host, port: time.sleep(0.5) or True)
    monkeypatch.setattr(health_module, 'push_volume', lambda host, port, volume: time.sleep(0.5))
    health = Health('robot', 8000, recorder, lambda: time.sleep(0.5) or 35)  # a slow settings.json too
    assert asyncio.run(longest_stall(health.probe_once()))[1] < 0.2
    assert health.daemon_ok


def test_concurrent_volumes_end_stored_pushed_and_broadcast_alike(
    app: FastAPI, mac: Mac, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard's 30 meets a slow daemon while the robot sets 70: every dashboard must end on 70, like the file."""
    pushing, answer = threading.Event(), threading.Event()
    pushed: list[int] = []

    def push_volume(host: str, port: int, volume: int) -> bool:
        if volume == 30:
            pushing.set()
            answer.wait(5)
        pushed.append(volume)
        return True

    broadcast: list[int] = []
    monkeypatch.setattr(health_module, 'push_volume', push_volume)
    monkeypatch.setattr(mac.broker, 'publish', lambda message: broadcast.append(message.data.volume))
    mac.health.daemon_ok = True
    http = TestClient(app, headers={'X-API-Key': KEY})  # no lifespan: no health probe to reset daemon_ok
    writers = [
        threading.Thread(target=http.put, args=('/api/volume',), kwargs={'json': {'volume': v}}) for v in (30, 70)
    ]
    writers[0].start()
    assert pushing.wait(5)
    writers[1].start()
    writers[1].join(0.3)  # time for the 70 to overtake the 30, if it can
    answer.set()
    for writer in writers:
        writer.join(5)
    assert mac.store.volume() == 70
    assert (pushed, broadcast) == ([30, 70], [30, 70])


def test_volume_is_stored_broadcast_and_pushed_only_to_a_live_daemon(
    app: FastAPI, mac: Mac, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app, headers={'X-API-Key': KEY})  # no lifespan: no health probe to reset daemon_ok
    pushed: list[int] = []
    monkeypatch.setattr(health_module, 'push_volume', lambda host, port, volume: pushed.append(volume) or True)
    with client.websocket_connect('/api/ws') as ws:
        unreached = client.put('/api/volume', json={'volume': 20})  # stored, but no daemon to take it
        assert (unreached.status_code, unreached.json()) == (503, {'detail': VOLUME_NOT_TAKEN.format(volume=20)})
        assert ws.receive_json() == {'channel': 'volume', 'data': {'volume': 20}}
        mac.health.daemon_ok = True
        client.put('/api/volume', json={'volume': 70})
        assert ws.receive_json() == {'channel': 'volume', 'data': {'volume': 70}}
    assert pushed == [70]
    assert mac.store.volume() == 70
    assert client.get('/api/state').json()['volume'] == 70
    assert client.put('/api/volume', json={'volume': 101}).status_code == 422


def test_a_volume_the_daemon_did_not_take_is_said_and_pushed_again(
    app: FastAPI, mac: Mac, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored volume is the truth: a push that failed is not called done, and goes again at the next probe."""
    takes = iter([False, True])  # the daemon times out once, then answers
    pushed: list[tuple[int, bool]] = []

    def push_volume(host: str, port: int, volume: int) -> bool:
        took = next(takes, True)
        pushed.append((volume, took))
        return took

    monkeypatch.setattr(health_module, 'push_volume', push_volume)
    monkeypatch.setattr(health_module, 'probe', lambda host, port: True)
    mac.health.daemon_ok = True
    http = TestClient(app, headers={'X-API-Key': KEY})  # no lifespan: the probes are the test's
    reply = http.put('/api/volume', json={'volume': 40})
    assert (reply.status_code, reply.json()) == (503, {'detail': VOLUME_NOT_TAKEN.format(volume=40)})
    assert mac.store.volume() == 40  # stored all the same, and broadcast

    async def probes() -> None:
        for _ in range(3):
            await mac.health.probe_once()

    asyncio.run(probes())
    assert pushed == [(40, False), (40, True)]  # pushed again once, then left alone
    assert http.put('/api/volume', json={'volume': 40}).json() == {'volume': 40}
    assert pushed == [(40, False), (40, True), (40, True)]  # a PUT always pushes


def test_probe_and_push_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        host, port = listener.getsockname()
        assert health_module.probe(host, port)
    assert not health_module.probe(host, port)

    calls: list[tuple[str, dict]] = []

    def post(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        raise health_module.requests.ConnectionError('down')

    monkeypatch.setattr(health_module.requests, 'post', post)
    assert not health_module.push_volume('10.0.0.5', 8000, 55)  # logged, never raised: pushed again later
    assert calls == [('http://10.0.0.5:8000/api/volume/set', {'json': {'volume': 55}, 'timeout': 2.0})]


def test_a_daemon_that_answers_an_error_has_not_taken_the_volume(recorder: _Recorder) -> None:
    """Up but refusing (its audio busy, restarting): the volume is not taken, so it is pushed again until it is."""
    answers, got = iter([500, 503, 200]), []

    class Daemon(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            got.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(next(answers))
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Daemon) as daemon:
        threading.Thread(target=daemon.serve_forever, daemon=True).start()
        health = Health('127.0.0.1', daemon.server_address[1], recorder, lambda: 35)

        async def probes() -> None:
            for _ in range(3):
                await health.probe_once()

        try:
            assert not health.apply_volume(force=True)  # what PUT /api/volume says: VOLUME_NOT_TAKEN
            asyncio.run(probes())
        finally:
            daemon.shutdown()
    assert got == [('/api/volume/set', {'volume': 35})] * 3  # refused twice, then taken and left alone
