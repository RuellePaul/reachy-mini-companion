"""Fan-out of WsMessage JSON to the dashboard's sockets. `publish`, `publish_pose` and `publish_voice` are safe from
any thread."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Iterable

from pydantic import BaseModel

QUEUE_MAX = 256
POSE = ''  # in a queue: "send this socket's latest pose frame now" (no message is empty)


class Broker:
    def __init__(self, queue_max: int = QUEUE_MAX) -> None:
        self._queues: set[asyncio.Queue[str | None]] = set()
        self._poses: dict[asyncio.Queue[str | None], str | None] = {}  # who wants `pose` -> its unsent latest frame
        self._voices: set[asyncio.Queue[str | None]] = set()  # who wants `voice`
        self._queue_max = queue_max
        self._loop: asyncio.AbstractEventLoop | None = None  # the loop the sockets live on
        self._shown: dict[asyncio.Queue[str | None], tuple[float, str, str]] = {}  # (since, kind, id) each one shows
        self._shown_lock = threading.Lock()  # `showing` is read from the routes' threads

    def subscribe(self) -> asyncio.Queue[str | None]:
        """A queue of JSON texts; None means the subscriber fell behind and must close; POSE, see `take_pose`."""
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue(self._queue_max)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str | None]) -> None:
        self._queues.discard(queue)
        self._poses.pop(queue, None)
        self._voices.discard(queue)
        self.show(queue, None)

    def watched(self) -> bool:
        """Whether a dashboard is connected: what is published now reaches one. From any thread."""
        return bool(self._queues)

    def show(self, queue: asyncio.Queue[str | None], showing: tuple[float, str, str] | None) -> None:
        """What a subscriber's dashboard shows now, (since, kind, id) of contracts.Showing, or nothing. `since` is the
        dashboard's: a tab reconnecting with an old app is not the latest shown."""
        with self._shown_lock:
            if showing is None:
                self._shown.pop(queue, None)
            else:
                self._shown[queue] = showing

    def showing(self, kind: str) -> str | None:
        """The id of the `kind` ('app', 'scene') a dashboard began showing last, among those still connected."""
        with self._shown_lock:
            shown = sorted(item for item in self._shown.values() if item[1] == kind)
        return shown[-1][2] if shown else None

    def want(self, queue: asyncio.Queue[str | None], channels: Iterable[str]) -> None:
        """The optional channels a subscriber wants now (contracts.WsClientMessage). On the loop."""
        if 'pose' in channels and queue in self._queues:
            self._poses.setdefault(queue, None)
        else:
            self._poses.pop(queue, None)
        if 'voice' in channels and queue in self._queues:
            self._voices.add(queue)
        else:
            self._voices.discard(queue)

    def wants_pose(self) -> bool:
        return bool(self._poses)

    def take_pose(self, queue: asyncio.Queue[str | None]) -> str | None:
        """The latest pose frame for this subscriber, once: what its POSE marker stands for."""
        text = self._poses.get(queue)
        if text is not None:
            self._poses[queue] = None
        return text

    def publish(self, message: BaseModel) -> None:
        """Send one WsMessage (WsSettings, WsVolume, ...) to every subscriber."""
        self._on_loop(self._fan_out, message.model_dump_json())

    def publish_pose(self, message: BaseModel) -> None:
        """A WsPose to the subscribers that want it, latest-only: each holds one frame and one marker in its queue at
        most, so a slow socket skips frames and the pose stream never pushes it past `queue_max`."""
        self._on_loop(self._offer_pose, message.model_dump_json())

    def publish_voice(self, message: BaseModel) -> None:
        """A WsVoice to the subscribers that want it; a full queue skips it (the next one comes 50 ms later)."""
        self._on_loop(self._offer_voice, message.model_dump_json())

    def _on_loop(self, deliver: Callable[[str], None], text: str) -> None:
        loop = self._loop
        if loop is None:
            return  # nobody has ever subscribed
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            deliver(text)
        else:
            with contextlib.suppress(RuntimeError):  # the loop has closed: nobody to tell
                loop.call_soon_threadsafe(deliver, text)

    def _offer_pose(self, text: str) -> None:
        for queue, unsent in list(self._poses.items()):
            if unsent is None:
                try:
                    queue.put_nowait(POSE)
                except asyncio.QueueFull:
                    continue  # this frame is skipped; the queue's own rule decides whether the socket closes
            self._poses[queue] = text

    def _offer_voice(self, text: str) -> None:
        for queue in self._voices:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(text)

    def _fan_out(self, text: str) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                # A dashboard this far behind is closed: it reconnects and re-reads its state.
                self.unsubscribe(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
