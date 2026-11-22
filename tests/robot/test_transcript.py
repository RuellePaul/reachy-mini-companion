"""The dashboard's transcript: one line per turn, a late chunk included, and the latency of each reply."""

from __future__ import annotations

from robot_agent.transcript import Transcript


def test_a_turn_with_a_late_chunk_stays_one_line_and_the_latency_is_measured_once() -> None:
    events: list[dict] = []
    transcript = Transcript(events.append)
    transcript.heard('Quelle heure ')
    transcript.user_done(at=10.0)
    transcript.audio(now=10.6)
    transcript.heard('est-il ?')  # the recogniser finishing after the robot started
    transcript.said('Il est midi.')
    transcript.audio(now=10.8)
    transcript.close()
    kinds = [(e['kind'], e.get('text')) for e in events]
    assert kinds == [
        ('user_delta', 'Quelle heure '),
        ('latency', None),
        ('user_delta', 'est-il ?'),
        ('robot_delta', 'Il est midi.'),
        ('user_end', 'Quelle heure est-il ?'),
        ('robot_end', 'Il est midi.'),
    ]
    user_ids = {e['turn_id'] for e in events if e['kind'].startswith('user')}
    robot_ids = {e['turn_id'] for e in events if e['kind'] in ('robot_delta', 'robot_end', 'latency')}
    assert len(user_ids) == 1 and len(robot_ids) == 1
    assert events[1]['ms'] == 600


def test_typing_over_a_streaming_answer_measures_the_next_answer_only() -> None:
    events: list[dict] = []
    transcript = Transcript(events.append)
    transcript.audio(now=1.0)  # an answer streaming
    transcript.typed('Stop, autre chose !', at=2.0)
    transcript.audio(now=2.02)  # the rest of that answer, before the server's `interrupted`
    assert [e['kind'] for e in events] == ['user_end']  # no latency: it is no reply to the typed turn
    transcript.interrupted()
    transcript.audio(now=2.9)  # the reply
    latency = [e for e in events if e['kind'] == 'latency']
    assert [e['ms'] for e in latency] == [900]


def test_a_turn_left_unanswered_times_no_later_audio() -> None:
    events: list[dict] = []
    transcript = Transcript(events.append)
    transcript.heard('Pas pour toi.')
    transcript.user_done(at=1.0)
    transcript.unanswered()  # no reply came, or the model declined
    transcript.audio(now=181.0)  # minutes later, the robot answers a note
    kinds = [(e['kind'], e.get('text')) for e in events]
    assert kinds == [('user_delta', 'Pas pour toi.'), ('user_end', 'Pas pour toi.')]


def test_a_typed_turn_and_an_interrupted_answer() -> None:
    events: list[dict] = []
    transcript = Transcript(events.append)
    transcript.typed('Raconte une histoire.', at=1.0)
    transcript.said('Il était une fois')
    transcript.interrupted()
    transcript.close()  # nothing left open
    assert [(e['kind'], e.get('typed')) for e in events] == [
        ('user_end', True),
        ('robot_delta', None),
        ('robot_end', None),
    ]


def test_markup_never_reaches_the_transcript_and_a_marked_up_rendition_is_dropped() -> None:
    """The exact chunks Gemini sent after a `look`: the text, then the same text again in marked-up segments."""
    events: list[dict] = []
    transcript = Transcript(events.append)
    plain = "Je te vois devant ton ordinateur avec ton clavier et ta souris. Il y a l'air d'avoir une bonne concentration par ici."
    transcript.said(plain)
    transcript.said('<speaker:0><audio:player src="audio-1.1.20-811c-1.webm"/>Je te vois devant ton ordinateur avec')
    transcript.said('<speaker:0><audio:player src="audio-1.1.20-811c-2.webm"/>ton clavier et ta souris.')
    transcript.close()
    assert [e['text'] for e in events if e['kind'] == 'robot_delta'] == [plain]
    assert [e['text'] for e in events if e['kind'] == 'robot_end'] == [plain]


def test_a_marked_up_rendition_alone_is_kept_without_its_markup() -> None:
    events: list[dict] = []
    transcript = Transcript(events.append)
    transcript.said('<speaker:0><audio:player src="a-1.webm"/>Je te vois devant ton ordinateur avec')
    transcript.said('<speaker:0><audio:player src="a-2.webm"/>ton clavier.')
    transcript.close()
    transcript.said('Ensuite.')  # the next turn starts afresh
    transcript.close()
    ends = [e['text'] for e in events if e['kind'] == 'robot_end']
    assert ends == ['Je te vois devant ton ordinateur avec ton clavier.', 'Ensuite.']
    assert all('<' not in e['text'] for e in events if e['kind'] == 'robot_delta')
