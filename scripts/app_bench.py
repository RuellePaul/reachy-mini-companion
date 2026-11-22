"""An app's page on a bench, without the robot nor the dashboard: served as the Mac serves it (its CSP, then the
toolbox), in `<iframe sandbox="allow-scripts" srcdoc>`, with a stand-in dashboard that plays the robot's part (awake
or asleep, the manette held, a stream of pads, a replay answered), in one headless Chrome driven in real time.

    .venv/bin/python scripts/app_bench.py PAGE.html --check
    .venv/bin/python scripts/app_bench.py PAGE.html --shots 800,4000,9000 --out /tmp/page.png [--size 360x520]
    .venv/bin/python scripts/app_bench.py PAGE.html --log --ms 9000 [--inner play.js] [--outer hand.js]

--check  what a shipped page must pass (tests/mac/test_shipped_apps.py): the builder's scan, one run in Chrome
         with its own `__selfTest`, its French signs. Nothing else is done.
--shots  screenshots at these ms after the load (OUT_800.png, OUT_4000.png…; one alone is OUT itself), then the
         page's uncaught errors, or « no uncaught error ».
--log    no screenshot: after --ms, what the page posted to the dashboard (ms since the load, the message).
--inner  JavaScript added inside the frame after the page, as the page's own: click « Jouer », press keys.
--gpu    Chrome draws with the machine's GPU, as the owner's browser does: a page in WebGL shows what it draws
         (without it, Chrome has no GPU, as in the builder's check: such a page must still load and pass its tests).
--outer  JavaScript run in the stand-in dashboard before the frame loads. It may set
           CFG = {awake: true, grant: true, delay: 500, wake: 2500, back: 800, lead: 1200}
             the manette is held `delay` ms after it is asked (the robot gets into place), with no question;
             awake false: the robot sleeps and is woken by the ask (`wake` ms, then {type: 'robot', awake: true});
             grant false: it never comes: 'off' after the delay;
             back: ms from the page letting go to 'off'; lead: ms from a `replay` asked to its 'started';
           padAt = (t, profile) => ({body, head: {yaw, pitch, roll, x, y, z}, left, right, antennas, voice})
             the pad `t` seconds after the manette went on; what it leaves out is at rest; null: no pad at all
             (the robot's link stalls for seconds).

Real time, and not Chrome's virtual time: a sandboxed frame is another process, which that clock never reaches.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

from websockets.sync.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mac_server import app_build, app_check

BROKEN = re.compile(r'« | »| [:;!?](?=\s|$|[»"\')])')  # a French sign cut from its word (tests/mac/test_french.py)
ERRORS = """<script>
addEventListener('error', (e) => parent.postMessage({reachy: 1, type: '__error', message: String(e.message || e) + ' @' + e.lineno}, '*'));
addEventListener('unhandledrejection', (e) => parent.postMessage({reachy: 1, type: '__error', message: 'rejet: ' + String(e.reason)}, '*'));
addEventListener('securitypolicyviolation', (e) => parent.postMessage({reachy: 1, type: '__error', message: 'CSP: ' + e.violatedDirective}, '*'));
</script>"""
DASHBOARD = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#16181B;overflow:hidden}
iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:#121417}
#log{display:none}</style></head><body><iframe id="f" sandbox="allow-scripts" srcdoc="__SRCDOC__"></iframe><pre id="log"></pre>
<script>
const f = document.getElementById('f'), out = document.getElementById('log'), t0 = performance.now();
const post = (m) => f.contentWindow.postMessage({reachy: 1, ...m}, '*');
let CFG = {awake: true, grant: true, delay: 500, wake: 2500, back: 800, lead: 1200};
let padAt = () => ({});
let held = null, since = 0, pads = null, awake = true, playing = null, granting = null;
__OUTER__
const rest = () => ({left: false, right: false, antennas: {left: 0, right: 0}, body: 0,
  head: {yaw: 0, pitch: 0, roll: 0, x: 0, y: 0, z: 0}, voice: 0, t: Date.now()});
const stop = () => { clearInterval(pads); pads = null; held = null; };
// A replay: once the body is back, 'started' after the lead-in, 'ended' after its loops (0.4 s between two).
const replay = (d) => {
  clearTimeout(playing);
  const ended = () => post({type: 'replay', state: 'ended', seconds: null, loops: null});
  if (!d.loops) return ended();
  const seconds = (d.end - d.start) / 1000;
  const start = () => {
    if (held) return (playing = setTimeout(start, 100));
    if (!(seconds >= 0.3)) return ended();
    playing = setTimeout(() => {
      post({type: 'replay', state: 'started', seconds, loops: d.loops});
      playing = setTimeout(ended, 1000 * (d.loops * seconds + (d.loops - 1) * 0.4));
    }, CFG.lead ?? 1200);
  };
  start();
};
addEventListener('message', (e) => {
  if (e.source !== f.contentWindow) return;
  const d = e.data;
  if (!d || d.reachy !== 1) return;
  out.textContent += Math.round(performance.now() - t0) + ' ' + JSON.stringify(d) + '\\n';
  if (d.type === 'replay') return replay(d);
  if (d.type !== 'manette') return;
  if (d.profile === null) {
    const was = held;
    clearTimeout(granting);  // let go while the robot was getting into place: no hold comes
    stop();
    return setTimeout(() => post({type: 'manette', state: 'off', profile: null}), was ? (CFG.back ?? 800) : 0);
  }
  if (held === d.profile) return post({type: 'manette', state: 'on', profile: held});
  post({type: 'manette', state: 'asking', profile: d.profile});
  const asleep = !awake;
  if (asleep && CFG.grant) setTimeout(() => { awake = true; post({type: 'robot', awake: true}); }, CFG.wake ?? 2500);
  clearTimeout(granting);
  granting = setTimeout(() => {
    if (!CFG.grant) return post({type: 'manette', state: 'off', profile: null});
    held = d.profile;
    since = performance.now();
    post({type: 'manette', state: 'on', profile: held});
    clearInterval(pads);
    pads = setInterval(() => {
      const said = padAt((performance.now() - since) / 1000, held), p = said || {}, n = rest();
      if (said !== null) post({type: 'pad', ...n, ...p, antennas: {...n.antennas, ...(p.antennas || {})}, head: {...n.head, ...(p.head || {})}});
    }, 33);
  }, (asleep ? (CFG.wake ?? 2500) : 0) + CFG.delay);
});
f.addEventListener('load', () => { awake = CFG.awake; post({type: 'robot', awake}); });
</script></body></html>"""


