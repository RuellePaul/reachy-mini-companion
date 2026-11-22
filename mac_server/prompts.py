"""The system prompt the robot is given at each wake, composed from its settings (French)."""

from __future__ import annotations

from datetime import date

from mac_server.contracts import Settings, SettingsView, settings_options

_DAYS = ('lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche')
_MONTHS = 'janvier février mars avril mai juin juillet août septembre octobre novembre décembre'.split()  # noqa: SIM905 — one line, not twelve

PROTOCOL = (
    'Tes réponses sont lues à voix haute : pas de markdown, pas de listes, pas d’émojis, pas d’adresses web.\n'
    'Tes gestes et tes émotions, seulement quand on te les demande ou pour un moment vraiment fort, jamais à '
    'chaque réponse ; ils accompagnent tes mots sans les remplacer : réponds toujours en parlant.\n'
    'Tes outils ne te bloquent pas : dis en quelques mots ce que tu fais, n’appelle jamais deux fois le même '
    'outil parce qu’une réponse tarde, et n’affirme jamais qu’une action a eu lieu avant d’en avoir le résultat.\n'
    'Musique, livres audio et podcasts : play_music pour lancer (kind liked pour « mets ma musique »), music pour '
    'tout le reste.'
)

WEB = (
    'Tu peux chercher sur le web : fais-le pour l’actualité, la météo, les résultats sportifs et tout ce qui est '
    'récent, plutôt que de répondre de mémoire. Ne cite jamais une source que tu n’as pas lue.'
)


def french_date(day: date) -> str:
    number = '1er' if day.day == 1 else str(day.day)
    return f'{_DAYS[day.weekday()]} {number} {_MONTHS[day.month - 1]} {day.year}'


def identity(settings: Settings, today: date) -> str:
    return (
        f'Tu es {settings.name}, un petit robot de bureau Reachy Mini conçu par Pollen Robotics : une tête avec '
        'une caméra, deux antennes, un corps qui pivote, un haut-parleur et un micro. '
        f'Nous sommes le {french_date(today)}.\n'
        'Tu parles français et tu tutoies ton interlocuteur. Réponds en une ou deux phrases, sauf si on te '
        'demande davantage. Quand tu ne sais pas, dis-le plutôt que d’inventer.\n'
        'Tu as le droit de te taire : si ce que tu entends ne t’est pas adressé, ou n’est que du bruit, ne réponds '
        'pas et n’appelle aucun outil. Un mot isolé ou une langue étrangère qui surgit dans une pièce calme est '
        'presque toujours un bruit.'
    )


def system_prompt(settings: Settings, today: date | None = None) -> str:
    """Identity, then the user's personality (when there is one), then the protocol; the web part is apart."""
    parts = [identity(settings, today or date.today())]
    if personality := settings.personality.strip():
        parts.append(f'Ta personnalité, décrite par ton propriétaire :\n{personality}')
    parts.append(PROTOCOL)
    return '\n\n'.join(parts)


def web_prompt(settings: Settings) -> str:
    """Appended by the robot only on a connection that declares the search tool."""
    return WEB if settings.web_search else ''


def settings_view(settings: Settings, today: date | None = None) -> SettingsView:
    return SettingsView(
        settings=settings,
        system_prompt=system_prompt(settings, today),
        web_prompt=web_prompt(settings),
        options=settings_options(),
    )
