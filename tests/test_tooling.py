"""The repo's own tooling, offline: what the make targets send to the robot (ssh and rsync faked on PATH), how CI
groups its runs, pins its actions and names its runners, and what `make doctor` says of the .venv."""

from __future__ import annotations

import contextlib
import json
import operator
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / '.github' / 'workflows').glob('*.yml'))
# CI's install, without its matrix's Python: `uv sync --locked --extra dev`.
CI_SYNC = re.search(r'(uv sync [^$\n]*?) --python', (ROOT / '.github' / 'workflows' / 'ci.yml').read_text())[1]
FAKE_SSH = """#!/bin/sh
echo "ssh $*" >> "$CALLS"
case "$*" in
    *pkill*) exit 0 ;;
    *pgrep*) [ "$AGENT_RUNNING" = 1 ] && echo 4242 && exit 0; exit 1 ;;
esac
exit 0
"""


def make_run(tmp_path: Path, agent_running: bool) -> list[str]:
    """The calls `make run` makes, in order; an unresolvable robot in case a real ssh were reached."""
    fakes = tmp_path / 'bin'
    fakes.mkdir()
    for name, body in (('ssh', FAKE_SSH), ('rsync', '#!/bin/sh\necho "rsync $*" >> "$CALLS"\n')):
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    calls = tmp_path / 'calls'
    env = {
        **os.environ,
        'PATH': f'{fakes}{os.pathsep}{os.environ["PATH"]}',
        'CALLS': str(calls),
        'AGENT_RUNNING': '1' if agent_running else '0',
    }
    command = ['make', '-s', 'run', 'ROBOT=pollen@robot.invalid']
    done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return calls.read_text().splitlines()


def agent_start(calls: list[str]) -> int:
    started = [i for i, call in enumerate(calls) if call.startswith('ssh -tt') and '-m robot_agent' in call]
    assert len(started) == 1, calls
    return started[0]


@pytest.mark.parametrize('agent_running', [False, True])
def test_make_run_stops_an_agent_already_running_before_it_starts_its_own(tmp_path: Path, agent_running: bool) -> None:
    calls = make_run(tmp_path, agent_running)
    start = agent_start(calls)
    before = calls[:start]
    assert any('pgrep -f' in call and '-m robot_agent' in call for call in before), calls
    stopped = [call for call in before if 'pkill -TERM' in call]
    assert len(stopped) == (1 if agent_running else 0), calls


def test_make_up_stops_a_leftover_agent_as_make_run_does() -> None:
    loop = (ROOT / 'scripts' / 'up.sh').read_text().split('robot_loop() {')[1]
    assert 'stop_leftover_agent' in loop.split('-m robot_agent')[0]  # before the agent starts
    assert 'bash scripts/stop_agent.sh' in (ROOT / 'scripts' / 'up.sh').read_text()


ROBOT_HERE_SSH = """#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac; done
shift # the robot is this machine: its daemon ready, the agent's own commands run here, nothing else
case "$*" in
    *daemon/status*) echo '{"state":"running","ready":true}' ;;
    *robot_agent*) exec bash -c "$*" ;;
esac
"""
# The agent's way: any signal ends it, after its sleep pose, with exit 0. ENDING makes it end otherwise: stopped while
# it boots, before it handles any signal (the default action, 143), killed past stop_agent's bound (137), or failing.
FAKE_AGENT = """import os, signal, sys, time
print(f'agent {os.getpid()} ({os.environ["TAG"]}) started', flush=True)
ending = os.environ.get('ENDING')
if ending == 'failing':
    sys.exit(1)
if ending == 'booting':
    time.sleep(60)
for sig in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, lambda *_: os.kill(os.getpid(), signal.SIGKILL) if ending == 'killed' else sys.exit(0))
while True:
    time.sleep(0.1)
"""


def wait_for(condition: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.1)


def own_python() -> str:
    """This interpreter's binary that keeps, as its command line, the path it is started by: a macOS framework build's
    bin/python (python.org, Homebrew) starts its Python.app in its own place, which a pgrep on that path never finds."""
    framework = sysconfig.get_config_var('PYTHONFRAMEWORK')
    if not framework:
        return os.path.realpath(sys.executable)
    app = Path(sys.base_prefix) / 'Resources' / 'Python.app' / 'Contents' / 'MacOS' / framework
    if not app.is_file():
        pytest.skip(f'{sys.executable} restarts itself as {app}, which is missing: no path of its own to find it by')
    return os.path.realpath(app)


def test_a_python_started_by_a_path_of_its_own_is_found_by_that_path(tmp_path: Path) -> None:
    """What the agents' pgrep pattern (scripts/stop_agent.sh) needs of the fake robot's python below."""
    python = tmp_path / 'python'
    python.symlink_to(own_python())
    probe = subprocess.Popen([python, '-c', 'import time; time.sleep(30)'])

    def found() -> list[str]:
        pgrep = ['pgrep', '-f', f'^{python} -c']
        return subprocess.run(pgrep, capture_output=True, text=True, check=False).stdout.split()

    try:
        wait_for(lambda: bool(found()), timeout=5)
        ps = ['ps', '-o', 'command=', '-p', str(probe.pid)]
        command = subprocess.run(ps, capture_output=True, text=True, check=False).stdout
        assert found() == [str(probe.pid)], f'started as {python}, it runs as {command}'
    finally:
        probe.kill()
        probe.wait()


