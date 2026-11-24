"""The robot silent while an app people play among themselves is on screen, until its speech is given back."""

from __future__ import annotations

from typing import Any

from robot_agent.hush import Hush


def test_the_hush_waits_for_the_answer_to_end_and_says_each_change_once() -> None:
    said: list[dict[str, Any]] = []
    answering = True
    hush = Hush(said.append, ready=lambda: not answering)
    hush.want(True)
    assert not hush.active() and said == []  # its last words are not cut
    answering = False
    assert hush.active() and hush.active() and said == [{'kind': 'hush', 'on': True}]
    hush.off('antenna')
    hush.off('say')  # already speaking: nothing more to say
    assert not hush.active() and said[1:] == [{'kind': 'hush', 'on': False, 'why': 'antenna'}]
    hush.want(True)
    hush.want(False)  # the app left before the robot fell silent: nothing changed
    answering = True
    assert not hush.active() and len(said) == 2
    hush.want(True)
    answering = False
    assert hush.active()
    hush.reset()  # the conversation is over: the next wake speaks
    assert not hush.active() and len(said) == 3
