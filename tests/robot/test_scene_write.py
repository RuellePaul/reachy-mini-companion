"""A scene asked for by voice: the tool that asks the Mac, and the watch that waits for it, has the model announce it,
then has the Mac play it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robot_agent import apps, scene
from robot_agent.mac import MacError
from robot_agent.state import SessionState
from robot_agent.tools import SILENT
from tests.robot.conftest import FakeMac, until

BUILDING = {'id': 'ab' * 8, 'status': 'building', 'title': None, 'scene_id': None, 'error': None}
READY = {**BUILDING, 'status': 'ready', 'title': 'Les deux fentes', 'scene_id': 'deux-fentes'}
WRITING = "J'écris la scène : une à trois minutes."


class Talk:
    """The session, as the watch sees it: its state, and the notes it is given (each one answered as `answers` says:
    the model speaks for `speaks` seconds, or says nothing)."""

    def __init__(self, speaks: float | None = 0.05) -> None:
        self.state = SessionState.IDLE
        self.notes: list[str] = []
        self.speaks = speaks
        self.spoke = asyncio.Event()

    def notify(self, text: str) -> asyncio.Task[None]:
        self.notes.append(text)

        async def sent() -> None:
            await asyncio.sleep(0.01)  # the next quiet point
            if self.speaks is not None:
                asyncio.get_running_loop().create_task(self._answer())

        return asyncio.create_task(sent())

    async def _answer(self) -> None:
        self.state = SessionState.SPEAKING
        await asyncio.sleep(self.speaks or 0.0)
        self.state = SessionState.IDLE
        self.spoke.set()


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps, 'BUILD_POLL_S', 0.01)
    monkeypatch.setattr(scene, 'SETTLE_S', 0.01)
    monkeypatch.setattr(scene, 'ANNOUNCE_S', 0.2)


def _tool(mac: FakeMac, writes: scene.Writes) -> Any:
    return scene.tools(mac, performer=None, session=lambda: None, writes=writes)['write_scene']  # type: ignore[arg-type]


def test_the_tool_asks_the_mac_for_the_scene_and_the_conversation_waits_for_it() -> None:
    async def scenario() -> None:
        mac = FakeMac({'/api/scenes/voice': {'message': WRITING, 'build': BUILDING}})
        writes = scene.Writes(mac, lambda: None)
        declaration, run = _tool(mac, writes)
        assert declaration.name == 'write_scene' and set(declaration.parameters.required) == {'request'}
        assert 'write_scene' not in scene.tools(mac, performer=None, session=lambda: None)  # type: ignore[arg-type]
        nothing = await run({'request': '   '}, None)
        assert nothing.message.startswith('Demande ce que la scène doit raconter') and mac.calls == []
        result = await run({'request': ' Deux fentes   qui débattent '}, None)
        assert (result.status, result.message) == ('ok', WRITING)
        assert mac.calls == [('POST', '/api/scenes/voice', {'request': 'Deux fentes qui débattent'})]
        assert writes.pending()  # no idle sleep while it is written
        writes.clear()
        assert not writes.pending()
        refused = FakeMac({'/api/scenes/voice': {'message': 'Une scène est déjà en cours. Dis-le simplement.'}})
        busy = scene.Writes(refused, lambda: None)
        assert (await _tool(refused, busy)[1]({'request': 'Une autre'}, None)).message.startswith('Une scène est déjà')
        assert not busy.pending()  # nothing was started: nothing to wait for
        down = FakeMac(error='Le Mac ne répond pas.')
        failed = await _tool(down, scene.Writes(down, lambda: None))[1]({'request': 'Une'}, None)
        assert (failed.status, failed.message) == ('error', 'Le Mac ne répond pas.')

    asyncio.run(scenario())


def test_one_ask_sent_as_identical_calls_writes_one_scene() -> None:
    async def scenario() -> None:
        mac = FakeMac({'/api/scenes/voice': {'message': WRITING, 'build': BUILDING}}, delay=0.05)
        writes = scene.Writes(mac, lambda: None)
        _, run = _tool(mac, writes)
        first, repeat = await asyncio.gather(run({'request': 'Une scène'}, None), run({'request': 'Une scène'}, None))
        assert len(mac.calls) == 1 and first.message == WRITING
        assert repeat.response('id', 'write_scene').scheduling == SILENT
        writes.clear()

    asyncio.run(scenario())


def test_a_scene_ready_is_announced_by_the_model_then_played_by_the_mac() -> None:
    async def scenario() -> None:
        mac = FakeMac({'/api/scenes/builds': {'builds': [BUILDING]}})
        talk = Talk(speaks=0.1)
        writes = scene.Writes(mac, lambda: talk)
        writes.watch(BUILDING)
        writes.watch(BUILDING)  # asked twice, watched once
        await asyncio.sleep(0.05)
        assert writes.pending() and talk.notes == []
        mac.answers['/api/scenes/builds'] = {'builds': [READY]}
        await until(lambda: talk.notes)
        assert talk.notes == [
            'La scène « Les deux fentes » est prête. Annonce-la en une phrase : elle commence juste après.'
        ]
        played = ('POST', '/api/scenes/deux-fentes/play', None)
        assert played not in mac.calls  # not before the model has said its sentence
        await until(lambda: played in mac.calls)
        assert talk.spoke.is_set() and talk.state is SessionState.IDLE
        await until(lambda: not writes.pending())
        assert mac.calls.count(played) == 1 and len(talk.notes) == 1

    asyncio.run(scenario())


def test_a_model_that_says_nothing_does_not_hold_the_scene_back() -> None:
    async def scenario() -> None:
        mac = FakeMac({'/api/scenes/builds': {'builds': [READY]}})
        talk = Talk(speaks=None)
        writes = scene.Writes(mac, lambda: talk)
        writes.watch(BUILDING)
        await until(lambda: ('POST', '/api/scenes/deux-fentes/play', None) in mac.calls)
        assert len(talk.notes) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ('ended', 'note'),
    [
        (
            {'status': 'failed', 'error': 'Gemini refuse cette réplique.'},
            "La scène demandée n'a pas pu être préparée : Gemini refuse cette réplique. Dis-le simplement.",
        ),
        ({'status': 'cancelled'}, None),  # cancelled on the dashboard: nothing to say
        (None, None),  # gone from the Mac's list: no longer waited for
    ],
)
def test_a_scene_that_did_not_come_is_said_and_never_played(ended: dict | None, note: str | None) -> None:
    async def scenario() -> None:
        listed = [] if ended is None else [{**BUILDING, **ended}]
        mac = FakeMac({'/api/scenes/builds': {'builds': listed}})
        talk = Talk()
        writes = scene.Writes(mac, lambda: talk)
        writes.watch(BUILDING)
        await until(lambda: not writes.pending())
        assert talk.notes == ([note] if note else [])
        assert all(path == '/api/scenes/builds' for _, path, _ in mac.calls)

    asyncio.run(scenario())


def test_a_play_the_mac_refuses_is_said_and_a_conversation_over_is_left_alone() -> None:
    class Refusing(FakeMac):
        def request(self, method: str, path: str, body: dict | None = None) -> dict:
            if path.endswith('/play'):
                self.calls.append((method, path, body))
                raise MacError('Le robot est éteint.')
            return super().request(method, path, body)

    async def scenario() -> None:
        mac = Refusing({'/api/scenes/builds': {'builds': [READY]}})
        talk = Talk()
        writes = scene.Writes(mac, lambda: talk)
        writes.watch(BUILDING)
        await until(lambda: len(talk.notes) == 2)
        assert (
            talk.notes[1] == "La scène est écrite mais n'a pas pu être jouée : Le robot est éteint. Dis-le simplement."
        )
        await until(lambda: not writes.pending())

        gone = FakeMac({'/api/scenes/builds': {'builds': [READY]}})
        asleep = scene.Writes(gone, lambda: None)  # the conversation ended while it was written
        asleep.watch(BUILDING)
        await until(lambda: not asleep.pending())
        assert all(path == '/api/scenes/builds' for _, path, _ in gone.calls)  # written, it stays on the Mac

    asyncio.run(scenario())