@contextlib.contextmanager
def robot_here(tmp_path: Path) -> Iterator[tuple[Path, Callable[[str, str], subprocess.Popen[bytes]]]]:
    """This machine as the robot, FAKE_AGENT its agent: its directory, and `start(tag, ending)`, which starts make up's
    robot loop ('up') or `make run` ('run') there, its output in <tag>.out. All of them and their agents are killed at
    the end."""
    fakes, robot = tmp_path / 'bin', tmp_path / 'robot'
    fakes.mkdir()
    for name, body in (('ssh', ROBOT_HERE_SSH), ('rsync', '#!/bin/sh\n')):
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    (robot / 'robot_agent').mkdir(parents=True)
    (robot / 'robot_agent' / '__init__.py').write_text('')
    (robot / 'robot_agent' / '__main__.py').write_text(FAKE_AGENT)
    python = tmp_path / 'python'  # a path of its own: the agents' pgrep pattern matches this test's alone
    python.symlink_to(own_python())
    where = {'ROBOT': 'pollen@robot.invalid', 'ROBOT_DIR': str(robot), 'ROBOT_PY': str(python)}
    where['ROBOT_LOG'] = str(robot / 'agent.log')
    env = {**os.environ, **where, 'PATH': f'{fakes}{os.pathsep}{os.environ["PATH"]}'}
    up_sh = ROOT / 'scripts' / 'up.sh'
    # up.sh up to the Mac server's start (its settings and functions), then its robot loop alone; $0 for its cd.
    mac_server = '\n( "$VENV/bin/python" -m mac_server'
    assert mac_server in up_sh.read_text()  # never the whole script: it would start the Mac server
    commands = {
        'up': ['bash', '-c', up_sh.read_text().split(mac_server)[0] + '\nrobot_loop\n', str(up_sh)],
        'run': ['make', '-s', 'run', *(f'{key}={value}' for key, value in where.items())],
    }
    started: list[subprocess.Popen[bytes]] = []

    def start(tag: str, ending: str) -> subprocess.Popen[bytes]:
        with (tmp_path / f'{tag}.out').open('wb') as out:
            tagged = {**env, 'TAG': tag, 'ENDING': ending}
            started.append(
                subprocess.Popen(commands[tag], cwd=ROOT, env=tagged, stdout=out, stderr=out, start_new_session=True)
            )
        return started[-1]

    try:
        yield robot, start
    finally:
        for process in started:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
        subprocess.run(['pkill', '-KILL', '-f', f'^{python} -u -m robot_agent'], check=False)


@pytest.mark.parametrize('ending', ['signalled', 'booting', 'killed'])
def test_make_run_while_make_up_runs_keeps_the_robot(tmp_path: Path, ending: str) -> None:
    """The last deploy wins: `make run` stops make up's agent and starts its own, and make up's loop leaves the robot
    to it, instead of stopping that agent in turn to start its own again. However make up's agent ended: on its
    signal handler, still booting, or killed."""
    up_out, run_out = tmp_path / 'up.out', tmp_path / 'run.out'
    with robot_here(tmp_path) as (_, start):
        up = start('up', ending)
        wait_for(lambda: '(up) started' in up_out.read_text(), timeout=20)
        run = start('run', '')
        wait_for(lambda: up.poll() is not None or run.poll() is not None, timeout=30)
        time.sleep(1)
        outputs = f'--- make up\n{up_out.read_text()}--- make run\n{run_out.read_text()}'
        assert run.poll() is None, f'make run ended ({run.returncode}): its agent was stopped\n{outputs}'
        assert up.poll() == 1, outputs  # make up's robot loop gave the robot up; its dashboard would stay
        assert 'the robot is left to it' in up_out.read_text(), outputs
        pattern = f'^{tmp_path / "python"} -u -m robot_agent'
        agents = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True, check=False).stdout.split()
        assert len(agents) == 1, outputs
        assert f'agent {agents[0]} (run) started' in run_out.read_text(), outputs


def test_make_up_starts_a_failed_agent_again_whatever_an_earlier_stop_left_on_the_robot(tmp_path: Path) -> None:
    """The mark a deploy's stop leaves on the robot names the agent running then, not the next one: make up's agent
    failing on its own is waited for and started again, never taken for one another deploy stopped."""
    up_out = tmp_path / 'up.out'
    with robot_here(tmp_path) as (robot, start):
        (robot / 'agent.stopped').touch()
        up = start('up', 'failing')
        wait_for(lambda: up_out.read_text().count('(up) started') == 2 or up.poll() is not None, timeout=20)
        assert up_out.read_text().count('(up) started') == 2, up_out.read_text()
        assert 'the agent stopped (exit 1)' in up_out.read_text()


