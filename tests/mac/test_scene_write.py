"""A scene written at someone's ask: Claude's play read and checked, written again once, stored; then its emotions
and its voices side by side; how each way of ending shows, and what stays in the store."""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mac_server import claude, gemini, scene_write
from mac_server.config import Config
from mac_server.contracts import SceneBuild, WsSceneBuild
from mac_server.gemini import GeminiError
from mac_server.server import create_app
from mac_server.voices import VOICES
from tests.mac.conftest import KEY
from tests.mac.test_scenes import FakeGemini, until

PLAY = """TITRE : Les deux fentes

@GAUCHE (Kore, 35) : physicienne pointilleuse, débit sec
@DROITE (Puck, -35) : rêveur taquin, parle avec entrain

GAUCHE : Il est passé par moi. Je l'ai senti.

DROITE (amusé) : Par nous deux, ma grande. <petit rire> C'est une onde.

[pause 1]

GAUCHE (vexée) : Une onde ne laisse pas un point sur l'écran.
"""
WRONG = PLAY.replace('(Kore, 35)', '(Personne, 35)')  # no such voice
MOVES = '{"moves": [{"place": 1, "emotion": "curious1"}, {"place": 3, "emotion": "laughing1"}]}'


class Run:
    """One claude.Run of the fake: it answers what its script says for the prompt it was given."""

    def __init__(self, fake: FakeClaude, text: str, system: str) -> None:
        self._fake, self.text, self.system = fake, text, system
        self.author = "Tu es l'auteur" in system
        self.progress = claude.Progress()
        self.cost_usd = 0.0
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()

    def wait(self) -> claude.Answer:
        script = self._fake.plays if self.author else self._fake.moves
        answer = script.pop(0) if len(script) > 1 else script[0]
        gate = self._fake.writing if self.author else self._fake.staging
        if gate is not None:  # held there until let go, or cancelled
            self.progress.phase, self.progress.text = 'writing', str(answer)[:60]
            while not gate.wait(0.01):
                if self.cancelled.is_set():
                    raise claude.Cancelled('Construction annulée.')
        if self.cancelled.is_set():
            raise claude.Cancelled('Construction annulée.')
        if isinstance(answer, Exception):
            raise answer
        self.cost_usd = 0.05
        return claude.Answer(answer, 0.05, 1000)


class FakeClaude:
    def __init__(self) -> None:
        self.plays: list[Any] = [PLAY]  # the author's answers, in order (the last one again after it)
        self.moves: list[Any] = [MOVES]  # the director's
        self.runs: list[Run] = []
        self.writing: threading.Event | None = None  # set: the author's runs are held until it is
        self.staging: threading.Event | None = None

    def __call__(self, text: str, system: str, **options: object) -> Run:
        self.runs.append(Run(self, text, system))
        return self.runs[-1]


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    fake = FakeClaude()
    monkeypatch.setattr(claude, 'Run', fake)
    return fake


@pytest.fixture
def tts(monkeypatch: pytest.MonkeyPatch) -> FakeGemini:
    fake = FakeGemini()
    monkeypatch.setattr(gemini, 'post', fake)
    return fake


@pytest.fixture
def app(config: Config) -> FastAPI:
    return create_app(dataclasses.replace(config, gemini_api_key='key'))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, headers={'X-API-Key': KEY}) as test_client:
        yield test_client


def _ended(client: TestClient, build_id: str) -> dict[str, Any]:
    """The build once it is no longer building."""

    def build() -> dict[str, Any]:
        return next(b for b in client.get('/api/scenes/builds').json()['builds'] if b['id'] == build_id)

    until(lambda: build()['status'] != 'building')
    return build()


def _mine(client: TestClient) -> list[str]:
    return [s['id'] for s in client.get('/api/scenes').json()['scenes'] if not s['builtin']]


def test_the_authors_prompt_casts_among_the_real_voices_and_sounds() -> None:
    prompt = scene_write.system()
    assert all(
        f'{name} | {"femme" if gender == "female" else "homme"} | ' in prompt for name, (gender, _) in VOICES.items()
    )
    assert '<soupir>' in prompt and '<petit rire>' in prompt and 'La longueur que la demande appelle' in prompt
    assert '{' not in prompt  # every blank is filled, and no length is imposed


