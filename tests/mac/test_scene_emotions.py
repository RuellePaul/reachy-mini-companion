"""« Ajouter des émotions »: the places a move may go without changing a word, the picks written in (spaced by the
moves' lengths, never re-rendering a voice), and the route around one Claude Code turn (faked)."""

from __future__ import annotations

import itertools
import json
import random
import threading
import time
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mac_server import claude, scene_emotions, scene_text
from mac_server.contracts import SCENE_TEXT_MAX
from mac_server.scenes import ASSETS, BUILTINS
from tests.mac.conftest import KEY

CAST = '@NARRATEUR (Charon) : conteur\n@MARCO (Algenib) : bourru, fâché\n\n'


def keys(text: str) -> list[str | None]:
    return [c.key for c in scene_text.parse(text, 'Kore').cues if c.kind == 'say']


def test_the_places_are_a_lines_first_word_and_the_word_after_a_mark() -> None:
    text = CAST + 'MARCO (fort) : Oui. Non, (en riant, fort) jamais ! [sad1] Fin\nIl part.\n\n// Non. Jamais.'
    spots, written = scene_emotions.places(text)
    # « Oui », « Non », « Il »: none before a bracket (« [sad1] Fin », « (en riant… »), none inside one
    assert [text[at : at + 6] for at, _, _ in spots] == ['Oui. N', 'Non, (', 'Il par']
    assert [seconds for _, seconds, _ in written] == [scene_text.MOVES['Tristesse']['sad1'][1]]
    assert spots[1][1] - spots[0][1] == len('Oui. ') and written[0][0] > spots[1][1]  # characters said before each
    assert [block for *_, block in spots] == [1, 1, 1]  # MARCO's turn goes on over the next line
    assert scene_emotions.shown('MARCO : Oui. Non.') == 'MARCO : {1}Oui. {2}Non.'


UNCAST = '@MARCO (Algenib) : x\n\nNarrateur : Il était une fois.\n\nMARCO : <rire> Oui. Non !\n\n(très doucement) Fin.'


@pytest.mark.parametrize('scene_id', [*BUILTINS, 'uncast'])
def test_moves_written_at_every_place_change_no_voice(scene_id: str) -> None:
    text = UNCAST if scene_id == 'uncast' else (ASSETS / f'{scene_id}.txt').read_text(encoding='utf-8')
    spots, _ = scene_emotions.places(text)
    errors = len(scene_text.parse(text, 'Kore').errors)
    for at, *_ in spots:  # each place alone, no spacing, no fallback
        alone = scene_emotions._put(text, [(at, 'sad1')])
        assert keys(alone) == keys(text) and len(scene_text.parse(alone, 'Kore').errors) == errors, text[at : at + 20]
    rng = random.Random(scene_id)
    out, added = scene_emotions.write_in(text, [(n, rng.choice(list(scene_text.EMOTIONS))) for n in range(1, 99)])
    assert 0 < added <= len(spots) and keys(out) == keys(text)
    assert scene_text.parse(out, 'Kore').errors == []


def test_picks_are_spaced_by_the_moves_lengths_and_junk_is_left_out() -> None:
    text = CAST + 'MARCO : ' + ' '.join(f'Phrase {n}.' for n in range(1, 30))  # 9-10 characters a phrase
    out, added = scene_emotions.write_in(text, [(n, 'sad1') for n in range(1, 30)])  # sad1 holds 4 s: ~49 characters
    starts = [out.index(f'[sad1] Phrase {n}.') for n in range(1, 30) if f'[sad1] Phrase {n}.' in out]
    assert added == len(starts) and 5 <= added <= 7 and all(b - a > 45 for a, b in itertools.pairwise(starts))
    assert scene_emotions.write_in(text, [(0, 'sad1'), (99, 'sad1'), (3, 'triste'), (3, 'yes1'), (3, 'no1')]) == (
        text.replace('Phrase 3.', '[no1] Phrase 3.'),  # out of range, unknown, and one per place: the last one
        1,
    )
    already = text.replace('Phrase 2.', '[sad1] Phrase 2.')
    assert '[yes1] Phrase 4.' not in scene_emotions.write_in(already, [(4, 'yes1')])[0]  # would cut sad1 short
    two = CAST + 'MARCO : [sad1] Oui.\n\nNARRATEUR : Il part.'  # another turn: MARCO's move ends there
    assert scene_emotions.write_in(two, [(1, 'hoche la tête')]) == (
        two.replace('Il part', '[hoche la tête] Il part'),
        1,
    )


def turns(count: int, words: str = '') -> str:
    """A scene of `count` one-line turns, a place each."""
    return CAST + '\n\n'.join(f'{"MARCO" if n % 2 else "NARRATEUR"} : Phrase {n}{words}.' for n in range(count))


