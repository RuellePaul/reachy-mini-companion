"""Spotify against a fake HTTP layer: tokens, scopes, device choice, every kind and action's calls, French errors."""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from mac_server.config import Config
from mac_server.spotify import (
    BUDGET_S,
    MISSING_SCOPE,
    NO_DEVICE,
    NOTHING_PLAYING,
    SCOPES,
    SEARCH_MAX,
    TOO_SLOW,
    Spotify,
    SpotifyError,
)
from robot_agent.config import MAC_TIMEOUT_S

PHONE = {'id': 'phone', 'name': 'iPhone', 'type': 'Smartphone', 'is_active': False}
MAC = {'id': 'mac', 'name': 'STUDIO MAC', 'type': 'Computer', 'is_active': False}
OTHER_COMPUTER = {'id': 'pc', 'name': 'Bureau', 'type': 'Computer', 'is_active': False}
TV = {'id': 'tv', 'name': 'Télé', 'type': 'TV', 'is_active': True, 'is_restricted': True}  # takes no command
GET_LUCKY = {'type': 'track', 'name': 'Get Lucky', 'uri': 'spotify:track:9', 'artists': [{'name': 'Daft Punk'}]}
PLAYING = {
    'is_playing': True,
    'progress_ms': 61_000,
    'device': MAC | {'is_active': True, 'volume_percent': 40},
    'item': GET_LUCKY,
}
DAY = 1_700_040_000.0  # a Mac clock at mid-morning, whatever the time zone around Paris


def _bearer(call: tuple) -> str:
    return call[2]['headers']['Authorization']


def _last(http: Any, method: str, path: str) -> dict[str, Any]:
    """The keyword arguments of the last call to (method, path)."""
    return next(kwargs for m, p, kwargs in reversed(http.calls) if (m, p) == (method, path))


def _sent(http: Any) -> list[tuple[str, str]]:
    """What reached Spotify's Web API, in order (the token grants aside)."""
    return [(method, path) for method, path, _ in http.calls if path != 'token']


def test_the_token_expires_on_the_wall_clock(config: Config, spotify_http, new_clock) -> None:
    wall, mono = new_clock(1_700_000_000.0), new_clock()
    spotify = Spotify(config, wall=wall, mono=mono)
    spotify_http.token(access='first', expires_in=3600).token(access='second')
    spotify_http.reply('PUT', '/me/player/pause', 204)
    spotify.control('pause')
    mono.now += 3600  # an hour on the monotonic clock alone: the token holds
    spotify.control('pause')
    wall.now += 3600 - 61  # the wall clock moves on while the monotonic one stands still, as in the Mac's sleep
    spotify.control('pause')
    assert spotify_http.count('POST', 'token') == 1
    wall.now += 1  # 60 s before the announced expiry, on the wall clock
    spotify.control('pause')
    assert spotify_http.count('POST', 'token') == 2
    assert _bearer(spotify_http.calls[-1]) == 'Bearer second'


def test_a_401_is_retried_exactly_once(config: Config, spotify_http) -> None:
    spotify = Spotify(config)
    spotify_http.token(access='stale').token(access='fresh').token(access='fresher').token(access='freshest')
    expired = {'error': {'status': 401, 'message': 'The access token expired'}}
    spotify_http.reply('PUT', '/me/player/pause', 401, expired).reply('PUT', '/me/player/pause', 204)
    assert spotify.control('pause').message == 'Lecture en pause.'
    pauses = [c for c in spotify_http.calls if c[1] == '/me/player/pause']
    assert [_bearer(c) for c in pauses] == ['Bearer stale', 'Bearer fresh']

    spotify_http.replies[('PUT', '/me/player/pause')] = [(401, expired)]
    with pytest.raises(SpotifyError) as refused:
        spotify.control('pause')
    assert spotify_http.count('PUT', '/me/player/pause') == 4  # two more: the call and its single retry
    assert refused.value.status == 502
    assert str(refused.value) == 'Spotify a refusé la pause (erreur 401).'


def test_a_rotated_refresh_token_is_persisted_and_never_logged(
    config: Config, spotify_http, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    spotify_http.token(access='access-token-1', refresh='rotated-token-1')
    spotify_http.reply('PUT', '/me/player/pause', 204)
    Spotify(config).control('pause')
    assert spotify_http.calls[0][2]['data'] == {'grant_type': 'refresh_token', 'refresh_token': 'refresh-from-env'}
    assert json.loads(config.spotify_token_path.read_text()) == {'refresh_token': 'rotated-token-1'}
    assert config.spotify_token_path.stat().st_mode & 0o777 == 0o600

    Spotify(config).control('pause')  # a restarted server reads the file, not the environment
    assert spotify_http.calls[-2][2]['data']['refresh_token'] == 'rotated-token-1'
    for secret in ('access-token-1', 'rotated-token-1', 'refresh-from-env', 'client-secret'):
        assert secret not in caplog.text


def test_scopes_are_the_regrouped_list_asked_in_one_authorisation(config: Config) -> None:
    asked = {
        'user-read-playback-state',
        'user-modify-playback-state',
        'user-read-currently-playing',
        'user-read-private',
        'user-library-read',
        'user-library-modify',
        'user-read-playback-position',
        'playlist-read-private',
        'playlist-read-collaborative',
        'playlist-modify-private',
        'playlist-modify-public',
    }
    assert set(SCOPES.split()) == asked and len(SCOPES.split()) == len(asked)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(Spotify(config).authorize_url()).query)
    assert set(query['scope'][0].split()) == asked  # streaming and user-read-email are no longer asked


