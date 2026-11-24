"""Size budgets, enforced in CI: lines per area, lines per subject, and the share of comment lines in Python.

A comment line is a line holding only a comment, or a line of a docstring; blank lines count for nothing. A
subject is a feature built as isolated modules across the areas: its line counts within its area's too.
Vendored files (generated code, binary assets) are excluded by name and counted nowhere.
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AREAS = {
    'robot_agent': (5737, {'.py'}),
    'mac_server': (6574, {'.py'}),
    'frontend/src': (9956, {'.ts', '.tsx', '.js', '.jsx', '.mjs', '.css', '.json', '.html'}),
}
# Subjects: (limit, the paths that are the subject's own: files, or directories taken whole).
SUBJECTS: dict[str, tuple[int, tuple[str, ...]]] = {
    'palette': (626, ('mac_server/catalog.py', 'frontend/src/palette')),
    'studio': (1595, ('robot_agent/studio.py', 'mac_server/studio.py', 'frontend/src/studio')),
    'scenes': (
        4225,
        (
            'robot_agent/scene.py',
            'mac_server/scenes.py',
            'mac_server/scene_text.py',
            'mac_server/scene_voice.py',
            'mac_server/scene_emotions.py',
            'mac_server/scene_write.py',
            'frontend/src/scenes',
        ),
    ),
    'apps': (
        3085,
        (
            'robot_agent/apps.py',
            'mac_server/apps.py',
            'mac_server/app_build.py',
            'mac_server/app_check.py',
            'frontend/src/apps',
        ),
    ),
    'spotify': (495, ('mac_server/spotify.py',)),
    'regard': (417, ('robot_agent/gaze.py',)),
    'manette': (
        1169,
        (
            'robot_agent/manette.py',
            'robot_agent/ear.py',
            'robot_agent/take.py',
            'robot_agent/poses.py',
            'frontend/src/apps/Manette.tsx',
            'frontend/src/apps/hold.ts',
            'frontend/src/apps/pad.ts',
        ),
    ),
}
VENDORED: frozenset[str] = frozenset()
PYTHON = ('robot_agent', 'mac_server', 'scripts', 'tests')
MAX_COMMENT_SHARE = 0.25


def sources(area: str, suffixes: set[str]) -> list[Path]:
    """An area's source files, judged on their path inside the repo: where the checkout lives does not matter."""
    skip = {'__pycache__', 'node_modules'}
    found = (p for p in (ROOT / area).rglob('*') if p.is_file() and not skip & set(p.relative_to(ROOT).parts))
    return sorted(p for p in found if p.suffix in suffixes and p.relative_to(ROOT).as_posix() not in VENDORED)


def lines(path: Path) -> int:
    return len(path.read_text(encoding='utf-8').splitlines())


def comment_lines(source: str) -> tuple[int, int]:
    """(comment or docstring lines, non-blank lines) of one Python file."""
    lines = source.splitlines()
    non_blank = {n for n, line in enumerate(lines, 1) if line.strip()}
    comments: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT and not lines[tok.start[0] - 1][: tok.start[1]].strip():
            comments.add(tok.start[0])
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                comments.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return len(comments & non_blank), len(non_blank)


def main() -> int:
    failed = False
    counted: dict[Path, int] = {}
    for area, (limit, suffixes) in AREAS.items():
        files = sources(area, suffixes)
        counted.update((p, lines(p)) for p in files)
        total = sum(counted[p] for p in files)
        ok = bool(files) and total <= limit  # a missing or emptied area fails rather than passes
        failed |= not ok
        print(f'{"ok  " if ok else "FAIL"}  {area:<14} {total:>6} / {limit} lines')
    for subject, (limit, paths) in SUBJECTS.items():
        roots = [ROOT / path for path in paths]
        mine = [p for p in counted if any(p == root or root in p.parents for root in roots)]
        total = sum(counted[p] for p in mine)
        ok = bool(mine) and total <= limit
        failed |= not ok
        print(f'{"ok  " if ok else "FAIL"}  {subject:<14} {total:>6} / {limit} lines')
    commented = non_blank = 0
    for area in PYTHON:
        for path in sources(area, {'.py'}):
            c, n = comment_lines(path.read_text(encoding='utf-8'))
            commented, non_blank = commented + c, non_blank + n
    share = commented / non_blank if non_blank else 0.0
    ok = share <= MAX_COMMENT_SHARE
    failed |= not ok
    print(
        f'{"ok  " if ok else "FAIL"}  {"comments":<14} {share:>6.1%} / {MAX_COMMENT_SHARE:.0%} of non-blank Python lines'
    )
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
