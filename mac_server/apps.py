"""Apps: single-file pages Claude writes on request (by voice or from the dashboard), kept in `config.apps_dir`
(`<id>.html` and `index.json`), beside those shipped with the robot (`assets/apps`, BUILTINS: what you change of one
is kept as yours and shadows it), run by the dashboard in a sandboxed srcdoc frame under a CSP, and allowed to move
the robot through a small versioned bridge (docs/CONTRACTS.md § App bridge). Every Claude build blocks for minutes: builds run
in a thread, the routes that touch a file are plain `def`."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import ConfigDict, ValidationError

from mac_server.app_build import CSP, Build, check, extract, quiet_of, served, title_of
from mac_server.claude import Cancelled, ClaudeError
from mac_server.contracts import (
    APP_MODELS,
    MANETTE_TTL_S,
    AppAction,
    AppBuild,
    AppEvent,
    AppHtml,
    AppList,
    AppQuiet,
    AppRename,
    AppRequest,
    AppSummary,
    AppVoice,
    AppVoiceOut,
    BridgeIn,
    CatalogEntry,
    ManetteIn,
    WsAppBuild,
    WsApps,
)
from mac_server.hush import Hush
from mac_server.store import write_json_atomic, write_text_atomic

if TYPE_CHECKING:
    from mac_server.server import Mac

log = logging.getLogger(__name__)

BUILDS_MAX = 2  # at once
ID = re.compile(r'[0-9a-f]{16}')
ASSETS = Path(__file__).resolve().parent / 'assets' / 'apps'
# The apps shipped with the robot: id -> (title, what it is and the manette it asks for: a retouch's first request).
# The page is assets/apps/<id>.html.
BUILTINS = {
    'slalom': ('Slalom', 'Un slalom à ski vu du dessus, où Reachy est le skieur, sa base tournée à la main (volant).'),
    'vol-de-nuit': ('Vol de nuit', 'Un planeur de nuit entre des portes de lumière, guidé par sa tête (tete).'),
    'tam-tam': ('Tam-tam', 'Un jeu de rythme où les deux antennes de Reachy sont deux tambours (gachettes).'),
    'marionnette': ('Marionnette', "Une mesure de gestes faite à la main sur la tête de Reachy, qu'il refait (tete)."),
    'manhattan': ('Minuit à Manhattan', 'Un vol en 3D entre les gratte-ciel, la nuit, sa tête pour manche (tete).'),
    'avant-la-nuit': ('Avant la nuit', 'Une route de corniche en 3D au couchant, Reachy pour volant (paume).'),
    'bille-en-tete': ('Bille en tête', 'Une bille menée par sa tête sur un parcours 3D suspendu, dix niveaux (tete).'),
    'banc-de-manette': ('Banc de manette', 'Chaque manette essayée, mesurée en direct, avec sa liaison et son rejeu.'),
    'jardin-abyssal': ('Jardin abyssal', 'Un monde sous la mer en 3D, parcouru à la nage, sa tête pour manche (tete).'),
}
# Per app: at most `count` messages of a kind in `window_s` (emotions and gestures share one budget: both are moves).
# Each one a margin over the dashboard's pacing (a pose per 200 ms tick, a move per 1.6 s): its ticks jitter.
RATES = {'look': (6, 1.0), 'antennas': (6, 1.0), 'move': (1, 1.5)}
# Every dashboard connected runs an app opened by voice: the robot follows one dashboard's app at a time, the one that
# moved it within HOLD_S. More than the longest a paced app is still while it plays (a move's 1.6 s, then a tick).
HOLD_S = 2.0
NOT_FOUND = "Cette app n'existe pas (ou plus)."
SHIPPED = 'Une app fournie ne se supprime pas.'
LATER = "Elle n'est pas encore prête\xa0: une note te dira quand elle le sera. Dis seulement que c'est lancé."
# On the dashboard, not shown there: a dashboard holding unsaved work offers it instead, which the Mac cannot tell.
BUILDING = "Je construis l'app (quelques minutes), sur le dashboard pendant qu'elle s'écrit."
UNSEEN = "mais aucun dashboard n'est ouvert pour la montrer"  # an `open` reaches only the dashboards connected
ASLEEP = "Reachy dort\xa0: réveille-le pour qu'il bouge avec l'app."
MANETTE_ASLEEP = "Reachy dort\xa0: réveille-le pour t'en servir comme manette."
MANETTE_TAKEN = 'Une autre app se sert déjà de Reachy comme manette.'
TOO_FAST = "L'app envoie trop de mouvements\xa0: Reachy saute les derniers."
ELSEWHERE = "Reachy suit déjà l'app d'un autre écran."
BUSY = "Deux apps sont déjà en construction\xa0: attends que l'une soit prête."
INTERRUPTED = 'La construction a été interrompue\xa0: le Mac a redémarré.'
UNSAVED = "La page n'a pas pu être enregistrée\xa0: regarde les logs du Mac."
CANCELLED = 'Modification annulée.'
UNREADABLE = (
    "Le fichier de tes apps ({path}) est illisible\xa0: rien n'a été enregistré. "
    'Corrige-le ou déplace-le, puis réessaie.'
)
NOT_BUILDING = "Cette app n'est pas en construction."
# Said around a name, never in it: articles, pointing words, the words of « l'app qu'on a faite tout à l'heure ».
# Accented as said: « dés » is no « des ».
STOP_WORDS = frozenset(
    'le la les l un une des du de d mon ma mes ton ta tes ce cet cette celle celui ci là qu que qui on tu as a à '  # noqa: SIM905
    'vient venons faire fait faite faits tout heure hier ouverte ouvert affichée écran actuelle '
    'app appli application jeu'.split()
)
# They point too (« c'est celle sur le dashboard », « la dernière »): said alone, they name nothing; around a name,
# they only choose between titles it names alike. A title may hold them, so fold() keeps them: « Dernier Pong » is no
# « Pong ». Not « précédente », « avant »: they point past the latest.
POINTING = frozenset('c est ça sur viens créer créé créée nouvelle dernière dernier dashboard'.split())  # noqa: SIM905
WORD_MIN = 0.75  # two words this alike (difflib, as they sound) are the same word misheard
MATCH_MIN = 0.5  # at least half of what was said is in the title
CLOSE = 0.2  # titles this close to the best one are candidates too


__all__ = ['CSP', 'check', 'extract', 'served', 'title_of']  # the page's side, kept at hand for the tests
BuildError = ClaudeError  # why a page was not built, in French


class Entry(AppSummary):
    """An app as index.json keeps it: its summary, and what it was first asked to be. A key another version wrote
    is kept as it is."""

    model_config = ConfigDict(extra='allow')
    prompt: str = ''


def known(app_id: str) -> bool:
    """Whether this may be an app's id: never a path from an unchecked name."""
    return bool(ID.fullmatch(app_id)) or app_id in BUILTINS