def test_each_grant_logs_its_scopes_and_warns_about_the_missing_ones(
    config: Config, spotify_http, caplog: pytest.LogCaptureFixture
) -> None:
    older = 'streaming user-modify-playback-state user-library-read user-library-modify user-read-playback-state'
    token = {'access_token': 'secret-access', 'token_type': 'Bearer', 'expires_in': 3600, 'scope': older}
    spotify_http.reply('POST', 'token', 200, token).reply('PUT', '/me/player/pause', 204)
    with caplog.at_level(logging.INFO):
        Spotify(config).control('pause')
    warning = next(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert 'user-read-playback-position' in warning and 'playlist-modify-private' in warning
    assert 'user-library-modify' not in warning and 'make spotify-auth' in warning
    assert 'secret-access' not in caplog.text


def test_one_kept_alive_session_carries_every_call(
    config: Config, spotify_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions: list[int] = []

    def counted(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
        sessions.append(id(session))
        return spotify_http(method, url, **kwargs)

    monkeypatch.setattr(requests.Session, 'request', counted)
    spotify_http.token().reply('PUT', '/me/player/pause', 204).reply('POST', '/me/player/next', 204)
    spotify = Spotify(config)
    spotify.control('pause')
    spotify.control('next')
    assert len(sessions) == 3 and len(set(sessions)) == 1


@pytest.mark.parametrize(
    ('devices', 'chosen'),
    [
        ([PHONE | {'is_active': True}, MAC, OTHER_COMPUTER], 'phone'),
        ([PHONE, OTHER_COMPUTER, MAC], 'mac'),
        ([PHONE, OTHER_COMPUTER], None),  # someone else's computer is never the Mac's app
        ([PHONE, MAC | {'id': None}, OTHER_COMPUTER], None),
        ([TV, PHONE, MAC], 'mac'),  # an active device that takes no command is passed over
        ([TV, PHONE], None),
        ([PHONE, MAC | {'is_restricted': True}], None),
    ],
)
def test_the_device_is_the_active_one_else_the_macs_own_app(
    config: Config, spotify_http, devices: list, chosen: str | None
) -> None:
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': devices})
    spotify_http.reply('GET', '/search', 200, {'tracks': {'items': [{'name': 'Song', 'uri': 'spotify:track:1'}]}})
    spotify_http.reply('PUT', '/me/player/play', 204)
    if chosen is None:
        with pytest.raises(SpotifyError) as none:
            Spotify(config).play('song', 'track')
        assert (str(none.value), none.value.status) == (NO_DEVICE, 409)
        assert spotify_http.count('PUT', '/me/player/play') == 0
        return
    Spotify(config).play('song', 'track')
    assert spotify_http.calls[-1][2]['params'] == {'device_id': chosen}


def test_the_macs_app_is_found_whatever_the_apostrophe(config: Config, spotify_http) -> None:
    macos = (
        'CAMILLE\u2019S MAC'  # macOS names a Mac with a typographic apostrophe; SPOTIFY_DEVICE_NAME is typed by hand
    )
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [OTHER_COMPUTER, MAC | {'name': macos}]})
    spotify_http.reply('GET', '/search', 200, {'tracks': {'items': [{'name': 'Song', 'uri': 'spotify:track:1'}]}})
    spotify_http.reply('PUT', '/me/player/play', 204)
    assert Spotify(replace(config, spotify_device_name="Camille's Mac")).play('song', 'track').device == macos


@pytest.mark.parametrize('delays', [(3, 3, 3, 3), (0, 0, 5, 5)])  # each call slow; a slow search, then a slow play
def test_every_call_ends_within_the_budget_well_inside_the_robots_wait(
    config: Config, spotify_http, new_clock, monkeypatch: pytest.MonkeyPatch, delays: tuple[int, ...]
) -> None:
    mono, calls = new_clock(), []
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    spotify_http.reply('GET', '/search', 200, {'tracks': {'items': [{'name': 'Song', 'uri': 'spotify:track:1'}]}})
    spotify_http.reply('PUT', '/me/player/play', 204)

    def slow(_session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Takes its delay, or gives up when its connect and read timeouts run out first."""
        limit = sum(kwargs['timeout'])
        calls.append((mono.now, limit))
        delay = delays[len(calls) - 1]
        mono.now += min(delay, limit)
        if delay > limit:
            raise requests.Timeout('slow')
        return spotify_http(method, url, **kwargs)

    monkeypatch.setattr(requests.Session, 'request', slow)
    began = mono.now
    with pytest.raises(SpotifyError) as slow_answer:
        Spotify(config, mono=mono).play('song', 'track')
    assert (str(slow_answer.value), slow_answer.value.status) == (TOO_SLOW, 502)
    assert spotify_http.count('PUT', '/me/player/play') == 0  # never answered: the robot hears it failed
    # No call can run past the budget, retries included, and the budget leaves the robot's wait room for the
    # transit, the thread pool and a reply's last bytes: no music after the robot said it does not know.
    assert all(at + limit <= began + BUDGET_S for at, limit in calls) and mono.now <= began + BUDGET_S
    assert BUDGET_S <= MAC_TIMEOUT_S - 3


def test_calls_that_outlast_their_timeouts_spend_the_budget_and_the_next_is_never_sent(
    config: Config, spotify_http, new_clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read timeout bounds each read, not the call: a reply trickled in can outlast it. Once the budget is spent,
    Spotify is too slow (a 502 the robot says), never a timeout of 0 that requests refuses (a 500)."""
    mono, sent_at = new_clock(), []
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    spotify_http.reply('GET', '/search', 200, {'tracks': {'items': [{'name': 'Song', 'uri': 'spotify:track:1'}]}})
    spotify_http.reply('PUT', '/me/player/play', 204)

    def trickled(_session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
        connect, read = kwargs['timeout']
        requests.adapters.TimeoutSauce(connect=connect, read=read)  # requests' own check: ValueError at 0 or less
        sent_at.append(mono.now)
        mono.now += BUDGET_S / 2 + 0.5
        return spotify_http(method, url, **kwargs)

    monkeypatch.setattr(requests.Session, 'request', trickled)
    began = mono.now
    with pytest.raises(SpotifyError) as slow_answer:
        Spotify(config, mono=mono).play('song', 'track')
    assert (str(slow_answer.value), slow_answer.value.status) == (TOO_SLOW, 502)
    assert _sent(spotify_http) == [('GET', '/me/player/devices')]  # the token, the devices, then nothing
    assert all(at < began + BUDGET_S for at in sent_at)


PREMIUM = {'message': 'Player command failed: Premium required', 'reason': 'PREMIUM_REQUIRED'}
NO_ACTIVE = {'message': 'Player command failed: No active device found', 'reason': 'NO_ACTIVE_DEVICE'}


@pytest.mark.parametrize(
    ('status', 'error', 'answer'),
    [
        (403, PREMIUM, (502, 'Spotify Premium est nécessaire pour que le robot pilote la musique.')),
        (404, NO_ACTIVE, (409, NO_DEVICE)),
        (403, {'message': 'Insufficient client scope'}, (503, MISSING_SCOPE)),
        (
            429,
            {'message': 'API rate limit exceeded'},
            (502, 'Spotify reçoit trop de demandes\xa0: réessaie dans un instant.'),
        ),
        (
            503,
            {'message': 'Service unavailable'},
            (502, 'Spotify a un problème de son côté (erreur 503)\xa0: réessaie dans un instant.'),
        ),
        (400, {'message': 'Device not found'}, (502, 'Spotify a refusé la lecture (erreur 400).')),
    ],
)
def test_spotify_refusals_are_said_in_french_and_its_own_words_only_logged(
    client: TestClient, spotify_http, caplog: pytest.LogCaptureFixture, status: int, error: dict, answer: tuple
) -> None:
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    spotify_http.reply('GET', '/search', 200, {'tracks': {'items': [{'name': 'Song', 'uri': 'spotify:track:1'}]}})
    spotify_http.reply('PUT', '/me/player/play', status, {'error': {'status': status} | error})
    with caplog.at_level(logging.WARNING):
        response = client.post('/api/spotify/play', json={'query': 'song'})
    assert (response.status_code, response.json()['detail']) == answer
    assert error['message'] in caplog.text


def test_a_missing_scope_is_told_apart_from_a_closed_endpoint(client: TestClient, spotify_http) -> None:
    spotify_http.token()
    spotify_http.reply('GET', '/me/player', 200, PLAYING)
    spotify_http.reply('PUT', '/me/library', 403, {'error': {'status': 403, 'message': 'Insufficient client scope'}})
    spotify_http.reply('PUT', '/me/library', 403, None)  # a closed endpoint: no message at all
    scope = client.post('/api/spotify/control', json={'action': 'like'})
    assert (scope.status_code, scope.json()) == (503, {'detail': MISSING_SCOPE})
    assert 'make spotify-auth' in MISSING_SCOPE and 'scope manquant' in MISSING_SCOPE
    closed = client.post('/api/spotify/control', json={'action': 'like'})
    assert (closed.status_code, closed.json()) == (502, {'detail': 'Spotify a refusé le like (erreur 403).'})


def test_no_device_is_a_409_the_robot_can_say(client: TestClient, spotify_http) -> None:
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [PHONE]})
    response = client.post('/api/spotify/play', json={'query': 'Daft Punk'})
    assert response.status_code == 409
    assert response.json() == {'detail': NO_DEVICE}
    assert response.json()['detail'] == 'Aucun appareil Spotify disponible\xa0: ouvre Spotify sur le Mac.'


def test_an_artist_plays_as_a_context_under_its_name_alone(client: TestClient, spotify_http) -> None:
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    artist = {'type': 'artist', 'name': 'Daft Punk', 'uri': 'spotify:artist:4tZ'}
    spotify_http.reply('GET', '/search', 200, {'artists': {'items': [artist]}})
    spotify_http.reply('PUT', '/me/player/play', 204)
    played = client.post('/api/spotify/play', json={'query': 'daft punk', 'kind': 'artist'}).json()
    assert _last(spotify_http, 'GET', '/search')['params']['type'] == 'artist'
    assert spotify_http.calls[-1][2]['json'] == {'context_uri': 'spotify:artist:4tZ'}
    assert played['track'] == {'title': 'Daft Punk', 'artist': '', 'uri': 'spotify:artist:4tZ'}  # not « de » itself


def test_play_a_track_and_a_playlist(client: TestClient, spotify_http) -> None:
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    track = {'name': 'One More Time', 'uri': 'spotify:track:1', 'artists': [{'name': 'Daft Punk'}]}
    playlist = {'name': 'Jazz du soir', 'uri': 'spotify:playlist:2', 'owner': {'display_name': 'Camille'}}
    spotify_http.reply('GET', '/search', 200, {'tracks': {'items': [track]}})
    spotify_http.reply('GET', '/search', 200, {'playlists': {'items': [None, playlist]}})
    spotify_http.reply('GET', '/me/playlists', 200, {'items': [{'name': 'Chill', 'uri': 'spotify:playlist:3'}]})
    spotify_http.reply('PUT', '/me/player/play', 204)

    played = client.post('/api/spotify/play', json={'query': 'one more time'}).json()
    assert played == {
        'track': {'title': 'One More Time', 'artist': 'Daft Punk', 'uri': 'spotify:track:1'},
        'device': 'STUDIO MAC',
    }
    search = _last(spotify_http, 'GET', '/search')['params']
    assert search == {'q': 'one more time', 'type': 'track', 'limit': SEARCH_MAX, 'market': 'from_token'}
    assert SEARCH_MAX > 1  # the fake honours `limit`: the playlist below comes after a null entry
    assert spotify_http.calls[-1][2]['json'] == {'uris': ['spotify:track:1']}

    played = client.post('/api/spotify/play', json={'query': 'jazz', 'kind': 'playlist'}).json()
    assert played['track'] == {'title': 'Jazz du soir', 'artist': 'Camille', 'uri': 'spotify:playlist:2'}
    assert spotify_http.calls[-1][2]['json'] == {'context_uri': 'spotify:playlist:2'}
    assert _sent(spotify_http)[-4:] == [  # none of mine is called « jazz »: the search
        ('GET', '/me/player/devices'),
        ('GET', '/me/playlists'),
        ('GET', '/search'),
        ('PUT', '/me/player/play'),
    ]


def test_my_own_playlist_is_found_by_name_before_any_search(config: Config, spotify_http) -> None:
    mine = [
        None,
        {'name': 'Chill', 'uri': 'spotify:playlist:1', 'owner': {'display_name': 'Camille'}},
        {'name': 'Café Jazz du Soir', 'uri': 'spotify:playlist:2', 'owner': {'display_name': 'Camille'}},
    ]
    spotify_http.token().reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    spotify_http.reply('GET', '/me/playlists', 200, {'items': mine}).reply('PUT', '/me/player/play', 204)
    played = Spotify(config).play('jazz du soir', 'playlist')
    assert played.track.uri == 'spotify:playlist:2'
    assert _last(spotify_http, 'GET', '/me/playlists')['params'] == {'limit': 50}
    assert spotify_http.count('GET', '/search') == 0
    assert _last(spotify_http, 'PUT', '/me/player/play')['json'] == {'context_uri': 'spotify:playlist:2'}


def test_a_library_name_matches_whole_words_and_the_exact_name_first(config: Config, spotify_http) -> None:
    mine = [
        {'name': 'Thérapie de groupe', 'uri': 'spotify:playlist:t'},
        {'name': 'Jazz Manouche', 'uri': 'spotify:playlist:m'},
        {'name': 'JAZZ', 'uri': 'spotify:playlist:j'},
    ]
    rap = {'name': 'Rap FR', 'uri': 'spotify:playlist:r', 'owner': {'display_name': 'Spotify'}}
    spotify_http.token().reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    spotify_http.reply('GET', '/me/playlists', 200, {'items': mine}).reply('PUT', '/me/player/play', 204)
    spotify_http.reply('GET', '/search', 200, {'playlists': {'items': [rap]}})
    spotify = Spotify(config)
    assert spotify.play('jazz', 'playlist').track.uri == 'spotify:playlist:j'  # not the « Jazz Manouche » before it
    assert spotify.play('manouche', 'playlist').track.uri == 'spotify:playlist:m'
    assert spotify_http.count('GET', '/search') == 0
    assert spotify.play('rap', 'playlist').track.uri == 'spotify:playlist:r'  # « Thérapie » is not « rap »: searched


def test_no_result_is_said_in_french(client: TestClient, spotify_http) -> None:
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    spotify_http.reply('GET', '/search', 200, {'albums': {'items': []}})
    response = client.post('/api/spotify/play', json={'query': 'zzz', 'kind': 'album'})
    assert (response.status_code, response.json()) == (404, {'detail': 'Aucun résultat Spotify pour «\xa0zzz\xa0».'})


def test_liked_songs_are_shuffled_on_the_same_device(client: TestClient, spotify_http) -> None:
    spotify_http.token()
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [MAC | {'is_active': True}]})
    saved = [{'track': {'name': f'T{i}', 'uri': f'spotify:track:{i}', 'artists': [{'name': 'A'}]}} for i in range(50)]
    spotify_http.reply('GET', '/me/tracks', 200, {'items': saved})
    spotify_http.reply('PUT', '/me/player/play', 204)
    played = client.post('/api/spotify/play', json={'kind': 'liked'}).json()
    assert _last(spotify_http, 'GET', '/me/tracks')['params']['limit'] == 50
    uris = spotify_http.calls[-1][2]['json']['uris']
    assert sorted(uris) == sorted(s['track']['uri'] for s in saved)
    assert uris != [s['track']['uri'] for s in saved]  # 1 chance in 50! to fail
    assert played['track']['uri'] == uris[0]
    assert played['device'] == 'STUDIO MAC'
    assert client.post('/api/spotify/play', json={'kind': 'album'}).status_code == 422  # only `liked` needs no query


