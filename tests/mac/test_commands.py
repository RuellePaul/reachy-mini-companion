"""The command queue: TTL on both clocks, latest wake/sleep wins, say in order, dead polls take nothing."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from fastapi.testclient import TestClient

from mac_server.commands import RESTART_WINDOW_S, Action, CommandQueue
from mac_server.contracts import COMMAND_TTL_S, TRY_TTL_S, SceneTimeline, TryMove
from mac_server.routes import ROBOT_OFF

KEY = {'X-API-Key': 'test-key'}


async def _gone() -> bool:
    return True


async def _here() -> bool:
    return False


def _actions(queue: CommandQueue) -> list[str]:
    """What is queued, without taking it (nor dropping what went stale: only a poll does)."""
    return [command.action for _, command in queue._pending]


@pytest.mark.parametrize('action', ['wake', 'sleep', 'say'])
def test_a_command_expires_when_either_clock_says_it_is_old(new_clock, action: Action) -> None:
    for advance_wall, advance_mono in ((COMMAND_TTL_S + 1, 0), (0, COMMAND_TTL_S + 1), (COMMAND_TTL_S, COMMAND_TTL_S)):
        wall, mono = new_clock(1_700_000_000.0), new_clock(50.0)
        queue = CommandQueue(wall=wall, mono=mono)
        queue.push(action, 'bonjour' if action == 'say' else None)
        wall.now += advance_wall
        mono.now += advance_mono
        expired = max(advance_wall, advance_mono) > COMMAND_TTL_S
        taken = asyncio.run(queue.next(0, _here))
        assert (taken is None) == expired, (advance_wall, advance_mono)
        assert queue._pending == []  # taken or dropped: a stale command never reaches a later poll


def test_a_command_lives_30_s(new_clock) -> None:
    """docs/CONTRACTS.md's 30 s, written out: the other tests take their numbers from COMMAND_TTL_S."""
    for age, delivered in ((30.0, True), (31.0, False)):
        wall, mono = new_clock(1_700_000_000.0), new_clock(50.0)
        queue = CommandQueue(wall=wall, mono=mono)
        queue.push('wake')
        wall.now += age
        mono.now += age
        assert (asyncio.run(queue.next(0, _here)) is not None) is delivered, age


def test_the_latest_wake_or_sleep_wins_and_say_keeps_its_order() -> None:
    queue = CommandQueue()
    queue.push('wake')
    queue.push('say', 'un')
    queue.push('sleep')
    queue.push('say', 'deux')
    assert _actions(queue) == ['say', 'sleep', 'say']
    queue.push('wake')
    assert [(c.action, c.text) for _, c in queue._pending] == [('say', 'un'), ('say', 'deux'), ('wake', None)]
    taken = [asyncio.run(queue.next(0, _here)) for _ in range(4)]
    assert [(c.action, c.text) if c else None for c in taken] == [('say', 'un'), ('say', 'deux'), ('wake', None), None]


def test_a_manette_renewal_keeps_the_humans_yes_the_robot_has_not_taken() -> None:
    """The latest wins, but the dashboard's first renewal may come before the robot's poll took the grant."""
    queue = CommandQueue()
    queue.push('manette', manette='volant', grant=True)
    queue.push('manette', manette='volant')  # renewed before the robot polled
    assert [(c.manette, c.grant) for _, c in queue._pending] == [('volant', True)]
    taken = asyncio.run(queue.next(0, _here))
    assert taken is not None and (taken.manette, taken.grant) == ('volant', True)
    queue.push('manette', manette='volant')  # once taken, a renewal only renews
    assert [(c.manette, c.grant) for _, c in queue._pending] == [('volant', False)]
    queue.push('manette', manette='tete', grant=True)
    queue.push('manette', manette='gachettes')  # another profile's renewal inherits nothing
    assert [(c.manette, c.grant) for _, c in queue._pending] == [('gachettes', False)]
    queue.push('manette', manette='tete', grant=True)
    queue.push('manette', manette='off')  # let go before the robot polled
    assert [(c.manette, c.grant) for _, c in queue._pending] == [('off', False)]


