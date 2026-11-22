"""Run a built page once, before anyone sees it: headless Chrome, network fenced, the page's CSP in force. What
breaks comes back in French: errors while it loads, and the `window.__selfTest` rows that fail (the prompt asks
every page for its own tests, which nothing ran until now). Blocking (a few seconds): call it from a build thread.

The page speaks on Chrome's stderr (`--enable-logging`): a first word as its first script runs, then its probe, read
as soon as written. Chrome's own start and its exit are not the page's: its deadline runs from its first word to its
probe. Then Chrome ends as it always did (END_S at most), and is killed with every helper it started."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
START_S = 60.0  # Chrome's own start, to the page's first script: a cold Chrome on a loaded runner took over 15 s
TIMEOUT_S = 15.0  # from there to its probe: a page runs in under a second; one still loading by then is stuck
END_S = 15.0  # once the probe is in, Chrome ends on its own as it always did (it dumps the page), killed past this
REAP_S = 2.0  # Chrome's helpers, killed with it, are gone within this (once the system has reaped them)
POLL_S = 0.05
STARTED, PROBED = '__app_check:start', '__app_check:probe '  # what the page says: it began; what it found (base64)
PROBLEMS_MAX = 6
# No network at all: every request goes to a proxy nobody listens on, loopback included, and no name resolves
# (CDP alone misses WebSocket and WebRTC). The CSP forbids them anyway.
FENCE = [
    '--proxy-server=127.0.0.1:9', '--proxy-bypass-list=<-loopback>', '--host-resolver-rules=MAP * ~NOTFOUND',
    '--disable-quic', '--disable-background-networking', '--no-first-run', '--no-default-browser-check',
    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',  # WebRTC's UDP goes around the proxy otherwise
]  # fmt: skip
# No GPU, and no WebGL drawn by the processor in its place: under --disable-gpu alone Linux's headless Chrome keeps
# one (SwiftShader), and a page's 3D world drawn that way takes seconds a run.
NO_GPU = ['--disable-gpu', '--disable-3d-apis']
STUCK = 'la page ne se charge pas\xa0: elle bloque ou plante avant la fin de son chargement'
# The probe speaks through its own console.log, kept where the page cannot reach it (one may silence its own logs).
_START = (
    "<script>Object.defineProperty(window, '__appCheckSay', {value: console.log.bind(console)});"
    + f"__appCheckSay('{STARTED}');"
    + """window.__probe = {errors: []};
