"""The studio on the Mac: previews sampled from the robot's own motion code, emotions fetched once, and the pose
relay (a fake daemon state stream on a random port stands in for the robot)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests
import websockets
from fastapi.testclient import TestClient
from scipy.spatial.transform import Rotation
from websockets.asyncio.server import ServerConnection, serve

from mac_server import studio
from mac_server.broker import POSE, Broker
from mac_server.config import Config
from mac_server.contracts import PoseFrame, PreviewFrames, Volume, WsPose, WsVolume
from mac_server.server import Mac, create_app
from robot_agent import animations, poses
from tests.mac.conftest import KEY

STREAM_PATH = '/api/state/ws/full?frequency=30&use_pose_matrix=true&with_head_joints=true'
# One message of the daemon's state stream (measured on the robot, asleep).
STATE = {
    'control_mode': 'enabled',
    'head_pose': {
        'm': [
            *(0.901465842, 0.004584259, 0.433409903, -0.021327949, -0.006689831, 0.999981077, 0.003204200),
            *(0.001346212, -0.433382310, -0.006046827, 0.901462262, -0.044200906, 0.0, 0.0, 0.0, 1.0),
        ]
    },
    'head_joints': [-0.001533981, -0.171805848, 0.863631184, -0.119650501, 0.070563116, -0.819145741, 0.179475752],
    'body_yaw': -0.001533981,
    'antennas_position': [-3.038815941, 3.049553806],
    'timestamp': '2026-10-31T18:01:15.275088Z',
    'passive_joints': None,
    'doa': None,
}


def _pitch(head: list[float]) -> float:
    return float(Rotation.from_matrix(np.array(head).reshape(4, 4)[:3, :3]).as_euler('xyz', degrees=True)[1])


def _recorded(seconds: float = 1.0) -> dict[str, Any]:
    head = np.eye(4)
    head[:3, :3] = Rotation.from_euler('y', 12, degrees=True).as_matrix()
    frames = [{'head': np.eye(4).tolist(), 'antennas': [-0.17, 0.17], 'body_yaw': 0.0, 'check_collision': False}]
    frames += [{'head': head.tolist(), 'antennas': [0.5, -0.5], 'body_yaw': 0.0, 'check_collision': False}] * 2
    return {'description': 'test', 'time': [0.0, seconds / 2, seconds], 'set_target_data': frames}


class FakeHF:
    """Stands in for `requests.get` on the emotions dataset."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.urls: list[str] = []
        self.status, self.body = status, _recorded() if body is None else body

    def __call__(self, url: str, timeout: float) -> requests.Response:
        self.urls.append(url)
        reply = requests.Response()
        reply.status_code, reply._content = self.status, json.dumps(self.body).encode()
        return reply


@pytest.fixture
def hf(monkeypatch: pytest.MonkeyPatch) -> FakeHF:
    fake = FakeHF()
    monkeypatch.setattr('mac_server.studio.requests.get', fake)
    return fake


@pytest.fixture
def studio_config(config: Config, tmp_path: Path) -> Config:
    return replace(config, cache_dir=tmp_path / 'cache')


@pytest.fixture
def studio_client(studio_config: Config) -> Iterator[TestClient]:
    with TestClient(create_app(studio_config), headers={'X-API-Key': KEY}) as test_client:
        yield test_client


def _frames(client: TestClient, **params: Any) -> PreviewFrames:
    reply = client.get('/api/studio/preview', params=params)
    assert reply.status_code == 200, reply.text
    return PreviewFrames.model_validate(reply.json())


# ----- Previews ------------------------------------------------------------------------------------------------


def test_a_gesture_preview_rests_then_plays_then_settles(studio_client: TestClient) -> None:
    frames = _frames(studio_client, kind='gesture', name='nod')
    nod = animations.GESTURES['nod']
    assert frames.hz == 30 and len(frames.head) == int((nod.duration + 1.0) * 30) + 1
    pitches = [_pitch(head) for head in frames.head]
    assert max(abs(p) for p in pitches[:9]) < 1.0  # at rest until 0.3 s
    assert 7.0 < max(pitches) <= 10.0  # the nod, as the robot's mixer blends into it
    assert abs(pitches[-1]) < 0.5  # back at rest


def test_an_animation_preview_is_the_variant_the_robot_plays(studio_client: TestClient) -> None:
    frames = _frames(studio_client, kind='animation', name='curious', mode='idle', amplitude=1.5, speed=0.8, seconds=4)
    assert len(frames.head) == 121
    head, antennas = animations.animate('idle', 'curious', animations.Tuning(1.5, 0.8))(2.0)  # after the blend
    assert np.allclose(np.array(frames.head[60]).reshape(4, 4), head, atol=1e-5)
    assert np.allclose(frames.antennas[60], antennas, atol=1e-5)


