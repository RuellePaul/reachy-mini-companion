"""Pollen's emotion library (recorded moves with their sounds), and its dances for scenes, loaded off the wake path."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Final

from robot_agent import animation_names, config
from robot_agent.recorded import Recorded

logger = logging.getLogger(__name__)

DATASET: Final = 'pollen-robotics/reachy-mini-emotions-library'
DANCES: Final = 'pollen-robotics/reachy-mini-dances-library'  # scenes only: loaded once the emotions are
CURATED: Final = tuple(animation_names.EMOTIONS)  # the names offered to the model


class Emotions:
    """Loads the library on its own thread, retrying with a backoff; everything else answers from memory.

    `load(dataset)` returns a library (`.list_moves()`, `.get(name)`): `adapters.robot.emotion_library` on the robot.
    """

    def __init__(self, load: Callable[[str], Any]) -> None:
        self._load = load
        self._library: Any = None
        self._names: list[str] = []
        self._scene_names: dict[str, Any] = {}  # every move a scene may play -> its library

    def start(self) -> None:
        threading.Thread(target=self._load_until_done, name='emotions', daemon=True).start()

    def names(self) -> list[str]:
        """The curated names the library has; empty until it is loaded. Never blocks, never loads."""
        return list(self._names)

    def get(self, name: str) -> Any | None:
        """The recorded move, or None (not loaded yet, or not offered to the model)."""
        return self._recorded(name) if name in self._names else None

    def move(self, name: str) -> Any | None:
        """Any move a scene may use (every emotion of the library, and the dances once loaded), or None."""
        library = self._scene_names.get(name)
        return None if library is None else self._recorded(name, library)

    def _recorded(self, name: str, library: Any = None) -> Any:
        """Played by our own interpolation (recorded.py), the one the dashboard's preview samples."""
        move = (library or self._library).get(name)
        data = getattr(move, 'move', None)
        return Recorded(data, getattr(move, 'sound_path', None)) if isinstance(data, dict) else move

    def _load_until_done(self) -> None:
        library, available = self._retrying(DATASET)
        missing = [name for name in CURATED if name not in available]
        if missing:
            logger.warning('the emotion library lacks %s', ', '.join(missing))
        self._library = library
        self._names = [name for name in CURATED if name in available]
        self._scene_names = dict.fromkeys(available, library)
        logger.info('emotion library loaded: %d of %d emotions offered', len(self._names), len(CURATED))
        dances, names = self._retrying(DANCES)  # a scene plays without its dances meanwhile
        self._scene_names = dict.fromkeys(names, dances) | self._scene_names

    def _retrying(self, dataset: str) -> tuple[Any, set[str]]:
        """A library and its moves, retried with a backoff until it loads."""
        delay, longest = config.EMOTIONS_RETRY_S
        while True:
            try:
                library = self._load(dataset)
                return library, set(library.list_moves())
            except Exception as exc:  # noqa: BLE001 — optional: no play_emotion, or no dance, until it loads
                logger.warning('%s unavailable (%s): retrying in %.0f s', dataset, exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, longest)
