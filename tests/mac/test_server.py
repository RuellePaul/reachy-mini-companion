"""The plumbing: broker fan-out, the health loop, CORS, the configuration and the uvicorn entry point."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mac_server import __main__ as entry
from mac_server import config as config_module
from mac_server import health as health_module
from mac_server.broker import Broker
from mac_server.config import Config, load_config
from mac_server.contracts import Volume, WsVolume
from mac_server.health import Health
from mac_server.routes import _send_all
from mac_server.server import Mac


def _volume(level: int) -> WsVolume:
    return WsVolume(channel='volume', data=Volume(volume=level))


def test_publish_from_a_thread_reaches_the_loop() -> None:
    async def scenario() -> str | None:
        broker = Broker()
        queue = broker.subscribe()
        await asyncio.to_thread(broker.publish, _volume(10))
        return await asyncio.wait_for(queue.get(), 1)

    assert asyncio.run(scenario()) == '{"channel":"volume","data":{"volume":10}}'


def test_a_subscriber_that_falls_behind_is_closed() -> None:
    async def scenario() -> tuple[list[str | None], list[str | None]]:
        broker = Broker(queue_max=3)
        slow, fresh = broker.subscribe(), broker.subscribe()
        for level in range(3):
            broker.publish(_volume(level))
        fresh.get_nowait()  # this one keeps up
        broker.publish(_volume(3))
        fresh.get_nowait()
        broker.publish(_volume(4))  # the slow one is gone: only the fresh one is fed
        return [slow.get_nowait() for _ in range(slow.qsize())], [fresh.get_nowait() for _ in range(fresh.qsize())]

    slow, fresh = asyncio.run(scenario())
    assert slow == [None]  # emptied, told to close, and no longer fed
    assert fresh == [_volume(level).model_dump_json() for level in (2, 3, 4)]


def test_the_socket_closes_on_the_close_marker() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[Any] = []

        async def send_text(self, text: str) -> None:
            self.sent.append(text)

        async def close(self, code: int) -> None:
            self.sent.append(('close', code))

    async def scenario() -> list[Any]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        for item in ('a', 'b', None):
            queue.put_nowait(item)
        ws = FakeSocket()
        await _send_all(ws, queue, Broker())  # type: ignore[arg-type]
        return ws.sent

    assert asyncio.run(scenario()) == ['a', 'b', ('close', 1013)]


def test_the_health_loop_outlives_a_failing_step(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    answers = iter([OSError('boom')])

    def probe(host: str, port: int) -> bool:
        answer = next(answers, True)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(health_module, 'probe', probe)
    monkeypatch.setattr(health_module, 'push_volume', lambda *_: None)
    monkeypatch.setattr(health_module, 'PROBE_EVERY_S', 0.01)
    monkeypatch.setattr(health_module, 'REFRESH_EVERY_S', 0.01)
    health = Health('robot', 8000, lambda message: None, lambda: 100)

    async def scenario() -> None:
        task = asyncio.create_task(health.run())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert health.daemon_ok
    assert 'health check failed' in caplog.text


def test_cors_preflight_allows_the_dashboard(client: TestClient) -> None:
    preflight = client.options(
        '/api/settings',
        headers={
            'Origin': 'http://localhost:5077',
            'Access-Control-Request-Method': 'PATCH',
            'Access-Control-Request-Headers': 'x-api-key,content-type',
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers['access-control-allow-origin'] == 'http://localhost:5077'
    assert (
        client.options(
            '/api/settings', headers={'Origin': 'http://evil.example', 'Access-Control-Request-Method': 'GET'}
        ).status_code
        == 400
    )


def test_config_reads_the_repo_root_env_and_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Path(config_module.__file__).resolve().parent.parent / '.env' == config_module.ENV_PATH
    loaded: list[Path] = []
    monkeypatch.setattr(config_module, 'load_dotenv', lambda path: loaded.append(path))
    for name in ('MAC_AGENT_API_KEY', 'MAC_AGENT_PORT', 'CORS_ORIGINS', 'SETTINGS_PATH', 'SPOTIFY_DEVICE_NAME'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_module.socket, 'gethostname', lambda: 'Studio.local')
    ran: list[list[str]] = []

    def scutil(outcome: object) -> Callable[..., object]:
        def run(command: list[str], **_: object) -> object:
            ran.append(command)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return run

    monkeypatch.setattr(config_module.subprocess, 'run', scutil(subprocess.CompletedProcess([], 0, 'Mac de Camille\n')))
    defaults = load_config()
    assert loaded == [config_module.ENV_PATH]
    # Spotify names the Mac's own app after the macOS computer name, not the (DHCP) host name.
    assert (defaults.api_key, defaults.port, defaults.spotify_device_name) == ('', 7777, 'Mac de Camille')
    assert ran == [['scutil', '--get', 'ComputerName']]
    for failed in (subprocess.CompletedProcess([], 1, ''), FileNotFoundError('scutil')):  # not a Mac: the host name
        monkeypatch.setattr(config_module.subprocess, 'run', scutil(failed))
        assert load_config().spotify_device_name == 'Studio'
    monkeypatch.setenv('SPOTIFY_DEVICE_NAME', 'Salon')
    assert load_config().spotify_device_name == 'Salon'
    assert defaults.settings_path == Path.home() / '.reachy' / 'settings.json'
    monkeypatch.setenv('MAC_AGENT_PORT', '8000')
    monkeypatch.setenv('CORS_ORIGINS', 'http://a:1, http://b:2,')
    monkeypatch.setenv('SETTINGS_PATH', '~/elsewhere/s.json')
    custom = load_config()
    assert (custom.port, custom.cors_origins) == (8000, ('http://a:1', 'http://b:2'))
    assert custom.settings_path == Path.home() / 'elsewhere' / 's.json'


def test_main_serves_everyone_but_trusts_only_this_macs_proxy(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(entry, 'load_config', lambda: config)
    monkeypatch.setattr(entry.uvicorn, 'run', lambda app, **kwargs: calls.append(kwargs))
    entry.main()
    assert (
        calls
        == [
            {
                'host': '0.0.0.0',
                'port': 7777,
                'proxy_headers': True,
                'forwarded_allow_ips': '127.0.0.1',
                'ws': 'websockets-sansio',  # never 'auto': without the library the dashboard would get 404s, not a failed boot
                'timeout_graceful_shutdown': 1,  # a parked robot poll would hold Ctrl-C for its whole wait
                'access_log': False,
            }
        ]
    )


def test_the_server_runs_on_the_clocks_the_tests_fake(config: Config) -> None:
    """The fakes injected elsewhere stand in for these; the Mac's monotonic clock stops in sleep."""
    mac = Mac.from_config(config)
    assert mac.spotify._wall is time.time  # the token expiry
    assert mac.spotify._mono is time.monotonic  # deadlines, OAuth states
    assert (mac.commands._wall, mac.commands._mono) == (time.time, time.monotonic)  # a command's age: both
    assert mac.health._clock is time.monotonic  # heartbeat age: the Mac's own sleep is not robot silence
