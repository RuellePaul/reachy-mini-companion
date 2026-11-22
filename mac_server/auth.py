"""The API key: one dependency guards every HTTP route and the WebSocket."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Header, HTTPException, WebSocketException, status
from starlette.requests import HTTPConnection

log = logging.getLogger(__name__)
REFUSED = "Clé d'API absente ou invalide."


def key_ok(expected: str, presented: str | None) -> bool:
    """Always true when no key is configured; compared in constant time otherwise."""
    return not expected or hmac.compare_digest((presented or '').encode(), expected.encode())


def require_key(expected: str) -> Callable[..., Awaitable[None]]:
    async def verify(conn: HTTPConnection, x_api_key: Annotated[str | None, Header()] = None) -> None:
        if key_ok(expected, x_api_key):
            return
        log.warning('refused %s %s: missing or wrong X-API-Key', conn.scope['type'], conn.url.path)
        if conn.scope['type'] == 'websocket':
            # Closed before accept: the client sees the handshake fail.
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason=REFUSED)
        raise HTTPException(status_code=401, detail=REFUSED)

    return verify
