"""A stand-in robot for a TEST Mac server, to try the dashboard and its apps end to end without the robot: it beats,
takes the Mac's commands and answers as the agent does, and serves its daemon's state stream.

    # a Mac server and a dashboard of their own (never the owner's: its port, its state)
    MAC_AGENT_PORT=7797 REACHY_STATE_DIR=/tmp/reachy-bench SETTINGS_PATH=/tmp/reachy-bench/settings.json \\
        REACHY_CACHE_DIR=/tmp/reachy-bench/cache REACHY_HOST=127.0.0.1 REACHY_PORT=8797 .venv/bin/python -m mac_server
    (cd frontend && VITE_MAC_AGENT_URL=http://localhost:7797 npx vite --port 5087 --strictPort)
    .venv/bin/python scripts/fake_robot.py --port 7797 --daemon 8797 [--asleep] [--voice 0.7:2.4:5]

- Asleep (`--asleep`) until the Mac's `wake`, awake 2 s later; `sleep` puts it back to sleep.
- A manette granted is answered `on` 0.6 s later (and at each renewal), one let go `off` 0.8 s later.
- The state stream (ws://127.0.0.1:DAEMON/api/state/ws/full, 30 messages a second): while a body is held, a hand
  carries the head from shoulder to shoulder (±20 mm, a sway every 2 s) looking ahead, nods it (±6°) and pushes the
  right antenna once a second; else the robot is still, in the sleep pose. Held under the palm (« paume »), the head
  is upright on a base a hand turns (±12°, a sway every 4 s), the antennas lowered as in sleep, the right one pushed
  further down once a second (past the half turn: told from its other side).
- An app's `replay` is answered `started` once the body is back (a 1 s lead-in) then `ended`; its moves are printed.
- `--voice LEVEL:ON:PERIOD`: while a profile listens, the level posted is LEVEL for ON seconds of every PERIOD.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from websockets.sync.server import ServerConnection, serve

ROOT = Path(__file__).resolve().parent.parent
SESSION = 'fake-robot-1'
SLEEP_HEAD = [
    [0.911, 0.004, 0.413, -0.021],
    [-0.004, 1.0, -0.001, 0.001],
    [-0.413, -0.001, 0.911, -0.044],
    [0, 0, 0, 1.0],
]
NEUTRAL = (-0.1745, 0.1745)  # the antennas at rest, [right, left]
LOWERED = (-3.05, 3.05)  # where they lie in sleep, and under the palm (« paume »)


def env(name: str, default: str = '') -> str:
    """A variable of the repo's .env, unquoted."""
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        key, _, value = line.partition('=')
        if key.strip() == name:
            return value.strip().strip('"\'')
    return default


