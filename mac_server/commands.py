"""Mac -> robot commands, taken by the robot's long-poll. Lives on the event loop: no lock."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

from mac_server.contracts import (
    APP_MOTION_TTL_S,
    COMMAND_TTL_S,
    HUSH_TTL_S,
    MANETTE_TTL_S,
    SCENE_STOP_TTL_S,
    TRY_TTL_S,
    AppMotion,
    Command,
    SceneTimeline,
    TryMove,
)

log = logging.getLogger(__name__)

Action = Literal['wake', 'sleep', 'say', 'try', 'scene', 'scene_stop', 'app_motion', 'manette', 'hush']
STATE_ACTIONS = ('wake', 'sleep')  # a new one replaces any pending one: the latest wins
LATEST_WINS = (STATE_ACTIONS, ('try',), ('scene', 'scene_stop'), ('app_motion',), ('manette',), ('hush',))
TTL_S = {
    'try': TRY_TTL_S,
    'scene_stop': SCENE_STOP_TTL_S,
    'app_motion': APP_MOTION_TTL_S,
    'manette': MANETTE_TTL_S,
    'hush': HUSH_TTL_S,
}
QUIET = ('app_motion', 'manette')  # many a second: not logged one by one
RESTART_WINDOW_S = 60.0  # a restart's wake is queued only if the session ends within this


class CommandQueue:
    def __init__(
        self,
        ttl_s: float = COMMAND_TTL_S,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._wall = wall
        self._mono = mono
        self._pending: list[tuple[float, Command]] = []  # (monotonic time queued, command)
        self._changed = asyncio.Event()
        self._polls = 0  # polls begun: the newest one is the robot's live poll
        self._restart_at: float | None = None  # monotonic time of a restart waiting for the session end

    def push(
        self,
        action: Action,
        text: str | None = None,
        *,
        move: TryMove | None = None,
        scene: SceneTimeline | None = None,
        motion: AppMotion | None = None,
        manette: str | None = None,
        grant: bool = False,
        hush: bool | None = None,
    ) -> Command:
        group = next((g for g in LATEST_WINS if action in g), ())
        if action == 'manette' and not grant:  # a renewal never takes back the human's yes the robot has not taken
            grant = any(c.action == 'manette' and c.manette == manette and c.grant for _, c in self._pending)
        self._pending = [p for p in self._pending if p[1].action not in group]
        if action in STATE_ACTIONS:
            self._restart_at = None
        ttl_s = min(self._ttl_s, TTL_S.get(action, self._ttl_s))
        command = Command.model_validate(
            {
                'id': uuid.uuid4().hex[:12],
                'action': action,
                'text': text,
                'move': move,
                'scene': scene,
                'motion': motion,
                'manette': manette,
                'grant': grant,
                'hush': hush,
                'queued_at': self._wall(),
                'ttl_s': ttl_s,
            }
        )
        self._pending.append((self._mono(), command))
        self._changed.set()
        (log.debug if action in QUIET else log.info)('command queued: %s', action)
        return command

    def restart(self, awake: bool) -> None:
        """Sleep then wake: the wake waits for the robot to report its session over."""
        if not awake:
            self.push('wake')
            return
        self.push('sleep')
        self._restart_at = self._mono()

    def session_ended(self) -> None:
        """The robot reported `session_end`, or a heartbeat said it is asleep."""
        started, self._restart_at = self._restart_at, None
        if started is not None and self._mono() - started <= RESTART_WINDOW_S:
            self.push('wake')

    async def next(self, wait_s: float, gone: Callable[[], Awaitable[bool]]) -> Command | None:
        """The oldest live command, waiting up to `wait_s`; nothing is taken once `gone()` says so, nor once a newer
        poll came: one robot, so an older poll still parked sits on a socket gone silent (no FIN: `gone()` stays
        False) and leaves at once, empty."""
        self._polls += 1
        mine = self._polls
        self._changed.set()  # an older poll still parked wakes, and leaves
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while mine == self._polls:
            self._changed.clear()
            self._drop_stale()
            if self._pending:
                if await gone():
                    return None
                if self._pending and mine == self._polls:  # after the await: a newer poll may have taken it
                    return self._pending.pop(0)[1]
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(remaining):
                    await self._changed.wait()
        return None

    def _drop_stale(self) -> None:
        # The Mac's monotonic clock stops while it sleeps, its wall clock can step: the older age wins.
        wall, mono = self._wall(), self._mono()
        self._pending = [(at, c) for at, c in self._pending if max(wall - c.queued_at, mono - at) <= c.ttl_s]
