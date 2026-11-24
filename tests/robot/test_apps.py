"""Apps on the robot: the `app` tool, and the moves an app's bridge asks for (page-centric look, the robot's own
antennas), played only in conversation."""

from __future__ import annotations

import asyncio
import math
import threading
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from robot_agent import animations, apps
from robot_agent.tools import SILENT
from tests.robot.conftest import FakeEmotions, FakeMac, settings_view, until
from tests.robot.test_main import _robot, robot_parts  # noqa: F401 — the fixture, used by name


def head_angles(move: Any) -> tuple[float, float]:
    """(pitch, yaw) in degrees of a move's head: +pitch looks down, +yaw turns to the robot's left."""
    _, pitch, yaw = Rotation.from_matrix(move.evaluate(0.0)[0][:3, :3]).as_euler('xyz', degrees=True)
    return float(pitch), float(yaw)


def test_a_look_at_the_pages_left_turns_the_robot_to_its_right() -> None:
    """Facing the user, the page's left is the robot's right: a negative yaw; the page's top: looking up."""
    pitch, yaw = head_angles(apps.move({'kind': 'look', 'yaw': -30, 'pitch': 0}, FakeEmotions()))
    assert yaw == pytest.approx(-30) and pitch == pytest.approx(0, abs=1e-9)
    pitch, yaw = head_angles(apps.move({'kind': 'look', 'yaw': 20, 'pitch': 20}, FakeEmotions()))
    assert yaw == pytest.approx(20) and pitch == pytest.approx(-20)
    pitch, yaw = head_angles(apps.move({'kind': 'look', 'yaw': 99, 'pitch': -99}, FakeEmotions()))
    assert (yaw, pitch) == (pytest.approx(30), pytest.approx(20))  # clamped to the contract's range
    assert apps.move({'kind': 'look', 'yaw': -30, 'pitch': 0}, FakeEmotions()).duration == apps.HOLD_S


def test_antennas_are_the_robots_own_and_positive_is_forward() -> None:
    hold = apps.move({'kind': 'antennas', 'left': 30, 'right': -10}, FakeEmotions())
    head, antennas = hold.evaluate(0.0)
    right, left = antennas
    assert right == pytest.approx(animations.NEUTRAL_R + np.radians(-10))
    assert left == pytest.approx(animations.NEUTRAL_L - np.radians(30))  # mirrored joint: forward is minus
    assert np.allclose(head, animations.pose())
    assert np.allclose(
        apps.move({'kind': 'antennas', 'left': 90, 'right': 0}, FakeEmotions()).evaluate(0)[1][1],
        animations.NEUTRAL_L - np.radians(60),
    )


def test_emotions_gestures_and_nonsense() -> None:
    emotions = FakeEmotions()
    assert apps.move({'kind': 'emotion', 'name': 'cheerful1'}, emotions).name == 'cheerful1'
    assert apps.move({'kind': 'gesture', 'name': 'nod'}, emotions) is animations.GESTURES['nod']
    for motion in (
        {'kind': 'emotion', 'name': 'furious1'},
        {'kind': 'gesture', 'name': 'moonwalk'},
        {'kind': 'look', 'yaw': 'left'},
        {'kind': 'antennas'},
        {'kind': 'say', 'text': 'bonjour'},
        {},
    ):
        assert apps.move(motion, emotions) is None, motion


BUILDING = {'id': 'ab' * 8, 'title': 'Snake', 'status': 'building', 'error': None}


