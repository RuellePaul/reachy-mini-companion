"""A scene as text: the strict play-script grammar, and the timeline its rendered voices give it. Pure, no I/O.

    @NOM (Voix, -60) : jeu       the cast; the narrator may omit its voice (it takes the robot's); an angle turns
                                 the robot to the character whenever it speaks
    NOM (jeu) : texte            a line; the lines under it, up to a blank line, are still NOM's
    texte                        narration, in the NARRATEUR's voice
    [sad1] [hoche la tête] [tourne à gauche] [pause 2]    the body: an emotion, a gesture, a turn, a silence
    <rire>                       a sound of the voice, performed by the speech model
    (chuchoté) …                 how it is said: right after the name, or at the start of a block
    # … or // …                  a comment

A block's words are one segment, rendered and cached once; only a silence splits it. A move written among them
fires at its share of the text over the voiced span, or, right after a punctuation mark, as the voice resumes after
the pause it makes there: adding a move renders nothing again. Whatever breaks a rule is an error with its line;
the scene then saves but does not play. The cache key and the text sent never change, so a line rendered once stays a hit.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from mac_server.contracts import CastMember, SceneCue, SceneError, SceneTimeline, SceneVocabulary, TimelineCue
from mac_server.voices import VOICES
from robot_agent import animation_names as anim

TTS_MODEL = 'gemini-3.8-flash-tts'  # in every cache key, even of a line its lite fallback rendered
NARRATOR = 'NARRATEUR'
TURN_SIDE_DEG = 35.0
TURN_MAX_DEG = 100.0  # robot_agent.config.BODY_YAW_LIMIT_DEG
PAUSE_RANGE_S = (0.2, 20.0)
PAUSE_DEFAULT_S = 1.5
MAX_BLOCK_CHARS = 1200
MAX_CUES = 400
SNAP_S = 1.0  # how far from its linear place a move written after a punctuation mark looks for the voice's pause

# Every recorded move the robot has (Pollen's emotions, its dances), by the feeling shown:
# family -> {name: [French label, seconds, Pollen's description]}.
MOVES: dict[str, dict[str, list]] = json.loads((Path(__file__).parent / 'assets' / 'moves.json').read_text('utf-8'))
EMOTIONS = {name: move[0] for family in MOVES.values() for name, move in family.items()}
# The gesture keys and how a scene writes them; the first phrase is the one the insert menu writes.
GESTURES: dict[str, tuple[str, ...]] = {
    'nod': ('hoche la tête', 'fait oui de la tête'), 'shake': ('secoue la tête', 'fait non de la tête'),
    'tilt_left': ('penche la tête à gauche',), 'tilt_right': ('penche la tête à droite',),
    'look_up': ('lève les yeux', "regarde en l'air"), 'look_down': ('baisse les yeux', 'regarde par terre'),
    'wiggle': ('remue les antennes',), 'bow': ('fait une révérence',), 'think': ('réfléchit',),
    'surprise': ('sursaute',), 'dance': ('danse',),
}  # fmt: skip
TURNS = {'tourne à gauche': TURN_SIDE_DEG, 'tourne à droite': -TURN_SIDE_DEG, 'face au public': 0.0}
# Sounds of the voice: the French word written in a scene -> the speech model's own tag.
SOUNDS = {
    'rire': 'laugh', 'petit rire': 'chuckle', 'gloussement': 'giggle', 'ricanement': 'snicker', 'soupir': 'sigh',
    'sanglot': 'sob', 'pleure': 'cry', 'souffle': 'breath', 'grand souffle': 'heavy breath', 'halète': 'pant',
    'toux': 'cough', 'éternue': 'sneeze', 'bâille': 'yawn', 'cri': 'shout', 'hurle': 'scream', 'grogne': 'growl',
    'gémit': 'groan', 'hoquet de surprise': 'gasp', 'raclement de gorge': 'throat-clearing', 'tss': 'tsk',
    'pff': 'pff', 'chuchote': 'whispers',
}  # fmt: skip
_DASHES = '-\u2013\u2014'
_CAST = re.compile(r'^@\s*([^:()]{1,40}?)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$')
_ROLE = re.compile(r'^([^\s:()\[\]<@][^:()\[\]<]{0,38}?)\s*(?:\(([^)]*)\))?\s*[:\u2013\u2014]\s*(.*)$')
_TOKEN = re.compile(r'(\[[^\[\]]*\]|\([^()]*\))')
_SOUND = re.compile(r'<([^<>]{1,40})>')
_PAUSE = re.compile(r'^(?:pause|silence)(?:\s+(\d+(?:[.,]\d+)?)\s*(?:s|sec|secondes?)?)?$')
_TURN_BY = re.compile(r'^tourne\s+(?:de\s+)?(-?\d+(?:[.,]\d+)?)\s*(?:°|degres?)?$')
_ANGLE = re.compile(r'^(?:(.*?\D)\s*,\s*)?([-+]?\d+(?:[.,]\d+)?)\s*(?:°|degr[ée]s?)?$', re.IGNORECASE)
_BREAK = tuple('.,;:!?…»)')


def fold(text: str) -> str:
    """Lower case, no accents, spaces collapsed."""
    decomposed = unicodedata.normalize('NFD', text.lower())
    return ' '.join(''.join(c for c in decomposed if unicodedata.category(c) != 'Mn').split())


def _words(text: str) -> str:
    """Folded, apostrophes and dashes as spaces, punctuation gone: how gestures and sounds are compared."""
    return ' '.join(re.sub('[\u2019\'`\\-!?.,;:«»"]', ' ', fold(text)).split())


_GESTURE_OF = {_words(phrase): key for key, phrases in GESTURES.items() for phrase in phrases}
_TURN_OF = {_words(phrase): degrees for phrase, degrees in TURNS.items()}
_SOUND_OF = {_words(word): tag for word, tag in SOUNDS.items()} | {tag: tag for tag in SOUNDS.values()}
# What an unknown bracket may have meant: written forms, and the French labels of the emotions.
_SUGGEST = {_words(family): next(iter(moves)) for family, moves in MOVES.items()}
_SUGGEST |= {_words(label.split(' — ')[0]): name for name, label in reversed(EMOTIONS.items())}
_SUGGEST |= {_words(label): name for name, label in EMOTIONS.items()} | {name: name for name in EMOTIONS}
_SUGGEST |= {_words(p): p for p in [*(p for phrases in GESTURES.values() for p in phrases), *TURNS, 'pause 2']}


def speech(text: str) -> str:
    """The text as the speech model gets it: `<rire>` becomes its tag `<laugh>`, an unknown sound is dropped."""

    def tag(match: re.Match[str]) -> str:
        found = _SOUND_OF.get(_words(match.group(1)))
        return f'<{found}>' if found else ' '

    return ' '.join(_SOUND.sub(tag, text).split())


def join(parts: list[str | None]) -> str:
    """A segment's words as said: its parts as written, each move (None) taken out with the space right before it,
    the one « Insérer » writes (« joues-tu [sad1]? » and « Bonjour [sad1] toi » are said as before the move)."""
    text = ''
    for part in parts:
        text = (text[:-1] if text[-1:].isspace() else text) if part is None else text + part
    return ' '.join(text.split())


def cache_key(voice: str, style: str, said: str) -> str:
    """A rendered line's key in the cache: it never changes, so that the lines already rendered stay hits."""
    return hashlib.sha1(f'{TTS_MODEL}\n{voice}\n{style}\n{said}'.encode()).hexdigest()


