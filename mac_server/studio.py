"""The studio on the Mac: 3D previews sampled from the robot's own motion code, and the robot's measured pose relayed
while a dashboard wants it: from its daemon's stream, or from the datagrams the robot sends itself while it lends its
body (docs/CONTRACTS.md). The Mac never commands the daemon."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
import websockets
from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from mac_server.broker import Broker
from mac_server.contracts import AnimationTuning, CatalogEntry, PoseFrame, PreviewFrames, WsPose
from robot_agent import animation_names as anim
from robot_agent import animations, motion, poses
from robot_agent import config as robot_config
from robot_agent.recorded import Recorded

if TYPE_CHECKING:
    from mac_server.server import Mac

log = logging.getLogger(__name__)

EMOTION_URL = 'https://huggingface.co/datasets/pollen-robotics/reachy-mini-emotions-library/resolve/main/{}.json'
DOWNLOAD_TIMEOUT_S = 10.0
MOVE_AT_S = 0.3  # a gesture or an emotion starts after a moment at rest
ANIMATION_S, PREVIEW_MAX_S = 8.0, 20.0
STATE_STREAM = 'ws://{}:{}/api/state/ws/full?frequency=30&use_pose_matrix=true&with_head_joints=true'
RELAY_LINGER_S = 3.0  # the stream stays open this long after the last studio leaves (a reload, a reconnect)
RELAY_RETRY_S = 1.0
DATAGRAMS_FRESH_S = 1.0  # a pose datagram this recent: the robot sends its pose itself, the stream is left alone


class Service:
    """Previews, the emotions they need (downloaded once, cached on disk), and the pose relay (`run`)."""

    def __init__(self, mac: Mac) -> None:
        self._mac = mac
        self._emotions: dict[str, Recorded] = {}
        self._lock = threading.Lock()

    def catalog(self) -> list[CatalogEntry]:
        return []

    def preview(self, cues: list[motion.Cue], seconds: float, mode: str = 'idle') -> PreviewFrames:
        """The frames the robot's motion thread would send for these cues (motion.rehearse): every 3D preview,
        the studio's and the scene editor's, goes through here."""
        frames = motion.rehearse(cues, seconds, mode=mode)
        return PreviewFrames(
            hz=robot_config.MOTION_HZ,
            head=[[round(float(v), 6) for v in head.flatten()] for head, _, _ in frames],
            antennas=[[round(float(v), 6) for v in antennas] for _, antennas, _ in frames],
            body=[round(float(body), 6) for _, _, body in frames],
        )

    def emotion(self, name: str) -> Recorded | None:
        """A recorded emotion's frames, as the robot plays them; None when unknown or unavailable. Blocking (a
        download the first time): call it from a plain `def` route or a thread."""
        if name not in anim.EMOTIONS:
            return None
        with self._lock:
            if name not in self._emotions:
                path = self._mac.config.cache_dir / 'emotions' / f'{name}.json'
                try:
                    data = json.loads(path.read_bytes()) if path.is_file() else _download(name, path)
                    self._emotions[name] = Recorded(data)
                except (OSError, ValueError, KeyError, TypeError, requests.RequestException) as exc:
                    log.warning('emotion %s unavailable: %s', name, exc)
                    path.unlink(missing_ok=True)  # a bad cached copy is fetched again next time
                    return None
            return self._emotions[name]

    def script(
        self, kind: str, name: str, mode: str | None, amplitude: float, speed: float, seconds: float
    ) -> tuple[list[motion.Cue], float, str]:
        """GET /api/studio/preview's parameters -> (cues, seconds, mode) for `preview`; HTTPException, French."""
        if kind == 'animation':
            if mode not in anim.MODES:
                raise HTTPException(422, detail=f'Mode inconnu\xa0: «\xa0{mode}\xa0» (idle, listening ou talking).')
            try:
                AnimationTuning.model_validate({mode: {'amplitude': amplitude, 'speed': speed}})
            except ValidationError as exc:
                raise RequestValidationError(exc.errors()) from exc
            if not 1.0 <= seconds <= PREVIEW_MAX_S:
                raise HTTPException(422, detail=f'Durée\xa0: entre 1 et {PREVIEW_MAX_S:g} secondes.')
            if name not in animations.VARIANTS[mode]:
                raise HTTPException(404, detail=f'Animation inconnue\xa0: «\xa0{name}\xa0».')
            tuning = animations.Tuning(amplitude, speed)
            return [(0.0, lambda m, at: m.set_variant(mode, name, at, tuning))], seconds, mode
        moves = {'gesture': animations.GESTURES.get, 'emotion': self.emotion}
        if kind not in moves:
            raise HTTPException(422, detail="Type d'aperçu inconnu\xa0: gesture, emotion ou animation.")
        move = moves[kind](name)
        if move is None:
            what = 'Geste inconnu' if kind == 'gesture' else 'Émotion inconnue ou indisponible'
            raise HTTPException(404, detail=f'{what}\xa0: «\xa0{name}\xa0».')
        still = (0.0, lambda m, at: m.set_variant('idle', 'still', at))
        return [still, (MOVE_AT_S, lambda m, at: m.play_move(move, at))], move.duration + 1.0, 'idle'

    async def run(self) -> None:
        """The robot's pose to whoever wants `pose`: its own datagrams while it lends its body (UDP, the server's
        port), else the daemon's stream."""
        config = self._mac.config
        heard, endpoint = Datagrams(config.api_key, self._mac.broker), None
        try:
            where = ('0.0.0.0', config.port)  # as the server itself listens
            endpoint, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: heard, local_addr=where)
        except OSError as exc:
            log.warning('no pose datagrams (UDP port %d: %s): the daemon stream alone', config.port, exc)
        try:
            stream = lambda: STATE_STREAM.format(self._mac.health.host, config.reachy_port)  # noqa: E731
            await relay_poses(stream, self._mac.broker, fresh=heard.fresh)
        finally:
            if endpoint is not None:
                endpoint.close()


