"""A scene written at someone's ask (« fais une scène où deux personnages débattent de… »), from the dashboard or by
voice. Claude writes the play, streamed; the grammar reads it, and a play it refuses goes back once with its errors.
Stored, the scene gets its emotions (Claude again, among numbered places: scene_emotions) and its voices (Gemini:
scene_voice) side by side: neither waits for the other, a move changes no voice. Progress goes out on `scene_build`,
the latest only. One scene at a time; a scene written stays in the store however the rest ends."""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from mac_server import claude, scene_emotions, scene_text
from mac_server.claude import Cancelled, ClaudeError
from mac_server.contracts import SCENE_TITLE_MAX, SceneBuild, SceneIn
from mac_server.gemini import GeminiError
from mac_server.voices import VOICES

if TYPE_CHECKING:
    from mac_server.scenes import Service

log = logging.getLogger(__name__)

EFFORT = 'medium'  # a play in ~30 s; low writes one in ~20 s, flatter
WRITE_TIMEOUT_S = 300.0
PUBLISH_S = 0.25  # the progress goes out at most this often, the latest only
KEPT = 5  # builds ended and still listed: whoever watched one reads how it ended
_PROMPT = (Path(__file__).parent / 'assets' / 'scene_prompt.txt').read_text(encoding='utf-8')
# What a refused play goes back with: the ask, the play as written, why the grammar refused it.
_AGAIN = (
    '{ask}\n\nTa scène :\n\nTITRE : {title}\n\n{text}\nElle a été refusée : {wrong}.\nRéécris-la entière, corrigée.'
)
_TITLE = re.compile(r'^\s*(?:TITRE|Titre)\s*:\s*(.+?)\s*$', re.MULTILINE)
BUSY = "Une scène est déjà en cours d'écriture\xa0: attends qu'elle soit prête."
NOT_BUILDING = "Cette scène n'est plus en préparation."
CRASHED = 'La préparation de la scène a planté\xa0: regarde les logs du Mac.'
EDITED = "La scène a été modifiée entre-temps\xa0: ses émotions n'ont pas été ajoutées."
UNUSABLE = "Claude n'a rien proposé d'utilisable."


def system() -> str:
    """The author's prompt, with the voices to cast and the sounds a voice can make."""
    voices = '\n'.join(f'{n} | {"femme" if g == "female" else "homme"} | {d}' for n, (g, d) in sorted(VOICES.items()))
    sounds = ', '.join(f'<{word}>' for word in scene_text.SOUNDS)
    return _PROMPT.replace('{sounds}', sounds).replace('{voices}', voices)


def read(answer: str) -> tuple[str, str]:
    """(title, text) of what Claude wrote: its `TITRE :` line, then the play; a code fence around it is dropped."""
    body = re.sub(r'^\s*```[a-z]*\n|\n```\s*$', '', answer.strip())
    title = _TITLE.search(body)
    if title is None or not body[title.end() :].strip():
        raise ClaudeError("Claude n'a pas écrit de scène\xa0: réessaie, avec d'autres mots.")
    return _named(title), body[title.end() :].strip() + '\n'


def _named(title: re.Match[str]) -> str:
    return title.group(1).strip('«»"\' \xa0')[:SCENE_TITLE_MAX] or 'Sans titre'


def misread(text: str, cast: set[str]) -> list[str]:
    """The lines that open on a character's name and are not read as that character's: something stands between the
    name and its colon (a sound, a second name). The grammar takes such a line for more of the line before: its words
    would be said, name included, by whoever spoke last."""
    wrong = []
    for number, raw in enumerate(text.split('\n'), 1):
        line = raw.strip()
        name = next((n for n in cast if line.upper().startswith(n) and not line[len(n) : len(n) + 1].isalnum()), None)
        role = scene_text._ROLE.match(line)
        if name and (role is None or role.group(1).strip().upper() not in cast):
            wrong.append(
                f'ligne {number}\xa0: entre «\xa0{name}\xa0» et ses deux-points, seule une indication entre parenthèses '
                '(un son va dans la réplique, un seul personnage la dit)'
            )
    return wrong


