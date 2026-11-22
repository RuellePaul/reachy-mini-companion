"""One wake-to-sleep conversation over Gemini Live, driven by an explicit state machine (`state.py`).

What happens on the socket, the speaker and the clock becomes an `Event`; `transition()` decides the state;
`_on_transition` is the one place where a state change acts (animation, dashboard phase, speaker flush,
teardown). Each connection runs three tasks (mic pump, receive loop, watchdog), plus one per tool call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
from google.genai import types

from robot_agent import config, live
from robot_agent.adapters import Audio, Body
from robot_agent.audio import ComfortNoise, MicClock, MicMeter, StreamResampler, float_to_pcm, mono_mix, pcm_to_float
from robot_agent.notes import Notes
from robot_agent.state import Event, SessionState, transition
from robot_agent.tools import WHEN_IDLE, ToolResult, Tools
from robot_agent.transcript import Transcript

logger = logging.getLogger(__name__)

S = SessionState
# The animation of each state; the thinking robot looks attentive. CLOSING leaves the body to the sleep move.
_MODES = {
    S.CONNECTING: 'idle',
    S.RECONNECTING: 'idle',
    S.IDLE: 'idle',
    S.LISTENING: 'listening',
    S.THINKING: 'listening',
    S.SPEAKING: 'talking',
}
_MIC_MIME = f'audio/pcm;rate={config.GEMINI_INPUT_SR}'
_REFUSED = "Personne n'a parlé ces dix dernières secondes\xa0: cet appel vient d'un bruit, il n'a pas été exécuté."
_NOTED = "Cet appel répond à une note du tableau de bord, personne ne l'a demandé\xa0: il n'a pas été exécuté."
_PERFORMING = "Une scène se joue\xa0: rien d'autre ne se fait avant la fin."
_SLEEPING = 'Veille enclenchée.'
_CANCELLED = 'Annulé avant la fin.'
_NOTE = '(Note du tableau de bord, personne ne parle.) {}'


class Session:
    """One conversation. All of it runs on the event loop: other threads reach it with call_soon_threadsafe."""

    def __init__(
        self,
        client: Any,
        settings: dict[str, Any],
        *,
        session_id: str,
        audio: Audio,
        body: Body,
        tools: Tools,
        report: Callable[[dict[str, Any]], None],
        woke_at: float | None = None,
        moved_at: float | None = None,
        settled: bool = True,
        mic_mute: bool = config.MIC_MUTE_WHILE_TALKING,
        performing: Callable[[], bool] = lambda: False,  # a scene plays (scene.Performer.busy)
        held: Callable[[], bool] = lambda: False,  # an app being built at its ask, or the body lent as a controller
        heard: Callable[[np.ndarray, bool], None] = lambda chunk, own: None,  # each microphone chunk (ear.Ear.hear)
    ) -> None:
        self.session_id = session_id
        self.state = S.CONNECTING
        self.listening = asyncio.Event()  # set once the first connection is up
        self._client, self._audio, self._body, self._tools, self._report = client, audio, body, tools, report
        self._woke_at = woke_at  # the wake's trigger: the [WAKE] latency
        self._moved_at = moved_at  # the start of the wake move: the comfort noise covers it
        self._mic_mute, self._performing, self._held, self._heard = mic_mute, performing, held, heard
        self._declarations = tools.declarations()  # fixed for the session: a resumed handle wants the same setup
        self._transcript = Transcript(self._emit)
        self._speaker = StreamResampler(config.GEMINI_OUTPUT_SR, audio.get_output_samplerate())
        self._calls: dict[str, asyncio.Task[None]] = {}  # running tool calls, by call id
        self._seen: set[str] = set()  # the call ids received on this connection
        self._tasks: set[asyncio.Task[None]] = set()  # typed says on their way
        self._stops: set[asyncio.Future[None]] = set()  # sound stops sent to the daemon and not done yet
        self._turns = asyncio.Lock()  # text turns go out one at a time, in the order they were made
        self._notes = Notes(self._send_note, self._quiet, busy=performing)
        self._closing = asyncio.Event()  # set on entering CLOSING
        self._settled = asyncio.Event()  # this wake's settings are in: until then no connection is served
        if settled:
            self._settled.set()
        # The session's only reassigned fields, besides `state`:
        self.end_reason: str | None = None  # why it ends (contracts.EndReason)
        self._settings = settings  # the latest SettingsView: the next connection is opened with it
        self._live: Any = None  # the Live socket being served; None while (re)connecting
        self._baked: tuple[Any, ...] | None = None  # what the last connection was opened with (live.baked)
        self._handle: str | None = None  # the last resumable session handle, issued under `_baked`
        self._go_away_by: float | None = None  # the server's go_away: reconnect by then
        self._proven = False  # the connection completed a turn: it resets the reconnect budget
        self._state_since = time.monotonic()  # the reply clock: the last state change, text turn or tool answer
        self._last_human_at = -math.inf  # last transcription or typed say (tool guard)
        self._last_activity_at = time.monotonic()  # last human speech, VAD start, tool, scene, hold, move (idle sleep)
        self._playback_end = 0.0  # when the robot's queued audio has played out
        self._answer: str | None = None  # until over: 'turn' open; owed: 'reply' to a tool answer, 'text' to a say

    # ----- what the robot's main loop calls -----

    async def run(self) -> str:
        """Hold the conversation until it closes; returns why (contracts.EndReason)."""
        attempt = 0  # connects since the last healthy connection; each retry waits its backoff
        died_young = False  # one of them was accepted, then lost before it proved itself
        self._emit({'kind': 'phase', 'phase': self.state.value})
        try:
            while self.state is not S.CLOSING:
                if attempt:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._closing.wait(), config.RECONNECT_BACKOFF_S[attempt - 1])
                    if self.state is S.CLOSING:
                        break
                if attempt == len(config.RECONNECT_BACKOFF_S) and self._handle and died_young:
                    # The server accepting the handle, then closing at once, may be refusing it: the last try is
                    # fresh. A connect that never got through, or a connection the network lost, says nothing of it.
                    logger.warning('[RESUME] last attempt without the handle — fresh session, history lost')
                    self._handle = None
                outcome = await self._connection()
                died_young = outcome == 'failed' or (died_young and outcome in ('unreachable', 'refused', 'dropped'))
                # A reconnect of our choosing goes at once and spends nothing; a connection that never proved
                # itself is a failed attempt whatever ended it, a go_away included.
                attempt = {'planned': 0, 'proven': 1}.get(outcome, attempt + 1)
                if attempt > len(config.RECONNECT_BACKOFF_S) and self.state is not S.CLOSING:
                    logger.error('[LIVE] %d reconnects failed (%s): ending the conversation', attempt - 1, outcome)
                    self.request_sleep('refused' if outcome == 'refused' else 'connection_lost')
        finally:
            await self._teardown()
        return self.end_reason or 'error'

    def request_sleep(self, reason: str) -> None:
        """End the conversation (goto_sleep, dashboard, idle, shutdown…): silent at once, then torn down."""
        if self.end_reason is None:
            self.end_reason = reason
        self._dispatch(Event.SLEEP_REQUESTED)

    def say(self, text: str) -> None:
        """A turn typed in the dashboard: a human asking, like speech. Shown at once, sent once connected."""
        if self.state is S.CLOSING:
            return
        now = time.monotonic()
        self._last_human_at = self._last_activity_at = now
        logger.info('USER (typed): %s', text)
        self._transcript.typed(text, now)
        task = asyncio.create_task(self._send_text(text, human=True))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def notify(self, text: str) -> asyncio.Task[None] | None:
        """Something the conversation asked for has happened (an app is built): a note for the model, at the next
        quiet point (the task ends once it is sent). No human said it: it authorises no tool."""
        if self.state is not S.CLOSING:
            self._last_activity_at = time.monotonic()  # what held the idle sleep off may end in this same step
            return self._notes.add(text)

    def adopt(self, settings: dict[str, Any]) -> None:
        """This wake's settings, when the session opened on the previous wake's (`settled` False). Until then no
        connection is served; if what a connection bakes in differs, it reopens before the conversation starts."""
        if live.baked(settings) != live.baked(self._settings):
            logger.info('[WAKE] the settings changed since the last wake: reconnecting with them')
        self._settings = settings
        self._settled.set()

    # ----- connections -----

    async def _connection(self) -> str:
        """One connection, from connect to loss: 'closing', 'planned' (a reconnect of our choosing: settings to
        adopt, or a go_away once the connection has proven itself), 'proven' (lost once proven), 'failed' (ended
        before, by the server or a go_away), 'dropped' (lost to the network before), 'unreachable' (the connect failed
        on the way) or 'refused' (Gemini refused it). The network's failures are live.network_error's."""
        stack = contextlib.AsyncExitStack()
        self._go_away_by = None
        self._seen.clear()
        baked = live.baked(self._settings)
        if baked != self._baked:
            self._handle = None  # a handle onto a changed setup loses the context
        opened = time.monotonic()
        try:
            connect = live.open_session(
                stack, self._client, self._settings, self._declarations, self._handle, on_refused=self._forget_handle
            )
            live_socket = await self._unless_closing(connect)
            # A socket opened on the last wake's settings waits for this wake's: the first request must not go to
            # a connection about to be reopened.
            if live_socket is None or await self._unless_closing(self._settled.wait()) is None:
                return 'closing'
            self._baked, opened, self._proven = baked, time.monotonic(), False
            if baked != live.baked(self._settings):
                why = 'reopen'  # never served
            else:
                self._live = live_socket
                self._dispatch(Event.CONNECTED)
                why = await self._serve()
        except Exception as exc:  # noqa: BLE001 — any connect failure counts against the reconnect budget
            logger.warning('[LIVE] connection failed: %r', exc)
            why = 'unreachable' if live.network_error(exc) else 'refused'
        finally:
            self._live = None
            await live.close(stack)
        if self.state is S.CLOSING:
            return 'closing'
        self._transcript.unanswered()  # a turn does not outlive its connection, nor does an answer
        self._answer = None
        self._dispatch(Event.CONNECTION_LOST)
        proven = self._proven or time.monotonic() - opened >= config.RECONNECT_PROVEN_S
        if why == 'reopen' or (why == 'go_away' and proven):
            return 'planned'
        if why in ('unreachable', 'refused') or (why == 'dropped' and not proven):
            return why
        return 'proven' if why in ('lost', 'dropped') and proven else 'failed'

    def _forget_handle(self) -> None:
        self._handle = None  # the server refused it: never offered again

    async def _serve(self) -> str:
        """Run the connection's tasks until one of them ends: 'lost', 'dropped', 'go_away' or 'closing'."""
        jobs = {'receive': self._receive(), 'watchdog': self._watchdog(), 'mic': self._pump_mic()}
        tasks = [asyncio.create_task(job, name=name) for name, job in jobs.items()]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        task = done.pop()
        if task.exception() is not None:
            logger.error('[LIVE] %s failed', task.get_name(), exc_info=task.exception())
            return 'lost'
        return task.result()

    async def _unless_closing(self, job: Awaitable[Any]) -> Any:
        """`job`'s result, or None if the session starts closing first: a connect must not hold up a sleep."""
        task = asyncio.ensure_future(job)
        closing = asyncio.ensure_future(self._closing.wait())
        try:
            await asyncio.wait({task, closing}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            closing.cancel()
            task.cancel()  # nothing if it is done
            await asyncio.gather(task, return_exceptions=True)
        return None if task.cancelled() else task.result()

    async def _live_socket(self, wait_s: float | None = None) -> Any:
        """The Live socket, waiting up to `wait_s` (LIVE_WAIT_S) for a (re)connection; None if there is none."""
        deadline = time.monotonic() + (config.LIVE_WAIT_S if wait_s is None else wait_s)
        while self._live is None and self.state is not S.CLOSING and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return None if self.state is S.CLOSING else self._live

    # ----- the three tasks of a connection -----

    async def _receive(self) -> str:
        """Read the socket until it fails: 'dropped' when the network lost it (no close frame), else 'lost'.
        receive() ends at each interaction's end, so it is re-entered."""
        try:
            while True:
                async for message in self._live.receive():
                    try:
                        self._on_message(message)
                    except Exception:
                        logger.exception('[LIVE] a server message could not be handled')
        except Exception as exc:  # noqa: BLE001 — a closed or broken socket: the reconnect decides
            if self.state is not S.CLOSING:
                logger.warning('[LIVE] connection lost: %s', exc)
            return 'dropped' if live.network_error(exc) else 'lost'

    async def _watchdog(self) -> str:
        """The clock's events: playback drained, no reply, the idle sleep, and the planned reconnects."""
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._closing.wait(), config.WATCHDOG_TICK_S)
            if self.state is S.CLOSING:
                return 'closing'
            now, performing = time.monotonic(), self._performing()
            # A scene (a dashboard's too), a build, a game, a move (an app's: it mutes the human): activity to its end.
            if performing or self._held() or self._body.moving_until() > now:
                self._last_activity_at = now
            quiet = not self._calls and now >= self._playback_end and not performing
            if self.state is S.SPEAKING and now >= self._playback_end:
                self._dispatch(Event.PLAYBACK_DRAINED)
            elif self.state is S.THINKING and not self._calls and now - self._state_since >= config.REPLY_TIMEOUT_S:
                logger.info('[LIVE] no reply for %.0f s: back to idle', config.REPLY_TIMEOUT_S)
                self._answer = None
                self._dispatch(Event.INTERACTION_IDLE)
            idle_s = float(self._settings['settings']['idle_sleep_minutes']) * 60.0
            # From the later of the last activity and the end of the robot's audio: the clock never runs back.
            if idle_s > 0 and quiet and now - max(self._last_activity_at, self._playback_end) >= idle_s:
                logger.info('[IDLE] nothing for %.0f s: going to sleep', idle_s)
                self.end_reason = self.end_reason or 'idle'
                self._dispatch(Event.IDLE_TIMEOUT)
                return 'closing'
            if self._go_away_by is not None and (now >= self._go_away_by or (quiet and self.state is S.IDLE)):
                return 'go_away'

    async def _pump_mic(self) -> str:
        """Stream the microphone; a chunk captured while the robot was audible (its voice, its own moves) is
        replaced by the comfort noise, however late the chunk is read."""
        mic = await asyncio.to_thread(MicClock.drained, self._audio)
        resampler = StreamResampler(self._audio.get_input_samplerate(), config.GEMINI_INPUT_SR)
        noise = ComfortNoise(config.GEMINI_INPUT_SR)
        meter = MicMeter() if config.MIC_LEVEL_LOG else None
        woken = (self._moved_at if self._moved_at is not None else -math.inf) + config.WAKE_S  # the wake move's end
        failures, logged_at = 0, -math.inf
        while True:
            try:
                sample = await asyncio.to_thread(mic.pull)
            except Exception as exc:  # noqa: BLE001 — one appsink hiccup must not deafen the robot
                failures += 1
                if time.monotonic() - logged_at >= config.ERROR_LOG_EVERY_S:
                    logged_at = time.monotonic()
                    logger.warning('mic pull failed (%d so far): %s', failures, exc)
                await asyncio.sleep(config.MIC_RETRY_S)
                continue
            if sample is None:
                await asyncio.sleep(config.MIC_RETRY_S)
                continue
            chunk = resampler.process(mono_mix(np.asarray(sample, dtype=np.float32)))
            loud_until = max(self._playback_end, woken, self._body.moving_until())
            own = mic.captured_at < loud_until + config.SELF_NOISE_TAIL_S  # its voice and its moves, with their tail
            performing = self._performing()  # a scene: the robot is the noise from its first line to its last
            self._heard(chunk, own)
            if meter is not None:
                meter.add(chunk, audible=performing or own, state=self.state.value)
            if self._mic_mute or performing:
                chunk = noise.process(chunk, robot_audible=performing or own)
            if chunk.size:
                with contextlib.suppress(Exception):  # a dead socket is the receive loop's to notice
                    await self._live.send_realtime_input(
                        audio=types.Blob(data=float_to_pcm(chunk), mime_type=_MIC_MIME)
                    )

    # ----- server messages -----

    def _on_message(self, message: types.LiveServerMessage) -> None:
        if message.usage_metadata is not None:
            self._emit(live.usage_event(message.usage_metadata))  # billed even when it comes after a sleep order
        if self.state is S.CLOSING:
            return  # asleep: nothing more acts, not even a tool call already on the wire
        update = message.session_resumption_update
        if update is not None and update.resumable and update.new_handle:
            self._handle = update.new_handle
        if message.go_away is not None:
            left = live.time_left(message.go_away)
            logger.info('[LIVE] go_away, %.1f s left: reconnecting at the next quiet point', left)
            self._go_away_by = time.monotonic() + left - config.GO_AWAY_URGENT_S
            self._dispatch(Event.GO_AWAY)
        if message.tool_call_cancellation is not None:
            for call_id in message.tool_call_cancellation.ids or []:
                if (task := self._calls.get(call_id)) is not None:
                    logger.info('[TOOL] call %s withdrawn by the server', call_id)
                    task.cancel('withdrawn')  # a scene says it was not the sleep (scene.STOPPED)
        activity = message.voice_activity.voice_activity_type if message.voice_activity else None
        if activity == types.VoiceActivityType.ACTIVITY_START:
            self._last_activity_at = time.monotonic()  # someone may be talking: no idle sleep on them
            self._dispatch(Event.VAD_START)
        elif activity == types.VoiceActivityType.ACTIVITY_END:
            self._transcript.user_done(time.monotonic())
            self._dispatch(Event.VAD_END)
        if message.tool_call is not None:
            for call in message.tool_call.function_calls or []:
                self._start_call(call)
        if message.server_content is not None:
            self._on_content(message.server_content)

    def _on_content(self, content: types.LiveServerContent) -> None:
        if content.interrupted:
            logger.info('[BARGE-IN] the user talked over the robot')
            self._transcript.interrupted()
            self._answer = 'text' if self._answer == 'text' else None  # a say's answer outlives what it cut into
            self._dispatch(Event.INTERRUPTED)
        for transcription in (content.interim_input_transcription, content.input_transcription):
            if transcription is not None and transcription.text and transcription.text.strip():
                self._last_human_at = self._last_activity_at = time.monotonic()
                if transcription is content.input_transcription:
                    self._transcript.heard(transcription.text)
                self._dispatch(Event.TRANSCRIPT_IN)
        parts = content.model_turn.parts if content.model_turn is not None else None
        for part in parts or []:
            if part.inline_data is not None and part.inline_data.data:
                self._play(part.inline_data.data)
        if content.output_transcription is not None and content.output_transcription.text and not self._performing():
            self._transcript.said(content.output_transcription.text)  # it trails the audio: it opens no answer
        status = content.interaction_status
        if content.turn_complete:
            self._proven = True
            self._transcript.close()
            if self._answer == 'text':  # the generation the say cut into is over: the say's own is open from now
                self._answer = 'turn'
            elif status != types.InteractionStatus.IN_PROGRESS:
                if self._answer == 'turn':
                    self._answer = None  # but a reply owed to a tool answer outlives the turn that made the call
                self._dispatch(Event.TURN_COMPLETE)
        if status == types.InteractionStatus.IDLE:
            self._answer = None
            self._dispatch(Event.INTERACTION_IDLE)

    def _play(self, pcm: bytes) -> None:
        """Queue model audio (24 kHz) on the speaker and move the playback clock."""
        if self.state is S.CLOSING or self._performing():
            return  # going to sleep, or a scene playing: muted
        samples = pcm_to_float(pcm)
        now = self._push(self._speaker.process(samples), samples.size / config.GEMINI_OUTPUT_SR)
        self._answer = 'text' if self._answer == 'text' else 'turn'  # until its end, what comes is the cut answer's
        if (
            self.state is not S.SPEAKING
            and now - self._last_human_at > config.HUMAN_GRACE_S
            and not self._notes.recent(now)
        ):
            logger.warning('[PHANTOM] the robot answers, but nobody spoke in the last %.0f s', config.HUMAN_GRACE_S)
        self._transcript.audio(now)
        self._dispatch(Event.AUDIO_OUT)

    def stage_audio(self, samples: np.ndarray, seconds: float) -> None:
        """A scene's audio, at the speaker's rate: it moves the playback clock (the comfort noise covers it, the
        robot shows it speaking) but is no turn of the model's."""
        if self.state is not S.CLOSING:
            self._push(samples, seconds)
            self._dispatch(Event.AUDIO_OUT)

    @property
    def playback_end(self) -> float:
        return self._playback_end

    def _push(self, out: np.ndarray, seconds: float) -> float:
        if out.size:  # the filter may hold a tiny chunk back, and an empty push crashes the SDK
            try:
                self._audio.push_audio_sample(out)
            except Exception as exc:  # noqa: BLE001 — a lost chunk is a glitch, not a reason to hang up
                logger.debug('speaker push failed: %s', exc)
        now = time.monotonic()
        self._playback_end = max(self._playback_end, now) + seconds
        return now

    # ----- tool calls -----

    def _start_call(self, call: types.FunctionCall) -> None:
        call_id = call.id or secrets.token_hex(4)
        name, args = call.name or '', dict(call.args or {})
        if call_id in self._seen or call_id in self._calls:
            logger.warning('[TOOL] call %s (%s) received again: it runs and is answered once', call_id, name)
            return
        self._seen.add(call_id)
        self._emit({'kind': 'tool_call', 'call_id': call_id, 'name': name, 'args': args})
        task = asyncio.create_task(self._run_call(call_id, name, args, self._live), name=f'tool-{name}')
        self._calls[call_id] = task
        task.add_done_callback(lambda done: self._call_done(call_id, name, done))
        self._last_activity_at = time.monotonic()
        self._answer = 'text' if self._answer == 'text' else 'turn'  # until its end, what comes is the cut answer's
        self._dispatch(Event.TOOL_STARTED)

    def _call_done(self, call_id: str, name: str, task: asyncio.Task[None]) -> None:
        if self._calls.get(call_id) is task:
            del self._calls[call_id]
        if task.cancelled():  # withdrawn by the server, or cut short by sleep, maybe before it started
            self._result(call_id, name, ToolResult('cancelled', _CANCELLED))
        self._last_activity_at = time.monotonic()
        if not self._calls:
            self._state_since = time.monotonic()  # the answers are out: the reply to them is owed from now
        self._dispatch(Event.TOOL_FINISHED)

    async def _run_call(self, call_id: str, name: str, args: dict[str, Any], origin: Any) -> None:
        """One call, answered exactly once, on the connection that issued it (`origin`): a later connection
        never issued it, since a resumption handle is not given while a call is pending. A withdrawn call,
        one cut short by sleep, and goto_sleep are never answered."""
        if self._notes.after(self._last_human_at):  # the model acts on a note that came after the human's turn
            logger.info('[NOTE] %s(%s) refused: it answers a note, no human asked for it', name, args)
            result = ToolResult('refused', _NOTED)
        elif time.monotonic() - self._last_human_at > config.HUMAN_GRACE_S:
            logger.warning('[PHANTOM] %s(%s) refused: no human asked for it', name, args)
            result = ToolResult('refused', _REFUSED)
        elif self._performing():
            result = ToolResult('refused', _PERFORMING, ask="N'en parle pas.")
        elif name == 'goto_sleep':
            self._result(call_id, name, ToolResult('ok', _SLEEPING))
            self.request_sleep('goto_sleep')
            return
        else:
            try:
                result = await self._tools.run(name, args, self._send_image)
            except Exception:
                logger.exception('[TOOL] %s crashed', name)
                result = ToolResult('error', "L'outil a échoué\xa0: rien n'a été fait.", ask='Dis-le simplement.')
        if self.state is S.CLOSING or self._live is not origin:
            logger.warning('[TOOL] the connection that asked for %s is gone: its answer is not sent', name)
        else:
            spoke = self._playback_end > self._last_human_at  # the robot's audio came after the human's turn
            response = result.response(call_id, name, spoke=spoke)
            try:
                await origin.send_tool_response(function_responses=[response])
                if response.scheduling == WHEN_IDLE:
                    self._answer = 'reply'  # the model speaks about it once idle: its answer is not over
            except Exception as exc:  # noqa: BLE001 — a dead socket: the reconnect is on its way
                logger.warning('[TOOL] answer to %s not sent: %s', name, exc)
        self._result(call_id, name, result)  # after the send: cancelled meanwhile, it is reported once, by _call_done

    def _result(self, call_id: str, name: str, result: ToolResult) -> None:
        logger.info('[TOOL] %s: %s, %s', name, result.status, result.message)
        self._emit(result.event(call_id, name))

    async def _send_image(self, jpeg: bytes) -> None:
        """An image as its own user turn (look): not a human turn, so it authorises nothing."""
        live_socket = await self._live_socket()
        if live_socket is None:
            raise ConnectionError('no Live connection')
        part = types.Part(inline_data=types.Blob(data=jpeg, mime_type='image/jpeg'))
        await live_socket.send_client_content(turns=[types.Content(role='user', parts=[part])])

    def _quiet(self) -> bool:
        """Nobody talks, nothing plays, no say is on its way, the connection is up and staying: a note may go."""
        served = self._live is not None and self._go_away_by is None and not self._turns.locked()
        return self.state is S.IDLE and time.monotonic() >= self._playback_end and not self._performing() and served

    async def _send_note(self, text: str) -> None:
        self._last_activity_at = time.monotonic()  # the model has something to say: no idle sleep meanwhile
        self._dispatch(Event.USER_TEXT)
        await self._send_text(_NOTE.format(text))

    async def _send_text(self, text: str, *, human: bool = False) -> None:
        async with self._turns:  # each waits for the socket on its own: the lock keeps them in order
            live_socket = await self._live_socket(math.inf if human else None)  # a human's waits out the reconnects
            if live_socket is None:
                logger.warning('[SAY] no connection: « %s » dropped', text)
                return
            if human:  # the model gets the human's turn now: the tool guard counts from here
                self._last_human_at = time.monotonic()
                if self._answer is not None or self.state is S.THINKING:
                    self._answer = 'text'  # a generation may be open: its end is not this say's
                self._dispatch(Event.USER_TEXT)
            try:
                await live_socket.send_client_content(turns=[types.Content(role='user', parts=[types.Part(text=text)])])
            except Exception as exc:  # noqa: BLE001 — a dead socket: the reconnect is on its way
                logger.warning('[SAY] not sent: %s', exc)

    # ----- the state machine's one place for effects -----

    def _dispatch(self, event: Event) -> None:
        old = self.state
        self.state = transition(old, event, tool_pending=bool(self._calls), answering=self._answer is not None)
        if self.state is not old or event is Event.USER_TEXT:  # a text turn waits for a reply of its own
            self._state_since = time.monotonic()
        self._on_transition(old, self.state, event)

    def _on_transition(self, old: SessionState, new: SessionState, event: Event) -> None:
        if event is Event.INTERRUPTED and new is not S.CLOSING:
            self.flush()
        if new is old:
            return
        self._emit({'kind': 'phase', 'phase': new.value})
        if new is S.CLOSING:
            logger.info('[SLEEP] closing the conversation (%s)', self.end_reason)
            current = asyncio.current_task()
            for task in self._calls.values():  # at once: a call not started yet never starts
                if task is not current:
                    task.cancel()
            self.flush()
            self._closing.set()
            return
        self._body.set_mode(_MODES[new])
        if old is S.THINKING and new is S.IDLE:  # the answer is over and never sounded: no reply, or it declined
            self._transcript.unanswered()
        if old is S.CONNECTING and not self.listening.is_set():
            self.listening.set()
            if self._woke_at is not None:
                logger.info('[WAKE] listening %.0f ms after the wake', (time.monotonic() - self._woke_at) * 1000)

    def flush(self) -> None:
        """Drop what the speaker has queued and stop an emotion's sound (barge-in, sleep). The motors' tail
        keeps the comfort noise on."""
        with contextlib.suppress(Exception):
            self._audio.clear_output()
        stop = asyncio.get_running_loop().run_in_executor(None, self._audio.stop_sound)  # blocking HTTP: off the loop
        self._stops.add(stop)
        stop.add_done_callback(self._stops.discard)
        self._playback_end = min(self._playback_end, time.monotonic())
        self._speaker.reset()

    async def _teardown(self) -> None:
        """Nothing acts once the robot sleeps: tool calls are cancelled (unanswered), moves stopped."""
        if self.state is not S.CLOSING:
            self.request_sleep('error')
        self._transcript.close()  # the turn the sleep cut short still gets its end, and its log line
        self._notes.cancel()
        pending: list[asyncio.Future[None]] = [*self._calls.values(), *self._tasks]
        for task in pending:
            task.cancel()
        pending += self._stops  # waited for, not cancelled: a sound played after the end (« confused ») is never cut
        if pending:
            await asyncio.wait(pending, timeout=config.CLOSE_TIMEOUT_S)
        self._body.stop_moves()

    def _emit(self, event: dict[str, Any]) -> None:
        self._report({**event, 'session_id': self.session_id})