def fold(text: str) -> str:
    """A title as said: no case, none of the words said around a name, then no accents. A title made of nothing
    else (« Mon jeu ») keeps its words."""
    said = re.findall(r'[^\W_]+', text.lower())
    kept = [word for word in said if word not in STOP_WORDS] or said
    plain = unicodedata.normalize('NFKD', ' '.join(kept)).encode('ascii', 'ignore').decode()
    return ' '.join(re.findall(r'[a-z0-9]+', plain))


def words(text: str) -> list[str]:
    """A name as said, word by word, each as it sounds (`sound`)."""
    return [sound(word) for word in fold(text).split()]


def named(text: str) -> bool:
    """Whether what was said names something: « celle-ci », « l'app ouverte », « la dernière » name nothing."""
    return any(word not in STOP_WORDS | POINTING for word in re.findall(r'[^\W_]+', text.lower()))


def sound(word: str) -> str:
    """A French word as it sounds, roughly: what a transcription may spell another way (« croc » and « croque »,
    « pomme » and « pommes », « snak » and « snake ») comes out the same."""
    for spelled, said in (('ph', 'f'), ('qu', 'k'), ('ck', 'k'), ('ch', 'sh'), ('y', 'i'), ('w', 'v'), ('z', 's')):
        word = word.replace(spelled, said)
    word = re.sub(r'c(?![eis])', 'k', word).replace('c', 's')
    word = re.sub(r'(.)\1+', r'\1', word)
    word = re.sub(r'e([nm])(?![aeiou])', r'a\1', word)  # « en » and « an » sound alike
    return re.sub(r'(?<=..)(?:es|[esxtd])$', '', word) or word  # one silent ending: « test » is no « tête »


