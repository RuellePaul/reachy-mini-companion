"""Fakes for the robot agent: hardware, Mac and a scripted Gemini Live. Each records what it was asked to do."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import google.genai.live as sdk_live
import numpy as np
import pytest
from google.genai import types
from websockets.asyncio.client import connect as ws_connect
from websockets.utils import accept_key

from robot_agent import config, live
from robot_agent.animations import GESTURES
from robot_agent.emotions import CURATED
from robot_agent.mac import MacError
from robot_agent.session import Session
from robot_agent.tools import Tools


class FakeAudio:
    """A microphone that hands out `mic()` (None: silent) every `pull_s`, and a speaker that records."""

    def __init__(self, mic: Callable[[], np.ndarray | None] | None = None, *, mic_rate=16000, speaker_rate=16000):
        self.mic, self.mic_rate, self.speaker_rate = mic, mic_rate, speaker_rate
        self.pull_s = 0.016 if mic else 0.002
        self.pushed: list[np.ndarray] = []
        self.empty_pushes = 0
        self.cleared = 0
        self.sounds: list[str] = []
        self.sound_stops = 0
        self.pull_threads: set[int] = set()  # the threads the microphone was read on

    def get_input_samplerate(self) -> int:
        return self.mic_rate

    def get_output_samplerate(self) -> int:
        return self.speaker_rate

    def get_audio_sample(self) -> np.ndarray | None:
        self.pull_threads.add(threading.get_ident())
        time.sleep(self.pull_s)
        return self.mic() if self.mic else None

    def push_audio_sample(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            self.empty_pushes += 1
        self.pushed.append(samples)

    def clear_output(self) -> None:
        self.cleared += 1

    def play_sound(self, path: str) -> None:
        self.sounds.append(path)

    def stop_sound(self) -> None:
        self.sound_stops += 1


class FakeBody:
    def __init__(self) -> None:
        self.modes: list[str] = []
        self.gestures: list[str] = []
        self.moves: list[Any] = []
        self.turns: list[float] = []
        self.facing = 0.0  # where the body faces or heads: the sleep pose faces the audience
        self.stopped = 0
        self.moving_end = 0.0  # what moving_until() answers: set it to stage a move
        self.playing: Any = None  # what current_move() answers: the last move played, until a stop

    def set_mode(self, mode: str, blend_s: float = 0.5) -> None:
        self.modes.append(mode)

    def turn_body(self, degrees: float, speed: float = 1.0) -> float:
        """Records the yaw asked; answers the target as MotionRunner does, within ±100° (pinned in test_motion). As
        there, a turn to where the body faces or heads is nothing: not recorded."""
        target = max(-100.0, min(100.0, degrees))
        if target != self.facing:
            self.turns.append(degrees)
            self.facing = target
        return target

    def gesture(self, name: str) -> bool:
        if name in GESTURES:
            self.gestures.append(name)
        return name in GESTURES

    def play_move(self, move: Any) -> None:
        self.moves.append(move)
        self.playing = move

    def stop_moves(self) -> None:
        self.stopped += 1
        self.playing = None

    def moving_until(self) -> float:
        return self.moving_end

    def current_move(self) -> Any:
        return self.playing


class FakeCamera:
    def __init__(self, jpeg: bytes | None = b'\xff\xd8jpeg') -> None:
        self.jpeg = jpeg

    def read_jpeg(self) -> bytes | None:
        return self.jpeg


class FakeMac:
    """`request` answers from `answers` by path, or fails with `error`, or waits `delay` first."""

    def __init__(self, answers: dict[str, dict] | None = None, error: str | None = None, delay: float = 0.0) -> None:
        self.answers, self.error, self.delay = answers or {}, error, delay
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls.append((method, path, body))
        time.sleep(self.delay)
        if self.error:
            raise MacError(self.error)
        return self.answers.get(path, {})


class FakeEmotions:
    def __init__(self, names: tuple[str, ...] = CURATED) -> None:
        self._names = list(names)

    def names(self) -> list[str]:
        return list(self._names)

    def get(self, name: str) -> Any:
        return SimpleNamespace(name=name, sound_path=f'/sounds/{name}.wav') if name in self._names else None


def _ends_interaction(message: Any) -> bool:
    """google-genai 2.24's rule for where receive() stops: interaction_status IDLE, else turn_complete."""
    content = getattr(message, 'server_content', None)
    if content is None:
        return False
    status = content.interaction_status
    if status not in (None, types.InteractionStatus.INTERACTION_STATUS_UNSPECIFIED):
        return status == types.InteractionStatus.IDLE
    return bool(content.turn_complete)


