"""The FastAPI app: one `Mac` object holds the server's state; every error the Mac raises itself is French JSON."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from mac_server import apps, routes, scenes, studio
from mac_server.auth import require_key
from mac_server.broker import Broker
from mac_server.commands import CommandQueue
from mac_server.config import Config, load_config
from mac_server.contracts import CatalogEntry
from mac_server.health import Health
from mac_server.spotify import Spotify, SpotifyError
from mac_server.store import Store, Unreadable

log = logging.getLogger(__name__)

# Field names as the dashboard labels them, for validation messages.
_FIELDS = {
    'name': 'nom',
    'voice': 'voix',
    'personality': 'personnalité',
    'idle_animation': 'animation de repos',
    'listening_animation': "animation d'écoute",
    'talking_animation': 'animation de parole',
    'web_search': 'recherche web',
    'gaze_tracking': 'regard actif',
    'idle_sleep_minutes': 'mise en veille',
    'tuning': 'réglage des animations',
    'text': 'texte',
    'query': 'recherche',
    'wait': 'attente',
    'amount': 'nombre',
}


@dataclass
class Mac:
    config: Config
    store: Store
    broker: Broker
    commands: CommandQueue
    health: Health
    spotify: Spotify
    # The subjects, each an isolated module with its own routes (built once the Mac exists: they hold it).
    studio: studio.Service = field(init=False)
    scenes: scenes.Service = field(init=False)
    apps: apps.Service = field(init=False)

    def catalog_extra(self) -> list[CatalogEntry]:
        """The catalogue entries of the subjects that keep their own stores."""
        return [*self.scenes.catalog(), *self.apps.catalog()]

    @classmethod
    def from_config(cls, config: Config) -> Mac:
        store, broker = Store(config.settings_path), Broker()
        health = Health(config.reachy_host, config.reachy_port, broker.publish, store.volume)
        mac = cls(config, store, broker, CommandQueue(), health, Spotify(config))
        mac.studio, mac.scenes, mac.apps = studio.Service(mac), scenes.Service(mac), apps.Service(mac)
        return mac


def create_app(config: Config | None = None) -> FastAPI:
    mac = Mac.from_config(config or load_config())

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not mac.config.api_key:
            log.warning(
                'MAC_AGENT_API_KEY is empty: every route and the WebSocket are open to the local network. '
                'Set it in the repo-root .env (the robot and the dashboard read it too).'
            )
        tasks = [
            asyncio.create_task(job)
            for job in (mac.health.run(), mac.studio.run(), mac.scenes.run(), mac.apps.run(), mac.apps.hush.run())
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # No /docs nor /openapi.json: they would be the only routes without the key.
    app = FastAPI(title='Reachy Mac server', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mac = mac
    app.add_middleware(
        CORSMiddleware,  # pure ASGI: a BaseHTTPMiddleware would hide a long-poll's disconnect
        allow_origins=list(mac.config.cors_origins),
        allow_methods=['GET', 'POST', 'PUT', 'PATCH'],
        allow_headers=['Content-Type', 'X-API-Key'],
    )
    app.add_exception_handler(SpotifyError, _spotify_error)
    app.add_exception_handler(Unreadable, _unreadable)
    app.add_exception_handler(RequestValidationError, _invalid)
    app.add_exception_handler(Exception, _crash)
    app.include_router(routes.keyed(mac))
    for subject in (studio, scenes, apps):
        app.include_router(subject.routes(mac), dependencies=[Depends(require_key(mac.config.api_key))])
    app.include_router(routes.public(mac))
    return app


def french_errors(errors: Sequence[Any]) -> str:
    """Pydantic's errors as one French sentence, for the dashboard and for the robot to say."""
    parts = []
    for error in errors:
        loc = [str(part) for part in error.get('loc', ())]
        if loc and loc[0] in ('body', 'query', 'path', 'header'):  # where it was, not what it was
            loc = loc[1:]
        field = _FIELDS.get(loc[-1], loc[-1]) if loc else 'requête'
        kind, ctx = error.get('type'), error.get('ctx') or {}
        if kind in ('unknown_value', 'empty_name', 'empty_query'):  # contracts.py words these itself
            parts.append(str(error.get('msg')))
        elif kind == 'json_invalid':  # its loc ends on where the parse stopped: a character's index, no field
            parts.append("requête illisible\xa0: ce n'est pas du JSON")
        elif kind == 'string_too_long':
            parts.append(f'{field}\xa0: {_number(ctx.get("max_length"))} caractères au plus')
        elif kind == 'string_too_short':
            parts.append(f'{field}\xa0: ne peut pas être vide')
        elif kind == 'less_than_equal':
            parts.append(f'{field}\xa0: au plus {_number(ctx.get("le"))}')
        elif kind == 'greater_than_equal':
            parts.append(f'{field}\xa0: au moins {_number(ctx.get("ge"))}')
        elif kind == 'missing':
            parts.append(f'{field}\xa0: manquant')
        elif kind == 'extra_forbidden':
            parts.append(f'{field}\xa0: champ inconnu')
        else:
            parts.append(f'{field}\xa0: valeur invalide')
    text = '\xa0; '.join(parts) or 'requête invalide'
    return f'{text[0].upper()}{text[1:]}.'


def _number(value: object) -> str:
    """A bound as the dashboard writes a figure (format.ts): 20, not 20.0 (a float field's bounds are floats); 1,5,
    not 1.5; 40 000, its thousands apart (a narrow no-break space); a true minus."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    text = f'{value:,.0f}' if float(value).is_integer() else f'{value:,}'
    return text.replace(',', '\u202f').replace('.', ',').replace('-', '\u2212')


async def _spotify_error(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({'detail': str(exc)}, status_code=getattr(exc, 'status', 502))


async def _unreadable(_: Request, exc: Exception) -> JSONResponse:
    detail = (
        f"Le fichier des réglages ({exc}) est illisible\xa0: rien n'a été enregistré. "
        'Corrige-le ou déplace-le, puis réessaie.'
    )
    return JSONResponse({'detail': detail}, status_code=500)


async def _invalid(request: Request, exc: Exception) -> JSONResponse:
    detail = french_errors(exc.errors() if isinstance(exc, RequestValidationError) else [])
    log.warning('refused %s %s: %s', request.method, request.url.path, detail)
    return JSONResponse({'detail': detail}, status_code=422)


async def _crash(_: Request, exc: Exception) -> JSONResponse:
    # Starlette re-raises after answering, so uvicorn still logs the traceback.
    return JSONResponse({'detail': 'Le Mac a rencontré une erreur interne\xa0: regarde ses logs.'}, status_code=500)
