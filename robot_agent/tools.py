"""The robot's tools: declarations (all NON_BLOCKING, short French descriptions) and their handlers.

A handler returns a `ToolResult` and never blocks the event loop: HTTP and the camera go through a thread,
motion is handed to the motion thread. The session owns the guard, `goto_sleep` and the one answer per call.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from google.genai import types

from robot_agent.adapters import Audio, Body, Camera
from robot_agent.animations import GESTURES
from robot_agent.emotions import Emotions
from robot_agent.mac import Mac, MacError

logger = logging.getLogger(__name__)

Status = Literal['ok', 'error', 'refused', 'cancelled']
SendImage = Callable[[bytes], Awaitable[None]]
Handler = Callable[[dict[str, Any], SendImage], Awaitable['ToolResult']]
Extra = Mapping[str, tuple[types.FunctionDeclaration, Handler]]  # a subject module's tools (scene, apps)
SILENT, WHEN_IDLE = types.FunctionResponseScheduling.SILENT, types.FunctionResponseScheduling.WHEN_IDLE
# The user saw the robot move, or the image is its own turn: nothing to say about these results.
PHYSICAL = frozenset({'do_gesture', 'play_emotion', 'turn_body', 'set_volume', 'look'})
PLAY_KINDS = ('track', 'artist', 'album', 'playlist', 'liked', 'audiobook', 'podcast')  # contracts.PlayKind
MUSIC_ACTIONS = (  # contracts.MusicAction
    'pause', 'resume', 'next', 'previous', 'now', 'like', 'queue', 'seek', 'shuffle', 'repeat', 'volume', 'device',
    'playlist',
)  # fmt: skip
LOOK_REPEAT_S = 3.0  # one ask can come as several identical calls: one picture for them all
MOVE_REPEAT_S = 2.5  # likewise for a move: the gesture, emotion or turn just played, asked again this soon, plays once
# What each move replaces: a gesture and an emotion share the head's move; the body turn is its own.
DEBOUNCED = {'do_gesture': 'move', 'play_emotion': 'move', 'turn_body': 'turn'}
LENT = frozenset({'do_gesture', 'play_emotion', 'turn_body', 'play_scene'})  # refused while an app holds the body


@dataclass(frozen=True)
class ToolResult:
    status: Status
    message: str  # French: what happened, for the model and the dashboard's transcript
    data: dict[str, Any] = field(default_factory=dict)  # contracts.ToolData: track, image_base64, volume
    silent: bool = False  # the deed was the answer (a scene played to its curtain): nothing more to say
    ask: str = ''  # what the model is told to do about it (« Dis-le simplement. »): the model's alone

    def response(self, call_id: str, name: str, *, spoke: bool = True) -> types.FunctionResponse:
        """The call's one answer: never `will_continue`.

        SILENT when there is nobody to tell (a refusal: nobody asked) or nothing to tell (a physical
        success the robot spoke over); WHEN_IDLE otherwise, errors included, so a failure is said rather
        than filed away. A gesture that was the whole answer (`spoke` False) is not silent either: the
        model answers in a gesture alone, and a SILENT result then leaves the question unanswered.
        `look` stays silent: its image is a turn of its own, which the model already answers.
        """
        physical = name in PHYSICAL and (spoke or name == 'look')
        silent = self.status == 'refused' or (self.status == 'ok' and (physical or self.silent))
        return types.FunctionResponse(
            id=call_id,
            name=name,
            response={'status': self.status, 'message': f'{self.message} {self.ask}'.strip()},
            scheduling=SILENT if silent else WHEN_IDLE,
        )

    def event(self, call_id: str, name: str) -> dict[str, Any]:
        """The dashboard's `tool_result` (contracts.ToolResult): what happened, never what the model is told (`ask`)."""
        return {
            'kind': 'tool_result',
            'call_id': call_id,
            'name': name,
            'status': self.status,
            'message': self.message,
            'data': self.data,
        }


