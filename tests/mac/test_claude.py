"""claude.Run against a real child process: a script named `claude` on PATH stands for `claude -p` (no Claude is
called), so the pipes, the parsing of what a newer CLI may print, the cancel, the timeout and the kill are the real
ones."""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from mac_server import claude

HANG_S = 20  # how long a hung child sleeps: far above every bound below
SCRIPT = """#!{python}
import json, os, pathlib, sys, time
prompt = sys.stdin.read()
pathlib.Path(os.environ['FAKE_CLAUDE_LAUNCHED']).write_text(json.dumps(os.listdir()))
mode = os.environ['FAKE_CLAUDE_MODE']
def say(line):
    print(line if isinstance(line, str) else json.dumps(line), flush=True)
if mode == 'fail':
    print('Usage limit reached.', file=sys.stderr)
    sys.exit(3)
say('not json at all')
say([1, 2])
say({{'type': 'stream_event', 'event': {{'type': 'content_block_start', 'content_block': 'odd'}}}})
say({{'type': 'stream_event', 'event': {{'type': 'content_block_start', 'content_block': {{'type': 'thinking'}}}}}})
say({{'type': 'system', 'subtype': 'thinking_tokens', 'estimated_tokens': '1200'}})
say({{'type': 'user', 'tool_use_result': 'ok'}})
say({{'type': 'stream_event', 'event': {{'type': 'message_delta', 'delta': {{'stop_reason': None}}}}}})
say({{'type': 'stream_event', 'event': {{'type': 'content_block_start', 'content_block': {{'type': 'text'}}}}}})
say({{'type': 'stream_event', 'event': {{'type': 'content_block_delta', 'delta': {{'type': 'text_delta', 'text': '<html>'}}}}}})
if mode == 'hang':
    time.sleep({hang})
say({{'type': 'result', 'result': prompt.upper(), 'is_error': False, 'total_cost_usd': 0.02, 'duration_ms': 5}})
"""


@pytest.fixture
def child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Puts the fake `claude` first on PATH; the function returned sets its mode and gives the file a launched child
    writes (what its working directory holds)."""
    binary = tmp_path / 'bin' / 'claude'
    binary.parent.mkdir()
    binary.write_text(SCRIPT.format(python=sys.executable, hang=HANG_S))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv('PATH', f'{binary.parent}{os.pathsep}{os.environ["PATH"]}')
    launched = tmp_path / 'launched'
    monkeypatch.setenv('FAKE_CLAUDE_LAUNCHED', str(launched))

    def mode(name: str) -> Path:
        monkeypatch.setenv('FAKE_CLAUDE_MODE', name)
        return launched

    return mode


def run(**kwargs) -> claude.Run:
    return claude.Run(
        'fais un morpion', 'Tu écris des pages.', effort='low', what='la page', **{'timeout_s': 10, **kwargs}
    )


def test_what_a_newer_cli_prints_otherwise_is_skipped(child) -> None:
    launched = child('ok')
    phases: list[str] = []
    turn = run(on_progress=lambda progress: phases.append(progress.phase))
    answer = turn.wait()
    assert answer.text == 'FAIS UN MORPION' and answer.cost_usd == 0.02 and turn.cost_usd == 0.02
    assert (turn.progress.thinking_tokens, turn.progress.text, turn.progress.phase) == (1200, '<html>', 'writing')
    assert phases == ['thinking', 'thinking', 'writing', 'writing']
    assert launched.read_text() == '[]'  # no tools: launched in an empty directory


def test_a_failure_says_what_claude_wrote_on_stderr(child) -> None:
    child('fail')
    with pytest.raises(claude.ClaudeError, match=r"^Claude n'a pas écrit la page \(Usage limit reached\.\)\.$"):
        run().wait()


def test_a_cancel_before_the_launch_launches_nothing(child) -> None:
    launched = child('ok')
    turn = run()
    turn.cancel()
    with pytest.raises(claude.Cancelled):
        turn.wait()
    assert not launched.exists()


def test_a_cancel_from_another_thread_ends_a_hung_child(child) -> None:
    child('hang')
    writing = threading.Event()
    turn = run(timeout_s=60, on_progress=lambda progress: progress.phase == 'writing' and writing.set())
    threading.Thread(target=lambda: writing.wait(10) and turn.cancel(), daemon=True).start()
    started = time.monotonic()
    with pytest.raises(claude.Cancelled):
        turn.wait()
    assert time.monotonic() - started < 5


def test_a_timeout_kills_a_hung_child(child) -> None:
    child('hang')
    started = time.monotonic()
    with pytest.raises(claude.ClaudeError, match='minutes'):
        run(timeout_s=1.0).wait()
    assert time.monotonic() - started < 5


def test_a_reading_that_breaks_off_kills_the_child(child) -> None:
    child('hang')

    def broken(_progress: claude.Progress) -> None:
        raise RuntimeError('cassé')

    started = time.monotonic()
    with pytest.raises(RuntimeError, match='cassé'):
        run(timeout_s=60, on_progress=broken).wait()
    assert time.monotonic() - started < 5


def test_the_events_shown_are_read_as_the_cli_writes_them() -> None:
    progress = claude.Progress()
    events = [
        {'type': 'system', 'subtype': 'thinking_tokens', 'estimated_tokens': 300},
        {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'name': 'Read'}}},
        {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'name': 'Edit'}}},
        {'type': 'user', 'tool_use_result': {'oldString': 'rouge', 'newString': 'vert'}},
        {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'name': 'Edit'}}},
    ]
    assert [progress.take(event) for event in events] == [True, True, True, True, False]
    assert (progress.phase, progress.thinking_tokens, progress.edits) == ('editing', 300, [('rouge', 'vert')])
