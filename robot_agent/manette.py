"""The robot as an app's controller (« mode manette »): its antennas as buttons, its base as a wheel (under the palm,
the antennas lowered as in sleep under that hand's fingers), its head as a joystick, its ear as a level. The robot
holds still and writes nothing, frees only the motors the profile needs, measures the body a hand moves and sends
each measure to the Mac as a datagram (poses.py: the dashboard's pads), and takes its motors back however it ends:
the app letting go, its lease running out (the dashboard closed), a scene, sleep, shutdown. « voix » lends the ear
(ear.Ear) with a body, or alone: then nothing is held, the robot goes on moving. Everything here runs on the event
loop; the SDK's blocking calls go through a thread."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from robot_agent import animations, apps, config, poses, take
from robot_agent.motion import SLEEP_ANTENNAS, SLEEP_HEAD, rot_z

logger = logging.getLogger(__name__)

HEAD = tuple(f'stewart_{n}' for n in range(1, 7))
# body -> the motors it frees (the SDK's names). Torque kept, an antenna pushed by hand deflects 5-10°: a button.
# The base carries no weight: freed, it turns by hand; « paume » frees it too, for a hand laid on the head whose
# fingers rest on the antennas. The head is freed from the sleep pose, where it sags 15 mm (upright it would drop
# 41 mm), with the base under it.
BODIES: dict[str, tuple[str, ...]] = {
    'gachettes': (),
    'volant': ('body_rotation',),
    'paume': ('body_rotation',),
    'tete': ('body_rotation', *HEAD),
}
EAR = 'voix'
PROFILES = (*BODIES, EAR, *(f'{body}+{EAR}' for body in BODIES))  # contracts.ManetteProfile
LEASE_S = 10.0  # past the last renewal (one a second): the link to the Mac stalls for seconds
SETTLE_S = 0.3  # past the blend into the still pose and the base's turn to the front, before writing stops
HOLD_S = 6.0  # the still pose's own length: past any blend and turn
TAKEN_BACK_S = 0.2  # after the torque returns, before the pose is read again
TAKE_BACK_ATTEMPTS = 3
WATCH_S = 0.25
# Before a motor is freed, the robot must stand where it was told: the base to the front, the head at its pose. The
# measured head sits ~8 mm and ~2.5° off its target at rest; upright and lowered are 53 mm apart.
POSED_MM, POSED_DEG = 15.0, 10.0
# Before the torque comes back: the base and the head within what the SDK's IK takes without a jump (its safe IK
# clamps the base at 160° and the head at 65° off it; past 180° of world yaw it swings the long way), and still for a
# moment (a hand let go). Until then the robot waits, freed, and says so; past RETURN_WAIT_S it takes them back anyway.
BASE_MAX_DEG, HEAD_YAW_MAX_DEG, WORLD_MAX_DEG = 150.0, 60.0, 170.0
STILL_DEG, STILL_S, RETURN_WAIT_S, RETURN_POLL_S = 1.0, 0.6, 30.0, 0.1
BACK_SPEED = 0.5  # the base back to the front at half its usual pace
ASK_AFTER_S = 1.0  # still held by a hand this long: the dashboard says to let go


def body_of(profile: str | None) -> str | None:
    """The body a profile lends ('tete+voix': 'tete'); None for « voix » alone, and for no profile."""
    body = (profile or '').partition('+')[0]
    return body if body in BODIES else None


def still(body: str) -> tuple[np.ndarray, np.ndarray, float | None]:
    """The pose a body is lent from: its head, its antennas, and the blend into it (None: the usual one). « tete »
    lowers the head and « paume » the antennas, where they lie in sleep, each at the sleep move's pace."""
    lowered = {'tete': (SLEEP_HEAD, animations.NEUTRAL), 'paume': (animations.pose(), SLEEP_ANTENNAS)}
    head, antennas = lowered.get(body, (animations.pose(), animations.NEUTRAL))
    return head, antennas, config.SLEEP_S if body in lowered else None


def head_yaw(head: np.ndarray, body: float) -> float:
    """The head's yaw relative to the base, in degrees."""
    local = rot_z(-body) @ head
    return math.degrees(math.atan2(local[1, 0], local[0, 0]))