def test_make_up_finds_the_mac_at_any_of_its_addresses(tmp_path: Path) -> None:
    """make up warns that every wake will fail only when the robot reaches none of the Mac's addresses: the Mac's
    own now (scripts/mac_hosts.sh), then MAC_AGENT_URL's, as the agent tries them. A stale fixed IP alone is no
    reason when the Mac answers at another of its addresses."""
    live = socket.socket()
    live.bind(('127.0.0.1', 0))
    live.listen(8)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        dead = probe.getsockname()[1]
    port = live.getsockname()[1]
    up_sh = (ROOT / 'scripts' / 'up.sh').read_text()
    check = re.search(r'^check_mac_link\(\) \{.*?^\}', up_sh, re.S | re.M)
    assert check is not None
    (tmp_path / 'scripts').mkdir()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    if shutil.which('timeout') is None:  # the robot's coreutils, missing on a Mac
        (bin_dir / 'timeout').write_text('#!/bin/bash\nshift\nexec "$@"\n')
        (bin_dir / 'timeout').chmod(0o755)

    def check_with(url: str, hosts: str) -> str:
        (tmp_path / 'scripts' / 'mac_hosts.sh').write_text(f'echo {hosts}\n')
        script = (
            'say() { echo "SAY $*"; }\n'
            f'env_value() {{ [ "$1" = MAC_AGENT_URL ] && echo "{url}"; }}\n'
            'robot_here() { shift; bash -c "$*"; }\nSSH=(robot_here)\nROBOT=robot\n'
            f'{check.group(0)}\ncheck_mac_link\n'
        )
        env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
        return subprocess.run(
            ['bash', '-c', script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=True
        ).stdout

    try:
        assert check_with(f'http://192.0.2.1:{port}', '127.0.0.1') == ''  # the fixed IP is stale, the Mac's own answers
        assert check_with(f'http://127.0.0.1:{dead}, http://127.0.0.1:{port}', '') == ''  # MAC_AGENT_URL's second
        said = check_with(f'http://127.0.0.1:{dead}', '127.0.0.1')
        assert 'reaches the Mac at none of its addresses' in said and f'127.0.0.1:{dead}' in said
    finally:
        live.close()


def test_the_macs_addresses_are_handed_to_the_robot_wired_first(tmp_path: Path) -> None:
    """scripts/mac_hosts.sh: the address of the interface the Mac's own traffic leaves by comes first (docked, the
    wired one: the agent tries them in this order, and the Wi-Fi of a docked Mac dozes), then the others as ifconfig
    lists them; never loopback nor link-local. With no default route, ifconfig's order."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()

    def stub(name: str, body: str) -> None:
        (bin_dir / name).write_text(f'#!/bin/bash\n{body}\n')
        (bin_dir / name).chmod(0o755)

    def hosts() -> str:
        env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
        script = ROOT / 'scripts' / 'mac_hosts.sh'
        return subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, check=True).stdout

    stub(
        'ifconfig',
        "printf 'lo0: flags\\n\\tinet 127.0.0.1 netmask 0xff000000\\nen0: flags\\n\\tinet 192.168.1.31 netmask "
        '0xffffff00\\nen6: flags\\n\\tinet6 fe80::1%%en6 prefixlen 64\\n\\tinet 192.168.1.30 netmask 0xffffff00\\n'
        "en7: flags\\n\\tinet 169.254.3.4 netmask 0xffff0000\\n'",
    )
    stub('route', "printf '   route to: default\\n  interface: en6\\n'")
    stub('ipconfig', '[ "$1 $2" = "getifaddr en6" ] && echo 192.168.1.30')
    assert hosts() == '192.168.1.30 192.168.1.31\n'
    stub('route', 'exit 1')  # no default route: no interface to prefer
    assert hosts() == '192.168.1.31 192.168.1.30\n'


def test_the_robots_wifi_lock_takes_itself_back_when_the_gateway_is_lost(tmp_path: Path) -> None:
    """scripts/robot_wifi.sh, the robot's tools stood in for: `lock` ties the connection to the access point it is on
    and turns the power save off; a lock after which the gateway no longer answers is undone by the robot itself (it
    could not be reached to be told); `unlock` puts both back."""
    bin_dir, said = tmp_path / 'bin', tmp_path / 'nmcli.said'
    bin_dir.mkdir()
    stubs = {
        'ssh': 'exec bash -c "${@: -1}"',  # the robot is here
        'sudo': 'shift; exec "$@"',
        'sleep': '[ "$1" = 2 ] && exec /bin/sleep 0.3; exec /bin/sleep 0.02',  # this side's wait; the robot's
        'nohup': 'exec "$@"',
        'ip': 'echo "default via 10.0.0.1 proto dhcp"',
        'ping': '[ -e "$GATEWAY" ]',
        'wpa_cli': 'echo FAIL',
        'iw': 'case "$*" in *link) printf "Connected to aa:bb:cc:dd:ee:ff (on wlan0)\\n\\tfreq: 2462.0\\n\\tsignal: -70 dBm\\n" ;; '
        '*) echo "Power save: on" ;; esac',
        'nmcli': 'case "$*" in *GENERAL.CONNECTION*) echo Box ;; *"connection show"*) echo "" ;; '
        '*) echo "$*" >> "$SAID" ;; esac',
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(f'#!/bin/bash\n{body}\n')
        (bin_dir / name).chmod(0o755)
    gateway = tmp_path / 'gateway'
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'ROBOT_WIFI_TMP': str(tmp_path)}
    env |= {'SAID': str(said), 'GATEWAY': str(gateway)}

    def wifi(action: str) -> tuple[int, str, list[str]]:
        said.write_text('')
        script = ROOT / 'scripts' / 'robot_wifi.sh'
        command = ['bash', str(script), action, 'robot']
        run = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, check=False)
        return run.returncode, run.stdout, said.read_text().splitlines()

    lock = 'connection modify Box 802-11-wireless.bssid AA:BB:CC:DD:EE:FF 802-11-wireless.powersave 2'
    unlock = 'connection modify Box 802-11-wireless.bssid  802-11-wireless.powersave 0'
    gateway.touch()
    code, out, changes = wifi('lock')
    assert (code, changes) == (0, [lock, 'connection up Box']) and 'locked to AA:BB:CC:DD:EE:FF' in out
    gateway.unlink()  # the lock loses the gateway: taken back, and said
    code, out, changes = wifi('lock')
    assert code == 1 and changes == [lock, 'connection up Box', unlock, 'connection up Box'] and 'taken back' in out
    gateway.touch()
    code, out, changes = wifi('unlock')
    assert (code, changes) == (0, [unlock, 'connection up Box']) and 'unlocked' in out
    assert wifi('status')[2] == []  # reads, changes nothing


def test_make_up_reads_the_env_file_as_the_mac_does(tmp_path: Path) -> None:
    """The values make up checks and waits on (MAC_AGENT_URL, MAC_AGENT_PORT) are the ones the Mac's python-dotenv
    reads (and the robot's boot.load_env, tests/robot/test_main.py): whatever the quotes, the comment after them,
    the spaces around `=`."""
    env = tmp_path / '.env'
    env.write_text(
        'MAC_AGENT_URL="http://192.168.1.20:7777"  # the Mac, fixed IP\nMAC_AGENT_PORT = 7777\n'
        "T_SINGLE='k3y'  # shared\nT_BARE=plain # a comment\nT_INSIDE=\"it's\"\n",
        encoding='utf-8',
    )
    (tmp_path / 'scripts').mkdir()  # up.sh goes to its $0's parent's parent: here, where this .env is
    up_sh = (ROOT / 'scripts' / 'up.sh').read_text().split('\n( "$VENV/bin/python" -m mac_server')[0]
    expected = dotenv_values(env)
    script = up_sh + ''.join(f'\nprintf "%s\\n" "$(env_value {name})"' for name in expected)
    done = subprocess.run(
        ['bash', '-c', script, str(tmp_path / 'scripts' / 'up.sh')],
        env={**os.environ, 'VENV': str(ROOT / '.venv')},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert dict(zip(expected, done.stdout.splitlines(), strict=True)) == expected


DAEMON_ERROR = (
    '{"state":"error","backend_status":null,'
    '"error":"could not open port /dev/ttyAMA3: [Errno 13] Permission denied: \'/dev/ttyAMA3\'"}'
)


@pytest.mark.parametrize(
    ('out', 'code', 'status', 'words'),
    [
        ('{"state":"running","backend_status":{"ready":true},"error":null}', 0, 0, ''),
        (DAEMON_ERROR, 0, 1, 'the daemon is not ready yet (state: error)'),
        ('pollen@reachy-mini.local: Permission denied (publickey,password).', 255, 2, 'Permission denied (publickey'),
        ('Host key verification failed.', 255, 2, 'Host key verification failed.'),
        ('ssh: connect to host reachy-mini.local port 22: Operation timed out', 255, 1, 'no SSH answer'),
    ],
)
def test_make_up_takes_only_ssh_itself_for_a_refusal(
    tmp_path: Path, out: str, code: int, status: int, words: str
) -> None:
    """The robot is given up at once only when ssh itself refuses this Mac (its exit 255): a daemon whose status says
    « Permission denied » (a device it cannot open) is a daemon not ready, waited for as any other state."""
    fakes = tmp_path / 'bin'
    fakes.mkdir()
    # ssh's own failures go to its stderr with exit 255; the remote command's output to its stdout.
    (fakes / 'ssh').write_text(
        '#!/bin/sh\nif [ "$CODE" = 255 ]; then echo "$OUT" >&2; else printf %s "$OUT"; fi\nexit "$CODE"\n'
    )
    (fakes / 'ssh').chmod(0o755)
    up_sh = (ROOT / 'scripts' / 'up.sh').read_text().split('\n( "$VENV/bin/python" -m mac_server')[0]
    script = up_sh + '\nwhy="$(robot_status)"; echo "status $? $why"; wait_for_robot; echo "waited $?"\n'
    env = {**os.environ, 'PATH': f'{fakes}{os.pathsep}{os.environ["PATH"]}', 'OUT': out, 'CODE': str(code)}
    env |= {'ROBOT': 'pollen@robot.invalid', 'ROBOT_WAIT_S': '0'}
    done = subprocess.run(
        ['bash', '-c', script, str(ROOT / 'scripts' / 'up.sh')],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    lines = done.stdout.splitlines()
    assert lines[0].startswith(f'status {status}'), done.stdout
    assert words in lines[0], done.stdout
    assert ('SSH refuses this Mac' in done.stdout) == (status == 2), done.stdout
    assert lines[-1] == f'waited {0 if status == 0 else 1}', done.stdout


def evaluate(expression: str, github: dict[str, str]) -> str | bool:
    """One ${{ }} of the workflow: github.* values, quoted strings, ==, !=, && and || (no parentheses)."""

    def operand(text: str) -> str:
        text = text.strip()
        return text[1:-1] if text.startswith("'") else github[text.removeprefix('github.')]

    def comparison(text: str) -> str | bool:
        for sign, compare in (('!=', operator.ne), ('==', operator.eq)):
            if sign in text:
                left, right = text.split(sign)
                return compare(operand(left), operand(right))
        return operand(text)

    value: str | bool = False
    for alternative in expression.split('||'):
        value = True
        for part in alternative.split('&&'):
            value = value and comparison(part)
        if value:
            return value
    return value


def ci_run(key: str, event_name: str, ref: str, run_id: str) -> str:
    """A key of ci.yml's concurrency block, as GitHub fills it in for one run."""
    workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text()
    block = re.search(r'^concurrency:\n((?:[ \t]+.*\n)+)', workflow, re.MULTILINE)
    assert block is not None
    template = dict(re.findall(r'^\s+([\w-]+):\s*(.+)$', block[1], re.MULTILINE))[key]
    github = {'workflow': 'CI', 'event_name': event_name, 'ref': ref, 'sha': f'sha-{run_id}', 'run_id': run_id}

    def filled(match: re.Match[str]) -> str:
        value = evaluate(match[1], github)
        return str(value).lower() if isinstance(value, bool) else value

    return re.sub(r'\$\{\{(.*?)\}\}', filled, template)


def test_ci_cancels_a_superseded_pull_request_run_never_a_push() -> None:
    """Runs of one group wait on each other, a newer pending one replaces the older, and cancel-in-progress cancels the
    running one: every push to main gets its own group, or a quick series of merges leaves commits with no result."""
    assert ci_run('group', 'push', 'refs/heads/main', '1') != ci_run('group', 'push', 'refs/heads/main', '2')
    pull = 'refs/pull/7/merge'
    assert ci_run('group', 'pull_request', pull, '3') == ci_run('group', 'pull_request', pull, '4')
    assert ci_run('cancel-in-progress', 'pull_request', pull, '4') == 'true'


def ci_jobs() -> dict[str, tuple[str, list[dict[str, str]]]]:
    """ci.yml's jobs: each one's text, and its steps in order, each as the keys written on the step's own lines."""
    jobs = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text().split('\njobs:\n')[1]
    found = {}
    for job, body in re.findall(r'^ {4}([\w-]+):\n((?:(?: {8}.*)?\n)*)', jobs, re.MULTILINE):
        listed = re.split(r'^ {12}- ', body.split('\n        steps:\n')[1], flags=re.MULTILINE)[1:]
        found[job] = (body, [dict(re.findall(r'^(?: {14})?([\w-]+): (.*)$', step, re.MULTILINE)) for step in listed])
    return found


def test_ci_reports_every_check_whichever_failed_before_it() -> None:
    """With no status function in its `if:`, a step runs only when every step before it succeeded, and a matrix
    cancels its other jobs once one fails: a format slip would hide the tests' results. After a successful install,
    every check runs and each Python reports; a failed install or a cancelled run stops them."""
    jobs = ci_jobs()
    assert set(jobs) == {'python', 'frontend'}
    for job, (body, steps) in jobs.items():
        if 'matrix:' in body:
            assert re.search(r'^ +fail-fast: false\b', body, re.MULTILINE), job
        installs = [i for i, step in enumerate(steps) if step.get('id') == 'install']
        assert len(installs) == 1, f'{job}: one step with `id: install`'
        checks = steps[installs[0] + 1 :]
        assert len(checks) >= 4, job
        for check in checks:
            condition = re.sub(r" && matrix\.python == '[\d.]+'", '', check.get('if', ''))  # its leg, if one
            assert condition == "${{ !cancelled() && steps.install.conclusion == 'success' }}", (job, check)


def test_only_the_tests_run_in_every_python_leg() -> None:
    """ruff (its target is pyproject.toml's floor) and the size budget read no interpreter: run in every leg, one slip
    showed as one failed check per leg. The tests run in every leg, each other check in one."""
    _, steps = ci_jobs()['python']
    [install] = [i for i, step in enumerate(steps) if step.get('id') == 'install']
    legs = ci_pythons()
    for check in steps[install + 1 :]:
        named = re.findall(r"matrix\.python == '([\d.]+)'", check['if'])
        runs_in = [leg for leg in legs if not named or leg in named]
        if check['run'] == '.venv/bin/pytest':
            assert runs_in == legs, check
        else:
            assert len(runs_in) == 1, (check, runs_in)


def test_every_pull_request_is_checked_whatever_its_base_and_again_once_retargeted() -> None:
    """A pull request stacked on another branch is checked at each push, and against its new base once retargeted (the
    `edited` action): with `branches: [main]` and the default types, neither got a run until a close and reopen."""
    workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text()
    block = re.search(r'^    pull_request:\n((?:        .*\n)*)', workflow, re.MULTILINE)
    assert block is not None
    assert 'branches' not in block[1]
    types = re.search(r'types: \[(.*)\]', block[1])
    assert types is not None
    assert {'opened', 'synchronize', 'reopened', 'edited'} <= {name.strip() for name in types[1].split(',')}
    # A job skipped on a title edit would replace the last result in the pull request's checks, a failure included.
    assert not re.search(r'^        if:', workflow, re.MULTILINE)


def run_hook(
    tmp_path: Path, lint_staged_installed: bool, lint: str = '', prepare: Callable[[Path], None] | None = None
) -> tuple[list[str], subprocess.CompletedProcess[str]]:
    """The pre-commit hook in a scratch repo, every tool it may call faked (`lint`: what the fake scripts/lint.sh does
    once its call is logged; `prepare`: what the repo holds, staged or not): (the calls, in order; the hook's run)."""
    repo, fakes, calls = tmp_path / 'repo', tmp_path / 'bin', tmp_path / 'calls'
    logger = '#!/bin/sh\necho "$(basename "$0") $*" >> "$CALLS"\n'
    tools = [fakes / name for name in ('npx', 'ruff', 'node')] + [repo / 'scripts' / 'lint.sh']
    tools += [repo / '.venv' / 'bin' / name for name in ('ruff', 'python')]
    if lint_staged_installed:
        tools.append(repo / 'node_modules' / '.bin' / 'lint-staged')
    for tool in tools:
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text(logger + (lint if tool.name == 'lint.sh' else ''))
        tool.chmod(0o755)
    (repo / '.husky').mkdir()
    (repo / '.husky' / 'pre-commit').write_text((ROOT / '.husky' / 'pre-commit').read_text())
    (repo / 'frontend').mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'add', 'scripts/lint.sh'], cwd=repo, check=True)
    if prepare is not None:
        prepare(repo)
    env = {**os.environ, 'PATH': f'{fakes}{os.pathsep}{os.environ["PATH"]}', 'CALLS': str(calls)}
    done = subprocess.run(
        ['sh', '-e', '.husky/pre-commit'], cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    return calls.read_text().splitlines(), done


@pytest.mark.parametrize('lint_staged_installed', [False, True])
def test_the_pre_commit_hook_runs_the_locked_lint_staged_then_make_lints_checks(
    tmp_path: Path, lint_staged_installed: bool
) -> None:
    """Never a lint-staged npx would fetch (the registry's latest, over the network); the whole committed tree checked
    by scripts/lint.sh, the gate `make lint` runs, so the two never drift."""
    calls, done = run_hook(tmp_path, lint_staged_installed)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [call for call in calls if call.startswith('npx')], calls
    fixed = ['lint-staged --no-stash'] if lint_staged_installed else []
    venv = (tmp_path / 'repo').resolve() / '.venv' / 'bin'
    assert calls == [*fixed, f'lint.sh {venv / "ruff"} {venv / "python"}']
    assert ('npm ci' in done.stdout) is not lint_staged_installed  # what to do to have the staged files fixed


@pytest.mark.parametrize(('staged', 'on_disk', 'passes'), [('BAD', 'GOOD', False), ('GOOD', 'BAD', True)])
def test_the_pre_commit_hook_checks_what_is_committed_not_the_working_tree(
    tmp_path: Path, staged: str, on_disk: str, passes: bool
) -> None:
    """A file fixed on disk but not staged is committed as it was staged, and a file never staged is not committed:
    the hook's verdict is the commit's, as CI's will be."""

    def prepare(repo: Path) -> None:
        (repo / 'app.py').write_text(staged)
        subprocess.run(['git', 'add', 'app.py'], cwd=repo, check=True)
        (repo / 'app.py').write_text(on_disk)
        (repo / 'wip.py').write_text(on_disk)

    lint = 'if cat ./*.py 2>/dev/null | grep -q BAD; then exit 1; fi\n'
    _, done = run_hook(tmp_path, lint_staged_installed=False, lint=lint, prepare=prepare)
    assert (done.returncode == 0) is passes, done.stdout + done.stderr


def test_a_commit_the_hook_refuses_leaves_a_partially_staged_file_as_it_was(tmp_path: Path) -> None:
    """The real hook, .lintstagedrc.cjs and the root lock's lint-staged (from npm's cache alone: skipped without it), a
    check that fails: the unstaged part lint-staged hid is back on disk, the stash list untouched."""
    npm = shutil.which('npm')
    if npm is None:
        pytest.skip('no npm here to install lint-staged')
    repo = tmp_path / 'repo'
    (repo / '.husky').mkdir(parents=True)
    for name in ('package.json', 'package-lock.json', '.lintstagedrc.cjs', '.husky/pre-commit'):
        shutil.copy(ROOT / name, repo / name)
    install = [npm, 'ci', '--offline', '--ignore-scripts', '--no-audit', '--no-fund']
    done = subprocess.run(install, cwd=repo, capture_output=True, text=True, timeout=120, check=False)
    if done.returncode != 0:
        pytest.skip(f"the root lock's lint-staged is not in npm's cache: {done.stderr.strip()[-300:]}")
    # ruff's check refuses every file; scripts/lint.sh, after lint-staged, is never reached.
    fakes = {'.venv/bin/ruff': '[ "$1" != check ]', '.venv/bin/python': 'true', 'scripts/lint.sh': 'true'}
    for tool, body in fakes.items():
        (repo / tool).parent.mkdir(parents=True, exist_ok=True)
        (repo / tool).write_text(f'#!/bin/sh\n{body}\n')
        (repo / tool).chmod(0o755)
    names = {'NAME': 't', 'EMAIL': 't@t'}
    who = {f'GIT_{role}_{key}': value for role in ('AUTHOR', 'COMMITTER') for key, value in names.items()}
    env = {**os.environ, **who, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['git', *args], cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=False
        )

    (repo / '.gitignore').write_text('node_modules\n')
    (repo / 'app.py').write_text('x = 1\n')
    for command in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'base'], ['config', 'core.hooksPath', '.husky']):
        assert git(*command).returncode == 0, command
    (repo / 'app.py').write_text('x = 1\ny = 2\n')
    assert git('add', 'app.py').returncode == 0
    (repo / 'app.py').write_text('# unstaged\nx = 1\ny = 2\n')
    done = git('commit', '-m', 'refused')
    assert done.returncode != 0, done.stdout + done.stderr
    assert (repo / 'app.py').read_text() == '# unstaged\nx = 1\ny = 2\n', done.stdout + done.stderr
    assert git('show', ':app.py').stdout == 'x = 1\ny = 2\n'
    assert git('stash', 'list').stdout == ''