class Voice(NamedTuple):  # a rendered segment as scene_voice.measure found it, in seconds
    duration: float
    onset: float = 0.0
    offset: float = 0.0
    gaps: tuple[tuple[float, float], ...] = ()


@dataclass
class Parsed:
    cast: list[CastMember] = field(default_factory=list)
    cues: list[SceneCue] = field(default_factory=list)
    errors: list[SceneError] = field(default_factory=list)
    warnings: list[SceneError] = field(default_factory=list)
    # a move among a segment's words (after it in `cues`) -> (segment's index, share of text before it, mark before it)
    inside: dict[int, tuple[int, float, bool]] = field(default_factory=dict)


@dataclass
class _Block:
    who: str
    line: int
    direction: list[str]
    items: list[tuple[int, str | SceneCue]] = field(default_factory=list)  # (line, text or move or pause)

    def said(self) -> bool:
        return any(isinstance(item, str) and item.strip() for _, item in self.items)


class _Parser:
    def __init__(self, narrator_voice: str) -> None:
        self.out = Parsed()
        self.cast: dict[str, tuple[str, str]] = {NARRATOR: (narrator_voice, '')}  # name -> (voice, acting)
        self.angles: dict[str, float] = {}  # name -> where the robot turns when that character speaks
        self.used: list[str] = []
        self.block: _Block | None = None

    def error(self, line: int, message: str) -> None:
        self.out.errors.append(SceneError(line=line, message=message))

    def run(self, text: str) -> Parsed:
        for n, raw in enumerate(text.replace('\r\n', '\n').replace('\r', '\n').split('\n'), 1):
            line = raw.strip()
            if not line:
                self.flush()
            elif not line.startswith(('#', '//')):
                self.line(n, line)
        self.flush()
        self.out.cast = [
            CastMember(name=n, voice=v, acting=a, angle=self.angles.get(n))
            for n in self.used
            for v, a in [self.cast[n]]
        ]
        if len(self.out.cues) > MAX_CUES:
            self.error(self.out.cues[MAX_CUES].line, f'plus de {MAX_CUES} répliques et mouvements\xa0: coupe la scène')
        return self.out

    def line(self, n: int, line: str) -> None:
        if line.startswith('@'):
            self.flush()
            self.cast_line(n, line)
            return
        role = _ROLE.match(line)
        name = role.group(1).strip() if role else ''
        letters = [c for c in name if c.isalpha()]
        capitals = len(letters) >= 2 and all(c.isupper() for c in letters) and len(name.split()) <= 4
        if role and (name.upper() in self.cast or capitals):
            who = name.upper()
            if who not in self.cast:
                self.error(n, f'«\xa0{who}\xa0» n\'est pas dans la distribution\xa0: ajoute @{who} (Voix)\xa0: …, '
                           f'ou écris «\xa0{name.capitalize()}\xa0:\xa0»')  # fmt: skip
                who = NARRATOR
            self.open(n, who, [role.group(2).strip()] if role.group(2) else [])
            self.body(n, role.group(3))
        elif line[0] in _DASHES:
            self.error(n, 'une réplique commence par le nom de qui parle\xa0: MARCO\xa0: …')
        else:
            if self.block is None:
                self.open(n, NARRATOR, [])
            self.body(n, line)

    def cast_line(self, n: int, line: str) -> None:
        match = _CAST.match(line)
        if match is None:
            self.error(n, "une distribution s'écrit @NOM (Voix)\xa0: jeu")
            return
        name, hint, acting = match.group(1).strip().upper(), (match.group(2) or '').strip(), match.group(3).strip()
        last = hint.rsplit(',', 1)[-1].strip()
        if found := _ANGLE.match(hint):  # (Gacrux, -60), (Gacrux, -60,5) or, for the narrator, (0)
            hint, angle = (found.group(1) or '').strip(), found.group(2)
            value = float(angle.replace(',', '.'))
            self.angles[name] = self.clamp(n, angle, value, -TURN_MAX_DEG, TURN_MAX_DEG, '°')
        elif any(c.isdigit() for c in last):
            hint = hint.rsplit(',', 1)[0].strip() if ',' in hint else ''
            self.error(n, f"«\xa0{last}\xa0» n'est pas un angle\xa0: @{name} ({hint or 'Voix'}, -60)\xa0: …")
        voice = next((v for v in VOICES if v.lower() == hint.lower()), None)
        if hint and voice is None:
            close = difflib.get_close_matches(hint.capitalize(), VOICES, 1, 0.6)
            guess = f'veux-tu dire {close[0]}\xa0?' if close else 'ex. Kore, Puck, Algenib…'
            self.error(n, f"«\xa0{hint}\xa0» n'est pas une voix ({guess})")
        elif voice is None and name != NARRATOR:
            self.error(n, f'donne une voix à {name}\xa0: @{name} (Algenib)\xa0: …')
        self.cast[name] = (voice or self.cast.get(name, ('', ''))[0], acting)
        if name not in self.used:
            self.used.append(name)

    def open(self, n: int, who: str, direction: list[str]) -> None:
        self.flush()
        self.block = _Block(who, n, direction)

    def body(self, n: int, body: str) -> None:
        block = self.block
        assert block is not None
        rest = _SOUND.sub(' ', _TOKEN.sub(' ', body))
        for opener, closer, what in (('[', ']', 'crochet mal fermé'), ('(', ')', 'parenthèse mal fermée'),
                                     ('<', '>', 'chevron mal fermé')):  # fmt: skip
            if opener in rest or closer in rest:
                self.error(n, what)
        for token in _TOKEN.split(body):
            if token.startswith('(') and token.endswith(')'):
                if block.said():
                    self.error(
                        n,
                        f'une indication de jeu se met au début de la réplique\xa0: {block.who} ({token[1:-1].strip()})\xa0: …',
                    )
                elif token[1:-1].strip():
                    block.direction.append(token[1:-1].strip())
            elif token.startswith('[') and token.endswith(']'):
                cue = self.bracket(n, token[1:-1].strip())
                if cue is not None:
                    block.items.append((n, cue))
            elif token.strip():
                for sound in _SOUND.findall(token):
                    if _words(sound) not in _SOUND_OF:
                        self.error(n, f"<{sound}> n'est pas un son (rire, soupir, sanglot…)")
                block.items.append((n, token))

    def bracket(self, n: int, inner: str) -> SceneCue | None:
        folded, words = fold(inner), _words(inner)
        if pause := _PAUSE.match(folded.replace(',', '.')):
            seconds = float(pause.group(1)) if pause.group(1) else PAUSE_DEFAULT_S
            return SceneCue(line=n, kind='pause', seconds=self.clamp(n, inner, seconds, *PAUSE_RANGE_S, 's'))
        if words in _TURN_OF:
            return SceneCue(line=n, kind='turn', degrees=_TURN_OF[words])
        if turn := _TURN_BY.match(folded):
            degrees = float(turn.group(1).replace(',', '.'))
            return SceneCue(
                line=n, kind='turn', degrees=self.clamp(n, inner, degrees, -TURN_MAX_DEG, TURN_MAX_DEG, '°')
            )
        if words in _GESTURE_OF:
            return SceneCue(line=n, kind='gesture', name=_GESTURE_OF[words])
        if folded in EMOTIONS:
            return SceneCue(line=n, kind='emotion', name=folded)
        variants = sorted(name for name in EMOTIONS if name.rstrip('0123456789') == folded)
        if variants:
            self.error(n, f'[{inner}]\xa0: écris le nom exact, ex. [{variants[0]}]')
            return None
        close = difflib.get_close_matches(words, list(_SUGGEST), 1, 0.6)
        hint = f'veux-tu dire [{_SUGGEST[close[0]]}]\xa0?' if close else 'voir le menu Insérer'
        self.error(n, f"[{inner}] n'est ni une émotion, ni un geste, ni une pause ({hint})")
        return None

    def clamp(self, n: int, inner: str, value: float, low: float, high: float, unit: str) -> float:
        kept = max(low, min(high, value))
        if kept != value:
            bound = f'{kept:g}'.replace('.', ',')
            self.out.warnings.append(SceneError(line=n, message=f'[{inner}]\xa0: limité à {bound}{unit}'))
        return kept

    def flush(self) -> None:
        block, self.block = self.block, None
        if block is None:
            return
        voice, acting = self.cast[block.who]
        style = ', '.join(p for p in (acting, ', '.join(dict.fromkeys(block.direction))) if p)
        if not block.items:
            self.error(block.line, f'{block.who} ne dit rien')
        chars = sum(len(' '.join(item.split())) for _, item in block.items if isinstance(item, str))
        if chars > MAX_BLOCK_CHARS:
            self.error(block.line, f'réplique trop longue ({chars} caractères, {MAX_BLOCK_CHARS} au plus)')
        cues, said, moves, lines = self.out.cues, [], [], []
        angle = self.angles.get(block.who)
        if angle is not None and block.said():  # as if [tourne …] began the words (the robot skips a no-op)
            moves.append((0, False, SceneCue(line=block.line, kind='turn', degrees=angle)))

        def segment() -> None:
            text, index = join(said), len(cues)
            if text:
                key = cache_key(voice, style, speech(text))
                cues.append(SceneCue(line=lines[0], end_line=lines[-1], kind='say', character=block.who, text=text,
                                     voice=voice, style=style, key=key))  # fmt: skip
                if block.who not in self.used:
                    self.used.append(block.who)
            for before, broke, move in moves:
                if before < len(text):  # words follow it: it fires inside the segment
                    self.out.inside[len(cues)] = (index, before / len(text), broke)
                cues.append(move)
            for pending in (said, moves, lines):
                pending.clear()

        for n, item in block.items:
            if said and lines and n != lines[-1]:
                said.append('\n')  # a line break is a space
            if isinstance(item, str):
                said.append(item)
                lines.append(n)
            elif item.kind == 'pause':
                segment()  # a move before a silence fires before it
                cues.append(item)
            else:
                so_far = join(said)
                moves.append((len(so_far), so_far.endswith(_BREAK), item))
                said.append(None)
        segment()