BOOK = {
    'name': 'Le Seigneur des Anneaux 1 - La Communauté',
    'uri': 'spotify:show:5Dnk',  # what /me/audiobooks and the search give for a book
    'type': 'audiobook',
    'authors': [{'name': 'J.R.R. Tolkien'}],
}


def _chapter(n: int, heard: bool | None = None, at: int = 0) -> dict[str, Any]:
    chapter = {'name': f'Track {n}', 'uri': f'spotify:episode:c{n}', 'chapter_number': n, 'is_playable': True}
    return chapter | ({} if heard is None else {'resume_point': {'fully_played': heard, 'resume_position_ms': at}})


def test_an_audiobook_resumes_at_its_first_unfinished_chapter(config: Config, spotify_http) -> None:
    """69 chapters: the first page all heard, the second one started at 12:34 in its 53rd."""
    spotify_http.token().reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    other = BOOK | {'name': 'Harry Potter', 'uri': 'spotify:show:hp'}
    spotify_http.reply('GET', '/me/audiobooks', 200, {'items': [other, BOOK]})
    first = {'items': [_chapter(n, heard=True, at=1000) for n in range(50)], 'next': 'https://api/next'}
    second = {'items': [_chapter(50, True), _chapter(51, True), _chapter(52, False, 754_000), _chapter(53, False)]}
    spotify_http.reply('GET', '/audiobooks/5Dnk/chapters', 200, first).reply(
        'GET', '/audiobooks/5Dnk/chapters', 200, second
    )
    spotify_http.reply('PUT', '/me/player/play', 204)

    played = Spotify(config).play('seigneur des anneaux 1', 'audiobook')
    assert played.track.model_dump() == {'title': BOOK['name'], 'artist': 'J.R.R. Tolkien', 'uri': 'spotify:show:5Dnk'}
    pages = [kwargs['params'] for _, path, kwargs in spotify_http.calls if path == '/audiobooks/5Dnk/chapters']
    assert [(p['offset'], p['limit'], p['market']) for p in pages] == [(0, 50, 'from_token'), (50, 50, 'from_token')]
    assert _last(spotify_http, 'PUT', '/me/player/play')['json'] == {
        'context_uri': 'spotify:show:5Dnk',
        'offset': {'uri': 'spotify:episode:c52'},
        'position_ms': 754_000,
    }
    assert spotify_http.count('GET', '/search') == 0


