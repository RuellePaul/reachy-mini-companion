"""Rehearsals on the Mac: the options of a play, and the timeline without the rehearsed voices."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mac_server.contracts import Heartbeat, SceneTimeline
from tests.mac.test_scenes import SHORT, FakeGemini, client, scenes_app, tts, until  # noqa: F401


def _scene(client: TestClient, app: FastAPI, tts: FakeGemini) -> str:  # noqa: F811
    """SHORT saved, its two voices rendered: NARRATEUR's « Il était une fois. », MARCO's « Ciao. »."""
    scene_id = client.post('/api/scenes', json={'title': 'Courte', 'text': SHORT}).json()['id']
    until(lambda: len(tts.calls) == 2 and not app.state.mac.scenes.voices._inflight)
    return scene_id


def _queued(client: TestClient) -> Any:  # noqa: F811
    return client.get('/api/robot/commands', params={'wait': 0}).json()['command']


def test_a_rehearsal_needs_no_voice_for_the_part_the_human_says(
    client: TestClient,  # noqa: F811
    scenes_app: FastAPI,  # noqa: F811
    tts: FakeGemini,  # noqa: F811
) -> None:
    mac = scenes_app.state.mac
    play = f'/api/scenes/{_scene(client, scenes_app, tts)}/play'
    marco = next(cue for cue in mac.scenes.check(SHORT).cues if cue.character == 'MARCO')
    sidecar = mac.scenes.voices.dir / f'{marco.key}.json'
    sidecar.unlink()  # MARCO's voice missing
    mac.health.beat(Heartbeat(awake=False))
    assert '(1/2)' in client.post(play).json()['detail']  # a play needs both voices, and renders the missing one
    until(lambda: len(tts.calls) == 3 and not mac.scenes.voices._inflight)
    sidecar.unlink()
    assert client.post(play, json={'rehearse': 'MARCO'}).status_code == 204
    timeline = SceneTimeline.model_validate(_queued(client)['scene'])
    assert (timeline.rehearse, timeline.moves) == ('MARCO', True)
    said = [(c.character, c.at_ms, c.duration_ms, c.key, c.text) for c in timeline.cues if c.kind == 'audio']
    assert said[1] == ('MARCO', 1700, 0, None, 'Ciao.')  # its text kept, no audio, its time the human's
    assert timeline.duration_ms == 1700 and len(tts.calls) == 3  # MARCO's voice was not rendered for it
    refused = client.post(play, json={'rehearse': 'NARRATEUR', 'moves': False})
    assert refused.status_code == 409 and '(0/1)' in refused.json()['detail']  # only MARCO's voice counts
    until(lambda: len(tts.calls) == 4 and not mac.scenes.voices._inflight)
    assert tts.calls[-1]['input'][0]['content'][0]['text'] == 'Ciao.'
    assert client.post(play, json={'rehearse': 'NARRATEUR', 'moves': False}).status_code == 204
    timeline = SceneTimeline.model_validate(_queued(client)['scene'])
    assert not timeline.moves and timeline.cues[0].key is None


def test_a_rehearsal_is_checked_before_it_is_queued(
    client: TestClient,  # noqa: F811
    scenes_app: FastAPI,  # noqa: F811
    tts: FakeGemini,  # noqa: F811
) -> None:
    mac = scenes_app.state.mac
    play = f'/api/scenes/{_scene(client, scenes_app, tts)}/play'
    mac.health.beat(Heartbeat(awake=False))
    unknown = client.post(play, json={'rehearse': 'PAOLO'})
    assert (unknown.status_code, unknown.json()) == (
        422,
        {'detail': "«\xa0PAOLO\xa0» n'est pas un personnage de «\xa0Courte\xa0»."},
    )
    assert _queued(client) is None
    assert client.post(play, json={'rehearse': 'MARCO'}).status_code == 204  # asleep: a tap needs no conversation
    assert _queued(client)['scene']['rehearse'] == 'MARCO'
    mac.health.beat(Heartbeat(awake=True, session_id='s1'))
    assert client.post(play, json={'rehearse': 'MARCO'}).status_code == 204
    assert _queued(client)['scene']['rehearse'] == 'MARCO'
    assert client.post(play, json={'cue': 'listen'}).status_code == 422  # no such option