class _Words(HTMLParser):
    """What a page says before any script runs: its text, outside its style and its scripts."""

    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []
        self._code = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._code += tag in ('script', 'style')

    def handle_endtag(self, tag: str) -> None:
        self._code -= tag in ('script', 'style')

    def handle_data(self, data: str) -> None:
        if not self._code and data.strip():
            self.said.append(data.strip())


def check(page: str) -> int:
    """What a shipped page must pass, one line per check; 1 if any fails."""
    words = _Words()
    words.feed(page)
    checks = {
        'the scan (app_build.check)': app_build.check(page),
        'the whole page, nothing around it': []
        if app_build.extract(page) == page.strip()
        else ['text around the page'],
        'a title': [] if app_build.title_of(page) else ['no <title>'],
        'its own tests': [] if 'window.__selfTest' in page else ['no window.__selfTest'],
        'the toolbox, no channel of its own': ['it posts to the dashboard itself'] if 'postMessage' in page else [],
        'French signs with their words': [text for text in words.said if BROKEN.search(text)],
        'one run in Chrome (load errors, __selfTest rows)': app_check.run(page, app_build.CSP, kit=app_build.kit()),
    }
    for name, problems in checks.items():
        print(('FAIL  ' if problems else 'ok    ') + name)
        for problem in problems:
            print('        ' + str(problem)[:300])
    quiet = 'silent while it is on screen' if app_build.quiet_of(page) else 'the robot talks over it'
    print(f'{app_build.title_of(page)!r}, {len(page.encode())} bytes, {quiet}')
    return 1 if any(checks.values()) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n\n', 1)[0])
    parser.add_argument('page')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--out', default='bench.png')
    parser.add_argument('--size', default='1100x850')
    parser.add_argument('--ms', type=int, default=3000)
    parser.add_argument('--shots')
    parser.add_argument('--dpr', type=float, default=1.0)
    parser.add_argument('--gpu', action='store_true')
    parser.add_argument('--inner')
    parser.add_argument('--outer')
    parser.add_argument('--log', action='store_true')
    args = parser.parse_args()
    page = Path(args.page).read_text(encoding='utf-8')
    if args.check:
        return check(page)
    inner = f'<script>{Path(args.inner).read_text(encoding="utf-8")}</script>' if args.inner else ''
    outer = Path(args.outer).read_text(encoding='utf-8') if args.outer else ''
    served = app_build.served(page)
    head = served[: served.index('>') + 1]  # its CSP, first as the Mac serves it; then the bench's ear for its errors
    srcdoc = f'{head}{ERRORS}{served[len(head) :]}{inner}'
    dashboard = DASHBOARD.replace('__SRCDOC__', html.escape(srcdoc, quote=True)).replace('__OUTER__', outer)
    width, height = (int(n) for n in args.size.split('x'))
    shots = [int(ms) for ms in str(args.shots or args.ms).split(',')]
    with tempfile.TemporaryDirectory(prefix='app-bench-') as work:
        path = Path(work) / 'bench.html'
        path.write_text(dashboard, encoding='utf-8')
        command = [
            str(app_check.CHROME), '--headless=new', *([] if args.gpu else app_check.NO_GPU), '--hide-scrollbars',
            '--no-first-run', '--no-default-browser-check', f'--user-data-dir={work}/profile',
            '--remote-debugging-port=0', '--autoplay-policy=no-user-gesture-required', 'about:blank',
        ]  # fmt: skip
        chrome = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            address = None
            for line in chrome.stderr or ():  # "DevTools listening on ws://127.0.0.1:PORT/devtools/browser/ID"
                if 'DevTools listening on ' in line:
                    address = line.split('DevTools listening on ', 1)[1].strip()
                    break
            if address is None:
                print('Chrome gave no DevTools address', file=sys.stderr)
                return 1
            with connect(address, max_size=None) as ws:
                sent = 0

                def call(method: str, params: dict | None = None, session: str | None = None) -> dict:
                    nonlocal sent
                    sent += 1
                    message = {'id': sent, 'method': method, 'params': params or {}}
                    if session:
                        message['sessionId'] = session
                    ws.send(json.dumps(message))
                    while True:
                        answer = json.loads(ws.recv())
                        if answer.get('id') == sent:
                            if 'error' in answer:
                                raise RuntimeError(f'{method}: {answer["error"]}')
                            return answer.get('result', {})

                target = call('Target.createTarget', {'url': 'about:blank'})['targetId']
                session = call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
                metrics = {'width': width, 'height': height, 'deviceScaleFactor': args.dpr, 'mobile': False}
                call('Emulation.setDeviceMetricsOverride', metrics, session)
                call('Page.enable', session=session)
                call('Page.navigate', {'url': path.as_uri()}, session)
                began = time.monotonic()
                for ms in shots:
                    time.sleep(max(0.0, began + ms / 1000 - time.monotonic()))
                    if not args.log:
                        shot = call('Page.captureScreenshot', {'format': 'png'}, session)['data']
                        out = Path(args.out)
                        name = out if len(shots) == 1 else out.with_name(f'{out.stem}_{ms}{out.suffix}')
                        name.write_bytes(base64.b64decode(shot))
                        print(name)
                read = {'expression': "document.getElementById('log').textContent", 'returnByValue': True}
                said = call('Runtime.evaluate', read, session)['result'].get('value', '')
                errors = [line for line in said.splitlines() if '__error' in line]
                print(said if args.log else '\n'.join(errors) if errors else 'no uncaught error')
        finally:
            chrome.kill()
            chrome.wait()
    return 0


if __name__ == '__main__':
    sys.exit(main())
