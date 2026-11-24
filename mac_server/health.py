"""The robot as the Mac sees it: daemon probe + heartbeat -> status, heartbeat + events -> session."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import requests
from pydantic import BaseModel

from mac_server import pricing
from mac_server.contracts import (
    Heartbeat,
    HushEvent,
    Latency,
    ManetteEvent,
    Phase,
    PhaseChange,
    RobotEvent,
    RobotStatus,
    RobotStatusOut,
    SceneEvent,
    SessionEnd,
    SessionSnapshot,
    SessionStart,
    Usage,
    WsRobotStatus,
    WsSession,
)

log = logging.getLogger(__name__)

HEARTBEAT_STALE_S = 10.0  # the robot beats every 3 s
PROBE_EVERY_S = 2.0
PROBE_TIMEOUT_S = 1.0
REFRESH_EVERY_S = 1.0  # staleness is time passing: re-derive the state even when nothing arrives
DAEMON_TIMEOUT_S = 2.0


def probe(host: str, port: int) -> bool:
    """Whether the robot's daemon accepts a TCP connection (blocking: call it in a thread)."""
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _routable(address: str) -> bool:
    """An IP another machine can reach the robot at: not a test client's name, not loopback."""
    try:
        return not ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def push_volume(host: str, port: int, volume: int) -> bool:
    """Whether the daemon took the volume; the stored one is the truth, pushed again until it does."""
    try:
        response = requests.post(
            f'http://{host}:{port}/api/volume/set', json={'volume': volume}, timeout=DAEMON_TIMEOUT_S
        )
    except requests.RequestException as exc:
        log.info('volume not pushed to the daemon: %s', exc)
        return False
    if not response.ok:
        log.info('the daemon refused the volume: HTTP %s', response.status_code)
    return response.ok


@dataclass
class _Tally:
    """What one session's events said."""

    session_id: str
    phase: Phase | None = None
    cost_usd: float = 0.0
    latency_ms: int | None = None
    ended: bool = False  # session_end received: a typed turn would be lost
    manette: str | None = None  # the profile an app holds the body with
    hushed: bool = False  # silent while an app is played