def test_an_audiobook_without_resume_points_is_started_as_a_whole(config: Config, spotify_http) -> None:
    """No user-read-playback-position yet: never chapter 1 from zero, the book alone (Spotify's own resume)."""
    spotify_http.token().reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    spotify_http.reply('GET', '/me/audiobooks', 200, {'items': [BOOK]})  # not the one asked for
    prince = {'name': 'Le Petit Prince', 'uri': 'spotify:show:pp', 'authors': [{'name': 'Saint-Exupéry'}]}
    spotify_http.reply('GET', '/search', 200, {'audiobooks': {'items': [None, prince]}})
    spotify_http.reply('GET', '/audiobooks/pp/chapters', 200, {'items': [_chapter(0), _chapter(1)], 'next': 'x'})
    spotify_http.reply('PUT', '/me/player/play', 204)

    played = Spotify(config).play('Le Petit Prince', 'audiobook')
    assert (played.track.title, played.track.artist) == ('Le Petit Prince', 'Saint-Exupéry')
    assert _last(spotify_http, 'GET', '/search')['params']['type'] == 'audiobook'
    assert spotify_http.count('GET', '/audiobooks/pp/chapters') == 1  # no resume points: no second page either
    assert _last(spotify_http, 'PUT', '/me/player/play')['json'] == {'context_uri': 'spotify:show:pp'}


def _episode(day: str, at: int | None = None, heard: bool = False) -> dict[str, Any]:
    point = {} if at is None else {'resume_point': {'fully_played': heard, 'resume_position_ms': at}}
    return {'name': f'Journal du {day}', 'uri': f'spotify:episode:{day}', 'release_date': day} | point


@pytest.mark.parametrize(
    ('episodes', 'chosen', 'position'),
    [
        ([_episode('2026-10-30'), _episode('2026-10-31'), _episode('2026-10-29')], '2026-10-31', 0),
        ([_episode('2026-10-31', 0), _episode('2026-10-30', 0, heard=True)], '2026-10-31', 0),
        (
            [_episode('2026-10-31', 0), _episode('2026-10-30', 125_000), _episode('2026-10-29', 9_000)],
            '2026-10-30',
            125_000,
        ),
        ([_episode('2026-10-31', 0), _episode('2026-10-30', 3_000_000, heard=True)], '2026-10-31', 0),
    ],
)
def test_a_podcast_plays_its_episode_in_progress_else_its_latest(
    config: Config, spotify_http, episodes: list, chosen: str, position: int
) -> None:
    spotify_http.token().reply('GET', '/me/player/devices', 200, {'devices': [MAC]})
    journal = {'name': 'Journal de 8 h', 'uri': 'spotify:show:j8', 'media_type': 'audio'}
    spotify_http.reply('GET', '/me/shows', 200, {'items': [{'added_at': 'x', 'show': journal}]})
    spotify_http.reply('GET', '/shows/j8/episodes', 200, {'items': [None, *episodes]})
    spotify_http.reply('PUT', '/me/player/play', 204)

    spotify_http.reply('GET', '/search', 200, {'shows': {'items': [None, journal]}})
    Spotify(config).play('journal de 8h', 'podcast')  # « 8h » is not « 8 h »: not mine by name, so searched
    assert _last(spotify_http, 'GET', '/search')['params']['type'] == 'show'
    played = Spotify(config).play('journal de 8 h', 'podcast')  # mine: no search this time
    assert spotify_http.count('GET', '/search') == 1
    assert _last(spotify_http, 'GET', '/shows/j8/episodes')['params'] == {'limit': SEARCH_MAX, 'market': 'from_token'}
    assert _last(spotify_http, 'PUT', '/me/player/play')['json'] == {
        'uris': [f'spotify:episode:{chosen}'],
        'position_ms': position,
    }
    assert played.track.model_dump() == {
        'title': f'Journal du {chosen}',
        'artist': 'Journal de 8 h',
        'uri': f'spotify:episode:{chosen}',
    }


