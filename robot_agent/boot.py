"""What runs before anything else is imported: the .env next to `robot_agent/`, and the SDK floor."""

from __future__ import annotations

import importlib.metadata
import os
import re
from pathlib import Path
from typing import Final

ENV_FILE: Final = Path(__file__).resolve().parents[1] / '.env'
# Below 2.24, receive() ends a Live turn early and quietly.
MIN_GENAI: Final = (2, 24)
# A major CI never ran (uv.lock): 3.0 opens the Live session on API v1, not v1beta.
NEXT_GENAI: Final = (3, 0)
# A quoted value: up to its closing quote, an escaped one aside; what follows (a comment) is not part of it.
QUOTED: Final = re.compile(r"""(["'])((?:\\.|(?!\1).)*)\1""")


def load_env(path: Path = ENV_FILE) -> None:
    """KEY=VALUE lines into the environment, read without a dependency; what the environment holds wins."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        key, sep, value = line.strip().removeprefix('export ').partition('=')
        value = value.strip()
        quoted = QUOTED.match(value)
        value = quoted[2] if quoted else value.split(' #', 1)[0].strip()  # unquoted: nor is a trailing comment
        if sep and key.strip() and not key.startswith('#'):
            os.environ.setdefault(key.strip(), value)


def require_genai(version: str | None = None) -> None:
    """Refuse to start on a google-genai too old for Gemini 3.8 Live, of a major CI never ran, or missing, before
    importing it."""
    try:
        version = version or importlib.metadata.version('google-genai')
    except importlib.metadata.PackageNotFoundError:
        raise SystemExit('google-genai is not installed: run `make robot-deps` from the Mac.')
    found = tuple(int(n) for n in re.findall(r'\d+', version)[:2])
    if found < MIN_GENAI:
        raise SystemExit(f'google-genai {version} is too old (need >= 2.24): run `make robot-deps` from the Mac.')
    if found >= NEXT_GENAI:
        raise SystemExit(
            f'google-genai {version} is a major CI never ran (need < 3): run `make robot-deps` from the Mac.'
        )
