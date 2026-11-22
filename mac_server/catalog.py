"""The catalogue of everything the robot can do (GET /api/catalog), built from the code that does it.

Gestures, emotions and animations come from `robot_agent.animation_names`, scenes and apps from their stores;
the robot's tools are linked by name (tests/test_cross.py checks every declared tool is here). The dashboard's
palette keeps no list of its own.
"""

from __future__ import annotations

from collections.abc import Iterable

from mac_server.contracts import Catalog, CatalogEntry, CatalogGroup, TryMove
from robot_agent import animation_names as anim

GROUPS = (
    ('session', 'Conversation'),
    ('gestures', 'Gestes et mouvements'),
    ('emotions', 'Émotions'),
    ('animations', 'Animations'),
    ('scenes', 'Scènes'),
    ('apps', 'Apps'),
    ('spotify', 'Spotify'),
    ('settings', 'Réglages'),
)
MODE_LABELS = {'idle': 'repos', 'listening': 'écoute', 'talking': 'parole'}


def _entry(**fields: object) -> CatalogEntry:
    return CatalogEntry.model_validate(fields)


def _session() -> list[CatalogEntry]:
    return [
        _entry(
            id='session:wake',
            group='session',
            label='Réveiller le robot',
            description='Il se réveille et écoute.',
            say=None,
            action={'kind': 'session', 'command': 'wake'},
        ),
        _entry(
            id='session:sleep',
            group='session',
            label='Mettre en veille',
            description='Il termine la conversation et se rendort.',
            say='dors',
            tool='goto_sleep',
            action={'kind': 'session', 'command': 'sleep'},
        ),
        _entry(
            id='session:restart',
            group='session',
            label='Relancer la conversation',
            description='Il se rendort puis se réveille avec les derniers réglages.',
            action={'kind': 'session', 'command': 'restart'},
        ),
        _asked('session:look', 'gestures', 'Regarder la pièce',
               "Il prend une photo avec sa caméra et décrit ce qu'il voit.", 'regarde autour de toi', 'look'),
        _asked('session:turn', 'gestures', 'Tourner le corps',
               "Il pivote tout son corps, jusqu'à 100° de chaque côté.", 'tourne-toi vers|la gauche', 'turn_body'),
    ]  # fmt: skip


def _moves() -> list[CatalogEntry]:
    entries = [
        _entry(
            id=f'gesture:{name}',
            group='gestures',
            label=label,
            description="Un geste de tête et d'antennes, joué seulement à la demande.",
            say=sentence,
            tool='do_gesture',
            action={'kind': 'try', 'move': TryMove(kind='gesture', name=name)},
            preview=True,
        )
        for name, (label, sentence) in anim.GESTURES.items()
    ]
    entries += [
        _entry(
            id=f'emotion:{name}',
            group='emotions',
            label=label,
            description='Une émotion enregistrée par Pollen, avec son petit son.',
            say=sentence,
            tool='play_emotion',
            action={'kind': 'try', 'move': TryMove(kind='emotion', name=name)},
            preview=True,
        )
        for name, (label, sentence) in anim.EMOTIONS.items()
    ]
    entries += [
        _entry(
            id=f'animation:{mode}:{key}',
            group='animations',
            label=f'{label} ({MODE_LABELS[mode]})',
            description=f"Animation de {MODE_LABELS[mode]}. Elle se choisit dans le studio et s'applique au prochain réveil.",
            action={'kind': 'try', 'move': TryMove(kind='animation', name=key, mode=mode)},
            preview=True,
        )
        for mode in anim.MODES
        for key, label in anim.VARIANTS[mode].items()
    ]
    return entries


def _asked(entry_id: str, group: str, label: str, description: str, sentence: str, tool: str) -> CatalogEntry:
    """An entry run by asking the robot. A sentence that takes no parameter is typed in the conversation as is; in one
    that does, what follows `|` is an example (what to play, where to turn): only the start goes to the composer, for
    the user to finish. The palette never sends a made-up example."""
    start, cut, _ = sentence.partition('|')
    said = sentence.replace('|', ' ')
    text = f'{start[0].upper()}{start[1:]}' + (' ' if cut else '' if said.endswith('?') else '.')
    return _entry(
        id=entry_id,
        group=group,
        label=label,
        description=description,
        say=said,
        tool=tool,
        action={'kind': 'compose' if cut else 'say', 'text': text},
    )