def test_what_is_written_in_stays_within_the_scenes_caps() -> None:
    """The earliest picks go in, up to the cue cap, then while the text stays within SceneIn's length."""
    near = scene_text.MAX_CUES - 5
    out, added = scene_emotions.write_in(turns(near), [(n, 'yes1') for n in range(1, near + 1)])
    assert (added, len(scene_text.parse(out, 'Kore').cues)) == (5, scene_text.MAX_CUES)
    assert '[yes1] Phrase 4.' in out and '[yes1] Phrase 5.' not in out

    long = turns(194, ' mot' * 45)
    out, added = scene_emotions.write_in(long, [(n, 'yes1') for n in range(1, 195)])
    assert len(long) < SCENE_TEXT_MAX < len(long) + 194 * len('[yes1] ')  # every pick would not fit
    assert len(out) <= SCENE_TEXT_MAX < len(out) + len('[yes1] ') and 0 < added < 194
    assert '[yes1] Phrase 0 ' in out and '[yes1] Phrase 193 ' not in out


class FakeClaude:
    """Stands for claude.Run: each run answers `answer`; the next `parked` runs read until cancelled (5 s at most);
    with `held`, the next run reads on through its cancels until that event is set."""

    def __init__(self) -> None:
        self.answer = '{"moves": [{"place": 1, "emotion": "curious1"}, {"place": 9, "emotion": "sad2"}]}'
        self.asked: list[tuple[str, str, dict]] = []
        self.parked = 0
        self.cancelled = 0
        self.held: threading.Event | None = None

    def __call__(self, text: str, system: str, **options: object) -> FakeClaude:
        self.asked.append((text, system, options))
        return self

    def cancel(self) -> None:
        self.cancelled += 1

    def wait(self) -> claude.Answer:
        if self.held is not None:
            held, self.held, cancelled = self.held, None, self.cancelled
            assert held.wait(5)
            if self.cancelled != cancelled:
                raise claude.Cancelled('Construction annulée.')
        if self.parked:
            self.parked -= 1
            cancelled = self.cancelled
            _until(lambda: self.cancelled != cancelled)
            raise claude.Cancelled('Construction annulée.')
        if isinstance(self.answer, Exception):
            raise self.answer
        return claude.Answer(self.answer, None, None)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    fake = FakeClaude()
    monkeypatch.setattr(scene_emotions.claude, 'Run', fake)
    return fake


def test_the_route_writes_claudes_picks_in(client: TestClient, fake: FakeClaude) -> None:
    text = CAST + 'MARCO : Paris. ' + 'Bla bla. ' * 20  # curious1 holds 4 s (~49 characters): place 4 comes too soon
    picks = ((1, 'curious1'), (4, 'sad1'), (12, 'x'), (15, 'sad2'))  # and « x » is no move
    fake.answer = json.dumps({'moves': [{'place': n, 'emotion': e} for n, e in picks]})
    reply = client.post('/api/scenes/emotions', json={'title': 'Le croissant', 'text': text})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body['added'] == 2 and keys(body['text']) == keys(text) and '[curious1] Paris.' in body['text']
    assert '[sad1]' not in body['text'] and '[sad2]' in body['text']
    asked, system, options = fake.asked[0]
    assert asked.startswith('Scène «\xa0Le croissant\xa0»\xa0:') and '{1}Paris.' in asked
    assert all(name in system for name in scene_text.EMOTIONS) and '{catalogue}' not in system
    assert options == {'effort': scene_emotions.EFFORT, 'timeout_s': scene_emotions.TIMEOUT_S, 'what': 'les émotions'}


@pytest.mark.parametrize(
    ('answer', 'status'),
    [('Je ne sais pas.', 502), (claude.ClaudeError('La commande claude est introuvable sur ce Mac.'), 503)],
)
def test_a_useless_answer_or_no_claude_is_said(
    client: TestClient, fake: FakeClaude, answer: object, status: int
) -> None:
    fake.answer = answer  # type: ignore[assignment]
    reply = client.post('/api/scenes/emotions', json={'title': 'X', 'text': CAST + 'MARCO : Oui.'})
    assert reply.status_code == status and reply.json()['detail']
    nothing = client.post('/api/scenes/emotions', json={'title': 'X', 'text': '// rien\n@MARCO (Algenib) : x'})
    assert nothing.status_code == 422


