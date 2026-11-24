"""One call to the Gemini REST API (scene voices, app code), with `requests`: blocking, so call it from a plain
`def` route or a thread. Every failure is a GeminiError with a French message and what a caller needs to decide
whether to retry, wait or switch model."""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

API = 'https://generativelanguage.googleapis.com/v1beta'


class GeminiError(Exception):
    def __init__(self, message: str, status: int = 502, *, retry_after_s: float | None = None, per_day: bool = False):
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s  # a per-minute 429: wait this long
        self.per_day = per_day  # a per-day 429: this model is spent until tomorrow


def _retry_delay(details: list[Any]) -> float | None:
    for detail in details:
        delay = detail.get('retryDelay') if isinstance(detail, dict) else None
        if isinstance(delay, str) and delay.endswith('s'):
            try:
                return float(delay[:-1])
            except ValueError:
                return None
    return None


def post(api_key: str, path: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """POST `body` to `{API}/{path}`; the decoded JSON answer."""
    if not api_key:
        raise GeminiError('GEMINI_API_KEY manque dans le .env du Mac.', 503)
    try:
        response = requests.post(f'{API}/{path}', json=body, headers={'x-goog-api-key': api_key}, timeout=timeout_s)
    except requests.Timeout as exc:
        raise GeminiError("Gemini n'a pas répondu à temps.", 504) from exc
    except requests.RequestException as exc:
        raise GeminiError('Impossible de joindre Gemini\xa0: le Mac a-t-il du réseau\xa0?') from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    data = data if isinstance(data, dict) else {}  # an answer with nothing in it: the caller says what is missing
    if response.status_code < 400:
        return data
    error = data['error'] if isinstance(data.get('error'), dict) else {}
    text = str(error.get('message') or response.text[:200])
    log.warning('Gemini %s refused (%d): %s', path, response.status_code, text)
    if response.status_code == 429:
        details = error.get('details') if isinstance(error.get('details'), list) else []
        per_day = 'perday' in str(details).lower().replace('_', '') or 'per day' in text.lower()
        return_after = _retry_delay(details)
        raise GeminiError(
            'Le quota Gemini est atteint pour le moment.', 429, retry_after_s=return_after, per_day=per_day
        )
    raise GeminiError(f'Gemini a refusé la demande (erreur {response.status_code}).', response.status_code)
