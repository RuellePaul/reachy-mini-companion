"""Everything that talks to the Mac, over one kept-alive `requests.Session` shared by its threads; and the lent
body's pose, as datagrams (`pose`: poses.py).

`fetch_settings` (each wake) and `request` (the tools) are blocking: call them in a thread. The reporter,
heartbeat and command threads run from boot to shutdown and survive any failure.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Final
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import MaxRetryError

from robot_agent import animation_names, config, poses

logger = logging.getLogger(__name__)

# The events the dashboard's idea of a session hangs on: the bounded queue never drops them.
CRITICAL: Final = frozenset({'session_start', 'session_end'})
_TEXT_SETTINGS: Final = ('voice', *(f'{mode}_animation' for mode in animation_names.MODES))


class MacError(Exception):
    """A Mac call that failed. The message is French, for the model to say as it is."""


class Mac:
    def __init__(
        self, url: str | tuple[str, ...] = config.MAC_AGENT_URLS, api_key: str = config.MAC_AGENT_API_KEY
    ) -> None:
        self._urls = (url,) if isinstance(url, str) else url  # where the Mac may answer, best first
        self._url = self._urls[0] if self._urls else ''
        self._moving = threading.Lock()
        self._http = requests.Session()
        if api_key:
            self._http.headers['X-API-Key'] = api_key
        self._key = api_key
        self._datagrams = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._datagrams.setblocking(False)
        self._to: tuple[str, tuple[str, int] | None] = ('', None)  # the URL the datagrams' address was read from
        self._events: deque[dict[str, Any]] = deque()
        self._cond = threading.Condition()
        self._sending = False
        self._level: float | None = None  # the ear's latest level, not sent yet
        self._voiced = threading.Event()
        self._stop = threading.Event()

    def start(self, on_command: Callable[[dict[str, Any]], None], beat: Callable[[], dict[str, Any]]) -> None:
        """Start the reporter, the ear's sender, the heartbeat (`beat()` is its body) and the command long-poll."""
        for name, target in (
            ('mac-events', self._send_events),
            ('mac-voice', self._send_voice),
            ('mac-heartbeat', lambda: self._heartbeat(beat)),
            ('mac-commands', lambda: self._poll(on_command)),
        ):
            threading.Thread(target=target, name=name, daemon=True).start()

    def close(self, timeout: float = 2.0) -> None:
        """Send what is queued, session_end above all, for at most `timeout`; then stop the threads."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while (self._events or self._sending) and time.monotonic() < deadline:
                self._cond.wait(0.05)
            self._stop.set()
            self._cond.notify_all()

    def fetch_settings(self) -> dict[str, Any]:
        """The SettingsView (contracts.py). Short timeout, no retry: a dead Mac sends the robot back to sleep.
        Every field the robot reads is checked here, once: the rest of the robot indexes them directly."""
        response = self._send('GET', '/api/settings', timeout=config.SETTINGS_TIMEOUT_S)
        response.raise_for_status()
        view = response.json()
        settings = view.get('settings') if isinstance(view, dict) else None
        if not (
            isinstance(settings, dict)
            and all(isinstance(view.get(key), str) for key in ('system_prompt', 'web_prompt'))
            and all(isinstance(settings.get(key), str) for key in _TEXT_SETTINGS)
            and settings['voice']
            and isinstance(settings.get('web_search'), bool)
            and type(settings.get('idle_sleep_minutes')) in (int, float)
            and _tunings(settings.get('tuning'))
        ):
            raise ValueError('the Mac sent malformed settings')
        return view

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """One call for a tool; any failure is a MacError the model can say."""
        try:
            response = self._send(method, path, json=body, timeout=config.MAC_TIMEOUT_S)
        except requests.RequestException as exc:
            raise MacError(_failure(exc))
        if response.status_code >= 400:
            raise MacError(_detail(response))
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise MacError("Le Mac a renvoyé une réponse illisible\xa0: impossible de savoir si c'est fait.")
        return data

    def get_bytes(self, path: str) -> bytes:
        """A file of the Mac's (a scene's audio); any failure is a MacError. Blocking: call it in a thread."""
        try:
            response = self._send('GET', path, timeout=config.MAC_TIMEOUT_S)
        except requests.RequestException:
            raise MacError('Le Mac est injoignable.')
        if response.status_code >= 400:
            raise MacError(_detail(response))
        return response.content

    def emit(self, event: dict[str, Any]) -> None:
        """Queue an event (contracts.RobotEvent). Full, the oldest ordinary event makes room."""
        with self._cond:
            if len(self._events) >= config.EVENT_QUEUE_MAX:
                oldest = next((i for i, e in enumerate(self._events) if e.get('kind') not in CRITICAL), None)
                if oldest is not None:
                    del self._events[oldest]
                elif event.get('kind') not in CRITICAL:
                    return
            self._events.append(event)
            self._cond.notify_all()

    def voice(self, level: float) -> None:
        """The ear's level (contracts.Voice), the latest only: one not sent yet is replaced. From any thread."""
        self._level = level
        self._voiced.set()

    def pose(self, at: float, joints: Any, head: Any, antennas: Any) -> None:
        """One measure of the lent body (poses.pack) as a datagram to the address the Mac answers at, its HTTP port:
        never waited for, never sent again. Nothing is raised: the next one leaves 33 ms later."""
        url = self._url
        if self._to[0] != url:
            parts = urlsplit(url)
            with contextlib.suppress(OSError):  # a name that does not resolve: no datagram until the Mac moves
                self._to = (url, None)
                self._to = (url, (socket.gethostbyname(parts.hostname or ''), parts.port or 80))
        if self._to[1] is not None:
            with contextlib.suppress(OSError, ValueError):
                self._datagrams.sendto(poses.pack(self._key, at, joints, head, antennas), self._to[1])

    def _send_voice(self) -> None:
        while not self._stop.is_set():
            if not self._voiced.wait(1.0):
                continue
            self._voiced.clear()
            level, self._level = self._level, None
            if level is not None:
                with contextlib.suppress(Exception):  # a level lost is replaced 50 ms later
                    self._send('POST', '/api/robot/voice', json={'level': level}, timeout=config.EVENT_TIMEOUT_S)

    def _send_events(self) -> None:
        while True:
            with self._cond:
                self._sending = False
                while not self._events and not self._stop.is_set():
                    self._cond.wait()
                if not self._events:
                    return
                event, self._sending = self._events.popleft(), True
            try:
                response = self._send('POST', '/api/robot/events', json=event, timeout=config.EVENT_TIMEOUT_S)
                if response.status_code >= 400:
                    logger.warning('[MAC] %s event refused: HTTP %d', event.get('kind'), response.status_code)
            except Exception as exc:  # noqa: BLE001 — one unsendable event must not end the transcript
                logger.debug('%s event not sent: %s', event.get('kind'), exc)

    def _heartbeat(self, beat: Callable[[], dict[str, Any]]) -> None:
        """A beat every HEARTBEAT_S from the last one's start, however long that one waited for the Mac."""
        said: str | None = None
        while True:
            started = time.monotonic()
            try:
                status = self._send('POST', '/api/robot/heartbeat', json=beat(), timeout=config.EVENT_TIMEOUT_S)
                status = status.status_code
            except Exception:  # noqa: BLE001 — the pulse outlives any failure
                status = None
            state = 'answering' if status and status < 400 else 'not answering'
            if status in (401, 403):
                state = f'refusing the robot (HTTP {status}): check MAC_AGENT_API_KEY'
            if state != said:
                said = state
                level = logging.INFO if state == 'answering' else logging.WARNING
                logger.log(level, '[MAC] %s at %s', state, self._url or '(MAC_AGENT_URL unset)')
            if self._stop.wait(max(0.0, started + config.HEARTBEAT_S - time.monotonic())):
                return

    def _poll(self, on_command: Callable[[dict[str, Any]], None]) -> None:
        """The robot's only ear for the Mac: nothing may end it but shutdown. One warning per run of failures."""
        failing = False
        while not self._stop.is_set():
            try:
                params = {'wait': config.COMMAND_WAIT_S}
                response = self._send(
                    'GET', '/api/robot/commands', params=params, timeout=(3.0, config.COMMAND_WAIT_S + 10)
                )
                response.raise_for_status()
                command = response.json().get('command')
                failing = False
                if command:
                    on_command(command)
            except Exception as exc:  # noqa: BLE001 — a bad body or a raising callback costs one command
                if not failing:
                    logger.warning('[MAC] command poll failing: %s', exc)
                failing = True
                self._stop.wait(config.COMMAND_RETRY_S)

    def _send(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """One request, never sent twice: the Mac may have handled a POST whose answer was lost (a transcript
        delta would show twice, a track start twice), and the settings fetch at wake gets one try. Only one that
        never reached the Mac goes again, once, to another of its addresses that answers."""
        url = self._url
        try:
            return self._http.request(method, f'{url}{path}', **kwargs)
        except requests.RequestException as exc:
            if not (_unreached(exc) and self._move_from(url)):
                raise
        return self._http.request(method, f'{self._url}{path}', **kwargs)

    def _move_from(self, dead: str) -> bool:
        """Whether another of the Mac's addresses answers now (a cable pulled, a new lease): it becomes the one."""
        with self._moving:
            if self._url != dead:
                return True  # another thread moved already
            for url in self._urls:
                if url != dead and _answers(url):
                    logger.warning('[MAC] %s unreachable: the Mac answers at %s', dead, url)
                    self._url = url
                    return True
            return False


def _tunings(tuning: object) -> bool:
    """contracts.AnimationTuning: an amplitude and a speed per mode, numbers."""
    return isinstance(tuning, dict) and all(
        isinstance(tuning.get(mode), dict)
        and all(type(tuning[mode].get(k)) in (int, float) for k in ('amplitude', 'speed'))
        for mode in animation_names.MODES
    )


def _unreached(exc: Exception) -> bool:
    """Whether a failed call never reached the Mac: it could not be built, or no connection was made (with no retry
    configured, requests wraps every failed connect in a MaxRetryError)."""
    cause = exc.args[0] if exc.args else None
    return isinstance(exc, requests.ConnectTimeout | ValueError) or isinstance(cause, MaxRetryError)


def _answers(url: str) -> bool:
    """Whether something accepts a TCP connection at the URL's host and port. Blocking."""
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname or '', parts.port or 80), timeout=config.MAC_PROBE_TIMEOUT_S):
            return True
    except (OSError, ValueError):
        return False


def _failure(exc: requests.RequestException) -> str:
    """What a failed call may have done: nothing only when it never reached the Mac."""
    if _unreached(exc):
        return "Le Mac est injoignable\xa0: rien n'a été fait."
    if isinstance(exc, requests.Timeout):
        return "Le Mac n'a pas répondu à temps\xa0: impossible de savoir si c'est fait."
    return "Le Mac a coupé la connexion avant de répondre\xa0: impossible de savoir si c'est fait."


def _detail(response: requests.Response) -> str:
    """The Mac's own French `detail`, else a French sentence of ours: whatever it is, the model reads it aloud."""
    try:
        detail = response.json().get('detail')
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return f'Le Mac a refusé la demande (erreur {response.status_code}).'