def test_the_app_tool_asks_the_mac_and_says_its_answer() -> None:
    async def scenario() -> None:
        answer = {'message': "Je construis l'app : elle est sur le dashboard.", 'app': BUILDING}
        mac = FakeMac({'/api/apps/voice': answer})
        builds = apps.Builds(mac, notify=lambda _text: None)
        declaration, run = apps.tools(mac, builds)['app']
        assert declaration.name == 'app' and set(declaration.parameters.required) == {'action'}
        result = await run({'action': 'create', 'request': 'un Snake'}, None)
        assert (result.status, result.message) == ('ok', answer['message'])
        assert mac.calls == [('POST', '/api/apps/voice', {'action': 'create', 'request': 'un Snake', 'name': ''})]
        assert builds.pending()  # the conversation waits for it
        builds.clear()
        assert not builds.pending()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ('app', 'note'),
    [
        ({'status': 'ready', 'error': None}, "L'app « Snake » est prête, sur le dashboard."),  # shown, or offered
        ({'status': 'failed', 'error': 'Claude a échoué.'}, "L'app « Snake » n'a pas pu être construite : Claude a"),
        ({'status': 'ready', 'error': 'Claude a échoué.'}, 'La modification de « Snake » a échoué : Claude a échoué.'),
    ],
)
def test_a_build_asked_for_is_waited_for_then_told(monkeypatch: pytest.MonkeyPatch, app: dict, note: str) -> None:
    monkeypatch.setattr(apps, 'BUILD_POLL_S', 0.01)

    async def scenario() -> None:
        mac = FakeMac({'/api/apps': {'apps': [BUILDING]}})
        told: list[str] = []
        builds = apps.Builds(mac, notify=told.append)
        builds.watch(BUILDING)
        builds.watch(BUILDING)  # asked twice, watched once
        await asyncio.sleep(0.05)
        assert builds.pending() and told == []
        mac.answers['/api/apps'] = {'apps': [{**BUILDING, **app}]}
        await until(lambda: told)
        assert told[0].startswith(note) and not builds.pending()
        assert len(told) == 1

    asyncio.run(scenario())


def test_a_build_deleted_or_ready_already_is_not_waited_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps, 'BUILD_POLL_S', 0.01)

    async def scenario() -> None:
        told: list[str] = []
        builds = apps.Builds(FakeMac({'/api/apps': {'apps': []}}), notify=told.append)
        builds.watch({**BUILDING, 'status': 'ready'})
        assert not builds.pending()
        builds.watch(BUILDING)
        await until(lambda: not builds.pending())
        assert told == []  # deleted meanwhile: nothing to say

    asyncio.run(scenario())


def test_a_mac_that_stays_unreachable_releases_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps, 'BUILD_POLL_S', 0.01)
    monkeypatch.setattr(apps, 'MAC_LOST_S', 0.05)

    async def scenario() -> None:
        builds = apps.Builds(FakeMac(error='Le Mac ne répond pas.'), notify=lambda _text: None)
        builds.watch(BUILDING)
        await asyncio.sleep(0.02)
        assert builds.pending()  # a short outage is waited out
        await until(lambda: not builds.pending())

    asyncio.run(scenario())


def test_the_app_tool_says_what_the_mac_refused() -> None:
    builds = apps.Builds(FakeMac(), notify=lambda _text: None)
    _, run = apps.tools(FakeMac(error='« Snake » est déjà en construction.'), builds)['app']
    result = asyncio.run(run({'action': 'update', 'name': 'Snake', 'request': 'plus vite'}, None))
    assert (result.status, result.message) == ('error', '« Snake » est déjà en construction.')


def test_one_ask_sent_as_identical_calls_builds_one_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """One ask can come as several identical calls: one build for them all, the repeat answered
    without a word, whatever the first one's outcome; another ask, or the same one later, reaches the Mac."""
    clock = [100.0]
    monkeypatch.setattr(apps.time, 'monotonic', lambda: clock[0])
    create = {'action': 'create', 'request': 'un Snake'}

    async def twice(mac: FakeMac) -> list[Any]:
        builds = apps.Builds(mac, notify=lambda _text: None)
        _, run = apps.tools(mac, builds)['app']
        answers = await asyncio.gather(run(dict(create), None), run(dict(create), None))
        answers.append(await run({'action': 'create', 'request': 'un Pong'}, None))
        clock[0] += 30.0
        answers.append(await run(dict(create), None))
        builds.clear()
        return answers

    mac = FakeMac({'/api/apps/voice': {'message': "« un Snake » s'écrit.", 'app': BUILDING}}, delay=0.05)
    first, repeat, other, later = asyncio.run(twice(mac))
    assert [body['request'] for _, path, body in mac.calls if path == '/api/apps/voice'] == [
        'un Snake',
        'un Pong',
        'un Snake',
    ], 'one ask, sent as two identical calls, reached the Mac twice'
    assert (first.status, first.message) == (later.status, later.message) == ('ok', "« un Snake » s'écrit.")
    assert repeat.status == 'ok' and repeat.response('id', 'app').scheduling == SILENT
    assert other.message == first.message  # the same fake answer: a POST of its own all the same
    failing = FakeMac(error='Le Mac ne répond pas.', delay=0.05)
    first, repeat, *_ = asyncio.run(twice(failing))
    assert len([c for c in failing.calls if c[1] == '/api/apps/voice']) == 3 and (
        first.status,
        repeat.response('id', 'app').scheduling,
    ) == ('error', SILENT)


