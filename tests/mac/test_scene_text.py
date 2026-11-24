"""The scene grammar (strict, French errors with their line), the cache keys, and the timeline, on the built-ins."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mac_server import scene_text
from mac_server.scenes import ASSETS, BUILTINS

CAST = '@NARRATEUR (Charon) : conteur\n@MARCO (Algenib) : bourru\n@LUCIE (Leda) : rieuse\n\n'


def parse(body: str, narrator: str = 'Achird') -> scene_text.Parsed:
    return scene_text.parse(CAST + body, narrator)


def errors(body: str) -> list[tuple[int, str]]:
    return [(e.line, e.message) for e in parse(body).errors]


def says(parsed: scene_text.Parsed) -> list[tuple[str | None, str | None]]:
    return [(c.character, c.text) for c in parsed.cues if c.kind == 'say']


def test_a_blank_line_ends_a_block_and_the_lines_under_a_role_stay_its_own() -> None:  # S1
    parsed = parse('MARCO : Bonjour,\ncomment vas-tu ?\n\nIl part.\nLoin.')
    assert says(parsed) == [('MARCO', 'Bonjour, comment vas-tu ?'), ('NARRATEUR', 'Il part. Loin.')]
    assert [c.line for c in parsed.cues] == [5, 8]
    assert parsed.errors == []


def test_the_cast_names_a_voice_and_how_the_role_plays() -> None:  # S2
    parsed = parse('LUCIE (en criant de joie) : Surprise ! <rire>')
    say = parsed.cues[0]
    assert (say.voice, say.style, say.text) == ('Leda', 'rieuse, en criant de joie', 'Surprise ! <rire>')
    assert [(m.name, m.voice) for m in parsed.cast] == [
        ('NARRATEUR', 'Charon'),
        ('MARCO', 'Algenib'),
        ('LUCIE', 'Leda'),
    ]
    assert errors('') == []
    assert scene_text.parse('@MARCO (Korre) : x\n', 'Achird').errors[0].message == (
        "«\xa0Korre\xa0» n'est pas une voix (veux-tu dire Kore\xa0?)"
    )
    assert scene_text.parse('@MARCO : bourru\n', 'Achird').errors[0].message.startswith('donne une voix à MARCO')
    assert [e.line for e in scene_text.parse('\n\n@MARCO (homme, grave) : x\n', 'Achird').errors] == [3]


def test_an_uncast_narrator_speaks_with_the_robots_voice() -> None:
    parsed = scene_text.parse('Il était une fois.', 'Kore')
    assert (parsed.cues[0].character, parsed.cues[0].voice, parsed.cues[0].style) == ('NARRATEUR', 'Kore', '')
    assert [(m.name, m.voice) for m in parsed.cast] == [('NARRATEUR', 'Kore')]


def test_a_capitalised_name_must_be_cast() -> None:  # S3
    assert errors('ATTENTION : ceci.') == [
        (
            5,
            "«\xa0ATTENTION\xa0» n'est pas dans la distribution\xa0: ajoute @ATTENTION (Voix)\xa0: …, ou écris «\xa0Attention\xa0:\xa0»",
        )
    ]
    assert errors('Il dit : bonjour.') == []  # lower case: narration
    assert says(parse('Marco : Salut.')) == [('MARCO', 'Salut.')]  # a cast name, any case


@pytest.mark.parametrize(
    ('bracket', 'kind', 'value'),
    [
        ('[sad1]', 'emotion', 'sad1'),
        ('[SERENITY1]', 'emotion', 'serenity1'),
        ('[hoche la tête]', 'gesture', 'nod'),
        ('[secoue la tête]', 'gesture', 'shake'),
        ('[penche la tête à gauche]', 'gesture', 'tilt_left'),
        ('[lève les yeux]', 'gesture', 'look_up'),
        ('[baisse les yeux]', 'gesture', 'look_down'),
        ('[fait une révérence]', 'gesture', 'bow'),
        ('[tourne à gauche]', 'turn', 35.0),
        ('[tourne à droite]', 'turn', -35.0),
        ('[face au public]', 'turn', 0.0),
        ('[tourne -45]', 'turn', -45.0),
        ('[tourne de 20 degrés]', 'turn', 20.0),
        ('[pause]', 'pause', 1.5),
        ('[pause 2]', 'pause', 2.0),
        ('[pause 1,5 s]', 'pause', 1.5),
        ('[silence 3 secondes]', 'pause', 3.0),
    ],
)
def test_brackets_are_exact_moves(bracket: str, kind: str, value: object) -> None:  # S4
    cue = next(c for c in parse(f'MARCO : Oui. {bracket} Non.').cues if c.kind != 'say')
    assert cue.kind == kind
    assert {'emotion': cue.name, 'gesture': cue.name, 'turn': cue.degrees, 'pause': cue.seconds}[kind] == value


def test_anything_else_in_brackets_is_an_error_with_a_suggestion() -> None:  # S4, S9
    assert errors('[triste] Il pleut.') == [
        (5, "[triste] n'est ni une émotion, ni un geste, ni une pause (veux-tu dire [sad1]\xa0?)")
    ]
    assert errors('[sad] Il pleut.') == [(5, '[sad]\xa0: écris le nom exact, ex. [sad1]')]
    assert errors('[colere] x')[0][1].endswith('(veux-tu dire [irritated1]\xa0?)')  # a feeling: its first move
    assert errors('[sad7] x')[0][1].endswith('(veux-tu dire [sad2]\xa0?)')
    assert errors('[accent italien] x')[0][1].endswith('(voir le menu Insérer)')
    assert errors('[emotion sad1] x')[0][1].startswith("[emotion sad1] n'est ni")
    assert errors('[toc-toc-toc] x')[0][1].startswith("[toc-toc-toc] n'est ni")  # not in the robot's library


def test_a_turn_beyond_the_limit_is_clamped_with_a_warning_not_an_error() -> None:  # S8
    parsed = parse('[tourne -160] x\n\n[pause 99]')
    assert [c.degrees for c in parsed.cues if c.kind == 'turn'] == [-100.0]
    assert [c.seconds for c in parsed.cues if c.kind == 'pause'] == [20.0]
    assert parsed.errors == []
    assert [(w.line, w.message) for w in parsed.warnings] == [
        (5, '[tourne -160]\xa0: limité à -100°'),
        (7, '[pause 99]\xa0: limité à 20s'),
    ]


def test_a_direction_goes_after_the_name_or_at_the_start_of_a_block() -> None:  # S5
    assert parse('(très doucement) Fin.').cues[0].style == 'conteur, très doucement'
    assert parse('MARCO (fâché) : (fort) Dehors !').cues[0].style == 'bourru, fâché, fort'
    assert errors('MARCO : Je suis (en riant) content.') == [
        (5, 'une indication de jeu se met au début de la réplique\xa0: MARCO (en riant)\xa0: …')
    ]


def test_sounds_must_be_known_and_become_the_models_tags() -> None:  # S6
    assert errors('LUCIE : Ha <bonjour> !') == [(5, "<bonjour> n'est pas un son (rire, soupir, sanglot…)")]
    assert scene_text.speech('Oh <éternue> là <petit rire> <laugh>') == 'Oh <sneeze> là <chuckle> <laugh>'


def test_unclosed_marks_and_empty_lines_are_errors() -> None:  # S7
    assert errors('Ciao [sad1 bella') == [(5, 'crochet mal fermé')]
    assert errors('Ciao <rire bella') == [(5, 'chevron mal fermé')]
    assert errors('Ciao (riant bella') == [(5, 'parenthèse mal fermée')]
    assert errors('MARCO :') == [(5, 'MARCO ne dit rien')]


def test_limits() -> None:  # S8
    assert errors('MARCO : ' + 'a' * 1300) == [(5, 'réplique trop longue (1300 caractères, 1200 au plus)')]
    many = '\n\n'.join(f'[sad1] x{i}' for i in range(201))
    assert errors(many)[-1][1] == 'plus de 400 répliques et mouvements\xa0: coupe la scène'


def test_dropped_v1_constructs_are_errors() -> None:  # S9
    assert errors('- Qui est là ?') == [(5, 'une réplique commence par le nom de qui parle\xa0: MARCO\xa0: …')]
    assert errors('@LA MOUETTE (Leda) = gouailleuse')[0][1] == "une distribution s'écrit @NOM (Voix)\xa0: jeu"


def test_a_move_inside_a_line_keeps_it_one_segment_that_a_pause_splits() -> None:
    parsed = parse('MARCO : [sad1] Un. [hoche la tête] Deux. [pause 1] [tourne à gauche] Trois. [sad1]')
    kinds = [(c.kind, c.text or c.name or c.degrees or c.seconds) for c in parsed.cues]
    assert kinds == [
        ('say', 'Un. Deux.'),
        ('emotion', 'sad1'),
        ('gesture', 'nod'),
        ('pause', 1.0),
        ('say', 'Trois.'),
        ('turn', 35.0),
        ('emotion', 'sad1'),
    ]
    # written before the words (share 0) and after « Un. » (3 of 9 characters, a punctuation mark before it);
    # the trailing [sad1] is not inside: it fires at the clock, when « Trois. » ends
    assert parsed.inside == {1: (0, 0.0, False), 2: (0, 3 / 9, True), 5: (4, 0.0, False)}


def test_moves_added_to_a_line_leave_its_voice_as_rendered() -> None:
    plain = parse('MARCO : Bonjour, toi. Comment vas-tu ?\ndit-il.').cues[0]
    moved = parse('MARCO : [sad1] Bonjour [hoche la tête], toi. [tourne 20] Comment vas-tu ?\n[oops1] dit-il. [no1]')
    assert [c.key for c in moved.cues if c.kind == 'say'] == [plain.key]
    assert (moved.cues[0].line, moved.cues[0].end_line) == (5, 6)
    for plain, moved in [('joues-tu?', 'joues-tu [sad1]?'), ('Bonjour … toi', 'Bonjour [sad1] … toi'),
                         ('Je pensais\n… que tu viens.', 'Je pensais\n[sad1] … que tu viens.')]:  # fmt: skip
        assert [c.key for c in parse(f'MARCO : {moved}').cues if c.kind == 'say'] == [
            c.key for c in parse(f'MARCO : {plain}').cues if c.kind == 'say'
        ]


def test_a_cast_angle_turns_the_robot_to_whoever_speaks() -> None:
    cast = '@MARCO (Algenib, -60) : bourru\n@LUCIE (Leda, +60°) : rieuse\n@NARRATEUR (0) : conteur\n\n'
    parsed = scene_text.parse(
        cast + 'MARCO : Un.\n\nMARCO : Deux.\n\nLUCIE : [sad1] Trois.\n\nIl part.\n\n[pause 1]', 'Kore'
    )
    assert [(m.name, m.voice, m.angle) for m in parsed.cast] == [
        ('MARCO', 'Algenib', -60.0),
        ('LUCIE', 'Leda', 60.0),
        ('NARRATEUR', 'Kore', 0.0),
    ]
    steps = [(c.kind, c.line, c.degrees if c.kind == 'turn' else c.text or c.name) for c in parsed.cues]
    assert steps == [
        ('say', 5, 'Un.'),
        ('turn', 5, -60.0),
        ('say', 7, 'Deux.'),
        ('turn', 7, -60.0),  # every block: the robot skips a turn to where it already faces (a rehearsal needs it)
        ('say', 9, 'Trois.'),
        ('turn', 9, 60.0),
        ('emotion', 9, 'sad1'),
        ('say', 11, 'Il part.'),
        ('turn', 11, 0.0),
        ('pause', 13, None),  # a block that says nothing turns nothing
    ]
    assert parsed.inside[1] == (0, 0.0, False)  # like [tourne -60] written before the words: at their onset
    unangled = scene_text.parse(
        cast.replace(', -60', '').replace(', +60°', '').replace(' (0)', '') + 'MARCO : Un.', 'Kore'
    )
    angled = scene_text.parse(cast + 'MARCO : Un.', 'Kore')
    assert [c.key for c in angled.cues if c.kind == 'say'] == [c.key for c in unangled.cues if c.kind == 'say']


def test_a_cast_angle_is_a_number_within_the_turn_limit() -> None:
    def problems(cast: str) -> list[tuple[int, str]]:
        parsed = scene_text.parse(cast + '\nMARCO : x', 'Kore')
        return [(e.line, e.message) for e in [*parsed.errors, *parsed.warnings]]

    assert problems('@MARCO (Algenib, gauche 6) : x') == [
        (1, "«\xa0gauche 6\xa0» n'est pas un angle\xa0: @MARCO (Algenib, -60)\xa0: …")
    ]
    assert problems('@MARCO (Algenib, -160) : x') == [(1, '[-160]\xa0: limité à -100°')]
    assert problems('@MARCO (homme, grave) : x')[0][1].startswith("«\xa0homme, grave\xa0» n'est pas une voix")


def test_the_cache_keys_never_change() -> None:
    """Lines rendered long ago (~/.cache/reachy/scene_audio): the same key means no paid render again."""
    sea = scene_text.parse((ASSETS / 'voir-la-mer.txt').read_text(encoding='utf-8'), 'Achird')
    party = scene_text.parse((ASSETS / 'fete-surprise.txt').read_text(encoding='utf-8'), 'Achird')
    keys = {c.key for c in [*sea.cues, *party.cues]}
    assert {
        'a041a1ce7f18b6a183580c03a63a4817b2c12544',  # the narrator's first line
        'bd50d8ebb1ced7af31e491f8af005b2eb12fd422',  # LA MOUETTE, with <rire>
        '0e6380407bcdf2bc73048df5e587f234e0fd0b92',  # LUCIE (en criant de joie), with <rire>
    } <= keys
    lucie = next(c for c in party.cues if c.character == 'LUCIE')
    keyed = 'gemini-3.8-flash-tts\nLeda\namie pétillante et rieuse, en criant de joie\nSurprise ! <laugh> Joyeux anniversaire, Reachy !'
    assert lucie.key == hashlib.sha1(keyed.encode()).hexdigest()


@pytest.mark.parametrize('scene_id', list(BUILTINS))
def test_the_built_ins_parse_and_lay_out(scene_id: str) -> None:
    text = (ASSETS / f'{scene_id}.txt').read_text(encoding='utf-8')
    parsed = scene_text.parse(text, 'Achird')
    assert parsed.errors == [] and parsed.warnings == []
    assert {c.voice for c in parsed.cues if c.kind == 'say'} <= {m.voice for m in parsed.cast}
    timeline = scene_text.timeline(scene_id, BUILTINS[scene_id], parsed, lambda key: scene_text.Voice(2.0, 0.3, 1.7))
    times = [c.at_ms for c in timeline.cues]
    spans = [(c.at_ms, c.at_ms + (c.duration_ms or 0)) for c in timeline.cues if c.kind == 'audio']
    for index, (segment, _, _) in parsed.inside.items():  # a move among a segment's words fires within its voice
        start, end = spans[[i for i, c in enumerate(parsed.cues) if c.kind == 'say'].index(segment)]
        move = parsed.cues[index]
        assert any(
            c.kind == move.kind and (c.name, c.degrees) == (move.name, move.degrees) and start <= c.at_ms <= end
            for c in timeline.cues
        )
    moves = [c for c in parsed.cues if c.kind in ('emotion', 'gesture', 'turn')]
    assert len([c for c in timeline.cues if c.kind != 'audio']) == len(moves)  # every move placed
    assert timeline.duration_ms >= max(times)
    lines = text.split('\n')
    for cue in timeline.cues:  # each cue points at the line it is written on
        assert cue.text is None or cue.text.split()[0] in lines[cue.line - 1]


def test_the_timeline_lays_segments_back_to_back_and_moves_inside_their_voice() -> None:
    parsed = parse(
        'MARCO : [sad1] Un, deux. [hoche la tête] Trois quatre [oops1] cinq.\n\n[pause 2]\n\n[tourne à gauche]\n\n'
        'LUCIE : Six. [sad1]'
    )
    voices = {  # MARCO pauses from 1.0 to 1.4 s, after « deux. »
        parsed.cues[0].key: scene_text.Voice(3.0, 0.25, 2.75, ((1.0, 1.4),)),
        parsed.cues[-2].key: scene_text.Voice(1.0, 0.25, 0.75),
    }
    timeline = scene_text.timeline('s', 'S', parsed, voices.__getitem__)
    placed = [(c.at_ms, c.kind, c.name or c.degrees or c.text) for c in timeline.cues]
    assert placed == [
        (0, 'audio', 'Un, deux. Trois quatre cinq.'),
        (250, 'emotion', 'sad1'),  # before the words: at their voiced onset
        (1400, 'gesture', 'nod'),  # after « deux. »: as the voice resumes after its pause
        (2214, 'emotion', 'oops1'),  # mid-phrase: at its share of the text (22/28) over the voiced span
        (5000, 'turn', 35.0),  # its own block: at the clock, after the pause
        (5000, 'audio', 'Six.'),
        (6000, 'emotion', 'sad1'),  # after the last words of its block: when they end
    ]
    assert timeline.duration_ms == 6000


def test_a_move_after_a_mark_stays_put_when_no_pause_is_near() -> None:
    voice = scene_text.Voice(10.0, 0.5, 9.5, ((1.0, 1.3), (8.0, 8.5)))
    assert scene_text.within(voice, 0.5, True) == 5.0  # 3.5 s from either pause
    assert scene_text.within(voice, 0.1, True) == 1.3
    assert scene_text.within(voice, 0.1, False) == pytest.approx(1.4)  # no mark: never snapped


def test_the_vocabulary_is_what_the_parser_reads() -> None:
    vocabulary = scene_text.vocabulary()
    for group, wrap in (('emotions', '[{}]'), ('gestures', '[{}]'), ('turns', '[{}]'), ('pauses', '[{}]')):
        for word in getattr(vocabulary, group):
            assert not parse(wrap.format(word.word) + ' x').errors, word
    for word in vocabulary.sounds:
        assert not parse(f'MARCO : Oh <{word.word}>').errors
    for word in vocabulary.voices:
        assert not scene_text.parse(f'@X ({word.word}) : y\n', 'Achird').errors
    assert len(vocabulary.emotions) == 100 and all(word.family for word in vocabulary.emotions)
    assert [w.family for w in vocabulary.voices[:1] + vocabulary.voices[-1:]] == ['Voix féminines', 'Voix masculines']
    assert Path(ASSETS).is_dir()


def test_a_play_from_a_line_starts_at_its_voice_turned_as_the_scene_left_it() -> None:
    parsed = scene_text.parse(
        '@MARCO (Algenib, -60) : x\n@LUCIE (Leda, 60) : y\n\nMARCO : [sad1] Un.\n\nLUCIE : Deux.\nsuite\n\n'
        'MARCO : Trois. [oops1]',
        'Kore',
    )
    laid = scene_text.timeline('s', 'S', parsed, lambda key: scene_text.Voice(2.0, 0.3, 1.7))
    from_lucie = scene_text.starting(laid, 6)
    assert [(c.at_ms, c.kind, c.line, c.name or c.degrees) for c in from_lucie.cues] == [
        (0, 'turn', 4, -60.0),  # where MARCO's turn left the body, at once
        (0, 'audio', 6, None),
        (300, 'turn', 6, 60.0),
        (2000, 'audio', 9, None),
        (2300, 'turn', 9, -60.0),
        (4000, 'emotion', 9, 'oops1'),
    ]
    assert from_lucie.duration_ms == laid.duration_ms - 2000