def test_a_scene_or_a_stop_replaces_the_one_the_robot_has_not_taken() -> None:
    """« Jouer » then « Arrêter » before the robot's poll: the scene never starts; a scene played again (a jump to
    another line) replaces the first, never plays after it. A say and a try are not of the group: they stay."""

    def scene(scene_id: str) -> SceneTimeline:
        return SceneTimeline(scene_id=scene_id, title=scene_id, duration_ms=0, cues=[])

    def pending(queue: CommandQueue) -> list[tuple[str, str | None]]:
        return [(c.action, c.scene.scene_id if c.scene else None) for _, c in queue._pending]

    queue = CommandQueue()
    queue.push('say', 'bonjour')
    queue.push('scene', scene=scene('a'))
    queue.push('try', move=TryMove(kind='gesture', name='nod'))
    queue.push('scene_stop')
    assert pending(queue) == [('say', None), ('try', None), ('scene_stop', None)]
    queue.push('scene', scene=scene('b'))
    queue.push('scene', scene=scene('c'))
    assert pending(queue) == [('say', None), ('try', None), ('scene', 'c')]
    taken = [asyncio.run(queue.next(0, _here)) for _ in range(4)]
    assert [c.action if c else None for c in taken] == ['say', 'try', 'scene', None]


def test_a_poll_whose_client_left_takes_nothing() -> None:
    queue = CommandQueue()
    queue.push('wake')
    assert asyncio.run(queue.next(5, _gone)) is None
    assert _actions(queue) == ['wake']


def test_a_newer_poll_sends_an_older_one_away_empty() -> None:
    """One robot: its newest poll is the live one. An older one still parked may sit on a socket gone silent (no FIN,
    which `gone()` cannot see): it leaves at once with nothing, and the next command goes to the live poll."""

    async def scenario() -> tuple[bool, object, str | None]:
        queue = CommandQueue()
        silent = asyncio.create_task(queue.next(5, _here))
        await asyncio.sleep(0.05)
        live = asyncio.create_task(queue.next(5, _here))
        await asyncio.sleep(0.05)
        left = silent.done()
        queue.push('wake')
        command = await live
        return left, await silent, command.action if command else None

    assert asyncio.run(scenario()) == (True, None, 'wake')


def test_a_poll_checking_its_socket_leaves_the_command_to_a_newer_one() -> None:
    """`gone()` awaits (Request.is_disconnected): a newer poll may begin meanwhile. It is the live one, and the
    command is its own, even when the older poll's check ends first."""

    def socket_check() -> tuple[Callable[[], Awaitable[bool]], asyncio.Event, asyncio.Event]:
        inside, answered = asyncio.Event(), asyncio.Event()

        async def gone() -> bool:
            inside.set()
            await answered.wait()
            return False

        return gone, inside, answered

    async def scenario() -> tuple[object, str | None]:
        queue = CommandQueue()
        queue.push('wake')
        older_gone, older_inside, older_answered = socket_check()
        newer_gone, newer_inside, newer_answered = socket_check()
        older = asyncio.create_task(queue.next(5, older_gone))
        await older_inside.wait()
        newer = asyncio.create_task(queue.next(1, newer_gone))
        await newer_inside.wait()
        older_answered.set()  # the older poll's check ends first, the wake still queued
        taken = await older
        newer_answered.set()
        command = await newer
        return taken, command.action if command else None

    assert asyncio.run(scenario()) == (None, 'wake')


def test_a_waiting_poll_wakes_on_push_and_times_out_empty() -> None:
    async def scenario() -> tuple[str | None, float, object]:
        queue = CommandQueue()
        started = time.monotonic()
        poll = asyncio.create_task(queue.next(5, _here))
        await asyncio.sleep(0.05)
        queue.push('sleep')
        command = await poll
        return command.action if command else None, time.monotonic() - started, await queue.next(0.05, _here)

    action, waited, empty = asyncio.run(scenario())
    assert action == 'sleep'
    assert waited < 1
    assert empty is None


