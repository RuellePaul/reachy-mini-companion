"""mac_server/app_check.py: what one run of a page in headless Chrome reports, and the run itself when Chrome is here."""

from __future__ import annotations

import base64
import contextlib
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from mac_server import app_check
from mac_server.app_build import CSP


def test_what_the_probe_saw_becomes_french_reasons() -> None:
    probe = {
        'errors': ['Uncaught ReferenceError: score is not defined', 'Uncaught ReferenceError: score is not defined'],
        'rows': [
            ['la pomme rapporte un point', 1, 1, 0],
            ['vitesse', 9.5, 8, 1],
            ['gagné', True, 1, 0],
            ['x', 'a', 1, 0],
        ],
    }
    assert app_check.problems(probe) == [
        'erreur au chargement\xa0: Uncaught ReferenceError: score is not defined',
        'le test «\xa0vitesse\xa0» donne 9.5 au lieu de 8.0',
        'le test «\xa0x\xa0» donne a au lieu de 1',
    ]
    assert app_check.problems({'errors': [], 'selftest': 'absent'}) == ["window.__selfTest n'existe pas"]
    assert app_check.problems({'errors': [], 'selftest': 'boom'}) == ['window.__selfTest a planté\xa0: boom']
    assert app_check.problems({'errors': [], 'rows': []}) == ['window.__selfTest ne rend aucune ligne']
    assert app_check.problems({'errors': [], 'rows': [['ok', 2, 2, 0]]}) == []
    # A false where true was wanted, as the probe sends it (a boolean becomes 0 or 1): nothing to tolerate.
    false = app_check.problems({'errors': [], 'rows': [['gagné', 0, 1, 0]]})
    assert false == ['le test «\xa0gagné\xa0» donne 0.0 au lieu de 1.0']


def test_without_chrome_a_page_is_only_scanned() -> None:
    assert not app_check.available()  # tests/mac/conftest.py hides it
    assert app_check.run('<!DOCTYPE html><html></html>', CSP) == []


GOOD = """<!DOCTYPE html><html lang="fr"><head><title>T</title></head><body><p>ok</p><script>
const ajouter = (a, b) => a + b;
window.__selfTest = () => [['deux et deux', ajouter(2, 2), 4, 0]];
</script></body></html>"""


@pytest.mark.usefixtures('chrome')
def test_a_real_run_finds_what_breaks() -> None:
    assert app_check.run(GOOD, CSP) == []
    assert app_check.run(GOOD.replace('a + b', 'a - b'), CSP) == [
        'le test «\xa0deux et deux\xa0» donne 0.0 au lieu de 4.0'
    ]
    broken = app_check.run(GOOD.replace('const ajouter', 'const ajouter ='), CSP)
    assert broken[0].startswith('erreur au chargement\xa0: Uncaught SyntaxError')
    fetched = app_check.run(GOOD.replace('<p>ok</p>', '<img src="https://example.org/x.png">'), CSP)
    assert fetched == ['erreur au chargement\xa0: bloqué par la CSP\xa0: img-src']  # nothing leaves the page
    standards = GOOD.replace(
        "[['deux et deux', ajouter(2, 2), 4, 0]]", "[['mode', document.compatMode === 'CSS1Compat', 1, 0]]"
    )
    assert app_check.run(standards, CSP) == []  # as in the dashboard's srcdoc, never quirks mode
    silenced = GOOD.replace('<script>', '<script>console.log = () => {};', 1)
    assert app_check.run(silenced, CSP) == []  # a page that silences its own logs still gives its probe


@pytest.mark.usefixtures('chrome')
def test_a_real_run_gives_a_page_no_webgl() -> None:
    # On the Mac as on CI's Linux, whose headless Chrome keeps a software WebGL under --disable-gpu alone: a page
    # that draws in 3D loads without it, and never draws its world with the processor.
    contexts = "['webgl2', 'webgl'].map((kind) => [kind, +!!document.createElement('canvas').getContext(kind), 0, 0])"
    assert app_check.run(GOOD.replace("[['deux et deux', ajouter(2, 2), 4, 0]]", contexts), CSP) == []


@pytest.mark.usefixtures('chrome')
def test_a_page_that_never_finishes_loading_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_check, 'TIMEOUT_S', 3.0)
    assert app_check.run(GOOD.replace('<p>ok</p>', '<script>while (true) {}</script>'), CSP) == [app_check.STUCK]
    assert app_check.run(GOOD, CSP, cancelled=lambda: True) == []  # stopped: no verdict


def _said(text: str) -> str:
    """A console line as Chrome writes it on its stderr."""
    return f'[1:2:1108/120000.000000:INFO:CONSOLE:1] "{text}", source: file:///app.html (1)'


GOOD_PROBE = _said(
    app_check.PROBED + base64.b64encode(json.dumps({'errors': [], 'rows': [['ok', 1, 1, 0]]}).encode()).decode()
)


