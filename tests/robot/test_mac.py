"""The link to the Mac: the bounded event queue, the long-poll that nothing kills, the heartbeat, the errors."""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import requests
from pydantic import TypeAdapter

from mac_server import contracts
from robot_agent import config, poses
from robot_agent.mac import Mac, MacError, _detail
from scripts import contract_examples


class FakeResponse:
    def __init__(self, body: Any = None, status: int = 200) -> None:
        self.body, self.status_code, self.ok = body, status, status < 400

    def json(self) -> Any:
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))


def test_a_full_queue_drops_the_oldest_ordinary_event_never_a_session_boundary(monkeypatch) -> None:
    monkeypatch.setattr(config, 'EVENT_QUEUE_MAX', 3)
    mac = Mac(url='http://mac.invalid')
    for event in ({'kind': 'session_start'}, {'kind': 'phase', 'n': 1}, {'kind': 'phase', 'n': 2}):
        mac.emit(event)
    mac.emit({'kind': 'phase', 'n': 3})
    assert [e.get('n') for e in mac._events] == [None, 2, 3]
    mac.emit({'kind': 'session_end'})
    assert [e['kind'] for e in mac._events] == ['session_start', 'phase', 'session_end']
    mac._events.clear()
    for kind in ('session_start', 'session_end', 'session_start'):
        mac.emit({'kind': kind})
    mac.emit({'kind': 'phase'})  # full of boundaries: the ordinary event is the one dropped
    mac.emit({'kind': 'session_end'})
    assert [e['kind'] for e in mac._events] == ['session_start', 'session_end', 'session_start', 'session_end']


def test_events_are_sent_in_order_and_flushed_on_close() -> None:
    mac = Mac(url='http://mac.invalid')
    sent: list[dict] = []

    def send(method: str, path: str, **kwargs: Any) -> FakeResponse:
        if path == '/api/robot/events':
            time.sleep(0.01)
            sent.append(kwargs['json'])
        return FakeResponse({})

    mac._send = send
    mac.start(on_command=lambda _c: None, beat=lambda: {'awake': False, 'session_id': None})
    for n in range(5):
        mac.emit({'kind': 'phase', 'n': n})
    mac.emit({'kind': 'session_end'})
    mac.close(timeout=2.0)
    assert [e.get('n') for e in sent] == [0, 1, 2, 3, 4, None]


def test_the_ears_level_is_sent_latest_only_and_a_failure_costs_one_level() -> None:
    mac = Mac(url='http://mac.invalid')
    sent: list[float] = []
    first = threading.Event()

    def send(method: str, path: str, **kwargs: Any) -> FakeResponse:
        if path == '/api/robot/voice':
            sent.append(kwargs['json']['level'])
            first.set()
            time.sleep(0.1)  # a slow Mac: the levels heard meanwhile replace one another
            if len(sent) == 1:
                raise OSError('the Mac hung up')
        return FakeResponse({})

    mac._send = send
    mac.start(on_command=lambda _c: None, beat=lambda: {'awake': False, 'session_id': None})
    mac.voice(0.1)
    assert first.wait(2.0)
    for level in (0.2, 0.3, 0.4):
        mac.voice(level)
    deadline = time.monotonic() + 2.0
    while len(sent) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.15)
    mac.close(timeout=0.5)
    assert sent == [0.1, 0.4]  # no backlog behind the slow one, and the sender outlived its failure


def test_the_command_poll_survives_bad_bodies_and_raising_callbacks(monkeypatch) -> None:
    monkeypatch.setattr(config, 'COMMAND_RETRY_S', 0.0)
    mac = Mac(url='http://mac.invalid')
    bodies = iter(
        [
            {'command': {'action': 'wake'}},  # the callback raises on this one
            ['not', 'an', 'object'],
            ValueError('not JSON'),
            requests.ConnectionError('Mac down'),
            {'command': None},
            {'command': {'action': 'say', 'text': 'salut'}},
        ]
    )
    received: list[dict] = []
    done = threading.Event()

    def send(method: str, path: str, **kwargs: Any) -> FakeResponse:
        body = next(bodies, None)
        if body is None:
            done.set()
            mac._stop.set()
            return FakeResponse({'command': None})
        if isinstance(body, requests.ConnectionError):
            raise body
        return FakeResponse(body)

    def on_command(command: dict) -> None:
        received.append(command)
        if command['action'] == 'wake':
            raise RuntimeError('event loop closed')

    mac._send = send
    thread = threading.Thread(target=mac._poll, args=(on_command,), daemon=True)
    thread.start()
    assert done.wait(2.0)
    thread.join(1.0)
    assert [c['action'] for c in received] == ['wake', 'say']


