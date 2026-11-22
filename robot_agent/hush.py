"""Silent while an app people play among themselves is on screen (the Mac's `hush` command). Once its answer is
over, the robot hears nobody and says nothing, as during a scene (the session's `performing`: comfort noise for the
microphone, its audio and every tool refused, notes held, no idle sleep), until its speech is given back: by the Mac
(the app left, « Lui rendre la parole »), an antenna held, or a typed say."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class Hush:
    def __init__(self, report: Callable[[dict[str, Any]], None], ready: Callable[[], bool]) -> None:
        self._report = report  # contracts.HushEvent, the session id added
        self._ready = ready  # the robot's answer is over: what it is saying is not cut
        self._wanted = False
        self.on = False

    def want(self, on: bool) -> None:
        """The Mac's word: silent, or speaking again."""
        if on:
            self._wanted = True
        else:
            self.off('mac')

    def active(self) -> bool:
        """Whether the robot keeps silent now: asked, from the first moment its answer is over."""
        if self._wanted and not self.on and self._ready():
            self.on = True
            logger.info('[HUSH] silent while the app is played: an antenna held or a typed say gives the speech back')
            self._report({'kind': 'hush', 'on': True})
        return self.on

    def off(self, why: str) -> None:
        """Speaking again: the Mac asked it (`mac`), an antenna was held, a say was typed."""
        self._wanted = False
        if self.on:
            self.on = False
            logger.info('[HUSH] speaking again (%s)', why)
            self._report({'kind': 'hush', 'on': False, 'why': why})

    def reset(self) -> None:
        """The conversation is over: a wake starts speaking, and the Mac asks again while a quiet app is shown."""
        self._wanted = self.on = False