def test_the_root_lock_holds_a_lint_staged_that_puts_back_what_it_hid() -> None:
    """What the test above shows where npm's cache has the lock (never in CI): under --no-stash, a lint-staged before
    17.1.0 leaves a failed check's partially staged file without its unstaged part."""
    declared = json.loads((ROOT / 'package.json').read_text())['devDependencies']['lint-staged']
    locked = json.loads((ROOT / 'package-lock.json').read_text())['packages']['node_modules/lint-staged']['version']
    floor = re.fullmatch(r'\^(\d+)\.(\d+)\.(\d+)', declared)
    assert floor, f'lint-staged {declared}: a caret range, its floor the release that restores'
    assert tuple(map(int, floor.groups())) >= (17, 1, 0), declared
    assert tuple(map(int, locked.split('.'))) >= (17, 1, 0), locked


def test_every_action_is_a_commit_named_by_its_release() -> None:
    """A tag can be moved to other code, a commit cannot; the comment says which release the commit is."""
    uses = [
        (path.name, line.strip()) for path in WORKFLOWS for line in path.read_text().splitlines() if 'uses:' in line
    ]
    assert uses
    for name, line in uses:
        assert re.fullmatch(r'(- )?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+', line), f'{name}: {line}'


def setup_uv() -> str:
    """The inputs CI gives setup-uv, as written."""
    ci = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text()
    step = re.search(r'uses: astral-sh/setup-uv@\S+.*\n {14}with:\n((?: {18}.*\n)+)', ci)
    assert step, 'no setup-uv step with inputs'
    return step[1]