def test_the_heartbeat_says_when_the_mac_stops_and_starts_answering(caplog: pytest.LogCaptureFixture) -> None:
    mac = Mac(url='http://mac.invalid')
    answers = iter([204, None, None, 401, 204])
    bodies: list[dict] = []

    def send(method: str, path: str, **kwargs: Any) -> FakeResponse:
        bodies.append(kwargs['json'])
        status = next(answers, 'done')
        if status == 'done':
            mac._stop.set()
            return FakeResponse(status=204)
        if status is None:
            raise requests.ConnectionError('down')
        return FakeResponse(status=status)

    mac._send = send
    with caplog.at_level('INFO'), pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, 'HEARTBEAT_S', 0.0)
        mac._heartbeat(lambda: {'awake': True, 'session_id': 's1'})
    lines = [r.getMessage().split(' at ')[0] for r in caplog.records if '[MAC]' in r.getMessage()]
    assert lines == [
        '[MAC] answering',
        '[MAC] not answering',
        '[MAC] refusing the robot (HTTP 401): check MAC_AGENT_API_KEY',
        '[MAC] answering',
    ]
    for body in bodies:
        contracts.Heartbeat.model_validate(body)


def test_a_measure_of_the_lent_body_leaves_as_one_datagram_to_where_the_mac_answers() -> None:
    joints, head, antennas = [0.1] * 7, [float(n) for n in range(16)], [-0.2, 0.2]
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other,
    ):
        for listener in (first, other):
            listener.bind(('127.0.0.1', 0))
            listener.settimeout(2.0)
        mac = Mac(url=f'http://127.0.0.1:{first.getsockname()[1]}', api_key='key')
        mac.pose(12.5, joints, head, antennas)
        at, got, *_ = poses.unpack('key', first.recv(1024)) or (0.0, [])
        assert at == 12.5 and got == pytest.approx(joints)  # under the key the HTTP calls carry, on the HTTP port
        mac._url = f'http://127.0.0.1:{other.getsockname()[1]}'  # the Mac answers elsewhere now: the datagrams follow
        mac.pose(13.0, joints, head, antennas)
        assert (poses.unpack('key', other.recv(1024)) or (0.0,))[0] == 13.0
    for unreachable in (Mac(url='http://mac.invalid'), Mac(url='')):  # no address: nothing sent, nothing raised
        unreachable.pose(1.0, joints, head, antennas)


def test_a_mac_error_speaks_the_macs_french() -> None:
    assert _detail(FakeResponse({'detail': 'Spotify ne répond pas.'}, 502)) == 'Spotify ne répond pas.'
    validation = FakeResponse({'detail': [{'msg': 'String should have at most 200 characters'}]}, 422)
    assert _detail(validation) == 'Le Mac a refusé la demande (erreur 422).'  # never FastAPI's English
    assert _detail(FakeResponse(ValueError('html'), 500)) == 'Le Mac a refusé la demande (erreur 500).'


def test_request_turns_every_failure_into_a_mac_error() -> None:
    mac = Mac(url='http://mac.invalid')

    def timeout(*_args: Any, **_kwargs: Any) -> FakeResponse:
        raise requests.Timeout('slow')

    mac._send = timeout
    with pytest.raises(MacError, match="n'a pas répondu"):
        mac.request('POST', '/api/spotify/pause')
    mac._send = lambda *_a, **_k: FakeResponse(['a', 'list'])
    with pytest.raises(MacError, match='illisible'):
        mac.request('POST', '/api/spotify/pause')
    mac._send = lambda *_a, **_k: FakeResponse({'paused': True})
    assert mac.request('POST', '/api/spotify/pause') == {'paused': True}


def _mac_that(answer: str) -> tuple[Mac, list[bytes], threading.Thread]:
    """A Mac on a local socket that reads one whole request, then `answer`s: 'drops' the connection, or 'hangs'."""
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    seen: list[bytes] = []

    def serve() -> None:
        conn, _ = server.accept()
        data = b''
        while b'\r\n\r\n' not in data:
            data += conn.recv(4096)
        seen.append(data.split(b'\r\n', 1)[0])
        while answer == 'hangs' and conn.recv(4096):  # the body, then nothing until the robot gives up and closes
            pass
        conn.close()
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return Mac(url=f'http://127.0.0.1:{server.getsockname()[1]}'), seen, thread


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_a_failed_call_says_whether_the_mac_may_have_done_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """« Rien n'a été fait » only when the request never reached the Mac; once it may have, the model hears that it
    cannot know (a second « joue du jazz » would start the music twice)."""
    monkeypatch.setattr(config, 'MAC_TIMEOUT_S', 0.3)
    nothing, unknown = "rien n'a été fait", 'impossible de savoir'
    for answer, said in (('drops', 'a coupé la connexion'), ('hangs', "n'a pas répondu à temps")):
        mac, seen, thread = _mac_that(answer)
        with pytest.raises(MacError) as caught:
            mac.request('POST', '/api/spotify/play', {'kind': 'track', 'query': 'jazz'})
        thread.join(2)
        assert seen == [b'POST /api/spotify/play HTTP/1.1']
        assert said in str(caught.value) and unknown in str(caught.value) and nothing not in str(caught.value)
    for mac in (Mac(url=f'http://127.0.0.1:{_closed_port()}'), Mac(url='')):  # refused; MAC_AGENT_URL unset
        with pytest.raises(MacError, match=nothing):
            mac.request('POST', '/api/spotify/play', {'kind': 'track', 'query': 'jazz'})
    mac = Mac(url='http://mac.invalid')

    def connect_timeout(*_args: Any, **_kwargs: Any) -> FakeResponse:
        raise requests.ConnectTimeout('connect timed out')  # no connection: nothing was sent

    mac._send = connect_timeout
    with pytest.raises(MacError, match=nothing):
        mac.request('POST', '/api/volume', {'volume': 50})


