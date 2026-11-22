"""Settings: the JSON store, the rules (one model), the broadcast, the composed prompt, the voice samples."""

from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mac_server import prompts
from mac_server.config import Config
from mac_server.contracts import NAME_MAX, PERSONALITY_MAX, Settings, SettingsPatch
from mac_server.store import Store, write_json_atomic
from mac_server.voices import VOICES
from robot_agent import live
from robot_agent.emotions import CURATED
from robot_agent.tools import declarations

SETUP_MAX = 10_000  # characters re-billed every turn: system instruction + tool declarations (CLAUDE.md)


def test_defaults_without_a_file(client: TestClient, config: Config) -> None:
    view = client.get('/api/settings').json()
    assert view['settings'] == Settings().model_dump()
    assert len(view['options']['voices']) == len(VOICES)
    assert set(view['options']['animations']) == {'idle', 'listening', 'talking'}
    assert not config.settings_path.exists()  # reading never writes


def test_patch_round_trip(client: TestClient, config: Config) -> None:
    changed = {'name': 'Bob', 'voice': 'Kore', 'talking_animation': 'nod', 'web_search': False, 'idle_sleep_minutes': 0,
               'gaze_tracking': True}  # fmt: skip
    view = client.patch('/api/settings', json=changed).json()
    assert {k: view['settings'][k] for k in changed} == changed
    assert client.get('/api/settings').json() == view
    stored = json.loads(config.settings_path.read_text(encoding='utf-8'))
    assert stored['settings'] == view['settings']
    assert stored['volume'] == 100
    client.patch('/api/settings', json={'personality': 'Tu aimes le jazz.'})
    assert client.get('/api/settings').json()['settings']['name'] == 'Bob'  # untouched fields stay


def test_settings_and_volume_keep_each_other(client: TestClient, config: Config) -> None:
    """They share settings.json: saving the settings keeps the volume, setting the volume keeps the settings."""
    client.put('/api/volume', json={'volume': 40})
    assert client.patch('/api/settings', json={'name': 'Bob'}).status_code == 200
    assert json.loads(config.settings_path.read_text(encoding='utf-8')).get('volume') == 40
    assert client.get('/api/state').json()['volume'] == 40
    client.put('/api/volume', json={'volume': 55})
    assert client.get('/api/settings').json()['settings']['name'] == 'Bob'


@pytest.mark.parametrize(
    ('patch', 'detail'),
    [
        ({'voice': 'Nobody'}, 'Voix inconnue\xa0: «\xa0Nobody\xa0».'),
        ({'idle_animation': 'expressive'}, 'Animation inconnue (idle)\xa0: «\xa0expressive\xa0».'),
        ({'idle_sleep_minutes': 61}, 'Mise en veille\xa0: au plus 60.'),
        ({'idle_sleep_minutes': -1}, 'Mise en veille\xa0: au moins 0.'),
        ({'name': 'x' * 41}, 'Nom\xa0: 40 caractères au plus.'),
        ({'name': '   '}, 'Le nom ne peut pas être vide.'),
        ({'personality': 'x' * (PERSONALITY_MAX + 1)}, 'Personnalité\xa0: 1\u202f500 caractères au plus.'),
        ({'temperature': 0.2}, 'Temperature\xa0: champ inconnu.'),
        ({'web_search': 'peut-être'}, 'Recherche web\xa0: valeur invalide.'),
    ],
)
def test_rules_refuse_in_french_and_write_nothing(client: TestClient, config: Config, patch: dict, detail: str) -> None:
    client.patch('/api/settings', json={'name': 'Bob'})
    before = config.settings_path.read_bytes()
    response = client.patch('/api/settings', json=patch)
    assert response.status_code == 422
    assert response.json() == {'detail': detail}
    assert config.settings_path.read_bytes() == before


@pytest.mark.parametrize('body', [b'{bad', b'{"name": "Bob",}', b'\xff\xfe'])  # cut, a stray comma, not UTF-8
def test_a_body_that_is_not_json_is_refused_in_one_french_sentence(
    client: TestClient, config: Config, body: bytes
) -> None:
    client.patch('/api/settings', json={'name': 'Bob'})
    before = config.settings_path.read_bytes()
    for method, path in (('PATCH', '/api/settings'), ('PUT', '/api/volume')):
        response = client.request(method, path, content=body, headers={'Content-Type': 'application/json'})
        assert response.status_code == 422
        assert response.json() == {'detail': "Requête illisible\xa0: ce n'est pas du JSON."}
    assert config.settings_path.read_bytes() == before


