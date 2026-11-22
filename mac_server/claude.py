"""Headless Claude Code turns on the owner's subscription: `Run`, streamed and cancellable, with Claude's own file
tools if asked (an app's page) or none (a scene's emotions). An ANTHROPIC_API_KEY in its environment would bill API
credits instead, so it is removed."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MODEL = 'claude-opus-5-5'  # chosen by the owner; an app's model is a setting (contracts.APP_MODELS)
# --restricted on every run: no tool that runs code, none of the user's settings, and no peer session that the
# owner's other Claude Code sessions could list and message.
QUIET = ['--strict-mcp-config', '--safe-mode', '--disable-slash-commands', '--no-session-persistence', '--restricted']
# File tools only, confined to the working directory by --restricted whatever the user's settings say.
FENCED = ['--permission-mode', 'acceptEdits', '--permission-prompts', 'none']
STREAM = ['--output-format', 'stream-json', '--include-partial-messages', '--verbose']


class ClaudeError(Exception):
    """Why Claude gave no answer, in French."""


class Cancelled(ClaudeError):
    """The run was cancelled."""


def _binary() -> str:
    if (binary := shutil.which('claude')) is None:
        raise ClaudeError('La commande claude est introuvable sur ce Mac\xa0: installe Claude Code.')
    return binary


def _env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')}


@dataclass
class Progress:
    """What a streamed turn has shown so far, read from Claude Code's stream-json events."""

    phase: str = 'starting'  # starting, thinking, writing, reading, editing
    thinking_tokens: int = 0  # estimated: the thinking itself is never shown
    text: str = ''  # the answer so far (a creation: the page being written)
    edits: list[tuple[str, str]] = field(default_factory=list)  # (old, new) of each Edit applied

    def take(self, event: dict[str, Any]) -> bool:
        """Read one event; whether anything shown changed."""
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'thinking_tokens':
            self.thinking_tokens = int(event.get('estimated_tokens') or self.thinking_tokens)
            return True
        if kind == 'user':
            done = event.get('tool_use_result')
            if isinstance(done, dict) and 'oldString' in done:
                self.edits.append((str(done.get('oldString', '')), str(done.get('newString', ''))))
                return True
            return False
        inner = event.get('event') if kind == 'stream_event' else None
        if not isinstance(inner, dict):
            return False
        if inner.get('type') == 'content_block_start':
            block = inner.get('content_block') or {}
            names = {'Read': 'reading', 'Edit': 'editing'}
            phase = {'thinking': 'thinking', 'text': 'writing'}.get(
                str(block.get('type')), names.get(str(block.get('name')))
            )
            changed, self.phase = phase is not None and phase != self.phase, phase or self.phase
            return changed
        delta = inner.get('delta') or {}
        if inner.get('type') == 'content_block_delta' and delta.get('type') == 'text_delta':
            self.text += str(delta.get('text', ''))
            return True
        return False


@dataclass(frozen=True)
class Answer:
    text: str
    cost_usd: float | None
    duration_ms: int | None


class Run:
    """One streamed turn. `wait` runs it in the calling thread, `on_progress` called on each change; `cancel`, from
    any thread, ends it (`wait` then raises Cancelled). `tools`: Claude Code's own (e.g. 'Read,Edit'), fenced to
    `workdir`; none by default, in an empty directory."""

    def __init__(
        self,
        text: str,
        system: str,
        *,
        effort: str,
        timeout_s: float,
        what: str,
        tools: str = '',
        workdir: Path | None = None,
        on_progress: Callable[[Progress], None] = lambda _p: None,
        model: str = MODEL,
    ) -> None:
        self._text, self._system, self._effort, self._timeout_s, self._what = text, system, effort, timeout_s, what
        self._model = model
        self._tools, self._workdir, self._on_progress = tools, workdir, on_progress
        self.progress = Progress()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = self._timed_out = False
        self.cost_usd = 0.0  # what the run cost, said by its result even when it failed

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._process is not None:
                self._process.terminate()

    def wait(self) -> Answer:
        args = [_binary(), '-p', '--model', self._model, '--effort', self._effort, *STREAM, '--tools', self._tools]
        args += QUIET
        args += [*(FENCED if self._tools else []), '--system-prompt', self._system]
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryFile('w+') as errors:
            with self._lock:
                if self._cancelled:
                    raise Cancelled('Construction annulée.')
                try:
                    self._process = subprocess.Popen(
                        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True,
                        cwd=self._workdir or empty, env=_env(),
                    )  # fmt: skip
                except OSError as exc:
                    raise ClaudeError("Claude n'a pas pu être lancé sur ce Mac.") from exc
            timer = threading.Timer(self._timeout_s, self._expire)
            timer.start()
            try:
                result = self._read(self._process)
            except BaseException:
                self._process.kill()  # the reading broke off: a claude left writing to a full pipe would never end
                raise
            finally:
                timer.cancel()
                self._process.wait()
            errors.seek(0)
            stderr = errors.read().strip()
        self.cost_usd = float((result or {}).get('total_cost_usd') or 0.0)
        if self._cancelled:
            raise Cancelled('Construction annulée.')
        if self._timed_out:
            raise ClaudeError(f"Claude n'a pas fini en {self._timeout_s / 60:.0f} minutes.")
        if result is None or result.get('is_error') or self._process.returncode != 0:
            why = str((result or {}).get('result') or stderr)[:200]
            log.warning('claude failed on %s (%s): %s', self._what, self._process.returncode, why)
            raise ClaudeError(f"Claude n'a pas écrit {self._what} ({why or f'code {self._process.returncode}'}).")
        answer = Answer(str(result.get('result') or ''), result.get('total_cost_usd'), result.get('duration_ms'))
        log.info('claude, %s: %s ms, %s USD (API equivalent)', self._what, answer.duration_ms, answer.cost_usd)
        return answer

    def _read(self, process: subprocess.Popen[str]) -> dict[str, Any] | None:
        assert process.stdin is not None and process.stdout is not None
        with contextlib.suppress(BrokenPipeError):
            process.stdin.write(self._text)
            process.stdin.close()
        result = None
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'result':
                result = event
                continue
            try:
                changed = self.progress.take(event)
            except (TypeError, ValueError, AttributeError):
                continue  # an event shaped otherwise (a newer CLI): only the progress misses it
            if changed:
                self._on_progress(self.progress)
        return result

    def _expire(self) -> None:
        with self._lock:
            self._timed_out = True
            if self._process is not None:
                self._process.kill()