class FakeLive:
    """One Live connection: frames pushed into `inbox` are received; an exception pushed there is raised.

    Like the real SDK, each receive() ends at the end of an interaction: the session has to call it again.
    """

    def __init__(self, live_config: types.LiveConnectConfig) -> None:
        self.config = live_config
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.audio: list[tuple[float, bytes]] = []  # (monotonic time sent, PCM)
        self.responses: list[types.FunctionResponse] = []
        self.turns: list[list[types.Content]] = []
        self.receives = 0  # how many times receive() was entered
        self.closed = False

    async def receive(self):
        self.receives += 1
        while True:
            item = await self.inbox.get()
            if isinstance(item, BaseException):
                raise item
            yield item
            if _ends_interaction(item):
                return

    async def send_realtime_input(self, *, audio: types.Blob) -> None:
        self.audio.append((time.monotonic(), audio.data))
        await asyncio.sleep(0)  # a socket send yields: a pump that read the mic inline would block, not hang

    async def send_tool_response(self, *, function_responses: list[types.FunctionResponse]) -> None:
        self.responses.extend(function_responses)

    async def send_client_content(self, *, turns: list[types.Content], turn_complete: bool = True) -> None:
        self.turns.append(turns)

    def push(self, **fields: Any) -> None:
        self.inbox.put_nowait(types.LiveServerMessage(**fields))

    @property
    def handle(self) -> str | None:
        return self.config.session_resumption.handle

    @property
    def searching(self) -> bool:
        return any(tool.google_search is not None for tool in self.config.tools)


class FakeClient:
    """Stands in for genai.Client. `fail` holds exceptions raised by the next connects, in order;
    `refuse` decides from the config whether a connect is refused (a handle, Google Search...)."""

    def __init__(self) -> None:
        self.connections: list[FakeLive] = []
        self.attempts: list[types.LiveConnectConfig] = []
        self.fail: list[BaseException] = []
        self.refuse: Callable[[types.LiveConnectConfig], BaseException | None] = lambda _config: None
        self.on_open: Callable[[FakeLive], None] = lambda _live: None  # e.g. a socket closed as soon as opened
        self.stall = False  # the server takes the socket and never completes the setup
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=self._connect))

    @contextlib.asynccontextmanager
    async def _connect(self, *, model: str, config: types.LiveConnectConfig):
        assert model == 'gemini-3.8-live'
        self.attempts.append(config)
        await asyncio.sleep(0)
        if self.stall:
            await asyncio.Event().wait()
        error = self.fail.pop(0) if self.fail else self.refuse(config)
        if error is not None:
            raise error
        connection = FakeLive(config)
        self.connections.append(connection)
        self.on_open(connection)
        try:
            yield connection
        finally:
            connection.closed = True

    @property
    def live(self) -> FakeLive:
        return self.connections[-1]


@contextlib.asynccontextmanager
async def silent_peer(monkeypatch: pytest.MonkeyPatch):
    """The real Gemini client (`live.client`), its WebSocket pointed at a local peer that completes the upgrade, then
    never reads nor answers again: a path black-holed after the handshake. Yields (client, the upgraded sockets)."""
    upgraded: list[asyncio.StreamWriter] = []

    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b'\r\n\r\n')).decode()
        key = next(
            line.split(':', 1)[1].strip()
            for line in head.split('\r\n')
            if line.lower().startswith('sec-websocket-key:')
        )
        writer.write(
            'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
            f'Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n'.encode()
        )
        upgraded.append(writer)
        await asyncio.Event().wait()

    server = await asyncio.start_server(peer, '127.0.0.1', 0)
    url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
    # The SDK opens wss://… with ssl=: a plain local socket takes every other argument as the SDK gives it.
    monkeypatch.setattr(sdk_live, 'ws_connect', lambda _uri, *, ssl=None, **kwargs: ws_connect(url, **kwargs))
    try:
        yield live.client('not-a-key'), upgraded
    finally:
        for writer in upgraded:
            writer.close()
        server.close()