def _browser(tmp_path: Path, body: str) -> Path:
    """A stand-in for Chrome: a Python script given Chrome's arguments (`args`), its profile at `profile`, and what a
    good page says on its stderr: that it began (`BEGAN`), then its probe (`GOOD`)."""
    browser = tmp_path / 'browser'
    head = (
        'import os, subprocess, sys, time\n'
        'args = sys.argv[1:]\n'
        "profile = next(a.split('=', 1)[1] for a in args if a.startswith('--user-data-dir='))\n"
        f'BEGAN, GOOD = {_said(app_check.STARTED)!r}, {GOOD_PROBE!r}\n'
        'def say(line):\n'
        '    print(line, file=sys.stderr, flush=True)\n'
    )
    browser.write_text(f'#!{sys.executable}\n{head}{body}', encoding='utf-8')
    browser.chmod(0o755)
    return browser


def _running(pid: int) -> bool:
    """Alive: neither gone nor dead and waiting to be reaped."""
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, check=False).stdout
    return state.strip()[:1] not in ('', 'Z')


def _gone(pid: int, within: float = 2.0) -> bool:
    deadline = time.monotonic() + within
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _running(pid)


WRITER = """
def write(where):  # a helper still writing into the profile, whatever happens to it
    os.makedirs(where, exist_ok=True)
    n = 0
    while True:
        try:
            open(os.path.join(where, f'f{n}'), 'w').close()
            n += 1
        except OSError:
            time.sleep(0.001)
"""


def test_a_chrome_slow_to_start_is_no_stuck_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A cold Chrome on a loaded runner takes its time before the page even begins: the page's deadline counts from
    the page's first script (its console line), not from Chrome's start."""
    body = 'time.sleep(1.2)\nsay(BEGAN)\ntime.sleep(0.2)\nsay(GOOD)\n'
    monkeypatch.setattr(app_check, 'CHROME', _browser(tmp_path, body))
    monkeypatch.setattr(app_check, 'TIMEOUT_S', 0.5)
    assert app_check.run(GOOD, CSP) == []


def test_a_chrome_slow_to_end_is_no_stuck_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Once the page's probe is in, what Chrome takes to dump the page and to shut down is not the page's: its verdict
    stands, and Chrome is killed END_S after it at most."""
    monkeypatch.setattr(app_check, 'CHROME', _browser(tmp_path, 'say(BEGAN)\nsay(GOOD)\ntime.sleep(30)\n'))
    monkeypatch.setattr(app_check, 'TIMEOUT_S', 0.5)
    monkeypatch.setattr(app_check, 'END_S', 1.0)
    started, t0 = [], time.monotonic()
    assert app_check.run(GOOD, CSP, started=started.append) == []
    assert time.monotonic() - t0 < 2.5 and _gone(started[0].pid, within=0.5)