def test_default_stays_default(client: TestClient, config: Config) -> None:
    assert client.patch('/api/settings', json={'name': 'Default'}).json()['settings']['name'] == 'Default'
    assert client.get('/api/settings').json()['settings']['name'] == 'Default'
    assert Store(config.settings_path).settings().name == 'Default'
    assert 'Tu es Default,' in client.get('/api/settings').json()['system_prompt']


def test_a_patch_is_broadcast(client: TestClient) -> None:
    with client.websocket_connect('/api/ws') as ws:
        view = client.patch('/api/settings', json={'voice': 'Puck'}).json()
        assert ws.receive_json() == {'channel': 'settings', 'data': view}


def test_concurrent_writers_keep_each_others_change(tmp_path: Path) -> None:
    """A settings PATCH and a volume PUT share settings.json from two threads: each read-modify-write is locked."""
    store = Store(tmp_path / 'settings.json')

    def at_once(*writes: object) -> None:
        start = threading.Barrier(len(writes))
        threads = [threading.Thread(target=lambda w=w: (start.wait(), w())) for w in writes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

    for i in range(30):
        at_once(lambda i=i: store.update(SettingsPatch(personality=f'p{i}')), lambda i=i: store.set_volume(i))
        assert (store.settings().personality, store.volume()) == (f'p{i}', i)


def test_writes_are_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'state' / 'settings.json'
    store = Store(path)
    store.update(SettingsPatch(name='Bob'))
    store.set_volume(40)
    assert sorted(p.name for p in path.parent.iterdir()) == ['settings.json']
    before = path.read_bytes()

    def crash(*_: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(os, 'replace', crash)
    with pytest.raises(OSError, match='disk full'):
        store.update(SettingsPatch(name='Alice'))
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ['settings.json']  # the temp file is gone too


def test_atomic_json_is_private(tmp_path: Path) -> None:
    write_json_atomic(tmp_path / 'secret.json', {'refresh_token': 't'})
    assert (tmp_path / 'secret.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('content', ['{not json', '[1, 2]', '{"settings": "Bob", "volume": 50}', ''])
def test_an_unreadable_file_reads_as_defaults(client: TestClient, config: Config, content: str) -> None:
    config.settings_path.write_text(content, encoding='utf-8')
    response = client.get('/api/settings')
    assert response.status_code == 200
    assert response.json()['settings'] == Settings().model_dump()
    assert client.get('/api/state').status_code == 200


def test_a_bad_field_falls_back_alone(config: Config, caplog: pytest.LogCaptureFixture) -> None:
    stored = {'name': 'Bob', 'voice': 'Retired', 'idle_animation': 'expressive', 'temperature': 1.0}
    config.settings_path.write_text(json.dumps({'settings': stored, 'volume': 'fort'}), encoding='utf-8')
    store = Store(config.settings_path)
    settings = store.settings()
    assert settings.name == 'Bob'
    assert (settings.voice, settings.idle_animation) == (Settings().voice, Settings().idle_animation)
    assert store.volume() == 100
    assert 'idle_animation, temperature, voice' in caplog.text


@pytest.mark.parametrize('broken', ['a stray comma', 'a list', 'settings not an object'])
def test_a_file_not_read_in_full_is_never_written_over(client: TestClient, config: Config, broken: str) -> None:
    mine = {'settings': {'name': 'Pépito', 'personality': 'Un texte écrit à la main. ' * 20}, 'volume': 50}
    text = {
        'a stray comma': json.dumps(mine, ensure_ascii=False)[:-1] + ',}',
        'a list': json.dumps([mine], ensure_ascii=False),
        'settings not an object': json.dumps({'settings': 'Pépito', 'volume': 50}),
    }[broken]
    config.settings_path.write_text(text, encoding='utf-8')
    for answer in (
        client.put('/api/volume', json={'volume': 40}),  # the slider, or « baisse le volume »
        client.patch('/api/settings', json={'web_search': False}),
    ):
        assert answer.status_code == 500
        assert answer.json()['detail'].startswith(f'Le fichier des réglages ({config.settings_path}) est illisible')
    assert config.settings_path.read_text(encoding='utf-8') == text
    assert client.get('/api/settings').json()['settings'] == Settings().model_dump()  # reads still never fail


def test_a_write_keeps_the_stored_values_it_refused(client: TestClient, config: Config) -> None:
    """A value the rules refuse (too long, retired, from another version) reads as its default, and stays stored
    until a write replaces it: the owner's text is never lost to a volume change."""
    long = 'Un texte écrit à la main. ' * 60
    stored = {'name': 'Pépito', 'personality': long, 'voice': 'Retired', 'pinned': True}
    config.settings_path.write_text(json.dumps({'settings': stored, 'volume': 'fort'}), encoding='utf-8')
    client.put('/api/volume', json={'volume': 40})
    client.patch('/api/settings', json={'web_search': False})
    kept = json.loads(config.settings_path.read_text(encoding='utf-8'))
    assert kept['volume'] == 40
    assert kept['settings'] | {'web_search': True} == Settings(name='Pépito').model_dump() | {
        'personality': long, 'voice': 'Retired', 'pinned': True}  # fmt: skip
    assert client.get('/api/settings').json()['settings']['personality'] == ''
    client.patch('/api/settings', json={'personality': 'Court.', 'voice': 'Kore'})
    kept = json.loads(config.settings_path.read_text(encoding='utf-8'))['settings']
    assert (kept['personality'], kept['voice'], kept['pinned']) == ('Court.', 'Kore', True)


def test_prompt_parts() -> None:
    settings = Settings(name='Bob', personality='  Tu adores les pirates.  ')
    prompt = prompts.system_prompt(settings, date(2026, 11, 1))
    assert prompt.startswith('Tu es Bob, un petit robot de bureau Reachy Mini')
    assert 'Nous sommes le dimanche 1er novembre 2026.' in prompt
    assert prompt.index('Tu adores les pirates.') < prompt.index('lues à voix haute')
    assert prompts.WEB not in prompt
    assert 'personnalité' not in prompts.system_prompt(Settings(), date(2026, 10, 30))
    assert 'vendredi 30 octobre 2026' in prompts.system_prompt(Settings(), date(2026, 10, 30))
    assert prompts.web_prompt(Settings()) == prompts.WEB
    assert prompts.web_prompt(Settings(web_search=False)) == ''
    assert 'look' not in prompt  # declared only with a camera, and its own description says what it does


def test_prompt_is_short_enough() -> None:
    """identity + protocol + web <= 3000 characters, so the robot's whole setup fits in 10 000."""
    settings = Settings(name='x' * 40)
    assert len(prompts.system_prompt(settings)) + len(prompts.web_prompt(settings)) <= 3000


def test_the_robots_whole_setup_fits_10000_characters(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worst setup the Mac lets through, as the robot bakes it: each side adds its own part, once."""

    class LongestDay(date):  # an 8-letter weekday, « 1er » and septembre: the longest French date
        @classmethod
        def today(cls) -> date:
            return date(2027, 9, 1)

    monkeypatch.setattr(prompts, 'date', LongestDay)
    personality = 'P' * PERSONALITY_MAX
    patch = {'name': 'N' * NAME_MAX, 'personality': personality, 'web_search': True}
    assert client.patch('/api/settings', json=patch).status_code == 200
    view = client.get('/api/settings').json()  # what the robot fetches at wake
    assert 'mercredi 1er septembre 2027' in view['system_prompt']
    setup = live.build_config(view, declarations(emotions=list(CURATED), camera=True), handle=None, search=True)
    instruction = ''.join(part.text or '' for part in setup.system_instruction.parts)
    tools = json.dumps([tool.model_dump(mode='json', exclude_none=True) for tool in setup.tools], ensure_ascii=False)
    assert len(instruction) + len(tools) <= SETUP_MAX, f'{len(instruction)} + {len(tools)} characters'
    assert instruction == f'{view["system_prompt"]}\n\n{view["web_prompt"]}'  # the robot adds the web part only
    assert instruction.count(personality) == 1  # the Mac writes the personality once


def test_the_prompt_follows_today(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Tomorrow(date):
        @classmethod
        def today(cls) -> date:
            return date(2027, 1, 1)

    monkeypatch.setattr(prompts, 'date', Tomorrow)
    assert 'vendredi 1er janvier 2027' in client.get('/api/settings').json()['system_prompt']


def test_voice_samples(client: TestClient) -> None:
    sample = client.get('/api/voices/Kore/sample')
    assert sample.status_code == 200
    assert sample.headers['content-type'] == 'audio/wav'
    assert sample.content[:4] == b'RIFF'
    for name in ('Nobody', 'kore', 'Kore.wav'):
        missing = client.get(f'/api/voices/{name}/sample')
        assert missing.status_code == 404
        assert missing.json()['detail'] == f"Pas d'échantillon pour la voix «\xa0{name}\xa0»."
    assert client.get('/api/voices/..%2Fcontracts.py/sample').status_code == 404  # never routed to a file