def _download(name: str, path: Path) -> dict[str, Any]:
    """One emotion from Pollen's dataset (plain HTTPS, free), checked, then cached for good."""
    reply = requests.get(EMOTION_URL.format(name), timeout=DOWNLOAD_TIMEOUT_S)
    reply.raise_for_status()
    data = reply.json()
    Recorded(data).evaluate(0.0)  # raises on a file that is not a recorded move
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix('.part')
    partial.write_bytes(reply.content)
    os.replace(partial, path)
    return data


def pose_frame(raw: str | bytes) -> PoseFrame | None:
    """One message of the daemon's state stream, compacted; None when it lacks the pose (motors off, a bad frame)."""
    try:
        state = json.loads(raw)
        stamp = state.get('timestamp')
        ms = datetime.fromisoformat(stamp).timestamp() * 1000 if isinstance(stamp, str) else time.time() * 1000
        values = (state['head_joints'], state['head_pose']['m'], state['antennas_position'])
        j, m, a = ([round(float(v), 5) for v in vector] for vector in values)
        return PoseFrame(t=round(ms, 1), j=j, m=m, a=a)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class Datagrams(asyncio.DatagramProtocol):
    """The lent body's pose as the robot sends it itself (robot_agent/poses.py), published as the daemon's frames
    are. Over TCP the daemon's stream stops whenever the box is late handing the robot the Mac's acknowledgements;
    a datagram waits for none."""

    def __init__(self, key: str, broker: Broker, clock: Callable[[], float] = time.monotonic) -> None:
        self._key, self._broker, self._clock = key, broker, clock
        self._at, self._heard = 0.0, float('-inf')  # the last measure's time on the robot's clock, and when it came

    def fresh(self) -> bool:
        return self._clock() - self._heard < DATAGRAMS_FRESH_S

    def datagram_received(self, data: bytes, addr: Any) -> None:
        """One measure, unless it is not one of this robot's (its key), or came after a later one (while they come:
        a robot whose clock was set back is heard again once it has been silent)."""
        measure = poses.unpack(self._key, data)
        if measure is None or (measure[0] <= self._at and self.fresh()):
            return
        at, *parts = measure
        self._at, self._heard = at, self._clock()
        j, m, a = ([round(value, 5) for value in part] for part in parts)
        self._broker.publish_pose(WsPose(channel='pose', data=PoseFrame(t=round(at * 1000, 1), j=j, m=m, a=a)))


async def relay_poses(
    url: Callable[[], str],
    broker: Broker,
    linger_s: float = RELAY_LINGER_S,
    retry_s: float = RELAY_RETRY_S,
    fresh: Callable[[], bool] = lambda: False,
) -> None:
    """Reads the daemon's state stream while some socket wants `pose` (READ-ONLY: it never sends the daemon
    anything), until `linger_s` after the last one leaves; retries every `retry_s` while wanted. While the robot's
    own datagrams come (`fresh`), the stream is closed: they are the pose, and it would only share their radio."""
    failing = False
    while True:
        while not broker.wants_pose() or fresh():
            await asyncio.sleep(0.1)
        try:
            async with websockets.connect(url(), open_timeout=3, max_size=1 << 16) as ws:
                failing, unwanted_since = False, None
                async for raw in ws:
                    if fresh():
                        break
                    if broker.wants_pose():
                        unwanted_since = None
                    elif unwanted_since is None:
                        unwanted_since = time.monotonic()
                    elif time.monotonic() - unwanted_since >= linger_s:
                        break
                    if (frame := pose_frame(raw)) is not None:
                        broker.publish_pose(WsPose(channel='pose', data=frame))
        except (OSError, TimeoutError, websockets.WebSocketException) as exc:
            if not failing:
                log.info('robot pose unavailable (%s): retrying every %g s while a studio is open', exc, retry_s)
            failing = True
        await asyncio.sleep(retry_s)


def routes(mac: Mac) -> APIRouter:
    router = APIRouter()

    @router.get('/api/studio/preview')
    def preview(
        kind: str,
        name: str,
        mode: str | None = None,
        amplitude: float = 1.0,
        speed: float = 1.0,
        seconds: float = ANIMATION_S,
    ) -> PreviewFrames:
        """A gesture, an emotion or a mode's variant as the robot would play it (`emotion` may download: plain def)."""
        return mac.studio.preview(*mac.studio.script(kind, name, mode, amplitude, speed, seconds))

    return router
