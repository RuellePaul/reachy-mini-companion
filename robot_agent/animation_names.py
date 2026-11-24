"""Animation, gesture and emotion names, shared by the robot (which plays them) and the Mac (which validates,
lists and previews them).

Dependency-free on purpose: the Mac imports this module, and `make sync` ships it to the robot.
"""

from __future__ import annotations

from typing import Final

MODES: Final = ('idle', 'listening', 'talking')

# mode -> {key: French label for the dashboard}. Talking variants stay small: the robot's
# motors are as loud as a voice in its own microphone.
VARIANTS: Final[dict[str, dict[str, str]]] = {
    'idle': {
        'calm': 'Posé',
        'curious': 'Curieux',
        'dreamy': 'Rêveur',
        'breathing': 'Respiration',
        'sleepy': 'Somnolent',
        'observer': 'Observateur',
        'playful': 'Espiègle',
        'still': 'Immobile',
    },
    'listening': {
        'attentive': 'Attentif',
        'curious': 'Curieux',
        'patient': 'Patient',
        'nodding': 'Approbateur',
        'tilted': 'Intrigué',
        'focused': 'Concentré',
        'still': 'Immobile',
    },
    'talking': {
        'calm': 'Posé',
        'nod': 'Hochements',
        'lean': 'Penché',
        'expressive': 'Expressif',
        'thoughtful': 'Pensif',
        'still': 'Immobile',
    },
}

DEFAULTS: Final[dict[str, str]] = {'idle': 'calm', 'listening': 'attentive', 'talking': 'calm'}

# How pronounced (amplitude) and how fast (speed) a mode's animation plays: factors of the variant as written.
# Talking never grows: its head already moves 3° at most.
TUNING_DEFAULT: Final = 1.0
AMPLITUDE_RANGE: Final[dict[str, tuple[float, float]]] = {
    'idle': (0.5, 1.5),
    'listening': (0.5, 1.5),
    'talking': (0.5, 1.0),
}
SPEED_RANGE: Final = (0.5, 1.5)

# name -> (French label, a sentence that asks for it). Played only on request.
GESTURES: Final[dict[str, tuple[str, str]]] = {
    'nod': ('Oui de la tête', 'fais oui de la tête'),
    'shake': ('Non de la tête', 'fais non de la tête'),
    'tilt_left': ('Tête penchée à gauche', 'penche la tête à gauche'),
    'tilt_right': ('Tête penchée à droite', 'penche la tête à droite'),
    'look_up': ('Regard vers le haut', 'regarde en haut'),
    'look_down': ('Regard vers le bas', 'regarde en bas'),
    'wiggle': ('Antennes joyeuses', 'remue les antennes'),
    'bow': ('Révérence', 'fais une révérence'),
    'think': ('Réfléchir', 'fais semblant de réfléchir'),
    'surprise': ('Surprise', 'fais le surpris'),
    'dance': ('Petite danse', 'fais une petite danse'),
}

# Pollen's recorded emotions offered to the model (a short list: every turn re-bills the declaration),
# name -> (French label, a sentence that asks for it).
EMOTIONS: Final[dict[str, tuple[str, str]]] = {
    'cheerful1': ('Joyeux', 'montre que tu es joyeux'),
    'enthusiastic1': ('Enthousiaste', 'montre ton enthousiasme'),
    'laughing1': ('Rire', 'fais-moi rire'),
    'loving1': ('Affectueux', "montre que tu m'aimes"),
    'grateful1': ('Reconnaissant', 'montre que tu es reconnaissant'),
    'proud1': ('Fier', 'montre que tu es fier'),
    'success1': ('Victoire', 'fête la victoire'),
    'amazed1': ('Émerveillé', 'montre que tu es émerveillé'),
    'surprised1': ('Surpris', 'montre que tu es surpris'),
    'curious1': ('Curieux', 'montre que tu es curieux'),
    'inquiring1': ('Interrogatif', 'prends un air interrogatif'),
    'thoughtful1': ('Pensif', 'prends un air pensif'),
    'understanding1': ('Compréhensif', 'montre que tu comprends'),
    'confused1': ('Perplexe', 'montre que tu es perplexe'),
    'uncertain1': ('Hésitant', 'montre que tu hésites'),
    'oops1': ('Oups', 'fais oups'),
    'shy1': ('Timide', 'fais le timide'),
    'sad1': ('Triste', 'montre que tu es triste'),
    'tired1': ('Fatigué', 'montre que tu es fatigué'),
    'boredom1': ('Ennuyé', "montre que tu t'ennuies"),
    'fear1': ('Effrayé', "fais semblant d'avoir peur"),
    'welcoming1': ('Accueillant', 'accueille-moi'),
    'yes1': ('Oui enthousiaste', 'dis oui avec tout ton corps'),
    'no1': ('Non catégorique', 'dis non avec tout ton corps'),
    'dance1': ('Danse', 'danse pour moi'),
}
