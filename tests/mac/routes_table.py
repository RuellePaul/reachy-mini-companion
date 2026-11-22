"""docs/CONTRACTS.md's route table, one set per module: the core routes, then each subject's own router."""

from __future__ import annotations

CORE = {
    ('GET', '/api/settings'),
    ('PATCH', '/api/settings'),
    ('GET', '/api/voices/{name}/sample'),
    ('GET', '/api/state'),
    ('POST', '/api/session/wake'),
    ('POST', '/api/session/sleep'),
    ('POST', '/api/session/restart'),
    ('POST', '/api/session/say'),
    ('PUT', '/api/volume'),
    ('POST', '/api/spotify/play'),
    ('POST', '/api/spotify/control'),
    ('POST', '/api/robot/events'),
    ('POST', '/api/robot/heartbeat'),
    ('POST', '/api/robot/voice'),
    ('GET', '/api/robot/commands'),
    ('GET', '/api/catalog'),
    ('POST', '/api/studio/try'),
    ('GET', '/api/spotify/login'),
    ('GET', '/api/spotify/callback'),
}
SUBJECTS = {
    'studio': {('GET', '/api/studio/preview')},
    'scenes': {
        ('GET', '/api/scenes'),
        ('POST', '/api/scenes'),
        ('GET', '/api/scenes/vocabulary'),
        ('POST', '/api/scenes/check'),
        ('POST', '/api/scenes/emotions'),
        ('POST', '/api/scenes/find'),
        ('POST', '/api/scenes/write'),
        ('POST', '/api/scenes/voice'),
        ('GET', '/api/scenes/builds'),
        ('POST', '/api/scenes/builds/{build_id}/cancel'),
        ('POST', '/api/scenes/stop'),
        ('GET', '/api/scenes/audio/{key}'),
        ('GET', '/api/scenes/{scene_id}'),
        ('PUT', '/api/scenes/{scene_id}'),
        ('DELETE', '/api/scenes/{scene_id}'),
        ('POST', '/api/scenes/{scene_id}/play'),
    },
    'apps': {
        ('GET', '/api/apps'),
        ('POST', '/api/apps'),
        ('POST', '/api/apps/voice'),
        ('GET', '/api/apps/{app_id}/html'),
        ('PATCH', '/api/apps/{app_id}'),
        ('DELETE', '/api/apps/{app_id}'),
        ('POST', '/api/apps/{app_id}/update'),
        ('POST', '/api/apps/{app_id}/cancel'),
        ('GET', '/api/apps/{app_id}/draft'),
        ('POST', '/api/apps/{app_id}/bridge'),
        ('POST', '/api/apps/{app_id}/manette'),
        ('POST', '/api/apps/{app_id}/quiet'),
    },
}
# Routes that read or write a file, call the daemon, Spotify or Gemini: they must never run on the event loop.
BLOCKING = {
    ('GET', '/api/settings'),
    ('PATCH', '/api/settings'),
    ('GET', '/api/voices/{name}/sample'),
    ('GET', '/api/state'),
    ('PUT', '/api/volume'),
    ('POST', '/api/spotify/play'),
    ('POST', '/api/spotify/control'),
    ('GET', '/api/catalog'),
    ('GET', '/api/spotify/login'),
    ('GET', '/api/spotify/callback'),
    ('GET', '/api/studio/preview'),
    *SUBJECTS['scenes'],
    *(
        route
        for route in SUBJECTS['apps']
        if route
        not in {
            ('POST', '/api/apps/{app_id}/bridge'),
            ('POST', '/api/apps/{app_id}/manette'),
        }
    ),
}