def _refusal(status: int, reason: str, message: str = 'Player command failed') -> tuple[int, dict]:
    return status, {'error': {'status': status, 'message': message, 'reason': reason}}


@pytest.mark.parametrize(
    ('reply', 'answer'),
    [
        ((204, None), (200, 'Lecture en pause.')),
        ((404, None), (200, 'Rien ne jouait.')),
        (_refusal(404, 'NO_ACTIVE_DEVICE'), (200, 'Rien ne jouait.')),
        ((403, None), (200, 'Rien ne jouait.')),
        (_refusal(403, 'ALREADY_PAUSED'), (200, 'Rien ne jouait.')),
        (_refusal(403, 'UNKNOWN', 'Player command failed: Restriction violated'), (200, 'Rien ne jouait.')),
        (
            _refusal(403, 'PREMIUM_REQUIRED'),
            (502, 'Spotify Premium est nécessaire pour que le robot pilote la musique.'),
        ),
        (
            _refusal(403, 'DEVICE_NOT_CONTROLLABLE'),
            (502, 'Cet appareil Spotify ne se laisse pas piloter à distance\xa0: ouvre Spotify sur le Mac.'),
        ),
        (_refusal(400, 'UNKNOWN'), (502, 'Spotify a refusé la pause (erreur 400).')),
    ],
)
def test_pause_says_whether_something_was_playing_or_why_it_could_not(
    client: TestClient, spotify_http, reply: tuple[int, Any], answer: tuple[int, str]
) -> None:
    """Only a player with nothing playing is « rien ne jouait »: a refusal while music plays is said as one."""
    spotify_http.token()
    spotify_http.reply('PUT', '/me/player/pause', *reply)
    response = client.post('/api/spotify/control', json={'action': 'pause'})
    body = response.json()
    assert (response.status_code, body.get('message') or body.get('detail')) == answer


@pytest.mark.parametrize(
    'reply', [_refusal(403, 'ALREADY_PLAYING'), _refusal(403, 'UNKNOWN', 'Player command failed: Restriction violated')]
)
def test_resume_while_music_plays_is_no_failure(client: TestClient, spotify_http, reply: tuple[int, Any]) -> None:
    spotify_http.token().reply('PUT', '/me/player/play', *reply)
    response = client.post('/api/spotify/control', json={'action': 'resume'})
    assert (response.status_code, response.json().get('message')) == (200, 'La lecture était déjà en cours.')


@pytest.mark.parametrize(
    ('ask', 'call'),
    [
        ({'action': 'resume'}, ('PUT', '/me/player/play')),
        ({'action': 'next'}, ('POST', '/me/player/next')),
        ({'action': 'queue', 'value': 'get lucky'}, ('POST', '/me/player/queue')),
    ],
)
def test_with_no_active_device_a_command_goes_to_the_macs_own_app(
    client: TestClient, spotify_http, ask: dict, call: tuple[str, str]
) -> None:
    """The Mac's app can be open, and listed, without being the active device: the command is sent to it by id."""
    spotify_http.token().reply('GET', '/search', 200, {'tracks': {'items': [GET_LUCKY]}})
    spotify_http.reply(*call, *_refusal(404, 'NO_ACTIVE_DEVICE')).reply(*call, 204)
    spotify_http.reply('GET', '/me/player/devices', 200, {'devices': [PHONE, MAC]})
    response = client.post('/api/spotify/control', json=ask)
    assert response.status_code == 200, response.text
    first, retry = [kwargs.get('params') or {} for m, p, kwargs in spotify_http.calls if (m, p) == call]
    assert 'device_id' not in first and retry == first | {'device_id': 'mac'}


@pytest.mark.parametrize(
    ('ask', 'call', 'said'),
    [
        ({'action': 'resume'}, ('PUT', '/me/player/play', None), 'Lecture reprise.'),
        ({'action': 'next'}, ('POST', '/me/player/next', None), 'Morceau suivant lancé.'),
        ({'action': 'previous'}, ('POST', '/me/player/previous', None), 'Morceau précédent lancé.'),
        ({'action': 'shuffle'}, ('PUT', '/me/player/shuffle', {'state': 'true'}), 'Lecture aléatoire activée.'),
        (
            {'action': 'shuffle', 'value': 'OFF'},
            ('PUT', '/me/player/shuffle', {'state': 'false'}),
            'Lecture aléatoire désactivée.',
        ),
        ({'action': 'repeat', 'value': 'on'}, ('PUT', '/me/player/repeat', {'state': 'context'}), 'Lecture en boucle.'),
        (
            {'action': 'repeat', 'value': 'track'},
            ('PUT', '/me/player/repeat', {'state': 'track'}),
            'Ce morceau passe en boucle.',
        ),
        (
            {'action': 'repeat', 'value': 'off'},
            ('PUT', '/me/player/repeat', {'state': 'off'}),
            'Répétition désactivée.',
        ),
        (
            {'action': 'volume', 'amount': 34.6},
            ('PUT', '/me/player/volume', {'volume_percent': 35}),
            'Volume de Spotify réglé à 35 sur 100.',
        ),
        (
            {'action': 'volume', 'amount': 180},
            ('PUT', '/me/player/volume', {'volume_percent': 100}),
            'Volume de Spotify réglé à 100 sur 100.',
        ),
    ],
)
def test_the_one_call_actions(client: TestClient, spotify_http, ask: dict, call: tuple, said: str) -> None:
    method, path, params = call
    spotify_http.token().reply(method, path, 204)
    response = client.post('/api/spotify/control', json=ask)
    assert (response.status_code, response.json()['message']) == (200, said)
    assert _sent(spotify_http) == [(method, path)]
    assert _last(spotify_http, method, path).get('params') == params and 'json' not in _last(spotify_http, method, path)
    if ask['action'] == 'volume':
        assert response.json()['volume'] == params['volume_percent']  # data.volume for the transcript


