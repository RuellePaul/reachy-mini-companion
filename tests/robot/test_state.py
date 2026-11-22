"""The transition table, complete: every state and every event, with and without a tool pending or the
model's answer still open."""

from __future__ import annotations

import pytest

from robot_agent.state import Event, SessionState, transition

CONN, RECON, IDLE, LIST, THINK, SPEAK, CLOSE = SessionState
REST = 'rest'  # IDLE, or THINKING while a tool call is pending or the model's answer is not over
CUT = 'cut'  # LISTENING (a barge-in), or THINKING while the answer is owed: a say's, nobody spoke

# event: the next state from CONNECTING, RECONNECTING, IDLE, LISTENING, THINKING, SPEAKING, CLOSING
TABLE = {
    Event.CONNECTED: (REST, REST, IDLE, LIST, THINK, SPEAK, CLOSE),
    Event.VAD_START: (CONN, RECON, LIST, LIST, LIST, LIST, CLOSE),
    Event.VAD_END: (CONN, RECON, THINK, THINK, THINK, THINK, CLOSE),
    Event.TRANSCRIPT_IN: (CONN, RECON, LIST, LIST, THINK, SPEAK, CLOSE),
    Event.USER_TEXT: (CONN, RECON, THINK, THINK, THINK, THINK, CLOSE),
    Event.INTERRUPTED: (CONN, RECON, CUT, LIST, THINK, CUT, CLOSE),  # thinking: the user's turn is already over
    Event.AUDIO_OUT: (CONN, RECON, SPEAK, SPEAK, SPEAK, SPEAK, CLOSE),
    Event.PLAYBACK_DRAINED: (CONN, RECON, IDLE, LIST, THINK, REST, CLOSE),
    Event.TURN_COMPLETE: (CONN, RECON, REST, LIST, REST, SPEAK, CLOSE),
    Event.INTERACTION_IDLE: (CONN, RECON, REST, LIST, REST, SPEAK, CLOSE),
    Event.TOOL_STARTED: (CONN, RECON, THINK, LIST, THINK, SPEAK, CLOSE),
    Event.TOOL_FINISHED: (CONN, RECON, IDLE, LIST, THINK, SPEAK, CLOSE),  # the reply to the answer is still to come
    Event.IDLE_TIMEOUT: (CLOSE, CLOSE, CLOSE, CLOSE, CLOSE, CLOSE, CLOSE),
    Event.SLEEP_REQUESTED: (CLOSE, CLOSE, CLOSE, CLOSE, CLOSE, CLOSE, CLOSE),
    Event.GO_AWAY: (CONN, RECON, IDLE, LIST, THINK, SPEAK, CLOSE),
    Event.CONNECTION_LOST: (CONN, RECON, RECON, RECON, RECON, RECON, CLOSE),  # the wake's until one has served
}
STATES = (CONN, RECON, IDLE, LIST, THINK, SPEAK, CLOSE)


def test_the_table_covers_every_event() -> None:
    assert set(TABLE) == set(Event)


@pytest.mark.parametrize(('tool_pending', 'answering'), [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('event', list(Event))
@pytest.mark.parametrize('index', range(len(STATES)))
def test_transition(event: Event, index: int, tool_pending: bool, answering: bool) -> None:
    expected = TABLE[event][index]
    if expected == REST:
        expected = THINK if tool_pending or answering else IDLE
    elif expected == CUT:
        expected = THINK if answering else LIST
    assert transition(STATES[index], event, tool_pending=tool_pending, answering=answering) is expected
