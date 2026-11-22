"""Apps on the Mac: the store, the engine (Claude faked), the CSP, the voice route and the bridge."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mac_server import app_build, apps, claude, store
from mac_server.config import Config
from mac_server.contracts import APP_MOTION_TTL_S, MANETTE_TTL_S, AppSummary, Catalog, Heartbeat, ManetteProfile
from mac_server.server import create_app
from robot_agent import animation_names as anim


def page(title: str = 'Snake', body: str = '<canvas></canvas>') -> str:
    return (
        f'<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>{title}</title></head>'
        f'<body>{body}<script>reachy.emotion("cheerful1"); reachy.regard(0, 0); reachy.antennes(10, 10);</script>'
        '</body></html>'
    )


class FakeProcess:
    """A `claude -p` stream: its answer as stream-json events, the result last; `gate` holds back the events after
    the first `free`."""

    def __init__(self, events: list[dict[str, Any]], gate: threading.Event | None, free: int = 0) -> None:
        self._events, self._gate, self._free, self._stop = events, gate, free, threading.Event()
        self.stdin = io.StringIO()
        self.stdin.close = lambda: None  # keep what was written readable
        self.returncode: int | None = None

    @property
    def stdout(self):
        for n, event in enumerate(self._events):
            if self._gate is not None and n >= self._free:
                while not self._gate.is_set() and not self._stop.is_set():
                    time.sleep(0.005)
            if self._stop.is_set():
                return
            yield json.dumps(event) + '\n'

    def terminate(self) -> None:
        self._stop.set()
        self.returncode = -15

    kill = terminate

    def wait(self) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class FakeClaude:
    """Stands in for `claude -p`: each run gets the next scripted answer, the last one repeating. A creation's
    answer is the text Claude writes; a retouch's (Read,Edit in its working dir) is the page it leaves in
    app.html, or a function of that page. An exception is raised at launch; a dict is Claude's result event."""

    def __init__(self) -> None:
        self.answers: list[Any] = [page()]
        self.calls: list[tuple[list[str], dict[str, Any], FakeProcess]] = []
        self.gate: threading.Event | None = None  # unset: the events wait for it
        self.free = 0  # the events of a run let through before the gate

    def __call__(self, args: list[str], **kwargs: Any) -> FakeProcess:
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        events: list[dict[str, Any]] = [{'type': 'system', 'subtype': 'init'}]
        result = {'type': 'result', 'result': 'Fait.', 'is_error': False, 'total_cost_usd': 0.01, 'duration_ms': 1}
        if isinstance(answer, dict):
            result |= answer
        elif args[args.index('--tools') + 1]:  # a retouch: the page left in app.html
            path = Path(kwargs['cwd']) / 'app.html'
            old = path.read_text()
            new = answer(old) if callable(answer) else answer
            path.write_text(new)
            events.append(
                {
                    'type': 'stream_event',
                    'event': {'type': 'content_block_start', 'content_block': {'type': 'tool_use', 'name': 'Edit'}},
                }
            )
            events.append({'type': 'user', 'tool_use_result': {'oldString': old[-30:], 'newString': new[-30:]}})
        else:
            events.append(
                {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'type': 'text'}}}
            )
            for at in range(0, len(answer), 200):
                delta = {'type': 'text_delta', 'text': answer[at : at + 200]}
                events.append({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': delta}})
            result['result'] = answer
        process = FakeProcess([*events, result], self.gate, self.free)
        self.calls.append((args, kwargs, process))
        return process

    def asked(self, n: int = -1) -> str:
        return self.calls[n][2].stdin.getvalue()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeClaude]:
    fake = FakeClaude()
    monkeypatch.setattr(claude.subprocess, 'Popen', fake)
    monkeypatch.setattr(claude.shutil, 'which', lambda name: f'/usr/local/bin/{name}')
    monkeypatch.setattr(app_build.app_check, 'run', lambda *_args, **_kit: fake_runs.pop(0) if fake_runs else [])
    yield fake
    # Every build the test started ends here, under these fakes: one left running would go on once they are
    # undone, and its repair would launch the real claude (and its check the real Chrome).
    if fake.gate is not None:
        fake.gate.set()
    assert not builds_running(), 'a build outlived its test'


def builds_running(app_id: str = '', timeout_s: float = 5.0) -> list[str]:
    """Waits for the threads of every build (of `app_id`'s) to end; the names of those still running."""
    deadline, prefix = time.monotonic() + timeout_s, f'app-{app_id}'
    for thread in threading.enumerate():
        if thread.name.startswith(prefix):
            thread.join(max(0.0, deadline - time.monotonic()))
    return [thread.name for thread in threading.enumerate() if thread.name.startswith(prefix)]


fake_runs: list[list[str]] = []  # what the next runs of a page in Chrome find (scripted per test)
CHECK, POPEN = app_build.app_check.run, subprocess.Popen  # the real ones, which `fake` stands in for


@pytest.fixture
def published(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []
    monkeypatch.setattr(app.state.mac.broker, 'publish', sent.append)
    return sent


@pytest.fixture
def events(published: list[Any]) -> list[Any]:
    """The `apps` channel's events, as they are published."""
    return Channel(published, 'apps')


class Channel(list):
    """A live view of one channel's data among what was published."""

    def __init__(self, published: list[Any], channel: str) -> None:
        super().__init__()
        self._published, self._channel = published, channel

    def _now(self) -> list[Any]:
        return [m.data for m in self._published if m.channel == self._channel]

    def __getitem__(self, i):
        return self._now()[i]

    def __iter__(self):
        return iter(self._now())

    def __len__(self) -> int:
        return len(self._now())

    def __bool__(self) -> bool:
        return bool(self._now())


def settled(client: TestClient, app_id: str) -> AppSummary:
    """The app once its build has ended: its threads gone, so how it ended is stored and published."""
    assert not builds_running(app_id), 'the build did not end'
    found = [a for a in client.get('/api/apps').json()['apps'] if a['id'] == app_id]
    assert found and found[0]['status'] != 'building'
    return AppSummary.model_validate(found[0])


def index(config: Config) -> list[dict[str, Any]]:
    return json.loads((config.apps_dir / 'index.json').read_text())['apps']


def awake(client: TestClient) -> None:
    assert client.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's1'}).status_code == 204


# ----- the store -----------------------------------------------------------------------------------------


def test_an_app_is_built_stored_renamed_and_deleted(client: TestClient, config: Config, fake, events) -> None:
    created = client.post('/api/apps', json={'request': 'Un Snake où tu comptes les points'})
    assert created.status_code == 200
    building = AppSummary.model_validate(created.json())
    assert (building.status, building.title) == ('building', 'Un Snake où tu comptes les points')
    ready = settled(client, building.id)
    assert (ready.status, ready.title, ready.error) == ('ready', 'Snake', None)
    assert (config.apps_dir / f'{building.id}.html').read_text() == page()
    assert index(config)[0]['prompt'] == 'Un Snake où tu comptes les points'
    assert [(e.app.status, e.open) for e in events] == [('building', False), ('ready', False)]

    renamed = client.patch(f'/api/apps/{building.id}', json={'title': 'Serpent'})
    assert renamed.json()['title'] == 'Serpent' and events[-1].app.title == 'Serpent'
    assert client.patch(f'/api/apps/{building.id}', json={'title': ''}).status_code == 422

    assert client.delete(f'/api/apps/{building.id}').status_code == 204
    assert events[-1].deleted and client.get('/api/apps').json() == {'apps': []}
    assert not (config.apps_dir / f'{building.id}.html').exists()
    assert client.delete(f'/api/apps/{building.id}').status_code == 404
    assert client.get(f'/api/apps/{building.id}/html').status_code == 404


def test_a_title_the_human_gave_outlives_the_builds(client: TestClient, fake) -> None:
    """Only a creation names the app after its page's <title>, and only if nobody renamed it while it was built."""
    fake.gate = threading.Event()
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    assert client.patch(f'/api/apps/{app_id}', json={'title': 'Le serpent de Léa'}).status_code == 200
    fake.gate.set()
    assert settled(client, app_id).title == 'Le serpent de Léa'  # renamed while built: the page's « Snake » is not

    fake.answers = [page('Pong')]
    other = client.post('/api/apps', json={'request': 'un Pong'}).json()['id']
    assert settled(client, other).title == 'Pong'
    assert client.patch(f'/api/apps/{other}', json={'title': 'Le pong de Léa'}).status_code == 200
    fake.answers = [lambda old: old.replace('<title>Pong</title>', '<title>Pong rapide</title>')]
    assert client.post(f'/api/apps/{other}/update', json={'request': 'plus rapide'}).status_code == 200
    assert settled(client, other).title == 'Le pong de Léa'  # a retouch renames nothing


def test_reads_never_fail(client: TestClient, config: Config) -> None:
    config.apps_dir.mkdir()
    (config.apps_dir / 'index.json').write_text('{not json')
    assert client.get('/api/apps').json() == {'apps': []}
    good = {'id': 'ab' * 8, 'title': 'Pong', 'status': 'ready', 'created_at': 1.0, 'updated_at': 2.0}
    bad = [{'id': '../../etc', 'title': 'x', 'status': 'ready', 'created_at': 1, 'updated_at': 1}, {'id': 3}, 'x']
    (config.apps_dir / 'index.json').write_text(json.dumps({'apps': [good, *bad]}))
    assert [a['title'] for a in client.get('/api/apps').json()['apps']] == ['Pong']
    assert client.get('/api/apps/..%2F..%2Fetc/html').status_code == 404
    assert client.get(f'/api/apps/{"ab" * 8}/html').status_code == 404  # in the index, no page on disk


def three_apps(config: Config) -> list[dict[str, Any]]:
    """Three ready apps on disk, their pages beside index.json; the entries, to write the index from."""
    config.apps_dir.mkdir()
    entries = [{'id': f'{n:016x}', 'title': f'Jeu {n}', 'status': 'ready', 'created_at': 1.0, 'updated_at': 1.0,
                'error': None, 'prompt': 'p'} for n in range(1, 4)]  # fmt: skip
    for entry in entries:
        (config.apps_dir / f'{entry["id"]}.html').write_text(page(entry['title']))
    return entries


