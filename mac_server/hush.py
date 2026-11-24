"""The robot silent while an app people play among themselves is on screen (AppSummary.quiet), until the app is left;
an antenna held or a typed say (the robot's own) gives its speech back before."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mac_server.server import Mac

STEP_S = 0.5  # what a dashboard shows is read this often: the hush follows the screen within it


class Hush:
    def __init__(self, mac: Mac) -> None:
        self._mac = mac
        self._asked: tuple[str | None, bool] = (None, False)  # (session, silent) the robot was last told
        self._given_back: str | None = None  # the app the speech was given back for, until it leaves the screen

    def give_back(self) -> None:
        """The robot took its speech back itself (an antenna held, a typed say): until this app leaves the screen. Any
        thread: the next step acts on it."""
        self._given_back = self._mac.broker.showing('app')

    async def step(self) -> None:
        """Tell the robot when what it should be changes: an app shown or left, a new wake, the speech given back."""
        session = self._mac.health.session()
        if not session.awake or session.session_id is None:  # asleep: a wake is a new session, asked again
            self._asked = (None, False)
            return
        shown = self._mac.broker.showing('app')
        if shown != self._given_back:
            self._given_back = None
        wanted = (
            shown is not None and shown != self._given_back and await asyncio.to_thread(self._mac.apps.quiet, shown)
        )
        asked = (session.session_id, wanted)
        if asked != self._asked and (wanted or self._asked[1] or session.hushed):
            self._mac.commands.push('hush', hush=wanted)
        self._asked = asked

    async def run(self) -> None:
        while True:
            await self.step()
            await asyncio.sleep(STEP_S)