def test_a_different_ask_is_not_held_behind_a_slow_mac() -> None:
    """Only an identical call waits for the first one: two different asks reach the Mac side by side, so the second
    is not held behind a slow or unreachable Mac."""
    both = threading.Barrier(2, timeout=2.0)  # each request goes on only once the other one is under way

    class SideBySide(FakeMac):
        def request(self, method: str, path: str, body: dict | None = None) -> dict:
            both.wait()
            return super().request(method, path, body)

    async def three(mac: FakeMac) -> list[Any]:
        builds = apps.Builds(mac, notify=lambda _text: None)
        _, run = apps.tools(mac, builds)['app']
        snake, morpion = {'action': 'create', 'request': 'un Snake'}, {'action': 'open', 'name': 'Morpion'}
        answers = await asyncio.gather(run(snake, None), run(dict(snake), None), run(morpion, None))
        builds.clear()
        return answers

    mac = SideBySide({'/api/apps/voice': {'message': "C'est noté."}})
    snake, repeat, morpion = asyncio.run(three(mac))
    assert (snake.status, morpion.status) == ('ok', 'ok') and repeat.response('id', 'app').scheduling == SILENT
    assert sorted(body['action'] for _, path, body in mac.calls if path == '/api/apps/voice') == ['create', 'open']


def test_an_app_moves_the_robot_only_in_conversation_and_never_over_a_scene(robot_parts) -> None:  # noqa: F811
    async def scenario() -> None:
        robot = _robot(robot_parts, settings_view())
        played: list[Any] = []
        robot._motion.play_move = played.append
        robot._command({'action': 'app_motion', 'motion': {'kind': 'gesture', 'name': 'nod'}})
        await asyncio.sleep(0.05)
        assert played == []  # asleep: nothing moves
        robot._session = SimpleNamespace()
        robot._performer.busy = True  # a scene plays: it refuses the model's tools, and an app's moves too
        robot._command({'action': 'app_motion', 'motion': {'kind': 'gesture', 'name': 'nod'}})
        await asyncio.sleep(0.05)
        assert played == []
        robot._performer.busy = False
        robot._command({'action': 'app_motion', 'motion': {'kind': 'gesture', 'name': 'nod'}})
        await until(lambda: played)
        assert played == [animations.GESTURES['nod']]

    asyncio.run(scenario())


def test_a_look_leans_the_head_as_the_pad_reads_its_roll() -> None:
    """The dashboard's pad reads the roll as atan2(r21, r22) (frontend/src/apps/pad.ts): a gesture read plays back."""
    head = apps.move({'kind': 'look', 'yaw': 10, 'pitch': 5, 'roll': 12}, FakeEmotions()).head
    assert math.degrees(math.atan2(head[2, 1], head[2, 2])) == pytest.approx(12, abs=0.01)
    far = apps.move({'kind': 'look', 'yaw': 0, 'pitch': 0, 'roll': -99}, FakeEmotions()).head
    assert math.degrees(math.atan2(far[2, 1], far[2, 2])) == pytest.approx(-20, abs=0.01)
    level = apps.move({'kind': 'look', 'yaw': 0, 'pitch': 0}, FakeEmotions()).head
    assert math.atan2(level[2, 1], level[2, 2]) == pytest.approx(0)


def test_a_look_then_antennas_keeps_the_look_and_the_reverse() -> None:
    """An app sends a look and an antenna pose in the same burst: each keeps what the other set."""
    look = apps.move({'kind': 'look', 'yaw': -20, 'pitch': 10}, emotions=None)
    both = apps.move({'kind': 'antennas', 'left': 30, 'right': -10}, emotions=None, previous=look)
    assert np.allclose(both.head, look.head)
    assert np.allclose(both.antennas, animations.antennas(right=-10, left=30))
    again = apps.move({'kind': 'look', 'yaw': 5, 'pitch': 0}, emotions=None, previous=both)
    assert np.allclose(again.antennas, both.antennas) and not np.allclose(again.head, both.head)