def parse(text: str, narrator_voice: str) -> Parsed:
    """The scene's cast, cues (in order) and errors. `narrator_voice`: the robot's own, unless the cast says."""
    return _Parser(narrator_voice).run(text)


def timeline(
    scene_id: str,
    title: str,
    parsed: Parsed,
    audio: Callable[[str], Voice],
    rehearse: str | None = None,
) -> SceneTimeline:
    """The cues on one clock, in ms. `audio(key)` is a segment's measured voice: segments are laid back to back,
    pauses add their seconds, a move written among a segment's words fires inside it (`within`), any other move at
    the clock. The `rehearse` character's segments last 0 ms and have no audio: the human says them, however long
    they take."""
    out: list[TimelineCue] = []
    clock, spans = 0.0, {}
    for index, cue in enumerate(parsed.cues):
        if cue.kind == 'pause':
            clock += cue.seconds or 0.0
        elif cue.kind == 'say':
            key = None if cue.character == rehearse else cue.key
            voice = audio(key) if key else Voice(0.0)
            fields = {'key': key, 'duration_ms': _ms(voice.duration), 'character': cue.character, 'text': cue.text}
            out.append(TimelineCue(at_ms=_ms(clock), kind='audio', line=cue.line, **fields))
            spans[index] = (clock, voice)
            clock += voice.duration
        elif index in parsed.inside:
            segment, share, broke = parsed.inside[index]
            start, voice = spans[segment]
            out.append(_move(cue, start + within(voice, share, broke)))
        else:
            out.append(_move(cue, clock))
    out.sort(key=lambda cue: cue.at_ms)  # stable: only moves inside one segment may change places
    return SceneTimeline(scene_id=scene_id, title=title, duration_ms=_ms(clock), cues=out)