addEventListener('error', (e) => __probe.errors.push(String(e.message || e)));
addEventListener('unhandledrejection', (e) => __probe.errors.push('promesse rejetée\xa0: ' + String(e.reason)));
addEventListener('securitypolicyviolation', (e) => __probe.errors.push('bloqué par la CSP\xa0: ' + e.violatedDirective));
</script>"""
)
_END = (
    """<script>setTimeout(() => {
  const probe = window.__probe;
  try {
    if (typeof window.__selfTest !== 'function') probe.selftest = 'absent';
    else probe.rows = window.__selfTest().map((row) => Array.isArray(row) ? row.map((v) => typeof v === 'boolean' ? +v : v) : [String(row)]);
  } catch (e) { probe.selftest = String(e && e.message || e); }
  probe.errors = [...new Set(probe.errors)].slice(0, 20).map((error) => error.slice(0, 300));
  if (probe.rows) probe.rows = probe.rows.slice(0, 200);
  let bytes = '';
  for (const byte of new TextEncoder().encode(JSON.stringify(probe))) bytes += String.fromCharCode(byte);
  __appCheckSay('"""
    + PROBED
    + """' + btoa(bytes));
}, 1500);</script>"""
)


def available() -> bool:
    return CHROME.is_file()


def run(
    page: str,
    csp: str,
    cancelled: Callable[[], bool] = lambda: False,
    started: Callable[[subprocess.Popen[bytes]], None] = lambda _chrome: None,
    kit: str = '',
) -> list[str]:
    """Why the page fails once run (French), or nothing. Nothing either when it cannot be run (no Chrome, or a Chrome
    that never reaches the page) or when `cancelled()` turns true meanwhile (Chrome is then stopped). A page that never
    finishes loading is a failure. `started` gets Chrome as it starts, for whoever cancels to `stop` it at once. `kit`:
    what the page is served with before its own scripts (app_build.kit), run here as there."""
    if not available():
        log.warning('no Chrome at %s: pages are scanned, not run', CHROME)
        return []
    # The doctype first, as in the dashboard's srcdoc: never quirks mode.
    probed = f'<!DOCTYPE html><meta http-equiv="Content-Security-Policy" content="{csp}">{_START}{kit}{page}{_END}'
    work = Path(tempfile.mkdtemp(prefix='app-check-'))
    try:
        path = work / 'app.html'
        path.write_text(probed, encoding='utf-8')
        args = [str(CHROME), '--headless=new', *NO_GPU, f'--user-data-dir={work}/profile', *FENCE]
        args += ['--window-size=1100,850', '--enable-logging=stderr', '--virtual-time-budget=5000', '--dump-dom']
        console = _Console(work / 'chrome.log')  # a file, not a pipe: a full pipe would block Chrome
        try:
            with console.path.open('wb') as err:
                chrome = subprocess.Popen(
                    [*args, path.as_uri()], stdout=subprocess.DEVNULL, stderr=err, start_new_session=True
                )
        except OSError as exc:
            log.warning('the page check could not start Chrome: %s', exc)
            return []
        started(chrome)
        try:
            return _verdict(chrome, console, cancelled)
        finally:
            _end(chrome)
    finally:
        _remove(work)


def stop(chrome: subprocess.Popen[bytes]) -> None:
    """Kill Chrome and every helper it started, from any thread: they share its own process group (killed alone, its
    helpers run on, writing into its profile)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(chrome.pid, signal.SIGKILL)


class _Console:
    """What the page says on Chrome's stderr, read as it comes: whether it began, then its probe."""

    def __init__(self, path: Path) -> None:
        self.path, self.began, self.probe = path, False, None
        self._read, self._rest = 0, b''

    def read(self, last: bool = False) -> None:
        """The lines written since the last read; `last`: Chrome has ended, an unfinished line is whole."""
        with self.path.open('rb') as said:
            said.seek(self._read)
            new = said.read()
        self._read += len(new)
        lines = (self._rest + new).split(b'\n')
        self._rest = b'' if last else lines.pop()
        for line in lines:
            self.began = self.began or STARTED.encode() in line
            found = re.search(re.escape(PROBED.encode()) + rb'([A-Za-z0-9+/=]+)', line)
            if found is not None and self.probe is None:
                with contextlib.suppress(binascii.Error, ValueError):
                    self.probe = json.loads(base64.b64decode(found.group(1)))


def _verdict(chrome: subprocess.Popen[bytes], console: _Console, cancelled: Callable[[], bool]) -> list[str]:
    """What the page says, as soon as it says it: its probe, stuck (no probe TIMEOUT_S after its first word, or Chrome
    gone after it), or nothing when cancelled or when Chrome never reaches the page (START_S, or gone before)."""
    launched, began = time.monotonic(), None
    while True:
        ended = chrome.poll() is not None
        console.read(last=ended)
        if console.probe is not None:  # the verdict; the page runs on as it always did, until Chrome ends
            end = time.monotonic() + END_S
            while chrome.poll() is None and time.monotonic() < end and not cancelled():
                time.sleep(POLL_S)
            return problems(console.probe)
        now = time.monotonic()
        if began is None and console.began:
            began = now
        if ended and began is not None:  # it began, then Chrome ended before its probe: it crashed
            return [STUCK]
        if ended or (began is None and now - launched > START_S):
            log.warning('Chrome never reached the page (ended: %s): it is scanned, not run', chrome.returncode)
            return []
        if cancelled():
            return []
        if began is not None and now - began > TIMEOUT_S:
            return [STUCK]
        time.sleep(POLL_S)


def _end(chrome: subprocess.Popen[bytes]) -> None:
    """Chrome and its helpers killed, and waited for until none is left (REAP_S at most): nothing of theirs writes
    into the profile as it is removed."""
    stop(chrome)
    chrome.wait()
    deadline = time.monotonic() + REAP_S
    while _running(chrome.pid):
        if time.monotonic() > deadline:
            log.warning("Chrome's helpers outlived it by %.0f s", REAP_S)
            return
        time.sleep(POLL_S)


def _running(group: int) -> bool:
    """Whether a process of the group still runs. On Linux, /proc tells a dead one waiting to be reaped (forever, in a
    container whose first process reaps nothing) from a live one; elsewhere the system reaps them at once."""
    proc = Path('/proc')
    if not proc.is_dir():
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True
    for stat in proc.glob('[0-9]*/stat'):
        with contextlib.suppress(OSError, IndexError, ValueError):
            state, _parent, pgrp = stat.read_text().rsplit(')', 1)[1].split()[:3]
            if int(pgrp) == group and state != 'Z':
                return True
    return False


def _remove(work: Path) -> None:
    """The run's directory removed. A writer outside Chrome's group (its crash handler, in a session of its own) may
    keep some of it: a leftover temporary directory, never an error that would end the build."""
    for attempt in range(3):
        try:
            shutil.rmtree(work)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1 * (attempt + 1))
    shutil.rmtree(work, ignore_errors=True)
    log.warning('the page check could not remove all of %s', work)


def problems(probe: dict) -> list[str]:
    """The French reasons in what the probe saw: load errors, then the self-test."""
    found = [f'erreur au chargement\xa0: {error[:160]}' for error in dict.fromkeys(probe.get('errors') or [])]
    if probe.get('selftest') == 'absent':
        found.append("window.__selfTest n'existe pas")
    elif probe.get('selftest'):
        found.append(f'window.__selfTest a planté\xa0: {str(probe["selftest"])[:160]}')
    rows = probe.get('rows')
    if rows is not None and not rows:
        found.append('window.__selfTest ne rend aucune ligne')
    for row in rows or []:
        label, *values = [*row, None, None, None][:4]
        try:
            got, want, tolerance = (float(v) for v in values)
            ok = abs(got - want) <= tolerance
        except (TypeError, ValueError):
            got, want, ok = values[0], values[1], False
        if not ok:
            found.append(f'le test «\xa0{str(label)[:80]}\xa0» donne {got} au lieu de {want}')
    return found[:PROBLEMS_MAX]