@pytest.mark.parametrize(
    ('params', 'status', 'detail'),
    [
        (
            {'kind': 'animation', 'name': 'curious'},
            422,
            'Mode inconnu\xa0: «\xa0None\xa0» (idle, listening ou talking).',
        ),
        (
            {'kind': 'animation', 'name': 'calm', 'mode': 'talking', 'amplitude': 1.2},
            422,
            'Amplitude (parole)\xa0: entre 0,5 et 1.',
        ),
        (
            {'kind': 'animation', 'name': 'calm', 'mode': 'idle', 'speed': 'nan'},
            422,
            'Vitesse (repos)\xa0: entre 0,5 et 1,5.',
        ),
        (
            {'kind': 'animation', 'name': 'calm', 'mode': 'idle', 'seconds': 25},
            422,
            'Durée\xa0: entre 1 et 20 secondes.',
        ),
        ({'kind': 'animation', 'name': 'zigzag', 'mode': 'idle'}, 404, 'Animation inconnue\xa0: «\xa0zigzag\xa0».'),
        ({'kind': 'gesture', 'name': 'moonwalk'}, 404, 'Geste inconnu\xa0: «\xa0moonwalk\xa0».'),
        ({'kind': 'emotion', 'name': 'evil1'}, 404, 'Émotion inconnue ou indisponible\xa0: «\xa0evil1\xa0».'),
        ({'kind': 'scene', 'name': 'x'}, 422, "Type d'aperçu inconnu\xa0: gesture, emotion ou animation."),
    ],
)
def test_preview_parameters_are_checked_in_french(
    studio_client: TestClient, hf: FakeHF, params: dict[str, Any], status: int, detail: str
) -> None:
    reply = studio_client.get('/api/studio/preview', params=params)
    assert (reply.status_code, reply.json()) == (status, {'detail': detail})
    assert hf.urls == []  # an unknown emotion is never looked for


def test_an_emotion_is_downloaded_once_then_read_from_the_cache(
    studio_client: TestClient, studio_config: Config, hf: FakeHF
) -> None:
    frames = _frames(studio_client, kind='emotion', name='yes1')
    assert len(frames.head) == int(2.0 * 30) + 1  # the move (1 s) and a second to settle
    assert 7.0 < max(_pitch(head) for head in frames.head) < 12.01
    assert hf.urls == [studio.EMOTION_URL.format('yes1')]
    assert (studio_config.cache_dir / 'emotions' / 'yes1.json').is_file()
    _frames(studio_client, kind='emotion', name='yes1')
    mac = Mac.from_config(studio_config)  # a restarted Mac reads its disk cache
    assert mac.studio.emotion('yes1') is not None
    assert len(hf.urls) == 1


def test_only_the_offered_emotions_are_previewed(studio_client: TestClient, hf: FakeHF) -> None:
    reply = studio_client.get('/api/studio/preview', params={'kind': 'emotion', 'name': 'serenity1'})  # scenes only
    assert reply.status_code == 404 and hf.urls == []


@pytest.mark.parametrize('fake', [FakeHF(status=503), FakeHF(body={'not': 'a move'})])
def test_an_emotion_that_cannot_be_fetched_is_a_404_and_nothing_is_cached(
    studio_client: TestClient, studio_config: Config, monkeypatch: pytest.MonkeyPatch, fake: FakeHF
) -> None:
    monkeypatch.setattr('mac_server.studio.requests.get', fake)
    reply = studio_client.get('/api/studio/preview', params={'kind': 'emotion', 'name': 'sad1'})
    assert reply.status_code == 404
    assert reply.json() == {'detail': 'Émotion inconnue ou indisponible\xa0: «\xa0sad1\xa0».'}
    assert not (studio_config.cache_dir / 'emotions' / 'sad1.json').exists()


def test_a_bad_cached_emotion_is_dropped_and_fetched_again(
    studio_client: TestClient, studio_config: Config, hf: FakeHF
) -> None:
    cached = studio_config.cache_dir / 'emotions' / 'yes1.json'
    cached.parent.mkdir(parents=True)
    cached.write_text('{not json')
    bad = studio_client.get('/api/studio/preview', params={'kind': 'emotion', 'name': 'yes1'})
    assert bad.status_code == 404 and hf.urls == []
    assert not cached.exists()
    _frames(studio_client, kind='emotion', name='yes1')  # never a 404 for good: the next preview downloads it
    assert hf.urls == [studio.EMOTION_URL.format('yes1')] and cached.is_file()


