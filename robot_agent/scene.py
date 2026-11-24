"""Scenes on the robot: the Performer plays a scene's timeline (voices rendered on the Mac, moves through the one
motion thread); the `play_scene` tool asks the Mac for a scene by its title, `write_scene` for a new one, written on
the Mac and played once ready (`Writes`). The stage is the session (its playback
clock moves: the comfort noise covers the scene; it mutes the model and refuses tools while `busy`), or, asleep,
`Standalone`: the speaker alone. In a rehearsal a human says one character's lines: on each of their turns the robot
is silent and holds still until an antenna is tapped, then goes on."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
import wave
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np

from robot_agent import animations, apps, config
from robot_agent.audio import StreamResampler, pcm_to_float
from robot_agent.mac import Mac, MacError
from robot_agent.state import SessionState
from robot_agent.tools import _LENT, Extra, SendImage, ToolResult, _decl, _text

logger = logging.getLogger(__name__)

SPEAKER_LATENCY_S = 0.15  # an estimate, not a measurement: from a push to the sound out of the speaker
LEAD_S = 0.3  # a segment that follows another is queued this early: the speaker plays them back to back
CHUNK_S = 0.5
END_WAIT_S = 5.0  # how long the last moves may outlast the last voice
POLL_S = 0.05  # how often a human's turn looks for a tap
STILL = animations.Gesture(((0, 0, 0, 0), (3600, 0, 0, 0)))  # neutral, held through a human's turn, then cut short
STOPPED = {  # why -> how it was stopped, in French
    'dashboard': 'depuis le tableau de bord', 'say': 'par un message écrit', 'antenna': 'à la main, par une antenne',
    'replaced': 'par une autre scène', 'wake': 'par le réveil du robot', 'sleep': 'par la mise en veille',
    'withdrawn': 'par la conversation, qui a annulé la demande',  # the server withdrew play_scene: still awake
}  # fmt: skip
_FAILURES = (MacError, OSError, EOFError, ValueError, wave.Error)
ANNOUNCE_S = 8.0  # how long the model has to start announcing a scene that is ready; silent, the scene starts
SAID_S = 30.0  # and to finish its sentence
SETTLE_S = 0.3  # the end of that sentence leaves the speaker before the first line


class Standalone:
    """The stage when no conversation runs: the speaker alone, with its own playback clock. Like the session, it
    has `playback_end` (monotonic), `stage_audio(samples, seconds)` (never empty) and `flush()`; `show` stands for
    the session's states."""

    def __init__(self, audio: Any) -> None:
        self._audio = audio
        self.playback_end = 0.0

    def stage_audio(self, samples: np.ndarray, seconds: float) -> None:
        with contextlib.suppress(Exception):  # a lost chunk is a glitch
            self._audio.push_audio_sample(samples)
        self.playback_end = max(self.playback_end, time.monotonic()) + seconds

    def flush(self) -> None:
        with contextlib.suppress(Exception):
            self._audio.clear_output()
        self.playback_end = min(self.playback_end, time.monotonic())

    async def show(self, body: Any) -> None:
        """Until cancelled, what the session's states show awake: the robot talking while a voice plays, idle between
        lines. Never out of a sleep pose asked meanwhile (a shutdown)."""
        while True:
            if body.mixer.mode != 'sleep':
                body.set_mode('talking' if time.monotonic() < self.playback_end else 'idle')
            await asyncio.sleep(config.WATCHDOG_TICK_S)


