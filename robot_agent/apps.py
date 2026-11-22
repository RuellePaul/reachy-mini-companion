"""Apps on the robot's side: the `app` tool (made, changed or opened by voice; built by the Mac) and the moves an
app's bridge asks for (contracts.AppMotion), played through the one motion thread while the robot converses."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from robot_agent import animations
from robot_agent.mac import Mac, MacError
from robot_agent.tools import Extra, SendImage, ToolResult, _decl, _text

logger = logging.getLogger(__name__)

HOLD_S = 1.2  # a look or an antenna pose an app asks for is held this long, then the robot eases back
BUILD_POLL_S = 3.0
BUILD_WATCH_S = 45 * 60.0  # longer than a build with its repair can last on the Mac (2 x 20 min)
MAC_LOST_S = 60.0  # the Mac unreachable this long: the build is no longer waited for (the idle sleep comes back)
REPEAT_S = 3.0  # one ask can come as several identical calls: one build for them all
REPEATED = ToolResult('ok', "Déjà transmis\xa0: c'est la demande de l'appel d'avant.", silent=True)


@dataclass(frozen=True)
class Hold:
    """A pose held for `duration`: the motion thread blends into it and back out of it."""

    head: Any
    antennas: Any
    duration: float = HOLD_S

    def evaluate(self, t: float) -> animations.Pose:
        return self.head, self.antennas


def move(motion: dict[str, Any], emotions: Any, previous: Hold | None = None) -> Any | None:
    """The move an app's message names. `look` is page-centric (yaw -30 = the page's left, pitch +20 its top):
    facing the user, the page's left is the robot's right, a negative yaw as is; looking up is a negative pitch; its
    roll is the manette pad's `head.roll` as it is. Blocking (an emotion is read from disk): call it in a thread."""
    kind = motion.get('kind')
    try:
        if kind == 'emotion':
            return emotions.get(str(motion.get('name', '')))
        if kind == 'gesture':
            return animations.GESTURES.get(str(motion.get('name', '')))
        if kind == 'look':
            yaw = max(-30.0, min(30.0, float(motion['yaw'])))
            pitch = max(-20.0, min(20.0, float(motion['pitch'])))
            roll = max(-20.0, min(20.0, float(motion.get('roll') or 0.0)))
            antennas = previous.antennas if previous is not None else animations.NEUTRAL  # a pose held stays
            return Hold(animations.pose(pitch=-pitch, yaw=yaw, roll=roll), antennas)
        if kind == 'antennas':
            left = max(-60.0, min(60.0, float(motion['left'])))
            right = max(-60.0, min(60.0, float(motion['right'])))
            head = previous.head if previous is not None else animations.pose()
            return Hold(head, animations.antennas(right=right, left=left))
    except (KeyError, TypeError, ValueError):
        return None
    return None


class Builds:
    """The apps this conversation asked for, while they are being built: the robot stays awake for them (no idle
    sleep) and is told when each one is ready (`notify`). On the event loop; `clear` when the conversation ends."""

    def __init__(self, mac: Mac, notify: Callable[[str], None]) -> None:
        self._mac, self._notify = mac, notify
        self._watching: dict[str, asyncio.Task[None]] = {}

    def pending(self) -> bool:
        return bool(self._watching)

    def watch(self, app: dict[str, Any]) -> None:
        app_id = str(app.get('id', ''))
        if app.get('status') == 'building' and app_id and app_id not in self._watching:
            self._watching[app_id] = asyncio.create_task(self._until_built(app_id), name=f'build-{app_id}')

    def clear(self) -> None:
        for task in self._watching.values():
            task.cancel()
        self._watching.clear()

    async def _until_built(self, app_id: str) -> None:
        try:
            app = await ended(self._mac, '/api/apps', 'apps', app_id, '[APP]')
            if app is not None and (note := built(app)):
                self._notify(note)
        finally:
            self._watching.pop(app_id, None)


