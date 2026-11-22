"""The session's states and events, and the one pure function between them."""

from __future__ import annotations

from enum import StrEnum


class SessionState(StrEnum):
    """Also the dashboard's `phase` (contracts.Phase): the values are the same words."""

    CONNECTING = 'connecting'  # the wake: no connection has served yet
    RECONNECTING = 'reconnecting'  # a connection that served was lost: awake, deaf until the next one
    IDLE = 'idle'
    LISTENING = 'listening'
    THINKING = 'thinking'
    SPEAKING = 'speaking'
    CLOSING = 'closing'


class Event(StrEnum):
    CONNECTED = 'connected'
    VAD_START = 'vad_start'
    VAD_END = 'vad_end'
    TRANSCRIPT_IN = 'transcript_in'
    USER_TEXT = 'user_text'
    INTERRUPTED = 'interrupted'
    AUDIO_OUT = 'audio_out'
    PLAYBACK_DRAINED = 'playback_drained'
    TURN_COMPLETE = 'turn_complete'
    INTERACTION_IDLE = 'interaction_idle'
    TOOL_STARTED = 'tool_started'
    TOOL_FINISHED = 'tool_finished'
    IDLE_TIMEOUT = 'idle_timeout'
    SLEEP_REQUESTED = 'sleep_requested'
    GO_AWAY = 'go_away'
    CONNECTION_LOST = 'connection_lost'


S, E = SessionState, Event

# Events that take any active state to the same place.
_TO = {
    E.VAD_START: S.LISTENING,
    E.INTERRUPTED: S.LISTENING,
    E.VAD_END: S.THINKING,
    E.USER_TEXT: S.THINKING,
    E.AUDIO_OUT: S.SPEAKING,
}
# Events that bring the machine to rest, from these states only: a turn ending never cuts audio still
# playing (SPEAKING) nor a user still talking (LISTENING). A tool finishing is not among them: the model
# has yet to react to its answer, so THINKING lasts until the reply ends or never comes (REPLY_TIMEOUT_S).
_RESTS_FROM = {
    E.PLAYBACK_DRAINED: (S.SPEAKING,),
    E.TURN_COMPLETE: (S.IDLE, S.THINKING),
    E.INTERACTION_IDLE: (S.IDLE, S.THINKING),
}


def transition(
    state: SessionState, event: Event, *, tool_pending: bool = False, answering: bool = False
) -> SessionState:
    """The next state. Total: an event that means nothing in a state leaves it unchanged.

    Two outside facts: a tool call still running (`tool_pending`) and the model's answer not over yet
    (`answering`: its turn is open, or it owes a reply to a tool answer or a say). Where the machine would come to
    rest, it rests in THINKING rather than IDLE while either holds: IDLE means the answer is over. An
    interruption that leaves an answer owed (a typed say's) is no barge-in: nobody spoke.
    """
    rest = S.THINKING if tool_pending or answering else S.IDLE
    if state is S.CLOSING or event in (E.SLEEP_REQUESTED, E.IDLE_TIMEOUT):
        return S.CLOSING
    if event is E.CONNECTION_LOST:
        return S.CONNECTING if state is S.CONNECTING else S.RECONNECTING
    if state in (S.CONNECTING, S.RECONNECTING):
        return rest if event is E.CONNECTED else state
    if event is E.INTERRUPTED and (state is S.THINKING or (answering and state is not S.LISTENING)):
        return S.THINKING  # the user's turn already ended (VAD end, a say still owed): only the speaker is flushed
    if event in _TO:
        return _TO[event]
    if event in _RESTS_FROM:
        return rest if state in _RESTS_FROM[event] else state
    if state is S.IDLE and event in (E.TRANSCRIPT_IN, E.TOOL_STARTED):
        return S.LISTENING if event is E.TRANSCRIPT_IN else S.THINKING
    return state  # connected, go_away, a late transcript, a tool starting mid-turn or finishing
