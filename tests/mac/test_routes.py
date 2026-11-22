"""The route table is docs/CONTRACTS.md's, blocking routes are plain `def`, and every route answers."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

from mac_server import apps, health, scenes, studio
from mac_server.contracts import (
    Catalog,
    CommandPoll,
    ControlOut,
    DashboardState,
    Heartbeat,
    PlayOut,
    SettingsView,
    Volume,
)
from mac_server.routes import HELD_BY_APP, NOT_RUNNING, ROBOT_OFF
from mac_server.server import french_errors
from mac_server.store import Store
from tests.mac.conftest import KEY, flat_routes, longest_stall
from tests.mac.routes_table import BLOCKING, CORE, SUBJECTS


def _contracts(app: FastAPI) -> set[tuple[str, str]]:
    """The core table, plus the table of every subject whose router is built (a stub router has no route)."""
    mac = app.state.mac
    built = {
        name for name, module in (('studio', studio), ('scenes', scenes), ('apps', apps)) if module.routes(mac).routes
    }
    return CORE | {route for name in built for route in SUBJECTS[name]}


def _http_routes(app: FastAPI) -> dict[tuple[str, str], APIRoute]:
    return {(m, r.path): r for r in flat_routes(app) if isinstance(r, APIRoute) for m in r.methods}


def test_the_routes_are_the_contracts(app: FastAPI) -> None:
    assert set(_http_routes(app)) == _contracts(app)
    assert [r.path for r in flat_routes(app) if isinstance(r, APIWebSocketRoute)] == ['/api/ws']


def _documented() -> set[tuple[str, str]]:
    """The routes in the first column of docs/CONTRACTS.md's table, as it writes them: `GET /a` · `/b` (the same
    method, a sibling path), `GET` · `PUT /a` (two methods, one path), `{id}` for any id."""
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'CONTRACTS.md').read_text(encoding='utf-8')
    table = doc.split('\n## HTTP routes\n', 1)[1].split('\n## ', 1)[0]
    found: set[tuple[str, str]] = set()
    for row in table.splitlines():
        methods, method, path = [], '', ''
        for span in re.findall(r'`([^`]+)`', row.split('|')[1]) if row.startswith('| `') else []:
            if span.isupper():
                methods.append(span)
                continue
            said, _, at = span.rpartition(' ')
            at = at.split('?')[0]
            path = at if at.startswith('/api') else path.rsplit('/', 1)[0] + at
            method = said or method
            found |= {(m, path) for m in [*methods, method]}
            methods = []
    return found


def test_the_contracts_doc_lists_every_route() -> None:
    table = {(method, re.sub(r'\{\w+_id\}', '{id}', path)) for method, path in CORE.union(*SUBJECTS.values())}
    assert _documented() == table | {('WS', '/api/ws')}


def test_every_call_the_dashboard_sends_is_one_the_mac_takes(app: FastAPI) -> None:
    """frontend/tests/routes.test.ts writes out each call of the dashboard's API modules as it leaves: each names a
    route and a method the Mac serves (its first match, as the Mac routes it), with a body and a query the route's
    own request model takes."""
    calls = json.loads((Path(__file__).resolve().parents[2] / 'frontend/tests/dashboard-calls.json').read_text())
    routes = [route for route in flat_routes(app) if isinstance(route, APIRoute)]
    assert len(calls) >= 30
    for call in calls:
        name, (path, _, query) = call['call'], call['path'].partition('?')
        route = next((r for r in routes if call['method'] in r.methods and r.path_regex.match(path)), None)
        assert route is not None, f'{name}: the Mac serves no {call["method"]} {path}'
        if call['body'] is None:
            assert route.body_field is None or not route.body_field.field_info.is_required(), name
        else:
            assert route.body_field is not None, f'{name}: {route.path} takes no body'
            assert route.body_field.validate(call['body'], {}, loc=('body',))[1] == [], name
        params = {param.name: param for param in route.dependant.query_params}
        for key, value in parse_qsl(query, keep_blank_values=True):
            assert key in params, f'{name}: {route.path} takes no {key}'
            assert params[key].validate(value, {}, loc=('query', key))[1] == [], (name, key)


def test_blocking_routes_are_plain_def(app: FastAPI) -> None:
    for key, route in _http_routes(app).items():
        assert inspect.iscoroutinefunction(route.endpoint) == (key not in BLOCKING), key


def test_a_slow_settings_file_never_stalls_the_event_loop(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """The robot's heartbeat, events and long-poll answer while settings.json takes 0.3 s to read."""
    read = Store._raw
    monkeypatch.setattr(Store, '_raw', lambda store, **kw: (time.sleep(0.3), read(store, **kw))[1])

    async def scenario() -> float:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://mac', headers={'X-API-Key': KEY}) as http:
            calls = [
                http.get('/api/state'),  # reads the file, in the threadpool
                http.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's1'}),
                http.post('/api/robot/events', json={'kind': 'session_start', 'session_id': 's1'}),
                http.post('/api/session/say', json={'text': 'coucou'}),
                http.post('/api/session/restart'),
                http.post('/api/session/wake'),
                http.post('/api/session/sleep'),
                http.get('/api/robot/commands', params={'wait': 0}),
            ]

            async def answers() -> list[httpx.Response]:
                return await asyncio.gather(*calls)

            responses, stall = await longest_stall(answers())
        assert [r.status_code < 500 for r in responses] == [True] * len(calls)
        return stall

    assert asyncio.run(scenario()) < 0.2