def at_pose(current: Any, head: np.ndarray) -> bool:
    """Whether the robot stands where it was told: the base to the front, the head at `head` (its body frame)."""
    measured, _, body = current
    local = rot_z(-body) @ measured
    moved = 1000 * float(np.linalg.norm(local[:3, 3] - head[:3, 3]))
    cosine = (np.trace(local[:3, :3] @ head[:3, :3].T) - 1) / 2
    turned = math.degrees(math.acos(max(-1.0, min(1.0, float(cosine)))))
    return abs(math.degrees(body)) <= POSED_DEG and moved <= POSED_MM and turned <= POSED_DEG


def returnable(current: Any) -> bool:
    """Whether the torque can come back here without the IK jumping (BASE_MAX_DEG, HEAD_YAW_MAX_DEG, WORLD_MAX_DEG)."""
    head, _, body = current
    yaw, base = head_yaw(head, body), math.degrees(body)
    return abs(base) <= BASE_MAX_DEG and abs(yaw) <= HEAD_YAW_MAX_DEG and abs(base + yaw) <= WORLD_MAX_DEG


class Manette:
    """At most one profile at a time; `report` gets the robot events (contracts.ManetteEvent fields); `refusal()`
    says why an app may not take the body now ('scene' while one plays, 'sleep' while the conversation ends), or None;
    `ear(on)` lends the ear from a listening profile's on to its end; `read_frame` reads the pose with its seven
    joints (adapters.robot.read_frame) and `pose(at, joints, head, antennas)` hands a measure of the lent body on
    (mac.Mac.pose), poses.HZ times a second."""

    def __init__(
        self,
        mini: Any,
        motion: Any,
        report: Callable[[dict[str, Any]], None],
        read_pose: Callable[[Any], Any],
        refusal: Callable[[], str | None] = lambda: None,
        ear: Callable[[bool], None] = lambda on: None,
        read_frame: Callable[[Any], Any] | None = None,
        pose: Callable[[float, Any, Any, Any], None] = lambda at, joints, head, antennas: None,
    ) -> None:
        self._mini, self._motion, self._report, self._read_pose = mini, motion, report, read_pose
        self._refusal, self._ear, self._read_frame, self._pose = refusal, ear, read_frame or self._whole, pose
        self.profile: str | None = None
        self._held = False  # the profile's motors freed and its on said: only then is it a hold in force
        self._until = 0.0
        self._lock = asyncio.Lock()  # one entry or exit at a time
        self._leaves, self._left_why = 0, 'app'  # the let-goes asked so far, the last one's why
        self._watch: asyncio.Task[None] | None = None
        self.tape = take.Tape()  # what the hand makes the body do, kept after the hold for a `replay`
        self._taping: asyncio.Task[None] | None = None
        self._measure: tuple[float, Any, Any, Any] | None = None  # the lent body's latest: at, joints, head, antennas
        self._sending: asyncio.Task[None] | None = None

    @property
    def on(self) -> bool:
        return self.profile is not None

    @property
    def lent(self) -> bool:
        """The body is the app's: every other move is dropped. Not so for « voix » alone."""
        return body_of(self.profile) is not None

    async def command(self, profile: str | None, grant: bool = False) -> None:
        """The dashboard's word: a profile granted by the human (`grant`), renewed every second (the lease), or None
        (let go). A renewal only extends a hold in force: one sent before the robot let go never takes it again, nor
        keeps an entry that failed. A word for the hold in force is answered on again: an on lost, or a hold the
        dashboard forgot, is put right. One for a hold that ended is answered off, unless a let-go was asked after it:
        the end it would repeat was said already, by that let-go or before it."""
        if profile is None:
            await self.leave('app')
            return
        if profile not in PROFILES:
            return
        asked = self._leaves  # a let-go asked after this word withdraws it until a motor is freed
        async with self._lock:
            if profile == self.profile and self._held:
                self._until = time.monotonic() + LEASE_S
                self._report({'kind': 'manette', 'state': 'on', 'profile': profile})
                return
            if not grant:  # the hold ended here: the dashboard learns it
                if self.profile is None and self._leaves == asked:
                    self._report({'kind': 'manette', 'state': 'off', 'profile': None, 'why': 'lease'})
                return
            if why := self._refusal():
                self._report({'kind': 'manette', 'state': 'refused', 'profile': None, 'why': why})
                return
            if self.profile is not None:
                await self._release('profile')
            self._until = time.monotonic() + LEASE_S
            await self._enter(profile, asked)

    async def leave(self, why: str) -> None:
        """Give the body back, if held: always before a sleep move, a scene or a shutdown. `why`: app, lease, scene,
        sleep, pose (contracts.ManetteWhy). Raises while a motor stays free (still held, still frozen). An entry asked
        before it stops before it frees anything."""
        self._leaves, self._left_why = self._leaves + 1, why
        async with self._lock:
            if self.profile is not None:
                await self._release(why)

    def take_back_now(self) -> None:
        """At the agent's very end, from any thread: the torque back on, every target pinned where the motors are."""
        if self.lent or self._motion.frozen:
            logger.warning('[MANETTE] shutting down while held: the torque comes back')
            self._mini.enable_motors()

    # ----- entering

    async def _enter(self, profile: str, asked: int) -> None:
        """A body is lent from the start: a move asked while the robot takes its still pose is dropped, or it would
        replace that pose and be frozen half done (or free an upright head). Its lease is watched from the start too: an
        entry whose exit fails is given back once nobody renews."""
        self.profile = profile
        if self.lent:
            self._motion.lend(True)
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._lease(), name='manette-lease')
        try:
            taken = await self._take(profile, asked)
        except Exception:
            logger.exception('[MANETTE] %s could not start: the body is taken back', profile)
            await self._release('pose')
            return
        if not taken:
            logger.info('[MANETTE] %s let go before anything was freed', profile)
            await self._release(self._left_why)

    async def _take(self, profile: str, asked: int) -> bool:
        """The profile's body held still and freed, if it lends one, then its ear; False, nothing freed, once a let-go
        came after `asked`."""
        body = body_of(profile)
        if self._leaves != asked or (body is not None and not await self._free(body, asked)):
            return False
        self._until = max(self._until, time.monotonic() + LEASE_S)  # the lease starts once the robot is in place
        logger.info('[MANETTE] on: %s', profile)
        self._held = True
        self._ear(profile.endswith(EAR))
        self._report({'kind': 'manette', 'state': 'on', 'profile': profile})
        return True

    async def _free(self, body: str, asked: int) -> bool:
        """The still pose, then the body's motors freed; False, nothing freed, once a let-go came after `asked`."""
        head, antennas, blend = still(body)
        for attempt in (1, 2):
            if self._leaves != asked:
                return False
            self._motion.turn_body(0.0, own=True)
            self._motion.play_move(apps.Hold(head, antennas, duration=HOLD_S), own=True, blend_s=blend)
            await asyncio.sleep(max(0.0, self._motion.posed_at() - time.monotonic()) + SETTLE_S)
            current = await asyncio.to_thread(self._read_pose, self._mini)
            if current is not None and at_pose(current, head):
                break
            logger.warning('[MANETTE] the still pose is not reached (try %d)', attempt)
        else:
            raise RuntimeError('the still pose was not reached: nothing is freed')
        if self._leaves != asked:
            return False
        self._motion.freeze()
        self._motion.stop_moves(own=True)  # written nowhere: the still pose no longer counts as a move (the mic)
        freed = BODIES[body]
        if freed:
            logger.warning('[MANETTE] %s: freeing %s', body, ', '.join(freed))
            await asyncio.to_thread(self._mini.disable_motors, list(freed))
        self._measure = None
        self._taping = asyncio.create_task(self._tape(), name='manette-tape')
        self._sending = asyncio.create_task(self._send(), name='manette-poses')
        return True

    def _whole(self, mini: Any) -> Any:
        """`read_pose`'s reading as `read_frame` gives it, where no reader of the seven joints was given: the
        platform's six are told as zeros."""
        pose = self._read_pose(mini)
        return None if pose is None else (pose[0], pose[1], np.array([pose[2], 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))

    async def _tape(self) -> None:
        """The pose a hand gives the body, measured until the hold ends: the tape of this hold replaces the last."""
        self.tape.clear()
        while True:
            frame = await asyncio.to_thread(self._read_frame, self._mini)
            if frame is not None:
                head, antennas, joints = frame
                at = time.time()
                self.tape.add(at, (head, antennas, float(joints[0])))
                self._measure = (at, joints, head, antennas)
            await asyncio.sleep(1.0 / take.TAPE_HZ if frame is not None else RETURN_POLL_S)

    async def _send(self) -> None:
        """The latest measure handed on, each once, at the pace the Mac expects (poses.HZ) until the hold ends."""
        sent = None
        while True:
            measure = self._measure
            if measure is not None and measure is not sent:
                at, joints, head, antennas = sent = measure
                self._pose(at, joints, np.asarray(head).ravel(), antennas)
            await asyncio.sleep(1.0 / poses.HZ)

    # ----- leaving

    async def _release(self, why: str) -> None:
        self._ear(False)
        for task in (self._taping, self._sending):
            if task is not None:
                task.cancel()
        self._taping = self._sending = None
        body = body_of(self.profile)
        if self._motion.frozen:
            if BODIES[body or 'gachettes']:
                await self._returned()
                await self._take_back()  # raises while a motor stays free: still held, frozen, retried at the next exit
                await asyncio.sleep(TAKEN_BACK_S)
            current = await asyncio.to_thread(self._read_pose, self._mini)
            if current is None:  # pinned under torque, nothing written: safe to wait for the next exit
                raise RuntimeError('the pose could not be read: the robot stays still, held')
            self._motion.thaw(current)
        elif body is not None:  # an entry stopped before its still pose was frozen: nothing freed, nothing stopped
            self._motion.stop_moves(own=True)
        self._motion.lend(False)
        self.profile, self._held = None, False
        if body is not None:
            self._motion.turn_body(0.0, BACK_SPEED)
        logger.info('[MANETTE] off (%s)', why)
        self._report({'kind': 'manette', 'state': 'off', 'profile': None, 'why': why})

    async def _returned(self) -> None:
        """Wait, freed, until the robot is let go within reach of the IK (RETURN_WAIT_S at most); the dashboard says
        so meanwhile."""
        started = time.monotonic()
        deadline, still_since, last, asked = started + RETURN_WAIT_S, None, None, False
        while time.monotonic() < deadline:
            current = await asyncio.to_thread(self._read_pose, self._mini)
            if current is not None:
                angles = (math.degrees(current[2]), head_yaw(current[0], current[2]))
                moving = last is not None and max(abs(a - b) for a, b in zip(angles, last, strict=True)) > STILL_DEG
                last, now = angles, time.monotonic()
                still_since = None if moving else (still_since or now)
                if returnable(current) and still_since is not None and now - still_since >= STILL_S:
                    return
                if not asked and (not returnable(current) or now - started >= ASK_AFTER_S):
                    asked = True
                    self._report({'kind': 'manette', 'state': 'returning', 'profile': self.profile, 'why': None})
            await asyncio.sleep(RETURN_POLL_S)
        logger.warning('[MANETTE] not let go within reach after %.0f s: taken back where it is', RETURN_WAIT_S)

    async def _take_back(self) -> None:
        """Torque on again everywhere: global, so every target is pinned to the present pose and nothing snaps back to
        a stale one (by ids, it is not). Retried: until it passes, nothing may be written."""
        for attempt in range(1, TAKE_BACK_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(self._mini.enable_motors)
                return
            except Exception:
                logger.exception('[MANETTE] the torque did not come back (%d/%d)', attempt, TAKE_BACK_ATTEMPTS)
                if attempt == TAKE_BACK_ATTEMPTS:
                    raise
                await asyncio.sleep(TAKEN_BACK_S * attempt)

    async def _lease(self) -> None:
        """Lets go once the lease runs out, as seen with the lock held: a switch or a return may outlast a lease while
        the renewals wait behind it. A failed exit is tried again every second until it passes; an entry's says why
        that entry failed."""
        while self.profile is not None:
            if time.monotonic() > self._until:
                try:
                    async with self._lock:
                        if self.profile is not None and time.monotonic() > self._until:
                            await self._release('lease' if self._held else 'pose')
                except Exception as exc:  # noqa: BLE001 — whatever kept it, it stays held and frozen, and is retried
                    logger.warning('[MANETTE] the lease ran out, the body is not taken back yet: %s', exc)
                    await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(WATCH_S)
