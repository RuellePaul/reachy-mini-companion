"""Scene voices (rendered once, cached, the daily quota's fallback) and the scene routes, Gemini faked."""

from __future__ import annotations

import base64
import dataclasses
import io
import json
import logging
import math
import struct
import threading
import time
import wave
from collections.abc import Iterator
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mac_server import gemini, scene_text, scene_voice
from mac_server.config import Config
from mac_server.contracts import SCENE_TEXT_MAX, Heartbeat, SceneTimeline
from mac_server.gemini import GeminiError
from mac_server.routes import ROBOT_OFF
from mac_server.scene_voice import FALLBACK_MODEL, Voices, measure
from mac_server.scenes import BUILTINS
from mac_server.server import create_app
from tests.mac.conftest import KEY


def tone_wav(lead: float = 0.3, voiced: float = 1.0, tail: float = 0.4, rate: int = 24000) -> bytes:
    """Silence, a 440 Hz tone at 0.3 full scale, silence: 16-bit mono, as the speech model answers."""
    samples = [0] * int(lead * rate)
    samples += [int(0.3 * 32767 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(int(voiced * rate))]
    samples += [0] * int(tail * rate)
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack(f'<{len(samples)}h', *samples))
    return out.getvalue()


class FakeGemini:
    """Stands in for gemini.post: answers a WAV, or raises the scripted errors first; counts every paid request."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.errors: list[GeminiError] = []
        self.lock = threading.Lock()
        self.delay = 0.0
        self.running = self.peak = 0  # requests in flight, now and at most

    def __call__(self, api_key: str, path: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        assert path == 'interactions' and api_key
        with self.lock:
            self.calls.append(body)
            error = self.errors.pop(0) if self.errors else None
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay)
        finally:
            with self.lock:
                self.running -= 1
        if error is not None:
            raise error
        audio = {'type': 'audio', 'mime_type': 'audio/wav', 'data': base64.b64encode(tone_wav()).decode()}
        return {'steps': [{'type': 'user_input'}, {'type': 'model_output', 'content': [audio]}]}

    def models(self) -> list[str]:
        return [body['model'] for body in self.calls]


@pytest.fixture
def tts(monkeypatch: pytest.MonkeyPatch) -> FakeGemini:
    fake = FakeGemini()
    monkeypatch.setattr(gemini, 'post', fake)
    return fake


def per_day() -> GeminiError:
    return GeminiError('Le quota Gemini est atteint pour le moment.', 429, per_day=True)


# ----- voices -----


def test_the_voiced_span_is_measured() -> None:
    assert measure(tone_wav(0.3, 1.0, 0.4)) == {'duration': 1.7, 'onset': 0.3, 'offset': 1.3, 'gaps': []}
    assert measure(tone_wav(0.0, 0.0, 0.5))['onset'] == 0.0  # silence: no voice found, nothing raised


def test_the_pauses_inside_the_voice_are_measured() -> None:
    parts = ((0.3, 1.0, 0.5), (0.0, 0.8, 0.1), (0.0, 0.3, 0.4))  # a pause of 0.5 s, then one of 0.1 s: too short
    frames = b''
    for part in parts:
        with wave.open(io.BytesIO(tone_wav(*part))) as reader:
            frames += reader.readframes(reader.getnframes())
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(frames)
    assert measure(out.getvalue()) == {'duration': 3.4, 'onset': 0.3, 'offset': 3.0, 'gaps': [[1.3, 1.8]]}


def test_a_render_sends_the_style_and_the_voice(tmp_path: Path, tts: FakeGemini) -> None:
    voices = Voices(tmp_path, 'key')
    voices.render('a' * 40, 'Leda', 'rieuse', 'Surprise ! <laugh>').result(5)
    body = tts.calls[0]
    assert body['model'] == scene_text.TTS_MODEL
    assert body['generation_config'] == {'speech_config': [{'voice': 'Leda'}]}
    text = body['input'][0]['content'][0]
    assert text == {
        'type': 'text',
        'text': 'Surprise ! <laugh>',
        'annotations': [{'type': 'speech_metadata', 'style': 'rieuse'}],
    }
    meta = voices.meta('a' * 40)
    assert meta is not None and (meta['onset'], meta['model']) == (0.3, scene_text.TTS_MODEL)
    assert voices.path('a' * 40) == tmp_path / f'{"a" * 40}.wav'
    assert voices.path('../settings') is None and voices.path('b' * 40) is None


@pytest.mark.parametrize(
    'sidecar',
    [{'duration': 1.7, 'start': 0.5, 'end': 1.5}, {'duration': 1.7, 'onset': 0.5, 'offset': 1.5, 'start': 0.5}],
)  # the oldest kind, and one written before the pauses were measured
def test_an_older_sidecar_is_completed_by_measuring_its_voice(tmp_path: Path, sidecar: dict[str, float]) -> None:
    (tmp_path / f'{"c" * 40}.wav').write_bytes(tone_wav(0.5, 1.0, 0.2))
    (tmp_path / f'{"c" * 40}.json').write_text(json.dumps(sidecar))
    meta = Voices(tmp_path, '').meta('c' * 40)
    assert meta is not None and (meta['onset'], meta['gaps'], meta['start']) == (0.5, [], 0.5)
    assert json.loads((tmp_path / f'{"c" * 40}.json').read_text())['gaps'] == []  # measured once


def test_the_daily_quota_moves_to_the_lite_model_until_tomorrow(tmp_path: Path, tts: FakeGemini) -> None:
    voices = Voices(tmp_path, 'key', sleep=lambda _s: None)
    tts.errors = [per_day()]
    voices.render('1' * 40, 'Kore', '', 'Un.').result(5)
    voices.render('2' * 40, 'Kore', '', 'Deux.').result(5)
    assert tts.models() == [scene_text.TTS_MODEL, FALLBACK_MODEL, FALLBACK_MODEL]  # the spent one is not asked again
    assert voices.meta('1' * 40)['model'] == FALLBACK_MODEL  # type: ignore[index]
    tts.errors = [per_day()]
    with pytest.raises(GeminiError, match='quota de voix Gemini du jour est épuisé'):
        voices.render('3' * 40, 'Kore', '', 'Trois.').result(5)
    calls = len(tts.calls)
    with pytest.raises(GeminiError, match='épuisé'):
        voices.render('4' * 40, 'Kore', '', 'Quatre.').result(5)
    assert len(tts.calls) == calls  # both spent: nothing more is sent today


@pytest.mark.parametrize(
    ('now', 'back'),
    [
        (datetime(2026, 11, 4, 10, 0, tzinfo=UTC), datetime(2026, 11, 5, 8, 0, tzinfo=UTC)),  # 2 h at night, PST
        (datetime(2026, 11, 4, 7, 59, tzinfo=UTC), datetime(2026, 11, 4, 8, 0, tzinfo=UTC)),  # a minute to midnight
        (datetime(2026, 7, 1, 10, 0, tzinfo=UTC), datetime(2026, 7, 2, 7, 0, tzinfo=UTC)),  # summer time, PDT
    ],
)
def test_a_spent_model_is_asked_again_from_the_next_pacific_midnight(
    tmp_path: Path, tts: FakeGemini, now: datetime, back: datetime
) -> None:
    clock = [now.timestamp()]
    voices = Voices(tmp_path, 'key', sleep=lambda _s: None, wall=lambda: clock[0])
    tts.errors = [per_day()]
    voices.render('1' * 40, 'Kore', '', 'Un.').result(5)
    clock[0] = back.timestamp() - 1
    voices.render('2' * 40, 'Kore', '', 'Deux.').result(5)
    clock[0] = back.timestamp()
    voices.render('3' * 40, 'Kore', '', 'Trois.').result(5)
    assert tts.models() == [scene_text.TTS_MODEL, FALLBACK_MODEL, FALLBACK_MODEL, scene_text.TTS_MODEL]


def test_three_voices_are_asked_at_once_at_most(tmp_path: Path, tts: FakeGemini) -> None:
    tts.delay = 0.1
    voices = Voices(tmp_path, 'key', sleep=lambda _s: None)
    for future in [voices.render(f'{n:040x}', 'Kore', '', f'Phrase {n}.') for n in range(12)]:
        future.result(10)
    assert (len(tts.calls), tts.peak) == (12, 3)


def test_per_minute_quotas_are_waited_out_and_server_errors_retried(tmp_path: Path, tts: FakeGemini) -> None:
    slept: list[float] = []
    voices = Voices(tmp_path, 'key', sleep=slept.append)
    tts.errors = [GeminiError('quota', 429, retry_after_s=12.0), GeminiError('down', 503), GeminiError('down', 502)]
    voices.render('5' * 40, 'Kore', '', 'Cinq.').result(5)
    assert slept == [12.0, 2.0, 4.0] and len(tts.calls) == 4
    tts.errors = [GeminiError('down', 500)] * 3
    with pytest.raises(GeminiError):
        voices.render('6' * 40, 'Kore', '', 'Six.').result(5)
    assert len(tts.calls) == 7  # three attempts
    slept.clear()
    tts.errors = [GeminiError('quota', 429, retry_after_s=30.0)] * 6
    with pytest.raises(GeminiError):
        voices.render('7' * 40, 'Kore', '', 'Sept.').result(5)
    assert slept == [30.0] * 4  # at most 120 s of waiting per segment


# ----- the routes -----


@pytest.fixture
def scenes_app(config: Config) -> FastAPI:
    """The app with a key for Gemini (faked by `tts`), its scenes file and audio cache those of the test."""
    return create_app(dataclasses.replace(config, gemini_api_key='key'))


@pytest.fixture
def client(scenes_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(scenes_app, headers={'X-API-Key': KEY}) as test_client:
        yield test_client


def until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'condition not met in time'
        time.sleep(0.01)


SHORT = '@NARRATEUR (Charon) : conteur\n@MARCO (Algenib) : bourru\n\nIl était une fois.\n\nMARCO : [sad1] Ciao. [tourne à gauche]'


def test_a_scene_is_saved_checked_and_its_voices_rendered_once(
    client: TestClient, scenes_app: FastAPI, tts: FakeGemini
) -> None:
    published: list[Any] = []
    scenes_app.state.mac.broker.publish = published.append
    created = client.post('/api/scenes', json={'title': 'Ma scène', 'text': SHORT})
    assert created.status_code == 200, created.text
    scene = created.json()
    assert (scene['id'], scene['builtin'], scene['check']['segments']) == ('ma', False, 2)
    says = [cue for cue in scene['check']['cues'] if cue['kind'] == 'say']
    assert says and not any(cue['ready'] for cue in says)  # each line says whether its voice is there yet
    until(lambda: client.get(f'/api/scenes/{scene["id"]}').json()['check']['rendered'] == 2)
    assert len(tts.calls) == 2
    says = [c for c in client.get(f'/api/scenes/{scene["id"]}').json()['check']['cues'] if c['kind'] == 'say']
    assert all(cue['ready'] for cue in says)
    until(lambda: published and published[-1].data.done == 2)
    assert [m.data.total for m in published] == [2, 2, 2]
    # A second render, and a second scene sharing a line, never render that line again.
    scenes_app.state.mac.scenes.render(scene['id'])
    other = client.post('/api/scenes', json={'title': 'Une autre', 'text': SHORT + '\n\nMARCO : Encore.'}).json()
    until(lambda: client.get(f'/api/scenes/{other["id"]}').json()['check']['rendered'] == 3)
    assert len(tts.calls) == 3
    listed = {s['id']: s for s in client.get('/api/scenes').json()['scenes']}
    assert listed['ma']['rendered'] == 2 and listed['fete-surprise']['builtin']


def test_a_render_with_a_line_refused_says_so_to_its_last_frame(
    client: TestClient, scenes_app: FastAPI, tts: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One line refused at once, the two others rendered after it: the dashboard keeps the last frame, and shows the
    voices « on their way » while done < total with no error (Scenes.tsx)."""
    refused = 'Gemini a refusé la demande (erreur 400).'

    def answer(api_key: str, path: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        if body['input'][0]['content'][0]['text'] == 'Une autre fois.':
            raise GeminiError(refused, 400)  # a 4xx: not asked again
        time.sleep(0.3)
        return tts(api_key, path, body, timeout_s)

    monkeypatch.setattr(gemini, 'post', answer)
    published: list[Any] = []
    monkeypatch.setattr(scenes_app.state.mac.broker, 'publish', published.append)
    client.post('/api/scenes', json={'title': 'Trois', 'text': 'Il était une fois.\n\nUne autre fois.\n\nLa fin.'})

    def frames() -> list[tuple[int, int, str | None]]:
        return [(m.data.done, m.data.total, m.data.error) for m in list(published) if m.channel == 'scene_render']

    until(lambda: len(frames()) == 4)
    assert frames() == [(0, 3, None), (0, 3, refused), (1, 3, refused), (2, 3, refused)]


def test_a_voice_that_fails_for_another_reason_is_logged_and_said_in_french(
    client: TestClient,
    scenes_app: FastAPI,
    tts: FakeGemini,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full disk writing the cache: the dashboard reads a French sentence, the Mac's log the error itself."""

    def full(path: Path, data: bytes) -> None:
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(scene_voice, '_write', full)
    published: list[Any] = []
    monkeypatch.setattr(scenes_app.state.mac.broker, 'publish', published.append)

    def frames() -> list[tuple[int, int, str | None]]:
        return [(m.data.done, m.data.total, m.data.error) for m in list(published) if m.channel == 'scene_render']

    with caplog.at_level(logging.ERROR, logger='mac_server.scenes'):
        client.post('/api/scenes', json={'title': 'Une', 'text': 'Il était une fois.'})
        until(lambda: len(frames()) == 2)
    said = "Une réplique n'a pas pu être rendue\xa0: regarde les logs du Mac."
    assert frames() == [(0, 1, None), (0, 1, said)]
    logged = [record.exc_info[1] for record in caplog.records if record.getMessage() == 'a scene voice failed']
    assert [str(error) for error in logged] == ['[Errno 28] No space left on device']


def test_only_a_scenes_latest_render_is_heard(
    client: TestClient, scenes_app: FastAPI, tts: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused line rewritten and saved while the two others render: the older render's frames, its refusal in
    them, stop; the newer one, sharing those two lines, counts them (Play.tsx ends a wait on any error frame)."""
    refused, release = 'Gemini a refusé la demande (erreur 400).', threading.Event()

    def answer(api_key: str, path: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        if body['input'][0]['content'][0]['text'] == 'Une autre fois.':
            raise GeminiError(refused, 400)
        release.wait(5)
        return tts(api_key, path, body, timeout_s)

    monkeypatch.setattr(gemini, 'post', answer)
    published: list[Any] = []
    monkeypatch.setattr(scenes_app.state.mac.broker, 'publish', published.append)

    def frames() -> list[tuple[int, int, str | None]]:
        return [(m.data.done, m.data.total, m.data.error) for m in list(published) if m.channel == 'scene_render']

    scene_id = client.post('/api/scenes', json={'title': 'Trois', 'text': 'Il était une fois.\n\nUne autre fois.\n\nLa fin.'}).json()['id']  # fmt: skip
    until(lambda: frames() == [(0, 3, None), (0, 3, refused)])
    client.put(f'/api/scenes/{scene_id}', json={'title': 'Trois', 'text': 'Il était une fois.\n\nUn autre jour.\n\nLa fin.'})  # fmt: skip
    release.set()
    until(lambda: (3, 3, None) in frames())  # each line's frames come in the order its renders asked for it
    assert frames()[2:] == [(0, 3, None), (1, 3, None), (2, 3, None), (3, 3, None)]


def test_a_line_the_latest_render_skips_still_gives_a_frame_once_rendered(
    client: TestClient, scenes_app: FastAPI, tts: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rehearsal's play (409) renders all but the rehearsed character's lines and is the latest render, while the
    save's render still has her line on its way: once it is there, a frame says so, its counts unchanged (the editor
    re-reads its « voix prêtes » on each frame, Scenes.tsx)."""
    others, hers = threading.Event(), threading.Event()

    def answer(api_key: str, path: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        (hers if 'comprendre' in body['input'][0]['content'][0]['text'] else others).wait(5)
        return tts(api_key, path, body, timeout_s)

    monkeypatch.setattr(gemini, 'post', answer)
    published: list[Any] = []
    monkeypatch.setattr(scenes_app.state.mac.broker, 'publish', published.append)

    def frames() -> list[tuple[int, int, str | None]]:
        return [(m.data.done, m.data.total, m.data.error) for m in list(published) if m.channel == 'scene_render']

    text = '@ANTIGONE (Kore) : sombre\n\nIl était une fois.\n\nANTIGONE : Je ne veux pas comprendre.\n\nLa fin.'
    scene_id = client.post('/api/scenes', json={'title': 'Deux voix', 'text': text}).json()['id']
    until(lambda: frames() == [(0, 3, None)])
    assert client.post(f'/api/scenes/{scene_id}/play', json={'rehearse': 'ANTIGONE'}).status_code == 409
    others.set()
    until(lambda: frames()[1:] == [(0, 2, None), (1, 2, None), (2, 2, None)])
    hers.set()
    until(lambda: scenes_app.state.mac.scenes.out(scene_id).check.rendered == 3)
    until(lambda: len(frames()) == 5, timeout=1.0)
    assert frames()[4] == (2, 2, None)


def test_a_segment_is_rendered_once_however_often_it_is_asked(tmp_path: Path, tts: FakeGemini) -> None:
    tts.delay = 0.2
    voices = Voices(tmp_path, 'key')
    first, second = voices.render('8' * 40, 'Kore', '', 'Huit.'), voices.render('8' * 40, 'Kore', '', 'Huit.')
    first.result(5), second.result(5)
    assert first is second and len(tts.calls) == 1  # at once: one request
    voices.render('8' * 40, 'Kore', '', 'Huit.').result(5)
    assert len(tts.calls) == 1  # later: from the cache


class _Pool:
    """Stands in for the pool with no thread of its own, so that a render or a close that hangs leaves nothing to
    join at exit. `at_once`: a render has finished by the time submit returns (one that fails at once can); else each
    waits its turn, and shutdown cancels them in the closing thread, as the pool does."""

    def __init__(self, at_once: bool) -> None:
        self.at_once, self.waiting = at_once, list[Future[None]]()

    def submit(self, fn: Any, *args: Any) -> Future[None]:
        future: Future[None] = Future()
        if not self.at_once:
            self.waiting.append(future)
            return future
        try:
            future.set_result(fn(*args))
        except Exception as exc:  # noqa: BLE001 — kept on the future, as a pool does
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        for future in self.waiting if cancel_futures else ():
            future.cancel()


def returns(call: Any, within_s: float = 2.0) -> bool:
    """Whether `call`, run on its own thread, returns in time."""
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(within_s)
    return not thread.is_alive()


def test_a_render_already_finished_when_it_returns_never_hangs(tmp_path: Path) -> None:
    voices = Voices(tmp_path, '')  # no key: the render fails at once
    voices._pool = _Pool(at_once=True)  # type: ignore[assignment]
    rendered: list[Future[None]] = []
    assert returns(lambda: rendered.append(voices.render('d' * 40, 'Kore', '', 'Dé.')))
    with pytest.raises(GeminiError, match='GEMINI_API_KEY manque'):
        rendered[0].result(0)
    assert not voices._inflight and returns(lambda: voices.render('d' * 40, 'Kore', '', 'Dé.'))  # tried again


def test_closing_with_renders_waiting_their_turn_never_hangs(tmp_path: Path) -> None:
    voices = Voices(tmp_path, 'key')
    voices._pool = pool = _Pool(at_once=False)  # type: ignore[assignment]
    waiting = [voices.render(f'{n:040x}', 'Kore', '', f'Ligne {n}.') for n in range(2)]
    assert returns(voices.close)
    assert pool.waiting == waiting and all(future.cancelled() for future in waiting)
    assert not voices._inflight


def test_a_mac_without_a_key_saves_its_scenes_at_once_and_says_why_their_voices_wait(config: Config) -> None:
    app = create_app(dataclasses.replace(config, gemini_api_key=''))  # each render fails at once, on a real pool
    published: list[Any] = []
    app.state.mac.broker.publish = published.append
    codes: list[int] = []

    def errors() -> list[str]:
        return [m.data.error for m in list(published) if m.channel == 'scene_render' and m.data.error]

    def saves_then_shutdown() -> None:
        with TestClient(app, headers={'X-API-Key': KEY}) as client:
            for n in range(20):
                text = f'@NARRATEUR (Charon) : conteur\n\nIl était une fois, {n}.\n\nEt puis {n} fois.'
                codes.append(client.post('/api/scenes', json={'title': f'S{n}', 'text': text}).status_code)
            until(lambda: len(errors()) == 40)  # two lines a scene, each said to have failed

    assert returns(saves_then_shutdown, 10.0), f'hung after {len(codes)} saves'
    assert codes == [200] * 20
    assert set(errors()) == {'GEMINI_API_KEY manque dans le .env du Mac\xa0: les voix ne peuvent pas être rendues.'}
    assert len(errors()) == 40


def test_a_scene_with_errors_saves_and_refuses_to_render(
    client: TestClient, scenes_app: FastAPI, tts: FakeGemini
) -> None:
    scene = client.post('/api/scenes', json={'title': 'Cassée', 'text': 'ATTENTION : non.'}).json()
    assert scene['check']['errors'][0]['line'] == 1
    with pytest.raises(HTTPException, match='ligne 1'):
        scenes_app.state.mac.scenes.render(scene['id'])
    time.sleep(0.05)
    assert tts.calls == []


def test_a_built_in_edited_saves_a_copy_that_shadows_it(client: TestClient, tts: FakeGemini) -> None:
    original = client.get('/api/scenes/voir-la-mer').json()
    edited = client.put('/api/scenes/voir-la-mer', json={'title': 'La mer', 'text': '@NARRATEUR (Kore) : x\n\nFin.'})
    assert edited.status_code == 200 and edited.json()['builtin']
    assert client.get('/api/scenes/voir-la-mer').json()['title'] == 'La mer'
    assert [s['id'] for s in client.get('/api/scenes').json()['scenes']][1] == 'voir-la-mer'  # keeps its place
    assert client.delete('/api/scenes/voir-la-mer').status_code == 204  # back as shipped
    assert client.get('/api/scenes/voir-la-mer').json() == original
    refused = client.delete('/api/scenes/voir-la-mer')
    assert refused.status_code == 409 and refused.json() == {'detail': 'Une scène fournie ne se supprime pas.'}
    assert client.get('/api/scenes/nope').status_code == 404


@pytest.mark.parametrize(
    'stored',
    [
        '{"scenes": [{"id": "a", "title": "A", "text": "Fin."},]}',  # a hand edit's trailing comma
        '{"scenes": {"a": {"title": "A", "text": "Fin."}}}',  # another shape
        '{"scenes": [{"id": "a", "title": "A", "text": "Fin."}, {"id": "b", "title": "B"}]}',  # one entry cut short
    ],
)
def test_an_unreadable_scenes_file_is_never_written_over(
    client: TestClient, scenes_app: FastAPI, tts: FakeGemini, stored: str
) -> None:
    path = scenes_app.state.mac.config.scenes_path
    path.write_text(stored)
    scene = {'title': 'Nouvelle', 'text': '@NARRATEUR (Kore) : x\n\nFin.'}
    for refused in (
        client.post('/api/scenes', json=scene),
        client.put('/api/scenes/voir-la-mer', json=scene),
        client.delete('/api/scenes/a'),
    ):
        assert refused.status_code == 500 and 'illisible' in refused.json()['detail'], refused.text
    assert path.read_text() == stored and tts.calls == []
    assert client.get('/api/scenes').status_code == 200  # the built-ins still list


def test_check_and_vocabulary(client: TestClient) -> None:
    check = client.post('/api/scenes/check', json={'title': 'x', 'text': 'Il pleut. [triste]'}).json()
    assert check['errors'] == [
        {'line': 1, 'message': "[triste] n'est ni une émotion, ni un geste, ni une pause (veux-tu dire [sad1]\xa0?)"}
    ]
    words = client.get('/api/scenes/vocabulary').json()
    assert {'word': 'hoche la tête', 'label': 'Oui de la tête', 'family': ''} in words['gestures']
    assert {'word': 'furious1', 'label': 'Furieux', 'family': 'Colère'} in words['emotions']


def _render_all(client: TestClient, scene_id: str) -> None:
    client.app.state.mac.scenes.render(scene_id)  # type: ignore[attr-defined]

    def done() -> bool:
        check = client.get(f'/api/scenes/{scene_id}').json()['check']
        return check['rendered'] == check['segments']

    until(done)


def test_a_play_waits_for_the_voices(client: TestClient, scenes_app: FastAPI, tts: FakeGemini) -> None:
    mac = scenes_app.state.mac
    scene_id = client.post('/api/scenes', json={'title': 'Courte', 'text': SHORT}).json()['id']
    until(lambda: not mac.scenes.voices._inflight)
    tts.errors = []
    (mac.scenes.voices.dir / f'{mac.scenes.check(SHORT).cues[0].key}.json').unlink()  # one voice missing
    waiting = client.post(f'/api/scenes/{scene_id}/play')  # the robot off: its voices are prepared all the same
    assert waiting.status_code == 409 and 'se préparent (1/2)' in waiting.json()['detail']
    until(lambda: len(tts.calls) == 3 and not mac.scenes.voices._inflight)
    assert client.post(f'/api/scenes/{scene_id}/play').json() == {'detail': ROBOT_OFF}  # no daemon, no heartbeat
    assert client.post('/api/scenes/stop').json() == {'detail': ROBOT_OFF}
    mac.health.beat(Heartbeat(awake=False))  # the robot runs, asleep
    assert client.post(f'/api/scenes/{scene_id}/play').status_code == 204
    command = client.get('/api/robot/commands', params={'wait': 0}).json()['command']
    assert command['action'] == 'scene'
    cues = [(c.at_ms, c.kind) for c in SceneTimeline.model_validate(command['scene']).cues]
    assert cues == [(0, 'audio'), (1700, 'audio'), (2000, 'emotion'), (3400, 'turn')]
    assert client.post(f'/api/scenes/{scene_id}/play', json={'start': 6}).status_code == 204  # from MARCO's line
    jump = SceneTimeline.model_validate(
        client.get('/api/robot/commands', params={'wait': 0}).json()['command']['scene']
    )
    assert [(c.at_ms, c.kind) for c in jump.cues] == [(0, 'audio'), (300, 'emotion'), (1700, 'turn')]
    assert jump.start == 6
    assert client.post(f'/api/scenes/{scene_id}/play', json={'start': 99}).status_code == 422  # nothing after it
    assert client.post('/api/scenes/stop').status_code == 204
    assert client.get('/api/robot/commands', params={'wait': 0}).json()['command']['action'] == 'scene_stop'


@pytest.mark.parametrize('why', ['both daily quotas spent', 'a line Gemini refuses'])
def test_voices_that_cannot_render_now_say_why_not_to_retry_in_a_minute(
    client: TestClient, scenes_app: FastAPI, tts: FakeGemini, why: str
) -> None:
    mac = scenes_app.state.mac
    refused = GeminiError('Gemini a refusé la demande (erreur 400).', 400)
    tts.errors = [per_day()] * 4 if why == 'both daily quotas spent' else [refused] * 4  # the save's, the 1st ask's
    client.post('/api/scenes', json={'title': 'Courte', 'text': SHORT})  # its voices are rendered, and fail
    until(lambda: len(tts.calls) >= 2 and not mac.scenes.voices._inflight)
    said = {
        'both daily quotas spent': 'Le quota de voix Gemini du jour est épuisé\xa0: il revient vers ',
        'a line Gemini refuses': 'Gemini a refusé la demande (erreur 400).',
    }[why]
    for _ in range(2):  # « joue la scène courte », again
        found = client.post('/api/scenes/find', json={'title': 'Courte'})
        assert found.status_code == 409
        assert found.json()['detail'].startswith('Les voix de «\xa0Courte\xa0» ne sont pas prêtes (0/2). ' + said)
        until(lambda: not mac.scenes.voices._inflight)
    if why == 'a line Gemini refuses':  # tried again at each ask: once Gemini takes it, the scene plays
        assert client.post('/api/scenes/find', json={'title': 'Courte'}).json()['timeline']['title'] == 'Courte'


def test_voices_on_a_mac_without_a_key_say_so(config: Config) -> None:
    with TestClient(create_app(config), headers={'X-API-Key': KEY}) as client:
        client.post('/api/scenes', json={'title': 'Courte', 'text': SHORT})
        found = client.post('/api/scenes/find', json={'title': 'Courte'})
    assert found.status_code == 409
    assert 'GEMINI_API_KEY manque dans le .env du Mac' in found.json()['detail']


@pytest.mark.parametrize(
    'sidecar', ['{"duration": 1.7,', '[1]', '{"duration": "1.7", "onset": 0.3, "offset": 1.3, "gaps": []}']
)  # cut, another shape, a value of another type
def test_an_unreadable_sidecar_is_a_missing_voice_rendered_again(
    client: TestClient, scenes_app: FastAPI, tts: FakeGemini, sidecar: str
) -> None:
    voices = scenes_app.state.mac.scenes.voices
    scene_id = client.post('/api/scenes', json={'title': 'Courte', 'text': SHORT}).json()['id']
    until(lambda: len(tts.calls) == 2 and not voices._inflight)
    key = scenes_app.state.mac.scenes.check(SHORT).cues[0].key
    (voices.dir / f'{key}.json').write_text(sidecar)
    assert client.get(f'/api/scenes/{scene_id}').json()['check']['rendered'] == 1
    assert voices.meta(key) is None
    _render_all(client, scene_id)
    assert len(tts.calls) == 3 and voices.meta(key)['onset'] == 0.3  # type: ignore[index]
    assert scenes_app.state.mac.scenes.timeline(scene_id).cues


def test_the_robot_finds_a_scene_by_its_spoken_title(client: TestClient, scenes_app: FastAPI, tts: FakeGemini) -> None:
    assert client.post('/api/scenes/find', json={'title': ''}).json()['titles'][0] == 'La fête surprise'
    missing = client.post('/api/scenes/find', json={'title': 'la scène du dragon'})
    assert missing.status_code == 409 and 'se préparent (0/13)' in missing.json()['detail']  # and they are on their way
    until(lambda: len(tts.calls) == 13 and not scenes_app.state.mac.scenes.voices._inflight)
    found = client.post('/api/scenes/find', json={'title': 'raconte-moi le dragon'}).json()
    assert found['timeline']['title'] == 'Le dragon enrhumé'
    lost = client.post('/api/scenes/find', json={'title': "l'anniversaire"})
    assert lost.status_code == 404 and '«\xa0La fête surprise\xa0»' in lost.json()['detail']
    client.post('/api/scenes', json={'title': 'Cassée', 'text': 'ATTENTION : non.'})
    broken = client.post('/api/scenes/find', json={'title': 'cassée'})
    assert broken.status_code == 422 and 'ligne 1' in broken.json()['detail']


def test_a_scene_too_long_is_refused_with_its_limit_written_the_french_way(client: TestClient) -> None:
    long = 'MARCO : Bonjour.\n' * (SCENE_TEXT_MAX // 17 + 1)
    for path in ('/api/scenes', '/api/scenes/check'):
        answer = client.post(path, json={'title': 'Longue', 'text': long})
        assert (answer.status_code, answer.json()) == (422, {'detail': 'Texte\xa0: 40\u202f000 caractères au plus.'})


def _voiced(scenes_app: FastAPI, scene_id: str) -> None:
    """A scene's voices on disk as if rendered: nothing renders in the background."""
    scenes = scenes_app.state.mac.scenes
    scenes.voices.dir.mkdir(parents=True, exist_ok=True)
    for cue in scenes.out(scene_id).check.cues:
        if cue.kind == 'say' and cue.key:
            (scenes.voices.dir / f'{cue.key}.wav').write_bytes(tone_wav())
            (scenes.voices.dir / f'{cue.key}.json').write_text(json.dumps(measure(tone_wav())))


def test_a_title_that_names_none_is_the_scene_the_dashboard_shows(client: TestClient, scenes_app: FastAPI) -> None:
    broker, find = scenes_app.state.mac.broker, lambda said: client.post('/api/scenes/find', json={'title': said})
    _voiced(scenes_app, 'dragon-enrhume')
    assert find('celle-ci').json()['titles'][0] == 'La fête surprise'  # nothing shown: the list, to ask which
    broker.show('a dashboard', (time.time(), 'scene', 'dragon-enrhume'))
    for said in ('celle-ci', 'celle là', 'la scène', 'joue cette scène', "celle qui est à l'écran", 'celle affichée'):
        found = find(said)
        assert found.status_code == 200 and found.json()['timeline']['title'] == 'Le dragon enrhumé', said
    assert find('').json()['titles'][0] == 'La fête surprise'  # no title: the list, a scene shown or not
    broker.show('a dashboard', (time.time(), 'scene', 'fete-surprise'))
    assert find('le dragon').json()['timeline']['title'] == 'Le dragon enrhumé'  # a name said beats the screen
    broker.show('a dashboard', (time.time(), 'scene', 'supprimee'))
    assert find('celle-ci').json()['titles'][0] == 'La fête surprise'  # shown, then deleted: the list


ANTIGONE = '@ANTIGONE (Kore) : sombre\n\nANTIGONE : Je ne veux pas comprendre.'


def test_a_title_that_names_no_scene_plays_none(client: TestClient, scenes_app: FastAPI, tts: FakeGemini) -> None:
    """« joue la scène du fantôme », and there is none: the list, for the robot to ask, never the closest spelling."""
    scene_id = client.post('/api/scenes', json={'title': 'Antigone', 'text': ANTIGONE}).json()['id']
    for key in (scene_id, *BUILTINS):
        _voiced(scenes_app, key)
    played = {}
    for said in ('le fantôme', 'la chanson', 'le lion', 'la reine', 'la plage', 'noël', 'la princesse', 'le serpent',
                 'Merlin', 'la voiture'):  # fmt: skip
        found = client.post('/api/scenes/find', json={'title': said})
        if found.status_code != 404 or '«\xa0Antigone\xa0»' not in found.json()['detail']:
            played[said] = found.json().get('timeline', {}).get('title') or found.json()
    assert played == {}
    for said, title in {'la présentation': 'Reachy se présente', 'Antigonne': 'Antigone', 'le croisant':
                        "L'affaire du croissant disparu", 'les fêtes': 'La fête surprise'}.items():  # fmt: skip
        assert client.post('/api/scenes/find', json={'title': said}).json()['timeline']['title'] == title, said


def test_a_title_needs_half_its_words_said_to_play(client: TestClient, scenes_app: FastAPI, tts: FakeGemini) -> None:
    """Half the words of the shorter side: « le robot pirate » plays « Le robot qui voulait voir la mer », one word in
    two; « le robot pirate volant », one in three, plays nothing (nor renders its voices)."""
    for key in BUILTINS:
        _voiced(scenes_app, key)
    found = client.post('/api/scenes/find', json={'title': 'le robot pirate'})
    assert found.json()['timeline']['title'] == 'Le robot qui voulait voir la mer'
    found = client.post('/api/scenes/find', json={'title': 'le robot pirate volant'})
    assert found.status_code == 404
    assert found.json()['detail'].startswith('Aucune scène ne ressemble à «\xa0le robot pirate volant\xa0».')


def test_a_plural_finds_the_scene_its_singular_names(client: TestClient, scenes_app: FastAPI, tts: FakeGemini) -> None:
    """« les chats » is « Le chat botté »: the plural and the singular are one word, whatever silent letter ends it."""
    played = {}
    for title, said in {'Le chat botté': 'les chats', 'La nuit': 'les nuits', 'Les pieds nus': 'le pied'}.items():
        scene_id = client.post('/api/scenes', json={'title': title, 'text': ANTIGONE}).json()['id']
        _voiced(scenes_app, scene_id)
        found = client.post('/api/scenes/find', json={'title': said}).json()
        played[said] = found.get('timeline', {}).get('title') or found['detail']
    assert played == {'les chats': 'Le chat botté', 'les nuits': 'La nuit', 'le pied': 'Les pieds nus'}


def test_the_audio_is_served_as_rendered(client: TestClient, scenes_app: FastAPI, tts: FakeGemini) -> None:
    key = scenes_app.state.mac.scenes.check(SHORT).cues[0].key
    assert client.get(f'/api/scenes/audio/{key}').status_code == 404
    scenes_app.state.mac.scenes.voices.render(key, 'Charon', 'conteur', 'Il était une fois.').result(5)
    served = client.get(f'/api/scenes/audio/{key}')
    assert served.headers['content-type'] == 'audio/wav' and served.content == tone_wav()


def test_the_catalogue_lists_the_playable_scenes(client: TestClient) -> None:
    client.post('/api/scenes', json={'title': 'Cassée', 'text': 'ATTENTION : non.'})
    entries = [e for e in client.get('/api/catalog').json()['entries'] if e['id'].startswith('scene:')]
    assert [e['action']['scene_id'] for e in entries] == [
        'fete-surprise',
        'voir-la-mer',
        'presentation',
        'dragon-enrhume',
        'croissant-disparu',
    ]
    assert entries[0] | {'action': None} == {
        'id': 'scene:fete-surprise',
        'group': 'scenes',
        'label': 'La fête surprise',
        'description': 'Une scène à plusieurs voix, jouée par le robot.',
        'say': 'joue la scène “La fête surprise”',
        'tool': 'play_scene',
        'action': None,
        'preview': True,
    }


def test_a_voice_is_served_at_the_speakers_rate() -> None:
    """The robot asks for its speaker's rate: a third less to send over its Wi-Fi, nothing to resample there."""
    rate, seconds = 24_000, 1.5
    tone = (np.sin(np.arange(int(rate * seconds)) * 2 * np.pi * 440 / rate) * 12_000).astype('<i2')
    source = io.BytesIO()
    with wave.open(source, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(tone.tobytes())
    wav = source.getvalue()
    assert scene_voice.at_rate(wav, None) == wav and scene_voice.at_rate(wav, rate) == wav
    with wave.open(io.BytesIO(scene_voice.at_rate(wav, 16_000))) as reader:
        assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (16_000, 1, 2)
        assert reader.getnframes() == int(16_000 * seconds)
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')
    assert 10_000 < np.abs(samples).max() <= 12_100  # the tone kept its level
