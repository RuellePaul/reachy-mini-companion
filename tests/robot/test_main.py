"""The robot's lifecycle: the boot checks, wakes, commands, whole wake-to-sleep cycles and a full run."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import requests
from dotenv import dotenv_values
from google.genai import errors as genai_errors
from pydantic import TypeAdapter

from mac_server import contracts
from robot_agent import apps, boot, config, main, manette, speaker, studio, take
from robot_agent.adapters import robot as adapters_robot
from robot_agent.audio import ComfortNoise
from robot_agent.main import Robot
from robot_agent.motion import SLEEP_ANTENNAS, SLEEP_HEAD
from robot_agent.state import SessionState
from tests.robot.conftest import FakeAudio, FakeCamera, FakeClient, settings_view, until


@pytest.mark.parametrize(
    ('version', 'refusal'),
    [
        ('2.24.0', None),
        ('2.34.0', None),
        ('2.99.1', None),
        ('2.23.9', 'too old'),
        ('3.0', 'never ran'),
        ('3.0.0', 'never ran'),
    ],
)
def test_the_sdk_range(version: str, refusal: str | None) -> None:
    """The floor, and the next major: 3.0 opens the Live session on another endpoint."""
    if refusal is None:
        boot.require_genai(version)
    else:
        with pytest.raises(SystemExit, match=refusal):
            boot.require_genai(version)


def test_the_installed_sdk_passes_the_floor() -> None:
    boot.require_genai()


class FakeMedia:
    def __init__(self) -> None:
        self.audio = SimpleNamespace(clear_player=lambda: None)
        self.camera = None

    def get_input_audio_samplerate(self) -> int:
        return 16000

    def get_output_audio_samplerate(self) -> int:
        return 16000

    def get_audio_sample(self) -> None:
        time.sleep(0.002)

    def push_audio_sample(self, samples: Any) -> None: ...
    def play_sound(self, path: str) -> None: ...


class FakeMini:
    """Enough of a ReachyMini for the motion thread, the antenna thread and the audio adapter."""

    def __init__(self) -> None:
        self.media = FakeMedia()
        self.antennas = [-3.05, 3.05]
        self.heads: list[np.ndarray] = []
        self.follow = False  # the antenna motors follow their command (else only a hand moves them)

    def get_current_head_pose(self) -> np.ndarray:
        return SLEEP_HEAD

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
        return [0.0] * 7, list(self.antennas)

    def get_present_antenna_joint_positions(self) -> list[float]:
        return list(self.antennas)

    def set_target_head_pose(self, pose: np.ndarray) -> None:
        self.heads.append(pose)

    def set_target_antenna_joint_positions(self, antennas: list[float]) -> None:
        if self.follow:
            self.antennas = list(antennas)

    def set_target_body_yaw(self, yaw: float) -> None: ...


class FakeLibrary:
    def __init__(self, dataset: str) -> None:
        self.dataset = dataset

    def list_moves(self) -> list[str]:
        return ['cheerful1', 'dance1']

    def get(self, name: str) -> Any:
        return SimpleNamespace(name=name)


@pytest.fixture
def robot_parts(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A Robot on fakes; nothing started, nothing reached over the network."""
    for name, value in (('SLEEP_S', 0.01), ('SLEEP_SETTLE_S', 0.0), ('BODY_TURN_MIN_S', 0.01)):
        monkeypatch.setattr(config, name, value)
    sounds: list[str] = []
    monkeypatch.setattr(speaker, 'play', sounds.append)
    monkeypatch.setattr(speaker, 'stop', lambda: sounds.append('stop'))  # never the daemon
    monkeypatch.setattr(main, 'emotion_library', FakeLibrary)  # never Hugging Face
    return SimpleNamespace(mini=FakeMini(), client=FakeClient(), sounds=sounds, events=[])


def _robot(parts: SimpleNamespace, settings: Any, camera: Any = None) -> Robot:
    robot = Robot(parts.mini, parts.client, camera=camera)
    robot._mac.emit = parts.events.append

    def fetch() -> dict:
        if callable(settings):
            return settings()
        if isinstance(settings, Exception):
            raise settings
        return settings

    robot._mac.fetch_settings = fetch
    return robot


def test_what_the_speaker_is_given_wobbles_the_head(robot_parts: SimpleNamespace) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        pushed: list[int] = []
        robot_parts.mini.media.push_audio_sample = lambda samples: pushed.append(samples.size)
        robot._motion.set_mode('talking')
        t = np.arange(16000) / 16000
        robot._audio.push_audio_sample((0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
        assert pushed == [16000]
        later = time.monotonic() + config.SPEAKER_DELAY_S + 0.5
        assert robot._motion.mixer.wobble.offset(later) is not None
        robot._audio.clear_output()  # a flush: nothing queued plays, the wobble fades out
        assert robot._motion.mixer.wobble.offset(later) is None

    asyncio.run(scenario())


def test_a_failed_settings_fetch_sends_the_robot_back_to_sleep(robot_parts: SimpleNamespace) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, TimeoutError('Mac down'))
        await robot._cycle()
        assert robot_parts.sounds == ['wake_up.wav', 'confused1.wav']
        assert [e['kind'] for e in robot_parts.events] == ['session_start', 'session_end']
        assert robot_parts.events[-1]['reason'] == 'error'
        assert robot_parts.client.attempts == []
        assert robot._motion.mixer.mode == 'sleep' and robot._beat()['awake'] is False

    asyncio.run(scenario())


def test_a_conversation_that_raises_still_ends_the_wake_asleep(robot_parts, monkeypatch, caplog) -> None:
    async def broken(*_wake: Any) -> str:
        raise RuntimeError('a bug in the conversation')

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        monkeypatch.setattr(robot, '_converse', broken)
        await asyncio.wait_for(robot._cycle(), 5.0)
        assert [e['kind'] for e in robot_parts.events] == ['session_start', 'session_end']
        assert robot_parts.events[-1]['reason'] == 'error'
        assert robot._motion.mixer.mode == 'sleep' and robot._beat()['awake'] is False

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert 'the conversation failed' in caplog.text


def test_a_sleep_move_that_fails_is_logged_and_the_wake_still_ends(robot_parts, monkeypatch, caplog) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, TimeoutError('Mac down'))
        set_mode = robot._motion.set_mode

        def no_sleep(mode: str, blend_s: float = 0.5) -> None:
            if mode == 'sleep':
                raise RuntimeError('the daemon is gone')
            set_mode(mode, blend_s)

        monkeypatch.setattr(robot._motion, 'set_mode', no_sleep)
        await asyncio.wait_for(robot._cycle(), 5.0)
        assert [e['kind'] for e in robot_parts.events] == ['session_start', 'session_end']
        assert robot._beat()['awake'] is False  # a next wake is not ignored as 'already awake'

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert 'the sleep move failed' in caplog.text


