"""The dashboard's transcript of one session: turn ids, deltas and ends, and each reply's latency."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)
# Gemini can wrap a rendition of the robot's speech in markup (<speaker:0><audio:player src=…/>).
_MARKUP = re.compile(r'<[^>]*>')


class Transcript:
    """Turns as `user_delta` / `user_end` / `robot_delta` / `robot_end` / `latency` events (contracts.py).

    A user turn ends with the turn (turn_complete, or no reply), since the recogniser may still be finishing
    the sentence when the robot starts answering. The latency runs from the end of the user's turn (Gemini's
    VAD end, or a typed say) to the reply's first audio; a turn left unanswered has none.
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit
        self._user: tuple[str, list[str]] | None = None  # the open user turn: id, chunks
        self._robot: tuple[str, list[str]] | None = None  # the open robot turn
        self._waiting_since: float | None = None  # end of the user's turn, until the reply's first audio
        self._voiced: str | None = None  # the last robot turn whose audio started
        self._plain = False  # the open robot turn has plain (markup-free) text

    def heard(self, text: str) -> None:
        """A committed input transcription chunk."""
        text = _MARKUP.sub(' ', text)
        if self._user is None:
            self._user = (_turn_id(), [])
        self._user[1].append(text)
        self._emit({'kind': 'user_delta', 'turn_id': self._user[0], 'text': text})

    def typed(self, text: str, at: float) -> None:
        """A dashboard `say`: a whole user turn at once."""
        self._end_user()
        self._emit({'kind': 'user_end', 'turn_id': _turn_id(), 'text': text, 'typed': True})
        self._waiting_since = at

    def user_done(self, at: float) -> None:
        """Gemini's VAD closed on the user."""
        self._waiting_since = at

    def said(self, text: str) -> None:
        """An output transcription chunk. Gemini sometimes sends a turn's text a second time, cut into audio
        segments by markup: markup never reaches the transcript, and a marked-up rendition is dropped once the
        turn has plain text."""
        marked = _MARKUP.search(text) is not None
        if marked and self._plain:
            return
        text = _MARKUP.sub(' ', text)
        self._plain = self._plain or not marked
        turn_id, chunks = self._robot_turn()
        chunks.append(text)
        self._emit({'kind': 'robot_delta', 'turn_id': turn_id, 'text': text})

    def audio(self, now: float) -> None:
        """A chunk of the robot's audio: a turn's first chunk after the user's turn ended gives the latency. The
        rest of an answer that was already playing when the user typed is no reply to them."""
        turn_id, _ = self._robot_turn()
        if turn_id == self._voiced:
            return
        self._voiced = turn_id
        if self._waiting_since is not None:
            ms = max(0, round((now - self._waiting_since) * 1000))
            logger.info('[LATENCY] %d ms', ms)
            self._emit({'kind': 'latency', 'turn_id': turn_id, 'ms': ms})
            self._waiting_since = None

    def interrupted(self) -> None:
        self._end_robot()

    def close(self) -> None:
        """The turn is over: turn_complete, or no reply came."""
        self._end_user()
        self._end_robot()

    def unanswered(self) -> None:
        """The answer is over and never sounded (no reply came, or the model declined): the turn ends, and the
        robot's next audio (its answer to a note, or its own) is no reply to it."""
        self.close()
        self._waiting_since = None

    def _robot_turn(self) -> tuple[str, list[str]]:
        if self._robot is None:
            self._robot = (_turn_id(), [])
        return self._robot

    def _end_user(self) -> None:
        if self._user is not None:
            (turn_id, chunks), self._user = self._user, None
            text = ''.join(chunks).strip()
            logger.info('USER: %s', text)
            self._emit({'kind': 'user_end', 'turn_id': turn_id, 'text': text, 'typed': False})

    def _end_robot(self) -> None:
        self._plain = False
        if self._robot is not None:
            (turn_id, chunks), self._robot = self._robot, None
            text = ' '.join(''.join(chunks).split())
            logger.info('ROBOT: %s', text)
            self._emit({'kind': 'robot_end', 'turn_id': turn_id, 'text': text})


def _turn_id() -> str:
    return secrets.token_hex(4)
