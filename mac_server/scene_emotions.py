"""« Ajouter des émotions »: Claude reads a scene and picks where its characters show what they feel; the Mac writes
the moves in, only at places that change no word said (so no voice is rendered again), and only once the move before
has had its time. Claude chooses among numbered places and never rewrites the text: a model echoing a whole scene
drops or changes characters."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from fastapi import HTTPException

from mac_server import claude, scene_text
from mac_server.contracts import SCENE_TEXT_MAX, SceneEmotions
from robot_agent import animations

EFFORT = 'medium'
TIMEOUT_S = 600.0
CHARS_PER_S = 12.3  # the speech model's median pace
SLACK = 0.5  # a move may be cut by the next one once it has played half of itself...
HOLD_S = 4.0  # ...or this long: a 9 s emotion does not keep a long tirade still
SOUND_CHARS = 12  # what a <rire> is worth on the clock
_PROMPT = (Path(__file__).parent / 'assets' / 'emotions_prompt.txt').read_text(encoding='utf-8')
# What Claude may write: every recorded move, and the gestures by their first phrase (name -> seconds).
_GESTURES = {phrases[0]: animations.GESTURES[key].duration for key, phrases in scene_text.GESTURES.items()}
_SECONDS = {name: move[1] for family in scene_text.MOVES.values() for name, move in family.items()} | _GESTURES
_MARK = re.compile(r'[.,;:!?…»][ \t]+(?=[^\s\[<(])')  # a mark, then more words: a place before those words
_CLOSE = {'[': ']', '(': ')', '<': '>'}
BUSY = "Claude ajoute déjà des émotions à une scène\xa0: attends qu'il ait fini."
REPLACED = 'Une demande plus récente a pris sa place\xa0: celle-ci est abandonnée.'
CANCEL_WAIT_S = 10.0  # for the run a newer request cancelled to end


def _role_end(line: str, cast: set[str]) -> int:
    """Where a line's words start: after `NOM (jeu) :` when the parser reads it as a role, else 0."""
    role = scene_text._ROLE.match(line)
    if role is None:
        return 0
    name = role.group(1).strip()
    letters = [c for c in name if c.isalpha()]
    capitals = len(letters) >= 2 and all(c.isupper() for c in letters) and len(name.split()) <= 4
    return role.start(3) if name.upper() in cast or capitals else 0


def places(text: str) -> tuple[list[tuple[int, float, int]], list[tuple[float, float, int]]]:
    """(offset, characters said before it, block) of each place a move may go: a line's first word, the word after a
    mark, never inside a bracket; and the moves already written, as (characters said before, seconds, block). A block
    is one character's turn: a role line, or narration after a blank line, opens the next."""
    cast = {scene_text.NARRATOR} | {
        m.group(1).strip().upper() for m in re.finditer(r'(?m)^\s*@\s*([^:()]{1,40}?)\s*[(:]', text)
    }
    spots: list[tuple[int, float, int]] = []
    written: list[tuple[float, float, int]] = []
    pos, spoken, block, fresh = 0, 0.0, 0, True
    for raw in text.split('\n'):
        line = raw.strip()
        fresh = fresh or not line or line.startswith('@')
        if line and not line.startswith(('#', '//', '@')):
            role = _role_end(line, cast)
            block, fresh = block + (bool(role) or fresh), False
            start = len(raw) - len(raw.lstrip()) + role
            words = start + len(raw[start:]) - len(raw[start:].lstrip())
            starts = {m.end() for m in _MARK.finditer(raw, start)} | {words}
            i = start
            while i < len(raw):
                if i in starts and (
                    raw[i] not in '[<(' or (i == words and raw[i] != '[')
                ):  # a line may open on a sound
                    spots.append((pos + i, spoken, block))
                close = raw.find(_CLOSE.get(raw[i], '\0'), i)
                if close < 0:
                    spoken, i = spoken + 1, i + 1
                    continue
                inner = raw[i + 1 : close].strip()
                if raw[i] == '<':
                    spoken += SOUND_CHARS
                elif raw[i] == '[' and (pause := scene_text._PAUSE.match(scene_text.fold(inner).replace(',', '.'))):
                    spoken += CHARS_PER_S * float(pause.group(1) or scene_text.PAUSE_DEFAULT_S)
                elif raw[i] == '[' and scene_text.fold(inner) in _SECONDS:
                    written.append((spoken, _SECONDS[scene_text.fold(inner)], block))
                elif raw[i] == '[' and (gesture := scene_text._GESTURE_OF.get(scene_text._words(inner))):
                    written.append((spoken, animations.GESTURES[gesture].duration, block))
                i = close + 1
        pos += len(raw) + 1
    return spots, written


