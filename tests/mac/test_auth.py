"""The API key on every door, and the two keyless Spotify OAuth routes: loopback only, one-time state."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import urllib.parse
from collections.abc import Iterator
from dataclasses import replace

import pytest
import requests
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mac_server.config import Config
from mac_server.server import create_app
from tests.mac.conftest import KEY, flat_routes
from tests.mac.test_routes import _contracts

OAUTH = {'/api/spotify/login', '/api/spotify/callback'}


def _keyed_routes(app: FastAPI) -> list[tuple[str, str]]:
    found = []
    for route in flat_routes(app):
        if isinstance(route, APIRoute) and route.path not in OAUTH:
            found += [(method, route.path.replace('{name}', 'Achird')) for method in sorted(route.methods)]
    return found


@pytest.fixture
def strangers(app: FastAPI) -> Iterator[list[TestClient]]:
    """Clients without the key: none at all, an unrelated one, a prefix of it, the key with more after it."""
    with contextlib.ExitStack() as stack:
        yield [
            stack.enter_context(TestClient(app, headers={} if key is None else {'X-API-Key': key}))
            for key in (None, 'wrong', KEY[:-1], f'{KEY}x')
        ]


def test_every_route_but_oauth_requires_the_key(app: FastAPI, strangers: list[TestClient]) -> None:
    routes = _keyed_routes(app)
    assert len(routes) == len(_contracts(app)) - 2  # all but the Spotify OAuth pair
    for method, path in routes:
        for test_client in strangers:
            response = test_client.request(method, path)
            assert response.status_code == 401, (method, path, test_client.headers.get('X-API-Key'))
            assert response.json() == {'detail': "Clé d'API absente ou invalide."}


def test_the_key_opens_the_routes(client: TestClient) -> None:
    assert client.get('/api/settings').status_code == 200
    assert client.get('/api/state').status_code == 200


def test_no_unguarded_docs(client: TestClient) -> None:
    for path in ('/docs', '/redoc', '/openapi.json'):
        assert client.get(path).status_code == 404


def test_websocket_closed_with_1008_before_accept(app: FastAPI, strangers: list[TestClient]) -> None:
    assert any(isinstance(r, APIWebSocketRoute) and r.path == '/api/ws' for r in flat_routes(app))
    for test_client in strangers:
        # Raised by the handshake itself. A socket that was accepted is closed on leaving the block, so a
        # regression fails with DID NOT RAISE instead of hanging the TestClient's exit.
        with pytest.raises(WebSocketDisconnect) as refused, test_client.websocket_connect('/api/ws'):
            pass
        assert refused.value.code == 1008, test_client.headers.get('X-API-Key')


def test_websocket_with_the_key_is_accepted(client: TestClient) -> None:
    with client.websocket_connect('/api/ws') as ws:
        client.put('/api/volume', json={'volume': 30})
        assert ws.receive_json() == {'channel': 'volume', 'data': {'volume': 30}}


def test_empty_key_opens_the_api_with_one_loud_warning(config: Config, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(replace(config, api_key=''))
    with caplog.at_level(logging.WARNING), TestClient(app) as anonymous:
        assert anonymous.get('/api/settings').status_code == 200
        with anonymous.websocket_connect('/api/ws'):
            pass
    warnings = [r for r in caplog.records if 'MAC_AGENT_API_KEY is empty' in r.getMessage()]
    assert len(warnings) == 1


# ----- Spotify OAuth ------------------------------------------------------------------------------


def _loopback(app: FastAPI) -> TestClient:
    return TestClient(app, client=('127.0.0.1', 50000), follow_redirects=False)


def _login_state(app: FastAPI) -> str:
    response = _loopback(app).get('/api/spotify/login')
    assert response.status_code == 307
    location = urllib.parse.urlparse(response.headers['location'])
    assert location.netloc == 'accounts.spotify.com'
    query = urllib.parse.parse_qs(location.query)
    assert query['redirect_uri'] == ['http://127.0.0.1:7777/api/spotify/callback']
    assert 'streaming' not in query['scope'][0]
    return query['state'][0]


@pytest.mark.parametrize('peer', [('192.168.1.20', 50000), ('testclient', 50000), ('::ffff:10.0.0.2', 1)])
def test_oauth_refuses_a_client_that_is_not_this_mac(app: FastAPI, peer: tuple[str, int]) -> None:
    lan = TestClient(app, client=peer, follow_redirects=False)
    assert lan.get('/api/spotify/login').status_code == 403
    state = _login_state(app)
    refused = lan.get('/api/spotify/callback', params={'code': 'c', 'state': state})
    assert refused.status_code == 403
    assert 'ne s&#x27;ouvre que sur le Mac' in refused.text


def test_oauth_callback_exchanges_once_and_persists(app: FastAPI, config: Config, spotify_http) -> None:
    spotify_http.token(access='a', refresh='refresh-from-oauth')
    local = _loopback(app)
    state = _login_state(app)
    done = local.get('/api/spotify/callback', params={'code': 'the-code', 'state': state})
    assert done.status_code == 200
    assert 'Spotify est connecté' in done.text
    sent = spotify_http.calls[0][2]['data']
    assert sent == {
        'grant_type': 'authorization_code',
        'code': 'the-code',
        'redirect_uri': 'http://127.0.0.1:7777/api/spotify/callback',
    }
    assert json.loads(config.spotify_token_path.read_text()) == {'refresh_token': 'refresh-from-oauth'}
    assert os.stat(config.spotify_token_path).st_mode & 0o777 == 0o600
    reused = local.get('/api/spotify/callback', params={'code': 'the-code', 'state': state})
    assert reused.status_code == 400
    assert spotify_http.count('POST', 'token') == 1


def test_oauth_callback_refuses_an_unknown_state_and_escapes_errors(app: FastAPI, spotify_http) -> None:
    local = _loopback(app)
    assert local.get('/api/spotify/callback', params={'code': 'c', 'state': 'forged'}).status_code == 400
    denied = local.get('/api/spotify/callback', params={'error': '<script>x</script>', 'state': _login_state(app)})
    assert denied.status_code == 400
    assert '<script>' not in denied.text
    assert '&lt;script&gt;' in denied.text
    assert spotify_http.calls == []


def test_oauth_login_without_credentials_says_how_to_fix_it(config: Config) -> None:
    app = create_app(replace(config, spotify_client_id='', spotify_client_secret=''))
    response = _loopback(app).get('/api/spotify/login')
    assert response.status_code == 503
    assert 'SPOTIFY_CLIENT_ID' in response.text


def test_behind_a_local_proxy_the_real_client_decides(live_server: str) -> None:
    """Uvicorn as __main__ runs it: X-Forwarded-For is believed from 127.0.0.1, so a LAN client stays one."""

    def login(forwarded_for: str | None) -> int:
        headers = {'X-Forwarded-For': forwarded_for} if forwarded_for else {}
        url = f'{live_server}/api/spotify/login'
        return requests.get(url, headers=headers, allow_redirects=False, timeout=5).status_code

    assert login('192.168.1.20') == 403
    assert login('127.0.0.1') == 307
    assert login(None) == 307
