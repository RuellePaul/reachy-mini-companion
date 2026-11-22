"""Notes: what the conversation is told that no human said (an app built by voice is ready). Each one is a text turn
sent at the next quiet point, so it never talks over anyone; like an image, it is not a human turn and authorises no
tool (the session's guard reads only transcriptions and typed says)."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

from robot_agent import config

logger = logging.getLogger(__name__)

WAIT_S = 60.0  # a note that finds no quiet point by then is dropped: the moment has passed (a scene: after it)
POLL_S = 0.2


class Notes:
    """Sends `text` through `send` once `quiet()` holds; while `busy()` (a scene plays) the wait does not count.
    On the event loop."""

    def __init__(
        self, send: Callable[[str], Awaitable[None]], quiet: Callable[[], bool], busy: Callable[[], bool]
    ) -> None:
        self._send, self._quiet, self._busy = send, quiet, busy
        self._tasks: set[asyncio.Task[None]] = set()
        self._sent_at = -math.inf

    def add(self, text: str) -> asyncio.Task[None]:
        """The note on its way: done once it is sent, or dropped."""
        task = asyncio.create_task(self._deliver(text), name='note')
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def recent(self, now: float) -> bool:
        """A note went out lately: the model's answer to it is no phantom turn."""
        return now - self._sent_at <= config.HUMAN_GRACE_S

    def after(self, moment: float) -> bool:
        """A note went out after `moment` (the last human turn): what the model does now answers the note."""
        return self._sent_at > moment

    def cancel(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    async def _deliver(self, text: str) -> None:
        deadline = time.monotonic() + WAIT_S
        while not self._quiet():
            if self._busy():
                deadline = max(deadline, time.monotonic() + WAIT_S)
            elif time.monotonic() >= deadline:
                logger.info('[NOTE] no quiet moment for « %s »: dropped', text)
                return
            await asyncio.sleep(POLL_S)
        logger.info('[NOTE] %s', text)
        self._sent_at = time.monotonic()
        await self._send(text)