POINTED = frozenset(fold(' '.join(POINTING)).split())  # as fold() spells them: « cas » only sounds like « ça »


def overlap(said: list[str], title: list[str]) -> tuple[float, float]:
    """How much of what was said the title holds, and how much of the title was said (each 0..1): every said word
    takes the title word closest to it, once."""
    left, found = list(title), 0.0
    for word in said:
        likeness = [(1.0 if word == other else difflib.SequenceMatcher(None, word, other).ratio(), i)
                    for i, other in enumerate(left)]  # fmt: skip
        if likeness and (best := max(likeness))[0] >= WORD_MIN:
            found += best[0]
            left.pop(best[1])
    return found / len(said), (found / len(title) if title else 0.0)


class Service:
    """The app store, the builds in flight, and the bridge's rate limits and the dashboard it follows."""

    def __init__(self, mac: Mac, clock: Callable[[], float] = time.monotonic) -> None:
        self._mac = mac
        self._dir: Path = mac.config.apps_dir
        self._lock = threading.Lock()  # every read-modify-write of index.json
        self._builds: dict[str, Build] = {}  # in flight, by app id
        self._stopped = False  # the server stopped: a build that ends leaves index.json as it is
        self._ended: dict[str, Entry] = {}  # a build's end index.json did not take, until a write takes it
        self._opened: dict[str, float] = {}  # id -> when the dashboard last opened it (epoch s)
        self.hush = Hush(mac)
        self._sent: dict[tuple[str, str], deque[float]] = {}  # (app, kind) -> recent bridge messages
        self._manette: tuple[str, float] | None = None  # the app holding the robot as its controller, last renewed
        self._follows: tuple[str, float] | None = None  # the dashboard whose app moved the robot last, and when
        self._clock = clock

    # ----- the store

    def _page(self, app_id: str) -> Path:
        return self._dir / f'{app_id}.html'

    def _source(self, app_id: str) -> Path | None:
        """The page's file: yours, else the one shipped; None when it has neither."""
        for path in (self._page(app_id), *([ASSETS / f'{app_id}.html'] if app_id in BUILTINS else [])):
            if path.is_file():
                return path
        return None

    def _shipped(self) -> dict[str, Entry]:
        """The apps shipped with the robot as entries, dated by their page's file."""
        shipped = {}
        for key, (title, what) in BUILTINS.items():
            if (page := ASSETS / f'{key}.html').is_file():
                at = page.stat().st_mtime
                shipped[key] = Entry(id=key, title=title, status='ready', created_at=at, updated_at=at, prompt=what)
        return shipped

    def _all(self) -> dict[str, Entry]:
        """Every app: those shipped, each shadowed by what you changed of it, then yours."""
        return self._shipped() | self._read()

    def _read(self, *, writing: bool = False) -> dict[str, Entry]:
        """index.json, entry by entry (yours, and what you changed of a shipped app): a missing or broken file is an
        empty store, a bad entry is skipped. An index not read in full is never written over: `writing` then refuses,
        so a write cannot erase what it skipped."""
        path = self._dir / 'index.json'
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raw, why = None, str(exc)
        else:
            why = 'not {"apps": [...]}, or an entry skipped'
        items = raw.get('apps', []) if isinstance(raw, dict) else None
        entries: dict[str, Entry] = {}
        for item in items if isinstance(items, list) else []:
            try:
                entry = Entry.model_validate(item)
            except ValidationError:
                log.warning('index.json: an invalid app entry is skipped: %.200s', item)
                continue
            if known(entry.id):
                ended = self._ended.get(entry.id)
                entries[entry.id] = ended if ended is not None and entry.status == 'building' else entry
        if not isinstance(items, list) or len(entries) < len(items):
            log.warning('%s is unreadable (%s): only the apps read are shown, and none is saved', path, why)
            if writing:
                raise HTTPException(500, detail=UNREADABLE.format(path=path))
        return entries

    def _save(self, entries: dict[str, Entry]) -> None:
        kept = [entry.model_dump(exclude={'builtin', 'edited'}) for entry in entries.values()]  # said by `_view`
        write_json_atomic(self._dir / 'index.json', {'apps': kept})
        self._ended.clear()  # read into `entries`, now written

    def _view(self, entry: Entry) -> AppSummary:
        """What the dashboard sees: a build no thread is running was cut short by a restart; a shipped app says so,
        and whether you changed it."""
        fields = entry.model_dump(include=set(AppSummary.model_fields))
        if entry.status == 'building' and entry.id not in self._builds:
            fields |= {'status': 'ready' if self._source(entry.id) else 'failed', 'error': INTERRUPTED}
        shipped = self._shipped().get(entry.id)
        fields |= {'builtin': shipped is not None, 'edited': shipped is not None and entry != shipped}
        return AppSummary.model_validate(fields)

    def _progress(self, snapshot: AppBuild) -> None:
        self._mac.broker.publish(WsAppBuild(channel='app_build', data=snapshot))

    def _publish(self, app: AppSummary, *, open: bool = False, deleted: bool = False) -> None:
        self._mac.broker.publish(WsApps(channel='apps', data=AppEvent(app=app, open=open, deleted=deleted)))

    def apps(self) -> list[AppSummary]:
        with self._lock:
            listed = [self._view(entry) for entry in self._all().values()]
        return sorted(listed, key=lambda a: a.updated_at, reverse=True)

    def html(self, app_id: str) -> AppHtml:
        with self._lock:
            page = self._source(app_id) if known(app_id) and app_id in self._all() else None
        if page is None:
            raise HTTPException(404, detail="Cette app n'a pas encore de page.")
        self._opened[app_id] = time.time()
        return AppHtml(id=app_id, html=served(page.read_text(encoding='utf-8')))

    def rename(self, app_id: str, title: str) -> AppSummary:
        return self._change(app_id, title=title)

    def set_quiet(self, app_id: str, quiet: bool) -> AppSummary:
        """Whether the robot keeps silent while the app is on screen; the dashboard's choice wins over the builder's."""
        return self._change(app_id, quiet=quiet)  # the hush follows at its next step

    def quiet(self, app_id: str) -> bool:
        """Whether the robot keeps silent while this app is on screen (an unknown app: no)."""
        with self._lock:
            entry = self._all().get(app_id)
        return entry is not None and entry.quiet

    def _change(self, app_id: str, **fields: Any) -> AppSummary:
        with self._lock:
            entries = self._read(writing=True)
            entry = entries.get(app_id) or self._shipped().get(app_id)  # a shipped app changed becomes yours
            if entry is None:
                raise HTTPException(404, detail=NOT_FOUND)
            entries[app_id] = entry.model_copy(update=fields)
            self._save(entries)
        app = self._view(entries[app_id])
        self._publish(app)
        return app

    def delete(self, app_id: str) -> None:
        """Out of the index, its page with it. A shipped app you changed comes back as shipped; as shipped, it stays."""
        with self._lock:
            entries = self._read(writing=True)
            entry, shipped = entries.pop(app_id, None), self._shipped().get(app_id)
            if entry is None:
                raise HTTPException(409 if shipped else 404, detail=SHIPPED if shipped else NOT_FOUND)
            self._save(entries)
            self._page(app_id).unlink(missing_ok=True)
            if (build := self._builds.get(app_id)) is not None:
                build.cancel()  # Claude stops writing an app nobody will see, and its slot frees
        self._publish(self._view(shipped or entry), deleted=shipped is None)

    # ----- builds

    def create(self, request: str, *, show: bool = False) -> AppSummary:
        now = time.time()
        with self._lock:
            entries = self._read(writing=True)
            app_id = secrets.token_hex(8)
            while app_id in entries or self._page(app_id).exists():
                app_id = secrets.token_hex(8)
            title = ' '.join(request.split())[:40]  # until the page names itself
            entry = Entry(id=app_id, title=title, status='building', created_at=now, updated_at=now, prompt=request)
            return self._start(entries, entry, f'Demande\xa0: «\xa0{request}\xa0»', base=None, show=show, new=True)

    def update(self, app_id: str, request: str, *, show: bool = False) -> AppSummary:
        """Rewrites the page; the old one keeps running until the new one passes."""
        with self._lock:
            entries = self._read(writing=True)
            entry = entries.get(app_id) or self._shipped().get(app_id)  # a shipped app: its copy is retouched
            if entry is None:
                raise HTTPException(404, detail=NOT_FOUND)
            if app_id in self._builds:
                raise HTTPException(409, detail=f'«\xa0{entry.title}\xa0» est déjà en construction.')
            try:
                base = source.read_text(encoding='utf-8') if (source := self._source(app_id)) else None
            except OSError:
                base = None
            if base is not None:
                ask = f"Demande d'origine\xa0: «\xa0{entry.prompt or entry.title}\xa0»\nRetouche demandée\xa0: «\xa0{request}\xa0»"
            else:  # a creation that failed: built again, with what is added
                ask = f'Demande\xa0: «\xa0{entry.prompt or entry.title}\xa0»\nPrécision\xa0: «\xa0{request}\xa0»'
            entry = entry.model_copy(update={'status': 'building', 'error': None})
            return self._start(entries, entry, ask, base=base, show=show, new=False)

    def cancel(self, app_id: str) -> None:
        """Stop a build: a creation goes, a retouch leaves the page as it was."""
        build = self._builds.get(app_id)
        if build is None:
            raise HTTPException(409, detail=NOT_BUILDING)
        build.cancel()

    def draft(self, app_id: str) -> AppHtml:
        """The page as written so far, its CSP first: the dashboard previews it (scripts run only once whole)."""
        build = self._builds.get(app_id)
        page = build.draft() if build else None
        if page is None:
            raise HTTPException(404, detail="Rien n'est encore écrit.")
        return AppHtml(id=app_id, html=served(page))

    def _start(
        self, entries: dict[str, Entry], entry: Entry, ask: str, *, base: str | None, show: bool, new: bool
    ) -> AppSummary:
        """Under the lock: store the building entry, start its thread. `show`: asked by voice, the dashboards are told
        to open it now (`open`), to watch it being built; one holding unsaved work offers it instead (launch.ts)."""
        if len(self._builds) >= BUILDS_MAX:
            raise HTTPException(429, detail=BUSY)
        entries[entry.id] = entry
        self._save(entries)
        settings = self._mac.store.settings()  # the apps' model and effort, as set now
        model = APP_MODELS[settings.apps_model]
        kind = 'update' if base is not None else 'create'
        build = self._builds[entry.id] = Build(entry.id, kind, self._progress, model, settings.apps_effort)
        args = (entry, build, ask, base, new)
        threading.Thread(target=self._build, args=args, name=f'app-{entry.id}', daemon=True).start()
        app = self._view(entry)
        self._publish(app, open=show)
        return app

    def _build(self, entry: Entry, build: Build, ask: str, base: str | None, new: bool) -> None:
        page = error = None
        cancelled = False
        try:
            page = build.retouch(base, ask) if base is not None else build.create(ask)
        except Cancelled:
            cancelled, error = True, CANCELLED
        except BuildError as exc:
            error = str(exc)
        except Exception:  # a crashed build must still end, as a failure
            log.exception('app %s: the build crashed', entry.id)
            error = 'La construction a planté\xa0: regarde les logs du Mac.'
        with self._lock:
            try:
                if self._stopped:
                    return  # still building in index.json: interrupted, as after any restart
                if build.cancelled:  # « Annuler » after the last check: the page is not written
                    page, cancelled, error = None, True, CANCELLED
                entries = self._read(writing=True)
                current = entries.get(entry.id)
                if current is None:
                    return  # deleted meanwhile
                if cancelled and new:  # a new app cancelled: nothing to keep
                    del entries[entry.id]
                    self._save(entries)
                    self._publish(self._view(current), deleted=True)
                    return
                if page is not None:
                    try:
                        write_text_atomic(self._page(entry.id), page)
                    except OSError:  # a full disk: a failure like any other, the old page (if any) stays
                        log.exception('app %s: its page could not be written', entry.id)
                        page, error = None, UNSAVED
                if page is not None:
                    fields = {'status': 'ready', 'error': None, 'updated_at': time.time()}
                    if base is None and current.title == entry.title:
                        fields['title'] = title_of(page) or current.title
                    if base is None:  # a retouch keeps what the app was judged, or set to, at its creation
                        fields['quiet'] = quiet_of(page)
                else:
                    log.warning('app %s failed: %s', entry.id, error)
                    fields = {'status': 'ready' if self._source(entry.id) else 'failed', 'error': error}
                current = entries[entry.id] = current.model_copy(update=fields)
                self._save(entries)
            except OSError:  # index.json unwritable: the dashboard still hears how the build ended
                log.exception('app %s: the index could not be written', entry.id)
                self._ended[entry.id] = current
            except HTTPException as exc:  # index.json not read in full: left as it is, the page not written
                kept = self._source(entry.id) is not None  # a retouch's old page
                current = entry.model_copy(update={'status': 'ready' if kept else 'failed', 'error': exc.detail})
                self._ended[entry.id] = current
            finally:
                self._builds.pop(entry.id, None)
        self._publish(self._view(current))

    # ----- voice

    def designate(self, name: str) -> AppSummary | list[AppSummary] | None:
        """The app a spoken name means, or the candidates to ask between, or None. Gemini repeats what it heard, not
        a title. Unnamed: the app the dashboard shows, else the one opened or changed last."""
        apps = self.apps()
        showing = self._mac.broker.showing('app')

        def recent(app: AppSummary) -> tuple[bool, float]:
            changed = 0.0 if app.builtin and not app.edited else app.updated_at  # a shipped page's date is not yours
            return app.id == showing, max(changed, self._opened.get(app.id, 0.0))

        titled = fold(name) in {fold(app.title) for app in apps}  # « Mon jeu », a title of words said around one
        heard = fold(name).split() if named(name) or titled else []
        said = [sound(word) for word in heard if word not in POINTED]  # the name
        pointing = [sound(word) for word in heard if word in POINTED]
        if not said and titled:  # « Dashboard », a title of pointing words said whole
            said, pointing = pointing, []
        if not apps or not said:
            return max(apps, key=recent) if apps else None
        scored = []  # how much of the name the title holds, then with the pointing words, how much of it was said
        for app in apps:
            title = words(app.title)
            scored.append((overlap(said, title)[0], *overlap(said + pointing, title), app))
        best = max(cover for cover, *_ in scored)
        if best < MATCH_MIN:  # many words around the name: a title said whole still names its app
            whole = [app for *_, title, app in scored if title == 1.0]
            longest = max((len(words(app.title)) for app in whole), default=0)
            whole = [app for app in whole if len(words(app.title)) == longest]
            return max(whole, key=recent) if len({fold(a.title) for a in whole}) == 1 else (whole[:4] or None)
        most = max(full for cover, full, _, _ in scored if cover >= best - 1e-9)
        # the name decides: the pointing words said around it only choose between the titles it names alike
        scored = [entry for entry in scored if entry[0] < best - 1e-9 or entry[1] >= most - 1e-9]
        close = [(title, app) for cover, _, title, app in scored if cover >= best - CLOSE]
        top = [app for cover, *_, app in scored if cover >= best - 1e-9]
        exact = [app for title, app in close if title == 1.0] if best == 1.0 else []
        shown = [app for app in top if app.id == showing]  # the screen breaks a tie, never a clearer name
        pick = shown or exact or ([close[0][1]] if len(close) == 1 else [])
        if pick:
            return max(pick, key=recent)  # the same title twice: the latest
        return [app for _, app in sorted(close, key=lambda c: (c[0], recent(c[1])), reverse=True)][:4]

    def voice(self, ask: AppVoice) -> AppVoiceOut:
        """The robot's `app` tool; the message is French, for the model. Asked by voice, an app goes to the
        dashboard at once, built or being built; with no dashboard connected, the message says it shows nowhere."""
        if ask.action != 'open' and not ask.request.strip():
            return AppVoiceOut(message="Demande ce que l'app doit faire ou changer, puis rappelle l'outil.")
        seen = self._mac.broker.watched()
        if ask.action == 'create':
            app = self.create(ask.request, show=True)
            said = BUILDING if seen else f"Je construis l'app (quelques minutes), {UNSEEN}."
            return AppVoiceOut(message=f'{said} {LATER}', app=app)
        found = self.designate(ask.name)
        if isinstance(found, list):
            titles = ' ou '.join(f'«\xa0{app.title}\xa0»' for app in found)
            return AppVoiceOut(message=f'Plusieurs apps peuvent correspondre\xa0: {titles}. Demande laquelle.')
        if found is None:
            titles = ', '.join(f'«\xa0{a.title}\xa0»' for a in self.apps()[:15])
            missing = f"Je ne trouve pas d'app «\xa0{ask.name}\xa0»." if ask.name.strip() else "Il n'y a aucune app."
            return AppVoiceOut(
                message=f'{missing} Les apps\xa0: {titles}.' if titles else f"{missing} Propose d'en créer une."
            )
        if ask.action == 'update':
            app = self.update(found.id, ask.request, show=True)
            return AppVoiceOut(message=f'Je modifie «\xa0{app.title}\xa0». {LATER}', app=app)
        self._publish(found, open=True)
        said = {
            'building': f'«\xa0{found.title}\xa0» est en construction, sur le dashboard.',
            'failed': f"«\xa0{found.title}\xa0» n'a pas pu être construite\xa0: {found.error}",
            'ready': f'«\xa0{found.title}\xa0» est sur le dashboard.',
        }[found.status]
        if not seen and found.status != 'failed':
            said = f'«\xa0{found.title}\xa0» est {"prête" if found.status == "ready" else "en construction"}, {UNSEEN}.'
        return AppVoiceOut(message=said, app=found)

    # ----- the bridge and the palette

    def allow(self, app_id: str, kind: str) -> bool:
        """Whether one more message of this kind fits the app's rate (RATES); counts it if so."""
        group = 'move' if kind in ('emotion', 'gesture', 'replay') else kind
        count, window_s = RATES[group]
        now = self._clock()
        sent = self._sent.setdefault((app_id, group), deque())
        while sent and now - sent[0] >= window_s:
            sent.popleft()
        if len(sent) >= count:
            return False
        sent.append(now)
        return True

    def follows(self, dashboard: str) -> bool:
        """Whether the robot follows this dashboard's app: yes unless another dashboard's moved it within HOLD_S."""
        now = self._clock()
        if self._follows and self._follows[0] != dashboard and now - self._follows[1] < HOLD_S:
            return False
        self._follows = (dashboard, now)
        return True

    def manette_holder(self) -> str | None:
        """The app whose manette lease is live (renewed within its TTL), if any."""
        holder = self._manette
        return holder[0] if holder and self._clock() - holder[1] <= MANETTE_TTL_S else None

    def hold_manette(self, app_id: str | None) -> None:
        self._manette = (app_id, self._clock()) if app_id else None

    def catalog(self) -> list[CatalogEntry]:
        ready = (app for app in self.apps() if app.status == 'ready')
        return [
            CatalogEntry(
                id=f'app:{app.id}',
                group='apps',
                label=app.title,
                description='Ouvre cette app sur le dashboard.',
                say=f'ouvre {app.title}',
                tool='app',
                action=AppAction(kind='app', app_id=app.id),
            )
            for app in ready
        ]

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        finally:  # the server stops: no claude writes on for nobody
            with self._lock:
                self._stopped = True
                for build in self._builds.values():
                    build.cancel()


