"""Fixtures for the Mac server: a temporary config, the app in-process, fake clocks, a fake Spotify."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import threading
import time
from collections.abc import Awaitable, Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mac_server import app_check
from mac_server.config import Config
from mac_server.server import Mac, create_app

KEY = 'test-key'


def flat_routes(app: FastAPI) -> list[Any]:
    """The app's routes, through the included routers (FastAPI >= 0.16x nests them)."""

    def walk(routes: list[Any]) -> list[Any]:
        found: list[Any] = []
        for route in routes:
            inner = getattr(route, 'original_router', None)
            found += walk(inner.routes) if inner is not None else [route]
        return found

    return walk(list(app.routes))


async def longest_stall(job: Awaitable[Any]) -> tuple[Any, float]:
    """What `job` gave, and the longest the event loop went without running a 10 ms ticker meanwhile.

    Pass a coroutine, not a task: a task may run, and block, before the ticker starts.
    """
    stalls = [0.0]

    async def tick() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            stalls.append(now - last)
            last = now

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.03)  # ticking before the job starts: a job that blocks at once is measured too
    result = await job
    await asyncio.sleep(0.03)  # the tick that follows a block records it
    ticker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ticker
    return result, max(stalls)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        api_key=KEY,
        reachy_host='127.0.0.1',
        reachy_port=_closed_port(),  # the daemon probe fails at once
        settings_path=tmp_path / 'settings.json',
        spotify_token_path=tmp_path / 'spotify.json',
        scenes_path=tmp_path / 'scenes.json',  # never the user's ~/.reachy nor ~/.cache
        apps_dir=tmp_path / 'apps',
        cache_dir=tmp_path / 'cache',
        spotify_client_id='client-id',
        spotify_client_secret='client-secret',
        spotify_refresh_token='refresh-from-env',
        spotify_device_name='Studio Mac',
    )


@pytest.fixture
def app(config: Config) -> FastAPI:
    return create_app(config)


@pytest.fixture
def mac(app: FastAPI) -> Mac:
    return app.state.mac


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """The app with its lifespan running, every request carrying the key."""
    with TestClient(app, headers={'X-API-Key': KEY}) as test_client:
        yield test_client


@pytest.fixture
def live_server(app: FastAPI) -> Iterator[str]:
    """The app under a real uvicorn with __main__'s proxy settings, on a free port; yields its base URL."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    settings = uvicorn.Config(app, log_level='warning', ws='none', proxy_headers=True, forwarded_allow_ips='127.0.0.1')
    server = uvicorn.Server(settings)
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, 'uvicorn did not start'
            time.sleep(0.01)
        yield f'http://127.0.0.1:{listener.getsockname()[1]}'
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def new_clock() -> type[Clock]:
    return Clock


def _limited(body: Any, limit: int) -> Any:
    """What Spotify sends for `limit`: at most that many entries in each `items` page of the body."""
    if not isinstance(body, dict):
        return body
    return {k: v[:limit] if k == 'items' and isinstance(v, list) else _limited(v, limit) for k, v in body.items()}


class FakeSpotifyHTTP:
    """Stands in for Spotify behind `requests.Session.request`: scripted replies per (method, path), the last
    one repeating."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.replies: dict[tuple[str, str], list[tuple[int, Any]]] = {}
        self.error: Exception | None = None

    def reply(self, method: str, path: str, status: int = 200, body: Any = None) -> FakeSpotifyHTTP:
        self.replies.setdefault((method, path), []).append((status, body))
        return self

    def token(self, access: str = 'access-1', refresh: str | None = None, expires_in: int = 3600) -> FakeSpotifyHTTP:
        body = {'access_token': access, 'token_type': 'Bearer', 'expires_in': expires_in}
        return self.reply('POST', 'token', 200, body | ({'refresh_token': refresh} if refresh else {}))

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.calls if (m, p) == (method, path))

    def __call__(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        path = 'token' if url == 'https://accounts.spotify.com/api/token' else url.split('/v1', 1)[1]
        self.calls.append((method, path, kwargs))
        if self.error is not None:
            raise self.error
        queue = self.replies[(method, path)]
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        if 'limit' in (kwargs.get('params') or {}):
            body = _limited(body, kwargs['params']['limit'])
        response = requests.Response()
        response.status_code = status
        response._content = b'' if body is None else json.dumps(body).encode()
        return response


SPOTIFY_HOSTS = ('https://api.spotify.com/', 'https://accounts.spotify.com/')


@pytest.fixture
def spotify_http(monkeypatch: pytest.MonkeyPatch) -> FakeSpotifyHTTP:
    """Spotify's two hosts answered by the fake, on every session (the app's own is built before the test
    runs); any other host goes through as it would."""
    fake, real = FakeSpotifyHTTP(), requests.Session.request

    def routed(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
        return fake(method, url, **kwargs) if url.startswith(SPOTIFY_HOSTS) else real(session, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, 'request', routed)
    return fake


@pytest.fixture(autouse=True)
def no_chrome(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pages are run in a real Chrome only by the tests that ask for one: mac_server/app_check.py finds no browser."""
    monkeypatch.setattr(app_check, 'CHROME', tmp_path / 'no-chrome')


# A real browser: the Mac's, or the google-chrome of CI's runner, run with the same fence (read before `no_chrome`
# hides it from every test).
REAL_CHROME = next(
    (Path(p) for p in (app_check.CHROME, shutil.which('google-chrome')) if p and Path(p).is_file()), None
)


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch, no_chrome: None) -> None:
    """Pages run in that browser; skipped without one, except on CI, where a skip would leave the fence untested."""
    if REAL_CHROME is None:
        if os.environ.get('CI'):
            pytest.fail('no google-chrome on the CI runner: the page fence and the CSP go untested')
        pytest.skip('no Chrome on this machine')
    monkeypatch.setattr(app_check, 'CHROME', REAL_CHROME)
