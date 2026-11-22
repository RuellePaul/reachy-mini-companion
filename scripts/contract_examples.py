"""Payloads the dashboard's Zod schemas are tested against, built from mac_server/contracts.py's models.

scripts/export_contract_examples.py writes them to frontend/tests/contract-examples.json; tests/test_contracts.py
fails while that file is stale.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from mac_server import catalog
from mac_server.contracts import (
    AppAction,
    AppBuild,
    AppEdit,
    AppEvent,
    AppHtml,
    AppList,
    AppSummary,
    CastMember,
    CatalogEntry,
    DashboardState,
    HushEvent,
    Latency,
    ManetteEvent,
    PhaseChange,
    PoseFrame,
    PreviewFrames,
    ReplayEvent,
    RobotDelta,
    RobotEnd,
    RobotStatusOut,
    SceneAction,
    SceneBuild,
    SceneBuilds,
    SceneCheck,
    SceneCue,
    SceneEmotions,
    SceneError,
    SceneEvent,
    SceneList,
    SceneOut,
    SceneRender,
    SceneSummary,
    SceneVocabulary,
    SceneWord,
    SessionEnd,
    SessionSnapshot,
    SessionStart,
    Settings,
    SettingsView,
    TokenCounts,
    ToolCall,
    ToolData,
    ToolResult,
    Track,
    Usage,
    UserDelta,
    UserEnd,
    Voice,
    Volume,
    WsAppBuild,
    WsApps,
    WsConversation,
    WsPose,
    WsRobotStatus,
    WsSceneBuild,
    WsSceneRender,
    WsSession,
    WsSettings,
    WsVoice,
    WsVolume,
    settings_options,
)


def examples() -> dict[str, list[dict[str, Any]]]:
    sid = 's-20261030-0001'
    settings = Settings()
    view = SettingsView(
        settings=settings,
        system_prompt='Tu es Reachy, un petit robot de bureau.',
        web_prompt='Tu peux chercher sur le web.',
        options=settings_options(),
    )
    tool_call = ToolCall(kind='tool_call', session_id=sid, call_id='c1', name='play_music', args={'query': 'Daft Punk'})
    events: list[BaseModel] = [
        SessionStart(kind='session_start', session_id=sid),
        UserDelta(kind='user_delta', session_id=sid, turn_id='u1', text='Mets de la '),
        UserEnd(kind='user_end', session_id=sid, turn_id='u1', text='Mets de la musique.', typed=False),
        PhaseChange(kind='phase', session_id=sid, phase='thinking'),
        tool_call,
        ToolResult(
            kind='tool_result',
            session_id=sid,
            call_id='c1',
            name='play_music',
            status='ok',
            message='Lecture lancée\xa0: One More Time, Daft Punk.',
            data=ToolData(
                track=Track(title='One More Time', artist='Daft Punk', uri='spotify:track:0DiWol3AO6WpXZgp0goxAV')
            ),
        ),
        ToolResult(
            kind='tool_result',
            session_id=sid,
            call_id='c2',
            name='look',
            status='ok',
            message='Voici ce que voit ta caméra.',
            data=ToolData(image_base64='/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP8='),
        ),
        ToolResult(
            kind='tool_result',
            session_id=sid,
            call_id='c3',
            name='music',
            status='error',
            message="Spotify ne répond pas : aucun appareil n'est disponible.",
        ),
        RobotDelta(kind='robot_delta', session_id=sid, turn_id='r1', text="C'est parti"),
        RobotEnd(kind='robot_end', session_id=sid, turn_id='r1', text="C'est parti !"),
        Latency(kind='latency', session_id=sid, turn_id='r1', ms=640),
        Usage(
            kind='usage',
            session_id=sid,
            prompt=TokenCounts(text=1800, audio=250),
            response=TokenCounts(audio=120),
            total_tokens=2170,
            cost_usd=0.0036,
            session_cost_usd=0.0125,
        ),
        SceneEvent(
            kind='scene', session_id=sid, scene_id='fete-surprise', title='La fête surprise', state='line', line=7
        ),
        SceneEvent(
            kind='scene',
            session_id=sid,
            scene_id='fete-surprise',
            title='La fête surprise',
            state='waiting',
            line=9,
            message='Surprise ! Joyeux anniversaire !',
        ),
        SceneEvent(
            kind='scene',
            session_id=sid,
            scene_id='fete-surprise',
            title='La fête surprise',
            state='stopped',
            message='Arrêtée depuis le tableau de bord.',
        ),
        ManetteEvent(kind='manette', session_id=sid, state='on', profile='volant'),
        ManetteEvent(kind='manette', session_id=sid, state='returning', profile='volant'),
        ManetteEvent(kind='manette', session_id=sid, state='off', why='lease'),
        ReplayEvent(kind='replay', session_id=sid, state='started', seconds=2.48, loops=4),
        ReplayEvent(kind='replay', session_id=sid, state='ended'),
        HushEvent(kind='hush', session_id=sid, on=True),
        HushEvent(kind='hush', session_id=sid, on=False, why='antenna'),
        SessionEnd(kind='session_end', session_id=sid, reason='goto_sleep'),
    ]
    rest = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    frames = PreviewFrames(hz=30, head=[rest, rest], antennas=[[-0.1745, 0.1745]] * 2, body=[0.0, 0.0])
    cues = [
        SceneCue(
            line=1,
            end_line=2,
            kind='say',
            character='NARRATEUR',
            text='Il était une fois…',
            voice='Charon',
            style='',
            key='k1',
        ),
        SceneCue(line=3, kind='emotion', name='cheerful1'),
        SceneCue(line=3, kind='pause', seconds=1.5),
        SceneCue(line=4, kind='turn', degrees=-35.0),
    ]
    check = SceneCheck(
        cast=[CastMember(name='NARRATEUR', voice='Charon', acting='conteur malicieux', angle=-60.0)],
        cues=cues,
        errors=[
            SceneError(
                line=8, message="[triste] n'est ni une émotion, ni un geste, ni une pause (veux-tu dire [sad1] ?)"
            )
        ],
        warnings=[SceneError(line=4, message='[tourne 160] : limité à 100°')],
        segments=1,
        rendered=0,
    )
    word = SceneWord(word='cheerful1', label='Joyeux', family='Joie')
    app = AppSummary(id='90f7b5fe0f1771f0', title='Snake', status='ready', created_at=1.79e9, updated_at=1.79e9)
    building = AppSummary(
        id='a1b2c3d4e5f60718', title='Morpion', status='building', created_at=1.79e9, updated_at=1.79e9
    )
    shipped = AppSummary(
        id='slalom', title='Slalom', status='ready', created_at=1.79e9, updated_at=1.79e9, builtin=True
    )
    awake = SessionSnapshot(awake=True, session_id=sid, phase='speaking', cost_usd=0.0125, last_latency_ms=640)
    asleep = SessionSnapshot(awake=False)
    moving_asleep = SessionSnapshot(awake=False, antenna_wake=False)  # a try or a scene played asleep
    on_stage = SessionSnapshot(awake=False, antenna_wake=False, scene='fete-surprise')  # a scene played asleep
    writing = SceneBuild(
        id='5e5510000a1b0c2d',
        request='Deux personnages débattent de la dualité onde-corpuscule',
        voiced=True,
        status='building',
        started_at=1793469318.5,
        write='running',
        phase='writing',
        thinking_tokens=1240,
        text='@GAUCHE (Kore, 35) : physicienne pointilleuse\n\nGAUCHE : Il est passé par moi.',
    )
    staged = writing.model_copy(
        update={
            'status': 'ready', 'ended_at': 1793469402.1, 'scene_id': 'deux-fentes', 'title': 'Les deux fentes',
            'write': 'done', 'stage': 'done', 'voice': 'done', 'moves': 12, 'voices_done': 19, 'voices_total': 19,
            'text': '@GAUCHE (Kore, 35) : physicienne pointilleuse\n\nGAUCHE : [curious1] Il est passé par moi.',
            'cost_usd': 0.1312,
        }
    )  # fmt: skip
    refused = writing.model_copy(
        update={
            'status': 'failed', 'ended_at': 1793469350.0, 'write': 'failed', 'round': 2, 'voiced': False,
            'problems': ['ligne 1\xa0: voix inconnue\xa0: «\xa0Personne\xa0»'],
            'error': 'La scène écrite ne peut pas se jouer\xa0: ligne 1\xa0: voix inconnue\xa0: «\xa0Personne\xa0».',
        }
    )  # fmt: skip
    messages: list[BaseModel] = [
        WsConversation(channel='conversation', data=tool_call),
        WsSession(channel='session', data=awake),
        WsRobotStatus(channel='robot_status', data=RobotStatusOut(status='live')),
        WsSettings(channel='settings', data=view),
        WsVolume(channel='volume', data=Volume(volume=70)),
        WsPose(channel='pose', data=PoseFrame(t=1793469318504.6, j=[0.0] * 7, m=rest, a=[-0.1745, 0.1745])),
        WsVoice(channel='voice', data=Voice(level=0.42)),
        WsSceneRender(channel='scene_render', data=SceneRender(scene_id='fete-surprise', done=7, total=13)),
        WsSceneBuild(channel='scene_build', data=writing),
        WsApps(channel='apps', data=AppEvent(app=building, open=True)),
        WsAppBuild(
            channel='app_build',
            data=AppBuild(
                app_id='a1b2c3d4e5f60718',
                kind='create',
                phase='writing',
                started_at=1.79e9,
                thinking_tokens=10400,
                chars=6120,
                tail='  <button id="jouer">Jouer</button>\n</main>\n<script>',
            ),
        ),
        WsAppBuild(
            channel='app_build',
            data=AppBuild(
                app_id='a1b2c3d4e5f60718',
                kind='update',
                phase='editing',
                round=2,
                started_at=1.79e9,
                edits=[AppEdit(old='--serpent: #3BD16F;', new='--serpent: #8B5CF6;')],
                problems=['le test «\xa0score\xa0» donne 0.0 au lieu de 1.0'],
                cost_usd=0.17,
            ),
        ),
    ]

    stored = [  # what the stores add to the catalogue: an app opened by its id, a scene the editor previews
        CatalogEntry(
            id='app:snake',
            group='apps',
            label='Snake',
            description='Ouvre cette app sur le dashboard.',
            say='ouvre Snake',
            tool='app',
            action=AppAction(kind='app', app_id='snake'),
        ),
        CatalogEntry(
            id='scene:fete-surprise',
            group='scenes',
            label='La fête surprise',
            description='Une scène à plusieurs voix, jouée par le robot.',
            say='joue la scène “La fête surprise”',
            tool='play_scene',
            action=SceneAction(kind='scene', scene_id='fete-surprise'),
            preview=True,
        ),
    ]

    def dump(models: list[BaseModel]) -> list[dict[str, Any]]:
        return [m.model_dump(mode='json') for m in models]

    return {
        'Catalog': dump([catalog.build(stored)]),
        'SettingsView': dump([view]),
        'RobotEvent': dump(events),
        'SessionSnapshot': dump([awake, asleep, moving_asleep, on_stage]),
        'DashboardState': dump([DashboardState(robot_status='live', session=awake, volume=70)]),
        'Volume': dump([Volume(volume=0), Volume(volume=100)]),
        'WsMessage': dump(messages),
        'PreviewFrames': dump([frames]),
        'SceneList': dump(
            [
                SceneList(
                    scenes=[
                        SceneSummary(
                            id='fete-surprise',
                            title='La fête surprise',
                            builtin=True,
                            errors=0,
                            segments=15,
                            rendered=15,
                        )
                    ]
                )
            ]
        ),
        'SceneOut': dump(
            [
                SceneOut(
                    id='fete-surprise',
                    title='La fête surprise',
                    text='@NARRATEUR (Charon) : conteur',
                    builtin=True,
                    check=check,
                )
            ]
        ),
        'SceneCheck': dump([check]),
        'SceneEmotions': dump([SceneEmotions(text='MARCO : [inquiring1] Alors ? [sad1] Rien.', added=2)]),
        'SceneBuild': dump([writing, staged, refused]),
        'SceneBuilds': dump([SceneBuilds(builds=[writing, staged])]),
        'SceneVocabulary': dump(
            [
                SceneVocabulary(
                    emotions=[word],
                    gestures=[SceneWord(word='hoche la tête', label='Oui de la tête')],
                    turns=[SceneWord(word='tourne à gauche', label='Se tourner à gauche')],
                    pauses=[SceneWord(word='pause 2', label='Pause de 2 s')],
                    sounds=[SceneWord(word='rire', label='Rire')],
                    voices=[SceneWord(word='Charon', label='Charon — masculine, informative')],
                )
            ]
        ),
        'AppList': dump([AppList(apps=[app, building, shipped])]),
        'AppHtml': dump(
            [
                AppHtml(
                    id=app.id,
                    html='<!doctype html><html lang="fr"><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"></head><body></body></html>',
                )
            ]
        ),
    }