def ci_uv() -> str:
    """The uv release CI installs: setup-uv's `version`."""
    version = re.search(r"^ {18}version: '(\d+\.\d+\.\d+)'$", setup_uv(), re.MULTILINE)
    assert version, f'setup-uv is given no uv release:\n{setup_uv()}'
    return version[1]


def ci_pythons() -> list[str]:
    """The Pythons of the python job's matrix, one leg each."""
    legs = re.search(r'^ +python: \[([^\]]*)\]', (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(), re.MULTILINE)
    assert legs, 'the python job has no matrix of Pythons'
    return [leg.strip().strip("'") for leg in legs[1].split(',')]


def test_each_python_leg_restores_a_uv_cache_of_its_own() -> None:
    """setup-uv names its cache after the Python it is given, else after the one `uv python find` finds before the leg
    installs its own: the runner's, in every leg. Compiled wheels fit one Python: under one key, the leg whose Python
    is not the saved wheels' downloads its own again at every run, and never saves them."""
    inputs = dict(re.findall(r'^ {18}([\w-]+): (.*?)\s*(?:#.*)?$', setup_uv(), re.MULTILINE))
    assert inputs.get('enable-cache') == 'true'

    def key(python: str) -> tuple[str, str]:
        given = {name: value.replace('${{ matrix.python }}', python) for name, value in inputs.items()}
        return given.get('python-version', "the runner's"), given.get('cache-suffix', '')

    assert len({key(python) for python in ci_pythons()}) == len(ci_pythons()) > 1


def test_ci_installs_a_named_uv_release() -> None:
    """With no `version` (and no required-version in pyproject.toml), setup-uv takes the latest uv at each run: the uv
    that judges `uv sync --locked` would change without a commit."""
    assert re.fullmatch(r'\d+\.\d+\.\d+', ci_uv())


def test_an_npm_older_than_cis_refuses_to_write_the_frontend_lock(tmp_path: Path) -> None:
    """CI's Node is a release, so its npm is one; the frontend asks for at least that npm, and npm refuses to install
    or write the lock below it: an older npm drops entries from package-lock.json that CI's `npm ci` requires."""
    ci = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text()
    pinned = re.search(r"node-version: '\d+\.\d+\.\d+' # its npm (\d+\.\d+\.\d+)", ci)
    assert pinned, 'CI installs the frontend on a Node that can move, so with an npm that can move'
    frontend = ROOT / 'frontend'
    package = json.loads((frontend / 'package.json').read_text())
    assert package.get('engines', {}).get('npm') == f'>={pinned[1]}'
    npm = shutil.which('npm')
    if npm is None:
        pytest.skip('no npm here to refuse anything')
    # The floor raised above this npm, in a copy: npm itself must refuse (engine-strict), not warn and write the lock.
    for name in ('package-lock.json', '.npmrc'):
        if (frontend / name).exists():
            shutil.copy(frontend / name, tmp_path / name)
    local = subprocess.run([npm, '--version'], capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / 'package.json').write_text(json.dumps({**package, 'engines': {'npm': f'>{local}'}}))
    lock = (tmp_path / 'package-lock.json').read_bytes()
    env = {**os.environ, 'npm_config_cache': str(tmp_path / 'cache')}
    command = [npm, 'install', '--package-lock-only', '--ignore-scripts', '--offline']
    done = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode != 0, done.stdout + done.stderr
    assert 'EBADENGINE' in done.stderr
    assert (tmp_path / 'package-lock.json').read_bytes() == lock


def test_every_job_runs_on_a_named_image() -> None:
    """`*-latest` is a label GitHub moves to the next image: the job would change without a commit."""
    runners = [
        line.split(':', 1)[1].strip()
        for path in WORKFLOWS
        for line in path.read_text().splitlines()
        if 'runs-on:' in line
    ]
    assert runners
    assert not [runner for runner in runners if runner.endswith('-latest')]


def doctor(
    tmp_path: Path,
    plan: str | None = None,
    *,
    locked: bool = True,
    npm: str | None = None,
    uv: str | None = None,
    genai: str | None = None,
) -> list[str]:
    """`make doctor`'s lines, run in an empty directory with stand-ins for ssh (never the robot) and uv. Given the
    plan uv's dry run prints, the directory has a .venv with the server packages; `locked`: uv.lock is current. Given
    `npm`, an npm of that version stands in, and the directory has the frontend's package.json; given `uv`, the uv
    stand-in is that version, and the directory has CI's workflow; given `genai`, the robot answers with that
    google-genai, else it is unreachable."""
    stubs = tmp_path / 'bin'
    stubs.mkdir()
    (stubs / 'ssh').write_text('#!/bin/sh\n[ -n "$GENAI" ] && echo "$GENAI" && exit 0\nexit 255\n')
    answers = '[ "$1" = export ] && exit "$EXPORT_STATUS"\n[ "$1" = pip ] && cat >/dev/null && printf %s "$PLAN"\n'
    (stubs / 'uv').write_text(f'#!/bin/sh\n{answers}[ "$1" = --version ] && echo "uv {uv} (stand-in)"\nexit 0\n')
    if uv is not None:
        (tmp_path / '.github' / 'workflows').mkdir(parents=True)
        shutil.copy(ROOT / '.github' / 'workflows' / 'ci.yml', tmp_path / '.github' / 'workflows' / 'ci.yml')
    executables = [stubs / 'ssh', stubs / 'uv']
    if npm is not None:
        (stubs / 'npm').write_text(f'#!/bin/sh\necho {npm}\n')
        executables.append(stubs / 'npm')
        (tmp_path / 'frontend').mkdir()
        shutil.copy(ROOT / 'frontend' / 'package.json', tmp_path / 'frontend' / 'package.json')
    if plan is not None:
        (tmp_path / '.venv' / 'bin').mkdir(parents=True)
        python = tmp_path / '.venv' / 'bin' / 'python'
        python.write_text('#!/bin/sh\ncase "$*" in *version_info*) echo 3.12 ;; esac\n')
        executables.append(python)
    for path in executables:
        path.chmod(0o755)
    env = {
        **os.environ,
        'PATH': f'{stubs}{os.pathsep}{os.environ["PATH"]}',
        'ROBOT': 'nobody@invalid',
        'PLAN': plan or '',
        'EXPORT_STATUS': '0' if locked else '1',
        'GENAI': genai or '',
    }
    run = subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'doctor.sh')],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return run.stdout.splitlines()


