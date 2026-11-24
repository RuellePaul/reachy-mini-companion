"""The robot's life: boot, sleep until an antenna or the dashboard wakes it, converse, sleep again."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import signal
import time
from collections.abc import Awaitable
from typing import Any

from robot_agent import animation_names, animations, apps, config, gaze, live, manette, scene, speaker, studio, take
from robot_agent.adapters.robot import (
    RobotAudio,
    RobotCamera,
    camera_released,
    connect,
    emotion_library,
    reach,
    read_frame,
    read_pose,
)
from robot_agent.antennas import AntennaWiggle
from robot_agent.ear import Ear
from robot_agent.emotions import Emotions
from robot_agent.hush import Hush
from robot_agent.mac import Mac
from robot_agent.motion import MotionRunner
from robot_agent.session import Session
from robot_agent.state import SessionState
from robot_agent.tools import Tools
from robot_agent.wobble import Tap

logger = logging.getLogger(__name__)


async def main_async() -> None:
    if not config.GEMINI_API_KEY:
        raise SystemExit('GEMINI_API_KEY is missing from .env')
    if not config.MAC_AGENT_URLS:
        logger.warning('MAC_AGENT_URL is empty: no settings can be read, so every wake goes back to sleep')
    with connect() as mini:
        await Robot(mini, live.client(config.GEMINI_API_KEY), await _camera(mini)).run()


async def _camera(mini: Any) -> RobotCamera | None:
    """The camera, unless there is none, no Pillow, or the daemon lent it away (no reclaim: say what to do)."""
    if not RobotCamera.available(mini):
        logger.warning('[CAMERA] no camera or no Pillow: no `look` tool')
        return None
    if await asyncio.to_thread(camera_released):
        logger.warning(
            '[CAMERA] the daemon lent the camera to a client that never gave it back: no `look` this run. '
            'Restart the daemon to get it back; bench scripts on the robot must use `with ReachyMini(...) as mini:`.'
        )
        return None
    return RobotCamera(mini)


class Robot:
    """From boot to shutdown: the sleep loop, and one conversation at a time."""

    def __init__(self, mini: Any, client: Any, camera: RobotCamera | None) -> None:
        self._loop = asyncio.get_running_loop()
        self._mini = mini
        self._client = client
        self._mac = Mac()
        self._motion = MotionRunner(mini, reach=reach() if camera else None)
        self._audio = Tap(RobotAudio(mini), self._motion)  # the head wobbles with what the speaker plays
        self._antennas = AntennaWiggle(
            mini,
            lambda: self._loop.call_soon_threadsafe(self._trigger, 'antenna'),
            holding=lambda: self._performer.busy or self._hush.on,  # held by hand: the scene stops, the hush ends
            commanded=lambda: self._motion.mixer.antennas,
            on_hold=lambda: self._loop.call_soon_threadsafe(self._antenna_held),
            tapping=lambda: self._performer.waiting,  # a rehearsal waits for the human: a tap goes on
            on_tap=lambda: self._loop.call_soon_threadsafe(self._performer.tap),
        )
        self._emotions = Emotions(emotion_library)
        self._performer = scene.Performer(self._mac, self._audio, self._motion, self._emotions)
        self._hush = Hush(
            lambda event: self._mac.emit({**event, 'session_id': self._key('hush')}),
            ready=lambda: self._session is not None and self._session.state is SessionState.IDLE,
        )
        self._builds = apps.Builds(self._mac, lambda text: self._session and self._session.notify(text))
        self._writes = scene.Writes(self._mac, lambda: self._session)
        self._ear = Ear(self._mac.voice)
        self._manette = manette.Manette(
            mini,
            self._motion,
            lambda event: self._mac.emit({**event, 'session_id': self._key('manette')}),
            read_pose,
            refusal=self._manette_refusal,
            ear=self._ear.lend,
            read_frame=read_frame,
            pose=self._mac.pose,
        )
        extra = {
            **apps.tools(self._mac, self._builds),
            **scene.tools(self._mac, self._performer, lambda: self._session, self._lent, self._writes),
        }
        self._tools = Tools(
            body=self._motion, audio=self._audio, camera=camera, mac=self._mac, emotions=self._emotions, extra=extra,
            lent=self._lent,
        )  # fmt: skip
        # Regard actif: in a conversation only, never under a scene (its staging turns the robot) nor a held body.
        self._gaze = camera and gaze.Tracker(
            camera,
            self._motion,
            lambda: self._session_id is not None and not self._performer.busy and not self._manette.on,
        )
        self._wake = asyncio.Event()  # an antenna, a wake command or the shutdown
        self._woke_at: float | None = None  # when the pending wake was triggered
        self._stopping = False
        self._session: Session | None = None
        self._session_id: str | None = None  # from the wake to the start of the sleep move
        self._ending: str | None = None  # a sleep asked for before this wake's session exists
        self._says: list[str] = []  # typed in the dashboard before this wake's session exists
        self._settings: dict[str, Any] | None = None  # the last settings read, to connect early at the next wake
        self._scenes = 0  # dashboard scenes asked and not over: the last one out puts a sleeping robot back to sleep
        self._background: set[asyncio.Future[Any]] = set()

    async def run(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):  # SIGHUP: `make run`'s SSH session dropped
            self._loop.add_signal_handler(sig, self._shutdown)
        try:
            self._motion.start(read_pose(self._mini))
            self._mac.start(on_command=lambda c: self._loop.call_soon_threadsafe(self._command, c), beat=self._beat)
            self._emotions.start()
            self._antennas.start()
            await self._fall_asleep()
            logger.info('Asleep: wiggle an antenna or wake me from the dashboard. Ctrl-C to quit.')
            while not self._stopping:
                self._spawn(self._arm_at_rest())
                await self._wake.wait()
                self._wake.clear()
                self._antennas.disarm()
                if not self._stopping:
                    await self._cycle()
            await self._fall_asleep()  # a try or a scene played asleep may be moving it
        finally:
            self._antennas.stop()
            if self._gaze:
                self._gaze.close()
            with contextlib.suppress(Exception):  # logged: the agent's last word must not be skipped
                self._manette.take_back_now()  # never ended with a motor freed (a second Ctrl-C, a crash)
            self._motion.stop()
            await asyncio.to_thread(self._mac.close)

    async def _cycle(self) -> None:
        """One wake: the wake move, the settings fetch and the connection in parallel; then the sleep pose."""
        woke_at, self._woke_at = self._woke_at or time.monotonic(), None
        self._performer.stop('wake')  # a scene played asleep
        session_id = self._session_id = secrets.token_hex(6)
        self._ending = None
        self._mac.emit({'kind': 'session_start', 'session_id': session_id})
        moved_at = time.monotonic()  # a wake asked for during the last sleep move starts moving only now
        self._motion.set_mode('idle', blend_s=config.WAKE_S)
        self._spawn(asyncio.to_thread(speaker.play, 'wake_up.wav'))
        reason = 'error'
        try:
            reason = await self._converse(session_id, woke_at, moved_at)
        except Exception:
            logger.exception('the conversation failed')
        finally:
            if self._says:
                logger.warning('[SAY] no conversation after all: %d typed turn(s) dropped', len(self._says))
                self._says.clear()
            self._performer.stop('sleep')  # a scene the dashboard started in the conversation
            self._hush.reset()
            self._builds.clear()  # a build asked for in this conversation is no longer waited for
            self._writes.clear()  # nor a scene: written, it stays on the Mac
            with contextlib.suppress(Exception):  # logged: a torque that will not come back keeps the robot frozen
                await self._manette.leave('sleep')  # the body taken back before the sleep move
            self._mac.emit({'kind': 'session_end', 'session_id': session_id, 'reason': reason})
            self._session = self._session_id = None
            await self._fall_asleep()

    async def _converse(self, session_id: str, woke_at: float, moved_at: float) -> str:
        """Connect with the last wake's settings while this wake's are read; reopen once if they changed. Settings
        that cannot be read or applied end the wake, and the session opened early with it."""
        fetch = asyncio.ensure_future(asyncio.to_thread(self._mac.fetch_settings))
        wake = {'session_id': session_id, 'woke_at': woke_at, 'moved_at': moved_at}
        run = self._open(self._settings, settled=False, **wake) if self._settings else None
        try:
            settings = await fetch
            for mode in animation_names.MODES:
                tuning = animations.Tuning(**settings['settings']['tuning'][mode])
                self._motion.set_variant(mode, settings['settings'][f'{mode}_animation'], tuning)
            if self._gaze:
                self._gaze.enable(bool(settings['settings'].get('gaze_tracking')))
            if run is not None and self._session is not None:
                self._session.adopt(settings)
        except Exception as exc:  # noqa: BLE001 — whatever it is, the robot cannot talk without its settings
            logger.error('[WAKE] settings unavailable (%r): back to sleep', exc)
            if run is not None and self._session is not None:
                self._session.request_sleep('error')
                await run
            await asyncio.to_thread(speaker.play, 'confused1.wav')
            return 'error'
        self._settings = settings  # only settings that worked: the next wake connects early on them
        if run is None:
            if self._ending is not None:
                logger.info('[WAKE] %s before the conversation opened: back to sleep', self._ending)
                return self._ending
            run = self._open(settings, settled=True, **wake)
        reason = await run
        if reason == 'refused':  # Gemini would not talk: the robot sounds confused, as without its settings
            await asyncio.to_thread(speaker.play, 'confused1.wav')
        return reason

    def _open(self, settings: dict[str, Any], *, settled: bool, **wake: Any) -> asyncio.Task[str]:
        """This wake's session; `settled` False: opened on the last wake's settings, served once `adopt`ed."""
        self._session = Session(
            self._client,
            settings,
            audio=self._audio,
            body=self._motion,
            tools=self._tools,
            report=self._mac.emit,
            settled=settled,
            # A hush mutes it as a scene does; so does a manette held: people play, its ear may be the app's.
            performing=lambda: self._performer.busy or self._hush.active() or self._manette.on,
            held=lambda: self._builds.pending() or self._writes.pending() or self._manette.on,
            heard=self._ear.hear,
            **wake,
        )
        for text in self._says:  # typed while the settings were on their way: sent once connected
            self._session.say(text)
        self._says.clear()
        return asyncio.create_task(self._session.run(), name='session')

    async def _fall_asleep(self) -> None:
        """Reach the sleep pose. Logged, never raised. A body still held (its torque refused) is set to sleep all the
        same: nothing is written until the lease watcher takes it back, and it eases into the sleep pose then."""
        try:
            await self._manette.leave('sleep')
        except Exception as exc:  # noqa: BLE001 — held and frozen: its lease runs out, it is taken back later
            logger.warning('[MANETTE] not taken back yet (%s): the sleep pose waits for it', exc)
        try:
            self._motion.stop_moves()
            self._motion.set_mode('sleep', blend_s=config.SLEEP_S)
            self._motion.turn_body(0.0)
            if not await self._motion.settled_within(config.SLEEP_S + 3.0):
                logger.warning('the sleep pose was not reached in time')
        except Exception:
            logger.exception('the sleep move failed')

    async def _arm_at_rest(self) -> None:
        """Arm the wake antennas once every move is over and the sleep pose settled, or the settling reads as a wiggle.
        Called at the end of whatever moved the sleeping robot; never awake, stopping, or while a scene asleep plays."""
        while True:
            left = max(0.0, self._motion.moving_until() - time.monotonic())
            settled = await self._motion.settled_within(left + config.SLEEP_S + 3.0)
            await asyncio.sleep(config.SLEEP_SETTLE_S)
            if self._session_id is not None or self._stopping or self._motion.mixer.mode != 'sleep':
                return
            if settled and self._motion.settled() and not self._manette.on:  # a body still held is not at rest
                self._antennas.arm()
                return

    def _trigger(self, source: str) -> None:
        """A wake from an antenna or the dashboard; meaningless while awake."""
        if self._session_id is not None or self._stopping:
            logger.info('[WAKE] by %s ignored: %s', source, 'shutting down' if self._stopping else 'already awake')
            return
        if self._woke_at is None:
            self._woke_at = time.monotonic()
            logger.info('[WAKE] by %s', source)
        self._wake.set()

    def _end(self, reason: str) -> None:
        """End this wake: its conversation, or the conversation it is about to open."""
        if self._session is not None:
            self._session.request_sleep(reason)
        elif self._session_id is not None and self._ending is None:
            self._ending = reason

    def _command(self, command: dict[str, Any]) -> None:
        """A Mac command (contracts.Command), on the event loop."""
        action, text, session = command.get('action'), str(command.get('text') or ''), self._session
        if action == 'wake':
            self._trigger('dashboard')
        elif action == 'sleep' and self._session_id is not None:
            self._end('dashboard')
        elif action == 'try':
            self._spawn(self._try(command.get('move') or {}))
        elif action == 'app_motion' and session is not None:
            self._spawn(self._app_motion(command.get('motion') or {}))
        elif action == 'manette' and self._session_id is not None:  # awake: it says why it refuses (_manette_refusal)
            profile = command.get('manette')
            self._spawn(self._manette.command(None if profile == 'off' else profile, bool(command.get('grant'))))
        elif action == 'scene':
            self._performer.stop('replaced')  # in the order the commands came: a stop after it voids it
            self._scenes += 1
            self._spawn(self._scene(command.get('scene') or {}, self._performer.stops))
        elif action == 'scene_stop':
            self._performer.stop('dashboard')
        elif action == 'hush' and session is not None:
            self._hush.want(bool(command.get('hush')))
        elif action == 'say' and text and session is not None:
            self._performer.stop('say')  # whoever types wants the robot back
            self._hush.off('say')
            session.say(text)
        elif action == 'say' and text and self._session_id is not None:
            self._says.append(text)  # the heartbeat already says awake, so the Mac took it: keep it for the session
        else:
            logger.info('[MAC] command %s ignored (%s)', action, 'in conversation' if session else 'no conversation')

    async def _try(self, spec: dict[str, Any]) -> None:
        """The studio's « Essayer » (contracts.TryMove). Asleep, the antennas are deaf to a wake until the robot
        rests again, or the move itself would wake it."""
        move = await asyncio.to_thread(studio.build, spec, self._emotions)
        if move is None or self._stopping or self._manette.lent:
            logger.info('[TRY] %s %s: nothing to play', spec.get('kind'), spec.get('name'))
            return
        asleep = self._session_id is None
        logger.info('[TRY] %s %s%s', spec.get('kind'), spec.get('name'), ' (asleep)' if asleep else '')
        if asleep:
            self._antennas.disarm()
        self._motion.play_move(move)
        if getattr(move, 'sound_path', None):
            with contextlib.suppress(Exception):  # the move plays anyway
                await asyncio.to_thread(self._audio.play_sound, str(move.sound_path))
        if asleep:
            await self._arm_at_rest()

    async def _scene(self, timeline: dict[str, Any], asked: int) -> None:
        """A scene the dashboard plays (contracts.SceneTimeline), replacing one under way: in the conversation, or
        asleep from the sleep pose and back to it however it ends, the antennas deaf meanwhile, as for a try. A stop
        or a newer scene after `asked` (the performer's stops when it came) while it waits to start (the body coming
        back from an app, the last scene ending) voids it, a stop saying so: the latest wins, as in the Mac's queue.
        Asleep, a scene replaced (a jump) leaves the robot to the next: the last one to end, played or voided, puts it
        back to sleep."""
        asleep = self._session_id is None
        try:
            with contextlib.suppress(Exception):  # logged; a robot still held plays no scene (its motion is frozen)
                await self._manette.leave('scene')  # a scene started from the dashboard takes the body back
            while self._performer.busy and self._performer.stops == asked:
                await asyncio.sleep(0.05)
            if self._performer.stops != asked:
                logger.info('[SCENE] « %s » not played: stopped or replaced before it started', timeline.get('title'))
                if (why := self._performer.stopped_by) != 'replaced':  # replaced: the latest scene answers for both
                    self._unplayed(timeline, 'stopped', f'Arrêtée {scene.STOPPED.get(why, "en cours de route")}.')
                return
            session, asleep = self._session, self._session_id is None
            if (session is None and not asleep) or self._stopping:
                logger.info('[SCENE] « %s » ignored: the robot is waking up', timeline.get('title'))
                self._unplayed(timeline, 'failed', 'Le robot se réveille\xa0: réessaie dans un instant.')
                return
            if session is not None:
                await self._performer.perform(timeline, session, session.session_id)
                return
            self._antennas.disarm()
            self._motion.set_mode('idle', blend_s=config.WAKE_S)  # out of the sleep pose; the stage shows the rest
            stage = scene.Standalone(self._audio)
            showing = asyncio.ensure_future(stage.show(self._motion))
            try:
                await self._performer.perform(timeline, stage, self._key('scene'))
            finally:
                showing.cancel()
        finally:
            self._scenes -= 1
            # Not awake (a wake took the robot over), nor shutting down: run() takes the sleep pose last.
            if asleep and not self._scenes and self._session_id is None and not self._stopping:
                await self._fall_asleep()
                await self._arm_at_rest()

    def _unplayed(self, timeline: dict[str, Any], state: str, message: str) -> None:
        """A scene the dashboard asked for that does not start: the dashboard shows « Jouer » again, and why."""
        scene_id, title = str(timeline.get('scene_id', '')), str(timeline.get('title', ''))
        self._mac.emit({'kind': 'scene', 'session_id': self._key('scene'), 'scene_id': scene_id, 'title': title,
                        'state': state, 'message': message})  # fmt: skip

    def _antenna_held(self) -> None:
        """An antenna held by hand: a scene stops; else a hush ends, the speech given back."""
        if self._performer.busy:
            self._performer.stop('antenna')
        else:
            self._hush.off('antenna')

    def _key(self, kind: str) -> str:
        """An event's session_id: this wake's, or asleep a key of its own (the Mac refuses an event without one)."""
        return self._session_id or f'{kind}-{secrets.token_hex(4)}'

    async def _app_motion(self, motion: dict[str, Any]) -> None:
        """An app's move (contracts.AppMotion), only in conversation (the Mac refuses the others, this refuses late
        ones) and never over a scene, which refuses every tool. The comfort noise covers it like any move."""
        if motion.get('kind') == 'replay':
            await self._replay(motion)
            return
        held = self._motion.current_move()  # an app's look keeps its antennas, and its antennas keep the look
        move = await asyncio.to_thread(apps.move, motion, self._emotions, held if isinstance(held, apps.Hold) else None)
        if move is None or self._session is None or self._performer.busy or self._manette.lent:
            return
        self._motion.play_move(move)
        if getattr(move, 'sound_path', None):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._audio.play_sound, str(move.sound_path))

    async def _replay(self, motion: dict[str, Any]) -> None:
        """An app's `replay` (contracts.ReplayMotion): what the last hold taped between two times of the robot's
        clock, played as it was once the body is the robot's again (a hand still on it: it waits). No loop: the one
        playing stops. Said to the app as it starts (how long, how many times) and as it ends, whatever ends it."""
        said = {'kind': 'replay', 'session_id': self._key('replay')}
        try:
            loops, start, end = int(motion['loops']), float(motion['start']) / 1000, float(motion['end']) / 1000
        except (KeyError, TypeError, ValueError):
            return
        if loops <= 0:
            if isinstance(self._motion.current_move(), take.Take):
                self._motion.stop_moves()
            return
        deadline = time.monotonic() + manette.RETURN_WAIT_S + 5.0
        while self._manette.lent and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        taped = self._manette.tape.take(start, end, loops)
        starts = None
        if taped is not None and self._session is not None and not self._performer.busy and not self._stopping:
            starts = self._motion.play_take(taped, take.LEAD_IN_S)
        if taped is None or starts is None:
            logger.info('[REPLAY] nothing to play back (%.1f s asked)', end - start)
            self._mac.emit({**said, 'state': 'ended'})
            return
        logger.info('[REPLAY] %.2f s of the tape, %d time(s)', taped.seconds, taped.loops)
        await asyncio.sleep(max(0.0, starts - time.monotonic()))
        if self._motion.current_move() is taped:
            self._mac.emit({**said, 'state': 'started', 'seconds': round(taped.seconds, 3), 'loops': taped.loops})
        while self._motion.current_move() is taped:
            await asyncio.sleep(0.05)
        if not self._manette.lent:
            self._motion.turn_body(0.0, manette.BACK_SPEED)  # the base to the front again
        self._mac.emit({**said, 'state': 'ended'})

    def _lent(self) -> str | None:
        """The profile an app holds the body with, if it holds it: « voix » alone holds nothing."""
        return self._manette.profile if self._manette.lent else None

    def _manette_refusal(self) -> str | None:
        """Why an app may not take the body now (contracts.ManetteWhy), or None."""
        if self._performer.busy:
            return 'scene'
        return 'sleep' if self._session_id is None or self._ending is not None or self._stopping else None

    def _beat(self) -> dict[str, Any]:
        """The heartbeat's body (contracts.Heartbeat). Read from the heartbeat thread."""
        session_id = self._session_id
        armed, scene = self._antennas.armed, self._performer.on_stage
        return {'awake': session_id is not None, 'session_id': session_id, 'antenna_wake': armed, 'scene': scene}

    def _shutdown(self) -> None:
        if self._stopping:
            raise KeyboardInterrupt  # a second Ctrl-C does not wait
        logger.info('shutdown: ending the conversation, then the sleep pose')
        self._stopping = True
        self._performer.stop('sleep')
        self._end('shutdown')
        self._wake.set()

    def _spawn(self, job: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(job)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