class Build:
    """One scene on its way: what `SceneBuild` says of it, published as it changes, and `cancel` from any thread."""

    def __init__(self, request: str, voiced: bool, publish: Callable[[SceneBuild], None]) -> None:
        self.id = secrets.token_hex(8)
        self._fields: dict[str, Any] = {
            'id': self.id, 'request': request, 'voiced': voiced, 'status': 'building', 'started_at': time.time(),
        }  # fmt: skip
        self._publish = publish
        self._lock = threading.Lock()
        self._run: claude.Run | None = None  # the author's: the one a progress is read from
        self._runs: set[claude.Run] = set()
        self._cost = 0.0
        self.cancelled = False
        self._dirty, self._done = threading.Event(), threading.Event()
        threading.Thread(target=self._publisher, name=f'scene-{self.id}-progress', daemon=True).start()

    def set(self, **fields: Any) -> None:
        with self._lock:
            self._fields |= fields
        self._dirty.set()

    def snapshot(self) -> SceneBuild:
        with self._lock:
            fields, run = dict(self._fields), self._run
            cost = round(self._cost, 4) or None
        if run is not None:  # the play as it is written: its title as soon as that line is whole
            written = run.progress.text
            title = _TITLE.match(written)
            fields |= {
                'phase': run.progress.phase if run.progress.phase in ('thinking', 'writing') else 'starting',
                'thinking_tokens': run.progress.thinking_tokens,
                'text': _shown(written),
            }
            if title and '\n' in written[title.start() :]:
                fields['title'] = _named(title)
        return SceneBuild(**fields, cost_usd=cost)

    def claude(self, text: str, prompt: str, effort: str, what: str, *, shown: bool = False) -> str:
        """One Claude run of this build, ended by `cancel`; `shown`: its progress is the build's (the play)."""
        with self._lock:
            if self.cancelled:
                raise Cancelled('Écriture annulée.')
            run = claude.Run(
                text, prompt, effort=effort, timeout_s=WRITE_TIMEOUT_S, what=what,
                on_progress=lambda _progress: self._dirty.set() if shown else None,
            )  # fmt: skip
            self._runs.add(run)
            if shown:
                self._run = run
        try:
            return run.wait().text
        finally:
            with self._lock:
                self._cost += run.cost_usd
                self._runs.discard(run)

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            runs = list(self._runs)
        for run in runs:
            run.cancel()

    def written(self, **fields: Any) -> None:
        """The author is done: what is shown from now on is the stored scene."""
        with self._lock:
            self._run = None
            self._fields |= fields
        self._dirty.set()

    def end(self, status: str, error: str | None = None) -> None:
        steps = {step: 'failed' for step in ('write', 'stage', 'voice') if self._fields.get(step) == 'running'}
        self.written(status=status, error=error, ended_at=time.time(), **(steps if status != 'ready' else {}))
        self._done.set()
        self._dirty.set()

    def _publisher(self) -> None:
        """The latest snapshot, at most every PUBLISH_S; the build's last state always goes out."""
        self._publish(self.snapshot())
        while not self._done.is_set():
            self._dirty.wait()
            self._dirty.clear()
            self._publish(self.snapshot())
            self._done.wait(PUBLISH_S)
        self._publish(self.snapshot())


def _shown(text: str) -> str:
    """A play being written, as the dashboard shows it: without its `TITRE :` line once that one is whole."""
    title = _TITLE.match(text)
    return text[title.end() :].lstrip('\n') if title and '\n' in text[title.start() :] else ''