def test_doctor_sets_the_venv_up_as_ci_does(tmp_path: Path) -> None:
    """The .venv doctor asks for is uv.lock's, as CI installs it: the tests run on the versions CI runs them on."""
    [line] = [line for line in doctor(tmp_path) if '.venv' in line]
    assert 'MISSING' in line
    assert CI_SYNC in line


@pytest.mark.parametrize(
    ('locked', 'plan', 'verdict', 'words'),
    [
        (True, 'Audited 40 packages in 0.01ms\nWould make no changes\n', 'OK', '.venv is uv.lock'),
        (
            True,
            'Would uninstall 2 packages\nWould install 1 package\n - pytest==9.0.3\n + pytest==9.2.0\n - six==1.17.0\n',
            'WARN',
            f'.venv is not uv.lock (2 to remove, 1 to install) — {CI_SYNC}',
        ),
        (False, '', 'WARN', 'uv.lock is behind pyproject.toml — uv lock'),
    ],
)
def test_doctor_says_whether_the_venv_is_the_lock(
    tmp_path: Path, locked: bool, plan: str, verdict: str, words: str
) -> None:
    """A .venv installed some other way runs the tests on other versions than CI's: doctor says so."""
    [line] = [line for line in doctor(tmp_path, plan, locked=locked) if 'uv.lock' in line]
    assert line.split()[0] == verdict
    assert words in line


