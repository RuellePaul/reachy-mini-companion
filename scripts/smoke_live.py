"""Manual smoke test against the real Gemini Live API. It costs money: run it by hand, never in CI.

One connection (two if Google Search is refused and the robot's fallback retries without it) with the
robot's own setup: the Mac's prompt for the default settings, every robot tool, Google Search as
the robot asks for it. It sends text turns, checks that each one is answered with audio and that a
usage_metadata arrives, prints the usage, and closes.

    .venv/bin/python scripts/smoke_live.py [--turns 1..3]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from robot_agent import boot  # noqa: E402 — the repo root must be on sys.path first

boot.load_env()
boot.require_genai()

from google.genai import types  # noqa: E402 — after the SDK floor check

from mac_server import prompts  # noqa: E402
from mac_server.contracts import Settings  # noqa: E402
from robot_agent import config, live, tools  # noqa: E402 — config reads the .env loaded above
from robot_agent.emotions import CURATED  # noqa: E402

TURNS = (
    'Bonjour ! Réponds en une phrase courte : comment vas-tu ?',
    'Merci. En une phrase : qui es-tu ?',
    'Dernière question, une phrase : quel jour sommes-nous ?',
)
TURN_TIMEOUT_S = 30.0


class Turn:
    """What one turn brought, filled as the messages arrive (so a timeout still shows it)."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.audio_bytes = 0
        self.usages: list[types.UsageMetadata] = []
        self.said: list[str] = []

    def log(self, what: str) -> None:
        print(f'  {time.monotonic() - self.started:5.1f} s  {what}')


async def one_turn(session: Any, text: str, turn: Turn) -> None:
    """Send a user text turn and read like the robot does: receive() is re-entered (it ends at each
    interaction's end) and every tool call gets exactly one answer, scheduled as the robot schedules it
    (SILENT for a physical tool once it has spoken, WHEN_IDLE otherwise). Stops at an interaction's end
    once audio came, or when nothing more is owed (no tool call was answered meanwhile)."""
    await session.send_client_content(turns=[types.Content(role='user', parts=[types.Part(text=text)])])
    while True:
        answered = False
        async for message in session.receive():
            if message.usage_metadata is not None:
                turn.usages.append(message.usage_metadata)
                turn.log(f'usage: {describe(message.usage_metadata)}')
            if message.tool_call is not None:
                answers = []
                for call in message.tool_call.function_calls or []:
                    # The robot's own rule: a physical tool is silent only once the robot has spoken.
                    answer = tools.ToolResult('ok', 'Fait.').response(
                        call.id or '', call.name or '', spoke=turn.audio_bytes > 0
                    )
                    turn.log(f'tool_call {call.name}({dict(call.args or {})}) -> {answer.scheduling.value}')
                    answers.append(answer)
                await session.send_tool_response(function_responses=answers)
                answered = True
            content = message.server_content
            if content is None:
                continue
            for part in (content.model_turn.parts or []) if content.model_turn else []:
                if part.inline_data is not None and part.inline_data.data:
                    if not turn.audio_bytes:
                        turn.log('first audio')
                    turn.audio_bytes += len(part.inline_data.data)
            if content.output_transcription is not None and content.output_transcription.text:
                turn.said.append(content.output_transcription.text)
            if content.turn_complete or content.interaction_status:
                status = content.interaction_status.value if content.interaction_status else None
                turn.log(f'turn_complete={bool(content.turn_complete)} interaction_status={status}')
        if turn.audio_bytes or not answered:
            return


def describe(usage: types.UsageMetadata) -> str:
    def details(items: list[types.ModalityTokenCount] | None) -> str:
        return ', '.join(f'{d.modality.value if d.modality else "?"} {d.token_count}' for d in items or []) or '-'

    return (
        f'prompt {usage.prompt_token_count} ({details(usage.prompt_tokens_details)}) · '
        f'response {usage.response_token_count} ({details(usage.response_tokens_details)}) · '
        f'total {usage.total_token_count}'
    )


async def main(turns: int) -> int:
    if not config.GEMINI_API_KEY:
        print('GEMINI_API_KEY is missing from .env')
        return 1
    settings = Settings()
    view = {
        'settings': settings.model_dump(),
        'system_prompt': prompts.system_prompt(settings),
        'web_prompt': prompts.web_prompt(settings),
    }
    client = live.client(config.GEMINI_API_KEY)
    ok = True
    async with contextlib.AsyncExitStack() as stack:
        started = time.monotonic()
        session = await live.open_session(
            stack, client, view, tools.declarations(emotions=list(CURATED), camera=True), None
        )
        search = 'refused, retried without it' if live.SEARCH.refused else 'accepted'
        print(f'connected to {config.MODEL} in {(time.monotonic() - started) * 1000:.0f} ms (Google Search {search})')
        for n, text in enumerate(TURNS[:turns], 1):
            print(f'\nturn {n}: « {text} »')
            turn = Turn()
            try:
                await asyncio.wait_for(one_turn(session, text, turn), TURN_TIMEOUT_S)
            except TimeoutError:
                turn.log(f'timeout ({TURN_TIMEOUT_S:.0f} s)')
            seconds = turn.audio_bytes / 2 / config.GEMINI_OUTPUT_SR
            print(f'  -> {seconds:.1f} s of audio — « {"".join(turn.said).strip()} »')
            if seconds <= 0 or not turn.usages:
                print('  FAIL: no audio' if seconds <= 0 else '  FAIL: no usage_metadata')
                ok = False
    print('\nOK' if ok else '\nFAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=(__doc__ or '').splitlines()[0])
    parser.add_argument('--turns', type=int, default=2, choices=(1, 2, 3))
    sys.exit(asyncio.run(main(parser.parse_args().turns)))