def test_what_claude_wrote_is_read_as_a_title_and_a_play() -> None:
    assert scene_write.read(PLAY) == ('Les deux fentes', PLAY.split('\n', 2)[2].lstrip('\n'))
    assert scene_write.read(f'```\n{PLAY}```')[0] == 'Les deux fentes'  # a fence around it is dropped
    assert scene_write.read('Titre : « Le dîner »\n\nA : Oui.')[0] == 'Le dîner'
    for nothing in ('', 'Voici une scène amusante.', 'TITRE : Rien\n\n'):
        with pytest.raises(claude.ClaudeError):
            scene_write.read(nothing)


def test_a_line_read_as_another_characters_is_caught() -> None:
    """A sound or a second name before the colon: the grammar raises nothing and gives the words to whoever spoke."""
    cast = {'CLAIRE', 'LE PROFESSEUR', 'LE PROFESSEUR DUPONT'}
    fine = 'CLAIRE (sèchement) : Non.\n\nLE PROFESSEUR DUPONT : Si. Claire, enfin !\n\nle professeur — Bon.\n@CLAIRE (Kore) : vive'
    assert scene_write.misread(fine, cast) == []
    slips = 'CLAIRE : Oui.\n\nLE PROFESSEUR <rire> : Magnifique.\n\nCLAIRE et LE PROFESSEUR (ensemble) : Non.\n\nCLAIREMENT : non.'
    found = scene_write.misread(slips, cast)
    assert [line.split('\xa0:')[0] for line in found] == ['ligne 3', 'ligne 5']
    assert 'entre «\xa0LE PROFESSEUR\xa0» et ses deux-points' in found[0]


def test_a_play_with_a_line_misread_goes_back_like_one_the_grammar_refuses(
    client: TestClient, writer: FakeClaude, tts: FakeGemini
) -> None:
    writer.plays = [PLAY.replace('DROITE (amusé) :', 'DROITE <rire> :'), PLAY]
    build = _ended(client, client.post('/api/scenes/write', json={'request': 'Deux fentes'}).json()['id'])
    assert (build['status'], build['round']) == ('ready', 2)
    assert build['problems'][0].startswith('ligne 6\xa0: entre «\xa0DROITE\xa0» et ses deux-points')


def test_a_scene_asked_is_written_stored_staged_and_voiced(
    client: TestClient, writer: FakeClaude, tts: FakeGemini
) -> None:
    sent: list[SceneBuild] = []
    publish = client.app.state.mac.broker.publish  # type: ignore[attr-defined]
    client.app.state.mac.broker.publish = lambda message: (  # type: ignore[attr-defined]
        sent.append(message.data) if isinstance(message, WsSceneBuild) else None,
        publish(message),
    )
    writer.staging = threading.Event()  # the director takes its time: the voices do not wait for it
    started = client.post('/api/scenes/write', json={'request': '  Deux fentes   débattent  '})
    assert started.status_code == 200
    first = started.json()
    assert (first['status'], first['request'], first['voiced']) == ('building', 'Deux fentes débattent', False)
    until(lambda: any(b.voice == 'done' for b in sent))
    meanwhile = next(b for b in sent if b.voice == 'done')
    assert (meanwhile.status, meanwhile.write, meanwhile.stage) == ('building', 'done', 'running')
    assert (meanwhile.scene_id, meanwhile.voices_done, meanwhile.moves) == ('deux-fentes', 3, 0)
    writer.staging.set()
    build = _ended(client, first['id'])
    assert (build['status'], build['error'], build['warning']) == ('ready', None, None)
    assert (build['write'], build['stage'], build['voice']) == ('done', 'done', 'done')
    assert (build['title'], build['scene_id']) == ('Les deux fentes', 'deux-fentes')
    assert (build['moves'], build['voices_done'], build['voices_total']) == (2, 3, 3)
    assert build['cost_usd'] == 0.1 and build['ended_at'] >= build['started_at']

    scene = client.get('/api/scenes/deux-fentes').json()
    assert scene['title'] == 'Les deux fentes' and not scene['check']['errors']
    assert 'GAUCHE : [curious1] Il est passé par moi.' in scene['text'] and '[laughing1]' in scene['text']
    assert scene['text'] == build['text']  # what the dashboard shows is the scene as stored, its emotions in
    assert scene['check']['rendered'] == scene['check']['segments'] == 3 == len(tts.calls)  # a move renders nothing
    author, director = writer.runs
    assert author.text == 'Demande\xa0: «\xa0Deux fentes débattent\xa0»' and '{1}' in director.text

    until(lambda: sent[-1].status == 'ready')
    assert sent[0].status == 'building' and sent[0].write in ('waiting', 'running')