@pytest.mark.parametrize('broken', ['a stray comma', 'an entry not read', 'an id twice'])
def test_an_index_not_read_in_full_is_never_written_over(
    client: TestClient, config: Config, fake: FakeClaude, broken: str
) -> None:
    entries = three_apps(config)
    text = {
        'a stray comma': json.dumps({'apps': entries})[:-2] + ',]}',
        'an entry not read': json.dumps({'apps': [*entries[:2], entries[2] | {'status': 'paused'}]}),
        'an id twice': json.dumps({'apps': [*entries, entries[0] | {'title': 'Jeu 1 bis'}]}),
    }[broken]
    (config.apps_dir / 'index.json').write_text(text)
    for answer in (
        client.post('/api/apps', json={'request': 'un snake'}),
        client.post('/api/apps/voice', json={'action': 'create', 'request': 'un pong'}),
        client.patch(f'/api/apps/{entries[0]["id"]}', json={'title': 'Autre'}),
        client.delete(f'/api/apps/{entries[1]["id"]}'),
        client.post(f'/api/apps/{entries[0]["id"]}/update', json={'request': 'en bleu'}),
    ):
        assert answer.status_code == 500, answer.json()
        assert answer.json()['detail'].startswith(f'Le fichier de tes apps ({config.apps_dir / "index.json"}) est')
    assert (config.apps_dir / 'index.json').read_text() == text
    assert not builds_running()
    assert sorted(p.name for p in config.apps_dir.iterdir()) == sorted([*(f'{e["id"]}.html' for e in entries),
                                                                        'index.json'])  # fmt: skip


def test_a_build_ending_on_an_index_broken_meanwhile_leaves_it_as_it_is(
    client: TestClient, config: Config, fake: FakeClaude, events: Channel
) -> None:
    entries = three_apps(config)
    (config.apps_dir / 'index.json').write_text(json.dumps({'apps': entries}))
    fake.gate = threading.Event()
    app_id = client.post('/api/apps', json={'request': 'un snake'}).json()['id']
    entries = index(config)
    entries[2]['status'] = 'paused'  # edited by hand during the build: an entry this version cannot read
    broken = json.dumps({'apps': entries})
    (config.apps_dir / 'index.json').write_text(broken)
    fake.gate.set()
    assert not builds_running(app_id)
    assert (config.apps_dir / 'index.json').read_text() == broken
    assert not (config.apps_dir / f'{app_id}.html').exists()
    assert (events[-1].app.status, events[-1].app.error[:24]) == ('failed', 'Le fichier de tes apps (')
    listed = next(a for a in client.get('/api/apps').json()['apps'] if a['id'] == app_id)
    assert (listed['status'], listed['error']) == (events[-1].app.status, events[-1].app.error)  # no restart


def test_a_key_another_version_wrote_hides_no_app_and_is_kept(
    client: TestClient, config: Config, fake: FakeClaude
) -> None:
    entries = three_apps(config)
    entries[0]['pinned'] = True  # a field another version wrote
    (config.apps_dir / 'index.json').write_text(json.dumps({'apps': entries}))
    assert sorted(a['title'] for a in client.get('/api/apps').json()['apps']) == ['Jeu 1', 'Jeu 2', 'Jeu 3']
    settled(client, client.post('/api/apps', json={'request': 'un snake'}).json()['id'])
    assert [e['title'] for e in index(config)] == ['Jeu 1', 'Jeu 2', 'Jeu 3', 'Snake']
    assert index(config)[0]['pinned'] is True
    assert client.get(f'/api/apps/{entries[0]["id"]}/html').status_code == 200