@pytest.mark.parametrize('below', [True, False])
def test_doctor_says_when_npm_cannot_install_the_frontend(tmp_path: Path, below: bool) -> None:
    """Below the frontend's engines floor (CI's npm), npm refuses to install it (engine-strict): doctor must not say OK,
    and says how to upgrade."""
    floor = json.loads((ROOT / 'frontend' / 'package.json').read_text())['engines']['npm'].removeprefix('>=')
    version = f'{int(floor.split(".")[0]) - 1}.9.9' if below else floor
    [line] = [line for line in doctor(tmp_path, npm=version) if 'npm' in line]
    if below:
        assert line.split()[0] == 'MISSING', line
        assert f'npm {version} < {floor}' in line and f'npm install -g npm@{floor}' in line
    else:
        assert line.split() == ['OK', 'npm']


@pytest.mark.parametrize('cis', [True, False])
def test_doctor_says_when_uv_is_not_cis(tmp_path: Path, cis: bool) -> None:
    """Another uv than CI's may write a lock CI's refuses, or accept one CI's does not: doctor says which to install,
    with uv's standalone installer for that release (`uv self update` needs a receipt of that installer, and refuses a
    uv installed another way or whose receipt names another place)."""
    version = ci_uv() if cis else '0.5.29'
    [line] = [line for line in doctor(tmp_path, uv=version) if line.split()[1:2] == ['uv']]
    if cis:
        assert line.split() == ['OK', 'uv']
    else:
        assert line.split()[0] == 'WARN', line
        installer = f'curl -LsSf https://astral.sh/uv/{ci_uv()}/install.sh | sh'
        assert line.endswith(f"uv {version}, not CI's {ci_uv()} — {installer}"), line