def test_a_play_the_grammar_refuses_is_written_once_more_with_its_errors(
    client: TestClient, writer: FakeClaude, tts: FakeGemini
) -> None:
    writer.plays = [WRONG, PLAY]
    build = _ended(client, client.post('/api/scenes/write', json={'request': 'Deux fentes'}).json()['id'])
    assert (build['status'], build['round']) == ('ready', 2)
    assert build['problems'] and 'Personne' in build['problems'][0] and build['problems'][0].startswith('ligne 1')
    again = writer.runs[1].text
    assert 'Personne' in again and 'Elle a été refusée' in again and 'Réécris-la entière' in again
    assert _mine(client) == ['deux-fentes']  # only the play that passes is stored

    writer.plays = [WRONG]  # refused twice: nothing is stored, and it says why
    failed = _ended(client, client.post('/api/scenes/write', json={'request': 'Encore'}).json()['id'])
    assert failed['status'] == 'failed' and failed['error'].startswith('La scène écrite ne peut pas se jouer')
    assert (failed['write'], failed['scene_id']) == ('failed', None) and _mine(client) == ['deux-fentes']


def test_one_scene_at_a_time_and_a_cancel_while_claude_writes_stores_nothing(
    client: TestClient, writer: FakeClaude, tts: FakeGemini
) -> None:
    writer.writing = threading.Event()
    first = client.post('/api/scenes/write', json={'request': 'Une'}).json()
    until(lambda: client.get('/api/scenes/builds').json()['builds'][0]['phase'] == 'writing')
    shown = client.get('/api/scenes/builds').json()['builds'][0]
    assert (shown['write'], shown['stage'], shown['voice']) == ('running', 'waiting', 'waiting')
    busy = client.post('/api/scenes/write', json={'request': 'Deux'})
    assert busy.status_code == 409 and busy.json()['detail'] == scene_write.BUSY
    asked = client.post('/api/scenes/voice', json={'request': 'Deux'}).json()  # the robot hears it as a sentence
    assert asked == {'message': f'{scene_write.BUSY} Dis-le simplement.', 'build': None}
    assert client.post('/api/scenes/builds/nope/cancel').status_code == 409
    assert client.post(f'/api/scenes/builds/{first["id"]}/cancel').status_code == 204
    build = _ended(client, first['id'])
    assert (build['status'], build['write'], build['scene_id']) == ('cancelled', 'failed', None)
    assert _mine(client) == [] and not tts.calls
    assert client.post(f'/api/scenes/builds/{first["id"]}/cancel').status_code == 409  # it has ended

    writer.writing = None  # the next one can start, and the ended one is still listed after it
    second = client.post('/api/scenes/write', json={'request': 'Trois'}).json()
    _ended(client, second['id'])
    assert [b['id'] for b in client.get('/api/scenes/builds').json()['builds']] == [second['id'], first['id']]


def test_without_its_emotions_a_scene_is_ready_all_the_same(
    client: TestClient, writer: FakeClaude, tts: FakeGemini
) -> None:
    writer.moves = [claude.ClaudeError("Claude n'a pas fini en 10 minutes.")]
    build = _ended(client, client.post('/api/scenes/write', json={'request': 'Une'}).json()['id'])
    assert (build['status'], build['stage'], build['voice'], build['moves']) == ('ready', 'failed', 'done', 0)
    assert build['warning'] == "Sans émotions\xa0: Claude n'a pas fini en 10 minutes." and build['error'] is None
    assert '[' not in client.get('/api/scenes/deux-fentes').json()['text'].replace('[pause 1]', '')

    writer.moves = ['Je ne sais pas.']  # an answer with no pick in it
    build = _ended(client, client.post('/api/scenes/write', json={'request': 'Deux'}).json()['id'])
    assert (build['status'], build['warning']) == ('ready', f'Sans émotions\xa0: {scene_write.UNUSABLE}')