# ----- The pose relay ------------------------------------------------------------------------------------------


def test_a_daemon_message_becomes_a_compact_pose_frame() -> None:
    frame = studio.pose_frame(json.dumps(STATE))
    assert frame == PoseFrame(
        t=1793469675275.1,
        j=[-0.00153, -0.17181, 0.86363, -0.11965, 0.07056, -0.81915, 0.17948],
        m=[round(v, 5) for v in STATE['head_pose']['m']],
        a=[-3.03882, 3.04955],
    )
    assert studio.pose_frame(json.dumps({**STATE, 'head_joints': None})) is None
    assert studio.pose_frame('not json') is None
    assert studio.pose_frame('[1, 2]') is None


class FakeDaemon:
    """The daemon's state stream: a frame every 5 ms (its body yaw counts them); records every request and message."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.received: list[Any] = []
        self.open = 0
        self.sent = 0

    async def handler(self, conn: ServerConnection) -> None:
        self.paths.append(conn.request.path if conn.request else '')
        self.open += 1
        try:
            while True:
                self.sent += 1
                await conn.send(json.dumps({**STATE, 'head_joints': [self.sent / 1000, *STATE['head_joints'][1:]]}))
                with contextlib.suppress(TimeoutError):
                    self.received.append(await asyncio.wait_for(conn.recv(), 0.005))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.open -= 1


async def _until(check: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, 'condition not met in time'
        await asyncio.sleep(0.01)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_the_relay_reads_the_daemon_only_while_a_socket_wants_pose() -> None:
    daemon = FakeDaemon()

    async def scenario() -> None:
        port = _free_port()
        broker = Broker()
        queue, other = broker.subscribe(), broker.subscribe()
        url = studio.STATE_STREAM.format('127.0.0.1', port)
        relay = asyncio.create_task(studio.relay_poses(lambda: url, broker, linger_s=0.2, retry_s=0.05))
        broker.want(queue, ['pose'])
        await asyncio.sleep(0.2)  # wanted, but the daemon is down: retried
        async with serve(daemon.handler, '127.0.0.1', port):
            await _until(lambda: queue.qsize() > 0)
            await asyncio.sleep(0.2)  # dozens of frames meanwhile, none taken
            assert [queue.get_nowait() for _ in range(queue.qsize())] == [POSE]  # one marker, never a backlog
            latest = WsPose.model_validate_json(broker.take_pose(queue) or '')
            assert latest.data.j[0] * 1000 >= daemon.sent - 3  # the newest frame, not the first
            assert broker.take_pose(queue) is None and other.qsize() == 0  # once; only to who wants it
            broker.want(queue, [])
            await _until(lambda: daemon.open == 0)  # closed once the linger is over
            await asyncio.sleep(0.2)
            assert len(daemon.paths) == 1  # and not reopened while unwanted
        relay.cancel()

    asyncio.run(scenario())
    assert daemon.paths == [STREAM_PATH]
    assert daemon.received == []  # read-only: the Mac never sends the daemon anything


def test_the_robots_own_datagrams_are_the_pose_and_the_daemons_stream_is_left_alone_while_they_come(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While it lends its body the robot sends its pose itself, as datagrams: they are published as the daemon's
    frames are, and the stream, which stops whenever the Mac's acknowledgements are late, is closed meanwhile."""
    monkeypatch.setattr(studio, 'DATAGRAMS_FRESH_S', 0.3)
    daemon = FakeDaemon()
    head, antennas = list(STATE['head_pose']['m']), list(STATE['antennas_position'])

    async def scenario() -> None:
        port = _free_port()
        broker = Broker()
        queue = broker.subscribe()
        broker.want(queue, ['pose'])
        heard = studio.Datagrams(KEY, broker)
        endpoint, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: heard, local_addr=('127.0.0.1', 0)
        )
        to = endpoint.get_extra_info('sockname')
        url = studio.STATE_STREAM.format('127.0.0.1', port)
        relay = asyncio.create_task(
            studio.relay_poses(lambda: url, broker, linger_s=0.2, retry_s=0.05, fresh=heard.fresh)
        )
        robot = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        def latest() -> PoseFrame:
            return WsPose.model_validate_json(broker.take_pose(queue) or '').data

        async with serve(daemon.handler, '127.0.0.1', port):
            await _until(lambda: daemon.open == 1 and queue.qsize() > 0)  # no datagram yet: the daemon's stream
            for n in range(1, 31):  # the robot lends its body: a measure every 10 ms
                robot.sendto(
                    poses.pack(KEY, 1000.0 + n / 100, [7.0 + n, *STATE['head_joints'][1:]], head, antennas), to
                )
                await asyncio.sleep(0.01)
            await _until(lambda: daemon.open == 0)  # the stream is closed
            frame = latest()
            assert frame.j[0] == 37.0 and frame.t == pytest.approx(1000300.0)  # the robot's own measure, its own clock
            assert frame.m == pytest.approx(head, abs=1e-5) and frame.a == pytest.approx(antennas, abs=1e-5)
            robot.sendto(poses.pack('other', 1001.0, [99.0, *STATE['head_joints'][1:]], head, antennas), to)  # not ours
            robot.sendto(b'x' * poses.SIZE, to)  # a stray datagram
            robot.sendto(poses.pack(KEY, 1000.05, [98.0, *STATE['head_joints'][1:]], head, antennas), to)  # a late one
            robot.sendto(poses.pack(KEY, 1000.4, [40.0, *STATE['head_joints'][1:]], head, antennas), to)
            await _until(lambda: broker.take_pose(queue) is not None and True)
            await asyncio.sleep(0.05)
            robot.sendto(poses.pack(KEY, 1000.5, [41.0, *STATE['head_joints'][1:]], head, antennas), to)
            await _until(lambda: (frame := broker.take_pose(queue)) is not None and '41.0' in frame)
            opened = len(daemon.paths)
            await _until(lambda: daemon.open == 1)  # the body given back, no datagram for a while: the stream again
            assert len(daemon.paths) == opened + 1
            await _until(lambda: (frame := broker.take_pose(queue)) is not None and '41.0' not in frame)
        relay.cancel()
        endpoint.close()
        robot.close()

    asyncio.run(scenario())
    assert daemon.received == []  # read-only still