def genai_spec(path: Path) -> str:
    [spec] = re.findall(r'"google-genai([^"]*)"', path.read_text())
    return spec


def test_the_robot_is_given_only_the_google_genai_major_ci_runs() -> None:
    """`make robot-deps` installs the range CI's dev extra holds, and that range stops below the next major of uv.lock's
    google-genai: a major CI never ran is never installed by it (3.0 opens the Live session on another endpoint)."""
    locked = Version(re.search(r'name = "google-genai"\nversion = "([^"]+)"', (ROOT / 'uv.lock').read_text())[1])
    spec = genai_spec(ROOT / 'Makefile')
    assert spec == genai_spec(ROOT / 'pyproject.toml')
    assert locked in SpecifierSet(spec)
    assert Version(f'{locked.major + 1}.0.0') not in SpecifierSet(spec), spec


@pytest.mark.parametrize(
    ('version', 'verdict'), [('2.34.0', 'OK'), ('2.24.0', 'OK'), ('2.23.9', 'MISSING'), ('3.0.0', 'MISSING')]
)
def test_doctor_says_whether_the_robot_runs_a_google_genai_ci_runs(tmp_path: Path, version: str, verdict: str) -> None:
    """The range robot-deps installs and the agent accepts at boot: below it or past it, doctor says to run robot-deps."""
    [line] = [line for line in doctor(tmp_path, genai=version) if 'google-genai' in line]
    assert line.split()[0] == verdict, line
    assert f'google-genai {version}' in line
    if verdict != 'OK':
        assert line.endswith('make robot-deps'), line