def starting(laid: SceneTimeline, line: int) -> SceneTimeline:
    """The timeline from the first voice written on `line`, its clock moved to start there; the body turned first as
    the turns before would have left it."""
    t0 = next(c.at_ms for c in laid.cues if c.kind == 'audio' and c.line >= line)
    turns = [c for c in laid.cues if c.kind == 'turn' and (c.at_ms < t0 or (c.at_ms == t0 and c.line < line))]
    kept = [c for c in laid.cues if c.at_ms > t0 or (c.at_ms == t0 and c.line >= line)]
    cues = [c.model_copy(update={'at_ms': c.at_ms - t0}) for c in [*turns[-1:], *kept]]
    return laid.model_copy(update={'cues': [c.model_copy(update={'at_ms': max(0, c.at_ms)}) for c in cues],
                                   'duration_ms': laid.duration_ms - t0})  # fmt: skip


def within(voice: Voice, share: float, broke: bool) -> float:
    """When a move written among a segment's words fires, in seconds from the segment's start: at its share of the
    text over the voiced span; right after a punctuation mark, as the voice resumes after the nearest pause (within
    SNAP_S: the speech model pauses there)."""
    at = voice.onset + share * (max(voice.offset, voice.onset) - voice.onset)
    if broke and voice.gaps:
        start, end = min(voice.gaps, key=lambda gap: max(gap[0] - at, at - gap[1], 0.0))
        if max(start - at, at - end) <= SNAP_S:
            return end
    return at


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def _move(cue: SceneCue, at: float) -> TimelineCue:
    kind = 'turn' if cue.kind == 'turn' else 'emotion' if cue.kind == 'emotion' else 'gesture'
    return TimelineCue(at_ms=_ms(at), kind=kind, line=cue.line, name=cue.name, degrees=cue.degrees)