class Writer:
    """The scenes being written: one at a time, and the last few that ended."""

    def __init__(self, scenes: Service, publish: Callable[[SceneBuild], None]) -> None:
        self._scenes, self._publish = scenes, publish
        self._lock = threading.Lock()
        self._build: Build | None = None
        self._ended: deque[Build] = deque(maxlen=KEPT)
        self._stopped = False

    def start(self, request: str, voiced: bool = False) -> SceneBuild:
        with self._lock:
            if self._build is not None:
                raise HTTPException(409, detail=BUSY)
            build = self._build = Build(' '.join(request.split()), voiced, self._publish)
        threading.Thread(target=self._work, args=(build,), name=f'scene-{build.id}', daemon=True).start()
        return build.snapshot()

    def builds(self) -> list[SceneBuild]:
        with self._lock:
            listed = [*([self._build] if self._build else []), *reversed(self._ended)]
        return [build.snapshot() for build in listed]

    def cancel(self, build_id: str) -> None:
        with self._lock:
            build = self._build
        if build is None or build.id != build_id:
            raise HTTPException(409, detail=NOT_BUILDING)
        build.cancel()

    def stop(self) -> None:
        """The server stops: no Claude writes on for nobody."""
        with self._lock:
            self._stopped, build = True, self._build
        if build is not None:
            build.cancel()

    # ----- one scene, on its own thread -----

    def _work(self, build: Build) -> None:
        try:
            title, text = self._write(build)
            scene = self._scenes.save(None, SceneIn(title=title, text=text))  # stored: its voices start rendering
            build.written(
                write='done', stage='running', voice='running', scene_id=scene.id, title=scene.title,
                text=scene.text, voices_total=scene.check.segments, voices_done=scene.check.rendered,
            )  # fmt: skip
            staging = threading.Thread(
                target=self._stage, args=(build, scene.id, scene.title, scene.text), name=f'scene-{build.id}-stage'
            )
            staging.start()
            try:
                self._voices(build, scene.text)
            except BaseException:
                build.cancel()  # the director's run too: the scene will not play now
                raise
            finally:
                staging.join()
            build.end('ready')
        except Cancelled:
            build.end('cancelled')
        except (ClaudeError, GeminiError) as exc:
            build.end('failed', str(exc))
        except HTTPException as exc:  # the store refused the scene (its file unreadable)
            build.end('failed', str(exc.detail))
        except Exception:  # a crash must still end the build, as a failure
            log.exception('scene build %s crashed', build.id)
            build.end('failed', CRASHED)
        finally:
            with self._lock:
                self._build = None
                self._ended.append(build)

    def _write(self, build: Build) -> tuple[str, str]:
        """The play, as the grammar takes it; one it refuses is written once more, with what was wrong."""
        build.set(write='running')
        ask = f'Demande\xa0: «\xa0{build.snapshot().request}\xa0»'
        title, text = read(build.claude(ask, system(), EFFORT, 'la scène', shown=True))
        wrong = self._wrong(text)
        if wrong:
            log.warning('scene build %s: the play was refused (%s): written once more', build.id, '; '.join(wrong))
            build.written(round=2, problems=wrong, phase='starting', text='')
            again = _AGAIN.format(ask=ask, title=title, text=text, wrong='\xa0; '.join(wrong))
            title, text = read(build.claude(again, system(), EFFORT, 'la scène', shown=True))
            wrong = self._wrong(text)
        if wrong:
            raise ClaudeError('La scène écrite ne peut pas se jouer\xa0: ' + '\xa0; '.join(wrong) + '.')
        return title, text

    def _wrong(self, text: str) -> list[str]:
        check = self._scenes.check(text)
        wrong = [f'ligne {error.line}\xa0: {error.message}' for error in check.errors]
        wrong += misread(text, {member.name.upper() for member in check.cast})
        return wrong[:6] or ([] if check.segments else ["elle n'a aucune réplique"])

    def _stage(self, build: Build, scene_id: str, title: str, text: str) -> None:
        """The emotions, written into the stored scene if it is still as Claude read it. Without them the scene
        plays all the same: a failure here is a warning."""
        try:
            answer = build.claude(scene_emotions.asked(title, text), scene_emotions.system(), scene_emotions.EFFORT,
                                  'les émotions')  # fmt: skip
            staged, added = scene_emotions.write_in(text, scene_emotions.picks(answer))
            if added and self._scenes.out(scene_id).text != text:
                build.set(stage='failed', warning=EDITED)
                return
            if added:
                self._scenes.save(scene_id, SceneIn(title=title, text=staged))
            build.set(stage='done', moves=added, **({'text': staged} if added else {}))
        except Cancelled:
            build.set(stage='failed')
        except (ClaudeError, ValueError, HTTPException) as exc:
            log.warning('scene build %s: no emotions (%s)', build.id, exc)
            why = getattr(exc, 'detail', None) or (str(exc) if isinstance(exc, ClaudeError) else UNUSABLE)
            build.set(stage='failed', warning=f'Sans émotions\xa0: {why}')
        except Exception:
            log.exception('scene build %s: the emotions crashed', build.id)
            build.set(stage='failed', warning='Sans émotions\xa0: regarde les logs du Mac.')

    def _voices(self, build: Build, text: str) -> None:
        """Every line's voice, rendered or failed: the futures are the store's own render's."""
        voices = self._scenes.voices
        says = {cue.key: cue for cue in self._scenes.check(text).cues if cue.kind == 'say' and cue.key}
        pending: dict[Future[None], str] = {}
        for key, cue in says.items():
            if voices.meta(key) is None:
                said = scene_text.speech(cue.text or '')
                pending[voices.render(key, cue.voice or '', cue.style or '', said)] = key
        if pending and (why := voices.failing(list(pending.values()))):
            raise GeminiError(why)
        while pending:
            if build.cancelled:
                raise Cancelled('Écriture annulée.')
            done, _ = wait(pending, timeout=PUBLISH_S, return_when=FIRST_COMPLETED)
            for future in done:
                del pending[future]
                error = None if future.cancelled() else future.exception()
                if isinstance(error, GeminiError):
                    raise error
                if error is not None or future.cancelled():
                    log.error('scene build %s: a voice failed', build.id, exc_info=error)
                    raise GeminiError("Une réplique n'a pas pu être rendue\xa0: regarde les logs du Mac.")
            if done:
                build.set(voices_done=len(says) - len(pending))
        build.set(voice='done', voices_done=len(says), voices_total=len(says))