class FakeRobot:
    def __init__(self, port: int, asleep: bool, voice: tuple[float, float, float]) -> None:
        self._base, self._key = f'http://127.0.0.1:{port}', env('MAC_AGENT_API_KEY')
        self._voice = voice
        self.awake = not asleep
        self.profile: str | None = None  # the manette held
        self._since = 0.0

    def say(self, text: str) -> None:
        print(f'{time.strftime("%H:%M:%S")} {text}', flush=True)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {'X-API-Key': self._key, 'Content-Type': 'application/json'}
        request = urllib.request.Request(self._base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            self.say(f'{method} {path}: HTTP {exc.code} {exc.read().decode()[:160]}')
        except OSError as exc:
            self.say(f'{method} {path}: {exc}')
        return {}

    def event(self, **fields: object) -> None:
        self.call('POST', '/api/robot/events', {'session_id': SESSION, **fields})

    def beat(self) -> None:
        self.call('POST', '/api/robot/heartbeat', {'awake': self.awake, 'session_id': SESSION if self.awake else None})

    def lends_body(self) -> bool:
        return self.profile is not None and self.profile != 'voix'

    # ----- its threads

    def beats(self) -> None:
        while True:
            self.beat()
            time.sleep(2.0)

    def voices(self) -> None:
        level, on, period = self._voice
        while True:
            if self.profile is not None and self.profile.endswith('voix'):
                heard = level if (time.monotonic() - self._since) % period < on else 0.0
                self.call('POST', '/api/robot/voice', {'level': round(heard, 3)}, timeout=2.0)
            time.sleep(0.05)

    def state(self) -> dict[str, Any]:
        """One message of the daemon's state stream: still, in a hand while a body is held, or under a palm."""
        m = [row[:] for row in SLEEP_HEAD]
        antennas, base = list(NEUTRAL), 0.0
        t = time.monotonic() - self._since
        if self.lends_body() and (self.profile or '').startswith('paume'):
            base = math.radians(12.0) * math.sin(2 * math.pi * t / 4)
            c, s = math.cos(base), math.sin(base)
            m = [[c, -s, 0.0, 0.0], [s, c, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
            pushed = LOWERED[0] - (0.14 if t % 1.0 < 0.25 else 0.0)
            antennas = [math.remainder(pushed, 2 * math.pi), LOWERED[1]]
        elif self.lends_body():
            m[1][3] += 0.02 * math.sin(math.pi * t)
            c, s = (
                math.cos(math.radians(6.0 * math.sin(2 * math.pi * t / 3))),
                math.sin(math.radians(6.0 * math.sin(2 * math.pi * t / 3))),
            )
            top, bottom = m[0][:3], m[2][:3]
            m[0][:3] = [c * a + s * b for a, b in zip(top, bottom, strict=True)]
            m[2][:3] = [c * b - s * a for a, b in zip(top, bottom, strict=True)]
            antennas[0] += 0.14 if t % 1.0 < 0.25 else 0.0
        stamp = datetime.datetime.now(datetime.UTC).isoformat()
        head = {'m': [value for row in m for value in row]}
        joints = [base, *([0.0] * 6)]
        return {'timestamp': stamp, 'head_joints': joints, 'head_pose': head, 'antennas_position': antennas}

    def stream(self, socket: ServerConnection) -> None:
        self.say("the Mac reads the daemon's state stream")
        try:
            while True:
                socket.send(json.dumps(self.state()))
                time.sleep(1 / 30)
        except Exception:  # noqa: BLE001 — the Mac hung up
            self.say('the Mac stopped reading the state stream')

    def replay(self, motion: dict[str, Any]) -> None:
        loops = int(motion.get('loops') or 0)
        seconds = (float(motion.get('end') or 0) - float(motion.get('start') or 0)) / 1000
        if loops <= 0:
            return
        while self.lends_body():
            time.sleep(0.1)
        if not 0.3 <= seconds <= 30:
            self.event(kind='replay', state='ended')
            self.say(f'replay: nothing to play ({seconds:.2f} s asked)')
            return
        time.sleep(1.0)
        self.event(kind='replay', state='started', seconds=round(seconds, 3), loops=loops)
        self.say(f'replay started: {seconds:.2f} s x {loops}')
        time.sleep(loops * seconds + (loops - 1) * 0.4)
        self.event(kind='replay', state='ended')
        self.say('replay ended')

    def wake(self) -> None:
        time.sleep(2.0)
        self.awake = True
        self.beat()
        self.event(kind='session_start')
        self.event(kind='phase', phase='idle')
        self.say('awake')

    # ----- the Mac's commands

    def take(self, command: dict[str, Any]) -> None:
        action = command.get('action')
        if action == 'wake' and not self.awake:
            self.say('wake asked')
            threading.Thread(target=self.wake, daemon=True).start()
        elif action == 'sleep' and self.awake:
            self.awake, self.profile = False, None
            self.event(kind='session_end', reason='dashboard')
            self.beat()
            self.say('asleep again')
        elif action == 'manette' and self.awake:
            self.manette(command.get('manette'), bool(command.get('grant')))
        elif action == 'app_motion':
            motion = command.get('motion') or {}
            self.say(f'move {json.dumps(motion)}')
            if motion.get('kind') == 'replay':
                threading.Thread(target=self.replay, args=(motion,), daemon=True).start()
        else:
            self.say(f'command {action} ignored')

    def manette(self, profile: str | None, grant: bool) -> None:
        if profile == 'off':
            if self.profile is not None:
                was = self.profile
                time.sleep(0.8)
                self.profile = None
                self.event(kind='manette', state='off', profile=None, why='app')
                self.say(f'manette off ({was})')
        elif grant or self.profile == profile:
            if self.profile != profile:
                time.sleep(0.6)
                self.profile, self._since = profile, time.monotonic()
                self.say(f'manette on: {profile}')
            self.event(kind='manette', state='on', profile=profile)

    def run(self, daemon: int) -> None:
        if daemon:
            threading.Thread(target=serve(self.stream, '127.0.0.1', daemon).serve_forever, daemon=True).start()
        self.beat()
        if self.awake:
            self.event(kind='session_start')
            self.event(kind='phase', phase='idle')
        threading.Thread(target=self.beats, daemon=True).start()
        threading.Thread(target=self.voices, daemon=True).start()
        self.say('fake robot ' + ('awake' if self.awake else 'asleep'))
        while True:
            command = self.call('GET', '/api/robot/commands?wait=10', timeout=25.0).get('command')
            if command:
                self.take(command)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n\n', 1)[0])
    parser.add_argument('--port', type=int, required=True, help="the TEST Mac server's port")
    parser.add_argument(
        '--daemon', type=int, default=0, help='where to serve the state stream (REACHY_PORT of that server)'
    )
    parser.add_argument('--asleep', action='store_true')
    parser.add_argument('--voice', default='0:0:1', help='LEVEL:ON:PERIOD')
    args = parser.parse_args()
    if args.port == int(env('MAC_AGENT_PORT', '7777') or 7777):
        print("that is the owner's Mac server: a fake robot is for a test server of its own", file=sys.stderr)
        return 1
    level, on, period = (float(part) for part in args.voice.split(':'))
    FakeRobot(args.port, args.asleep, (level, on, max(period, 0.1))).run(args.daemon)
    return 0


if __name__ == '__main__':
    sys.exit(main())