def _newer_tuning() -> dict[str, Any]:
    view = settings_view()
    for tuning in view['settings']['tuning'].values():
        tuning['curve'] = 'ease'  # a key this robot's animations.Tuning does not know (a newer Mac)
    return view


def _no_prompt() -> dict[str, Any]:
    view = settings_view()
    del view['system_prompt']
    return view


@pytest.mark.parametrize('fresh', [_newer_tuning, _no_prompt], ids=['a newer tuning', 'no system prompt'])
def test_settings_that_cannot_be_applied_close_the_connection_opened_early(robot_parts, fresh) -> None:
    """A wake on cached settings connects at once; this wake's settings cannot be applied: back to sleep, confused,
    and the session opened on the last wake's settings ends with the wake, its socket closed."""

    async def scenario() -> None:
        robot = _robot(robot_parts, fresh())
        robot._settings = settings_view()  # the last wake's: the connect starts with them
        await asyncio.wait_for(robot._cycle(), 3.0)
        await asyncio.sleep(0.1)
        assert not [task for task in asyncio.all_tasks() if task.get_name() == 'session']
        assert [connection.closed for connection in robot_parts.client.connections] == [True]
        assert [sound for sound in robot_parts.sounds if sound != 'stop'] == ['wake_up.wav', 'confused1.wav']
        assert robot_parts.events[-1]['reason'] == 'error'
        assert robot._settings == settings_view()  # the next wake connects early on settings that worked

    asyncio.run(scenario())


def test_a_mac_down_at_a_wake_on_cached_settings_closes_the_connection_opened_early(robot_parts) -> None:
    """A wake after a good one, the Mac asleep: the connection opened on the last wake's settings is up when the
    fetch fails. Back to sleep, confused; that connection is closed without ever being served."""

    def mac_down() -> dict:
        deadline = time.monotonic() + 2.0  # the fetch's timeout: the early connect is through by then
        while not robot_parts.client.connections and time.monotonic() < deadline:
            time.sleep(0.005)
        raise TimeoutError('Mac down')

    async def scenario() -> None:
        robot = _robot(robot_parts, mac_down)
        robot._settings = settings_view(voice='Achird')
        await asyncio.wait_for(robot._cycle(), 3.0)
        assert [connection.closed for connection in robot_parts.client.connections] == [True]
        phases = [event['phase'] for event in robot_parts.events if event['kind'] == 'phase']
        assert phases == ['connecting', 'closing']  # never listening: no word reached that connection
        assert [sound for sound in robot_parts.sounds if sound != 'stop'] == ['wake_up.wav', 'confused1.wav']
        end = robot_parts.events[-1]
        assert (end['kind'], end['reason']) == ('session_end', 'error')
        assert robot._settings == settings_view(voice='Achird')
        assert robot._motion.mixer.mode == 'sleep' and robot._beat()['awake'] is False

    asyncio.run(scenario())


def test_a_wake_on_cached_settings_reopens_with_the_fresh_ones(robot_parts: SimpleNamespace) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view(voice='Kore', talking_animation='nod'))
        robot._settings = settings_view(voice='Achird')
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: len(robot_parts.client.connections) == 2)
        voices = [c.speech_config.voice_config.prebuilt_voice_config.voice_name for c in robot_parts.client.attempts]
        assert voices == ['Achird', 'Kore']
        assert robot._motion.mixer.variants['talking'] == 'nod'
        assert robot._beat()['awake'] is True
        robot._command({'action': 'wake'})  # already awake: nothing
        robot._command({'action': 'say', 'text': 'Bonjour'})
        await until(lambda: robot_parts.client.live.turns)
        robot._command({'action': 'sleep'})
        await asyncio.wait_for(cycle, 3.0)
        start, end = robot_parts.events[0], robot_parts.events[-1]
        assert (start['kind'], end['kind'], end['reason']) == ('session_start', 'session_end', 'dashboard')
        assert not robot._wake.is_set()

    asyncio.run(scenario())


def test_a_connection_on_the_last_wakes_settings_is_not_served_before_this_wakes(robot_parts) -> None:
    answered = threading.Event()

    def slow_fetch() -> dict:
        answered.wait(2.0)
        return settings_view(voice='Kore')

    async def scenario() -> None:
        robot = _robot(robot_parts, slow_fetch)
        robot._settings = settings_view(voice='Achird')  # the last wake's: the connect starts with them
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot_parts.client.connections)
        stale = robot_parts.client.live
        robot._command({'action': 'say', 'text': 'Bonjour Reachy'})  # the first request, while the Mac is slow
        await asyncio.sleep(0.2)
        assert robot._session.state is SessionState.CONNECTING and stale.receives == 0 and not stale.turns
        answered.set()
        await until(lambda: len(robot_parts.client.connections) == 2 and robot_parts.client.live.turns)
        assert stale.closed and stale.receives == 0 and not stale.turns  # it went to this wake's connection
        robot._command({'action': 'sleep'})
        await asyncio.wait_for(cycle, 3.0)

    asyncio.run(scenario())


def test_a_wake_asked_for_during_the_last_sleep_move_still_masks_its_own_move(robot_parts, monkeypatch) -> None:
    """« Relancer la conversation »: the wake comes while the robot is still falling asleep; its own wake move
    starts with the cycle, seconds later, and the comfort noise covers it from there."""
    audible: list[bool] = []
    process = ComfortNoise.process

    def recording(self: ComfortNoise, chunk: np.ndarray, *, robot_audible: bool) -> np.ndarray:
        audible.append(robot_audible)
        return process(self, chunk, robot_audible=robot_audible)

    monkeypatch.setattr(ComfortNoise, 'process', recording)

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        robot._audio = FakeAudio(lambda: np.zeros(256, dtype=np.float32))  # a live microphone
        robot._woke_at = time.monotonic() - 2.0  # asked for during the previous sleep move
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: len(audible) >= 5)
        robot._command({'action': 'sleep'})
        await asyncio.wait_for(cycle, 3.0)
        assert audible[:5] == [True] * 5

    asyncio.run(scenario())


def test_a_sleep_asked_while_the_settings_are_on_their_way_is_kept(robot_parts: SimpleNamespace) -> None:
    answered = threading.Event()

    def slow_fetch() -> dict:
        answered.wait(2.0)
        return settings_view()

    async def scenario() -> None:
        robot = _robot(robot_parts, slow_fetch)
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot._beat()['awake'])
        robot._command({'action': 'say', 'text': 'Bonjour ?'})  # held for a session that will not open
        robot._command({'action': 'sleep'})  # no conversation yet: the wake itself is called off
        answered.set()
        await asyncio.wait_for(cycle, 3.0)
        assert robot_parts.client.attempts == [] and robot._says == []
        assert robot_parts.events[-1] == {
            'kind': 'session_end',
            'session_id': robot_parts.events[0]['session_id'],
            'reason': 'dashboard',
        }

    asyncio.run(scenario())