_LENT = ToolResult(
    'error',
    "Une app se sert du robot comme manette\xa0: ni geste, ni émotion, ni scène tant qu'elle joue.",
    ask='Dis-le simplement.',
)


def _decl(
    tool: str, description: str, /, *, optional: tuple[str, ...] = (), **params: types.Schema
) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=tool,
        description=description,
        behavior=types.Behavior.NON_BLOCKING,
        parameters=types.Schema(
            type=types.Type.OBJECT, properties=params, required=[key for key in params if key not in optional]
        ),
    )


def _text(description: str, enum: list[str] | None = None) -> types.Schema:
    return types.Schema(type=types.Type.STRING, description=description, enum=enum)


def _number(description: str, kind: types.Type = types.Type.NUMBER) -> types.Schema:
    return types.Schema(type=kind, description=description)


def declarations(*, emotions: list[str], camera: bool) -> list[types.FunctionDeclaration]:
    """The catalogue. `play_emotion` needs the library loaded, `look` a camera and an encoder."""
    decls = [
        _decl(
            'do_gesture',
            'Un geste de tête, seulement quand on te le demande (« fais oui de la tête », « penche la tête ») : '
            "n'en ajoute jamais de toi-même à tes réponses.",
            name=_text(
                'nod = oui, shake = non, tilt_left et tilt_right = tête penchée, look_up et look_down = regard.',
                list(GESTURES),
            ),
        ),
        _decl(
            'turn_body',
            'Tourne tout ton corps vers un angle absolu. Pour « tourne-toi », « regarde à gauche », « reviens ».',
            angle=_number('Degrés : 0 = de face, 100 = à gauche, -100 = à droite.'),
        ),
        _decl('set_volume', 'Règle le volume de ton haut-parleur.', level=_number('De 0 à 100.', types.Type.INTEGER)),
        _decl(
            'goto_sleep',
            'Te met en veille et termine la conversation : pour « dors », « bonne nuit », « mets-toi en veille ». '
            "N'ajoute rien : ton haut-parleur se coupe dès l'appel.",
        ),
        _decl(
            'play_music',
            "Lance sur Spotify, sur l'appareil de la maison (pas ton haut-parleur) : musique, livre audio ou "
            'podcast, repris où il en était. Sans précision, choisis toi-même.',
            query=_text('Titre, artiste, album, playlist, livre, émission ou ambiance.'),
            kind=_text('Ce que désigne query.', list(PLAY_KINDS)),
            optional=('query', 'kind'),
        ),
        _decl(
            'music',
            'Pilote la lecture Spotify en cours.',
            action=_text(
                'now : ce qui joue ; like : ajoute aux likés ; queue : value à la suite ; seek : avance de amount s '
                "(négatif : recule) ; volume : celui de Spotify, pas le tien ; device : passe sur l'appareil value ; "
                "playlist : enregistre sous le nom value ce qui a été lancé aujourd'hui.",
                list(MUSIC_ACTIONS),
            ),
            value=_text('shuffle : on ou off ; repeat : on, off ou track.'),
            amount=_number('seek : secondes ; volume : 0 à 100.'),
            optional=('value', 'amount'),
        ),
    ]
    if emotions:
        decls.append(
            _decl(
                'play_emotion',
                'Joue une émotion (un mouvement enregistré, avec son petit son), seulement quand on te le demande ou '
                'pour un moment vraiment fort, jamais à chaque réponse ; elle accompagne tes mots sans les remplacer.',
                name=_text("L'émotion.", emotions),
            )
        )
    if camera:
        decls.append(
            _decl(
                'look',
                'Prend une photo avec ta caméra, ton œil tourné vers la pièce, et te la montre : pour « regarde », '
                "« qu'est-ce que tu vois », « prends une photo ». Un appel suffit ; décris l'image sans rien inventer.",
            )
        )
    return decls


