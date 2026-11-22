"""The robot's French that the dashboard shows as it is (a tool's message in the transcript, a Mac failure, a scene's
message in Scènes) keeps its words with their signs, as the Mac's does (tests/mac/test_french.py). Left out: the
docstrings, the logs (in English) and what only the model reads: the tool declarations and the notes on a build."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.mac.test_french import BROKEN

ROBOT = Path(__file__).resolve().parents[2] / 'robot_agent'
# Called or defined: the tool declarations, the notes on a build (an app's, a scene's).
FOR_A_MODEL = {'_decl', '_text', '_number', 'built', 'told'}


def broken(name: str, source: str) -> list[str]:
    """Each string of a module shown on the dashboard that breaks a French word from its sign, with its line."""
    tree, left_out = ast.parse(source), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                left_out.add(id(first.value))
        callee = node.func if isinstance(node, ast.Call) else None
        logged = (
            isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name) and callee.value.id == 'logger'
        )
        for_a_model = isinstance(callee, ast.Name) and callee.id in FOR_A_MODEL
        if logged or for_a_model or (isinstance(node, ast.FunctionDef) and node.name in FOR_A_MODEL):
            left_out.update(id(inner) for inner in ast.walk(node))
    return [
        f'{name}:{node.lineno} {node.value.strip()[:80]!r}'
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in left_out
        if BROKEN.search(node.value)
    ]


def test_the_robot_keeps_the_french_it_shows_with_its_signs() -> None:
    found = [line for path in sorted(ROBOT.rglob('*.py')) for line in broken(path.name, path.read_text('utf-8'))]
    assert found == []


def test_a_message_is_checked_a_log_a_declaration_or_a_note_is_not() -> None:
    source = '''"""« Un docstring » : en anglais."""
A = ToolResult('error', f"L'outil « {name} » n'existe pas : rien n'a été fait.")
logger.info('[SCENE] « %s » ignored: waking up')
D = _decl('look', 'Prend une photo : pour « regarde ».')
def built(app):
    return f"L'app « {title} » est prête : annonce-le."
B = "Le Mac est injoignable\\xa0: rien n'a été fait."
'''
    assert [line.split(' ')[0] for line in broken('tools.py', source)] == ['tools.py:2'] * 2  # before the name, after