def test_a_say_typed_while_the_settings_are_on_their_way_is_said_once_connected(robot_parts) -> None:
    answered = threading.Event()

    def slow_fetch() -> dict:
        answered.wait(2.0)
        return settings_view()

    async def scenario() -> None:
        robot = _robot(robot_parts, slow_fetch)
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot._beat()['awake'])  # from here on the Mac accepts a say
        robot._command({'action': 'say', 'text': 'Bonjour Reachy'})
        answered.set()
        await until(lambda: robot_parts.client.connections and robot_parts.client.live.turns)
        assert robot_parts.client.live.turns[0][0].parts[0].text == 'Bonjour Reachy'
        typed = [e for e in robot_parts.events if e['kind'] == 'user_end']
        assert [(e['text'], e['typed']) for e in typed] == [('Bonjour Reachy', True)]
        robot._command({'action': 'sleep'})
        await asyncio.wait_for(cycle, 3.0)

    asyncio.run(scenario())


class Looker:
    """The gaze's thread, as the robot's life sees it: the setting it is given, whether it would look now."""

    def __init__(self, camera: Any, body: Any, wanted: Any) -> None:
        self.wanted, self.enabled, self.closed = wanted, [], False

    def enable(self, on: bool) -> None:
        self.enabled.append(on)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize('setting', [True, False])
def test_the_gaze_takes_the_setting_at_each_wake_and_looks_only_in_a_conversation(
    robot_parts: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, setting: bool
) -> None:
    monkeypatch.setattr(main.gaze, 'Tracker', Looker)
    monkeypatch.setattr(main, 'reach', lambda: None)

    async def scenario() -> None:
        assert _robot(robot_parts, settings_view())._gaze is None  # no camera: no gaze, and nothing else changes
        robot = _robot(robot_parts, settings_view(gaze_tracking=setting), camera=FakeCamera())
        looker = robot._gaze
        assert not looker.wanted()  # asleep
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        assert looker.enabled == [setting] and looker.wanted()
        robot._performer.busy = True  # a scene: its staging turns the robot, the gaze keeps out of it
        assert not looker.wanted()
        robot._performer.busy = False
        robot._shutdown()
        await asyncio.wait_for(cycle, 3.0)
        assert not looker.wanted()

    asyncio.run(scenario())


def test_a_wake_command_while_asleep_wakes_and_shutdown_ends_the_conversation(robot_parts: SimpleNamespace) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        robot._command({'action': 'say', 'text': 'Personne ?'})  # asleep: dropped
        robot._command({'action': 'sleep'})  # asleep: nothing to end
        robot._command({'action': 'wake'})
        assert robot._wake.is_set() and robot._woke_at is not None
        robot._wake.clear()
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        robot._shutdown()
        await asyncio.wait_for(cycle, 3.0)
        assert robot_parts.events[-1]['reason'] == 'shutdown'
        with pytest.raises(KeyboardInterrupt):
            robot._shutdown()  # the second Ctrl-C does not wait

    asyncio.run(scenario())


def test_a_lost_connection_that_cannot_come_back_ends_in_the_sleep_pose(robot_parts: SimpleNamespace) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        robot_parts.client.fail = [ConnectionError('no route')] * 4
        await asyncio.wait_for(robot._cycle(), 3.0)
        assert len(robot_parts.client.attempts) == 4  # the first connect, then the 3 of the budget
        assert robot_parts.events[-1]['reason'] == 'connection_lost'
        assert robot._motion.mixer.mode == 'sleep' and not robot._beat()['awake']

    asyncio.run(scenario())


def test_a_wake_gemini_refuses_ends_confused_and_says_gemini_refused(robot_parts: SimpleNamespace) -> None:
    """Credits depleted: every connect closed by Gemini. The robot does not wake silently into a « lost connection »:
    it sounds confused, as when its settings are missing, and the dashboard reads that Gemini refused."""
    depleted = genai_errors.APIError(1011, {'message': 'Your prepayment credits are depleted.'})

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        robot_parts.client.refuse = lambda _config: depleted
        await asyncio.wait_for(robot._cycle(), 3.0)
        assert [sound for sound in robot_parts.sounds if sound != 'stop'] == ['wake_up.wav', 'confused1.wav']
        end = contracts.SessionEnd.model_validate(robot_parts.events[-1])
        assert end.reason == 'refused'
        assert robot._motion.mixer.mode == 'sleep' and not robot._beat()['awake']

    asyncio.run(scenario())


@pytest.mark.parametrize('cause', ['gemini refuses', 'the mac is down'])
def test_the_confused_sound_is_never_cut_by_the_sessions_own_sound_stop(robot_parts, monkeypatch, cause: str) -> None:
    """A session ends by stopping whatever sound plays, and the daemon may act on that stop late. The « confused »
    played after it, the human's only sign at the robot that it cannot talk, is never cut by it."""

    def slow_stop() -> None:
        time.sleep(0.1)  # the daemon's HTTP, slower than the play that follows
        robot_parts.sounds.append('stop')

    monkeypatch.setattr(speaker, 'stop', slow_stop)

    async def scenario() -> None:
        if cause == 'gemini refuses':
            robot = _robot(robot_parts, settings_view())
            depleted = genai_errors.APIError(1011, {'message': 'Your prepayment credits are depleted.'})
            robot_parts.client.refuse = lambda _config: depleted
        else:
            robot = _robot(robot_parts, TimeoutError('Mac down'))
            robot._settings = settings_view()  # the last wake's: a session opens on them at once
        await asyncio.wait_for(robot._cycle(), 3.0)
        await asyncio.sleep(0.2)  # every stop sent has landed by now
        sounds = robot_parts.sounds
        assert 'confused1.wav' in sounds and 'stop' not in sounds[sounds.index('confused1.wav') :], sounds

    asyncio.run(scenario())


class FakeMacServer:
    """Answers the Mac link's requests; `commands` is what the long-poll hands out, one per poll. Each event takes
    `event_s` to reach it (a POST over the Wi-Fi is no instant)."""

    def __init__(self, view: dict[str, Any], event_s: float = 0.0) -> None:
        self.view, self.event_s = view, event_s
        self.commands: queue.Queue[dict[str, Any]] = queue.Queue()
        self.events: list[dict[str, Any]] = []
        self.beats: list[dict[str, Any]] = []

    def send(self, method: str, path: str, **kwargs: Any) -> Any:
        body: Any = {}
        if path == '/api/settings':
            body = self.view
        elif path == '/api/robot/events':
            time.sleep(self.event_s)
            self.events.append(kwargs['json'])
        elif path == '/api/robot/heartbeat':
            self.beats.append(kwargs['json'])
        elif path == '/api/robot/commands':
            try:
                body = {'command': self.commands.get(timeout=0.05)}
            except queue.Empty:
                body = {'command': None}
        return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)


