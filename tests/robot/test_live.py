"""The Live setup and the connect's fallbacks."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import time

import pytest
from google.genai import errors, types
from websockets import exceptions as ws
from websockets.datastructures import Headers
from websockets.frames import Close
from websockets.http11 import Response

from robot_agent import config, live
from robot_agent.emotions import CURATED
from robot_agent.tools import declarations
from tests.robot.conftest import FakeClient, settings_view, silent_peer

DECLS = declarations(emotions=['cheerful1'], camera=True)


@pytest.fixture(autouse=True)
def _search_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live.SEARCH, 'refused', False)


def _keys(value: object) -> set[str]:
    """Every key, at any depth, of a JSON value."""
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value))
    return set()


@pytest.mark.parametrize('search', [True, False])
def test_the_config_carries_v1s_turn_taking_and_a_bounded_context(search: bool) -> None:
    cfg = live.build_config(settings_view(voice='Kore'), DECLS, handle='h1', search=search)
    realtime = cfg.realtime_input_config
    vad = realtime.automatic_activity_detection
    assert not vad.disabled  # the server VAD decides the turns
    assert realtime.activity_handling in (None, types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS)  # barge-in
    assert (vad.silence_duration_ms, vad.prefix_padding_ms) == (800, 800)
    assert vad.start_of_speech_sensitivity is types.StartSensitivity.START_SENSITIVITY_HIGH
    assert vad.end_of_speech_sensitivity is types.EndSensitivity.END_SENSITIVITY_LOW
    compression = cfg.context_window_compression
    assert (compression.trigger_tokens, compression.sliding_window.target_tokens) == (20000, 8000)
    assert cfg.session_resumption.handle == 'h1'
    assert cfg.response_modalities == [types.Modality.AUDIO]
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == 'Kore'
    assert cfg.input_audio_transcription is not None and cfg.output_audio_transcription is not None
    assert cfg.thinking_config is None and cfg.temperature is None and cfg.history_config is None
    assert not [key for key in _keys(cfg.model_dump(mode='json', exclude_none=True)) if 'thinking' in key]
    assert all(d.behavior is types.Behavior.NON_BLOCKING for d in cfg.tools[0].function_declarations)


@pytest.mark.parametrize('search', [True, False])
def test_the_web_prompt_comes_only_with_the_search_tool(search: bool) -> None:
    cfg = live.build_config(settings_view(web_search=True), DECLS, handle=None, search=search)
    prompt = cfg.system_instruction.parts[0].text
    assert prompt.startswith('Tu es Reachy')
    assert ('Tu peux chercher sur le web.' in prompt) is search
    assert any(tool.google_search is not None for tool in cfg.tools) is search


def _open(client: FakeClient, view: dict, handle: str | None = None, **kwargs):
    async def scenario():
        async with contextlib.AsyncExitStack() as stack:
            return await live.open_session(stack, client, view, DECLS, handle, **kwargs)

    return asyncio.run(scenario())


def test_search_follows_the_setting() -> None:
    client = FakeClient()
    _open(client, settings_view(web_search=True))
    _open(client, settings_view(web_search=False))
    assert [c.searching for c in client.connections] == [True, False]


def test_a_refused_search_is_retried_without_it_and_stays_off(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient()
    exhausted = errors.APIError(1011, {'message': 'RESOURCE_EXHAUSTED', 'status': 'RESOURCE_EXHAUSTED'})
    client.refuse = lambda cfg: exhausted if any(t.google_search for t in cfg.tools) else None
    with caplog.at_level(logging.WARNING):
        _open(client, settings_view(web_search=True))
    assert [any(t.google_search for t in a.tools) for a in client.attempts] == [True, False]
    assert live.SEARCH.refused and '[SEARCH]' in caplog.text
    _open(client, settings_view(web_search=True))  # the latch: no second try with search
    assert len(client.attempts) == 3 and not client.connections[-1].searching


def _status(code: int) -> ws.InvalidStatus:
    return ws.InvalidStatus(Response(code, 'status', Headers()))


NETWORK_ERRORS = {
    'reset': ConnectionResetError('reset'),
    'timeout': TimeoutError('handshake timed out'),
    'dns': socket.gaierror(-3, 'Temporary failure in name resolution'),
    'no-close-frame': errors.APIError(1006, {'message': 'Abnormal closure.'}),
    'closed-unheard': ws.ConnectionClosedError(None, None),
    'dropped-before-101': ws.InvalidMessage('did not receive a valid HTTP response'),
    'http-503': _status(503),
}
REFUSALS = {
    'search-1011': errors.APIError(1011, {'message': 'RESOURCE_EXHAUSTED'}),
    'invalid-1007': errors.APIError(1007, {'message': 'Request contains an invalid argument.'}),
    'close-frame-1008': ws.ConnectionClosedError(Close(1008, 'session not found'), None),
    'http-403': _status(403),
    'anything-else': RuntimeError('refused'),
}


@pytest.mark.parametrize(
    ('error', 'network'),
    [(e, True) for e in NETWORK_ERRORS.values()] + [(e, False) for e in REFUSALS.values()],
    ids=[*NETWORK_ERRORS, *REFUSALS],
)
def test_a_network_failure_is_told_from_a_refusal(error: Exception, network: bool) -> None:
    assert live.network_error(error) is network


@pytest.mark.parametrize('handle', [None, 'h1'])
@pytest.mark.parametrize('error', NETWORK_ERRORS.values(), ids=list(NETWORK_ERRORS))
def test_a_network_error_is_neither_a_refused_search_nor_a_refused_handle(error: Exception, handle: str | None) -> None:
    """It goes to the reconnect budget, handle kept: never retried without search (which would turn search off
    for good) nor fresh (which loses the context)."""
    client = FakeClient()
    client.fail = [error]
    refused: list[str] = []
    with pytest.raises(type(error)):
        _open(client, settings_view(web_search=True), handle, on_refused=lambda: refused.append('handle'))
    assert not live.SEARCH.refused and refused == []
    assert [a.session_resumption.handle for a in client.attempts] == [handle]


def test_a_setup_that_never_completes_is_a_failed_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'CONNECT_TIMEOUT_S', 0.05)
    client = FakeClient()
    client.stall = True  # the server takes the socket and never answers the setup

    async def scenario() -> BaseException | None:
        async with contextlib.AsyncExitStack() as stack:
            view = settings_view(web_search=True)
            task = asyncio.ensure_future(live.open_session(stack, client, view, DECLS, 'h1'))
            done, _ = await asyncio.wait({task}, timeout=2.0)
            task.cancel()
            return task.exception() if done else None  # None: still connecting after 2 s

    assert isinstance(asyncio.run(scenario()), TimeoutError)
    assert len(client.attempts) == 1 and not live.SEARCH.refused


def test_a_connect_to_a_peer_gone_silent_after_the_upgrade_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real SDK: the connect given up at CONNECT_TIMEOUT_S still closes its socket, and that closing handshake
    gets no answer either: it is bounded by CLOSE_TIMEOUT_S."""
    monkeypatch.setattr(config, 'CONNECT_TIMEOUT_S', 0.3)
    monkeypatch.setattr(config, 'CLOSE_TIMEOUT_S', 0.3)

    async def scenario() -> float:
        async with silent_peer(monkeypatch) as (client, upgraded), contextlib.AsyncExitStack() as stack:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await live.open_session(stack, client, settings_view(), DECLS, None)
            assert len(upgraded) == 1
            return time.monotonic() - started

    took = asyncio.run(asyncio.wait_for(scenario(), 30.0))
    assert took < config.CONNECT_TIMEOUT_S + config.CLOSE_TIMEOUT_S + 0.5, f'the connect gave up after {took:.1f} s'