class Tools:
    def __init__(
        self,
        *,
        body: Body,
        audio: Audio,
        camera: Camera | None,
        mac: Mac,
        emotions: Emotions,
        extra: Extra | None = None,
        lent: Callable[[], str | None] = lambda: None,  # the manette profile an app holds the body with, if any
    ) -> None:
        self._body, self._audio, self._camera, self._mac, self._emotions = body, audio, camera, mac, emotions
        self._lent = lent
        self._extra = dict(extra or {})
        self._looking = asyncio.Lock()  # one photo at a time: a duplicate call waits for the first one's outcome
        self._looked_at = -math.inf  # when a photo last reached the model
        self._moving = asyncio.Lock()  # one move call at a time: a repeat waits for the first one's outcome
        self._moved: dict[str, tuple[str, float]] = {}  # per layer: the last call that played, and when it was asked
        self._handlers = {
            'do_gesture': self._do_gesture,
            'turn_body': self._turn_body,
            'set_volume': self._set_volume,
            'play_emotion': self._play_emotion,
            'look': self._look,
            'play_music': self._play_music,
            'music': self._music,
        }

    def declarations(self) -> list[types.FunctionDeclaration]:
        own = declarations(emotions=self._emotions.names(), camera=self._camera is not None)
        return [*own, *(decl for decl, _ in self._extra.values())]

    async def run(self, name: str, args: dict[str, Any], send_image: SendImage) -> ToolResult:
        handler = self._extra[name][1] if name in self._extra else self._handlers.get(name)
        if handler is None:
            return ToolResult('error', f"L'outil «\xa0{name}\xa0» n'existe pas\xa0: rien n'a été fait.")
        if name in LENT and self._lent():
            return _LENT
        if (layer := DEBOUNCED.get(name)) is not None:
            call, asked_at = f'{name}:{sorted(args.items())}', time.monotonic()
            async with self._moving:
                last, at = self._moved.get(layer, ('', -math.inf))
                if call == last and asked_at - at < MOVE_REPEAT_S:  # left, front, left: the third is a new ask
                    return ToolResult('ok', "Déjà fait\xa0: ce mouvement vient d'être joué.")
                result = await handler(args, send_image)
                if result.status == 'ok':  # a move that did not play is no move: asked again, it is tried again
                    self._moved[layer] = (call, asked_at)
                return result
        try:
            return await handler(args, send_image)
        except MacError as exc:
            return ToolResult('error', str(exc))

    async def _do_gesture(self, args: dict[str, Any], _send: SendImage) -> ToolResult:
        name = str(args.get('name', ''))
        if not self._body.gesture(name):
            return ToolResult(
                'error',
                f"Geste inconnu «\xa0{name}\xa0»\xa0: rien n'a été joué.",
                ask=f'Choisis parmi {", ".join(GESTURES)}.',
            )
        return ToolResult('ok', f'Geste «\xa0{name}\xa0» joué.')

    async def _turn_body(self, args: dict[str, Any], _send: SendImage) -> ToolResult:
        angle = _as_number(args.get('angle'))
        if angle is None:
            return ToolResult('error', "Rotation non lancée\xa0: l'angle doit être un nombre de degrés, de -100 à 100.")
        target = self._body.turn_body(angle)
        limited = ' (limitée à 100° de chaque côté)' if target != angle else ''
        return ToolResult('ok', f'Rotation vers {target:.0f}° lancée{limited}.')

    async def _set_volume(self, args: dict[str, Any], _send: SendImage) -> ToolResult:
        level = _as_number(args.get('level'))
        if level is None:
            return ToolResult('error', 'Volume inchangé\xa0: le niveau doit être un nombre entre 0 et 100.')
        volume = int(min(100.0, max(0.0, round(level))))
        answer = await asyncio.to_thread(self._mac.request, 'PUT', '/api/volume', {'volume': volume})
        volume = int(answer.get('volume', volume))
        return ToolResult('ok', f'Volume réglé à {volume} sur 100.', {'volume': volume})

    async def _play_emotion(self, args: dict[str, Any], _send: SendImage) -> ToolResult:
        name = str(args.get('name', ''))
        move = await asyncio.to_thread(self._emotions.get, name)
        if move is None:
            return ToolResult('error', f"Émotion indisponible «\xa0{name}\xa0»\xa0: rien n'a été joué.")
        self._body.play_move(move)
        if getattr(move, 'sound_path', None):
            try:
                await asyncio.to_thread(self._audio.play_sound, str(move.sound_path))
            except Exception as exc:  # noqa: BLE001 — the move plays anyway
                logger.warning('emotion sound failed: %s', exc)
        return ToolResult('ok', f'Émotion «\xa0{name}\xa0» jouée.')

    async def _look(self, args: dict[str, Any], send_image: SendImage) -> ToolResult:
        if self._camera is None:
            return ToolResult(
                'error', "Pas de caméra en ce moment\xa0: rien n'a été vu.", ask="Dis-le, n'invente rien."
            )
        async with self._looking:
            if time.monotonic() - self._looked_at < LOOK_REPEAT_S:
                return ToolResult('ok', 'Photo déjà prise\xa0: décris celle que tu as reçue.')
            jpeg = await asyncio.to_thread(self._camera.read_jpeg)
            if not jpeg:
                return ToolResult(
                    'error', "Pas d'image\xa0: la caméra n'a rien renvoyé.", ask="Dis-le simplement, n'invente rien."
                )
            await send_image(jpeg)  # its own user turn: that is what makes the model describe it
            self._looked_at = time.monotonic()  # only once sent: a failed send leaves nothing to describe
        image = base64.b64encode(jpeg).decode('ascii')
        return ToolResult('ok', "Photo prise\xa0: c'est l'image que tu viens de recevoir.", {'image_base64': image})

    async def _play_music(self, args: dict[str, Any], _send: SendImage) -> ToolResult:
        query = str(args.get('query') or '').strip()[:200]
        kind = args.get('kind') if args.get('kind') in PLAY_KINDS else 'track' if query else 'liked'
        if not query and kind != 'liked':
            return ToolResult(
                'error', 'Rien à chercher.', ask='Précise un titre, un artiste, un livre ou une émission.'
            )
        answer = await asyncio.to_thread(self._mac.request, 'POST', '/api/spotify/play', {'query': query, 'kind': kind})
        data = _music_data(answer)
        track = data.get('track', {})
        what = 'Lecture de tes titres likés' if kind == 'liked' else 'Lecture lancée'
        by = f', de {track["artist"]}' if track.get('artist') else ''  # an artist is its own title
        return ToolResult('ok', f'{what}\xa0: {track.get("title", "")}{by}.', data)

    async def _music(self, args: dict[str, Any], _send: SendImage) -> ToolResult:
        action = args.get('action')
        if action not in MUSIC_ACTIONS:
            return ToolResult('error', f"Action inconnue «\xa0{action}\xa0»\xa0: rien n'a été fait.")
        value, amount = str(args.get('value') or '').strip()[:200], _as_number(args.get('amount'))
        body = {'action': action, 'value': value, 'amount': amount}
        answer = await asyncio.to_thread(self._mac.request, 'POST', '/api/spotify/control', body)
        return ToolResult('ok', str(answer.get('message') or 'Fait.'), _music_data(answer))


def _music_data(answer: dict[str, Any]) -> dict[str, Any]:
    """contracts.ToolData from a PlayOut or a ControlOut: structured fields, never parsed out of the sentence."""
    data: dict[str, Any] = {}
    if isinstance(track := answer.get('track'), dict):
        data['track'] = {
            'title': str(track.get('title', '')),
            'artist': str(track.get('artist', '')),
            'uri': track.get('uri'),
        }
    if isinstance(volume := answer.get('volume'), int) and 0 <= volume <= 100:
        data['volume'] = volume
    return data


def _as_number(value: object) -> float | None:
    """A finite number from what the model sent ('45', 45, 45.0), or None."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