def _delivered(mac: Any) -> bool:
    """Whether every event the robot emitted has reached the Mac: its one reporter thread sends them in order, one at a
    time, after `emit` has returned. Read the queue before the send in flight: an event is never out of both."""
    return not mac._events and not mac._sending


def test_a_whole_run_from_boot_to_shutdown(robot_parts: SimpleNamespace) -> None:
    mac = FakeMacServer(settings_view())

    async def scenario() -> None:
        robot = Robot(robot_parts.mini, robot_parts.client, camera=None)
        robot._mac._send = mac.send
        task = asyncio.create_task(robot.run())
        await until(lambda: robot._antennas._armed and robot._antennas._baseline is not None)
        assert robot._motion.mixer.mode == 'sleep'
        robot_parts.mini.antennas[1] += 0.2  # a hand flicks the left antenna
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        assert mac.events[0]['kind'] == 'session_start'
        mac.commands.put({'id': 'c1', 'action': 'sleep', 'queued_at': time.time()})
        await until(lambda: robot._session_id is None and robot._antennas._armed, timeout=5.0)
        await until(lambda: robot._emotions.names() == ['cheerful1', 'dance1'])
        robot._shutdown()
        await asyncio.wait_for(task, 5.0)
        assert mac.events[-1]['kind'] == 'session_end' and mac.events[-1]['reason'] == 'dashboard'
        boot = {'awake': False, 'session_id': None, 'antenna_wake': False, 'scene': None}  # the sleep pose not settled
        assert mac.beats[0] == boot  # the pulse starts at boot
        events = TypeAdapter(contracts.RobotEvent)
        for event in mac.events:
            events.validate_python(event)
        for beat in mac.beats:
            contracts.Heartbeat.model_validate(beat)
        assert np.allclose(robot_parts.mini.heads[-1], SLEEP_HEAD, atol=1e-2)

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_a_dropped_ssh_session_ends_like_ctrl_c(robot_parts: SimpleNamespace) -> None:
    """SIGHUP (`make run`'s SSH session lost): session_end, then the sleep pose."""
    mac = FakeMacServer(settings_view())
    unhandled: list[int] = []
    previous = signal.signal(signal.SIGHUP, lambda sig, _frame: unhandled.append(sig))  # if the robot ignores it

    async def scenario() -> None:
        robot = Robot(robot_parts.mini, robot_parts.client, camera=None)
        robot._mac._send = mac.send
        task = asyncio.create_task(robot.run())
        await until(lambda: robot._antennas._armed)
        robot._trigger('dashboard')
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.wait_for(task, 5.0)
        assert mac.events[-1]['kind'] == 'session_end' and mac.events[-1]['reason'] == 'shutdown'
        assert np.allclose(robot_parts.mini.heads[-1], SLEEP_HEAD, atol=1e-2)

    try:
        asyncio.run(asyncio.wait_for(scenario(), 15.0))
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert unhandled == []


def test_main_refuses_to_start_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'GEMINI_API_KEY', '')
    with pytest.raises(SystemExit, match='GEMINI_API_KEY'):
        asyncio.run(main.main_async())