@pytest.mark.parametrize(
    ('reason', 'said'),
    [
        ('NO_NEXT_TRACK', "Il n'y a pas de morceau suivant."),
        ('VOLUME_CONTROL_DISALLOW', 'Cet appareil Spotify ne laisse pas régler son volume à distance.'),
    ],
)
def test_a_player_refusal_is_said_in_french(config: Config, spotify_http, reason: str, said: str) -> None:
    spotify_http.token().reply('POST', '/me/player/next', *_refusal(403, reason))
    spotify_http.reply('PUT', '/me/player/volume', *_refusal(403, reason))
    with pytest.raises(SpotifyError) as refused:
        Spotify(config).control('next' if reason == 'NO_NEXT_TRACK' else 'volume', amount=10)
    assert str(refused.value) == said


def test_now_says_what_plays_and_where(config: Config, spotify_http) -> None:
    episode = {
        'type': 'episode',
        'name': 'Le grand débat',
        'uri': 'spotify:episode:e1',
        'show': {'name': 'France Inter'},
    }
    spotify_http.token().reply('GET', '/me/player', 200, PLAYING)
    spotify_http.reply('GET', '/me/player', 200, {'is_playing': False, 'device': {'name': 'Salon'}, 'item': episode})
    spotify_http.reply('GET', '/me/player', 204)
    spotify = Spotify(config)

    now = spotify.control('now')
    assert now.message == 'En cours\xa0: Get Lucky, de Daft Punk, sur STUDIO MAC, volume Spotify à 40.'
    assert (now.track.uri, now.device, now.volume) == ('spotify:track:9', 'STUDIO MAC', 40)
    assert _last(spotify_http, 'GET', '/me/player')['params'] == {'additional_types': 'episode'}
    now = spotify.control('now')
    assert now.message == 'En pause\xa0: Le grand débat, de France Inter, sur Salon.'
    assert (now.track.artist, now.volume) == ('France Inter', None)
    nothing = spotify.control('now')  # an answer, not a failure: the robot says it as an ordinary result
    assert (nothing.message, nothing.track, nothing.device) == (NOTHING_PLAYING, None, None)


def test_now_with_spotify_out_of_reach_says_why_not_that_nothing_plays(client: TestClient, spotify_http) -> None:
    spotify_http.token().reply('GET', '/me/player', 503, {'error': {'status': 503, 'message': 'Unavailable'}})
    down = client.post('/api/spotify/control', json={'action': 'now'})
    assert (down.status_code, down.json()) == (
        502,
        {'detail': 'Spotify a un problème de son côté (erreur 503)\xa0: réessaie dans un instant.'},
    )
    spotify_http.error = requests.ConnectionError('no route')
    offline = client.post('/api/spotify/control', json={'action': 'now'})
    assert (offline.status_code, offline.json()) == (
        502,
        {'detail': 'Impossible de joindre Spotify\xa0: le Mac a-t-il du réseau\xa0?'},
    )


def test_like_saves_what_plays_to_the_library(config: Config, spotify_http) -> None:
    spotify_http.token().reply('GET', '/me/player', 200, PLAYING).reply('PUT', '/me/library', 200)
    liked = Spotify(config).control('like')
    assert _sent(spotify_http) == [('GET', '/me/player'), ('PUT', '/me/library')]
    assert _last(spotify_http, 'PUT', '/me/library')['params'] == {'uris': 'spotify:track:9'}
    assert (liked.message, liked.track.title) == ('«\xa0Get Lucky\xa0» ajouté à tes titres likés.', 'Get Lucky')

    spotify_http.replies[('GET', '/me/player')] = [(204, None)]  # nothing plays
    with pytest.raises(SpotifyError) as nothing:
        Spotify(config).control('like')
    assert nothing.value.status == 404 and spotify_http.count('PUT', '/me/library') == 1


def test_queue_searches_a_track_and_adds_it(config: Config, spotify_http) -> None:
    spotify_http.token().reply('GET', '/search', 200, {'tracks': {'items': [GET_LUCKY]}})
    spotify_http.reply('POST', '/me/player/queue', 204)
    queued = Spotify(config).control('queue', ' get lucky ')
    assert _sent(spotify_http) == [('GET', '/search'), ('POST', '/me/player/queue')]
    assert _last(spotify_http, 'GET', '/search')['params']['q'] == 'get lucky'
    assert _last(spotify_http, 'POST', '/me/player/queue')['params'] == {'uri': 'spotify:track:9'}
    assert queued.message == 'Ajouté à la file\xa0: Get Lucky, de Daft Punk.'


@pytest.mark.parametrize(
    ('amount', 'position', 'said'), [(30, 91_000, 'Avancé de 30 secondes.'), (-90, 0, 'Reculé de 90 secondes.')]
)
def test_seek_moves_from_where_it_plays(config: Config, spotify_http, amount: float, position: int, said: str) -> None:
    spotify_http.token().reply('GET', '/me/player', 200, PLAYING).reply('PUT', '/me/player/seek', 204)
    assert Spotify(config).control('seek', amount=amount).message == said
    assert _last(spotify_http, 'PUT', '/me/player/seek')['params'] == {'position_ms': position}


@pytest.mark.parametrize(
    ('value', 'chosen'),
    [
        ('salon', 'room'),  # the exact name before the one that contains it
        ('enceinte', 'speaker'),
        ('CAMILLE\u2019S MAC', 'mac'),
        ('mac', 'mac'),  # whole words: not inside « iMac de Alice », listed first
    ],
)
def test_device_moves_playback_to_the_device_of_that_name(
    client: TestClient, spotify_http, value: str, chosen: str
) -> None:
    devices = [
        {'id': 'imac', 'name': 'iMac de Alice', 'is_active': False},
        {'id': 'speaker', 'name': 'Enceinte du Salon', 'is_active': False},
        {'id': 'room', 'name': 'Salon', 'is_active': False},
        MAC | {'name': "Camille's Mac"},
        {'id': None, 'name': 'Sans identifiant'},
    ]
    spotify_http.token().reply('GET', '/me/player/devices', 200, {'devices': devices}).reply('PUT', '/me/player', 204)
    moved = client.post('/api/spotify/control', json={'action': 'device', 'value': value}).json()
    assert _last(spotify_http, 'PUT', '/me/player')['json'] == {'device_ids': [chosen], 'play': True}
    assert moved['device'] == next(d['name'] for d in devices if d['id'] == chosen)
    assert moved['message'] == f'Lecture passée sur {moved["device"]}.'