def test_restart_is_a_sleep_then_a_wake_once_the_session_has_ended(new_clock) -> None:
    mono = new_clock()
    queue = CommandQueue(mono=mono)
    queue.restart(awake=True)
    assert _actions(queue) == ['sleep']
    queue.session_ended()
    assert _actions(queue) == ['wake']  # the sleep had not been taken yet: the wake replaces it
    queue.session_ended()
    assert _actions(queue) == ['wake']

    asleep = CommandQueue()
    asleep.restart(awake=False)
    assert _actions(asleep) == ['wake']

    late = CommandQueue(mono=mono)
    late.restart(awake=True)
    asyncio.run(late.next(0, _here))
    mono.now += RESTART_WINDOW_S + 1
    late.session_ended()
    assert _actions(late) == []

    overridden = CommandQueue()
    overridden.restart(awake=True)
    overridden.push('sleep')  # the user changed their mind: no wake follows
    overridden.session_ended()
    assert _actions(overridden) == ['sleep']


def test_restart_over_http_follows_the_robot(client: TestClient) -> None:
    def poll() -> str | None:
        command = client.get('/api/robot/commands', params={'wait': 0}).json()['command']
        return command and command['action']

    client.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's1'})
    assert client.post('/api/session/restart').status_code == 204
    assert poll() == 'sleep'
    assert poll() is None
    client.post('/api/robot/events', json={'kind': 'session_end', 'session_id': 's1', 'reason': 'dashboard'})
    assert poll() == 'wake'

    client.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's2'})  # awake again
    client.post('/api/session/restart')
    assert poll() == 'sleep'
    client.post('/api/robot/heartbeat', json={'awake': False})  # asleep, whether or not session_end arrived
    assert poll() == 'wake'

    client.post('/api/session/restart')  # asleep now: a plain wake
    assert poll() == 'wake'


def test_say_needs_a_conversation(client: TestClient) -> None:
    refused = client.post('/api/session/say', json={'text': 'bonjour'})
    assert refused.status_code == 409
    assert refused.json() == {'detail': "Le robot n'est pas en conversation."}
    client.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's1'})
    assert client.post('/api/session/say', json={'text': 'bonjour'}).status_code == 204
    command = client.get('/api/robot/commands', params={'wait': 0}).json()['command']
    assert (command['action'], command['text']) == ('say', 'bonjour')
    assert client.post('/api/session/say', json={'text': ''}).status_code == 422


@pytest.mark.parametrize('ending', ['closing', 'session_end'])
def test_say_is_refused_once_the_robot_reported_the_conversation_ending(client: TestClient, ending: str) -> None:
    """The robot drops a typed turn while it falls asleep: refused here, the dashboard keeps the text."""
    client.post('/api/robot/heartbeat', json={'awake': False})
    client.post('/api/robot/events', json={'kind': 'session_start', 'session_id': 's1'})
    assert client.post('/api/session/say', json={'text': 'bonjour'}).status_code == 204  # no wait for a heartbeat
    client.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's1'})
    end = {'kind': 'phase', 'phase': 'closing'} if ending == 'closing' else {'kind': 'session_end', 'reason': 'idle'}
    client.post('/api/robot/events', json=end | {'session_id': 's1'})
    client.post('/api/robot/heartbeat', json={'awake': True, 'session_id': 's1'})  # beats lag the robot by up to 3 s
    refused = client.post('/api/session/say', json={'text': 'encore une chose'})
    assert (refused.status_code, refused.json()) == (409, {'detail': "Le robot n'est pas en conversation."})
    taken = [client.get('/api/robot/commands', params={'wait': 0}).json()['command'] for _ in range(2)]
    assert [c and c['text'] for c in taken] == ['bonjour', None]