def test_one_run_at_a_time_the_latest_requests(client: TestClient, fake: FakeClaude) -> None:
    fake.parked = 1
    body = {'title': 'X', 'text': CAST + 'MARCO : Oui. Non.'}
    results: list = []
    first = threading.Thread(target=lambda: results.append(client.post('/api/scenes/emotions', json=body)))
    first.start()
    _until(lambda: fake.asked)
    second = client.post('/api/scenes/emotions', json={**body, 'title': 'Y'})
    first.join(5)
    assert (results[0].status_code, results[0].json()) == (409, {'detail': scene_emotions.REPLACED})
    assert second.status_code == 200 and second.json()['added'] >= 1  # it took the first one's place
    assert (fake.cancelled, [asked.split('\xa0»')[0] for asked, *_ in fake.asked]) == (
        1,
        ['Scène «\xa0X', 'Scène «\xa0Y'],
    )
    assert client.post('/api/scenes/emotions', json=body).status_code == 200  # and the turn is free again


def test_a_run_that_does_not_end_when_cancelled_makes_the_newer_request_busy_not_stuck(
    client: TestClient, fake: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scene_emotions, 'CANCEL_WAIT_S', 0.2)
    fake.held = release = threading.Event()
    results: dict[str, httpx.Response] = {}
    post = _poster(client, results)
    first = post('A')
    _until(lambda: fake.asked)
    second = post('B')
    second.join(3)  # far above CANCEL_WAIT_S: the newer request does not wait on a run that does not end
    busy = results.get('B')  # answered while A's run still reads
    release.set()
    first.join(5)
    second.join(5)
    assert busy is not None and (busy.status_code, busy.json()) == (409, {'detail': scene_emotions.BUSY})
    assert (results['A'].status_code, results['A'].json()) == (409, {'detail': scene_emotions.REPLACED})
    post('C').join(5)
    assert results['C'].status_code == 200  # the turn is free again
    assert [asked.split('\xa0»')[0] for asked, *_ in fake.asked] == ['Scène «\xa0A', 'Scène «\xa0C']


def test_a_request_replaced_while_it_waits_never_starts_claude(client: TestClient, fake: FakeClaude) -> None:
    fake.held = release = threading.Event()
    results: dict[str, httpx.Response] = {}
    post = _poster(client, results)
    threads = [post('A')]
    _until(lambda: fake.asked)
    for n, title in enumerate('BC', 1):  # B waits for A's run, which reads on; then C comes
        threads.append(post(title))
        _until(lambda n=n: fake.cancelled == n)
    release.set()
    for thread in threads:
        thread.join(5)
    assert [results[title].status_code for title in 'ABC'] == [409, 409, 200]
    assert results['B'].json() == {'detail': scene_emotions.REPLACED}
    assert [asked.split('\xa0»')[0] for asked, *_ in fake.asked] == ['Scène «\xa0A', 'Scène «\xa0C']


def _poster(client: TestClient, results: dict[str, httpx.Response]) -> Callable[[str], threading.Thread]:
    """Posts a scene titled so in a thread of its own, its reply kept under its title."""

    def post(title: str) -> threading.Thread:
        body = {'title': title, 'text': CAST + 'MARCO : Oui. Non.'}
        thread = threading.Thread(
            target=lambda: results.update({title: client.post('/api/scenes/emotions', json=body)})
        )
        thread.start()
        return thread

    return post


def test_a_run_whose_requester_left_does_not_hold_the_next(live_server: str, fake: FakeClaude) -> None:
    """The dashboard drops its request when the owner leaves the scene: the next request is not refused for it."""
    fake.parked = 1
    body = {'title': 'A', 'text': CAST + 'MARCO : Oui. Non.'}
    with pytest.raises(httpx.TimeoutException):  # the owner leaves scene A
        httpx.post(f'{live_server}/api/scenes/emotions', json=body, headers={'X-API-Key': KEY}, timeout=0.5)
    assert (len(fake.asked), fake.cancelled) == (1, 0)  # Claude still reads scene A
    other = httpx.post(
        f'{live_server}/api/scenes/emotions', json={**body, 'title': 'B'}, headers={'X-API-Key': KEY}, timeout=5
    )
    assert (other.status_code, fake.cancelled, len(fake.asked)) == (200, 1, 2), other.text


def test_the_servers_shutdown_ends_the_run_in_progress(app: FastAPI, fake: FakeClaude) -> None:
    """The server stops while Claude reads a scene: the run ends, nobody being left to read its answer."""
    fake.parked = 1
    refused: list[HTTPException] = []

    def ask() -> None:
        try:
            scene_emotions.add('A', CAST + 'MARCO : Oui. Non.')
        except HTTPException as exc:
            refused.append(exc)

    asking = threading.Thread(target=ask)
    with TestClient(app, headers={'X-API-Key': KEY}):
        asking.start()
        _until(lambda: fake.asked)
    assert fake.cancelled == 1, 'the run outlived the server'
    asking.join(5)
    assert [exc.status_code for exc in refused] == [409]


def _until(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'condition not met in time'
        time.sleep(0.01)