class Performer:
    """Plays one scene at a time (contracts.SceneTimeline). `stop` ends it at once; cancelling it (sleep) too. A scene
    starts facing the audience unless it opens with a turn, and ends facing it, wherever the conversation or the scene
    it replaced left the body (a turn to where it faces is nothing); one replaced leaves that to the next, unless a stop
    voids the next before it starts. A still play (no moves) turns nothing but what a scene it replaced left turned."""

    def __init__(
        self,
        mac: Mac,
        audio: Any,
        body: Any,
        emotions: Any,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._mac, self._audio, self._body, self._emotions = mac, audio, body, emotions
        self._clock = clock
        self._sleep = sleep or self._until_stopped
        self._stop = asyncio.Event()
        self._why = ''
        self.busy = self.waiting = self._tapped = False  # waiting: a human's turn, until a tap
        self._owed = False  # a scene replaced left the body as it was: the next one faces the audience, still or not
        self.on_stage: str | None = None  # what its events say it plays, `started` to the end: the heartbeat says it
        self.stops = 0  # how many were asked: a scene waiting to start (main) never does once another comes
        self.stopped_by = ''  # why the last one was asked

    def tap(self) -> None:
        self._tapped = self._tapped or self.waiting

    def stop(self, why: str) -> None:
        self.stops, self.stopped_by = self.stops + 1, why
        if self._owed and not self.busy and why != 'replaced':  # the next scene never starts (main voids it)
            self._owed = False
            self._body.turn_body(0.0)
        if self.busy and not self._stop.is_set():
            logger.info('[SCENE] stop: %s', why)
            self._why = why
            self._stop.set()

    async def perform(self, timeline: dict[str, Any], stage: Any, session_id: str) -> ToolResult:
        title = str(timeline.get('title', ''))
        if self.busy:
            return ToolResult('refused', 'Une scène est déjà en train de se jouer.')
        self.busy, self._why = True, ''
        owed, self._owed, moving = self._owed, False, timeline.get('moves', True)
        self._stop.clear()

        def emit(state: str, **fields: Any) -> None:
            scene = {'scene_id': str(timeline.get('scene_id', '')), 'title': title}
            self.on_stage = scene['scene_id'] if state in ('started', 'line', 'waiting') else None
            self._mac.emit({'kind': 'scene', 'session_id': session_id, **scene, 'state': state, **fields})

        fetching = None
        try:
            cues = [cue for cue in timeline.get('cues') or [] if moving or cue['kind'] == 'audio']
            cues = _turns(cues, timeline.get('rehearse'))
            if (moving or owed) and not any(cue['kind'] == 'turn' and cue['at_ms'] == 0 for cue in cues):
                self._body.turn_body(0.0, config.SCENE_TURN_SPEED)
            schedule = _schedule(cues, moving)
            moves = await self._off_loop(self._moves, cues) or {}
            voices, fetching = self._prefetch([cue['key'] for _, cue in schedule if cue['kind'] == 'audio'])
            start = max(self._clock(), stage.playback_end)
            if not self._stop.is_set():  # else stopped while the emotions loaded: nothing started
                logger.info('[SCENE] « %s »: %d cues, %.0f s', title, len(cues), timeline.get('duration_ms', 0) / 1000)
                emit('started')
            for due, cue in schedule:
                if await self._wait(start + due):
                    break
                if cue['kind'] == 'human':
                    if await self._human(cue, stage, emit):
                        break
                    start = self._clock() - cue['end_ms'] / 1000  # what follows comes as long after it as written
                elif cue['kind'] == 'audio':
                    asked = self._clock()
                    if (chunks := await self._until_stop(voices[cue['key']])) is None:
                        break
                    start += self._clock() - asked  # a voice that came late delays what follows by as much
                    emit('line', line=cue['line'])
                    for chunk in chunks:
                        stage.stage_audio(chunk, chunk.size / self._audio.get_output_samplerate())
                elif cue['kind'] == 'turn':
                    self._body.turn_body(float(cue.get('degrees') or 0.0), config.SCENE_TURN_SPEED)
                elif cue['kind'] == 'cut':
                    self._body.stop_moves()  # another character speaks: the last one's move ends here
                elif cue['kind'] == 'gesture':
                    self._body.gesture(str(cue.get('name')))
                elif moves.get(cue.get('name')) is not None:
                    self._body.play_move(moves[cue['name']])  # an emotion, without its sound: the voices are the sound
            else:
                await self._wait(self._quiet(stage))
            if self._stop.is_set():
                how = STOPPED.get(self._why, 'en cours de route')
                stage.flush()
                self._body.stop_moves()
                emit('stopped', message=f'Arrêtée {how}.')
                return ToolResult('ok', f'Scène «\xa0{title}\xa0» arrêtée {how}.', silent=True)
            emit('ended')
            # Silent: the scene was the answer. Spoken, it makes the model comment the scene or play it again.
            return ToolResult('ok', f"Scène «\xa0{title}\xa0» jouée jusqu'au bout.", silent=True)
        except asyncio.CancelledError as exc:  # by the session: the robot goes to sleep, or the call is withdrawn
            stage.flush()
            self._body.stop_moves()
            emit('stopped', message=f'Arrêtée {STOPPED["withdrawn" if "withdrawn" in exc.args else "sleep"]}.')
            raise
        except _FAILURES as exc:
            logger.warning('[SCENE] « %s » failed: %s', title, exc)
            emit('failed', message=f"La scène n'a pas pu être jouée\xa0: {exc}")
            failed = f"La scène «\xa0{title}\xa0» n'a pas pu être jouée\xa0: {exc}"
            return ToolResult('error', failed, ask='Dis-le simplement.')
        finally:
            if fetching is not None:
                fetching.cancel()  # no download after the curtain
            if self._why == self.stopped_by == 'replaced':  # the next scene turns where it starts
                self._owed = moving
            elif moving:  # it ends facing the audience
                self._body.turn_body(0.0)
            self.busy, self.on_stage = False, None

    async def _human(self, cue: dict[str, Any], stage: Any, emit: Callable[..., None]) -> bool:
        """The human's turn, once the robot's last voice and moves are over: silent and still until an antenna is
        tapped; whether the scene was stopped meanwhile."""
        self.waiting, self._tapped = True, False
        try:
            if await self._wait(self._quiet(stage)):
                return True
            emit('waiting', line=cue['line'], message=cue.get('text'))
            self._body.play_move(STILL)
            while not self._stop.is_set() and not self._tapped:
                if self._body.current_move() is None:  # a try played over the hold has ended: still again
                    self._body.play_move(STILL)
                await self._sleep(POLL_S)
            return self._stop.is_set()
        finally:
            self.waiting = False
            self._body.stop_moves()

    def _quiet(self, stage: Any) -> float:  # the end of the robot's voice, and of its moves (END_WAIT_S at most)
        return max(stage.playback_end, min(self._body.moving_until(), stage.playback_end + END_WAIT_S))

    async def _until_stop(self, work: asyncio.Future[Any]) -> Any:
        """`work`'s result, or None once stopped: a stop never waits for a download, whose result is lost."""
        stop = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait((work, stop), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        return None if self._stop.is_set() else work.result()

    async def _off_loop(self, job: Callable[..., Any], *args: Any) -> Any:
        work = asyncio.ensure_future(asyncio.to_thread(job, *args))
        try:
            return await self._until_stop(work)
        finally:
            work.cancel()

    def _prefetch(self, keys: list[str]) -> tuple[dict[str, asyncio.Future[list[np.ndarray]]], asyncio.Task[None]]:
        """Each voice fetched in the order it is said, one at a time, while the scene plays: the first word waits
        for one download, not for all of them (a ten-minute scene is ~19 MB at 16 kHz)."""

        async def fetch_all() -> None:
            for key, voice in voices.items():
                try:
                    voice.set_result(await asyncio.to_thread(self._voice, key))  # download and decode: off the loop
                except Exception as exc:  # noqa: BLE001 — raised when its line comes: no voice is left pending
                    voice.set_exception(exc)
                    return

        loop = asyncio.get_running_loop()
        voices = {key: loop.create_future() for key in dict.fromkeys(keys)}
        return voices, asyncio.create_task(fetch_all())

    def _voice(self, key: str) -> list[np.ndarray]:
        """One voice, at the speaker's rate (the Mac resamples it), in chunks. Blocking."""
        return self._chunks(self._mac.get_bytes(f'/api/scenes/audio/{key}?rate={self._audio.get_output_samplerate()}'))

    def _moves(self, cues: list[dict[str, Any]]) -> dict[str, Any]:
        return {cue['name']: self._emotions.move(cue['name']) for cue in cues if cue['kind'] == 'emotion'}

    def _chunks(self, wav: bytes) -> list[np.ndarray]:
        """A WAV resampled for the speaker, in chunks of CHUNK_S, none empty. Blocking (CPU)."""
        rate = self._audio.get_output_samplerate()
        with wave.open(io.BytesIO(wav)) as reader:
            source, channels = reader.getframerate(), reader.getnchannels()
            samples = pcm_to_float(reader.readframes(reader.getnframes())).reshape(-1, channels).mean(axis=1)
        out = StreamResampler(source, rate).process(samples)
        step = int(rate * CHUNK_S)
        return [chunk for chunk in (out[i : i + step] for i in range(0, out.size, step)) if chunk.size]

    async def _wait(self, until: float) -> bool:
        """Until `until` (on the clock); whether the scene was stopped meanwhile."""
        delay = until - self._clock()
        if delay > 0 and not self._stop.is_set():
            await self._sleep(delay)
        return self._stop.is_set()

    async def _until_stopped(self, delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), delay)


def _turns(cues: list[dict[str, Any]], rehearse: str | None) -> list[dict[str, Any]]:
    """The cues, each run of the rehearsed character's segments made one `human` cue from its first segment's time
    to its last's (`end_ms`). The moves written from it to the robot's next segment are dropped: it holds still."""
    out: list[dict[str, Any]] = []
    turn: dict[str, Any] | None = None
    for cue in cues:
        if cue['kind'] == 'audio' and rehearse and cue.get('character') == rehearse:
            if turn is None:
                turn = {**cue, 'kind': 'human', 'text': ''}
                out.append(turn)
            turn.update(end_ms=cue['at_ms'], text=f'{turn["text"]} {cue.get("text") or ""}'.strip())
        elif cue['kind'] == 'audio' or turn is None:
            turn = None
            out.append(cue)
    return out


def _schedule(cues: list[dict[str, Any]], cuts: bool = True) -> list[tuple[float, dict[str, Any]]]:
    """(seconds from the start, cue), in order. A segment right after another is queued LEAD_S early so the speaker
    plays them back to back; after a silence it is pushed on time. Moves wait for the speaker's latency; when another
    character starts speaking, a `cut` ends the move still playing first. A human's turn comes after every cue written
    before it and before every cue written after it (whose clock it moves)."""
    steps, voice_end, part, speaker = [], None, 0, None
    for cue in cues:
        at = cue['at_ms'] / 1000
        if cue['kind'] == 'audio':
            follows = voice_end is not None and abs(at - voice_end) < 0.002
            steps.append((part, max(0.0, at - LEAD_S) if follows else at, cue))
            if cuts and speaker is not None and cue.get('character') != speaker:
                steps.append((part, at, {'kind': 'cut', 'line': cue['line']}))
            voice_end, speaker = at + (cue.get('duration_ms') or 0) / 1000, cue.get('character')
        else:
            steps.append((part, at + SPEAKER_LATENCY_S, cue))
        if cue['kind'] == 'human':
            part, voice_end = part + 1, None
    return [(due, cue) for _, due, cue in sorted(steps, key=lambda step: step[:2])]


class Writes:
    """The scenes this conversation asked for, while the Mac writes them: the robot stays awake for them (no idle
    sleep), and once one is ready the model announces it, then the robot asks the Mac to play it, as the dashboard
    would. A note authorises no tool: the scene is not the model's to start. On the event loop; `clear` when the
    conversation ends."""

    def __init__(self, mac: Mac, session: Callable[[], Any]) -> None:
        self._mac, self._session = mac, session
        self._watching: dict[str, asyncio.Task[None]] = {}

    def pending(self) -> bool:
        return bool(self._watching)

    def watch(self, build: dict[str, Any]) -> None:
        build_id = str(build.get('id', ''))
        if build.get('status') == 'building' and build_id and build_id not in self._watching:
            self._watching[build_id] = asyncio.create_task(self._until_ready(build_id), name=f'scene-{build_id}')

    def clear(self) -> None:
        for task in self._watching.values():
            task.cancel()
        self._watching.clear()

    async def _until_ready(self, build_id: str) -> None:
        try:
            build = await apps.ended(self._mac, '/api/scenes/builds', 'builds', build_id, '[SCENE]')
            session = self._session()
            if build is None or session is None or build.get('status') == 'cancelled':
                return  # cancelled on the dashboard: nothing to say
            if build.get('status') != 'ready':
                session.notify(told(build))
                return
            await self._announced(session, told(build))
            try:
                await asyncio.to_thread(self._mac.request, 'POST', f'/api/scenes/{build.get("scene_id")}/play')
            except MacError as exc:
                logger.warning('[SCENE] the scene written was not played: %s', exc)
                session.notify(told(build, unplayed=str(exc)))
        finally:
            self._watching.pop(build_id, None)

    async def _announced(self, session: Any, text: str) -> None:
        """The note sent (at the next quiet point), then the model's sentence over: the scene never starts on it."""
        note = session.notify(text)
        if note is not None:
            await asyncio.wait([note])
        if await _within(lambda: session.state is not SessionState.IDLE, ANNOUNCE_S):
            await _within(lambda: session.state is SessionState.IDLE, SAID_S)
            await asyncio.sleep(SETTLE_S)


def told(build: dict[str, Any], unplayed: str = '') -> str:
    """The note that tells the model how a scene it asked for ended (French)."""
    if unplayed:
        return f"La scène est écrite mais n'a pas pu être jouée : {unplayed} Dis-le simplement."
    if build.get('status') != 'ready':
        return f"La scène demandée n'a pas pu être préparée : {build.get('error') or ''} Dis-le simplement."
    return f'La scène « {build.get("title") or ""} » est prête. Annonce-la en une phrase : elle commence juste après.'


async def _within(holds: Callable[[], bool], seconds: float) -> bool:
    """Whether `holds()` comes true within `seconds`, polled on the event loop."""
    deadline = time.monotonic() + seconds
    while not holds():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(POLL_S)
    return True


def tools(
    mac: Mac,
    performer: Performer,
    session: Callable[[], Any],
    lent: Callable[[], Any] = lambda: None,
    writes: Writes | None = None,
) -> Extra:
    """`play_scene`: the Mac finds the scene by its title and hands its timeline over; the answer comes at the end.
    `lent`: the profile an app holds the body with, asked again once the scene is found (a grant may land meanwhile).
    `write_scene` (with `writes`): the Mac writes a new scene; the answer says it is on its way."""
    declaration = _decl(
        'play_scene',
        "Joue une scène écrite à l'avance, avec ses voix et ses gestes enregistrés : pour « joue la scène de la "
        "fête ». N'en dis rien toi-même, ni avant ni pendant. Sans titre : la liste des scènes.",
        title=_text('Le titre tel que dit, même approximatif ; « celle-ci » : celle du dashboard.'),
        optional=('title',),
    )

    async def run(args: dict[str, Any], _send: SendImage) -> ToolResult:
        found = await asyncio.to_thread(
            mac.request, 'POST', '/api/scenes/find', {'title': str(args.get('title', ''))[:120]}
        )
        current = session()
        if not found.get('timeline') or current is None:
            titles = '\xa0; '.join(f'«\xa0{t}\xa0»' for t in found.get('titles') or []) or 'aucune'
            return ToolResult('ok', f'Scènes disponibles\xa0: {titles}. Dis-les brièvement et demande laquelle jouer.')
        if lent():  # from here to the performer's busy, nothing waits: a later grant is refused
            return _LENT
        return await performer.perform(found['timeline'], current, current.session_id)

    if writes is None:
        return {'play_scene': (declaration, run)}
    written = _decl(
        'write_scene',
        "Écris une nouvelle scène à plusieurs voix sur ce qui est demandé, jouée dès qu'elle est prête (quelques "
        'minutes) : « fais une scène où deux personnages débattent de… ».',
        request=_text('Ce que la scène doit être, avec tous les détails dits : sujet, personnages, ton.'),
    )
    once = apps.Once(mac, '/api/scenes/voice')

    async def write(args: dict[str, Any], _send: SendImage) -> ToolResult:
        request = ' '.join(str(args.get('request', '')).split())[:2000]
        if not request:
            return ToolResult('ok', "Demande ce que la scène doit raconter, puis rappelle l'outil.")
        try:
            answer = await once.send({'request': request})
        except MacError as exc:
            return ToolResult('error', str(exc))
        if answer is None:
            return apps.REPEATED
        writes.watch(answer.get('build') or {})
        return ToolResult('ok', str(answer.get('message') or "C'est lancé."))

    return {'play_scene': (declaration, run), 'write_scene': (written, write)}