def test_when_the_retry_fails_too_the_first_error_is_raised() -> None:
    client = FakeClient()
    client.fail = [RuntimeError('first'), RuntimeError('second')]
    with pytest.raises(RuntimeError, match='first'):
        _open(client, settings_view(web_search=True))
    assert not live.SEARCH.refused


def test_a_refused_handle_falls_back_to_a_fresh_session_and_says_so() -> None:
    client = FakeClient()
    client.refuse = lambda cfg: REFUSALS['close-frame-1008'] if cfg.session_resumption.handle else None
    refused: list[str] = []
    _open(client, settings_view(), handle='stale', on_refused=lambda: refused.append('stale'))
    assert [a.session_resumption.handle for a in client.attempts] == ['stale', None] and refused == ['stale']


def test_what_a_connection_bakes_in() -> None:
    assert live.baked(settings_view()) == live.baked(settings_view(name='Robby', idle_animation='still'))
    assert live.baked(settings_view()) != live.baked(settings_view(voice='Kore'))
    assert live.baked(settings_view()) != live.baked(settings_view(web_search=True))


def test_prompt_and_tools_fit_the_budget() -> None:
    """The setup re-billed each turn: the declarations leave room for the Mac's prompt (10 000 chars in all)."""
    full = declarations(emotions=list(CURATED), camera=True)
    size = len(json.dumps([d.model_dump(mode='json', exclude_none=True) for d in full], ensure_ascii=False))
    assert size <= 4000, size