@pytest.mark.parametrize(
    ('listed', 'value', 'chosen'),
    [
        (['iMac de Alice', 'MacBook Pro de Camille', 'iPhone'], 'mac', 'MacBook Pro de Camille'),  # this Mac's own name
        (['iMac de Alice', 'iPhone'], 'mac', None),  # the start of a word, never its inside
        (['Enceintes du salon', 'iPhone'], 'enceinte', 'Enceintes du salon'),
        (['Télévision Samsung', 'iPhone'], 'télé', 'Télévision Samsung'),
        (['MacBook Pro de Camille', 'Salle Mac'], 'mac', 'Salle Mac'),  # a whole word before the start of one
        (['MacBook Pro de Camille', 'iMac de Alice'], 'imac', 'iMac de Alice'),
    ],
)
def test_device_said_by_the_start_of_the_words_of_its_name(
    client: TestClient, spotify_http, listed: list[str], value: str, chosen: str | None
) -> None:
    devices = [{'id': f'd{n}', 'name': name, 'is_active': False} for n, name in enumerate(listed)]
    spotify_http.token().reply('GET', '/me/player/devices', 200, {'devices': devices}).reply('PUT', '/me/player', 204)
    moved = client.post('/api/spotify/control', json={'action': 'device', 'value': value})
    if chosen is None:
        assert (moved.status_code, spotify_http.count('PUT', '/me/player')) == (404, 0)
        return
    assert moved.status_code == 200, moved.json()
    assert moved.json()['message'] == f'Lecture passée sur {chosen}.'
    assert _last(spotify_http, 'PUT', '/me/player')['json']['device_ids'] == [f'd{listed.index(chosen)}']


def test_device_unknown_or_restricted_is_said(client: TestClient, spotify_http) -> None:
    devices = [{'id': 'tv', 'name': 'Télé', 'is_restricted': True}, PHONE]
    spotify_http.token().reply('GET', '/me/player/devices', 200, {'devices': devices})
    unknown = client.post('/api/spotify/control', json={'action': 'device', 'value': 'cuisine'})
    assert (unknown.status_code, unknown.json()['detail']) == (
        404,
        'Aucun appareil Spotify nommé «\xa0cuisine\xa0» (disponibles\xa0: Télé, iPhone).',
    )
    restricted = client.post('/api/spotify/control', json={'action': 'device', 'value': 'télé'})
    assert (restricted.status_code, restricted.json()['detail']) == (
        502,
        '«\xa0Télé\xa0» ne se laisse pas piloter à distance.',
    )
    assert spotify_http.count('PUT', '/me/player') == 0


def test_playlist_keeps_what_was_started_or_queued_today(config: Config, spotify_http, new_clock) -> None:
    wall = new_clock(DAY - 86_400)
    spotify = Spotify(config, wall=wall)
    track = {'name': 'One More Time', 'uri': 'spotify:track:1', 'artists': [{'name': 'Daft Punk'}]}
    album = {'name': 'Discovery', 'uri': 'spotify:album:2', 'artists': [{'name': 'Daft Punk'}]}
    spotify_http.token().reply('GET', '/me/player/devices', 200, {'devices': [MAC]}).reply(
        'PUT', '/me/player/play', 204
    )
    spotify_http.reply('GET', '/search', 200, {'tracks': {'items': [track]}})
    spotify_http.reply('POST', '/me/player/queue', 204)
    spotify.play('one more time', 'track')  # yesterday: not in today's playlist
    wall.now = DAY
    with pytest.raises(SpotifyError) as nothing:
        spotify.control('playlist', 'Soirée')
    assert nothing.value.status == 404 and spotify_http.count('POST', '/me/playlists') == 0

    spotify.control('queue', 'get lucky')  # the search answers « One More Time » again
    spotify_http.replies[('GET', '/search')] = [(200, {'tracks': {'items': [GET_LUCKY]}})]
    spotify.play('get lucky', 'track')
    spotify.control('queue', 'get lucky')  # twice the same: once in the playlist
    spotify_http.replies[('GET', '/search')] = [(200, {'albums': {'items': [album]}})]
    spotify.play('discovery', 'album')  # an album is a context, not a track a playlist can hold
    spotify_http.reply('POST', '/me/playlists', 201, {'id': 'pl1', 'name': 'Soirée', 'uri': 'spotify:playlist:pl1'})
    spotify_http.reply('POST', '/playlists/pl1/items', 201, {'snapshot_id': 's'})

    made = spotify.control('playlist', ' Soirée ')
    assert _last(spotify_http, 'POST', '/me/playlists')['json'] == {'name': 'Soirée', 'public': False}
    assert _last(spotify_http, 'POST', '/playlists/pl1/items')['json'] == {
        'uris': ['spotify:track:1', 'spotify:track:9']
    }
    assert made.message == 'Playlist «\xa0Soirée\xa0» créée avec 2 titres.'
    assert made.track is not None and made.track.uri == 'spotify:playlist:pl1'


def test_playlist_reads_the_memory_while_another_request_adds_to_it(config: Config, spotify_http, new_clock) -> None:
    spotify, today = Spotify(config, wall=new_clock(DAY)), date.fromtimestamp(DAY)

    class Busy:
        """A day that, once compared, has another request add a track: a thread switch in mid-read."""

        __hash__ = None  # type: ignore[assignment]

        def __eq__(self, other: object) -> bool:
            spotify._played.append((today, 'spotify:track:later'))
            return False

    spotify._played.extend([(Busy(), 'spotify:track:old'), (today, 'spotify:track:1')])
    spotify_http.token().reply('POST', '/me/playlists', 201, {'id': 'pl1', 'name': 'S', 'uri': 'spotify:playlist:pl1'})
    spotify_http.reply('POST', '/playlists/pl1/items', 201, {'snapshot_id': 's'})
    assert spotify.control('playlist', 'Soirée').message == 'Playlist «\xa0Soirée\xa0» créée avec 1 titre.'
    assert _last(spotify_http, 'POST', '/playlists/pl1/items')['json'] == {'uris': ['spotify:track:1']}