def test_a_robot_whose_clock_was_set_back_is_heard_again_once_it_has_been_silent() -> None:
    clock = [100.0]
    broker = Broker()
    heard = studio.Datagrams(KEY, broker, clock=lambda: clock[0])
    said: list[float] = []
    broker.publish_pose = lambda message: said.append(message.data.t)  # type: ignore[method-assign]
    measure = ([0.0] * 7, [0.0] * 16, [0.0, 0.0])
    assert not heard.fresh()
    heard.datagram_received(poses.pack(KEY, 50.0, *measure), None)
    clock[0] += 0.1
    heard.datagram_received(poses.pack(KEY, 49.0, *measure), None)  # behind the last one, while they come: dropped
    assert said == [50000.0] and heard.fresh()
    clock[0] += studio.DATAGRAMS_FRESH_S + 1
    assert not heard.fresh()
    heard.datagram_received(poses.pack(KEY, 10.0, *measure), None)  # after a silence: whatever its clock says
    assert said == [50000.0, 10000.0]


def test_a_pose_never_closes_a_slow_socket_nor_queues_behind_it() -> None:
    async def scenario() -> list[str | None]:
        broker = Broker(queue_max=3)
        queue = broker.subscribe()
        broker.want(queue, ['pose'])
        for level in range(3):
            broker.publish(WsVolume(channel='volume', data=Volume(volume=level)))
        pose = WsPose.model_validate({'channel': 'pose', 'data': studio.pose_frame(json.dumps(STATE))})
        broker.publish_pose(pose)  # full: this frame is skipped, the socket stays
        queue.get_nowait()
        broker.publish_pose(pose)
        broker.publish_pose(pose)
        items = [queue.get_nowait() for _ in range(queue.qsize())]
        assert broker.take_pose(queue) == pose.model_dump_json() and broker.wants_pose()
        broker.unsubscribe(queue)
        assert not broker.wants_pose()
        return items

    items = asyncio.run(scenario())
    assert items[-1] == POSE and items.count(POSE) == 1 and None not in items


def test_the_dashboard_socket_subscribes_to_pose(studio_client: TestClient) -> None:
    mac: Mac = studio_client.app.state.mac  # type: ignore[attr-defined]
    pose = WsPose.model_validate({'channel': 'pose', 'data': studio.pose_frame(json.dumps(STATE))})

    def until(check: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 3
        while not check():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    with studio_client.websocket_connect('/api/ws') as ws:
        ws.send_text('{"subscribe": ["everything"]}')  # not a WsClientMessage: ignored, the socket stays
        ws.send_json({'subscribe': ['pose']})
        until(mac.broker.wants_pose)
        mac.broker.publish_pose(pose)  # from this thread, as safely as `publish`
        assert ws.receive_json() == pose.model_dump(mode='json')
        ws.send_json({'subscribe': []})
        until(lambda: not mac.broker.wants_pose())
    assert not mac.broker.wants_pose()