def test_the_index_is_written_atomically(client: TestClient, config: Config, fake, monkeypatch) -> None:
    app_id = client.post('/api/apps', json={'request': 'un Pong'}).json()['id']
    settled(client, app_id)
    before = (config.apps_dir / 'index.json').read_text()

    def crash(*_: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(store.os, 'replace', crash)
    with pytest.raises(OSError):
        client.patch(f'/api/apps/{app_id}', json={'title': 'Autre'})
    assert (config.apps_dir / 'index.json').read_text() == before
    assert sorted(p.name for p in config.apps_dir.iterdir()) == sorted([f'{app_id}.html', 'index.json'])


def test_a_build_cut_short_by_a_restart_is_not_left_building(client: TestClient, config: Config) -> None:
    config.apps_dir.mkdir()
    stale = {'title': 'x', 'status': 'building', 'created_at': 1.0, 'updated_at': 1.0}
    entries = [{**stale, 'id': 'a' * 16}, {**stale, 'id': 'b' * 16}]
    (config.apps_dir / 'index.json').write_text(json.dumps({'apps': entries}))
    (config.apps_dir / f'{"b" * 16}.html').write_text(page())
    listed = {a['id']: (a['status'], a['error']) for a in client.get('/api/apps').json()['apps']}
    assert listed == {'a' * 16: ('failed', apps.INTERRUPTED), 'b' * 16: ('ready', apps.INTERRUPTED)}


def test_the_servers_shutdown_ends_each_builds_claude_and_leaves_it_interrupted(
    app: FastAPI, config: Config, fake
) -> None:
    """The server stops mid-build: its claude stops writing for nobody, and the app reads as interrupted after the
    restart, not cancelled: a creation stays, to be built again."""
    (pong,) = _store(config, 'Pong')
    fake.gate = threading.Event()
    with TestClient(app, headers={'X-API-Key': 'test-key'}) as client:
        made = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
        assert client.post(f'/api/apps/{pong}/update', json={'request': 'en bleu'}).status_code == 200
        deadline = time.monotonic() + 2
        while len(fake.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    assert all(process.returncode is not None for *_, process in fake.calls), 'a claude outlived the server'
    assert not builds_running()
    with TestClient(create_app(config), headers={'X-API-Key': 'test-key'}) as again:
        listed = {a['id']: (a['status'], a['error']) for a in again.get('/api/apps').json()['apps']}
    assert listed == {pong: ('ready', apps.INTERRUPTED), made: ('failed', apps.INTERRUPTED)}


def test_the_servers_shutdown_ends_the_page_check_in_flight(
    app: FastAPI, fake, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The server stops while a page that hangs is run once: its Chrome goes with the server, its helpers too, though
    the build's thread never polls again (uvicorn re-raises the signal once its shutdown is done)."""
    started, helper, browser = tmp_path / 'chrome.pid', tmp_path / 'helper.pid', tmp_path / 'chrome'
    script = f'#!/bin/sh\nsleep 60 &\necho $! > {helper}\necho $$ > {started}\nexec sleep 60\n'
    browser.write_text(script, encoding='utf-8')
    browser.chmod(0o755)
    monkeypatch.setattr(app_build.app_check, 'run', CHECK)
    monkeypatch.setattr(app_build.app_check, 'subprocess', SimpleNamespace(Popen=POPEN, DEVNULL=subprocess.DEVNULL))
    monkeypatch.setattr(app_build.app_check, 'CHROME', browser)
    monkeypatch.setattr(app_build.app_check, 'POLL_S', 3.0)  # the build's thread asleep as the server stops

    def running(pid: int) -> bool:
        state = POPEN(['ps', '-o', 'stat=', '-p', str(pid)], stdout=subprocess.PIPE, text=True).communicate()[0]
        return state.strip()[:1] not in ('', 'Z')  # gone, or dead and not yet reaped ('Z+' in a terminal's group)

    with TestClient(app, headers={'X-API-Key': 'test-key'}) as client:
        client.post('/api/apps', json={'request': 'un Snake'})
        deadline = time.monotonic() + 2
        while not (started.is_file() and started.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.01)
        pid, helps = int(started.read_text()), int(helper.read_text())
        assert running(pid) and running(helps)
    deadline = time.monotonic() + 1
    while (running(pid) or running(helps)) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not running(pid), 'a page check outlived the server'
    assert not running(helps), "a page check's helper outlived the server"


def test_two_builds_at_most(client: TestClient, fake) -> None:
    fake.gate = threading.Event()
    first = client.post('/api/apps', json={'request': 'un'}).json()['id']
    client.post('/api/apps', json={'request': 'deux'})
    refused = client.post('/api/apps', json={'request': 'trois'})
    assert (refused.status_code, refused.json()['detail']) == (429, apps.BUSY)
    again = client.post(f'/api/apps/{first}/update', json={'request': 'plus vite'})
    assert again.status_code == 409 and 'déjà en construction' in again.json()['detail']
    fake.gate.set()
    settled(client, first)


def test_the_palette_lists_the_ready_apps(client: TestClient, fake) -> None:
    fake.answers = [page('Morpion')]
    ready = client.post('/api/apps', json={'request': 'un morpion'}).json()['id']
    settled(client, ready)
    fake.answers = ['pas de page']
    failed = client.post('/api/apps', json={'request': 'rien'}).json()['id']
    assert settled(client, failed).status == 'failed'
    catalog = Catalog.model_validate(client.get('/api/catalog').json())
    entries = [e for e in catalog.entries if e.id.startswith('app:')]
    assert [(e.label, e.say, e.tool, e.action.model_dump()) for e in entries] == [
        ('Morpion', 'ouvre Morpion', 'app', {'kind': 'app', 'app_id': ready})
    ]


# ----- the engine ----------------------------------------------------------------------------------------


def test_the_request_claude_gets(client: TestClient, config: Config, fake, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opus 5.5, streamed, no MCP, no session kept, never with an API key in its environment (that would bill API
    credits instead of the owner's subscription). A creation: Opus at high by default, no tool, an empty directory. A retouch:
    the same model and effort, only Read and Edit, fenced to a directory holding a copy of the page."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-must-not-leak')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'must-not-leak')
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    settled(client, app_id)
    args, kwargs, _ = fake.calls[0]
    assert args[:2] == ['/usr/local/bin/claude', '-p']
    flags = dict(zip(args[2::2], args[3::2], strict=False))
    assert (flags['--model'], flags['--effort'], flags['--output-format']) == (
        'claude-opus-5-5',
        'high',
        'stream-json',
    )  # the defaults
    assert args[args.index('--tools') + 1] == '' and '--permission-mode' not in args
    assert '--restricted' in args  # without it, a peer session the owner's other Claude Code sessions can message
    assert {'--strict-mcp-config', '--safe-mode', '--disable-slash-commands', '--no-session-persistence'} <= set(args)
    assert {'--include-partial-messages', '--verbose'} <= set(args)
    assert fake.asked() == 'Demande\xa0: «\xa0un Snake\xa0»'
    assert 'ANTHROPIC_API_KEY' not in kwargs['env'] and 'ANTHROPIC_AUTH_TOKEN' not in kwargs['env']
    prompt = args[args.index('--system-prompt') + 1]
    assert apps.CSP in prompt and 'reachy.regard' in prompt and '-30 (bord gauche de la page)' in prompt
    assert 'reachy.lancer' in prompt and 'postMessage' in prompt  # the toolbox, and no channel of the page's own
    assert all(name in prompt for name in [*anim.EMOTIONS, *anim.GESTURES])
    assert '1 message/s' not in prompt and 'par seconde' not in prompt  # the bridge paces, never the page

    seen: list[str] = []
    fake.answers = [lambda old: seen.append(old) or old.replace('Snake', 'Serpent violet')]
    client.post(f'/api/apps/{app_id}/update', json={'request': 'en violet'})
    settled(client, app_id)
    args, kwargs, _ = fake.calls[1]
    flag = lambda name: args[args.index(name) + 1]  # noqa: E731
    assert (flag('--effort'), flag('--tools'), flag('--permission-mode')) == ('high', 'Read,Edit', 'acceptEdits')
    assert '--restricted' in args and flag('--permission-prompts') == 'none'
    assert seen == [page()]  # a copy of the page, alone in Claude's directory
    assert (
        fake.asked() == "Demande d'origine\xa0: «\xa0un Snake\xa0»\nRetouche demandée\xa0: «\xa0en violet\xa0»"
    )  # not the page
    assert (
        'Edit' in args[args.index('--system-prompt') + 1]
        and 'Serpent violet' in (config.apps_dir / f'{app_id}.html').read_text()
    )


@pytest.mark.parametrize(
    ('answer', 'expected'),
    [
        (f'```html\n{page()}\n```', page()),
        (f'Voici ta page :\n{page()}\nBon jeu !', page()),
        (page().replace('<!DOCTYPE html>', ''), page().replace('<!DOCTYPE html>', '')),
        (page()[:-20], None),
        ('Désolé.', None),
    ],
)
def test_the_page_is_taken_out_of_the_answer(answer: str, expected: str | None) -> None:
    assert apps.extract(answer) == expected


@pytest.mark.parametrize(
    ('code', 'names'),
    [
        ('fetch("/x")', ['fetch']),
        ('window.fetch(u)', ['fetch']),
        ('self["fetch"](u)', ['fetch']),
        ('new XMLHttpRequest(); new WebSocket(u); new EventSource(u)', ['XMLHttpRequest', 'WebSocket', 'EventSource']),
        (
            'new RTCPeerConnection(); RTCDataChannel; new WebTransport(u)',
            ['RTCPeerConnection', 'RTCDataChannel', 'WebTransport'],
        ),
        ('new webkitRTCPeerConnection({iceServers: []})', ['webkitRTCPeerConnection']),  # Chrome's alias, still there
        ('const P = window.mozRTCPeerConnection;', ['mozRTCPeerConnection']),
        ('navigator.sendBeacon(u)', ['sendBeacon']),
        ('localStorage.x = 1; sessionStorage; indexedDB.open()', ['localStorage', 'sessionStorage', 'indexedDB']),
        ('document.cookie = "a"', ['document.cookie']),
        ('window.open("x")', ['window.open']),
        ('alert("x"); window.confirm("y"); prompt("z")', ['alert', 'confirm', 'prompt']),
        ('<img src="https://x.org/a.png">', ['une adresse externe']),
        ('<script src="//cdn.x/lib.js"></script>', ['une adresse externe']),
        ('<link rel="stylesheet" href="https://fonts.googleapis.com/css">', ['une adresse externe']),
        ('body { background: url(http://x.org/a.png) }', ['une adresse externe']),
        ('<video poster="https://x.org/a.png"></video>', ['une adresse externe']),
        ('<style>@import "https://x.org/a.css";</style>', ['une adresse externe']),
        ("<style>@import '//x.org/a.css';</style>", ['une adresse externe']),
    ],
)
def test_the_scan_names_what_the_sandbox_forbids(code: str, names: list[str]) -> None:
    reasons = apps.check(page(body=code))
    assert reasons == [f'elle utilise ce que la sandbox interdit\xa0: {", ".join(names)}']


@pytest.mark.parametrize(
    ('code', 'ways'),
    [
        ('restart.onclick = () => location.reload();', 'location.reload'),
        ('window.location.replace(u); document.location.assign(u)', 'location.replace, location.assign'),
        ('location.href = u; window.location = u', 'location.href, location'),
        ('location = "about:blank"', 'location'),
        ('location.search = "?r=1"; location.pathname = "/x"', 'location.search, location.pathname'),
        ('history.go(0)', 'history.go'),
        ('btn.onclick = location.reload.bind(location)', 'location.reload'),
    ],
)
def test_the_scan_refuses_a_page_that_reloads_or_leaves(code: str, ways: str) -> None:
    """The dashboard cuts the bridge as the frame's document goes: a « Rejouer » that reloads would stop the app.
    Both prompts say so, and the reason tells Claude what to do instead."""
    assert apps.check(page(body=code)) == [
        f'elle recharge ou quitte sa page ({ways}), ce que le tableau de bord arrête\xa0: une partie se recommence '
        'dans la page'
    ]
    assert 'location.reload' in app_build.PROMPT and 'location.reload' in app_build.RETOUCH


def test_the_scan_refuses_a_page_that_writes_its_own_channel_to_the_dashboard() -> None:
    """The toolbox served before every page (`window.reachy`) is the one channel: a protocol change is made there."""
    own = '<script>window.parent.postMessage({reachy: 1, type: "look", yaw: 0, pitch: 0}, "*")</script>'
    reasons = app_build.check(page(body=own))
    assert len(reasons) == 1 and 'boîte à outils `reachy`' in reasons[0]
    assert app_build.check(page(body='<script>reachy.regard(0, 0); reachy.lancer("tete", {})</script>')) == []


def test_the_scan_lets_a_page_name_a_location_of_its_own() -> None:
    code = (
        'let location = 1; places.map(location => location.x); location == p; player.location = p; location.hash = "a"'
    )
    assert apps.check(page(body=f'<script>{code}</script>')) == []


def test_the_prompt_paints_an_app_in_the_dashboards_colours() -> None:
    """« Accordé au tableau de bord »: each colour the prompt asks for is the dashboard's token for that role, the
    page's background its frame's own (`screen`), so an app shows no seam inside the dashboard."""
    theme = (Path(__file__).resolve().parents[2] / 'frontend' / 'src' / 'theme.ts').read_text()
    tokens = dict(re.findall(r"^ {4}'?([\w-]+)'?: '(#[0-9A-F]{6})'", theme, re.M))
    roles = {
        'fond': 'screen',
        'surfaces': 'plate',
        'texte': 'ink',
        'texte secondaire': 'ink-2',
        'accent jaune': 'speak',
    }
    style = next(line for line in app_build.PROMPT.splitlines() if 'Accordé au tableau de bord' in line)
    asked = [f'{role} {colour}' for role, colour in re.findall(r'([a-z][a-z ]*?) (#[0-9A-Fa-f]{6})', style)]
    assert asked == [f'{role} {tokens[token]}' for role, token in roles.items()]


def test_the_scan_leaves_ordinary_code_alone() -> None:
    code = (
        'game.fetch(); showPrompt(); ui.alert("x"); confirmMove(); const prompts = 1; '
        '<a href="#">x</a><img src="data:image/png;base64,AAAA"> '
        '<svg xmlns="http://www.w3.org/2000/svg"></svg> // Appuie sur Entrée pour confirmer '
        'text.replace("a", "b"); player.location.replace(a); const location = spot(); '
        'if (location.href === u || window.location == v) location.hash = "#fin";'
    )
    assert apps.check(page(body=code)) == []
    assert apps.check('x' * (app_build.PAGE_MAX + 1)) == ['la page dépasse 400 Ko']


def test_the_settings_choose_who_writes_the_apps(client: TestClient, fake) -> None:
    assert client.patch('/api/settings', json={'apps_model': 'sonnet', 'apps_effort': 'xhigh'}).status_code == 200
    app_id = client.post('/api/apps', json={'request': 'un Pong'}).json()['id']
    settled(client, app_id)
    fake.answers = [lambda old: old.replace('Snake', 'Pong')]
    client.patch('/api/settings', json={'apps_model': 'haiku', 'apps_effort': 'low'})  # at once, no wake needed
    client.post(f'/api/apps/{app_id}/update', json={'request': 'en bleu'})
    settled(client, app_id)
    flags = [(args[args.index('--model') + 1], args[args.index('--effort') + 1]) for args, _, _ in fake.calls]
    assert flags == [('claude-sonnet-5-5', 'xhigh'), ('claude-haiku-5-5', 'low')]
    refused = client.patch('/api/settings', json={'apps_model': 'gpt', 'apps_effort': 'max'})
    assert refused.status_code == 422


def test_a_refused_page_is_repaired_once_by_editing_it(client: TestClient, fake, published) -> None:
    refused = page(body='<script>localStorage.best = 3</script>')
    fake.answers = [{'result': refused, 'total_cost_usd': 0.03}, page('Snake réparé')]
    app = settled(client, client.post('/api/apps', json={'request': 'un Snake'}).json()['id'])
    assert (app.status, app.title) == ('ready', 'Snake réparé')
    assert Channel(published, 'app_build')[-1].cost_usd == 0.04  # both runs, the repair's 0.01 (FakeClaude)
    args, _, _ = fake.calls[1]
    repair = fake.asked()
    assert len(fake.calls) == 2 and args[args.index('--tools') + 1] == 'Read,Edit'
    assert 'localStorage' in repair and 'Demande\xa0: «\xa0un Snake\xa0»' in repair and 'a été refusée' in repair


def test_what_breaks_once_run_goes_back_too(client: TestClient, fake) -> None:
    fake_runs[:] = [['le test «\xa0score\xa0» donne 0.0 au lieu de 1.0'], []]
    fake.answers = [page(), page('Snake corrigé')]
    app = settled(client, client.post('/api/apps', json={'request': 'un Snake'}).json()['id'])
    assert (app.status, app.title) == ('ready', 'Snake corrigé')
    assert 'le test «\xa0score\xa0» donne 0.0 au lieu de 1.0' in fake.asked()


def test_a_page_refused_twice_fails_with_the_reason(client: TestClient, fake) -> None:
    fake.answers = [page(body='<script>fetch("/api")</script>')]
    app = settled(client, client.post('/api/apps', json={'request': 'un Snake'}).json()['id'])
    assert app.status == 'failed' and len(fake.calls) == 2
    assert app.error == 'La page a été refusée\xa0: elle utilise ce que la sandbox interdit\xa0: fetch.'


def test_a_cut_page_is_written_again_whole(client: TestClient, fake) -> None:
    fake.answers = [page()[:-30], page('Complet')]
    app = settled(client, client.post('/api/apps', json={'request': 'un Snake'}).json()['id'])
    assert (app.status, app.title) == ('ready', 'Complet')
    assert 'COMPLÈTE' in fake.asked() and fake.calls[1][0][fake.calls[1][0].index('--tools') + 1] == ''

    fake.answers = [page()[:-30], page(body='<script>localStorage.best = 3</script>')]
    app = settled(client, client.post('/api/apps', json={'request': 'un Pong'}).json()['id'])
    assert len(fake.calls) == 4  # written again whole was its one repair: refused then, it fails
    assert (app.status, app.error) == (
        'failed',
        'La page a été refusée\xa0: elle utilise ce que la sandbox interdit\xa0: localStorage.',
    )


def test_a_retouch_that_changes_nothing_says_so(client: TestClient, config: Config, fake) -> None:
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    settled(client, app_id)
    fake.answers = [lambda old: old]
    client.post(f'/api/apps/{app_id}/update', json={'request': 'rien'})
    assert settled(client, app_id).error == "Claude n'a rien changé à la page."


@pytest.mark.parametrize(
    ('failure', 'error'),
    [
        (OSError('exec format error'), "Claude n'a pas pu être lancé sur ce Mac."),
        (RuntimeError('boom'), 'La construction a planté\xa0: regarde les logs du Mac.'),
        ({'result': 'Credit balance is too low', 'is_error': True}, "Claude n'a pas écrit la page (Credit balance"),
    ],
)
def test_a_failed_call_fails_the_build_with_a_french_reason(client: TestClient, fake, failure, error) -> None:
    fake.answers = [failure]
    app = settled(client, client.post('/api/apps', json={'request': 'un Snake'}).json()['id'])
    assert app.status == 'failed' and app.error.startswith(error)


def test_a_run_that_takes_too_long_is_stopped(client: TestClient, fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_build, 'CREATE_TIMEOUT_S', 0.1)
    fake.gate = threading.Event()  # never set: Claude never answers
    app = settled(client, client.post('/api/apps', json={'request': 'un Snake'}).json()['id'])
    assert (app.status, app.error) == ('failed', "Claude n'a pas fini en 0 minutes.")


def test_a_missing_claude_is_said(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude.shutil, 'which', lambda _name: None)
    with pytest.raises(apps.BuildError, match='La commande claude est introuvable'):
        claude.Run('x', 'y', effort='high', timeout_s=1, what='la page').wait()


def test_a_failed_update_keeps_the_page_that_works(client: TestClient, config: Config, fake) -> None:
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    settled(client, app_id)
    fake.answers = [OSError('gone')]
    client.post(f'/api/apps/{app_id}/update', json={'request': 'plus vite'})
    app = settled(client, app_id)
    assert (app.status, app.error) == ('ready', "Claude n'a pas pu être lancé sur ce Mac.")
    assert (config.apps_dir / f'{app_id}.html').read_text() == page()


UNSAVED = "La page n'a pas pu être enregistrée\xa0: regarde les logs du Mac."


def unwritable(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The build thread's writes through `apps.<name>` fail as on a full disk; the routes' still go through."""
    real = getattr(apps, name)

    def write(*args: Any) -> None:
        if threading.current_thread().name.startswith('app-'):
            raise OSError(28, 'No space left on device')
        real(*args)

    monkeypatch.setattr(apps, name, write)


def heard(events: Channel) -> tuple[str, str | None]:
    """How the last `apps` event says the build ended (its thread publishes once out of the lock)."""
    deadline = time.monotonic() + 5
    while events[-1].app.status == 'building':
        assert time.monotonic() < deadline, 'the dashboard never heard the build end'
        time.sleep(0.01)
    return events[-1].app.status, events[-1].app.error


def test_a_page_that_cannot_be_written_fails_the_build(
    client: TestClient, config: Config, fake, events, monkeypatch
) -> None:
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    settled(client, app_id)
    unwritable(monkeypatch, 'write_text_atomic')
    fake.answers = [page('Snake rapide')]
    client.post(f'/api/apps/{app_id}/update', json={'request': 'plus vite'})
    assert heard(events) == ('ready', UNSAVED)  # the old page stays
    assert (config.apps_dir / f'{app_id}.html').read_text() == page()
    created = client.post('/api/apps', json={'request': 'un Pong'}).json()['id']
    assert heard(events) == ('failed', UNSAVED)
    assert settled(client, created).error == UNSAVED  # never a restart that did not happen
    assert next(a for a in index(config) if a['id'] == created)['status'] == 'failed'


def test_an_index_that_cannot_be_written_still_ends_the_build(
    client: TestClient, config: Config, fake, events, monkeypatch
) -> None:
    unwritable(monkeypatch, 'write_json_atomic')
    app_id = client.post('/api/apps', json={'request': 'un serpent'}).json()['id']
    assert heard(events) == ('ready', None)  # the page is written
    listed = client.get('/api/apps').json()['apps'][0]
    assert (listed['status'], listed['error'], listed['title']) == ('ready', None, 'Snake')  # no restart
    client.patch(f'/api/apps/{app_id}', json={'title': 'Serpent'})  # the next write the index takes keeps the end
    assert [(e['status'], e['title']) for e in index(config)] == [('ready', 'Serpent')]


# ----- the build as it goes ------------------------------------------------------------------------------


def test_a_build_shows_its_progress_and_its_draft(client: TestClient, fake, published) -> None:
    progress = Channel(published, 'app_build')
    fake.gate = threading.Event()
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    assert client.get(f'/api/apps/{app_id}/draft').status_code == 404  # nothing written yet
    deadline = time.monotonic() + 2
    while not progress and time.monotonic() < deadline:
        time.sleep(0.01)
    assert progress[0].phase == 'starting' and progress[0].app_id == app_id
    fake.gate.set()
    settled(client, app_id)
    shown = list(progress)
    assert shown[-1].phase in ('writing', 'checking') and len(shown) <= 3 + 1  # throttled: never one per token
    assert any(s.chars and '</html>' in s.tail for s in shown) or shown[-1].chars == len(page())


def test_the_page_so_far_is_served_under_the_csp(client: TestClient, fake) -> None:
    fake.gate, fake.free = threading.Event(), 3  # the init, the text block, its first 200 characters
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    deadline = time.monotonic() + 2
    while (draft := client.get(f'/api/apps/{app_id}/draft')).status_code == 404 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert draft.status_code == 200
    assert draft.json() == {'id': app_id, 'html': apps.served(page()[:200])}
    assert draft.json()['html'].startswith(META) and len(page()) > 200  # a part, its CSP first
    fake.gate.set()
    settled(client, app_id)
    assert client.get(f'/api/apps/{app_id}/draft').status_code == 404  # built: the page itself is served


def test_a_creation_being_repaired_shows_and_serves_the_page_it_edits(client: TestClient, fake, published) -> None:
    written, repaired = page(body='<script>localStorage.best = 3</script>'), page('Snake réparé')
    served: list[Any] = []

    def repair(_old: str) -> str:  # inside the repair's run: its progress is the build's by now
        building = client.get('/api/apps').json()['apps'][0]['id']
        served.append(client.get(f'/api/apps/{building}/draft').json())
        return repaired

    fake.answers = [written, repair]
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    assert settled(client, app_id).title == 'Snake réparé'
    assert served == [{'id': app_id, 'html': apps.served(written)}]
    repairing = [(b.phase, b.chars, b.tail) for b in Channel(published, 'app_build') if b.round == 2]
    assert repairing and all(chars and tail for _, chars, tail in repairing)
    assert repairing[-1] == ('checking', len(repaired), repaired)  # the repaired page, run once to check it


def test_a_builds_last_state_is_never_dropped() -> None:
    sent: list[Any] = []
    build = app_build.Build('ab' * 8, 'create', sent.append)

    def until(done: Any) -> None:
        deadline = time.monotonic() + 2
        while not done() and time.monotonic() < deadline:
            time.sleep(0.005)

    build._phase = 'writing'
    build._dirty.set()
    until(lambda: len(sent) == 2)  # the publisher now waits out its pace
    build._phase = 'checking'
    build._end()
    until(lambda: sent[-1].phase == 'checking')
    assert [s.phase for s in sent] == ['starting', 'writing', 'checking']


def test_a_draft_is_the_page_so_far_under_its_csp(fake) -> None:
    build = app_build.Build('ab' * 8, 'create', lambda _snapshot: None)
    build._progress.text = 'Voici la page :\n<!DOCTYPE html><html><body><p>Jou'
    assert build.draft() == '<!DOCTYPE html><html><body><p>Jou'
    assert build.snapshot().chars == len(build.draft()) and build.snapshot().tail.endswith('<p>Jou')
    build._end()


def test_a_cancelled_creation_goes_and_a_cancelled_retouch_keeps_the_page(
    client: TestClient, config: Config, fake, events
) -> None:
    assert client.post(f'/api/apps/{"ab" * 8}/cancel').status_code == 409
    fake.gate = threading.Event()
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    assert client.post(f'/api/apps/{app_id}/cancel').status_code == 204
    deadline = time.monotonic() + 2
    while client.get('/api/apps').json()['apps'] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.get('/api/apps').json()['apps'] == [] and events[-1].deleted

    fake.gate.set()
    kept = client.post('/api/apps', json={'request': 'un Pong'}).json()['id']
    settled(client, kept)
    fake.gate.clear()
    client.post(f'/api/apps/{kept}/update', json={'request': 'plus vite'})
    assert client.post(f'/api/apps/{kept}/cancel').status_code == 204
    app = settled(client, kept)
    assert (app.status, app.error) == ('ready', apps.CANCELLED)
    assert (config.apps_dir / f'{kept}.html').read_text() == page()


def test_a_cancel_once_the_page_passed_its_checks_still_writes_nothing(
    client: TestClient, app: FastAPI, config: Config, fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """« Annuler » between the page's last check and its saving: the creation goes, the retouch keeps the old page."""

    def cancelled_once_checked(kind: str) -> None:
        built = getattr(app_build.Build, kind)

        def then_cancelled(build: app_build.Build, *args: str) -> str:
            written = built(build, *args)
            app.state.mac.apps.cancel(build.app_id)  # the owner's click, every check passed
            return written

        monkeypatch.setattr(app_build.Build, kind, then_cancelled)

    kept = client.post('/api/apps', json={'request': 'un Pong'}).json()['id']
    settled(client, kept)
    cancelled_once_checked('create')
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    assert not builds_running(app_id)
    assert [a['id'] for a in client.get('/api/apps').json()['apps']] == [kept]
    assert not (config.apps_dir / f'{app_id}.html').exists()

    fake.answers = [lambda old: old.replace('</body>', '<p>plus vite</p></body>')]
    cancelled_once_checked('retouch')
    client.post(f'/api/apps/{kept}/update', json={'request': 'plus vite'})
    done = settled(client, kept)
    assert (done.status, done.error) == ('ready', apps.CANCELLED)
    assert (config.apps_dir / f'{kept}.html').read_text() == page()


# ----- serving -------------------------------------------------------------------------------------------


META = f'<meta http-equiv="Content-Security-Policy" content="{apps.CSP}">'


def test_the_csp_lets_nothing_in_from_outside_the_page() -> None:
    """Each directive at most as open as this; any other falls back to default-src, which allows nothing."""
    widest = {
        'default-src': {"'none'"},
        'script-src': {"'unsafe-inline'"},  # no host, no 'unsafe-eval'
        'style-src': {"'unsafe-inline'"},
        'img-src': {'data:', 'blob:'},
        'media-src': {'data:', 'blob:'},
        'font-src': {'data:', 'blob:'},
        'connect-src': {"'none'"},
    }
    directives = [part.split() for part in apps.CSP.split(';') if part.strip()]
    policy = {name.lower(): set(sources) for name, *sources in directives}
    assert len(policy) == len(directives)  # a directive given twice: browsers keep the first, a reader the last
    assert policy['default-src'] == policy['connect-src'] == {"'none'"}
    for name, sources in policy.items():
        assert sources and sources <= widest.get(name, set()), f'{name} {sources}'


@pytest.mark.parametrize(
    'source',
    [
        page(),
        '<!doctype html>\n<HTML lang="fr">\n  <HEAD data-x="1"><title>x</title></HEAD><body></body></HTML>',
        '<!DOCTYPE html><html><body><p>sans head</p></body></html>',
        '<!DOCTYPE html><html><!-- <head> --><head><title>x</title></head></html>',
        # Pages a guess at where <head> starts gets wrong: a quoted `>`, a custom element, a character Python
        # takes for a space and HTML does not, a byte-order mark, a comment first, neither <html> nor <head>.
        '<!DOCTYPE html><html lang="x>"><head><title>x</title></head><body></body></html>',
        '<!DOCTYPE html><html lang="fr"><head data-x=">"><title>x</title></head></html>',
        '<!DOCTYPE html><html lang="fr"><head-x></head-x><body></body></html>',
        '<!DOCTYPE html>\v<html lang="fr"><head><title>x</title></head></html>',
        '<html lang="fr">\xa0<head><title>x</title></head></html>',
        '\ufeff<!DOCTYPE html><html lang="fr"><head><title>x</title></head></html>',
        '<!-- page --><!DOCTYPE html><html lang="fr"><head><title>x</title></head></html>',
        '<!DOCTYPE html><title>x</title><p>sans html ni head</p>',
    ],
)
def test_the_csp_comes_first(source: str) -> None:
    """Before everything: a srcdoc is never in quirks mode, so the parser opens <html> and <head> for the meta and
    it is the head's first element, whatever the page's own tags say. The toolbox comes next, before the page's own
    scripts, and the page as it was written."""
    served = apps.served(source)
    assert served == META + app_build.kit() + source
    toolbox = app_build.kit()
    assert toolbox.startswith('<script>') and toolbox.endswith('</script>') and toolbox.count('</script') == 1
    assert '__' not in toolbox  # its lists are filled: the contracts' own
    for name in (*anim.EMOTIONS, *anim.GESTURES, *get_args(ManetteProfile)):
        assert f'"{name}"' in toolbox


def test_the_page_is_served_with_its_csp(client: TestClient, fake) -> None:
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    settled(client, app_id)
    served = client.get(f'/api/apps/{app_id}/html').json()
    assert served['id'] == app_id and served['html'] == apps.served(page())
    assert "connect-src 'none'" in served['html']


# ----- voice ---------------------------------------------------------------------------------------------


def test_an_app_asked_for_by_voice_shows_at_once(client: TestClient, fake, events) -> None:
    """The dashboard opens it while it is being built, to watch it come; its end is only announced."""
    answer = client.post('/api/apps/voice', json={'action': 'create', 'request': 'un Snake'}).json()
    assert answer['message'].startswith("Je construis l'app") and answer['app']['status'] == 'building'
    assert 'pas encore prête' in answer['message']  # the model once said « je viens de terminer » 5 s in
    settled(client, answer['app']['id'])
    assert [(e.app.status, e.open) for e in events] == [('building', True), ('ready', False)]
    empty = client.post('/api/apps/voice', json={'action': 'create'}).json()
    assert empty['app'] is None and 'Demande ce que' in empty['message']


def test_a_dashboard_build_opens_nothing(client: TestClient, fake, events) -> None:
    settled(client, client.post('/api/apps', json={'request': 'un Pong'}).json()['id'])
    assert [e.open for e in events] == [False, False]


def _built(client: TestClient, fake: FakeClaude, title: str) -> str:
    fake.answers = [page(title)]
    app_id = client.post('/api/apps', json={'request': title}).json()['id']
    settled(client, app_id)
    return app_id


def test_update_and_open_by_voice_find_the_app_as_said(client: TestClient, fake, events) -> None:
    morpion = _built(client, fake, 'Morpion élégant')
    snake = _built(client, fake, 'Snake')

    fake.answers = [page('Morpion élégant', '<p>v2</p>')]
    fake.gate = threading.Event()  # the retouch cannot end before its first event is read
    out = client.post('/api/apps/voice', json={'action': 'update', 'name': 'le morpion elegant', 'request': 'en bleu'})
    assert out.json()['app']['id'] == morpion and 'Je modifie «\xa0Morpion élégant\xa0»' in out.json()['message']
    assert (events[-1].app.id, events[-1].app.status, events[-1].open) == (morpion, 'building', True)
    fake.gate.set()
    settled(client, morpion)
    assert fake.asked() == "Demande d'origine\xa0: «\xa0Morpion élégant\xa0»\nRetouche demandée\xa0: «\xa0en bleu\xa0»"

    with client.websocket_connect('/api/ws'):  # a dashboard to open it on
        opened = client.post('/api/apps/voice', json={'action': 'open', 'name': 'snak'}).json()
    assert opened['app']['id'] == snake and opened['message'] == '«\xa0Snake\xa0» est sur le dashboard.'
    assert (events[-1].app.id, events[-1].open) == (snake, True)

    client.get(f'/api/apps/{snake}/html')  # the dashboard opened it after the morpion's update
    fake.answers = [page('Snake', '<p>niveau 2</p>')]
    unnamed = client.post('/api/apps/voice', json={'action': 'update', 'request': 'ajoute un niveau'}).json()
    assert unnamed['app']['id'] == snake
    settled(client, snake)
    assert len(client.get('/api/apps').json()['apps']) == 2  # an update never makes a new app


# ----- designation: which app a spoken name means ---------------------------------------------------------


def _store(config: Config, *titles: str) -> list[str]:
    """Ready apps with these titles, the last one changed last; their ids."""
    config.apps_dir.mkdir(exist_ok=True)
    ids = [f'{i:016x}' for i in range(1, len(titles) + 1)]
    entries = [
        {'id': app_id, 'title': title, 'status': 'ready', 'created_at': float(n), 'updated_at': float(n)}
        for n, (app_id, title) in enumerate(zip(ids, titles, strict=True))
    ]
    (config.apps_dir / 'index.json').write_text(json.dumps({'apps': entries}))
    for app_id, title in zip(ids, titles, strict=True):
        (config.apps_dir / f'{app_id}.html').write_text(page(title))
    return ids


def _show(app: FastAPI, app_id: str | None) -> None:
    app.state.mac.broker.show('a dashboard', None if app_id is None else (time.time(), 'app', app_id))


@pytest.mark.parametrize(
    'said',
    ['Snake Croc Pomme', "l'app de Snake Croc Pomme qu'on vient de faire", 'snake croque pommes', 'le snake croque'],
)
def test_a_retouch_finds_the_app_meant_not_an_older_one_whose_title_it_contains(
    app: FastAPI, config: Config, said: str
) -> None:
    """2026-11-03: « Snake » scored above « Snake croque-pommes » because « snake » is in « snake croc pomme »."""
    _old, meant = _store(config, 'Snake', 'Snake croque-pommes')
    assert app.state.mac.apps.designate(said).id == meant


def test_a_name_both_titles_hold_means_the_one_on_screen_else_the_exact_title(app: FastAPI, config: Config) -> None:
    snake, croque = _store(config, 'Snake', 'Snake croque-pommes')
    apps_ = app.state.mac.apps
    assert apps_.designate('le snake').id == snake  # nothing on screen: the title said exactly
    _show(app, croque)
    assert apps_.designate('le snake').id == croque  # « le snake » while the croque-pommes is open
    _show(app, None)
    assert apps_.designate('snake').id == snake


def test_close_titles_with_nothing_on_screen_are_asked_about(client: TestClient, app: FastAPI, config: Config) -> None:
    elegant, three_d = _store(config, 'Morpion élégant', 'Morpion 3D')
    candidates = app.state.mac.apps.designate('le morpion')
    assert [a.id for a in candidates] == [three_d, elegant]  # the latest first
    out = client.post('/api/apps/voice', json={'action': 'update', 'name': 'le morpion', 'request': 'en bleu'}).json()
    assert out == {'message': 'Plusieurs apps peuvent correspondre\xa0: «\xa0Morpion 3D\xa0» ou «\xa0Morpion élégant\xa0». '
                   'Demande laquelle.', 'app': None}  # fmt: skip
    _show(app, elegant)
    assert app.state.mac.apps.designate('le morpion').id == elegant
    assert app.state.mac.apps.designate('morpion 3d').id == three_d  # a name that says which wins over the screen


def test_the_same_title_twice_means_the_latest(app: FastAPI, config: Config) -> None:
    _first, latest, italiano = _store(config, 'Tetris', 'Tetris', 'Tetris Italiano')
    assert app.state.mac.apps.designate('le tétris').id == latest
    assert app.state.mac.apps.designate('tetris italiano').id == italiano


def test_no_name_means_the_app_on_screen_else_the_last_one_touched(app: FastAPI, config: Config, client) -> None:
    pong, snake, tetris = _store(config, 'Pong', 'Snake', 'Tetris')
    apps_ = app.state.mac.apps
    assert apps_.designate('').id == tetris
    client.get(f'/api/apps/{pong}/html')  # opened since
    assert apps_.designate('celle-ci').id == pong
    _show(app, snake)
    assert apps_.designate('').id == snake
    assert apps_.designate('le pong').id == pong
    assert apps_.designate('le casse-briques') is None
    assert apps_.designate('le cas') is None  # sounds like « ça », yet names something


def test_the_screen_breaks_a_tie_never_a_clearer_name(app: FastAPI, config: Config) -> None:
    """Found in review: « le pendu » while « Panda qui danse » was shown gave the panda (pendu ~ panda, 0.8)."""
    pendu, panda, _test = _store(config, 'Pendu', 'Panda qui danse', 'Test de réflexes')
    _show(app, panda)
    assert app.state.mac.apps.designate('le pendu').id == pendu
    assert app.state.mac.apps.designate('la tête') is None  # « tête » is no « test »


def test_accents_and_words_said_around_a_name(app: FastAPI, config: Config) -> None:
    snake, des = _store(config, 'Snake', 'Lancer de dés')
    apps_ = app.state.mac.apps
    assert apps_.designate('les dés').id == des  # « dés » is no article
    assert apps_.designate("l'app de Snake qu'on a faite tout à l'heure").id == snake
    assert apps_.designate('le snake que tu as fait hier').id == snake
    _show(app, snake)
    assert apps_.designate("l'app ouverte").id == snake  # names nothing: the one on screen


@pytest.mark.parametrize(
    'said',
    [
        'celle qui est ouverte',
        "celle sur l'écran",
        'celle que tu viens de faire',
        "l'app que tu as créée",
        'la nouvelle app',
        'la dernière',
        'celle du dashboard',
        'ça',
        "c'est celle-ci",
    ],
)
def test_the_app_on_screen_said_another_way_names_nothing(app: FastAPI, config: Config, said: str) -> None:
    snake, pong = _store(config, 'Snake', 'Pong')
    assert getattr(app.state.mac.apps.designate(said), 'id', None) == pong  # nothing on screen: the latest
    _show(app, snake)
    assert getattr(app.state.mac.apps.designate(said), 'id', None) == snake


@pytest.mark.parametrize(
    ('titles', 'shown', 'said', 'meant'),
    [
        (('Dernier Pong', 'Pong'), None, 'dernier pong', 'Dernier Pong'),
        (('Dernier Pong', 'Pong'), 'Pong', 'le dernier pong', 'Dernier Pong'),  # a clearer name than the screen
        (('Pong', 'Dernier Pong'), None, 'pong', 'Pong'),
        (('Partie', 'Nouvelle partie'), None, 'partie', 'Partie'),
        (('Nouvelle partie', 'Partie'), None, 'la nouvelle partie', 'Nouvelle partie'),
        (('Morpion', 'Morpion sur le dashboard'), None, 'morpion', 'Morpion'),
        (('Morpion géant', 'Snake'), 'Snake', 'le morpion qui est sur le dashboard', 'Morpion géant'),
        (('Morpion géant', 'Dernier Pong'), None, 'le morpion que tu viens de créer', 'Morpion géant'),
        (('C\u2019est parti', 'Morpion'), 'Morpion', "c'est le morpion", 'Morpion'),
        (('C\u2019est parti', 'Morpion'), None, "c'est le morpion", 'Morpion'),
        (('C\u2019est parti', 'Pong', 'Snake'), 'Pong', "c'est le pong", 'Pong'),
        (('C\u2019est parti', 'Morpion'), 'Morpion', "c'est parti", 'C\u2019est parti'),
        (('Sur la lune', 'Morpion'), None, 'le morpion qui est sur le dashboard', 'Morpion'),
        (('Dashboard météo', 'Morpion'), None, 'le morpion sur le dashboard', 'Morpion'),
        (('Dernier Pong', 'Snake'), None, 'le dernier snake que tu viens de créer', 'Snake'),
        (('Ça pousse', 'Snake'), None, "ça, c'est le snake", 'Snake'),
        (('Dashboard', 'Snake'), 'Snake', 'le dashboard', 'Dashboard'),
        (('Snake', 'Snake sur la plage'), 'Snake', 'le snake qui est affiché sur le dashboard en ce moment', 'Snake'),
        (('Snake', 'Snake sur la plage'), None, 'le snake qui est sur le dashboard en ce moment', 'Snake'),
        (
            ('Morpion géant', 'Morpion géant sur Mars'),
            None,
            "le morpion géant qui est sur le dashboard pour les enfants de l'école",
            'Morpion géant',
        ),
    ],
)
def test_a_word_that_points_at_an_app_is_a_word_of_the_title_that_holds_it(
    app: FastAPI, config: Config, titles: tuple[str, ...], shown: str | None, said: str, meant: str
) -> None:
    """« la dernière » names nothing, yet « Dernier Pong » is no « Pong »: the name said decides, and the pointing
    words around it only choose between titles it names alike."""
    ids = dict(zip(titles, _store(config, *titles), strict=True))
    _show(app, shown and ids[shown])
    found = app.state.mac.apps.designate(said)
    assert getattr(found, 'id', found) == ids[meant]


def test_a_title_made_of_words_said_around_a_name_is_named_by_saying_it(app: FastAPI, config: Config, client) -> None:
    jeu, snake, le_jeu, _pong = _store(config, 'Mon jeu', 'Snake', 'Le Jeu', 'Pong')
    apps_ = app.state.mac.apps
    _show(app, snake)
    assert apps_.designate('mon jeu').id == jeu  # neither the app on screen nor the latest
    assert apps_.designate('le jeu').id == le_jeu
    assert apps_.designate('celle-ci').id == snake  # still names nothing
    out = client.post('/api/apps/voice', json={'action': 'open', 'name': 'Mon jeu'}).json()
    assert out['app']['id'] == jeu


@pytest.mark.parametrize(
    ('heard', 'title'),
    [('croc', 'croque'), ('pomme', 'pommes'), ('snak', 'snake'), ('serpant', 'serpent'), ('kasse', 'casse')],
)
def test_words_heard_another_way_sound_alike(heard: str, title: str) -> None:
    assert apps.sound(heard) == apps.sound(title)


@pytest.mark.parametrize(('one', 'other'), [('tete', 'test'), ('poste', 'pot'), ('panda', 'pendu')])
def test_words_that_only_look_alike_do_not_sound_alike(one: str, other: str) -> None:
    assert apps.sound(one) != apps.sound(other)


def test_an_app_not_found_by_voice_lists_the_titles(client: TestClient, fake) -> None:
    nothing = client.post('/api/apps/voice', json={'action': 'open', 'name': 'Tetris'}).json()
    assert nothing == {'message': "Je ne trouve pas d'app «\xa0Tetris\xa0». Propose d'en créer une.", 'app': None}
    _built(client, fake, 'Snake')
    _built(client, fake, 'Pong')
    missing = client.post('/api/apps/voice', json={'action': 'update', 'name': 'Tetris', 'request': 'x'}).json()
    assert (
        missing['message'] == "Je ne trouve pas d'app «\xa0Tetris\xa0». Les apps\xa0: «\xa0Pong\xa0», «\xa0Snake\xa0»."
    )


def test_an_app_is_said_on_screen_only_while_a_dashboard_is_connected(client: TestClient, fake) -> None:
    """An `open` reaches the dashboards connected as it is sent, and no other: with none, the app shows nowhere."""
    _built(client, fake, 'Snake')

    def said(**ask: str) -> str:
        return client.post('/api/apps/voice', json=ask).json()['message']

    nobody = "mais aucun dashboard n'est ouvert pour la montrer"
    assert said(action='open', name='snake') == f'«\xa0Snake\xa0» est prête, {nobody}.'
    fake.gate = threading.Event()
    building = client.post('/api/apps', json={'request': 'Casse-briques'}).json()['id']
    assert said(action='open', name='casse briques') == f'«\xa0Casse-briques\xa0» est en construction, {nobody}.'
    fake.gate.set()
    settled(client, building)
    created = client.post('/api/apps/voice', json={'action': 'create', 'request': 'un morpion'}).json()
    assert created['message'] == f"Je construis l'app (quelques minutes), {nobody}. {apps.LATER}"
    settled(client, created['app']['id'])
    with client.websocket_connect('/api/ws'):
        assert said(action='open', name='snake') == '«\xa0Snake\xa0» est sur le dashboard.'
        assert said(action='create', request='un pong').startswith("Je construis l'app (quelques minutes), sur le")
    assert not builds_running()


def test_the_robot_is_told_the_app_is_on_the_dashboard_never_that_it_shows_there(client: TestClient, fake) -> None:
    """A dashboard holding unsaved work only offers the app it is sent (frontend/src/launch.ts), which the Mac cannot
    tell: the robot hears that the app is on the dashboard, never that it is open or shown there."""
    _built(client, fake, 'Snake')
    fake.gate = threading.Event()
    building = client.post('/api/apps', json={'request': 'Casse-briques'}).json()['id']
    asks = ({'action': 'open', 'name': 'snake'}, {'action': 'open', 'name': 'casse briques'})
    with client.websocket_connect('/api/ws'):
        said = [client.post('/api/apps/voice', json=ask).json()['message'] for ask in asks]
        created = client.post('/api/apps/voice', json={'action': 'create', 'request': 'un pong'}).json()
    fake.gate.set()
    settled(client, building)
    settled(client, created['app']['id'])
    for message in [*said, created['message']]:
        assert 'sur le dashboard' in message and not re.search(r'ouverte|affichée', message), message


def test_opening_an_app_being_built_says_so(client: TestClient, fake, events) -> None:
    fake.gate = threading.Event()
    client.post('/api/apps', json={'request': 'Casse-briques'})
    out = client.post('/api/apps/voice', json={'action': 'open', 'name': 'casse briques'}).json()
    assert 'est en construction' in out['message'] and events[-1].open
    fake.gate.set()


# ----- the bridge ----------------------------------------------------------------------------------------

APP = 'ab' * 8


@pytest.mark.parametrize(
    'body',
    [
        {'v': 1, 'motion': {'kind': 'emotion', 'name': 'furious1'}},
        {'v': 1, 'motion': {'kind': 'gesture', 'name': 'moonwalk'}},
        {'v': 1, 'motion': {'kind': 'look', 'yaw': 31, 'pitch': 0}},
        {'v': 1, 'motion': {'kind': 'look', 'yaw': 0, 'pitch': -21}},
        {'v': 1, 'motion': {'kind': 'antennas', 'left': 61, 'right': 0}},
        {'v': 1, 'motion': {'kind': 'say', 'text': 'bonjour'}},
        {'v': 2, 'motion': {'kind': 'emotion', 'name': 'cheerful1'}},
        {'motion': {'kind': 'emotion', 'name': 'cheerful1'}},
        {'v': 1, 'motion': {'kind': 'look', 'yaw': 0, 'pitch': 0}, 'extra': 1},
        {'v': 1, 'motion': {'kind': 'look', 'yaw': 0, 'pitch': 0}, 'dashboard': ''},
        {'v': 1, 'motion': {'kind': 'look', 'yaw': 0, 'pitch': 0}, 'dashboard': 'd' * 33},
    ],
)
def test_the_bridge_refuses_what_v1_does_not_say(client: TestClient, body: dict[str, Any]) -> None:
    awake(client)
    refused = client.post(f'/api/apps/{APP}/bridge', json={'dashboard': 'mac', **body})
    assert refused.status_code == 422 and isinstance(refused.json()['detail'], str)


def test_a_bridge_message_becomes_one_short_lived_app_motion(client: TestClient, app: FastAPI) -> None:
    mac = app.state.mac
    look = {'v': 1, 'motion': {'kind': 'look', 'yaw': -30, 'pitch': 20}, 'dashboard': 'mac'}
    asleep = client.post(f'/api/apps/{APP}/bridge', json=look)
    assert (asleep.status_code, asleep.json()) == (409, {'detail': apps.ASLEEP})
    awake(client)
    assert client.post(f'/api/apps/{APP}/bridge', json=look).status_code == 204
    antennas = {'v': 1, 'motion': {'kind': 'antennas', 'left': 20, 'right': -20}, 'dashboard': 'mac'}
    assert client.post(f'/api/apps/{APP}/bridge', json=antennas).status_code == 204
    pending = [command for _, command in mac.commands._pending]
    assert [(c.action, c.ttl_s, c.motion.model_dump()) for c in pending] == [
        ('app_motion', APP_MOTION_TTL_S, {'kind': 'antennas', 'left': 20, 'right': -20})  # the latest wins
    ]
    taken = client.get('/api/robot/commands', params={'wait': 0}).json()['command']
    assert taken['motion'] == {'kind': 'antennas', 'left': 20.0, 'right': -20.0} and taken['ttl_s'] == 1.5
    assert client.post('/api/apps/not-an-id/bridge', json=look).status_code == 404
    mac.health.beat(Heartbeat(awake=False))
    assert client.post(f'/api/apps/{APP}/bridge', json=look).status_code == 409


def test_the_bridge_takes_a_replay_of_what_a_hand_did_as_one_of_the_apps_moves(
    client: TestClient, app: FastAPI
) -> None:
    awake(client)
    ask = {'kind': 'replay', 'start': 1794000001000.0, 'end': 1794000003500.0, 'loops': 4}
    assert client.post(f'/api/apps/{APP}/bridge', json={'v': 1, 'motion': ask, 'dashboard': 'd1'}).status_code == 204
    queued = [c.motion for _, c in app.state.mac.commands._pending if c.action == 'app_motion']
    assert [m.model_dump() for m in queued] == [ask]
    nod = {'v': 1, 'motion': {'kind': 'gesture', 'name': 'nod'}, 'dashboard': 'd1'}
    assert client.post(f'/api/apps/{APP}/bridge', json=nod).json() == {'detail': apps.TOO_FAST}  # one move at a time
    for wrong in ({**ask, 'loops': 9}, {**ask, 'loops': -1}, {**ask, 'start': 'now'}):
        refused = client.post(f'/api/apps/{APP}/bridge', json={'v': 1, 'motion': wrong, 'dashboard': 'd1'})
        assert refused.status_code == 422


def test_the_bridge_limits_each_apps_rate(client: TestClient, app: FastAPI, new_clock) -> None:
    clock = new_clock()
    app.state.mac.apps._clock = clock
    awake(client)
    look = {'v': 1, 'motion': {'kind': 'look', 'yaw': 0, 'pitch': 0}, 'dashboard': 'mac'}
    assert [client.post(f'/api/apps/{APP}/bridge', json=look).status_code for _ in range(7)] == [204] * 6 + [429]
    assert client.post(f'/api/apps/{APP}/bridge', json=look).json() == {'detail': apps.TOO_FAST}
    assert client.post(f'/api/apps/{"cd" * 8}/bridge', json=look).status_code == 204  # another app, its own rate
    antennas = {'v': 1, 'motion': {'kind': 'antennas', 'left': 0, 'right': 0}, 'dashboard': 'mac'}
    sent = [client.post(f'/api/apps/{APP}/bridge', json=antennas).status_code for _ in range(7)]
    assert sent == [204] * 6 + [429]  # a budget of their own, capped alike
    clock.now += 1.0
    assert client.post(f'/api/apps/{APP}/bridge', json=look).status_code == 204

    emotion = {'v': 1, 'motion': {'kind': 'emotion', 'name': 'cheerful1'}, 'dashboard': 'mac'}
    gesture = {'v': 1, 'motion': {'kind': 'gesture', 'name': 'nod'}, 'dashboard': 'mac'}
    assert client.post(f'/api/apps/{APP}/bridge', json=emotion).status_code == 204
    clock.now += 1.4
    assert client.post(f'/api/apps/{APP}/bridge', json=gesture).status_code == 429  # one move budget for both
    clock.now += 0.1
    assert client.post(f'/api/apps/{APP}/bridge', json=gesture).status_code == 204


def test_looks_paced_by_the_dashboard_are_all_taken(client: TestClient, app: FastAPI, new_clock) -> None:
    """frontend/src/apps/bridge.ts sends one look per 200 ms tick; Chrome's setInterval runs a tick up to 2 ms late."""
    clock = new_clock()
    app.state.mac.apps._clock = clock
    awake(client)
    look = {'v': 1, 'motion': {'kind': 'look', 'yaw': 0, 'pitch': 0}, 'dashboard': 'mac'}
    late = [0.0017, 0, 0, 0, 0, 0, 0.0004, 0, 0, 0, 0, 0] * 3  # as measured in Chrome
    start, codes = clock.now, []
    for tick, delay in enumerate(late):
        clock.now = start + 0.2 * tick + delay
        codes.append(client.post(f'/api/apps/{APP}/bridge', json=look).status_code)
    assert codes == [204] * len(late)


def test_the_robot_follows_one_dashboards_app_at_a_time(client: TestClient, app: FastAPI, new_clock) -> None:
    """A voice `open` reaches every dashboard connected, and each one runs the app: the robot follows the one that
    moved it first, every paced look of it taken, until it has been still for HOLD_S; then another one moves it."""
    clock = new_clock()
    app.state.mac.apps._clock = clock
    awake(client)
    look = {'v': 1, 'motion': {'kind': 'look', 'yaw': 0, 'pitch': 0}}

    def bridge(who: str, motion: dict[str, Any] = look) -> int:
        return client.post(f'/api/apps/{APP}/bridge', json={**motion, 'dashboard': who}).status_code

    start, codes = clock.now, {'mac': [], 'phone': []}
    for tick in range(25):  # 5 s of one look per 200 ms tick from each, the phone's 100 ms after the Mac's
        for who, offset in (('mac', 0.0), ('phone', 0.1)):
            clock.now = start + 0.2 * tick + offset
            codes[who].append(bridge(who))
    assert codes == {'mac': [204] * 25, 'phone': [409] * 25}
    other = client.post(f'/api/apps/{"cd" * 8}/bridge', json={**look, 'dashboard': 'phone'})
    assert other.json() == {'detail': apps.ELSEWHERE}  # another app on the phone: the robot follows the Mac's still

    assert bridge('mac', {'v': 1, 'motion': {'kind': 'emotion', 'name': 'cheerful1'}}) == 204
    clock.now += 1.8  # the Mac paces no look for 1.6 s after a move, then a tick
    assert (bridge('phone'), bridge('mac')) == (409, 204)
    clock.now += apps.HOLD_S  # the Mac's app is still: the phone's moves the robot
    assert (bridge('phone'), bridge('mac')) == (204, 409)


def test_a_cancel_during_the_check_writes_no_page(client: TestClient, config: Config, fake, monkeypatch) -> None:
    """Found in review: « Annuler » while the page ran in Chrome was ignored, the page written anyway."""
    checking, release = threading.Event(), threading.Event()

    def slow_check(_page: str, _csp: str, cancelled=lambda: False, started=lambda _chrome: None, kit='') -> list[str]:
        checking.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(app_build.app_check, 'run', slow_check)
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    assert checking.wait(5)
    assert client.post(f'/api/apps/{app_id}/cancel').status_code == 204
    release.set()
    deadline = time.monotonic() + 3
    while client.get('/api/apps').json()['apps'] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.get('/api/apps').json()['apps'] == [] and not (config.apps_dir / f'{app_id}.html').exists()


def test_deleting_an_app_being_built_stops_its_build(client: TestClient, fake) -> None:
    fake.gate = threading.Event()  # Claude never answers
    first = client.post('/api/apps', json={'request': 'un'}).json()['id']
    client.post('/api/apps', json={'request': 'deux'})
    assert client.delete(f'/api/apps/{first}').status_code == 204
    deadline = time.monotonic() + 3
    while client.post('/api/apps', json={'request': 'trois'}).status_code == 429:  # its slot frees
        assert time.monotonic() < deadline
        time.sleep(0.02)
    fake.gate.set()


def test_cancelling_the_rebuild_of_a_failed_app_keeps_the_app(client: TestClient, fake) -> None:
    fake.answers = ['pas de page']
    app_id = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    assert settled(client, app_id).status == 'failed'
    fake.gate = threading.Event()
    client.post(f'/api/apps/{app_id}/update', json={'request': 'en bleu'})
    assert client.post(f'/api/apps/{app_id}/cancel').status_code == 204
    fake.gate.set()
    app = settled(client, app_id)
    assert (app.status, app.error) == ('failed', apps.CANCELLED)


# ----- the manette ---------------------------------------------------------------------------------------


def test_the_manette_is_a_lease_the_robot_takes_only_awake(client: TestClient, app: FastAPI) -> None:
    mac = app.state.mac
    asleep = client.post(f'/api/apps/{APP}/manette', json={'profile': 'gachettes'})
    assert (asleep.status_code, asleep.json()) == (409, {'detail': apps.MANETTE_ASLEEP})
    assert client.post(f'/api/apps/{APP}/manette', json={'profile': None}).status_code == 204  # letting go: always
    awake(client)
    assert client.post(f'/api/apps/{APP}/manette', json={'profile': 'volant', 'grant': True}).status_code == 204
    assert client.post(f'/api/apps/{APP}/manette', json={'profile': 'volant'}).status_code == 204  # renewed
    pending = [command for _, command in mac.commands._pending]  # the robot has not polled: the human's yes stays
    assert [(c.action, c.manette, c.grant, c.ttl_s) for c in pending] == [('manette', 'volant', True, MANETTE_TTL_S)]
    other = 'cd' * 8
    taken = client.post(f'/api/apps/{other}/manette', json={'profile': 'gachettes', 'grant': True})
    assert (taken.status_code, taken.json()) == (409, {'detail': apps.MANETTE_TAKEN})  # one app at a time
    assert client.post(f'/api/apps/{other}/manette', json={'profile': None}).status_code == 204
    assert [c.manette for _, c in mac.commands._pending] == ['volant']  # its letting go is not the holder's
    assert client.post(f'/api/apps/{APP}/manette', json={'profile': 'joystick'}).status_code == 422

    event = {'kind': 'manette', 'session_id': 's1', 'state': 'on', 'profile': 'volant'}
    assert client.post('/api/robot/events', json=event).status_code == 204
    assert client.get('/api/state').json()['session']['manette'] == 'volant'
    client.post('/api/robot/events', json={**event, 'state': 'off', 'profile': None, 'why': 'lease'})
    assert client.get('/api/state').json()['session']['manette'] is None


def test_a_lease_nobody_renews_lets_another_app_take_the_manette(client: TestClient, app: FastAPI, new_clock) -> None:
    """The holder's dashboard closed: no renewal, no letting go. Its lease lapses after MANETTE_TTL_S, not before."""
    clock = new_clock()
    app.state.mac.apps._clock = clock
    awake(client)
    assert client.post(f'/api/apps/{APP}/manette', json={'profile': 'volant', 'grant': True}).status_code == 204
    other = {'profile': 'gachettes', 'grant': True}
    clock.now += MANETTE_TTL_S - 0.1
    taken = client.post(f'/api/apps/{"cd" * 8}/manette', json=other)
    assert taken.status_code == 409 and taken.json() == {'detail': apps.MANETTE_TAKEN}
    clock.now += 0.2
    assert client.post(f'/api/apps/{"cd" * 8}/manette', json=other).status_code == 204


@pytest.mark.parametrize(
    ('meta', 'quiet'),
    [
        ('<meta name="reachy-quiet" content="oui">', True),
        ('<meta name="reachy-quiet" content="non">', False),
        ("<meta content='Non' name='reachy-quiet'>", False),  # any order, any quotes, any case
        ('', True),  # an app made before the tag: a game played in front of the robot
    ],
)
def test_the_builder_judges_whether_the_robot_keeps_silent(meta: str, quiet: bool) -> None:
    assert app_build.quiet_of(f'<html><head>{meta}<title>Oui ou non ?</title></head></html>') is quiet
    assert 'reachy-quiet' in app_build.PROMPT


def test_a_quiet_app_on_screen_keeps_the_robot_silent_until_it_is_left(
    app: FastAPI, config: Config, client: TestClient
) -> None:
    """Shown in a dashboard while the robot converses, a quiet app has the robot told to keep silent until it leaves
    the screen; its speech taken back by the robot itself (an antenna, a say), it speaks until then; an app people
    talk to it in does not."""
    quiz, talk = _store(config, 'Oui ou non ?', 'Raconte-moi')
    mac = app.state.mac
    assert client.post(f'/api/apps/{talk}/quiet', json={'quiet': False}).json()['quiet'] is False
    assert client.post('/api/apps/0000000000000999/quiet', json={'quiet': True}).status_code == 404
    told: list[bool | None] = []

    def step() -> None:
        mac.commands._pending.clear()
        asyncio.run(mac.apps.hush.step())
        told.extend(command.hush for _, command in mac.commands._pending)

    _show(app, quiz)
    step()  # asleep: nothing to tell
    awake(client)
    step()
    _show(app, talk)
    step()
    _show(app, quiz)
    step()
    _show(app, None)
    step()  # left: its speech is its own again
    _show(app, quiz)
    step()  # shown again: silent again
    events = {'kind': 'hush', 'session_id': 's1', 'on': False, 'why': 'say'}
    assert client.post('/api/robot/events', json=events).status_code == 204  # the robot took its speech back
    step()
    assert told == [True, False, True, False, True, False]


# ----- the apps shipped with the robot ---------------------------------------------------------------------


@pytest.fixture
def shipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Shipped pages of the test's own, where the Mac reads them."""
    assets = tmp_path / 'shipped'
    assets.mkdir()
    for key, (title, _) in apps.BUILTINS.items():
        (assets / f'{key}.html').write_text(page(title))
    monkeypatch.setattr(apps, 'ASSETS', assets)
    return assets


def test_the_shipped_apps_are_listed_and_served_and_never_stored(
    client: TestClient, config: Config, fake, shipped: Path
) -> None:
    listed = {
        a['id']: (a['title'], a['status'], a['builtin'], a['edited']) for a in client.get('/api/apps').json()['apps']
    }
    assert listed == {key: (title, 'ready', True, False) for key, (title, _) in apps.BUILTINS.items()}
    assert client.get('/api/apps/slalom/html').json()['html'] == app_build.served(page('Slalom'))
    catalog = Catalog.model_validate(client.get('/api/catalog').json())
    assert {e.id for e in catalog.entries if e.id.startswith('app:')} == {f'app:{key}' for key in apps.BUILTINS}
    mine = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    settled(client, mine)
    assert [entry['id'] for entry in index(config)] == [mine]  # yours alone: a shipped app is read where it ships
    kept = client.delete('/api/apps/slalom')
    assert (kept.status_code, kept.json()['detail']) == (409, 'Une app fournie ne se supprime pas.')
    (shipped / 'tam-tam.html').unlink()  # a page that did not ship is no app
    assert 'tam-tam' not in {a['id'] for a in client.get('/api/apps').json()['apps']}
    assert client.get('/api/apps/tam-tam/html').status_code == 404


def test_a_shipped_app_you_change_is_yours_until_you_delete_it(
    client: TestClient, config: Config, shipped: Path, events
) -> None:
    renamed = client.patch('/api/apps/tam-tam', json={'title': 'Tambours'}).json()
    assert (renamed['title'], renamed['builtin'], renamed['edited']) == ('Tambours', True, True)
    assert [entry['id'] for entry in index(config)] == ['tam-tam'] and 'builtin' not in index(config)[0]
    assert client.post('/api/apps/tam-tam/quiet', json={'quiet': False}).json()['quiet'] is False
    others = [a for a in client.get('/api/apps').json()['apps'] if a['id'] != 'tam-tam']
    assert others and not any(a['edited'] for a in others)
    assert client.delete('/api/apps/tam-tam').status_code == 204
    back = events[-1]  # not gone: back as shipped
    assert (back.deleted, back.app.title, back.app.edited, back.app.quiet) == (False, 'Tam-tam', False, True)
    assert index(config) == []
    assert client.delete('/api/apps/tam-tam').status_code == 409


def test_a_retouch_of_a_shipped_app_edits_a_copy_of_its_page(
    client: TestClient, config: Config, fake, shipped: Path
) -> None:
    fake.answers = [lambda old: old.replace('<canvas>', '<canvas class="vite">')]
    assert client.post('/api/apps/slalom/update', json={'request': 'plus vite'}).status_code == 200
    app = settled(client, 'slalom')
    assert (app.status, app.error, app.title, app.builtin, app.edited) == ('ready', None, 'Slalom', True, True)
    assert apps.BUILTINS['slalom'][1] in fake.asked()  # what it is: the retouch's first request
    assert 'class="vite"' in (config.apps_dir / 'slalom.html').read_text()
    assert (shipped / 'slalom.html').read_text() == page('Slalom')  # where it ships, a page is never written
    assert 'class="vite"' in client.get('/api/apps/slalom/html').json()['html']
    assert client.delete('/api/apps/slalom').status_code == 204
    assert client.get('/api/apps/slalom/html').json()['html'] == app_build.served(page('Slalom'))
    assert not (config.apps_dir / 'slalom.html').exists()


def test_a_failed_retouch_leaves_a_shipped_app_ready(client: TestClient, fake, shipped: Path) -> None:
    fake.answers = [OSError('gone')]
    client.post('/api/apps/slalom/update', json={'request': 'plus vite'})
    app = settled(client, 'slalom')
    assert (app.status, app.error) == ('ready', "Claude n'a pas pu être lancé sur ce Mac.")
    assert client.get('/api/apps/slalom/html').json()['html'] == app_build.served(page('Slalom'))


def test_a_shipped_app_moves_the_robot_and_holds_it_as_its_manette(client: TestClient, shipped: Path) -> None:
    awake(client)
    assert client.post('/api/apps/slalom/manette', json={'profile': 'volant', 'grant': True}).status_code == 204
    assert client.post('/api/apps/slalom/manette', json={'profile': None}).status_code == 204
    body = {'v': 1, 'dashboard': 'd1', 'motion': {'kind': 'emotion', 'name': 'success1'}}
    assert client.post('/api/apps/slalom/bridge', json=body).status_code == 204
    assert client.post('/api/apps/slalom-2/bridge', json=body).status_code == 404  # no id, no path


def test_a_shipped_app_is_opened_by_voice_and_its_pages_date_makes_it_no_latest(
    client: TestClient, fake, shipped: Path
) -> None:
    opened = client.post('/api/apps/voice', json={'action': 'open', 'name': 'le slalom'}).json()
    assert opened['app']['id'] == 'slalom'
    mine = client.post('/api/apps', json={'request': 'un Snake'}).json()['id']
    settled(client, mine)
    later = time.time() + 3600  # a pull brought newer pages: none of them is the app you made last
    for path in shipped.iterdir():
        os.utime(path, (later, later))
    assert client.post('/api/apps/voice', json={'action': 'open', 'name': ''}).json()['app']['id'] == mine
