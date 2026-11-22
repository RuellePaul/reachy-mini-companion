"""Spotify Web API: playback on a Connect device the Mac picks, its remote control, and the one-shot OAuth flow.

Every failure is a SpotifyError carrying a French sentence the robot can say and the status to answer with.
Only endpoints the February 2026 migration keeps are used.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import secrets
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from datetime import date
from typing import Any

import requests

from mac_server.config import Config
from mac_server.contracts import ControlOut, PlayOut, Track
from mac_server.store import write_json_atomic

log = logging.getLogger(__name__)

API = 'https://api.spotify.com/v1'
AUTHORIZE_URL = 'https://accounts.spotify.com/authorize'
TOKEN_URL = 'https://accounts.spotify.com/api/token'
SCOPES = (  # one re-authorisation grants them all
    'user-read-playback-state user-modify-playback-state user-read-currently-playing user-read-private '
    'user-library-read user-library-modify user-read-playback-position '
    'playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public'
)
TIMEOUT_S = (2.0, 3.0)  # connect, read: one call
BUDGET_S = 7.0  # one operation, every call ended: 3 s under the robot's 10 s wait (robot_agent MAC_TIMEOUT_S)
MARKET = 'from_token'
SEARCH_MAX = 10  # results asked for: Spotify sends null for each one this app may not open
PAGE_MAX = 50  # the largest page: the library, the liked tracks, a book's chapters
PLAYED_MAX = 100  # URIs started or queued, for `playlist`: one POST /playlists/{id}/items takes 100
STATE_TTL_S = 600.0
# play kind -> (library path, the key wrapping each item there): the user's own item of that name comes first.
LIBRARY = {'playlist': ('/me/playlists', None), 'audiobook': ('/me/audiobooks', None), 'podcast': ('/me/shows', 'show')}
SEARCHED = {'podcast': 'show'}  # play kind -> search type, where they differ
OFF = ('off', 'non', 'false', '0')

NO_DEVICE = 'Aucun appareil Spotify disponible\xa0: ouvre Spotify sur le Mac.'
NOT_CONFIGURED = (
    "Spotify n'est pas configuré sur le Mac\xa0: ajoute SPOTIFY_CLIENT_ID et SPOTIFY_CLIENT_SECRET au .env, "
    'puis lance make spotify-auth.'
)
NOT_AUTHORIZED = "Spotify n'est pas autorisé sur le Mac\xa0: lance make spotify-auth."
MISSING_SCOPE = 'Il manque une autorisation Spotify (scope manquant)\xa0: relance make spotify-auth sur le Mac.'
TOO_SLOW = 'Spotify ne répond pas (délai dépassé). Réessaie dans un instant.'
RATE_LIMITED = 'Spotify reçoit trop de demandes\xa0: réessaie dans un instant.'
NOTHING_PLAYING = 'Rien ne joue sur Spotify en ce moment.'
# Reason codes of Spotify's player errors. Its `message` is English: logged, never sent to the robot.
REFUSALS = {
    'PREMIUM_REQUIRED': 'Spotify Premium est nécessaire pour que le robot pilote la musique.',
    'NO_ACTIVE_DEVICE': NO_DEVICE,
    'DEVICE_NOT_CONTROLLABLE': 'Cet appareil Spotify ne se laisse pas piloter à distance\xa0: ouvre Spotify sur le Mac.',
    'VOLUME_CONTROL_DISALLOW': 'Cet appareil Spotify ne laisse pas régler son volume à distance.',
    'NO_NEXT_TRACK': "Il n'y a pas de morceau suivant.",
    'NO_PREV_TRACK': "Il n'y a pas de morceau précédent.",
}
# A pause refused with one of these (or a 404) means nothing was playing; 'UNKNOWN' is « Restriction violated ».
NOTHING_PLAYS = (None, 'NO_ACTIVE_DEVICE', 'ALREADY_PAUSED', 'UNKNOWN')
PLAYS = ('ALREADY_PLAYING', 'UNKNOWN')  # a resume refused so: the music was already playing
COMMANDS = {  # action -> one call: method, path, what was refused, what is said
    'resume': ('PUT', '/me/player/play', 'la reprise', 'Lecture reprise.'),
    'next': ('POST', '/me/player/next', 'le passage au suivant', 'Morceau suivant lancé.'),
    'previous': ('POST', '/me/player/previous', 'le retour en arrière', 'Morceau précédent lancé.'),
}
REPEATS = {'track': 'Ce morceau passe en boucle.', 'context': 'Lecture en boucle.', 'off': 'Répétition désactivée.'}


class SpotifyError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _error(response: requests.Response) -> tuple[str | None, str]:
    """A refusal's reason code, when Spotify gives one, and its own words for the log (Web API or token shape)."""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get('error') if isinstance(body, dict) else None
    if isinstance(error, dict):
        reason = error.get('reason')
        return (reason if isinstance(reason, str) else None), str(error.get('message') or '')[:200]
    if isinstance(body, dict):
        return None, str(body.get('error_description') or error or '')[:200]
    return None, response.text[:200]