def test_a_scene_changed_while_claude_chose_its_emotions_keeps_the_change(
    client: TestClient, writer: FakeClaude, tts: FakeGemini
) -> None:
    writer.staging = threading.Event()
    started = client.post('/api/scenes/write', json={'request': 'Une'}).json()
    until(lambda: 'deux-fentes' in _mine(client))
    mine = PLAY.split('\n', 2)[2].lstrip('\n') + '\nDROITE : Et toc.\n'
    assert client.put('/api/scenes/deux-fentes', json={'title': 'Les deux fentes', 'text': mine}).status_code == 200
    writer.staging.set()
    build = _ended(client, started['id'])
    assert (build['status'], build['stage'], build['warning']) == ('ready', 'failed', scene_write.EDITED)
    assert client.get('/api/scenes/deux-fentes').json()['text'] == mine


def test_voices_that_cannot_be_rendered_fail_the_build_and_keep_the_scene(
    client: TestClient, writer: FakeClaude, tts: FakeGemini
) -> None:
    writer.staging = threading.Event()  # the director is at work when the voices fail: its run ends with them
    tts.errors = [GeminiError('Gemini refuse cette réplique.', 400)] * 6  # each line, asked twice: by the save too
    build = _ended(client, client.post('/api/scenes/write', json={'request': 'Une'}).json()['id'])
    assert (build['status'], build['error']) == ('failed', 'Gemini refuse cette réplique.')
    assert (build['write'], build['stage'], build['voice']) == ('done', 'failed', 'failed')
    assert writer.runs[1].cancelled.is_set()
    assert _mine(client) == ['deux-fentes']  # written: it stays, to be mended in its editor


def test_a_cancel_while_the_voices_render_keeps_the_scene_written(
    client: TestClient, writer: FakeClaude, tts: FakeGemini
) -> None:
    tts.delay = 0.4
    started = client.post('/api/scenes/write', json={'request': 'Une'}).json()
    until(lambda: client.get('/api/scenes/builds').json()['builds'][0]['voice'] == 'running')
    assert client.post(f'/api/scenes/builds/{started["id"]}/cancel').status_code == 204
    build = _ended(client, started['id'])
    assert (build['status'], build['scene_id']) == ('cancelled', 'deux-fentes') and _mine(client) == ['deux-fentes']


def test_the_robot_is_told_the_scene_is_on_its_way_and_where_it_shows(
    client: TestClient, writer: FakeClaude, tts: FakeGemini
) -> None:
    alone = client.post('/api/scenes/voice', json={'request': 'Une scène sur les ondes'}).json()
    assert alone['build']['voiced'] is True and 'dashboard' not in alone['message']
    assert "quelques minutes. Elle se jouera toute seule dès qu'elle sera prête" in alone['message']
    _ended(client, alone['build']['id'])
    with client.websocket_connect('/api/ws'):  # a dashboard is there to follow it
        seen = client.post('/api/scenes/voice', json={'request': 'Une autre'}).json()
        assert 'quelques minutes, à suivre sur le dashboard.' in seen['message']
        _ended(client, seen['build']['id'])
    assert client.post('/api/scenes/voice', json={'request': ''}).status_code == 422


def test_a_scene_never_takes_the_name_of_a_route(client: TestClient, tts: FakeGemini) -> None:
    text = '@A (Kore) : vive\n\nA : Oui.\n'
    for title in ('Builds', 'Write', 'Voice', 'Vocabulary'):
        scene_id = client.post('/api/scenes', json={'title': title, 'text': text}).json()['id']
        assert (
            scene_id.startswith(f'{title.lower()}-') and client.get(f'/api/scenes/{scene_id}').json()['title'] == title
        )