def write_in(text: str, picks: list[tuple[int, str]]) -> tuple[str, int]:
    """The text with the picked moves (place number, name) written in, those too close to another move of the same
    character's turn, or that would change a word, left out; and how many went in. A new turn ends the moves before
    it (the robot cuts them as another character speaks)."""
    spots, written = places(text)
    chosen = {n: name for n, name in picks if 1 <= n <= len(spots) and name in _SECONDS}  # one per place
    moves = sorted([(at, seconds, block, None) for at, seconds, block in written] + [
        (spots[n - 1][1], _SECONDS[name], spots[n - 1][2], (spots[n - 1][0], name)) for n, name in chosen.items()
    ], key=lambda move: (move[0], move[3] is not None))  # fmt: skip
    kept, free_at, turn = [], -1.0, -1
    for index, (at, seconds, block, pick) in enumerate(moves):
        free_at = free_at if block == turn else -1.0
        turn, lasts = block, min(SLACK * seconds, HOLD_S) * CHARS_PER_S
        ahead = (m for m in moves[index + 1 :] if m[2] == block and m[3] is None)
        crowded = at < free_at or any(m[0] < at + lasts for m in ahead)
        if pick is not None and not crowded:
            kept.append(pick)
        if pick is None or not crowded:
            free_at = max(free_at, at + lasts)
    before = scene_text.parse(text, 'Kore')
    kept = kept[: max(0, scene_text.MAX_CUES - len(before.cues))]  # the scene's caps, the earliest picks first
    while kept and len(_put(text, kept)) > SCENE_TEXT_MAX:
        kept.pop()

    def same(candidate: str) -> bool:
        after = scene_text.parse(candidate, 'Kore')
        keys = [[c.key for c in parsed.cues if c.kind == 'say'] for parsed in (before, after)]
        return keys[0] == keys[1] and len(after.errors) == len(before.errors)

    out = _put(text, kept)
    if not same(out):  # a place the parser reads otherwise: each pick tried alone, the faulty ones left out
        kept = [pick for pick in kept if same(_put(text, [pick]))]
        out = _put(text, kept)
    return (out, len(kept)) if same(out) else (text, 0)


def _put(text: str, picks: list[tuple[int, str]]) -> str:
    for at, name in sorted(picks, reverse=True):
        text = f'{text[:at]}[{name}] {text[at:]}'
    return text


def shown(text: str) -> str:
    """The text as Claude reads it: each place numbered `{n}`."""
    for n, (at, *_) in reversed(list(enumerate(places(text)[0], 1))):
        text = f'{text[:at]}{{{n}}}{text[at:]}'
    return text


class _Turn:
    """One Claude run at a time, the latest request's: a newer one cancels the run in progress (its requester has
    moved on, to another scene or a new try), and a request still waiting for its turn never starts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # guards the two fields
        self._free = threading.Lock()  # held while a run is in progress
        self._latest = 0
        self._run: claude.Run | None = None

    def ask(self, text: str, system: str) -> str:
        mine = self.stop()
        if not self._free.acquire(timeout=CANCEL_WAIT_S):
            raise HTTPException(409, detail=BUSY)
        try:
            with self._lock:
                if mine != self._latest:
                    raise HTTPException(409, detail=REPLACED)
                run = self._run = claude.Run(text, system, effort=EFFORT, timeout_s=TIMEOUT_S, what='les émotions')
            try:
                return run.wait().text
            finally:
                with self._lock:
                    self._run = None
        except claude.Cancelled as exc:
            raise HTTPException(409, detail=REPLACED) from exc
        except claude.ClaudeError as exc:
            raise HTTPException(503, detail=str(exc)) from exc
        finally:
            self._free.release()

    def stop(self) -> int:
        """What a newer request does, and the server's shutdown: the run in progress ends, a request waiting for its
        turn never starts. The newest turn."""
        with self._lock:
            self._latest += 1
            if self._run is not None:
                self._run.cancel()
            return self._latest


_turn = _Turn()
stop = _turn.stop


def system() -> str:
    """The director's prompt, with every move the robot has."""
    lines = [
        f'{name} | {family} | {label} | {seconds:g} s | {about}'
        for family, moves in scene_text.MOVES.items()
        for name, (label, seconds, about) in moves.items()
    ]
    lines += [
        f'{phrase} | Geste | {scene_text.anim.GESTURES[key][0]} | {_GESTURES[phrase]:g} s | un geste bref'
        for key, (phrase, *_) in scene_text.GESTURES.items()
    ]
    return _PROMPT.replace('{catalogue}', '\n'.join(lines))


def asked(title: str, text: str) -> str:
    """What Claude is given: the scene, its places numbered."""
    return f'Scène «\xa0{title}\xa0»\xa0:\n\n{shown(text)}'


def picks(answer: str) -> list[tuple[int, str]]:
    """The (place, move) pairs of Claude's answer; ValueError when it holds none that can be read."""
    try:
        moves = json.loads(answer[answer.index('{') : answer.rindex('}') + 1]).get('moves')
        return [(int(m['place']), str(m['emotion'])) for m in moves if isinstance(m, dict)]
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError('no moves') from exc


def add(title: str, text: str) -> SceneEmotions:
    """POST /api/scenes/emotions: one at a time, the latest request's, a few minutes at most; Claude's picks
    written in."""
    if not places(text)[0]:
        raise HTTPException(422, detail="Cette scène n'a pas de réplique où ajouter une émotion.")
    try:
        chosen = picks(_turn.ask(asked(title, text), system()))
    except ValueError as exc:
        raise HTTPException(502, detail="Claude n'a rien proposé d'utilisable\xa0: réessaie.") from exc
    out, added = write_in(text, chosen)
    return SceneEmotions(text=out, added=added)