def test_a_playlist_whose_tracks_did_not_go_in_is_unfollowed_so_no_empty_one_stays(
    config: Config, spotify_http, new_clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Created, then the add failed: the playlist made is taken out of the library (Spotify deletes none: an
    unfollow) before the robot says to try again. Nothing is kept: a retry, today or a week later, makes one."""
    clock = new_clock(DAY)
    spotify = Spotify(config, wall=clock)
    spotify._played.append((date.fromtimestamp(DAY), 'spotify:track:1'))
    spotify_http.token()
    for n in (1, 2, 3):
        spotify_http.reply(
            'POST', '/me/playlists', 201, {'id': f'pl{n}', 'name': 'Soirée', 'uri': f'spotify:playlist:pl{n}'}
        )
    spotify_http.reply('POST', '/playlists/pl1/items', 500, {'error': {'status': 500, 'message': 'Server error'}})
    spotify_http.reply('DELETE', '/me/library', 200)
    with pytest.raises(SpotifyError) as failed:
        spotify.control('playlist', 'Soirée')
    assert str(failed.value) == 'Spotify a un problème de son côté (erreur 500)\xa0: réessaie dans un instant.'
    assert _sent(spotify_http)[-3:] == [
        ('POST', '/me/playlists'),
        ('POST', '/playlists/pl1/items'),
        ('DELETE', '/me/library'),
    ]
    assert _last(spotify_http, 'DELETE', '/me/library')['params'] == {'uris': 'spotify:playlist:pl1'}

    spotify_http.reply('POST', '/playlists/pl2/items', 201, {'snapshot_id': 's'})
    made = spotify.control('playlist', 'soirée')  # asked again: a new one, all of it
    assert (made.message, made.track and made.track.uri) == (
        'Playlist «\xa0soirée\xa0» créée avec 1 titre.',
        'spotify:playlist:pl2',
    )

    clock.now += 7 * 86_400  # a week later, an add that fails and whose unfollow fails too: the add's error is said
    spotify._played.append((date.fromtimestamp(clock.now), 'spotify:track:2'))
    spotify_http.reply('POST', '/playlists/pl3/items', 502, {'error': {'status': 502, 'message': 'Bad gateway'}})
    spotify_http.replies[('DELETE', '/me/library')] = [(503, {'error': {'status': 503, 'message': 'Unavailable'}})]
    with pytest.raises(SpotifyError) as failed:
        spotify.control('playlist', 'Soirée')
    assert str(failed.value) == 'Spotify a un problème de son côté (erreur 502)\xa0: réessaie dans un instant.'
    assert 'spotify:playlist:pl3' in caplog.text  # the empty one left behind is logged
    assert spotify_http.count('POST', '/me/playlists') == 3


@pytest.mark.parametrize(
    ('ask', 'detail'),
    [
        ({'action': 'queue'}, 'File\xa0: dis quel morceau ajouter.'),
        ({'action': 'device', 'value': '  '}, 'Appareil\xa0: dis lequel.'),
        ({'action': 'playlist'}, 'Playlist\xa0: dis sous quel nom la créer.'),
        ({'action': 'volume'}, 'Volume\xa0: dis un niveau de 0 à 100.'),
        ({'action': 'seek'}, 'Avance\xa0: dis de combien de secondes (négatif pour reculer).'),
    ],
)
def test_an_action_without_its_value_or_amount_is_a_422_said_in_french(
    client: TestClient, spotify_http, ask: dict, detail: str
) -> None:
    spotify_http.token()
    response = client.post('/api/spotify/control', json=ask)
    assert (response.status_code, response.json()) == (422, {'detail': detail})
    assert _sent(spotify_http) == []


@pytest.mark.parametrize(
    ('body', 'detail'),
    [
        ('{"action":"volume","amount":NaN}', 'Nombre\xa0: valeur invalide.'),
        ('{"action":"seek","amount":Infinity}', 'Nombre\xa0: valeur invalide.'),
        ('{"action":"volume","amount":-Infinity}', 'Nombre\xa0: valeur invalide.'),
        (
            '{"action":"seek","amount":1e306}',
            'Nombre\xa0: au plus 86\u202f400.',
        ),  # finite, infinite once in milliseconds
        ('{"action":"seek","amount":-1e306}', 'Nombre\xa0: au moins \u221286\u202f400.'),
    ],
)
def test_an_amount_not_finite_or_past_a_day_is_a_422_said_in_french(
    client: TestClient, spotify_http, body: str, detail: str
) -> None:
    spotify_http.token().reply('GET', '/me/player', 200, PLAYING)  # what plays, for a seek to get that far
    response = client.post('/api/spotify/control', content=body, headers={'Content-Type': 'application/json'})
    assert (response.status_code, response.json()) == (422, {'detail': detail})
    assert _sent(spotify_http) == []


def test_a_seek_of_a_day_is_sent(client: TestClient, spotify_http) -> None:
    spotify_http.token().reply('GET', '/me/player', 200, PLAYING).reply('PUT', '/me/player/seek', 204)
    response = client.post('/api/spotify/control', json={'action': 'seek', 'amount': -86_400})
    assert (response.status_code, response.json()['message']) == (200, 'Reculé de 86400 secondes.')
    assert _last(spotify_http, 'PUT', '/me/player/seek')['params'] == {'position_ms': 0}


@pytest.mark.parametrize(
    ('change', 'status', 'detail'),
    [
        ({'spotify_client_id': ''}, 503, 'make spotify-auth'),
        ({'spotify_refresh_token': ''}, 503, "Spotify n'est pas autorisé sur le Mac\xa0: lance make spotify-auth."),
    ],
)
def test_not_configured_is_a_503_saying_how_to_fix_it(config: Config, change: dict, status: int, detail: str) -> None:
    from mac_server.server import create_app  # noqa: PLC0415 — the app is built on the changed config

    with TestClient(create_app(replace(config, **change)), headers={'X-API-Key': 'test-key'}) as test_client:
        response = test_client.post('/api/spotify/control', json={'action': 'pause'})
    assert response.status_code == status
    assert detail in response.json()['detail']


def test_spotify_failures_are_french_502s(client: TestClient, spotify_http) -> None:
    spotify_http.reply('POST', 'token', 400, {'error': 'invalid_grant', 'error_description': 'Refresh token revoked'})
    revoked = client.post('/api/spotify/control', json={'action': 'pause'})
    assert revoked.status_code == 503
    assert revoked.json() == {'detail': "Spotify a refusé l'accès\xa0: relance make spotify-auth sur le Mac."}

    spotify_http.error = requests.ConnectionError('no route')
    down = client.post('/api/spotify/play', json={'kind': 'liked'})
    assert down.status_code == 502
    assert down.json() == {'detail': 'Impossible de joindre Spotify\xa0: le Mac a-t-il du réseau\xa0?'}

    spotify_http.error = requests.Timeout('slow')
    slow = client.post('/api/spotify/play', json={'query': 'x'})
    assert (slow.status_code, slow.json()['detail']) == (
        502,
        'Spotify ne répond pas (délai dépassé). Réessaie dans un instant.',
    )

    long = client.post('/api/spotify/play', json={'query': 'x' * 201})
    assert (long.status_code, long.json()) == (422, {'detail': 'Recherche\xa0: 200 caractères au plus.'})