def _valid(model: type[BaseModel], response: object) -> BaseModel:
    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    return model.model_validate(response.json())  # type: ignore[attr-defined]


def test_every_contract_route_answers(
    app: FastAPI, client: TestClient, spotify_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    spotify_http.token()
    computer = {'id': 'mac', 'name': 'Studio Mac', 'type': 'Computer', 'is_active': False}
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [computer]})
    item = {'name': 'Titre', 'uri': 'spotify:track:1', 'artists': [{'name': 'Artiste'}]}
    spotify_http.reply('GET', '/search', 200, {'tracks': {'items': [item]}})
    spotify_http.reply('GET', '/me/tracks', 200, {'items': [{'track': item}]})
    spotify_http.reply('PUT', '/me/player/play', 204).reply('PUT', '/me/player/pause', 204)

    _valid(SettingsView, client.get('/api/settings'))
    _valid(SettingsView, client.patch('/api/settings', json={'name': 'Bob'}))
    assert client.get('/api/voices/Achird/sample').status_code == 200
    _valid(DashboardState, client.get('/api/state'))
    assert client.post('/api/session/wake').status_code == 409  # no robot to wake
    assert client.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's1'}).status_code == 204
    for action in ('wake', 'sleep', 'restart'):
        assert client.post(f'/api/session/{action}').status_code == 204
    assert client.post('/api/robot/events', json={'kind': 'session_start', 'session_id': 's1'}).status_code == 204
    assert client.post('/api/session/say', json={'text': 'coucou'}).status_code == 204
    assert _valid(CommandPoll, client.get('/api/robot/commands', params={'wait': 0})).command is not None
    monkeypatch.setattr(health, 'probe', lambda host, port: True)  # a daemon that takes the volume
    monkeypatch.setattr(health, 'push_volume', lambda host, port, volume: True)
    app.state.mac.health.daemon_ok = True
    _valid(Volume, client.put('/api/volume', json={'volume': 50}))
    _valid(PlayOut, client.post('/api/spotify/play', json={'query': 'titre', 'kind': 'track'}))
    _valid(PlayOut, client.post('/api/spotify/play', json={'kind': 'liked'}))
    _valid(ControlOut, client.post('/api/spotify/control', json={'action': 'pause'}))
    with client.websocket_connect('/api/ws'):
        pass
    local = TestClient(app, client=('127.0.0.1', 50000), follow_redirects=False)
    assert local.get('/api/spotify/login').status_code == 307
    assert local.get('/api/spotify/callback', params={'state': 'unknown', 'code': 'c'}).status_code == 400


def test_a_try_needs_a_running_robot_and_queues_one_command(app: FastAPI) -> None:
    mac = app.state.mac
    client = TestClient(app, headers={'X-API-Key': KEY})  # no lifespan: no health probe to reset daemon_ok
    move = {'kind': 'animation', 'name': 'curious', 'mode': 'idle', 'tuning': {'amplitude': 1.4, 'speed': 1.0}}
    refused = client.post('/api/studio/try', json=move)
    assert (refused.status_code, refused.json()) == (409, {'detail': ROBOT_OFF})  # no daemon, no heartbeat
    mac.health.daemon_ok = True  # the robot is on, its agent is not running
    refused = client.post('/api/studio/try', json=move)
    assert (refused.status_code, refused.json()) == (409, {'detail': NOT_RUNNING})
    mac.health.beat(Heartbeat(awake=False))
    assert client.post('/api/studio/try', json=move).status_code == 204
    assert [c.move.name for _, c in mac.commands._pending if c.action == 'try'] == ['curious']
    too_loud = client.post('/api/studio/try', json={**move, 'mode': 'talking', 'name': 'calm'})
    assert too_loud.status_code == 422 and too_loud.json()['detail'] == 'Amplitude (parole)\xa0: entre 0,5 et 1.'
    unknown = client.post('/api/studio/try', json={'kind': 'gesture', 'name': 'moonwalk'})
    assert unknown.status_code == 422 and 'moonwalk' in unknown.json()['detail']