def routes(mac: Mac) -> APIRouter:
    router = APIRouter()
    service = mac.apps

    @router.get('/api/apps')
    def list_apps() -> AppList:
        return AppList(apps=service.apps())

    @router.post('/api/apps')
    def create_app(body: AppRequest) -> AppSummary:
        return service.create(body.request)

    @router.post('/api/apps/voice')
    def voice(body: AppVoice) -> AppVoiceOut:
        return service.voice(body)

    @router.get('/api/apps/{app_id}/html')
    def app_html(app_id: str) -> AppHtml:
        return service.html(app_id)

    @router.patch('/api/apps/{app_id}')
    def rename_app(app_id: str, body: AppRename) -> AppSummary:
        return service.rename(app_id, body.title)

    @router.post('/api/apps/{app_id}/quiet')
    def quiet_app(app_id: str, body: AppQuiet) -> AppSummary:
        return service.set_quiet(app_id, body.quiet)

    @router.delete('/api/apps/{app_id}', status_code=204)
    def delete_app(app_id: str) -> None:
        service.delete(app_id)

    @router.post('/api/apps/{app_id}/update')
    def update_app(app_id: str, body: AppRequest) -> AppSummary:
        return service.update(app_id, body.request)

    @router.post('/api/apps/{app_id}/cancel', status_code=204)
    def cancel_build(app_id: str) -> None:
        service.cancel(app_id)

    @router.get('/api/apps/{app_id}/draft')
    def app_draft(app_id: str) -> AppHtml:
        return service.draft(app_id)

    @router.post('/api/apps/{app_id}/manette', status_code=204)
    async def manette(app_id: str, body: ManetteIn) -> None:
        """Hold the robot as this app's controller (granted once, renewed every second: the lease), or let go. One app
        at a time: another one's lease is refused while the holder renews, and its letting go is ignored."""
        if not known(app_id):
            raise HTTPException(404, detail=NOT_FOUND)
        holder = service.manette_holder()
        if body.profile is None:
            if holder in (None, app_id):
                service.hold_manette(None)
                mac.commands.push('manette', manette='off')
            return
        if holder not in (None, app_id):
            raise HTTPException(409, detail=MANETTE_TAKEN)
        if not mac.health.takes_say():
            raise HTTPException(409, detail=MANETTE_ASLEEP)
        service.hold_manette(app_id)
        mac.commands.push('manette', manette=body.profile, grant=body.grant)

    @router.post('/api/apps/{app_id}/bridge', status_code=204)
    async def bridge(app_id: str, body: BridgeIn) -> None:
        if not known(app_id):
            raise HTTPException(404, detail=NOT_FOUND)
        if not mac.health.takes_say():
            raise HTTPException(409, detail=ASLEEP)
        if not service.follows(body.dashboard):  # before the rate: what another screen sends spends none of it
            raise HTTPException(409, detail=ELSEWHERE)
        if not service.allow(app_id, body.motion.kind):
            raise HTTPException(429, detail=TOO_FAST)
        mac.commands.push('app_motion', motion=body.motion)

    return router
