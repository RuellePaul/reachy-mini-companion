"""The scenes on the Mac: the built-ins and the user's own, their check, voices and timeline, and the routes of
docs/CONTRACTS.md. Every route is a plain `def` (files, the audio cache, Gemini, the motion rehearsal);
the commands they queue are handed to the event loop, where the queue lives."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import Future
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from anyio import from_thread
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from mac_server import scene_emotions, scene_text, scene_voice, scene_write
from mac_server.contracts import (
    CatalogEntry,
    SceneAsked,
    SceneBuild,
    SceneBuilds,
    SceneCheck,
    SceneEmotions,
    SceneFind,
    SceneFound,
    SceneIn,
    SceneList,
    SceneOptions,
    SceneOut,
    SceneRender,
    SceneRequest,
    SceneSummary,
    SceneTimeline,
    SceneVocabulary,
    WsSceneBuild,
    WsSceneRender,
)
from mac_server.gemini import GeminiError
from mac_server.routes import require_live
from mac_server.scene_voice import Voices
from mac_server.store import write_json_atomic

if TYPE_CHECKING:
    from mac_server.server import Mac

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent / 'assets' / 'scenes'
BUILTINS = {  # id -> title, in the dashboard's order; the text is assets/scenes/<id>.txt
    'fete-surprise': 'La fête surprise', 'voir-la-mer': 'Le robot qui voulait voir la mer',
    'presentation': 'Reachy se présente', 'dragon-enrhume': 'Le dragon enrhumé',
    'croissant-disparu': "L'affaire du croissant disparu",
}  # fmt: skip
ROUTED = {'vocabulary', 'check', 'emotions', 'find', 'stop', 'audio', 'write', 'voice', 'builds'}  # never a scene's id
WRITING = (
    "J'écris la scène\xa0: quelques minutes{seen}. Elle se jouera toute seule dès qu'elle sera prête\xa0: dis "
    "seulement que c'est lancé, en une phrase."
)
MATCH_CUTOFF = 0.5  # at least half the words of the shorter side, the title or what was said
WORD_MIN = 0.85  # two words spelled this alike (difflib) are one word misheard
# Words that say what is asked for, not which one (« joue la scène de la fête »), and those that point at the one on
# the screen (« joue celle-ci », « celle qui est à l'écran »).
_FILLER = {
    'la', 'le', 'les', 'l', 'un', 'une', 'du', 'de', 'des', 'd', 'scene', 'scenes', 'piece', 'histoire', 'sketch',
    'saynete', 'conte', 'celle', 'celui', 'avec', 'sur', 'qui', 'que', 'et', 'joue', 'raconte', 'moi', 'nous',
    'ce', 'cet', 'cette', 'ci', 'ca', 'est', 'a', 'ecran', 'affichee', 'ouverte', 'actuelle',
}  # fmt: skip


def _words(text: str) -> list[str]:
    return [w for w in re.split(r'[^a-z0-9]+', scene_text.fold(text)) if w and w not in _FILLER]


def _same(said: str, word: str) -> bool:
    """One word, said another way: another ending (« présentation », « présente »; « fêtes », « chats ») or misheard. A
    word of three letters or less is only itself: « serpent » does not start « se »."""
    # The plural's mark, then one silent ending: « chats » and « chat » both lose their t.
    said, word = (re.sub(r'(?<=...)[etd]$', '', re.sub(r'(?<=...)[sx]$', '', w)) for w in (said, word))
    if said == word or min(len(said), len(word)) <= 3:
        return said == word
    if said.startswith(word) or word.startswith(said):
        return True
    return difflib.SequenceMatcher(None, said, word).ratio() >= WORD_MIN


def _score(wanted: list[str], title: str) -> tuple[float, float]:
    """How well a title said aloud fits, word by word: its words said over the shorter side (extra words cost
    little), then, between titles that fit alike, how much of the title was said."""
    words = _words(title)
    if not wanted or not words:
        return 0.0, 0.0
    said = sum(any(_same(w, x) for x in words) for w in wanted)
    return said / min(len(wanted), len(words)), sum(any(_same(w, x) for w in wanted) for x in words) / len(words)


class Service:
    def __init__(self, mac: Mac) -> None:
        self._mac = mac
        self._lock = threading.Lock()  # the user's scenes file: read, changed, written in one go
        self._renders: dict[str, tuple[Callable[[], None], set[str]]] = {}  # each scene's latest: its frame, its lines
        self._renders_lock = threading.Lock()
        self.voices = Voices(mac.config.cache_dir / 'scene_audio', mac.config.gemini_api_key)
        self.writer = scene_write.Writer(self, self._build)

    def _build(self, build: SceneBuild) -> None:
        self._mac.broker.publish(WsSceneBuild(channel='scene_build', data=build))

    def voice(self, ask: SceneRequest) -> SceneAsked:
        """The robot's `write_scene`; the message is French, for the model. The scene is written, shown as it is
        prepared on the dashboards connected, and played by the robot once ready (robot_agent/scene.py)."""
        try:
            build = self.writer.start(ask.request, voiced=True)
        except HTTPException as exc:
            return SceneAsked(message=f'{exc.detail} Dis-le simplement.')
        seen = ', à suivre sur le dashboard' if self._mac.broker.watched() else ''
        return SceneAsked(message=WRITING.format(seen=seen), build=build)

    def catalog(self) -> list[CatalogEntry]:
        """One entry per playable scene (no error): played by the robot, or previewed in the editor."""
        voice, entries = self._narrator(), []
        for key, (title, text) in self._all().items():
            if not scene_text.parse(text, voice).errors:
                entries.append(CatalogEntry.model_validate({
                    'id': f'scene:{key}', 'group': 'scenes', 'label': title, 'tool': 'play_scene', 'preview': True,
                    'description': 'Une scène à plusieurs voix, jouée par le robot.', 'say': f'joue la scène “{title}”',
                    'action': {'kind': 'scene', 'scene_id': key}}))  # fmt: skip
        return entries

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            self.voices.close()
            scene_emotions.stop()  # no Claude reads on for a request nobody waits for
            self.writer.stop()

    # ----- the scenes, stored -----

    def _mine(self, *, writing: bool = False) -> dict[str, dict[str, str]]:
        """Yours, those that can be read. A file that is there but not wholly read (not JSON, another shape, an entry
        cut short) is never written over: `writing` then refuses, so a save cannot delete what it did not read."""
        path, why = self._mac.config.scenes_path, 'not {"scenes": [{id, title, text}, ...]}'
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raw, why = None, str(exc)
        scenes = raw.get('scenes') if isinstance(raw, dict) else None
        mine = {
            str(s['id']): {'title': str(s['title']), 'text': str(s['text'])}
            for s in (scenes if isinstance(scenes, list) else [])
            if isinstance(s, dict) and {'id', 'title', 'text'} <= s.keys()
        }
        if not isinstance(scenes, list) or len(mine) < len(scenes):
            log.warning('%s is unreadable (%s): only the scenes read are shown, and none is saved', path, why)
            if writing:
                raise HTTPException(500, detail=f"Le fichier de tes scènes ({path}) est illisible\xa0: rien n'a été "
                                    'enregistré. Corrige-le ou déplace-le, puis réessaie.')  # fmt: skip
        return mine

    def _keep(self, mine: dict[str, dict[str, str]]) -> None:
        write_json_atomic(self._mac.config.scenes_path, {'scenes': [{'id': k, **v} for k, v in mine.items()]})

    def _all(self) -> dict[str, tuple[str, str]]:
        """id -> (title, text): the built-ins in order (a copy of yours keeps its place and shadows it), then yours."""
        scenes = {key: (title, (ASSETS / f'{key}.txt').read_text(encoding='utf-8')) for key, title in BUILTINS.items()}
        return scenes | {key: (s['title'], s['text']) for key, s in self._mine().items()}

    def _get(self, scene_id: str) -> tuple[str, str]:
        scene = self._all().get(scene_id)
        if scene is None:
            raise HTTPException(404, detail=f"La scène «\xa0{scene_id}\xa0» n'existe pas.")
        return scene

    def _narrator(self) -> str:
        return self._mac.store.settings().voice  # a narrator not cast speaks with the robot's own voice

    def save(self, scene_id: str | None, body: SceneIn) -> SceneOut:
        with self._lock:
            mine = self._mine(writing=True)
            if scene_id is None:
                slug = '-'.join(_words(body.title))[:40] or 'scene'
                taken = slug in mine or slug in BUILTINS or slug in ROUTED
                scene_id = f'{slug}-{secrets.token_hex(2)}' if taken else slug
            elif scene_id not in mine and scene_id not in BUILTINS:
                raise HTTPException(404, detail=f"La scène «\xa0{scene_id}\xa0» n'existe pas.")
            mine[scene_id] = {'title': body.title, 'text': body.text}
            self._keep(mine)
        out = self.out(scene_id)
        if not out.check.errors and out.check.rendered < out.check.segments:
            self.render(scene_id)
        return out

    def delete(self, scene_id: str) -> None:
        """Yours goes; a built-in you changed comes back as shipped; a built-in as shipped stays."""
        with self._lock:
            mine = self._mine(writing=True)
            if scene_id in BUILTINS and scene_id not in mine:
                raise HTTPException(409, detail='Une scène fournie ne se supprime pas.')
            self._get(scene_id)
            del mine[scene_id]
            self._keep(mine)

    # ----- what a text says, and its voices -----

    def check(self, text: str, voice: str | None = None) -> SceneCheck:
        parsed = scene_text.parse(text, voice or self._narrator())
        ready = {cue.key: self.voices.meta(cue.key) is not None for cue in parsed.cues if cue.kind == 'say' and cue.key}
        cues = [cue.model_copy(update={'ready': ready.get(cue.key or '', False)}) for cue in parsed.cues]
        fields = {'cast': parsed.cast, 'cues': cues, 'errors': parsed.errors, 'warnings': parsed.warnings}
        return SceneCheck(**fields, segments=len(ready), rendered=sum(ready.values()))

    def out(self, scene_id: str) -> SceneOut:
        title, text = self._get(scene_id)
        builtin = scene_id in BUILTINS
        return SceneOut(id=scene_id, title=title, text=text, builtin=builtin, check=self.check(text),
                        edited=builtin and scene_id in self._mine())  # fmt: skip

    def list(self) -> SceneList:
        voice, summaries = self._narrator(), []
        for key, (title, text) in self._all().items():
            check = self.check(text, voice)
            counts = {'errors': len(check.errors), 'segments': check.segments, 'rendered': check.rendered}
            summaries.append(SceneSummary(id=key, title=title, builtin=key in BUILTINS, **counts))
        return SceneList(scenes=summaries)

    def render(self, scene_id: str, skip: str | None = None) -> None:
        """Render the missing segments (but the `skip` character's) in the background; progress on `scene_render`,
        from the scene's latest render only: an older one's lines are this one's too (one future each), or gone. A
        line it does not count (one it skips, an older render's still on its way) gives its frame, unchanged."""
        title, text = self._get(scene_id)
        cues = self._playable(title, text).cues
        says = {cue.key: cue for cue in cues if cue.kind == 'say' and cue.key and cue.character != skip}
        missing = [cue for key, cue in says.items() if self.voices.meta(key) is None]
        done = [len(says) - len(missing)]
        failed: list[str | None] = [None]

        def publish() -> None:  # the first error stays in every later frame: this render will not complete
            data = SceneRender(scene_id=scene_id, done=done[0], total=len(says), error=failed[0])
            self._mac.broker.publish(WsSceneRender(channel='scene_render', data=data))

        def finished(key: str, future: Future[None]) -> None:
            error = None if future.cancelled() else future.exception()
            if error is not None and not isinstance(error, GeminiError):
                log.error('a scene voice failed', exc_info=error)
                error = GeminiError("Une réplique n'a pas pu être rendue\xa0: regarde les logs du Mac.")
            with self._renders_lock:
                done[0] += error is None and not future.cancelled()
                failed[0] = failed[0] or (str(error) if error else None)
                latest, counted = self._renders[scene_id]
                if latest is publish or key not in counted:
                    latest()

        with self._renders_lock:
            self._renders[scene_id] = (publish, set(says))
            publish()
        for cue in missing:
            said = scene_text.speech(cue.text or '')
            future = self.voices.render(cue.key or '', cue.voice or '', cue.style or '', said)
            future.add_done_callback(partial(finished, cue.key or ''))

    def _playable(self, title: str, text: str) -> scene_text.Parsed:
        parsed = scene_text.parse(text, self._narrator())
        if parsed.errors:
            first, count = parsed.errors[0], len(parsed.errors)
            raise HTTPException(422, detail=f'La scène «\xa0{title}\xa0» a {count} erreur{"s" * (count > 1)} (ligne '
                                f'{first.line}\xa0: {first.message})\xa0: corrige-la dans le tableau de bord.')  # fmt: skip
        return parsed

    def timeline(self, scene_id: str, options: SceneOptions | None = None) -> SceneTimeline:
        """The scene on its clock, as `options` play it (from its `start` line on); 409 while voices are missing,
        which are rendered meanwhile. A rehearsed character needs none: the human says that part, nor a line before
        the start."""
        options = options or SceneOptions()
        title, text = self._get(scene_id)
        parsed = self._playable(title, text)
        if options.rehearse is not None and options.rehearse not in {member.name for member in parsed.cast}:
            raise HTTPException(
                422, detail=f"«\xa0{options.rehearse}\xa0» n'est pas un personnage de «\xa0{title}\xa0»."
            )
        says = [cue for cue in parsed.cues if cue.kind == 'say' and (cue.end_line or cue.line) >= (options.start or 1)]
        if not says:
            raise HTTPException(422, detail=f'Rien à jouer à partir de la ligne {options.start} de «\xa0{title}\xa0».')
        voiced = [cue for cue in says if cue.key and cue.character != options.rehearse]
        metas = {cue.key: self.voices.meta(cue.key) for cue in voiced}
        ready = sum(meta is not None for meta in metas.values())
        if ready < len(metas):
            why = self.voices.failing([key for key, meta in metas.items() if meta is None])
            self.render(scene_id, skip=options.rehearse)
            if why:
                raise HTTPException(409, detail=f'Les voix de «\xa0{title}\xa0» ne sont pas prêtes ({ready}/'
                                    f'{len(metas)}). {why}')  # fmt: skip
            raise HTTPException(409, detail=f'Les voix de «\xa0{title}\xa0» se préparent ({ready}/{len(metas)})\xa0: réessaie '
                                'dans une minute.')  # fmt: skip
        voices = {
            key: scene_text.Voice(**{k: meta[k] for k in scene_text.Voice._fields}) for key, meta in metas.items()
        }
        laid = scene_text.timeline(scene_id, title, parsed, lambda key: voices.get(key, scene_text.Voice(0.0)),
                                   options.rehearse)  # fmt: skip
        if options.start is not None:
            laid = scene_text.starting(laid, says[0].line)
        return laid.model_copy(update=options.model_dump())

    def find(self, title: str) -> SceneFound:
        """The scene a spoken title means (play_scene), as a timeline; an empty title lists the playable ones. A title
        that names none (« celle-ci », « la scène ») means the scene a dashboard shows, else lists them too."""
        voice, scenes, wanted = self._narrator(), self._all(), _words(title)
        shown = self._mac.broker.showing('scene') if title.strip() and not wanted else None
        if shown in scenes:
            return SceneFound(timeline=self.timeline(shown))
        titles = [t for t, text in scenes.values() if not scene_text.parse(text, voice).errors]
        if not wanted:
            return SceneFound(titles=titles)
        (fit, _), scene_id = max(((_score(wanted, t), key) for key, (t, _) in scenes.items()), key=lambda p: p[0])
        if fit < MATCH_CUTOFF:
            listed = '\xa0; '.join(f'«\xa0{t}\xa0»' for t in titles) or 'aucune'
            raise HTTPException(
                404, detail=f'Aucune scène ne ressemble à «\xa0{title}\xa0». Scènes disponibles\xa0: {listed}.'
            )
        return SceneFound(timeline=self.timeline(scene_id))

    def command(self, action: str, timeline: SceneTimeline | None = None) -> None:
        """Queue `scene` or `scene_stop` for a running robot."""
        require_live(self._mac)
        from_thread.run_sync(partial(self._mac.commands.push, action, scene=timeline))


def routes(mac: Mac) -> APIRouter:
    router = APIRouter()
    scenes = mac.scenes

    @router.get('/api/scenes')
    def list_scenes() -> SceneList:
        return scenes.list()

    @router.post('/api/scenes')
    def create_scene(body: SceneIn) -> SceneOut:
        return scenes.save(None, body)

    @router.get('/api/scenes/vocabulary')
    def vocabulary() -> SceneVocabulary:
        return scene_text.vocabulary()

    @router.post('/api/scenes/check')
    def check(body: SceneIn) -> SceneCheck:
        return scenes.check(body.text)

    @router.post('/api/scenes/emotions')
    def emotions(body: SceneIn) -> SceneEmotions:
        return scene_emotions.add(body.title, body.text)

    @router.post('/api/scenes/find')
    def find(body: SceneFind) -> SceneFound:
        return scenes.find(body.title)

    @router.post('/api/scenes/write')
    def write(body: SceneRequest) -> SceneBuild:
        return scenes.writer.start(body.request)

    @router.post('/api/scenes/voice')
    def voice(body: SceneRequest) -> SceneAsked:
        return scenes.voice(body)

    @router.get('/api/scenes/builds')
    def builds() -> SceneBuilds:
        return SceneBuilds(builds=scenes.writer.builds())

    @router.post('/api/scenes/builds/{build_id}/cancel', status_code=204)
    def cancel_build(build_id: str) -> None:
        scenes.writer.cancel(build_id)

    @router.post('/api/scenes/stop', status_code=204)
    def stop() -> None:
        scenes.command('scene_stop')

    @router.get('/api/scenes/audio/{key}')
    def audio(key: str, rate: Annotated[int | None, Query(ge=8000, le=48000)] = None) -> Response:
        """The voice as rendered, or resampled to `rate` (the robot's speaker): less to send, nothing to compute there."""
        path = scenes.voices.path(key)
        if path is None:
            raise HTTPException(404, detail="Cette réplique n'est pas encore rendue.")
        return Response(scene_voice.at_rate(path.read_bytes(), rate), media_type='audio/wav')

    @router.get('/api/scenes/{scene_id}')
    def get_scene(scene_id: str) -> SceneOut:
        return scenes.out(scene_id)

    @router.put('/api/scenes/{scene_id}')
    def put_scene(scene_id: str, body: SceneIn) -> SceneOut:
        return scenes.save(scene_id, body)

    @router.delete('/api/scenes/{scene_id}', status_code=204)
    def delete_scene(scene_id: str) -> None:
        scenes.delete(scene_id)

    @router.post('/api/scenes/{scene_id}/play', status_code=204)
    def play(scene_id: str, options: SceneOptions | None = None) -> None:
        scenes.command('scene', scenes.timeline(scene_id, options))  # the voices first: rendered, robot on or off

    return router