def test_a_try_is_refused_while_an_app_holds_the_robot(client: TestClient, app: FastAPI) -> None:
    """The robot drops every move but the app's while it is held (robot_agent/main.py `_try`): the Mac says so."""
    mac = app.state.mac
    mac.health.beat(Heartbeat(awake=True, session_id='s1'))
    held = {'kind': 'manette', 'session_id': 's1', 'state': 'on', 'profile': 'volant'}
    assert client.post('/api/robot/events', json=held).status_code == 204
    move = {'kind': 'gesture', 'name': 'nod'}
    refused = client.post('/api/studio/try', json=move)
    assert refused.status_code == 409
    assert refused.json() == {'detail': HELD_BY_APP}
    assert not [c for _, c in mac.commands._pending if c.action == 'try']
    let_go = {**held, 'state': 'off', 'profile': None, 'why': 'app'}
    assert client.post('/api/robot/events', json=let_go).status_code == 204
    assert client.post('/api/studio/try', json=move).status_code == 204
    assert [c.move.name for _, c in mac.commands._pending if c.action == 'try'] == ['nod']
    ear = {**held, 'profile': 'voix'}  # its ear alone is lent: the body stays the robot's
    assert client.post('/api/robot/events', json=ear).status_code == 204
    assert client.post('/api/studio/try', json=move).status_code == 204


def test_a_bound_reads_as_french_writes_it() -> None:
    """A float field's bounds come as floats: 20, not 20.0; 1,5, not 1.5. As the dashboard writes a figure: its
    thousands apart, a true minus."""
    errors = [
        {'type': 'less_than_equal', 'loc': ('query', 'wait'), 'ctx': {'le': 20.0}},
        {'type': 'greater_than_equal', 'loc': ('body', 'wait'), 'ctx': {'ge': -1500.5}},
    ]
    assert french_errors(errors) == 'Attente\xa0: au plus 20\xa0; attente\xa0: au moins \u22121\u202f500,5.'


def test_the_catalog_lists_what_the_robot_does(client: TestClient) -> None:
    catalog = Catalog.model_validate(client.get('/api/catalog').json())
    ids = {entry.id for entry in catalog.entries}
    assert {'gesture:dance', 'emotion:cheerful1', 'animation:idle:observer', 'session:restart'} <= ids
    assert [g.key for g in catalog.groups][:3] == ['session', 'gestures', 'emotions']


def test_the_dashboard_says_what_it_shows_while_its_socket_lives(client: TestClient, app: FastAPI) -> None:
    broker = app.state.mac.broker

    def until(check) -> None:
        deadline = time.monotonic() + 2
        while not check():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    with client.websocket_connect('/api/ws') as first, client.websocket_connect('/api/ws') as second:
        first.send_json({'subscribe': [], 'showing': {'kind': 'app', 'id': 'a1', 'since': 100.0}})
        until(lambda: broker.showing('app') == 'a1')
        second.send_json({'subscribe': [], 'showing': {'kind': 'app', 'id': 'b2', 'since': 200.0}})
        until(lambda: broker.showing('app') == 'b2')  # the latest shown wins
        first.send_json({'subscribe': ['pose'], 'showing': {'kind': 'app', 'id': 'a1', 'since': 100.0}})
        time.sleep(0.05)
        assert broker.showing('app') == 'b2'  # said again (a reconnect, a channel asked for): still older
        second.send_json({'subscribe': [], 'showing': {'kind': 'scene', 'id': 'antigone', 'since': 300.0}})
        until(lambda: broker.showing('app') == 'a1' and broker.showing('scene') == 'antigone')
        first.send_json({'subscribe': []})  # nothing shown any more; an old dashboard's message still parses
        until(lambda: broker.showing('app') is None)
    until(lambda: broker.showing('scene') is None)  # a closed dashboard shows nothing
