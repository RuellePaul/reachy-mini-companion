"""The notes' clock: a note waits for a quiet point, out of a scene however long, and is dropped once its moment has
passed. The clock is the test's, so a wait of 60 s is exact; the session's own note tests cover a note sent at a
quiet point."""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest

from robot_agent import notes

TEXT = "L'app « Pong » est prête."
WAIT_S = notes.WAIT_S


class Stage:
    """What the notes see, set by the test: the clock, a quiet point, a scene playing."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now, self.quiet, self.scene = 0.0, False, False
        self.sent: list[str] = []
        monkeypatch.setattr(notes, 'time', SimpleNamespace(monotonic=lambda: self.now))
        monkeypatch.setattr(notes, 'POLL_S', 0.001)
        self.notes = notes.Notes(self._send, lambda: self.quiet, lambda: self.scene)

    async def _send(self, text: str) -> None:
        self.sent.append(text)

    async def at(self, now: float, **change: bool) -> None:
        """The stage from `now` on: the note's next poll comes before this sleep ends, so it sees it."""
        self.now = now
        for name, value in change.items():
            setattr(self, name, value)
        await asyncio.sleep(5 * notes.POLL_S)


def test_a_note_waits_out_a_scene_longer_than_its_wait_then_has_its_whole_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    stage = Stage(monkeypatch)

    async def scenario() -> None:
        stage.notes.add(TEXT)
        await stage.at(0.0, scene=True)
        await stage.at(4 * WAIT_S)  # a scene four times the wait
        assert stage.sent == []
        await stage.at(4 * WAIT_S, scene=False)  # the curtain, and someone speaks at once
        await stage.at(4.5 * WAIT_S)  # still talking: the wait counts from the curtain
        assert stage.sent == []
        await stage.at(4.5 * WAIT_S, quiet=True)
        assert stage.sent == [TEXT]

    asyncio.run(scenario())


def test_a_note_with_no_quiet_moment_in_its_wait_after_a_scene_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    stage = Stage(monkeypatch)

    async def scenario() -> None:
        stage.notes.add(TEXT)
        await stage.at(0.0, scene=True)
        await stage.at(4 * WAIT_S)
        await stage.at(4 * WAIT_S, scene=False)  # the curtain, then talking for longer than the wait
        await stage.at(5.5 * WAIT_S)
        await stage.at(5.5 * WAIT_S, quiet=True)  # too late: the moment has passed
        assert stage.sent == []

    asyncio.run(scenario())


def test_a_note_with_no_quiet_moment_in_its_wait_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    stage = Stage(monkeypatch)

    async def scenario() -> None:
        stage.notes.add(TEXT)
        await stage.at(0.0)
        await stage.at(3 * WAIT_S)  # talking all along, no scene
        await stage.at(3 * WAIT_S, quiet=True)  # too late: the moment has passed
        assert stage.sent == [] and not stage.notes.after(-math.inf)  # nor taken for one that went out

    asyncio.run(scenario())