async def ended(mac: Mac, path: str, listed: str, item_id: str, tag: str) -> dict[str, Any] | None:
    """What the Mac builds for a conversation (an app, a scene), once it is no longer building: the item `item_id`
    of `path`'s `listed` list. None when it is gone, or no longer waited for (the Mac silent for MAC_LOST_S, or
    BUILD_WATCH_S gone by)."""
    deadline = time.monotonic() + BUILD_WATCH_S
    answered = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(BUILD_POLL_S)
        try:
            answer = await asyncio.to_thread(mac.request, 'GET', path)
        except MacError as exc:  # the Mac restarting: the build is marked interrupted, read at the next poll
            if time.monotonic() - answered < MAC_LOST_S:
                continue
            logger.warning('%s the Mac has not answered for %.0f s (%s): %s no longer waited for',
                           tag, MAC_LOST_S, exc, item_id)  # fmt: skip
            return None
        answered = time.monotonic()
        item = next((i for i in answer.get(listed) or [] if i.get('id') == item_id), None)
        if item is None or item.get('status') != 'building':
            return item
    logger.warning('%s %s still building after %.0f min: no longer waited for', tag, item_id, BUILD_WATCH_S / 60)
    return None


class Once:
    """One ask can come as several identical calls: the first is sent to the Mac; one that
    repeats it within REPEAT_S waits for that answer, whatever it is, and gets None (the tool says REPEATED)."""

    def __init__(self, mac: Mac, path: str) -> None:
        self._mac, self._path = mac, path
        self._ask, self._at = '', -math.inf  # the last ask sent, and when it was asked
        self._sent: asyncio.Future[dict[str, Any]] | None = None  # its request: only an identical call waits for it

    async def send(self, body: dict[str, Any]) -> dict[str, Any] | None:
        ask, asked_at = repr(sorted(body.items())), time.monotonic()
        if self._sent is not None and ask == self._ask and asked_at - self._at < REPEAT_S:
            await asyncio.wait([self._sent])  # the first call's answer, whatever it was, says it
            return None
        self._ask, self._at = ask, asked_at
        self._sent = sent = asyncio.ensure_future(asyncio.to_thread(self._mac.request, 'POST', self._path, body))
        return await sent


CANCELLED = 'Modification annulée.'  # mac_server/apps.CANCELLED: cancelled on the dashboard, nothing to say


def built(app: dict[str, Any]) -> str | None:
    """The note that tells the model how a build it asked for ended (French); none when it was cancelled."""
    title, error = app.get('title', ''), app.get('error')
    if error == CANCELLED:
        return None
    if app.get('status') == 'failed':
        return f"L'app « {title} » n'a pas pu être construite : {error} Dis-le simplement."
    if error:
        return f"La modification de « {title} » a échoué : {error} L'ancienne version reste. Dis-le simplement."
    return f"L'app « {title} » est prête, sur le dashboard. Annonce-le en une phrase."  # shown there, or offered


def tools(mac: Mac, builds: Builds) -> Extra:
    declaration = _decl(
        'app',
        'Crée, modifie ou ouvre une app interactive sur le dashboard (jeu, petit outil) : « fais-moi un Snake », '
        '« ajoute un niveau », « ouvre le morpion ». Elle peut faire bouger ta tête et tes antennes.',
        action=_text(
            'create = nouvelle app, update = modifier une app, open = la montrer.', ['create', 'update', 'open']
        ),
        request=_text("Ce que l'app doit être ou devenir, avec tous les détails dits."),
        name=_text("Le nom de l'app à modifier ou ouvrir."),
        optional=('request', 'name'),
    )

    once = Once(mac, '/api/apps/voice')

    async def run(args: dict[str, Any], _send: SendImage) -> ToolResult:
        body = {
            'action': str(args.get('action', '')),
            'request': str(args.get('request', ''))[:2000],
            'name': str(args.get('name', ''))[:60],
        }
        try:
            answer = await once.send(body)
        except MacError as exc:
            return ToolResult('error', str(exc))
        if answer is None:
            return REPEATED
        builds.watch(answer.get('app') or {})
        return ToolResult('ok', str(answer.get('message') or "C'est noté."))

    return {'app': (declaration, run)}
