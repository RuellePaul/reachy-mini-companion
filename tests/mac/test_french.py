"""The Mac's French keeps its words together on a screen: a no-break space inside « » and before : ; ! ?, so a narrow
dashboard never starts a line with « » », « : » or « ? ». Every string the Mac writes is read from its source, except
the docstrings and the prompts only a model reads (the robot's system instruction, Claude's, the page's probe)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from fastapi.testclient import TestClient

MAC = Path(__file__).resolve().parents[2] / 'mac_server'
FOR_A_MODEL = {
    'prompts.py': None,
    'app_build.py': {'PROMPT', 'RETOUCH'},
    'scene_emotions.py': {'_PROMPT'},
    'scene_write.py': {'_AGAIN'},
}
CODE = {'app_check.py': {'_END'}}  # JavaScript run in the page (its French, in _START, is checked)
BROKEN = re.compile(r'« | »| [:;!?](?=\s|$|[»"\')])')


def broken(name: str, source: str) -> list[str]:
    """Each string of a module that breaks a French word from its sign, with its line."""
    tree = ast.parse(source)
    skipped = (FOR_A_MODEL.get(name) or set()) | CODE.get(name, set())
    left_out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                left_out.add(id(first.value))
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in skipped for t in node.targets):
            left_out.update(id(inner) for inner in ast.walk(node.value))
    return [
        f'{name}:{node.lineno} {node.value.strip()[:80]!r}'
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in left_out
        if BROKEN.search(node.value)
    ]


def test_the_mac_keeps_its_french_words_with_their_signs() -> None:
    found = [
        line
        for path in sorted(MAC.rglob('*.py'))
        if not (path.name in FOR_A_MODEL and FOR_A_MODEL[path.name] is None)
        for line in broken(path.name, path.read_text(encoding='utf-8'))
    ]
    assert found == []


def test_a_plain_space_is_found_in_a_string_and_an_f_string_not_in_a_docstring_or_a_prompt() -> None:
    source = '''"""« Un docstring » : en anglais."""
A = "Reachy dort : réveille-le."
B = f"La scène « {title} » n'existe pas."
PROMPT = "Tu codes : va droit au but."
C = "Reachy dort\\xa0: réveille-le\\xa0?"
'''
    assert [line.split(' ')[0] for line in broken('app_build.py', source)] == [
        'app_build.py:2',
        'app_build.py:3',  # before the title
        'app_build.py:3',  # after it
    ]


def test_the_palette_shows_each_sentence_in_one_pair_of_guillemets(client: TestClient) -> None:
    """The palette writes « Dis « … » » around a sentence: one that quotes uses “ ”, French's inner quotes."""
    said = [entry['say'] for entry in client.get('/api/catalog').json()['entries'] if entry['say']]
    assert 'joue la scène “La fête surprise”' in said
    assert [say for say in said if '«' in say or '»' in say] == []