def _check(response: requests.Response, what: str) -> requests.Response:
    if response.ok:
        return response
    code, (reason, words) = response.status_code, _error(response)
    log.warning('Spotify refused %s: HTTP %s %s %s', what, code, reason or '', words)
    if reason in REFUSALS:
        raise SpotifyError(REFUSALS[reason], 409 if reason == 'NO_ACTIVE_DEVICE' else 502)
    if code == 403 and 'scope' in words.casefold():  # « Insufficient client scope »; a closed endpoint says nothing
        raise SpotifyError(MISSING_SCOPE, 503)
    if code == 429:
        raise SpotifyError(RATE_LIMITED)
    if code >= 500:
        raise SpotifyError(f'Spotify a un problème de son côté (erreur {code})\xa0: réessaie dans un instant.')
    raise SpotifyError(f'Spotify a refusé {what} (erreur {code}).')


def _body(response: requests.Response, what: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise SpotifyError(f'Spotify a renvoyé une réponse illisible pour {what}.')
    return body


def _items(page: Any, key: str | None = None) -> list[dict[str, Any]]:
    """A page's items that have a URI (Spotify sends null for those this app may not open); `key` unwraps each."""
    items = page.get('items') if isinstance(page, dict) else None
    found = [item.get(key) if key else item for item in items or [] if isinstance(item, dict)]
    return [item for item in found if isinstance(item, dict) and item.get('uri')]


def _same_name(name: object) -> str:
    """A name as compared: case and apostrophe style aside (macOS writes a curly one, a hand types ')."""
    return str(name or '').casefold().replace('\u2019', "'").strip()


def _named(name: object, query: str, start: bool = False) -> bool:
    """Every word of the query a word of the name (or, with `start`, the start of one): « chill » finds « Chill du
    soir », « rap » not « Thérapie »; « mac » starts « MacBook », never « iMac »."""
    words, named = set(re.findall(r"[\w']+", _same_name(query))), re.findall(r"[\w']+", _same_name(name))
    return bool(words) and all(any(n == w or (start and n.startswith(w)) for n in named) for w in words)


def _resume_ms(item: dict[str, Any]) -> int:
    """Where the user stopped in a chapter or an episode: 0 unstarted, finished, or unknown (a missing scope)."""
    point = item.get('resume_point') or {}
    return 0 if point.get('fully_played') else int(point.get('resume_position_ms') or 0)


def _track(item: dict[str, Any], kind: str) -> Track:
    """What plays: the item's name, and its artist (a playlist's owner, a book's author, an episode's show); an
    artist has none, its name is the title."""
    if kind == 'playlist':
        artist = (item.get('owner') or {}).get('display_name')
    elif kind == 'artist':
        artist = None
    else:
        people = item.get('artists') or item.get('authors') or [item.get('show') or {}]
        artist = people[0].get('name') if isinstance(people[0], dict) else None
    return Track(title=str(item.get('name') or ''), artist=str(artist or ''), uri=item.get('uri'))


def _required(value: Any, sentence: str) -> Any:
    if value is None or value == '':
        raise SpotifyError(sentence, 422)
    return value


class Spotify:
    def __init__(
        self, config: Config, wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic
    ) -> None:
        self._config = config
        self._wall = wall  # token expiry on the wall clock (monotonic stops in sleep)
        self._mono = mono  # deadlines and OAuth states: spans of seconds and minutes
        self._lock = threading.Lock()  # routes run in threads: one token refresh at a time
        self._http = requests.Session()  # one kept-alive connection: ~55 ms less per call
        self._access: str | None = None
        self._expires_at = 0.0
        self._refresh = self._load_refresh() or config.spotify_refresh_token
        self._states: dict[str, float] = {}  # OAuth state -> monotonic expiry
        self._played: deque[tuple[date, str]] = deque(maxlen=PLAYED_MAX)  # (day, URI) started or queued

    def _load_refresh(self) -> str | None:
        try:
            token = json.loads(self._config.spotify_token_path.read_text(encoding='utf-8')).get('refresh_token')
        except (OSError, ValueError, AttributeError):
            return None
        return token if isinstance(token, str) and token else None

    def _save_refresh(self, token: str) -> None:
        self._refresh = token
        try:
            write_json_atomic(self._config.spotify_token_path, {'refresh_token': token})
        except OSError as exc:
            log.warning('Spotify refresh token kept in memory only: %s', exc)
            return
        log.info('Spotify refresh token saved to %s', self._config.spotify_token_path)

    def _send(self, method: str, url: str, deadline: float, **kwargs: Any) -> requests.Response:
        """One call, over by the operation's deadline: connect and read timeouts each at most half the time left."""
        left = deadline - self._mono()
        if left <= 0:
            raise SpotifyError(TOO_SLOW)
        try:
            return self._http.request(method, url, timeout=tuple(min(t, left / 2) for t in TIMEOUT_S), **kwargs)
        except requests.Timeout as exc:
            raise SpotifyError(TOO_SLOW) from exc
        except requests.RequestException as exc:
            raise SpotifyError('Impossible de joindre Spotify\xa0: le Mac a-t-il du réseau\xa0?') from exc

    def _grant(self, data: dict[str, str], deadline: float) -> dict[str, Any]:
        """A token request (refresh or authorization code); the caller holds the lock and checked the credentials."""
        cfg = self._config
        basic = base64.b64encode(f'{cfg.spotify_client_id}:{cfg.spotify_client_secret}'.encode()).decode()
        response = self._send('POST', TOKEN_URL, deadline, headers={'Authorization': f'Basic {basic}'}, data=data)
        if response.status_code in (400, 401):
            log.warning('Spotify refused the token request: %s', _error(response)[1])
            raise SpotifyError("Spotify a refusé l'accès\xa0: relance make spotify-auth sur le Mac.", 503)
        body = _body(_check(response, "l'accès"), "l'accès")
        if not isinstance(body.get('access_token'), str):
            raise SpotifyError("Spotify n'a pas renvoyé de jeton d'accès. Réessaie dans un instant.")
        lifetime = body.get('expires_in')
        self._access = body['access_token']
        self._expires_at = self._wall() + max(60, (lifetime if isinstance(lifetime, int) else 3600) - 60)
        rotated = body.get('refresh_token')
        if isinstance(rotated, str) and rotated and rotated != self._refresh:
            self._save_refresh(rotated)
        if isinstance(body.get('scope'), str):
            log.info('Spotify token granted with scopes: %s', body['scope'])
            if missing := sorted(set(SCOPES.split()) - set(body['scope'].split())):
                log.warning('Spotify token lacks %s: run make spotify-auth', ' '.join(missing))
        return body

    def _token(self, deadline: float) -> str:
        with self._lock:
            if self._access is not None and self._wall() < self._expires_at:
                return self._access
            if not (self._config.spotify_client_id and self._config.spotify_client_secret):
                raise SpotifyError(NOT_CONFIGURED, 503)
            if not self._refresh:
                raise SpotifyError(NOT_AUTHORIZED, 503)
            grant = {'grant_type': 'refresh_token', 'refresh_token': self._refresh}
            return self._grant(grant, deadline)['access_token']

    def _call(self, method: str, path: str, deadline: float, **kwargs: Any) -> requests.Response:
        """One Web API call; a 401 drops the access token and is retried exactly once."""
        for _ in range(2):
            token = self._token(deadline)
            auth = {'Authorization': f'Bearer {token}'}
            response = self._send(method, f'{API}{path}', deadline, headers=auth, **kwargs)
            if response.status_code != 401:
                break
            with self._lock:
                if self._access == token:
                    self._access = None
        return response

    def _get(self, path: str, what: str, deadline: float, **params: Any) -> dict[str, Any]:
        return _body(_check(self._call('GET', path, deadline, params=params), what), what)

    def _command(self, method: str, path: str, what: str, deadline: float, fine: tuple = (), **params: Any) -> bool:
        """A player command; False when refused (403) for a `fine` reason: it was already so. With no active
        device, it is sent once more to the Mac's own app (listed, yet not active)."""
        response = self._call(method, path, deadline, params=params or None)
        if not response.ok and _error(response)[0] == 'NO_ACTIVE_DEVICE':
            response = self._call(method, path, deadline, params=params | {'device_id': self._player(deadline)['id']})
        if response.status_code == 403 and _error(response)[0] in fine:
            return False
        _check(response, what)
        return True

    def _devices(self, deadline: float) -> list[dict[str, Any]]:
        listed = self._get('/me/player/devices', 'la liste des appareils', deadline).get('devices')
        return [d for d in listed or [] if isinstance(d, dict) and d.get('id')]

    def _player(self, deadline: float) -> dict[str, Any]:
        """The active device, else the Mac's own Spotify app; never someone else's computer, nor a device that
        takes no command (`is_restricted`)."""
        devices = self._devices(deadline)
        mine, free = _same_name(self._config.spotify_device_name), [d for d in devices if not d.get('is_restricted')]
        found = [d for d in free if d.get('is_active')]
        found += [d for d in free if _same_name(d.get('name')) == mine]
        if not found:
            names = ', '.join(repr(d.get('name')) for d in devices) or 'none'
            log.warning('Spotify: no active device, none named %r, that takes commands (devices: %s)', mine, names)
            raise SpotifyError(NO_DEVICE, 409)
        return found[0]

    def _find(self, query: str, kind: str, deadline: float) -> dict[str, Any]:
        """The user's own playlist, book or podcast of that name, else the first search result."""
        if kind in LIBRARY:
            path, key = LIBRARY[kind]
            mine = _items(self._get(path, 'ta bibliothèque', deadline, limit=PAGE_MAX), key)
            if named := [item for item in mine if _named(item.get('name'), query)]:
                return min(named, key=lambda item: _same_name(item.get('name')) != _same_name(query))  # exact first
        kind = SEARCHED.get(kind, kind)
        found = self._get('/search', 'la recherche', deadline, q=query, type=kind, limit=SEARCH_MAX, market=MARKET)
        if not (items := _items(found.get(f'{kind}s'))):
            raise SpotifyError(f'Aucun résultat Spotify pour «\xa0{query}\xa0».', 404)
        return items[0]

    def _book(self, book: dict[str, Any], deadline: float) -> dict[str, Any]:
        """A book from its first unfinished chapter, where the user stopped. Without resume points (the scope
        user-read-playback-position), the book alone: Spotify's own resume, if it keeps one."""
        uri, offset = book['uri'], 0
        while True:
            path = f'/audiobooks/{uri.rsplit(":", 1)[-1]}/chapters'
            chapters = self._get(path, 'les chapitres', deadline, limit=PAGE_MAX, offset=offset, market=MARKET)
            points = [(c, c.get('resume_point')) for c in _items(chapters)]
            if todo := [c for c, point in points if isinstance(point, dict) and not point.get('fully_played')]:
                return {'context_uri': uri, 'offset': {'uri': todo[0]['uri']}, 'position_ms': _resume_ms(todo[0])}
            if not chapters.get('next') or not any(isinstance(point, dict) for _, point in points):
                return {'context_uri': uri}  # no resume points, or every chapter heard
            offset += PAGE_MAX

    def _episode(self, show: dict[str, Any], deadline: float) -> dict[str, Any]:
        """The latest episode in progress, else the latest one; the show is its `show`."""
        path = f'/shows/{show["uri"].rsplit(":", 1)[-1]}/episodes'
        episodes = _items(self._get(path, 'les épisodes', deadline, limit=SEARCH_MAX, market=MARKET))
        if not episodes:
            raise SpotifyError(f'Aucun épisode disponible pour «\xa0{show.get("name")}\xa0».', 404)
        episodes.sort(key=lambda episode: str(episode.get('release_date') or ''), reverse=True)
        return next((e for e in episodes if _resume_ms(e)), episodes[0]) | {'show': show}

    def play(self, query: str, kind: str) -> PlayOut:
        deadline = self._mono() + BUDGET_S
        device = self._player(deadline)
        if kind == 'liked':
            saved = self._get('/me/tracks', 'les titres likés', deadline, limit=PAGE_MAX, market=MARKET)
            if not (tracks := _items(saved, 'track')):
                raise SpotifyError('Aucun titre liké sur ce compte Spotify.', 404)
            random.shuffle(tracks)
            item, payload = tracks[0], {'uris': [t['uri'] for t in tracks]}
        elif kind == 'audiobook':
            payload = self._book(item := self._find(query, kind, deadline), deadline)
        elif kind == 'podcast':
            item = self._episode(self._find(query, kind, deadline), deadline)
            payload = {'uris': [item['uri']], 'position_ms': _resume_ms(item)}
        else:
            item = self._find(query, kind, deadline)
            payload = {'uris': [item['uri']]} if kind == 'track' else {'context_uri': item['uri']}
        params = {'device_id': device['id']}
        _check(self._call('PUT', '/me/player/play', deadline, params=params, json=payload), 'la lecture')
        if kind in ('track', 'podcast'):
            self._played.append((date.fromtimestamp(self._wall()), item['uri']))
        log.info('Spotify: %s %s on %s', kind, item['uri'], device.get('name'))
        return PlayOut(track=_track(item, kind), device=str(device.get('name') or ''))

    def control(self, action: str, value: str = '', amount: float | None = None) -> ControlOut:
        """Everything but starting something: `action` is a contracts.MusicAction."""
        deadline = self._mono() + BUDGET_S
        if action not in COMMANDS:
            return getattr(self, f'_{action}')(value.strip(), amount, deadline)
        method, path, what, said = COMMANDS[action]
        done = self._command(method, path, what, deadline, fine=PLAYS if action == 'resume' else ())
        return ControlOut(message=said if done else 'La lecture était déjà en cours.')

    def _pause(self, _value: str, _amount: float | None, deadline: float) -> ControlOut:
        response = self._call('PUT', '/me/player/pause', deadline)
        if response.status_code == 404 or (response.status_code == 403 and _error(response)[0] in NOTHING_PLAYS):
            return ControlOut(message='Rien ne jouait.')
        _check(response, 'la pause')  # a refusal while music plays (Premium, an uncontrollable device) is said
        return ControlOut(message='Lecture en pause.')

    def _shuffle(self, value: str, _amount: float | None, deadline: float) -> ControlOut:
        on = _same_name(value) not in OFF
        self._command('PUT', '/me/player/shuffle', "l'aléatoire", deadline, state='true' if on else 'false')
        return ControlOut(message=f'Lecture aléatoire {"activée" if on else "désactivée"}.')

    def _repeat(self, value: str, _amount: float | None, deadline: float) -> ControlOut:
        state = 'track' if _same_name(value) == 'track' else 'off' if _same_name(value) in OFF else 'context'
        self._command('PUT', '/me/player/repeat', 'la répétition', deadline, state=state)
        return ControlOut(message=REPEATS[state])

    def _volume(self, _value: str, amount: float | None, deadline: float) -> ControlOut:
        level = int(min(100.0, max(0.0, round(_required(amount, 'Volume\xa0: dis un niveau de 0 à 100.')))))
        self._command('PUT', '/me/player/volume', 'le volume', deadline, volume_percent=level)
        return ControlOut(message=f'Volume de Spotify réglé à {level} sur 100.', volume=level)

    def _playback(self, deadline: float) -> dict[str, Any]:
        """What plays (a track or an episode) and where; nothing playing (a 204) is a 404 the robot says."""
        what = 'la lecture en cours'
        response = _check(self._call('GET', '/me/player', deadline, params={'additional_types': 'episode'}), what)
        state = {} if response.status_code == 204 else _body(response, what)
        if not (isinstance(state.get('item'), dict) and state['item'].get('uri')):
            raise SpotifyError(NOTHING_PLAYING, 404)
        return state

    def _now(self, _value: str, _amount: float | None, deadline: float) -> ControlOut:
        try:
            state = self._playback(deadline)
        except SpotifyError as exc:
            if str(exc) != NOTHING_PLAYING:
                raise
            return ControlOut(message=NOTHING_PLAYING)  # an answer to « what's playing? », not a failure
        track, device = _track(state['item'], 'track'), state.get('device') or {}
        name, volume = device.get('name') or None, device.get('volume_percent')
        volume = volume if isinstance(volume, int) and 0 <= volume <= 100 else None
        said = f'{"En cours" if state.get("is_playing") else "En pause"}\xa0: {track.title}, de {track.artist}'
        said += (f', sur {name}' if name else '') + (f', volume Spotify à {volume}' if volume is not None else '')
        return ControlOut(message=f'{said}.', track=track, device=name, volume=volume)

    def _like(self, _value: str, _amount: float | None, deadline: float) -> ControlOut:
        item = self._playback(deadline)['item']
        self._command('PUT', '/me/library', 'le like', deadline, uris=item['uri'])
        track, where = _track(item, 'track'), 'tes titres likés' if item.get('type') == 'track' else 'ta bibliothèque'
        return ControlOut(message=f'«\xa0{track.title}\xa0» ajouté à {where}.', track=track)

    def _queue(self, value: str, _amount: float | None, deadline: float) -> ControlOut:
        item = self._find(_required(value, 'File\xa0: dis quel morceau ajouter.'), 'track', deadline)
        self._command('POST', '/me/player/queue', "l'ajout à la file", deadline, uri=item['uri'])
        self._played.append((date.fromtimestamp(self._wall()), item['uri']))
        track = _track(item, 'track')
        return ControlOut(message=f'Ajouté à la file\xa0: {track.title}, de {track.artist}.', track=track)

    def _seek(self, _value: str, amount: float | None, deadline: float) -> ControlOut:
        seconds = _required(amount, 'Avance\xa0: dis de combien de secondes (négatif pour reculer).')
        state = self._playback(deadline)
        progress = state.get('progress_ms') if isinstance(state.get('progress_ms'), int) else 0
        position = max(0, progress + round(seconds * 1000))
        self._command('PUT', '/me/player/seek', "l'avance", deadline, position_ms=position)
        return ControlOut(message=f'{"Avancé" if seconds >= 0 else "Reculé"} de {abs(seconds):.0f} secondes.')

    def _device(self, value: str, _amount: float | None, deadline: float) -> ControlOut:
        """Playback moved to the device of that name: « salon » finds « Salon », else « Enceinte du salon » (whole
        words), else « Enceintes du salon » (their start: « mac » finds « MacBook Pro », never « iMac »)."""
        wanted, devices = _same_name(_required(value, 'Appareil\xa0: dis lequel.')), self._devices(deadline)
        found = [d for d in devices if _named(d.get('name'), wanted, start=True)]
        found.sort(key=lambda d: (_same_name(d.get('name')) != wanted, not _named(d.get('name'), wanted)))
        if not found:
            listed = ', '.join(str(d.get('name')) for d in devices) or 'aucun'
            raise SpotifyError(f'Aucun appareil Spotify nommé «\xa0{value}\xa0» (disponibles\xa0: {listed}).', 404)
        device, name = found[0], str(found[0].get('name'))
        if device.get('is_restricted'):
            raise SpotifyError(f'«\xa0{name}\xa0» ne se laisse pas piloter à distance.')
        body = {'device_ids': [device['id']], 'play': True}
        _check(self._call('PUT', '/me/player', deadline, json=body), "le changement d'appareil")
        return ControlOut(message=f'Lecture passée sur {name}.', device=name)

    def _playlist(self, value: str, _amount: float | None, deadline: float) -> ControlOut:
        """A private playlist, under that name, of what was started or queued today (this server's memory). All or
        nothing: one made whose tracks did not go in is taken out of the library, so a retry leaves no empty one."""
        name, today = _required(value, 'Playlist\xa0: dis sous quel nom la créer.'), date.fromtimestamp(self._wall())
        played = list(self._played)  # one copy: other request threads append meanwhile
        if not (uris := list(dict.fromkeys(uri for day, uri in played if day == today))):
            raise SpotifyError("Rien n'a été lancé ni mis en file aujourd'hui\xa0: pas de playlist à créer.", 404)
        response = self._call('POST', '/me/playlists', deadline, json={'name': name, 'public': False})
        playlist = _body(_check(response, 'la création de la playlist'), 'la création de la playlist')
        if not isinstance(playlist.get('id'), str):
            raise SpotifyError('Spotify a créé la playlist sans dire laquelle\xa0: regarde dans Spotify.')
        try:
            _check(self._call('POST', f'/playlists/{playlist["id"]}/items', deadline, json={'uris': uris}), "l'ajout")
        except SpotifyError:
            self._unfollow(f'spotify:playlist:{playlist["id"]}', deadline)
            raise
        said = f'Playlist «\xa0{name}\xa0» créée avec {len(uris)} titre{"s" if len(uris) > 1 else ""}.'
        return ControlOut(message=said, track=_track(playlist, 'playlist'))

    def _unfollow(self, uri: str, deadline: float) -> None:
        """A playlist out of the user's library (the Web API deletes none), best effort: the error said is the add's."""
        try:
            _check(self._call('DELETE', '/me/library', deadline, params={'uris': uri}), 'le retrait de la playlist')
        except SpotifyError as exc:
            log.warning('Spotify: the empty playlist %s stays in the library: %s', uri, exc)

    def authorize_url(self) -> str:
        if not (self._config.spotify_client_id and self._config.spotify_client_secret):
            raise SpotifyError(NOT_CONFIGURED, 503)
        state, now = secrets.token_urlsafe(24), self._mono()
        with self._lock:
            self._states = {s: expiry for s, expiry in self._states.items() if expiry > now}
            self._states[state] = now + STATE_TTL_S
        params = {
            'client_id': self._config.spotify_client_id, 'response_type': 'code', 'scope': SCOPES, 'state': state,
            'redirect_uri': self._config.spotify_redirect_uri,
            'show_dialog': 'true',  # a re-run of make spotify-auth always yields a fresh refresh token
        }  # fmt: skip
        return f'{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}'

    def redeem_state(self, state: str | None) -> bool:
        """True once per state `authorize_url` issued in the last 10 minutes."""
        with self._lock:
            expiry = self._states.pop(state or '', None)
        return expiry is not None and expiry > self._mono()

    def exchange_code(self, code: str) -> None:
        grant = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self._config.spotify_redirect_uri}
        with self._lock:
            body = self._grant(grant, self._mono() + BUDGET_S)
        if not body.get('refresh_token'):
            raise SpotifyError("Spotify n'a pas renvoyé de jeton de rafraîchissement\xa0: relance make spotify-auth.")
