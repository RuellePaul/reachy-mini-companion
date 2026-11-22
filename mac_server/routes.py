"""The HTTP routes and the WebSocket (docs/CONTRACTS.md). Routes touching a file, the daemon or Spotify are
plain `def`: Starlette runs them in its threadpool, so nothing blocks the event loop."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import threading
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from mac_server import catalog
from mac_server.auth import require_key
from mac_server.broker import POSE, Broker
from mac_server.contracts import (
    Catalog,
    CommandPoll,
    ControlIn,
    ControlOut,
    DashboardState,
    Heartbeat,
    PlayIn,
    PlayOut,
    RobotEvent,
    SayIn,
    SettingsPatch,
    SettingsView,
    TryMove,
    Voice,
    Volume,
    WsClientMessage,
    WsConversation,
    WsSettings,
    WsVoice,
    WsVolume,
)
from mac_server.prompts import settings_view
from mac_server.spotify import SpotifyError
from mac_server.voices import SAMPLES_DIR, VOICES

if TYPE_CHECKING:
    from mac_server.server import Mac

log = logging.getLogger(__name__)

NOT_IN_CONVERSATION = "Le robot n'est pas en conversation."
NOT_RUNNING = "Le robot n'est pas lancé\xa0: démarre-le avec make run."
ROBOT_OFF = 'Le robot est éteint ou hors réseau\xa0: allume-le, ou vérifie sa connexion.'
HELD_BY_APP = 'Une app se sert de Reachy comme manette\xa0: rends la main pour essayer un mouvement.'
POLL_MAX_S = 25
VOLUME_NOT_TAKEN = (
    "Volume enregistré à {volume}, mais Reachy ne l'a pas encore pris\xa0: le Mac le lui redonnera dès qu'il répondra."
)


def require_live(mac: Mac) -> None:
    """A move or a scene only for a running robot; else 409, saying whether the robot itself or only its agent is off."""
    status = mac.health.status()
    if status != 'live':
        raise HTTPException(409, detail=ROBOT_OFF if status == 'off' else NOT_RUNNING)


def keyed(mac: Mac) -> APIRouter:
    """Every route but the Spotify OAuth pair, behind the API key."""
    router = APIRouter(dependencies=[Depends(require_key(mac.config.api_key))])
    volume_lock = threading.Lock()  # stored, pushed and broadcast in one order: the last PUT wins everywhere

    @router.get('/api/settings')
    def get_settings() -> SettingsView:
        return settings_view(mac.store.settings())

    @router.patch('/api/settings')
    def patch_settings(patch: SettingsPatch) -> SettingsView:
        try:
            view = settings_view(mac.store.update(patch))
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        mac.broker.publish(WsSettings(channel='settings', data=view))
        return view

    @router.get('/api/voices/{name}/sample')
    def voice_sample(name: str) -> FileResponse:
        path = SAMPLES_DIR / f'{name}.wav' if name in VOICES else None  # never a path from an unchecked name
        if path is None or not path.is_file():
            raise HTTPException(404, detail=f"Pas d'échantillon pour la voix «\xa0{name}\xa0».")
        return FileResponse(path, media_type='audio/wav')

    @router.get('/api/state')
    def get_state() -> DashboardState:
        return DashboardState(robot_status=mac.health.status(), session=mac.health.session(), volume=mac.store.volume())

    @router.post('/api/session/wake', status_code=204)
    async def wake() -> None:
        require_live(mac)  # a wake nobody takes says so at once, rather than leave whoever asked to wait for it
        mac.commands.push('wake')

    @router.post('/api/session/sleep', status_code=204)
    async def sleep() -> None:
        mac.commands.push('sleep')

    @router.post('/api/session/restart', status_code=204)
    async def restart() -> None:
        mac.commands.restart(awake=mac.health.session().awake)

    @router.post('/api/session/say', status_code=204)
    async def say(body: SayIn) -> None:
        if not mac.health.takes_say():
            raise HTTPException(409, detail=NOT_IN_CONVERSATION)
        mac.commands.push('say', text=body.text)

    @router.get('/api/catalog')
    def get_catalog() -> Catalog:
        return catalog.build(mac.catalog_extra())

    @router.post('/api/studio/try', status_code=204)
    async def studio_try(move: TryMove) -> None:
        require_live(mac)
        if mac.health.session().manette not in (None, 'voix'):  # held, the robot drops every move but the app's
            raise HTTPException(409, detail=HELD_BY_APP)
        mac.commands.push('try', move=move)

    @router.put('/api/volume')
    def put_volume(body: Volume) -> Volume:
        with volume_lock:
            mac.store.set_volume(body.volume)
            mac.broker.publish(WsVolume(channel='volume', data=body))
            taken = mac.health.daemon_ok and mac.health.apply_volume(force=True)
        if not taken:  # stored all the same: the health probe pushes it again until the daemon takes it
            raise HTTPException(503, detail=VOLUME_NOT_TAKEN.format(volume=body.volume))
        return body

    @router.post('/api/spotify/play')
    def spotify_play(body: PlayIn) -> PlayOut:
        return mac.spotify.play(body.query, body.kind)

    @router.post('/api/spotify/control')
    def spotify_control(body: ControlIn) -> ControlOut:
        return mac.spotify.control(body.action, body.value, body.amount)

    @router.post('/api/robot/events', status_code=204)
    async def robot_event(event: RobotEvent) -> None:
        event = mac.health.record(event)
        mac.broker.publish(WsConversation(channel='conversation', data=event))
        mac.health.publish_changes()
        if event.kind == 'session_end':
            mac.commands.session_ended()
        elif event.kind == 'hush' and event.why in ('antenna', 'say'):  # the robot took its speech back itself
            mac.apps.hush.give_back()

    @router.post('/api/robot/heartbeat', status_code=204)
    async def robot_heartbeat(beat: Heartbeat, request: Request) -> None:
        mac.health.beat(beat, request.client.host if request.client else None)
        if not beat.awake:
            mac.commands.session_ended()

    @router.post('/api/robot/voice', status_code=204)
    async def robot_voice(voice: Voice) -> None:
        mac.broker.publish_voice(WsVoice(channel='voice', data=voice))

    @router.get('/api/robot/commands')
    async def robot_commands(request: Request, wait: Annotated[float, Query(ge=0, le=POLL_MAX_S)] = 20) -> CommandPoll:
        # Dead long-polls take nothing (and no BaseHTTPMiddleware may hide the disconnect).
        return CommandPoll(command=await mac.commands.next(wait, request.is_disconnected))

    @router.websocket('/api/ws')
    async def websocket(ws: WebSocket) -> None:
        queue = mac.broker.subscribe()
        try:
            await ws.accept()
            sender = asyncio.create_task(_send_all(ws, queue, mac.broker))
            try:
                # The dashboard sends only WsClientMessage; reading is also how its departure is noticed.
                while (message := await ws.receive())['type'] != 'websocket.disconnect':
                    try:
                        said = WsClientMessage.model_validate_json(message.get('text') or '')
                    except ValidationError:
                        log.warning('dashboard WebSocket message ignored: %.80r', message.get('text'))
                        continue
                    mac.broker.want(queue, said.subscribe)
                    shown = said.showing
                    mac.broker.show(queue, (shown.since, shown.kind, shown.id) if shown else None)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
        finally:
            mac.broker.unsubscribe(queue)

    return router


async def _send_all(ws: WebSocket, queue: asyncio.Queue[str | None], broker: Broker) -> None:
    while (text := await queue.get()) is not None:
        if text == POSE:
            text = broker.take_pose(queue)
        if text:
            await ws.send_text(text)
    await ws.close(code=1013)  # fell behind: the dashboard reconnects and re-reads its state


def public(mac: Mac) -> APIRouter:
    """The Spotify OAuth pair: no key (a browser opens them), so loopback clients and a one-time state."""
    router = APIRouter(include_in_schema=False)

    @router.get('/api/spotify/login')
    def spotify_login(request: Request) -> Response:
        if not _loopback(request):
            return _refused(request)
        try:
            return RedirectResponse(mac.spotify.authorize_url())
        except SpotifyError as exc:
            return _page("Spotify n'est pas configuré", str(exc), exc.status)

    @router.get('/api/spotify/callback')
    def spotify_callback(request: Request, code: str = '', state: str = '', error: str = '') -> Response:
        if not _loopback(request):
            return _refused(request)
        if not mac.spotify.redeem_state(state):
            return _page('Lien expiré ou déjà utilisé', 'Relance make spotify-auth sur ce Mac.', 400)
        if error or not code:
            return _page("Spotify a refusé l'autorisation", error or 'Aucun code reçu.', 400)
        try:
            mac.spotify.exchange_code(code)
        except SpotifyError as exc:
            return _page("L'autorisation Spotify a échoué", str(exc), exc.status)
        return _page('Spotify est connecté', 'Tu peux fermer cet onglet\xa0: le robot peut lancer la musique.', 200)

    return router


def _loopback(request: Request) -> bool:
    """Uvicorn trusts X-Forwarded-For from 127.0.0.1 only, so a client the Vite proxy forwards keeps its address."""
    try:
        ip = ipaddress.ip_address(request.client.host if request.client else '')
    except ValueError:
        return False
    return (getattr(ip, 'ipv4_mapped', None) or ip).is_loopback


def _refused(request: Request) -> HTMLResponse:
    who = request.client.host if request.client else 'inconnu'
    log.warning('Spotify OAuth refused for non-loopback client %s', who)
    return _page(
        'Autorisation refusée', f"Cette page ne s'ouvre que sur le Mac du robot (demande reçue de {who}).", 403
    )


def _page(title: str, detail: str, status: int) -> HTMLResponse:
    """Everything interpolated is escaped: the query string and Spotify's reply are not ours."""
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f'<body style="font-family:system-ui;max-width:36em;margin:4em auto">'
        f'<h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>',
        status_code=status,
    )