def test_wake_and_sleep_routes_and_the_poll_bound(client: TestClient) -> None:
    refused = client.post('/api/session/wake')  # no robot to take it: said at once, nothing queued
    assert (refused.status_code, refused.json()) == (409, {'detail': ROBOT_OFF})
    assert client.get('/api/robot/commands', params={'wait': 0}).json()['command'] is None
    client.post('/api/robot/heartbeat', json={'awake': False})
    assert client.post('/api/session/wake').status_code == 204
    assert client.post('/api/session/sleep').status_code == 204
    command = client.get('/api/robot/commands', params={'wait': 0}).json()['command']
    assert command['action'] == 'sleep'
    assert abs(command['queued_at'] - time.time()) < 5
    assert client.get('/api/robot/commands', params={'wait': 26}).status_code == 422


def test_a_dead_long_poll_takes_nothing_under_uvicorn(live_server: str) -> None:
    """Real uvicorn, real sockets: the disconnect reaches `request.is_disconnected()`."""
    host, port = live_server.removeprefix('http://').split(':')
    request = f'GET /api/robot/commands?wait=10 HTTP/1.1\r\nHost: mac\r\nX-API-Key: {KEY["X-API-Key"]}\r\n\r\n'
    with socket.create_connection((host, int(port))) as dead:
        dead.sendall(request.encode())
        time.sleep(0.3)  # the poll is parked, waiting
    time.sleep(0.2)
    requests.post(f'{live_server}/api/robot/heartbeat', json={'awake': False}, headers=KEY, timeout=5)
    assert requests.post(f'{live_server}/api/session/wake', headers=KEY, timeout=5).status_code == 204
    time.sleep(0.3)  # the dead poll wakes for the command and must leave it
    alive = requests.get(f'{live_server}/api/robot/commands', params={'wait': 0}, headers=KEY, timeout=5)
    assert alive.json()['command']['action'] == 'wake'


def test_a_poll_gone_without_a_fin_leaves_the_command_to_the_live_poll(live_server: str) -> None:
    """Real uvicorn: a robot gone without a FIN (unplugged, or the Mac asleep through its retries) leaves a poll
    `is_disconnected()` cannot see. The robot's next poll sends it away empty, and the wake reaches the robot."""
    host, port = live_server.removeprefix('http://').split(':')
    request = f'GET /api/robot/commands?wait=6 HTTP/1.1\r\nHost: mac\r\nX-API-Key: {KEY["X-API-Key"]}\r\n\r\n'
    url = f'{live_server}/api/robot/commands'
    with socket.create_connection((host, int(port))) as silent, ThreadPoolExecutor(1) as pool:
        silent.sendall(request.encode())  # then never read nor closed while the wake is sent
        time.sleep(0.3)
        live = pool.submit(requests.get, url, params={'wait': 3}, headers=KEY, timeout=10)
        time.sleep(0.3)
        requests.post(f'{live_server}/api/robot/heartbeat', json={'awake': False}, headers=KEY, timeout=5)
        assert requests.post(f'{live_server}/api/session/wake', headers=KEY, timeout=5).status_code == 204
        assert live.result().json()['command']['action'] == 'wake'
        silent.settimeout(5)
        assert silent.recv(4096).endswith(b'{"command":null}')


def test_a_try_expires_sooner_and_the_latest_try_wins(new_clock) -> None:
    wall, mono = new_clock(1_700_000_000.0), new_clock(50.0)
    queue = CommandQueue(wall=wall, mono=mono)
    queue.push('say', text='Bonjour')
    queue.push('try', move=TryMove(kind='gesture', name='nod'))
    queue.push('try', move=TryMove(kind='gesture', name='bow'))
    pending = [c for _, c in queue._pending]
    assert [(c.action, c.move.name if c.move else c.text) for c in pending] == [('say', 'Bonjour'), ('try', 'bow')]
    assert pending[1].ttl_s == TRY_TTL_S < COMMAND_TTL_S
    wall.now += TRY_TTL_S + 0.1
    mono.now += TRY_TTL_S + 0.1
    queue._drop_stale()
    assert [c.action for _, c in queue._pending] == ['say']  # the try is stale, the say is not
