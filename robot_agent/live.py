"""The Live connection: its setup (voice, prompt, VAD, compression, resumption, tools), the connect with its
two fallbacks (a refused handle, a refused Google Search), the `usage` event and a go_away's time left."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from google import genai
from google.genai import errors, types
from websockets import exceptions as ws

from robot_agent import config

logger = logging.getLogger(__name__)

_MODALITY = {types.MediaModality.AUDIO: 'audio', types.MediaModality.IMAGE: 'image', types.MediaModality.VIDEO: 'image'}


class _SearchLatch:
    """Google Search refused once stays off for the life of the process."""

    refused = False


SEARCH = _SearchLatch()


def client(api_key: str) -> genai.Client:
    """The Gemini client. Its WebSocket's closing handshake lasts at most CLOSE_TIMEOUT_S, not websockets' 10 s: a
    connect given up (timed out, a sleep asked) closes its socket before it ends. The SDK hands ws_connect the
    async_client_args it takes; httpx is given none of them."""
    options = types.HttpOptions(async_client_args={'close_timeout': config.CLOSE_TIMEOUT_S})
    return genai.Client(api_key=api_key, http_options=options)


def baked(view: dict[str, Any]) -> tuple[Any, ...]:
    """What a connection bakes in at setup, from a SettingsView: a change there needs a new connection."""
    settings = view['settings']
    return settings['voice'], view['system_prompt'], view['web_prompt'], settings['web_search']


def network_error(exc: BaseException) -> bool:
    """Whether a connect failed on the way (the network, a timeout, a server that fell over) rather than being
    refused by the server (a close frame it sent, an HTTP 4xx): such a failure refuses no handle and no search."""
    if isinstance(exc, ws.InvalidStatus):
        return exc.response.status_code >= 500
    if isinstance(exc, ws.ConnectionClosed):
        return exc.rcvd is None
    if isinstance(exc, errors.APIError):
        return exc.code == 1006  # the SDK's code for a socket that closed with no close frame
    return isinstance(exc, OSError | ws.InvalidHandshake)  # TimeoutError included


def vocabulary(view: dict[str, Any]) -> list[str]:
    """Words the recogniser should not miss: the robot's own name and the orders it must hear right."""
    return list(dict.fromkeys([view['settings']['name'], *config.TRANSCRIPTION_VOCABULARY]))


def build_config(
    view: dict[str, Any], declarations: list[types.FunctionDeclaration], *, handle: str | None, search: bool
) -> types.LiveConnectConfig:
    """One connection's setup. Never thinking_config (gemini-3.8-live closes on it), temperature, history_config."""
    prompt = view['system_prompt']
    if search and view['web_prompt']:
        prompt = f'{prompt}\n\n{view["web_prompt"]}'  # the prompt promises only the tools it has
    tools = [types.Tool(function_declarations=declarations)]
    if search:
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    voice = types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=view['settings']['voice']))
    vad = types.AutomaticActivityDetection(
        silence_duration_ms=config.VAD_SILENCE_MS,
        prefix_padding_ms=config.PREFIX_PADDING_MS,
        start_of_speech_sensitivity=types.StartSensitivity(config.VAD_START_SENSITIVITY),
        end_of_speech_sensitivity=types.EndSensitivity(config.VAD_END_SENSITIVITY),
    )
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        # No language_code: native-audio models choose the language they speak (the prompt says French).
        speech_config=types.SpeechConfig(voice_config=voice),
        system_instruction=types.Content(parts=[types.Part(text=prompt)]) if prompt.strip() else None,
        tools=tools,
        # The recogniser auto-detects the language unless told: noise came out as « ¿Qué? ».
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=[config.TRANSCRIPTION_LANGUAGE], custom_vocabulary=vocabulary(view)
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=vad),
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=config.COMPRESSION_TRIGGER_TOKENS,
            sliding_window=types.SlidingWindow(target_tokens=config.COMPRESSION_TARGET_TOKENS),
        ),
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )


async def open_session(
    stack: contextlib.AsyncExitStack,
    client: Any,
    view: dict[str, Any],
    declarations: list[types.FunctionDeclaration],
    handle: str | None,
    *,
    on_refused: Callable[[], None] | None = None,
) -> Any:
    """Connect, resuming `handle` if the server still takes it; if it refuses it, `on_refused()` and a fresh
    session. `stack` owns the connection. A network failure is raised as it is: the caller's budget decides."""
    if handle:
        try:
            return await _connect_searching(stack, client, view, declarations, handle)
        except Exception as exc:
            if network_error(exc):
                raise
            logger.warning('[RESUME] handle refused — fresh session, history lost (%s)', exc)
            if on_refused is not None:
                on_refused()
    return await _connect_searching(stack, client, view, declarations, None)


async def _connect_searching(
    stack: contextlib.AsyncExitStack, client: Any, view: dict[str, Any], declarations: list, handle: str | None
) -> Any:
    """Connect with Google Search when wanted; a refusal (not a network failure) is retried once without it."""
    search = view['settings']['web_search'] and not SEARCH.refused
    try:
        return await _connect(stack, client, build_config(view, declarations, handle=handle, search=search))
    except Exception as exc:
        if not search or network_error(exc):
            raise
        try:
            session = await _connect(stack, client, build_config(view, declarations, handle=handle, search=False))
        except Exception as retry_exc:
            raise exc from retry_exc  # the first error is the one that explains the problem
        SEARCH.refused = True
        logger.warning('[SEARCH] Google Search refused (%s): off until the robot restarts', exc)
        return session


async def _connect(stack: contextlib.AsyncExitStack, client: Any, live_config: types.LiveConnectConfig) -> Any:
    """Bounded: a server that takes the socket and never completes the setup is a failed attempt (TimeoutError)."""
    connect = stack.enter_async_context(client.aio.live.connect(model=config.MODEL, config=live_config))
    return await asyncio.wait_for(connect, config.CONNECT_TIMEOUT_S)


async def close(stack: contextlib.AsyncExitStack) -> None:
    """Close a connection, bounded: a close that hangs must not hold the robot up."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(stack.aclose(), config.CLOSE_TIMEOUT_S)


def usage_event(usage: types.UsageMetadata) -> dict[str, Any]:
    """A `usage` event (contracts.Usage) from one usage_metadata. The Mac computes the cost."""
    prompt = _by_modality(usage.prompt_tokens_details, usage.prompt_token_count)
    response = _by_modality(usage.response_tokens_details, usage.response_token_count)
    prompt['text'] += usage.tool_use_prompt_token_count or 0
    response['text'] += usage.thoughts_token_count or 0
    total = usage.total_token_count or sum(prompt.values()) + sum(response.values())
    logger.info('[USAGE] prompt %s, response %s, total %d', prompt, response, total)
    return {'kind': 'usage', 'prompt': prompt, 'response': response, 'total_tokens': total}


def time_left(go_away: types.LiveServerGoAway) -> float:
    """The seconds a go_away leaves (a protobuf duration: '10s', '1.5s'); 0, meaning now, when unreadable."""
    try:
        return float(str(go_away.time_left).removesuffix('s'))
    except ValueError:
        return 0.0


def _by_modality(details: list[types.ModalityTokenCount] | None, count: int | None) -> dict[str, int]:
    tokens = {'text': 0, 'audio': 0, 'image': 0}
    for item in details or []:
        tokens[_MODALITY.get(item.modality, 'text')] += item.token_count or 0
    tokens['text'] += max(0, (count or 0) - sum(tokens.values()))  # what the details leave unaccounted
    return tokens