class Health:
    def __init__(
        self,
        host: str,
        port: int,
        publish: Callable[[BaseModel], None],
        volume: Callable[[], int],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._host, self._port = host, port  # REACHY_HOST, until a heartbeat says where the robot is
        self._publish = publish
        self._volume = volume
        self._clock = clock
        self.daemon_ok = False
        self._push_lock = threading.Lock()  # one push at a time, of the volume stored when it starts: the last wins
        self._landed: int | None = None  # the volume the daemon last took
        self._said = Heartbeat(awake=False)  # the robot's latest word: a heartbeat, a start, an end or a scene event
        self._beat_at: float | None = None  # monotonic: the Mac's own sleep is not robot silence
        self._tally: _Tally | None = None
        self._sent_status: RobotStatus = self.status()
        self._sent_session = self.session()

    def _fresh(self) -> bool:
        return self._beat_at is not None and self._clock() - self._beat_at <= HEARTBEAT_STALE_S

    def status(self) -> RobotStatus:
        if self._fresh():
            return 'live'
        return 'idle' if self.daemon_ok else 'off'

    def _current(self) -> _Tally | None:
        """The tally of the session the robot is in, None when it is in none; the heartbeat decides staleness."""
        if not (self._fresh() and self._said.awake):
            return None
        sid = self._said.session_id
        return self._tally if self._tally and sid and self._tally.session_id == sid else _Tally('')

    def session(self) -> SessionSnapshot:
        tally, fresh = self._current(), self._fresh()
        scene = self._said.scene if fresh else None  # asleep too: a dashboard that missed its events reads it
        if tally is None:  # asleep, or not there: then nothing is said of its antennas
            return SessionSnapshot(awake=False, antenna_wake=self._said.antenna_wake if fresh else True, scene=scene)
        return SessionSnapshot(
            awake=True,
            session_id=self._said.session_id,
            phase=tally.phase,
            cost_usd=round(tally.cost_usd, 6),
            last_latency_ms=tally.latency_ms,
            manette=tally.manette,
            scene=scene,
            hushed=tally.hushed,
        )

    def takes_say(self) -> bool:
        """Whether a typed turn would reach the conversation: not once the robot reported it closing or over."""
        tally = self._current()
        return tally is not None and tally.phase != 'closing' and not tally.ended

    @property
    def host(self) -> str:
        """Where the robot's daemon is: the address its last heartbeat came from, else REACHY_HOST."""
        return self._host

    def beat(self, heartbeat: Heartbeat, origin: str | None = None) -> None:
        """The robot's word; `origin`, the address it came from, follows the robot when its lease changes."""
        if origin and origin != self._host and _routable(origin):
            log.info('robot seen at %s (was %s): its daemon is looked for there', origin, self._host)
            self._host = origin
        self._said, self._beat_at = heartbeat, self._clock()
        self.publish_changes()

    def record(self, event: RobotEvent) -> RobotEvent:
        """Tally one event of the robot's; a `usage` comes back with its costs filled in."""
        if self._tally is None or self._tally.session_id != event.session_id:
            self._tally = _Tally(event.session_id)
        tally = self._tally
        if isinstance(event, SessionStart | SessionEnd):  # news the next heartbeat would bring up to 3 s later
            started = isinstance(event, SessionStart)
            # a wake disarms the antennas; after an end, the sleep move keeps them deaf until a beat says they listen.
            # No scene: a wake stops one played asleep, the sleep the one played in the conversation.
            self._said = Heartbeat(awake=started, session_id=event.session_id if started else None, antenna_wake=False)
            tally.ended = not started
        elif isinstance(event, SceneEvent):  # news the next heartbeat would bring, and puts right if this one was lost
            on = event.state in ('started', 'line', 'waiting')
            self._said = self._said.model_copy(update={'scene': event.scene_id if on else None})
        elif isinstance(event, PhaseChange):
            tally.phase = event.phase
        elif isinstance(event, Latency):
            tally.latency_ms = event.ms
        elif isinstance(event, ManetteEvent) and event.state in ('on', 'off'):  # returning, refused: the hold goes on
            tally.manette = event.profile if event.state == 'on' else None
        elif isinstance(event, HushEvent):
            tally.hushed = event.on
        elif isinstance(event, Usage):
            cost = pricing.cost_usd(event)
            tally.cost_usd += cost
            event = event.model_copy(update={'cost_usd': round(cost, 6), 'session_cost_usd': round(tally.cost_usd, 6)})
        return event

    def publish_changes(self) -> None:
        status, session = self.status(), self.session()
        if status != self._sent_status:
            log.info('robot status: %s -> %s', self._sent_status, status)
            self._sent_status = status
            self._publish(WsRobotStatus(channel='robot_status', data=RobotStatusOut(status=status)))
        if session != self._sent_session:
            self._sent_session = session
            self._publish(WsSession(channel='session', data=session))

    async def probe_once(self) -> None:
        ok = await asyncio.to_thread(probe, self._host, self._port)
        came_back, self.daemon_ok = ok and not self.daemon_ok, ok
        self.publish_changes()
        if came_back:
            log.info('daemon reachable at %s:%s, pushing the stored volume', self._host, self._port)
        if ok:
            await asyncio.to_thread(self.apply_volume, came_back)  # back: it may have restarted on its own volume

    def apply_volume(self, force: bool = False) -> bool:
        """Push the stored volume, unless the daemon took it already and not `force`; whether the daemon has it now.
        Blocking: call it in a thread."""
        with self._push_lock:
            volume = self._volume()
            if force or self._landed != volume:
                self._landed = volume if push_volume(self._host, self._port, volume) else None
            return self._landed == volume

    async def _refresh(self) -> None:
        self.publish_changes()

    async def run(self) -> None:
        """Probe the daemon every 2 s and re-derive the state every second, until cancelled."""
        async with asyncio.TaskGroup() as group:
            group.create_task(_every(PROBE_EVERY_S, self.probe_once))
            group.create_task(_every(REFRESH_EVERY_S, self._refresh))


async def _every(period_s: float, step: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            await step()
        except Exception:
            log.exception('health check failed')  # a background loop outlives any one failure
        await asyncio.sleep(period_s)