def settings_view(**settings: Any) -> dict[str, Any]:
    """A SettingsView as GET /api/settings returns it (contracts.py)."""
    base = {
        'name': 'Reachy',
        'voice': 'Achird',
        'personality': '',
        'idle_animation': 'calm',
        'listening_animation': 'attentive',
        'talking_animation': 'calm',
        'tuning': {mode: {'amplitude': 1.0, 'speed': 1.0} for mode in ('idle', 'listening', 'talking')},
        'web_search': False,
        'gaze_tracking': False,
        'idle_sleep_minutes': 3,
    }
    return {
        'settings': {**base, **settings},
        'system_prompt': 'Tu es Reachy, un petit robot.',
        'web_prompt': 'Tu peux chercher sur le web.',
        'options': {},
    }


# ----- server frames -----


def content(**fields: Any) -> dict[str, Any]:
    return {'server_content': types.LiveServerContent(**fields)}


def heard(text: str) -> dict[str, Any]:
    return content(input_transcription=types.Transcription(text=text))


def speech(n_samples: int = 2400, said: str | None = None) -> dict[str, Any]:
    """Model audio: `n_samples` of silence at 24 kHz, with what it says if `said`."""
    part = types.Part(inline_data=types.Blob(data=b'\x00\x00' * n_samples, mime_type='audio/pcm;rate=24000'))
    transcription = types.Transcription(text=said) if said else None
    return content(model_turn=types.Content(role='model', parts=[part]), output_transcription=transcription)


def call(tool: str, call_id: str, /, **args: Any) -> dict[str, Any]:
    return {
        'tool_call': types.LiveServerToolCall(function_calls=[types.FunctionCall(id=call_id, name=tool, args=args)])
    }


def vad(kind: str) -> dict[str, Any]:
    return {'voice_activity': types.VoiceActivity(voice_activity_type=types.VoiceActivityType(kind))}


def resumable(handle: str) -> dict[str, Any]:
    return {'session_resumption_update': types.LiveServerSessionResumptionUpdate(resumable=True, new_handle=handle)}


async def until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('condition not met in time')
        await asyncio.sleep(0.005)


class Rig:
    """A session wired to fakes. Use inside asyncio.run: `start()`, push frames, then `stop()`."""

    def __init__(self, view: dict[str, Any] | None = None, *, audio: FakeAudio | None = None, **tools: Any) -> None:
        self.client = FakeClient()
        self.audio = audio or FakeAudio()
        self.body = FakeBody()
        self.mac = tools.pop('mac', FakeMac())
        self.events: list[dict[str, Any]] = []
        self.tools = Tools(
            body=self.body,
            audio=self.audio,
            camera=tools.pop('camera', FakeCamera()),
            mac=self.mac,
            emotions=tools.pop('emotions', FakeEmotions()),
        )
        self.session = Session(
            self.client,
            view or settings_view(),
            session_id='s-test',
            audio=self.audio,
            body=self.body,
            tools=self.tools,
            report=self.events.append,
            **tools,
        )
        self.task: asyncio.Task[str] | None = None

    async def start(self) -> Rig:
        self.task = asyncio.create_task(self.session.run())
        await until(lambda: self.session.state.value != 'connecting')
        return self

    async def stop(self, reason: str = 'dashboard') -> str:
        self.session.request_sleep(reason)
        assert self.task is not None
        return await asyncio.wait_for(self.task, 3.0)

    def kinds(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event['kind'] == kind]

    def phases(self) -> list[str]:
        return [event['phase'] for event in self.kinds('phase')]


@pytest.fixture(autouse=True)
def _fast_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short waits everywhere: the tests run the real loops on real (short) time."""
    monkeypatch.setattr(config, 'WATCHDOG_TICK_S', 0.01)
    monkeypatch.setattr(config, 'RECONNECT_BACKOFF_S', (0.01, 0.01, 0.01))
    monkeypatch.setattr(config, 'MIC_RETRY_S', 0.002)
    monkeypatch.setattr(config, 'LIVE_WAIT_S', 0.3)