def test_a_failed_request_is_never_sent_twice() -> None:
    mac = Mac(url=('http://mac.invalid', 'http://other.invalid'))  # the Mac got it: no other address is tried
    sent: list[str] = []

    def reset(method: str, url: str, **_kwargs: Any) -> FakeResponse:
        sent.append(f'{method} {url}')
        raise requests.ConnectionError('connection reset by peer')

    mac._http.request = reset
    with pytest.raises(requests.ConnectionError):
        mac.fetch_settings()  # at wake: one try, then back to sleep
    with pytest.raises(MacError, match='impossible de savoir'):
        mac.request('POST', '/api/spotify/play', {'query': 'jazz'})  # the Mac may have started it already
    assert sent == ['GET http://mac.invalid/api/settings', 'POST http://mac.invalid/api/spotify/play']


def test_a_call_that_never_reached_the_mac_goes_once_to_another_of_its_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cable pulled: the Mac's first address refuses, another answers. The call goes there, once, and that
    address is kept for the next calls; with none answering, nothing was done and nothing moves."""
    served: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            served.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    dead, live = f'http://127.0.0.1:{_closed_port()}', f'http://127.0.0.1:{server.server_address[1]}'
    try:
        mac = Mac(url=(dead, live))
        assert mac.request('POST', '/api/volume', {'volume': 50}) == {'ok': True}
        assert mac.request('POST', '/api/volume', {'volume': 40}) == {'ok': True}
        assert served == ['/api/volume', '/api/volume'] and mac._url == live
        assert f'{dead} unreachable: the Mac answers at {live}' in caplog.text
    finally:
        server.shutdown()
        server.server_close()
    mac = Mac(url=(dead, f'http://127.0.0.1:{_closed_port()}'))
    with pytest.raises(MacError, match="rien n'a été fait"):
        mac.request('POST', '/api/volume', {'volume': 50})
    assert mac._url == dead


def test_the_macs_addresses_come_best_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address the agent's SSH came from, then the Mac's at launch, then MAC_AGENT_URL's: IPv4 only, each once."""
    monkeypatch.setenv('SSH_CLIENT', '192.168.1.31 51234 22')
    monkeypatch.setenv('MAC_AGENT_HOSTS', '192.168.1.31 10.0.0.2 169.254.3.4 127.0.0.1')
    monkeypatch.setenv('MAC_AGENT_URL', 'http://192.168.1.30:7777/, http://192.168.1.31:7777')
    assert config._mac_urls() == ('http://192.168.1.31:7777', 'http://10.0.0.2:7777', 'http://192.168.1.30:7777')
    monkeypatch.setenv('SSH_CLIENT', 'fe80::1c2a:5bff:fe00:1 51234 22')  # link-local: unusable without its zone
    monkeypatch.delenv('MAC_AGENT_HOSTS')
    monkeypatch.setenv('MAC_AGENT_URL', '')
    monkeypatch.setenv('MAC_AGENT_PORT', '7788')
    assert config._mac_urls() == ()
    monkeypatch.setenv('MAC_AGENT_HOSTS', '192.168.1.31')
    assert config._mac_urls() == ('http://192.168.1.31:7788',)


def test_settings_are_checked_before_use() -> None:
    """Every field the robot reads, once, at the fetch: the rest of the robot indexes them directly."""
    mac = Mac(url='http://mac.invalid')
    view = contract_examples.examples()['SettingsView'][0]
    broken = [
        {'settings': {}},
        {'settings': {'voice': 'Kore'}},
        ['a', 'list'],
        {k: v for k, v in view.items() if k != 'web_prompt'},
        {**view, 'settings': {**view['settings'], 'voice': ''}},
        {**view, 'settings': {**view['settings'], 'web_search': 'yes'}},
        {**view, 'settings': {**view['settings'], 'idle_sleep_minutes': None}},
        {**view, 'settings': {k: v for k, v in view['settings'].items() if k != 'talking_animation'}},
    ]
    for body in broken:
        mac._send = lambda *_a, body=body, **_k: FakeResponse(body)
        with pytest.raises(ValueError, match='malformed'):
            mac.fetch_settings()
    mac._send = lambda *_a, **_k: FakeResponse(view)
    assert TypeAdapter(contracts.SettingsView).validate_python(mac.fetch_settings())