# Every value the model can give `play_music` (kind) and `music` (action), with its label, description and sentence
# (tests/test_cross.py checks none is missing).
PLAY = {
    'track': ('Lancer un titre', 'Un morceau, cherché sur Spotify.', 'mets le titre|Get Lucky'),
    'artist': ('Lancer un artiste', "Les titres d'un artiste.", 'mets des chansons de|Daft Punk'),
    'album': ('Lancer un album', 'Un album en entier.', "mets l'album|Discovery"),
    'playlist': ('Lancer une playlist', 'Une de tes playlists, ou une playlist publique.', 'mets ma playlist|Dimanche'),
    'liked': ('Mes titres likés', 'Tes titres likés, en aléatoire.', 'mets mes titres likés'),
    'audiobook': ('Livre audio', 'Reprend un livre audio là où tu en étais.', 'reprends mon livre audio'),
    'podcast': ('Podcast', "Le dernier épisode d'une émission.", 'mets le dernier épisode de mon podcast'),
}
MUSIC = {
    'pause': ('Pause', 'Met la musique en pause.', 'mets la musique en pause'),
    'resume': ('Reprendre la musique', 'Relance la lecture là où elle était.', 'remets la musique'),
    'next': ('Morceau suivant', 'Passe au morceau suivant.', 'passe au suivant'),
    'previous': ('Morceau précédent', 'Revient au morceau précédent.', 'reviens au morceau précédent'),
    'now': ("Qu'est-ce qui passe\xa0?", "Il dit le titre et l'artiste en cours.", "qu'est-ce qui passe\xa0?"),
    'like': ('Liker ce titre', 'Ajoute le titre en cours à tes titres likés.', 'like ce morceau'),
    'queue': ('Ajouter à la file', 'Met un morceau à la suite.', 'ajoute à la file|Get Lucky'),
    'seek': ('Avancer ou reculer', 'Se déplace dans le morceau en cours.', 'avance la musique de|30 secondes'),
    'shuffle': ('Lecture aléatoire', 'La met en marche ou la coupe.', 'mets la lecture aléatoire|en marche'),
    'repeat': ('Répétition', 'Répète le morceau, la playlist, ou plus rien.', 'répète|ce morceau'),
    'volume': ('Volume de Spotify', "Celui de l'appareil Spotify, pas du robot.", 'mets le volume de Spotify à|30'),
    'device': ("Changer d'appareil", 'Passe la musique sur un autre appareil.', 'passe la musique sur|le salon'),
    'playlist': ('Créer une playlist', "Ce qui a été lancé aujourd'hui, sous le nom que tu dis.",
                 "fais une playlist de ce qu'on a écouté, appelée|Dimanche"),
}  # fmt: skip


def _spotify() -> list[CatalogEntry]:
    played = [_asked(f'spotify:play:{kind}', 'spotify', *row, 'play_music') for kind, row in PLAY.items()]
    return played + [_asked(f'spotify:{action}', 'spotify', *row, 'music') for action, row in MUSIC.items()]


def _apps_and_scenes() -> list[CatalogEntry]:
    return [
        _entry(
            id='apps:create',
            group='apps',
            label='Créer une app',
            description='Un jeu ou un petit outil, construit en quelques minutes\xa0: décris-le.',
            say='fais-moi un Snake où tu comptes les points',
            tool='app',
            action={'kind': 'app'},
        ),
        _asked('apps:update', 'apps', 'Modifier une app', 'Change une app existante.',
               "modifie l'app|Snake\xa0: ajoute un niveau", 'app'),
        _asked('scenes:play', 'scenes', 'Jouer une scène', "Une saynète à plusieurs voix, écrite à l'avance.",
               'joue la scène|de la fête', 'play_scene'),
        _asked('scenes:write', 'scenes', 'Écrire une scène', "Une saynète écrite sur ton idée, jouée dès qu'elle est prête.",
               'fais une scène où|deux personnages débattent de la dualité onde-corpuscule', 'write_scene'),
    ]  # fmt: skip


def _settings() -> list[CatalogEntry]:
    fields = (
        ('name', 'Nom du robot', 'Le nom auquel il répond.', None, None),
        ('voice', 'Voix', 'Sa voix Gemini, avec un échantillon pour chacune.', None, None),
        ('personality', 'Personnalité', 'Quelques lignes qui disent qui il est.', None, None),
        (
            'volume',
            'Volume',
            'Le volume de son haut-parleur, appliqué tout de suite.',
            'baisse le volume',
            'set_volume',
        ),
        ('gaze_tracking', 'Regard actif', 'Sa tête suit le visage de la personne qui lui parle.', None, None),
        ('web_search', 'Recherche web', "Il cherche sur le web pour l'actualité et la météo.", None, None),
        ('idle_sleep_minutes', 'Mise en veille', 'Après combien de minutes de silence il se rendort.', None, None),
    )
    return [
        _entry(
            id=f'settings:{field}',
            group='settings',
            label=label,
            description=description,
            say=sentence,
            tool=tool,
            action={'kind': 'settings', 'field': field},
        )
        for field, label, description, sentence, tool in fields
    ]


def build(extra: Iterable[CatalogEntry] = ()) -> Catalog:
    """The whole catalogue; `extra` are the entries of the stores and subjects (scenes, apps, Spotify)."""
    entries = [*_session(), *_moves(), *_apps_and_scenes(), *_spotify(), *_settings(), *extra]
    order = [key for key, _ in GROUPS]
    entries.sort(key=lambda e: order.index(e.group))  # stable: each group keeps its own order
    return Catalog(groups=[CatalogGroup(key=k, label=lbl) for k, lbl in GROUPS], entries=entries)  # type: ignore[arg-type]