def test_a_chrome_that_says_a_lot_still_gives_the_pages_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A page that builds a board and logs as it goes: Chrome's output, far past a pipe's 64 KiB, never blocks it, and
    its probe is found among the rest, even in a line Chrome had not finished writing."""
    body = (
        'say(BEGAN)\nsys.stdout.write("<div></div>" * 20_000)\n'
        "for n in range(3000):\n    say(f'[INFO:CONSOLE:1] case {n}, source: app.html (1)')\n"
        'sys.stderr.write(GOOD)\n'
    )
    monkeypatch.setattr(app_check, 'CHROME', _browser(tmp_path, body))
    monkeypatch.setattr(app_check, 'TIMEOUT_S', 3.0)
    assert app_check.run(GOOD, CSP) == []


@pytest.mark.parametrize('exits', [False, True], ids=['never starts it', 'exits without it'])
def test_a_chrome_that_never_runs_the_page_gives_no_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exits: bool
) -> None:
    """Chrome itself failing (it never reaches the page's first script) says nothing of the page: no verdict, as
    without Chrome, rather than a page « that blocks » for every build."""
    monkeypatch.setattr(app_check, 'CHROME', _browser(tmp_path, 'sys.exit(1)\n' if exits else 'time.sleep(30)\n'))
    monkeypatch.setattr(app_check, 'START_S', 0.5)
    started = []
    assert app_check.run(GOOD, CSP, started=started.append) == []
    assert _gone(started[0].pid, within=0.5)


def test_chrome_and_its_helpers_are_gone_before_its_profile_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stuck page: Chrome is killed with every helper it started (its own process group), so none still writes
    into the profile while it is removed, nor runs on for nobody."""
    pids = tmp_path / 'helper.pid'
    body = (
        f'{WRITER}\nsay(BEGAN)\n'
        'if os.fork() == 0:\n'
        f"    open({str(pids)!r}, 'w').write(str(os.getpid()))\n"
        "    write(os.path.join(profile, 'Default'))\n"
        'time.sleep(60)\n'
    )
    monkeypatch.setattr(app_check, 'CHROME', _browser(tmp_path, body))
    monkeypatch.setattr(app_check, 'TIMEOUT_S', 0.8)
    try:
        assert app_check.run(GOOD, CSP) == [app_check.STUCK]
        assert _gone(int(pids.read_text()), within=0.5)
    finally:
        with contextlib.suppress(ProcessLookupError, ValueError, FileNotFoundError):
            os.kill(int(pids.read_text()), signal.SIGKILL)


def test_a_helper_that_left_chromes_group_never_crashes_the_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chrome's crash handler leaves its process group (its own session, on Linux): one still writing into the
    profile as it is removed costs a leftover temporary directory, never the build (« La construction a planté »)."""
    pids = tmp_path / 'escaped.pid'
    body = (
        f'{WRITER}\nsay(BEGAN)\n'
        'if os.fork() == 0:\n'
        '    os.setsid()\n'
        f"    open({str(pids)!r}, 'w').write(str(os.getpid()))\n"
        "    write(os.path.join(profile, 'Default'))\n"
        'time.sleep(60)\n'
    )
    monkeypatch.setattr(app_check, 'CHROME', _browser(tmp_path, body))
    monkeypatch.setattr(app_check, 'TIMEOUT_S', 0.8)
    try:
        assert app_check.run(GOOD, CSP) == [app_check.STUCK]
    finally:
        with contextlib.suppress(ProcessLookupError, ValueError, FileNotFoundError):
            os.kill(int(pids.read_text()), signal.SIGKILL)


def test_a_group_whose_processes_are_dead_runs_no_more() -> None:
    """What the check waits for once Chrome is killed: no process of its group left running. A dead one the system has
    not reaped yet counts as gone where /proc says so (a container's first process may reap nothing, ever)."""
    child = subprocess.Popen(['sleep', '30'], start_new_session=True)
    assert app_check._running(child.pid)
    os.killpg(child.pid, signal.SIGKILL)
    if Path('/proc').is_dir():  # dead, not reaped yet: no longer running
        deadline = time.monotonic() + 1.0
        while app_check._running(child.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not app_check._running(child.pid)
    child.wait()
    assert not app_check._running(child.pid)


def test_chrome_runs_the_page_fenced_off_the_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Chrome starts fenced off the network, behind the page's CSP (which does not stop WebRTC)."""
    seen = tmp_path / 'argv.json'
    body = f'import json\nopen({str(seen)!r}, "w").write(json.dumps(args))\nsay(BEGAN)\nsay(GOOD)\n'
    monkeypatch.setattr(app_check, 'CHROME', _browser(tmp_path, body))
    assert app_check.run(GOOD, CSP) == []
    args = json.loads(seen.read_text())
    assert {
        '--proxy-server=127.0.0.1:9',  # every request to a port nobody listens on
        '--proxy-bypass-list=<-loopback>',  # loopback included
        '--host-resolver-rules=MAP * ~NOTFOUND',
        '--disable-quic',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',  # WebRTC's UDP would go around the proxy
    } <= set(args)


@pytest.mark.usefixtures('chrome')
def test_a_real_run_reaches_nothing_where_the_csp_would_let_it(monkeypatch: pytest.MonkeyPatch) -> None:
    hits: list[str] = []

    class Listener(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Listener)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    at = f'127.0.0.1:{server.server_address[1]}'
    reach = (
        f"fetch('http://{at}/f').catch(() => {{}}); new WebSocket('ws://{at}/w'); new Image().src = 'http://{at}/i';"
    )
    page, open_csp = GOOD.replace('<p>ok</p>', f'<script>{reach}</script>'), "default-src * 'unsafe-inline'"
    end, patience = app_check._end, [3.0]

    def heard_out(chrome: Any) -> None:  # Chrome sends the image and the WebSocket last, just before its own exit,
        deadline = time.monotonic() + patience[0]  # seconds later under load: it lives until all three are heard
        while len(set(hits)) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        end(chrome)

    monkeypatch.setattr(app_check, '_end', heard_out)
    try:
        assert app_check.run(page, open_csp) == []
        assert hits == []  # the fence alone stops all three, given 3 s more to leak
        unfenced = [f for f in app_check.FENCE if not f.startswith(('--proxy-server', '--host-resolver-rules'))]
        monkeypatch.setattr(app_check, 'FENCE', unfenced)
        patience[0] = 30.0  # what gets through is waited for, however loaded the machine
        assert app_check.run(page, open_csp) == []
        assert sorted(hits) == ['/f', '/i', '/w']  # without its proxy and its resolver, the page gets through
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.usefixtures('chrome')
def test_a_real_page_with_a_large_dom_runs() -> None:
    cell = "Object.assign(document.createElement('div'), {className: 'case vide'})"
    board = f"<div id='g'></div><script>for (let i = 0; i < 3000; i++) g.append({cell});</script>"
    assert len(board) < 200
    assert app_check.run(GOOD.replace('<p>ok</p>', board), CSP) == []  # its DOM outgrows a pipe's 64 KiB
    assert app_check.run(GOOD.replace('<p>ok</p>', f'<p>{"x" * 70_000}</p>'), CSP) == []