def test_the_env_file_is_read_without_overriding_the_environment(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / '.env'
    path.write_text(
        '# a comment\nexport ROBOT_T_A=plain # the Mac\nROBOT_T_B="quoted # kept"\nROBOT_T_C=\'single\'\n'
        'ROBOT_T_D=set by the shell\nnot a line\n',
        encoding='utf-8',
    )
    for name in ('ROBOT_T_A', 'ROBOT_T_B', 'ROBOT_T_C'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('ROBOT_T_D', 'shell')
    boot.load_env(path)
    assert [os.environ[n] for n in ('ROBOT_T_A', 'ROBOT_T_B', 'ROBOT_T_C', 'ROBOT_T_D')] == [
        'plain',
        'quoted # kept',
        'single',
        'shell',
    ]
    boot.load_env(tmp_path / 'missing')  # no file: nothing to do


def test_the_robot_reads_the_env_file_as_the_mac_does(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same .env, rsynced to the robot, gives it the values the Mac's python-dotenv gives the Mac: a quoted value
    followed by a comment loses its quotes and its comment, as a bare one loses its comment."""
    path = tmp_path / '.env'
    path.write_text(
        'ROBOT_T_A=plain # the Mac\nROBOT_T_B="quoted # kept"\nROBOT_T_C=\'single\'\n'
        'ROBOT_T_E="http://192.168.1.20:7777" # the Mac, fixed IP\nROBOT_T_F=\'k3y\'  # shared\n'
        'ROBOT_T_G = 7777\nROBOT_T_H="it\'s"  # a quote inside\nROBOT_T_I=\'a "b" c\' # the other one\n',
        encoding='utf-8',
    )
    expected = dotenv_values(path)
    for name in expected:
        monkeypatch.delenv(name, raising=False)
    boot.load_env(path)
    assert {name: os.environ[name] for name in expected} == expected
    assert expected['ROBOT_T_E'] == 'http://192.168.1.20:7777'


@pytest.mark.parametrize(
    ('status', 'released'), [({'released': True}, True), ({'released': False}, False), (None, False)]
)
def test_the_daemon_is_asked_whether_it_lent_the_camera(monkeypatch: pytest.MonkeyPatch, status, released) -> None:
    def get(url: str, timeout: float) -> Any:
        assert url == 'http://localhost:8000/api/media/status'
        if status is None:
            raise requests.ConnectionError('daemon down')
        return SimpleNamespace(json=lambda: status)

    monkeypatch.setattr(adapters_robot.requests, 'get', get)
    assert adapters_robot.camera_released() is released


def test_a_lent_camera_is_said_at_boot_and_there_is_no_look(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    mini = SimpleNamespace(media=SimpleNamespace(camera=object()))
    monkeypatch.setattr(main, 'camera_released', lambda: True)
    with caplog.at_level('WARNING'):
        assert asyncio.run(main._camera(mini)) is None
    assert 'Restart the daemon' in caplog.text and 'with ReachyMini(...)' in caplog.text
    monkeypatch.setattr(main, 'camera_released', lambda: False)
    assert asyncio.run(main._camera(mini)) is not None


def test_a_try_while_asleep_plays_through_the_motion_thread_and_does_not_wake_the_robot(
    robot_parts: SimpleNamespace,
) -> None:
    """The studio's « Essayer »: the move plays from the sleep pose, the antennas stop listening meanwhile (the
    move swings them), and they listen again once the robot is back asleep and settled."""

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        played: list[Any] = []
        robot._motion.play_move = played.append
        back_asleep = asyncio.Event()

        async def settled_within(timeout: float) -> bool:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(back_asleep.wait(), 2.0)
            return back_asleep.is_set()

        robot._motion.settled_within = settled_within
        robot._antennas.arm()
        robot._command({'action': 'try', 'move': {'kind': 'gesture', 'name': 'dance'}})
        await until(lambda: played)
        assert played == [main.animations.GESTURES['dance']]
        assert not robot._antennas._armed  # the dance must not read as a wiggle
        assert robot._beat()['antenna_wake'] is False  # nor the dashboard say it would
        back_asleep.set()
        await until(lambda: robot._antennas._armed)
        assert robot._beat()['awake'] is False and not robot._wake.is_set()
        robot._command({'action': 'try', 'move': {'kind': 'gesture', 'name': 'moonwalk'}})
        await asyncio.sleep(0.05)
        assert len(played) == 1  # an unknown move plays nothing

    asyncio.run(scenario())


def test_a_try_builds_each_kind_of_move() -> None:
    animation = studio.build(
        {'kind': 'animation', 'mode': 'idle', 'name': 'observer', 'tuning': {'amplitude': 1.2, 'speed': 0.8}}, None
    )
    assert animation is not None and animation.duration == 6.0
    expected = main.animations.animate('idle', 'observer', main.animations.Tuning(1.2, 0.8))(2.0)
    assert np.allclose(animation.evaluate(2.0)[0], expected[0])
    emotions = SimpleNamespace(get=lambda name: f'move:{name}' if name == 'cheerful1' else None)
    assert studio.build({'kind': 'emotion', 'name': 'cheerful1'}, emotions) == 'move:cheerful1'
    assert studio.build({'kind': 'animation', 'mode': 'talking', 'name': 'observer'}, None) is None
    assert studio.build({'kind': 'animation', 'mode': 'idle', 'name': 'calm', 'seconds': 'x'}, None) is None


LONG_TRY = {'kind': 'animation', 'mode': 'idle', 'name': 'observer', 'seconds': 4.2}  # outlasts a 1 s move's wait


def at_rest_and_listening(robot: Robot) -> bool:
    return robot._antennas._armed and robot._motion.mixer.mode == 'sleep' and robot._motion.settled()


def test_two_tries_asleep_in_a_row_never_wake_the_robot(robot_parts: SimpleNamespace, monkeypatch) -> None:
    """« Essayer » twice: the end of the first try must not let the antennas listen while the second moves them."""
    monkeypatch.setattr(config, 'WAKE_S', 0.3)
    robot_parts.mini.follow = True

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        robot._motion.start()
        robot._antennas.start()
        try:
            robot._antennas.arm()
            robot._command({'action': 'try', 'move': {'kind': 'gesture', 'name': 'nod'}})
            await asyncio.sleep(0.2)
            robot._command({'action': 'try', 'move': LONG_TRY})
            await until(lambda: isinstance(robot._motion.current_move(), studio.AnimationMove))
            await until(lambda: robot._wake.is_set() or at_rest_and_listening(robot), timeout=8.0)
            await asyncio.sleep(0.2)  # time for the antenna thread to read a wiggle
            assert not robot._wake.is_set() and robot._antennas._armed
        finally:
            robot._antennas.stop()
            robot._motion.stop()

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_tries_asleep_hold_no_thread_while_the_robot_comes_to_rest(robot_parts: SimpleNamespace) -> None:
    """« Essayer » pressed again while the last try plays: waiting for the robot to rest takes no thread from the
    pool that builds the next try and reads a wake's settings (8 threads on the robot's 4 cores, 2 here)."""

    async def scenario() -> None:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(2))
        robot = _robot(robot_parts, settings_view())
        robot._antennas.arm()

        async def try_at(speed: float, timeout: float = 3.0) -> None:
            robot._command({'action': 'try', 'move': {**LONG_TRY, 'seconds': 6.0, 'tuning': {'speed': speed}}})
            tuning = main.animations.Tuning(1.0, speed)
            await until(lambda: getattr(robot._motion.current_move(), 'tuning', None) == tuning, timeout)

        for speed in (1.0, 1.1):  # as many tries still playing as the pool has threads
            await try_at(speed)
        asked = time.monotonic()
        await try_at(1.2, timeout=12.0)
        assert time.monotonic() - asked < 0.5
        asked = time.monotonic()
        await asyncio.to_thread(lambda: None)  # what a wake's settings fetch waits for
        assert time.monotonic() - asked < 0.5

    asyncio.run(asyncio.wait_for(scenario(), 30.0))


def test_a_try_during_a_scene_asleep_leaves_the_antennas_deaf(robot_parts: SimpleNamespace) -> None:
    """The scene still moves the robot after the try: its own end arms the antennas."""

    async def at_rest(timeout: float) -> bool:
        return True

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        robot._motion.set_mode('talking')  # a scene plays asleep, the antennas disarmed
        robot._motion.play_move = lambda move: None
        robot._motion.settled_within = at_rest  # the try's gesture is over
        await robot._try({'kind': 'gesture', 'name': 'nod'})
        assert not robot._antennas._armed

    asyncio.run(asyncio.wait_for(scenario(), 5.0))


def test_a_try_as_the_conversation_ends_never_wakes_the_robot(robot_parts: SimpleNamespace, monkeypatch) -> None:
    """« Essayer » while the robot falls asleep: the sleep move giving up on its wait must not let the antennas
    listen while the try moves them."""
    monkeypatch.setattr(config, 'SLEEP_S', 1.0)
    monkeypatch.setattr(config, 'WAKE_S', 0.3)
    robot_parts.mini.follow = True
    mac = FakeMacServer(settings_view())

    async def scenario() -> None:
        robot = Robot(robot_parts.mini, robot_parts.client, camera=None)
        robot._mac._send = mac.send
        task = asyncio.create_task(robot.run())
        await until(lambda: robot._antennas._armed, timeout=5.0)
        robot._trigger('dashboard')
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        robot._command({'action': 'sleep'})
        await until(lambda: robot._session_id is None)
        robot._command({'action': 'try', 'move': LONG_TRY})
        await until(lambda: isinstance(robot._motion.current_move(), studio.AnimationMove))
        await until(lambda: robot._session_id is not None or at_rest_and_listening(robot), timeout=8.0)
        await asyncio.sleep(0.2)
        assert robot._session_id is None and [e['kind'] for e in mac.events].count('session_start') == 1
        robot._shutdown()
        await asyncio.wait_for(task, 5.0)

    asyncio.run(asyncio.wait_for(scenario(), 20.0))


def test_ctrl_c_during_a_try_asleep_ends_in_the_sleep_pose(robot_parts: SimpleNamespace) -> None:
    robot_parts.mini.follow = True
    mac = FakeMacServer(settings_view())

    async def scenario() -> None:
        robot = Robot(robot_parts.mini, robot_parts.client, camera=None)
        robot._mac._send = mac.send
        task = asyncio.create_task(robot.run())
        await until(lambda: robot._antennas._armed)
        robot._command({'action': 'try', 'move': {'kind': 'gesture', 'name': 'dance'}})
        await asyncio.sleep(1.5)
        robot._shutdown()  # Ctrl-C, or `make run`'s SSH session dropped, mid-dance
        await asyncio.wait_for(task, 8.0)
        assert np.allclose(robot_parts.mini.heads[-1], SLEEP_HEAD) and np.allclose(
            robot_parts.mini.antennas, SLEEP_ANTENNAS
        )

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_sleep_takes_the_body_back_from_an_app_before_the_sleep_move(robot_parts: SimpleNamespace) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        order: list[str] = []
        robot_parts.mini.disable_motors = lambda ids: order.append(f'free {ids}')
        robot_parts.mini.enable_motors = lambda ids=None: order.append('take')
        mini = robot_parts.mini
        mini.get_current_head_pose = lambda: mini.heads[-1] if mini.heads else SLEEP_HEAD  # it goes where it is sent
        robot._motion.start(None)
        robot._motion.set_mode('idle', blend_s=0.01)
        robot._session = SimpleNamespace(session_id='s1')
        robot._session_id = 's1'
        robot._command({'action': 'manette', 'manette': 'volant', 'grant': True})
        await until(lambda: any(e.get('state') == 'on' for e in robot_parts.events))
        robot._command({'action': 'app_motion', 'motion': {'kind': 'gesture', 'name': 'nod'}})  # ignored: it is held
        await robot._fall_asleep()
        assert order == ["free ['body_rotation']", 'take'] and not robot._manette.on
        assert robot._motion.mixer.mode == 'sleep' and not robot._motion.frozen
        assert [e['state'] for e in robot_parts.events if e['kind'] == 'manette'] == ['on', 'off']
        robot._motion.stop()

    asyncio.run(scenario())


def test_an_app_replays_what_the_hand_did_once_the_body_is_the_robots_again(robot_parts: SimpleNamespace) -> None:
    def replays() -> list[dict]:
        return [e for e in robot_parts.events if e['kind'] == 'replay']

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        mini = robot_parts.mini
        mini.disable_motors = lambda ids: None
        mini.enable_motors = lambda ids=None: None
        mini.get_current_head_pose = lambda: mini.heads[-1] if mini.heads else SLEEP_HEAD  # it goes where it is sent
        robot._motion.start(None)
        robot._motion.set_mode('idle', blend_s=0.01)
        robot._session = SimpleNamespace(session_id='s1')
        robot._session_id = 's1'
        robot._command({'action': 'manette', 'manette': 'gachettes', 'grant': True})
        await until(lambda: any(e.get('state') == 'on' for e in robot_parts.events), timeout=5.0)
        began = time.time()
        await asyncio.sleep(0.6)  # the hold: the tape runs
        ended = time.time()
        ask = {'kind': 'replay', 'start': began * 1000, 'end': ended * 1000, 'loops': 2}
        robot._command({'action': 'app_motion', 'motion': ask})  # asked while the app still holds the body
        await asyncio.sleep(0.3)
        assert replays() == [] and robot._manette.lent  # it waits for the body
        robot._command({'action': 'manette', 'manette': 'off'})
        await until(lambda: [e['state'] for e in replays()] == ['started'], timeout=8.0)
        started = replays()[0]
        assert 0.4 <= started['seconds'] <= 0.6 and started['loops'] == 2
        assert isinstance(robot._motion.current_move(), take.Take)
        await until(lambda: [e['state'] for e in replays()] == ['started', 'ended'], timeout=8.0)
        robot._command({'action': 'app_motion', 'motion': {**ask, 'start': 0, 'end': 1000}})  # nothing taped then
        await until(lambda: [e['state'] for e in replays()][2:] == ['ended'], timeout=3.0)
        robot._command({'action': 'app_motion', 'motion': ask})
        await until(lambda: [e['state'] for e in replays()][3:] == ['started'], timeout=8.0)
        robot._command({'action': 'app_motion', 'motion': {'kind': 'replay', 'loops': 0}})  # the app stops it
        await until(lambda: [e['state'] for e in replays()][3:] == ['started', 'ended'], timeout=3.0)
        robot._motion.stop()

    asyncio.run(asyncio.wait_for(scenario(), 40.0))


def test_an_ear_lent_alone_leaves_the_body_to_the_app_and_mutes_the_conversation(robot_parts: SimpleNamespace) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        freed: list[Any] = []
        robot_parts.mini.disable_motors = freed.append
        robot._motion.start(None)
        robot._motion.set_mode('idle', blend_s=0.01)
        robot._session = SimpleNamespace(session_id='s1')
        robot._session_id = 's1'
        robot._command({'action': 'manette', 'manette': 'voix', 'grant': True})
        await until(lambda: any(e.get('state') == 'on' for e in robot_parts.events))
        assert robot._manette.on and robot._ear.lent and robot._lent() is None and not robot._motion.frozen
        robot._command({'action': 'app_motion', 'motion': {'kind': 'look', 'yaw': 20, 'pitch': 0}})
        await until(lambda: isinstance(robot._motion.current_move(), apps.Hold))  # the app's look plays
        robot._command({'action': 'manette', 'manette': 'off'})
        await until(lambda: not robot._manette.on)
        assert not robot._ear.lent and freed == []
        robot._motion.stop()

    asyncio.run(asyncio.wait_for(scenario(), 10.0))


@pytest.mark.parametrize('hz', [config.MOTION_HZ, 5], ids=['on time', 'a starved motion thread'])
def test_a_body_taken_back_only_after_the_conversation_still_ends_asleep_and_rearms(
    robot_parts, monkeypatch, hz: int
) -> None:
    """The torque refused at both exits of the conversation's end: the robot stays held, deaf to a wake, until the
    lease watcher takes it back; then into the sleep pose, and only then the wake antennas."""
    monkeypatch.setattr(config, 'MOTION_HZ', hz)
    for name, value in (('SETTLE_S', 0.05), ('TAKEN_BACK_S', 0.01), ('STILL_S', 0.05), ('RETURN_POLL_S', 0.01)):
        monkeypatch.setattr(manette, name, value)
    monkeypatch.setattr(manette, 'LEASE_S', 0.5)
    monkeypatch.setattr(manette, 'WATCH_S', 0.02)
    monkeypatch.setattr(config, 'WAKE_S', 0.05)
    monkeypatch.setattr(config, 'SLEEP_SETTLE_S', 0.05)
    mac, mini = FakeMacServer(settings_view(), event_s=0.01), robot_parts.mini
    mini.get_current_head_pose = lambda: mini.heads[-1] if mini.heads else SLEEP_HEAD  # it goes where it is sent
    mini.disable_motors = lambda ids: None
    refusals = [0]

    def enable(ids: list[str] | None = None) -> None:
        if refusals[0]:
            refusals[0] -= 1
            raise RuntimeError('daemon busy')

    mini.enable_motors = enable

    def command(action: str, **fields: Any) -> None:
        mac.commands.put({'id': action, 'action': action, 'queued_at': time.time(), **fields})

    async def scenario() -> None:
        robot = Robot(mini, robot_parts.client, camera=None)
        robot._mac._send = mac.send
        task = asyncio.create_task(robot.run())
        await until(lambda: robot._antennas._armed)
        robot._trigger('dashboard')
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        command('manette', manette='volant', grant=True)
        await until(lambda: robot._manette.on and robot._motion.frozen, timeout=5.0)
        command('manette', manette='volant')  # renewed by the dashboard
        refusals[0] = 2 * manette.TAKE_BACK_ATTEMPTS  # the conversation's end, then the sleep move
        command('sleep')
        await until(lambda: robot._session_id is None, timeout=5.0)
        await until(lambda: refusals[0] == 0, timeout=5.0)
        assert robot._manette.on and not robot._antennas._armed  # still held: no wake by antenna
        await until(lambda: not robot._manette.on, timeout=5.0)  # nobody renews: the lease watcher takes it back
        await until(lambda: _delivered(robot._mac))  # its off is emitted with it; the reporter thread sends it after
        kinds = [event['kind'] for event in mac.events]
        assert mac.events[-1]['kind'] == 'manette' and mac.events[-1]['state'] == 'off'  # said asleep,
        assert kinds.index('session_end') < len(kinds) - 1  # after the conversation's end, as the robot emitted them
        for event in mac.events:  # the Mac takes every one
            TypeAdapter(contracts.RobotEvent).validate_python(event)
        await until(lambda: robot._antennas._armed, timeout=5.0)
        assert robot._motion.mixer.mode == 'sleep' and robot._motion.settled()
        # Settled is a time: the frame that reaches the pose is written at the motion thread's next tick, maybe later.
        await until(lambda: np.allclose(mini.heads[-1], SLEEP_HEAD, atol=1e-2))
        robot._shutdown()
        await asyncio.wait_for(task, 8.0)

    asyncio.run(asyncio.wait_for(scenario(), 30.0))


def test_ctrl_c_while_an_app_holds_the_body_never_ends_the_agent_with_a_motor_freed(robot_parts, monkeypatch) -> None:
    """The torque refused at every exit before the agent's end (the conversation's, the sleep moves'): its last word
    takes every motor back, and the motion thread, frozen to the end, gives the freed base no target meanwhile."""
    for name, value in (('SETTLE_S', 0.05), ('TAKEN_BACK_S', 0.01), ('STILL_S', 0.05), ('RETURN_POLL_S', 0.01)):
        monkeypatch.setattr(manette, name, value)
    monkeypatch.setattr(config, 'WAKE_S', 0.05)
    mac, mini = FakeMacServer(settings_view()), robot_parts.mini
    mini.get_current_head_pose = lambda: mini.heads[-1] if mini.heads else SLEEP_HEAD  # it goes where it is sent
    freed: list[str] = []
    taken: list[list[str] | None] = []
    mini.disable_motors = freed.extend
    ending = threading.Event()  # set by Robot.run's last steps

    def enable(ids: list[str] | None = None) -> None:
        if not ending.is_set():
            raise RuntimeError('daemon busy')
        taken.append(ids)

    mini.enable_motors = enable

    async def scenario() -> None:
        robot = Robot(mini, robot_parts.client, camera=None)
        robot._mac._send = mac.send
        stop_antennas = robot._antennas.stop
        robot._antennas.stop = lambda: (ending.set(), stop_antennas())  # the first of run()'s last steps
        task = asyncio.create_task(robot.run())
        await until(lambda: robot._antennas._armed)
        robot._trigger('dashboard')
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        mac.commands.put(
            {'id': 'm1', 'action': 'manette', 'manette': 'volant', 'grant': True, 'queued_at': time.time()}
        )
        await until(lambda: robot._manette.on and robot._motion.frozen, timeout=5.0)
        written = len(mini.heads)
        robot._shutdown()
        await asyncio.wait_for(task, 20.0)
        assert freed == ['body_rotation']
        assert taken == [None]  # every motor, once, at the very end
        assert len(mini.heads) == written  # no head target since the base was freed, the last frame included

    asyncio.run(asyncio.wait_for(scenario(), 30.0))


@pytest.mark.parametrize('then', ['the conversation opens', 'a sleep was asked'])
def test_a_grant_while_the_first_wake_reads_its_settings_is_answered(robot_parts: SimpleNamespace, then: str) -> None:
    """The first wake opens the conversation once its settings came; the Mac, told of the wake, takes a grant
    meanwhile. The robot holds as asked, or refuses and says why once a sleep was asked before it opened."""
    answered = threading.Event()

    def slow_fetch() -> dict:
        answered.wait(2.0)
        return settings_view()

    def said() -> list[tuple[str, str | None]]:
        return [(e['state'], e.get('why')) for e in robot_parts.events if e['kind'] == 'manette']

    async def scenario() -> None:
        robot = _robot(robot_parts, slow_fetch)
        mini = robot_parts.mini
        mini.get_current_head_pose = lambda: mini.heads[-1] if mini.heads else SLEEP_HEAD  # it goes where it is sent
        robot._motion.start(None)
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot._beat()['awake'])
        if then == 'a sleep was asked':
            robot._command({'action': 'sleep'})
        robot._command({'action': 'manette', 'manette': 'gachettes', 'grant': True})  # « Autoriser »
        with contextlib.suppress(AssertionError):
            await until(said, timeout=3.0)
        assert said() == ([('on', None)] if then == 'the conversation opens' else [('refused', 'sleep')])
        answered.set()
        if then == 'the conversation opens':
            await until(lambda: robot._session is not None)
            robot._command({'action': 'manette', 'manette': 'gachettes'})  # the lease, renewed
            await until(lambda: len(said()) == 2)
            robot._command({'action': 'sleep'})
        await asyncio.wait_for(cycle, 5.0)
        assert said()[-1] == (('off', 'sleep') if then == 'the conversation opens' else ('refused', 'sleep'))
        assert not robot._manette.on and not robot._motion.frozen
        robot._motion.stop()

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_a_scene_keeps_the_body_from_an_app(robot_parts: SimpleNamespace) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        robot._session = SimpleNamespace(session_id='s1')
        robot._performer.busy = True
        robot._session_id = 's1'
        robot._command({'action': 'manette', 'manette': 'gachettes', 'grant': True})
        await asyncio.sleep(0.05)
        assert not robot._manette.on
        refused = {'kind': 'manette', 'session_id': 's1', 'state': 'refused', 'profile': None, 'why': 'scene'}
        assert robot_parts.events[-1] == refused

    asyncio.run(scenario())


def test_a_conversation_ending_keeps_the_body_from_an_app_and_says_so(robot_parts: SimpleNamespace) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        robot._session = SimpleNamespace(session_id='s1')
        robot._session_id = 's1'
        robot._stopping = True  # Ctrl-C: the conversation ends, no scene plays
        robot._command({'action': 'manette', 'manette': 'volant', 'grant': True})
        await asyncio.sleep(0.05)
        assert not robot._manette.on
        refused = {'kind': 'manette', 'session_id': 's1', 'state': 'refused', 'profile': None, 'why': 'sleep'}
        assert robot_parts.events[-1] == refused

    asyncio.run(scenario())


def test_an_app_holding_the_body_holds_the_idle_sleep_off(robot_parts: SimpleNamespace, monkeypatch) -> None:
    """The robot as an app's controller: nobody speaks for several idle sleeps while the dashboard renews the lease,
    and the conversation stays open; once the app lets go, the idle clock counts again."""
    for name, value in (('SETTLE_S', 0.05), ('TAKEN_BACK_S', 0.01), ('STILL_S', 0.05), ('RETURN_POLL_S', 0.01)):
        monkeypatch.setattr(manette, name, value)
    monkeypatch.setattr(config, 'WAKE_S', 0.05)
    idle_s, mini = 0.3, robot_parts.mini
    mini.get_current_head_pose = lambda: mini.heads[-1] if mini.heads else SLEEP_HEAD  # it goes where it is sent
    mini.disable_motors = lambda ids: None
    mini.enable_motors = lambda ids=None: None

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view(idle_sleep_minutes=idle_s / 60))
        robot._motion.start(None)
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        robot._command({'action': 'manette', 'manette': 'gachettes', 'grant': True})  # « Autoriser »
        await until(lambda: robot._manette.on)
        for _ in range(10):  # four idle sleeps of play, nobody speaking
            robot._command({'action': 'manette', 'manette': 'gachettes'})  # the lease, renewed
            await asyncio.sleep(idle_s * 0.4)
        assert not cycle.done() and robot._manette.on
        robot._command({'action': 'manette', 'manette': 'off'})
        await asyncio.wait_for(cycle, 5.0)
        ends = [e.get('state') or e['reason'] for e in robot_parts.events if e['kind'] in ('manette', 'session_end')]
        assert set(ends[:-2]) == {'on'} and ends[-2:] == ['off', 'idle']  # the idle sleep, as soon as it is let go
        robot._motion.stop()

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_an_app_moving_the_robot_holds_the_idle_sleep_off(robot_parts: SimpleNamespace, monkeypatch) -> None:
    """An app plays on the dashboard: the robot looks left and right ten times a second for four idle sleeps, nobody
    speaking (its moves bring the comfort noise: nobody could). The conversation stays open, the moves keep working,
    and the idle clock counts from the end of the last one."""
    monkeypatch.setattr(config, 'WAKE_S', 0.05)
    idle_s = 0.3

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view(idle_sleep_minutes=idle_s / 60))
        robot._motion.start(None)
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        for yaw in (10, -10) * 6:
            robot._command({'action': 'app_motion', 'motion': {'kind': 'look', 'yaw': yaw, 'pitch': 0}})
            await asyncio.sleep(0.1)
        assert not cycle.done() and isinstance(robot._motion.current_move(), apps.Hold)
        await asyncio.sleep(max(0.0, robot._motion.moving_until() - time.monotonic()) + idle_s / 2)
        assert not cycle.done()  # half an idle sleep after the last look let go
        await asyncio.wait_for(cycle, 5.0)
        assert [e['reason'] for e in robot_parts.events if e['kind'] == 'session_end'] == ['idle']
        robot._motion.stop()

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_a_build_asked_in_one_conversation_is_neither_waited_for_nor_told_in_the_next(robot_parts, monkeypatch) -> None:
    """« Fais-moi un Pong », then « Endormir » while the Mac builds it: the next wake is not held awake for it, and is
    not told of an app it did not ask for when it is ready."""
    monkeypatch.setattr(apps, 'BUILD_POLL_S', 0.02)
    idle_s, pong = 0.3, {'id': 'a1', 'title': 'Pong', 'status': 'building', 'error': None}

    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view(idle_sleep_minutes=idle_s / 60))
        robot._mac.request = lambda method, path, body=None: {'apps': [pong]}  # GET /api/apps
        robot._motion.start(None)
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        robot._builds.watch(pong)  # the app tool's answer: being built
        robot._command({'action': 'sleep'})
        await asyncio.wait_for(cycle, 5.0)
        assert not robot._builds.pending()  # the next conversation is not held awake by it
        cycle = asyncio.create_task(robot._cycle())  # the next wake, nobody speaking
        await until(lambda: robot._session is not None and robot._session.listening.is_set())
        pong['status'] = 'ready'
        await asyncio.wait_for(cycle, 5.0)
        assert robot_parts.events[-1]['reason'] == 'idle' and robot_parts.client.live.turns == []  # nothing told
        robot._motion.stop()

    asyncio.run(asyncio.wait_for(scenario(), 15.0))


def test_a_hushed_robot_hears_nobody_until_a_say_or_an_antenna_gives_its_speech_back(
    robot_parts: SimpleNamespace,
) -> None:
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        cycle = asyncio.create_task(robot._cycle())
        await until(lambda: robot._session is not None and robot._session.state is SessionState.IDLE)
        robot._command({'action': 'hush', 'hush': True})
        await until(lambda: robot._session._performing())  # muted as during a scene: comfort noise, no audio, no tool
        robot._command({'action': 'say', 'text': 'Reachy, tu es là ?'})
        await until(lambda: robot_parts.client.live.turns)
        assert not robot._session._performing()
        robot._command({'action': 'hush', 'hush': True})
        await until(lambda: robot._hush.on or robot._session.state is not SessionState.IDLE)
        robot._antenna_held()
        robot._command({'action': 'sleep'})
        await asyncio.wait_for(cycle, 3.0)
        hushes = [(e['on'], e.get('why')) for e in robot_parts.events if e['kind'] == 'hush']
        assert hushes[:2] == [(True, None), (False, 'say')]
        assert all(e['session_id'] == robot_parts.events[0]['session_id'] for e in robot_parts.events)

    asyncio.run(scenario())