def vocabulary() -> SceneVocabulary:
    """What the editor's « Insérer » menu offers, from the lists the parser reads: the moves by feeling, the voices
    by gender."""

    turns = ['À gauche (35°)', 'À droite (35°)', 'Face au public', 'De 60° (-60\xa0: à droite)', 'De 90°', 'De -90°']
    genders = {'female': 'Voix féminines', 'male': 'Voix masculines'}
    pauses = ('0,5', '1', '1,5', '2', '3', '5')
    words = {
        'emotions': [(name, move[0], family) for family, moves in MOVES.items() for name, move in moves.items()],
        'gestures': [(phrases[0], anim.GESTURES[key][0], '') for key, phrases in GESTURES.items()],
        'turns': [(w, lbl, '') for w, lbl in zip([*TURNS, 'tourne 60', 'tourne 90', 'tourne -90'], turns, strict=True)],
        'pauses': [(f'pause {s}', f'{s} s', '') for s in pauses],
        'sounds': [(word, word.capitalize(), '') for word in SOUNDS],
        'voices': [(v, f'{v} — {d}', genders[g]) for g in genders for v, (g2, d) in sorted(VOICES.items()) if g2 == g],
    }
    return SceneVocabulary.model_validate(
        {k: [{'word': w, 'label': lbl, 'family': f} for w, lbl, f in v] for k, v in words.items()}
    )
