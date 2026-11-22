"""The daemon's sounds, over its REST API: a built-in file name or an absolute path. Blocking HTTP: from async
code, go through a thread."""

from __future__ import annotations

import logging

import requests

from robot_agent import config

logger = logging.getLogger(__name__)
_HTTP = requests.Session()


def play(filename: str) -> None:
    """Best effort: a sound that does not play is not worth an error."""
    _post('play_sound', {'file': filename})


def stop() -> None:
    """Cut the sound playing, if any (best effort)."""
    _post('stop_sound', None)


def _post(action: str, body: dict[str, str] | None) -> None:
    try:
        _HTTP.post(f'{config.DAEMON_URL}/api/media/{action}', json=body, timeout=3.0)
    except requests.RequestException as exc:
        logger.debug('%s %s: %s', action, body or '', exc)
