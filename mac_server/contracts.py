"""Every payload that crosses a process boundary, defined once (see docs/CONTRACTS.md).

The robot never imports this module (it ships alone): it sends plain dicts, and tests validate what
it produces against these models. The dashboard mirrors them with minimal Zod schemas
(frontend/src/api/schemas.ts), tested against the examples of scripts/contract_examples.py. Error bodies are FastAPI's
`{"detail": ...}`, with a French string whenever the Mac raises it itself.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError

from mac_server.voices import DEFAULT_VOICE, VOICES
from robot_agent import animation_names as anim

COMMAND_TTL_S: Final = 30.0
TRY_TTL_S: Final = 10.0  # a try the robot has not taken by then is stale: the user moved on
NAME_MAX: Final = 40
PERSONALITY_MAX: Final = 1500
IDLE_SLEEP_MAX_MIN: Final = 60
SAY_MAX: Final = 1000
SCENE_TITLE_MAX: Final = 120
SCENE_TEXT_MAX: Final = 40_000
SCENE_REQUEST_MAX: Final = 2000
APP_TITLE_MAX: Final = 60
APP_REQUEST_MAX: Final = 2000
APP_MOTION_TTL_S: Final = 1.5  # an app's move the robot has not taken by then is stale: the game moved on
SCENE_STOP_TTL_S: Final = 10.0
MANETTE_TTL_S: Final = 3.0  # the manette's lease: the dashboard renews it every second while the app holds it
HUSH_TTL_S: Final = 10.0  # the robot silent during an app: the Mac says it again at each change, and each new wake
# Antennas as buttons; + the base freed; « paume »: the base freed, the antennas lowered as in sleep, under the
# fingers of the hand that turns it; + the head freed. « voix »: the microphone's level lent too (`Voice`), or
# alone: nothing of the body is lent, the robot goes on moving.
ManetteProfile = Literal[
    'gachettes', 'volant', 'paume', 'tete', 'voix', 'gachettes+voix', 'volant+voix', 'paume+voix', 'tete+voix'
]
ManetteWhy = Literal['app', 'lease', 'scene', 'sleep', 'profile', 'pose']  # pose: it could not take its still pose


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


def _unknown(what: str, value: object) -> PydanticCustomError:
    return PydanticCustomError('unknown_value', '{what}\xa0: «\xa0{value}\xa0»', {'what': what, 'value': str(value)})


# ----- Settings -------------------------------------------------------------------------------


class Tuning(_Model):
    """How pronounced and how fast a mode's animation plays: factors of the variant as written."""

    amplitude: float = anim.TUNING_DEFAULT
    speed: float = anim.TUNING_DEFAULT


class AnimationTuning(_Model):
    """One `Tuning` per mode, within animation_names.AMPLITUDE_RANGE and SPEED_RANGE."""

    idle: Tuning = Tuning()
    listening: Tuning = Tuning()
    talking: Tuning = Tuning()

    @field_validator('idle', 'listening', 'talking')
    @classmethod
    def _ranges(cls, v: Tuning, info: ValidationInfo) -> Tuning:
        mode = str(info.field_name)
        french = {'idle': 'repos', 'listening': 'écoute', 'talking': 'parole'}[mode]
        for what, value, (low, high) in (
            ('amplitude', v.amplitude, anim.AMPLITUDE_RANGE[mode]),
            ('vitesse', v.speed, anim.SPEED_RANGE),
        ):
            if not low <= value <= high:
                ctx = {
                    'what': what,
                    'mode': french,
                    'low': f'{low:g}'.replace('.', ','),
                    'high': f'{high:g}'.replace('.', ','),
                }
                raise PydanticCustomError('unknown_value', '{what} ({mode})\xa0: entre {low} et {high}', ctx)
        return v


# Who writes the apps (Claude Code on the owner's subscription): the setting's word -> the model's id.
APP_MODELS: dict[str, str] = {'haiku': 'claude-haiku-5-5', 'sonnet': 'claude-sonnet-5-5', 'opus': 'claude-opus-5-5'}
AppModel = Literal['haiku', 'sonnet', 'opus']
AppEffort = Literal['low', 'medium', 'high', 'xhigh']


class Settings(_Model):
    """Who the robot is. The only place defaults and rules live; read by the robot at each wake (the apps' model and
    effort by the Mac, at each build)."""

    name: str = Field(default='Reachy', min_length=1, max_length=NAME_MAX)
    voice: str = DEFAULT_VOICE
    personality: str = Field(default='', max_length=PERSONALITY_MAX)
    idle_animation: str = anim.DEFAULTS['idle']
    listening_animation: str = anim.DEFAULTS['listening']
    talking_animation: str = anim.DEFAULTS['talking']
    tuning: AnimationTuning = AnimationTuning()
    web_search: bool = True
    gaze_tracking: bool = False  # regard actif: awake, the head follows one face (the robot's camera, on the robot)
    idle_sleep_minutes: int = Field(default=3, ge=0, le=IDLE_SLEEP_MAX_MIN)  # 0 disables the idle sleep
    apps_model: AppModel = 'opus'
    apps_effort: AppEffort = 'high'  # a Snake: 187 s at high, 564 s at xhigh

    @field_validator('name')
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise PydanticCustomError('empty_name', 'le nom ne peut pas être vide')
        return v.strip()

    @field_validator('voice')
    @classmethod
    def _voice(cls, v: str) -> str:
        if v not in VOICES:
            raise _unknown('voix inconnue', v)
        return v

    @field_validator('idle_animation', 'listening_animation', 'talking_animation')
    @classmethod
    def _animation(cls, v: str, info: ValidationInfo) -> str:
        mode = str(info.field_name).removesuffix('_animation')
        if v not in anim.VARIANTS[mode]:
            raise _unknown(f'animation inconnue ({mode})', v)
        return v


class SettingsPatch(_Model):
    """PATCH /api/settings: only the changed fields. No rule here — `merge_settings` validates the result."""

    name: str | None = None
    voice: str | None = None
    personality: str | None = None
    idle_animation: str | None = None
    listening_animation: str | None = None
    talking_animation: str | None = None
    tuning: AnimationTuning | None = None
    web_search: bool | None = None
    gaze_tracking: bool | None = None
    idle_sleep_minutes: int | None = None
    apps_model: AppModel | None = None
    apps_effort: AppEffort | None = None


def merge_settings(current: Settings, patch: SettingsPatch) -> Settings:
    return Settings.model_validate({**current.model_dump(), **patch.model_dump(exclude_unset=True)})


class VoiceOption(_Model):
    name: str
    gender: Literal['female', 'male']
    descriptor: str


class AnimationOption(_Model):
    key: str
    label: str


class TuningRange(_Model):
    amplitude: tuple[float, float]
    speed: tuple[float, float]


class SettingsOptions(_Model):
    voices: list[VoiceOption]
    animations: dict[str, list[AnimationOption]]  # keyed by mode: idle, listening, talking
    tuning: dict[str, TuningRange]  # keyed by mode: the sliders' bounds


def settings_options() -> SettingsOptions:
    return SettingsOptions(
        voices=[VoiceOption(name=n, gender=g, descriptor=d) for n, (g, d) in sorted(VOICES.items())],
        animations={m: [AnimationOption(key=k, label=lbl) for k, lbl in anim.VARIANTS[m].items()] for m in anim.MODES},
        tuning={m: TuningRange(amplitude=anim.AMPLITUDE_RANGE[m], speed=anim.SPEED_RANGE) for m in anim.MODES},
    )


class SettingsView(_Model):
    """GET/PATCH /api/settings and the `settings` channel."""

    settings: Settings
    system_prompt: str  # identity + personality + protocol, without the web part
    web_prompt: str  # '' when web_search is off; the robot appends it only if the search tool is declared
    options: SettingsOptions


# ----- Robot -> Mac: events (POST /api/robot/events) ------------------------------------------

Phase = Literal['connecting', 'reconnecting', 'idle', 'listening', 'thinking', 'speaking', 'closing']
EndReason = Literal['goto_sleep', 'dashboard', 'idle', 'connection_lost', 'refused', 'shutdown', 'error']
ToolStatus = Literal['ok', 'error', 'refused', 'cancelled']


class Track(_Model):
    title: str
    artist: str  # '' when none: an artist played is the title alone
    uri: str | None = None


class ToolData(_Model):
    track: Track | None = None  # play_music, music
    image_base64: str | None = None  # look: the JPEG the model was shown
    volume: int | None = Field(default=None, ge=0, le=100)  # set_volume; music: Spotify's own


class _Event(_Model):
    session_id: str = Field(min_length=1)


class SessionStart(_Event):
    kind: Literal['session_start']


class SessionEnd(_Event):
    kind: Literal['session_end']
    reason: EndReason


class UserDelta(_Event):
    kind: Literal['user_delta']
    turn_id: str
    text: str


class UserEnd(_Event):
    kind: Literal['user_end']
    turn_id: str
    text: str
    typed: bool = False  # a dashboard `say`, not speech


class RobotDelta(_Event):
    kind: Literal['robot_delta']
    turn_id: str
    text: str


class RobotEnd(_Event):
    kind: Literal['robot_end']
    turn_id: str
    text: str


class ToolCall(_Event):
    kind: Literal['tool_call']
    call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResult(_Event):
    kind: Literal['tool_result']
    call_id: str
    name: str
    status: ToolStatus
    message: str  # French, what happened: the model got it, less what it was told to do about it
    data: ToolData = ToolData()


class PhaseChange(_Event):
    kind: Literal['phase']
    phase: Phase


class TokenCounts(_Model):
    text: int = Field(default=0, ge=0)
    audio: int = Field(default=0, ge=0)
    image: int = Field(default=0, ge=0)


class Usage(_Event):
    """One Gemini `usage_metadata`. The robot fills the tokens; the Mac adds the costs before broadcasting."""

    kind: Literal['usage']
    prompt: TokenCounts
    response: TokenCounts
    total_tokens: int = Field(ge=0)
    cost_usd: float | None = None
    session_cost_usd: float | None = None


class Latency(_Event):
    """From the end of the user's turn (VAD end, or a typed say sent) to its reply's first audio: none if unanswered."""

    kind: Literal['latency']
    turn_id: str
    ms: int = Field(ge=0)


class SceneEvent(_Event):
    """A scene played by the robot: started, each audio segment as it is heard (in a rehearsal, each turn of the
    human's), then how it ended."""

    kind: Literal['scene']
    scene_id: str
    title: str
    state: Literal['started', 'line', 'waiting', 'ended', 'stopped', 'failed']
    line: int | None = None  # line: the source line being heard; waiting: the first line the human says
    message: str | None = None  # stopped, failed: why, in French; waiting: the human's text


class ManetteEvent(_Event):
    """The robot as an app's controller: on (still, the profile's motors freed; said again in answer to each renewal or
    grant of it); returning (let go by the app, it waits, freed, to be released within reach and still before its torque
    comes back); off (taken back, and why); refused (why: scene while one plays, sleep while the conversation is
    ending)."""

    kind: Literal['manette']
    state: Literal['on', 'returning', 'off', 'refused']
    profile: ManetteProfile | None = None  # on: the one in force; returning: the one let go
    why: ManetteWhy | None = None  # off, refused


class ReplayEvent(_Event):
    """An app's `replay` (ReplayMotion) on the robot: started, as the taped stretch itself begins (`seconds` long,
    `loops` times, a 0.4 s seam between two); ended, however it ends, at once when there was nothing to play."""

    kind: Literal['replay']
    state: Literal['started', 'ended']
    seconds: float | None = None  # started
    loops: int | None = None  # started


class HushEvent(_Event):
    """The robot silent while an app people play among themselves is on screen (AppSummary.quiet): on once its
    answer is over (it hears nobody and says nothing); off, and why: the Mac (the app left, or the speech given back
    from the dashboard), an antenna held, a typed say."""

    kind: Literal['hush']
    on: bool
    why: Literal['mac', 'antenna', 'say'] | None = None  # off only


RobotEvent = Annotated[
    SessionStart
    | SessionEnd
    | UserDelta
    | UserEnd
    | RobotDelta
    | RobotEnd
    | ToolCall
    | ToolResult
    | PhaseChange
    | Usage
    | Latency
    | SceneEvent
    | ManetteEvent
    | ReplayEvent
    | HushEvent,
    Field(discriminator='kind'),
]


class Heartbeat(_Model):
    """POST /api/robot/heartbeat, every 3 s, asleep or awake."""

    awake: bool
    session_id: str | None = None
    antenna_wake: bool = True  # asleep, a wiggled antenna wakes it: not while a try, a scene or the sleep move moves it
    scene: str | None = None  # the scene it plays (SceneEvent.scene_id, started to its end): a lost event put right


class Voice(_Model):
    """POST /api/robot/voice, ~20 a second while a manette lends the robot's ear (« voix »), the latest only: how loud
    its microphone is over the room's own quiet, learnt before the hold. A number: no sound leaves the robot."""

    level: float = Field(ge=0, le=1)  # 0: the room's quiet; 1: 36 dB over it or more


# ----- Dashboard <-> Mac -----------------------------------------------------------------------

RobotStatus = Literal['off', 'idle', 'live']  # daemon down / `make run` not running / heartbeat fresh


class SayIn(_Model):
    text: str = Field(min_length=1, max_length=SAY_MAX)


class Volume(_Model):
    volume: int = Field(ge=0, le=100)


class SessionSnapshot(_Model):
    awake: bool
    session_id: str | None = None
    phase: Phase | None = None
    cost_usd: float = 0.0
    last_latency_ms: int | None = None
    manette: ManetteProfile | None = None  # an app holds the body as its controller: from its on to its off
    antenna_wake: bool = True  # asleep: Heartbeat.antenna_wake
    scene: str | None = None  # the scene the robot plays, awake or asleep: its `scene` events, each heartbeat
    hushed: bool = False  # silent while an app is played: from its hush on to its off


class RobotStatusOut(_Model):
    status: RobotStatus


class DashboardState(_Model):
    """GET /api/state: everything the dashboard re-reads after a (re)connect, settings aside."""

    robot_status: RobotStatus
    session: SessionSnapshot
    volume: int = Field(ge=0, le=100)


# ----- Studio: moves played at once, and the catalogue (palette) ------------------------------


class TryMove(_Model):
    """A move the real robot plays at once, awake or asleep, settings untouched (the studio's « Essayer »)."""

    kind: Literal['gesture', 'emotion', 'animation']
    name: str  # a gesture, an emotion, or (animation) a variant of `mode`
    mode: Literal['idle', 'listening', 'talking'] | None = None  # animation only
    tuning: Tuning = Tuning()  # animation only
    seconds: float = Field(default=6.0, ge=1.0, le=20.0)  # animation only

    @model_validator(mode='after')
    def _known(self) -> TryMove:
        if self.kind == 'gesture' and self.name not in anim.GESTURES:
            raise _unknown('geste inconnu', self.name)
        if self.kind == 'emotion' and self.name not in anim.EMOTIONS:
            raise _unknown('émotion inconnue', self.name)
        if self.kind == 'animation':
            if self.mode is None or self.name not in anim.VARIANTS[self.mode]:
                raise _unknown('animation inconnue', f'{self.mode} / {self.name}')
            AnimationTuning.model_validate({self.mode: self.tuning.model_dump()})
        return self


CatalogGroupKey = Literal['session', 'gestures', 'emotions', 'animations', 'scenes', 'apps', 'spotify', 'settings']


class SessionAction(_Model):
    kind: Literal['session']
    command: Literal['wake', 'sleep', 'restart']


class TryAction(_Model):
    kind: Literal['try']
    move: TryMove


class SceneAction(_Model):
    kind: Literal['scene']
    scene_id: str


class AppAction(_Model):
    kind: Literal['app']
    app_id: str | None = None  # None: the Apps surface opens on its field to describe a new app


class SayAction(_Model):
    """Typed in the conversation, as if said: needs an awake robot. Only for a sentence that takes no parameter."""

    kind: Literal['say']
    text: str = Field(min_length=1, max_length=SAY_MAX)


class ComposeAction(_Model):
    """The start of a sentence put in the conversation's composer, for the user to finish and send: nothing is sent."""

    kind: Literal['compose']
    text: str = Field(min_length=1, max_length=SAY_MAX)


# The settings drawer's fields, and the robot's volume (shown there too, applied at once).
SettingsField = Literal[
    'name', 'voice', 'personality', 'idle_animation', 'listening_animation', 'talking_animation', 'gaze_tracking',
    'web_search', 'idle_sleep_minutes', 'apps_model', 'apps_effort', 'volume',
]  # fmt: skip


class SettingsAction(_Model):
    kind: Literal['settings']
    field: SettingsField  # the drawer opens on it


CatalogAction = Annotated[
    SessionAction | TryAction | SceneAction | AppAction | SayAction | ComposeAction | SettingsAction,
    Field(discriminator='kind'),
]


class CatalogEntry(_Model):
    id: str
    group: CatalogGroupKey
    label: str
    description: str  # what it does, French
    say: str | None = None  # a sentence that gets it from the robot
    tool: str | None = None  # the robot tool behind it, if any
    action: CatalogAction
    preview: bool = False  # Shift+Enter: the studio plays it in 3D; a scene opens in its editor


class CatalogGroup(_Model):
    key: CatalogGroupKey
    label: str


class Catalog(_Model):
    """GET /api/catalog: everything the robot can do, from the code that does it."""

    groups: list[CatalogGroup]
    entries: list[CatalogEntry]


class PreviewFrames(_Model):
    """What the robot would show, sampled by its own motion code on the Mac (motion.rehearse): the 3D preview."""

    hz: int
    head: list[list[float]]  # one row-major 4x4 per frame, in the body's frame (the SDK's head pose)
    antennas: list[list[float]]  # [right, left] rad per frame
    body: list[float]  # body yaw, rad per frame


class PoseFrame(_Model):
    """The robot's measured pose, relayed from its daemon while a studio is open (the `pose` channel)."""

    t: float  # daemon timestamp, ms
    j: list[float] = Field(min_length=7, max_length=7)  # body yaw, stewart_1..6 (rad)
    m: list[float] = Field(min_length=16, max_length=16)  # head pose, row-major 4x4, world frame (body yaw in)
    a: list[float] = Field(min_length=2, max_length=2)  # antennas [right, left] rad


# ----- Scenes ----------------------------------------------------------------------------------


class SceneIn(_Model):
    title: str = Field(min_length=1, max_length=SCENE_TITLE_MAX)
    text: str = Field(max_length=SCENE_TEXT_MAX)


class SceneError(_Model):
    line: int = Field(ge=1)
    message: str  # French


class CastMember(_Model):
    name: str
    voice: str
    acting: str = ''
    angle: float | None = None  # @NOM (Voix, -60): the body yaw (+left) the robot turns to when that character speaks


class SceneCue(_Model):
    """One step of a parsed scene, in order. `say` is one segment: a block's words up to a pause; a move written
    among them comes after it and fires inside it."""

    line: int = Field(ge=1)  # where it is written
    end_line: int | None = None  # say: the last line its words are written on
    kind: Literal['say', 'emotion', 'gesture', 'turn', 'pause']
    character: str | None = None  # say
    text: str | None = None  # say: what is shown
    voice: str | None = None  # say
    style: str | None = None  # say: the cast's acting and the line's own direction
    key: str | None = None  # say: the audio cache key (model, voice, style, speech)
    ready: bool = False  # say: its voice is rendered (in the cache)
    name: str | None = None  # emotion, gesture
    degrees: float | None = None  # turn: absolute body yaw, +left
    seconds: float | None = None  # pause


class SceneCheck(_Model):
    """POST /api/scenes/check: the editor's live analysis. A scene with errors saves but does not play."""

    cast: list[CastMember]
    cues: list[SceneCue]
    errors: list[SceneError]
    warnings: list[SceneError] = Field(default_factory=list)  # changed, still playable: a turn clamped to ±100°
    segments: int  # audio segments to render
    rendered: int  # of which already in the cache


class SceneSummary(_Model):
    id: str
    title: str
    builtin: bool
    errors: int
    segments: int
    rendered: int


class SceneList(_Model):
    scenes: list[SceneSummary]


class SceneOut(_Model):
    id: str
    title: str
    text: str
    builtin: bool
    check: SceneCheck
    edited: bool = False  # a built-in saved over: DELETE brings back the original


class TimelineCue(_Model):
    """A cue at its offset from the start of the scene, in ms, laid out from each segment's measured audio."""

    at_ms: int = Field(ge=0)
    kind: Literal['audio', 'emotion', 'gesture', 'turn']
    line: int = Field(ge=1)
    key: str | None = None  # audio: GET /api/scenes/audio/{key}
    duration_ms: int | None = None  # audio
    character: str | None = None  # audio
    text: str | None = None  # audio
    name: str | None = None  # emotion, gesture
    degrees: float | None = None  # turn


class SceneOptions(_Model):
    """The optional body of POST /api/scenes/{id}/play; none is a play with every move."""

    moves: bool = True  # False: no emotion, gesture or turn
    rehearse: str | None = Field(default=None, max_length=40)  # a cast name: the human says that part, a tap goes on
    start: int | None = Field(default=None, ge=1)  # a line: the play starts at the voice written there, or after


class SceneTimeline(_Model):
    """A rehearsed character's segments last 0 ms and have no audio: the robot waits for the human instead."""

    scene_id: str
    title: str
    duration_ms: int = Field(ge=0)
    cues: list[TimelineCue]
    moves: bool = True
    rehearse: str | None = None
    start: int | None = None


class SceneWord(_Model):
    word: str  # what to type between the brackets, chevrons or parentheses
    label: str  # French, for the insert menu
    family: str = ''  # emotions: the feeling shown (« Colère »…), a heading of the insert menu, in its order


class SceneVocabulary(_Model):
    """GET /api/scenes/vocabulary: what a scene may contain, for the editor's insert menu."""

    emotions: list[SceneWord]
    gestures: list[SceneWord]
    turns: list[SceneWord]
    pauses: list[SceneWord]
    sounds: list[SceneWord]
    voices: list[SceneWord]


class SceneEmotions(_Model):
    """POST /api/scenes/emotions (a SceneIn): the draft with the emotions Claude chose written in, at places that
    change no word, so no voice; nothing is saved."""

    text: str = Field(max_length=SCENE_TEXT_MAX)
    added: int = Field(ge=0)


class SceneFind(_Model):
    """POST /api/scenes/find (robot, play_scene): the title as said; empty asks for the list; naming none
    (« celle-ci », « la scène ») means the scene a dashboard shows, else asks for the list too."""

    title: str = Field(default='', max_length=SCENE_TITLE_MAX)


class SceneFound(_Model):
    timeline: SceneTimeline | None = None  # None: `titles` lists what can be played
    titles: list[str] = Field(default_factory=list)


class SceneRender(_Model):
    """The `scene_render` channel: a scene's voices being rendered, by its latest render only."""

    scene_id: str
    done: int
    total: int
    error: str | None = None  # French: the render's first failed line, in every frame after it


class SceneRequest(_Model):
    """POST /api/scenes/write (dashboard) and /api/scenes/voice (the robot's `write_scene`): what the scene must be
    (« deux personnages débattent de la dualité onde-corpuscule »)."""

    request: str = Field(min_length=1, max_length=SCENE_REQUEST_MAX)


SceneStep = Literal['waiting', 'running', 'done', 'failed']


class SceneBuild(_Model):
    """A scene written at someone's ask: the `scene_build` channel (the latest only, at most 4 a second) and
    GET /api/scenes/builds. Claude writes the play (`write`), it is stored (`scene_id`), then its emotions (`stage`,
    Claude) and its voices (`voice`, Gemini) are prepared side by side. A scene written stays in the store however
    the rest ends."""

    id: str
    request: str
    voiced: bool = False  # asked by voice: the robot plays it once ready
    status: Literal['building', 'ready', 'failed', 'cancelled']
    error: str | None = None  # French: why it failed
    warning: str | None = None  # French: what it does without (its emotions), ready all the same
    started_at: float  # epoch seconds: the dashboard counts the time itself
    ended_at: float | None = None
    scene_id: str | None = None  # once written: the scene in the store
    title: str | None = None
    write: SceneStep = 'waiting'
    stage: SceneStep = 'waiting'
    voice: SceneStep = 'waiting'
    phase: Literal['starting', 'thinking', 'writing'] = 'starting'  # what Claude does while it writes
    thinking_tokens: int = 0  # estimated by Claude Code
    round: int = Field(default=1, ge=1, le=2)  # 2: written again, after `problems`
    problems: list[str] = Field(default_factory=list)  # French: why the first play went back
    text: str = ''  # the play so far, then as stored
    moves: int = 0  # the emotions written in
    voices_done: int = 0
    voices_total: int = 0
    cost_usd: float | None = None  # Claude's runs so far (API equivalent; the subscription pays)


class SceneBuilds(_Model):
    """GET /api/scenes/builds: the one in flight, then the last few ended, the latest first."""

    builds: list[SceneBuild]


class SceneAsked(_Model):
    """POST /api/scenes/voice: what the model is told (French), and the build it started, if any."""

    message: str
    build: SceneBuild | None = None


# ----- Apps ------------------------------------------------------------------------------------

AppStatus = Literal['building', 'ready', 'failed']


class AppSummary(_Model):
    id: str
    title: str
    status: AppStatus
    created_at: float  # epoch seconds
    updated_at: float
    error: str | None = None  # French: why the last build failed (a failed update leaves the app ready)
    quiet: bool = True  # played among people: the robot keeps silent while it is on screen (HushEvent)
    builtin: bool = False  # shipped with the robot (mac_server/assets/apps): never deleted
    edited: bool = False  # a shipped app you changed (name, silence, a retouch): DELETE brings it back as shipped


class AppList(_Model):
    apps: list[AppSummary]


class AppRequest(_Model):
    """POST /api/apps (create) and POST /api/apps/{id}/update: what the app should be or become."""

    request: str = Field(min_length=1, max_length=APP_REQUEST_MAX)


class AppRename(_Model):
    title: str = Field(min_length=1, max_length=APP_TITLE_MAX)


class AppQuiet(_Model):
    """POST /api/apps/{id}/quiet: whether the robot keeps silent while the app is on screen."""

    quiet: bool


class AppHtml(_Model):
    """GET /api/apps/{id}/html: the page, its CSP first in <head>; the dashboard runs it in a sandboxed srcdoc."""

    id: str
    html: str


class AppVoice(_Model):
    """POST /api/apps/voice (robot, the `app` tool): an app made, changed or opened by voice."""

    action: Literal['create', 'update', 'open']
    request: str = Field(default='', max_length=APP_REQUEST_MAX)  # create, update: what is asked
    name: str = Field(default='', max_length=APP_TITLE_MAX)  # update, open: the app as named


class AppVoiceOut(_Model):
    message: str  # French, for the model
    app: AppSummary | None = None


class AppEdit(_Model):
    """One targeted edit Claude made to a page: the text it replaced, and with what (each cut at 1,500 chars)."""

    old: str
    new: str


class AppBuild(_Model):
    """The `app_build` channel: a build under way, as Claude works; the latest only, at most 4 a second. It ends
    with the `apps` event that says the app is built (or not)."""

    app_id: str
    kind: Literal['create', 'update']
    # thinking (its text is never shown), writing (a creation: the page), reading and editing (a retouch, a
    # repair), checking (the scan and one run in headless Chrome)
    phase: Literal['starting', 'thinking', 'writing', 'reading', 'editing', 'checking']
    round: int = Field(default=1, ge=1, le=2)  # 2: a repair, after `problems`
    started_at: float  # epoch seconds: the dashboard counts the time itself
    thinking_tokens: int = 0  # estimated by Claude Code
    chars: int = 0  # of the page written so far (a creation)
    tail: str = ''  # its last lines: the code being written
    edits: list[AppEdit] = Field(default_factory=list)  # the last ones
    problems: list[str] = Field(default_factory=list)  # French: why the page went back for a repair
    cost_usd: float | None = None  # the runs finished so far (API equivalent; the subscription pays)


class AppEvent(_Model):
    """The `apps` channel: an app changed; `open` when the robot was asked to show it."""

    app: AppSummary
    open: bool = False
    deleted: bool = False


class EmotionMotion(_Model):
    kind: Literal['emotion']
    name: str

    @field_validator('name')
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in anim.EMOTIONS:
            raise _unknown('émotion inconnue', v)
        return v


class GestureMotion(_Model):
    kind: Literal['gesture']
    name: str

    @field_validator('name')
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in anim.GESTURES:
            raise _unknown('geste inconnu', v)
        return v


class LookMotion(_Model):
    """Page-centric degrees: yaw -30 is the page's left edge, +30 its right; pitch +20 its top."""

    kind: Literal['look']
    yaw: float = Field(ge=-30, le=30)
    pitch: float = Field(ge=-20, le=20)
    roll: float = Field(0, ge=-20, le=20)  # the pad's `head.roll`, as it is: a gesture read there plays back


class AntennasMotion(_Model):
    """Degrees off rest, + towards the face; the robot's own left and right."""

    kind: Literal['antennas']
    left: float = Field(ge=-60, le=60)
    right: float = Field(ge=-60, le=60)


class ReplayMotion(_Model):
    """What a hand made the robot do while this app held its body, played back as it was: the stretch between two
    times of the robot's clock (ms, as the pads carry them in `t`), `loops` times; 0 stops the one playing."""

    kind: Literal['replay']
    start: float = 0.0
    end: float = 0.0
    loops: int = Field(1, ge=0, le=8)


AppMotion = Annotated[
    EmotionMotion | GestureMotion | LookMotion | AntennasMotion | ReplayMotion, Field(discriminator='kind')
]


class ManetteIn(_Model):
    """POST /api/apps/{id}/manette (dashboard): hold the robot as this app's controller (`grant`: the human's yes, once;
    then renewed every second), or let go (None)."""

    profile: ManetteProfile | None
    grant: bool = False


class BridgeIn(_Model):
    """POST /api/apps/{id}/bridge: one message of an app's bridge (version 1), checked again by the Mac."""

    v: Literal[1]
    motion: AppMotion
    dashboard: str = Field(min_length=1, max_length=32)  # drawn by each dashboard as it loads: the robot follows one


# ----- Robot -> Mac: Spotify ------------------------------------------------------------------


PlayKind = Literal['track', 'artist', 'album', 'playlist', 'liked', 'audiobook', 'podcast']
MusicAction = Literal[
    'pause',
    'resume',
    'next',
    'previous',
    'now',
    'like',
    'queue',
    'seek',
    'shuffle',
    'repeat',
    'volume',
    'device',
    'playlist',
]


class PlayIn(_Model):
    query: str = Field(default='', max_length=200)  # required but for `liked`
    kind: PlayKind = 'track'

    @model_validator(mode='after')
    def _query(self) -> PlayIn:
        if self.kind != 'liked' and not self.query.strip():
            raise PydanticCustomError('empty_query', 'recherche\xa0: précise un titre, un artiste ou une ambiance')
        return self


class PlayOut(_Model):
    track: Track
    device: str  # name of the Spotify Connect device it plays on


AMOUNT_MAX_S = 86_400  # a seek of a day passes any track or chapter; beyond it, milliseconds overflow


class ControlIn(_Model):
    """POST /api/spotify/control (robot, the `music` tool): everything but starting something."""

    action: MusicAction
    value: str = Field(default='', max_length=200)  # queue: what; device: its name; playlist: its name; on/off
    # seek: seconds (negative: back), a day at most; volume: 0..100 (clamped)
    amount: float | None = Field(default=None, allow_inf_nan=False, ge=-AMOUNT_MAX_S, le=AMOUNT_MAX_S)


class ControlOut(_Model):
    message: str  # French, for the model
    track: Track | None = None
    device: str | None = None
    volume: int | None = Field(default=None, ge=0, le=100)


# ----- Mac -> robot: commands (GET /api/robot/commands, long-poll) -----------------------------


class Command(_Model):
    id: str
    action: Literal['wake', 'sleep', 'say', 'try', 'scene', 'scene_stop', 'app_motion', 'manette', 'hush']
    text: str | None = None  # say only
    move: TryMove | None = None  # try only
    scene: SceneTimeline | None = None  # scene only
    motion: AppMotion | None = None  # app_motion only
    manette: ManetteProfile | Literal['off'] | None = None  # manette only: a profile, or off
    grant: bool = False  # manette only: the human's yes (else a renewal, which only extends a hold in force)
    hush: bool | None = None  # hush only: silent while the app on screen is played, or speaking again
    queued_at: float  # Mac wall clock, epoch seconds; expiry (`ttl_s`) is enforced by the Mac
    ttl_s: float = COMMAND_TTL_S


class CommandPoll(_Model):
    command: Command | None = None


# ----- Mac -> dashboard: WebSocket /api/ws ----------------------------------------------------


class WsConversation(_Model):
    channel: Literal['conversation']
    data: RobotEvent


class WsSession(_Model):
    channel: Literal['session']
    data: SessionSnapshot


class WsRobotStatus(_Model):
    channel: Literal['robot_status']
    data: RobotStatusOut


class WsSettings(_Model):
    channel: Literal['settings']
    data: SettingsView


class WsVolume(_Model):
    channel: Literal['volume']
    data: Volume


class WsPose(_Model):
    """Only to the sockets that subscribed, each holding the latest frame: a slow one skips, never queues."""

    channel: Literal['pose']
    data: PoseFrame


class WsVoice(_Model):
    """Only to the sockets that subscribed (an app's frame holding a manette that listens)."""

    channel: Literal['voice']
    data: Voice


class WsSceneRender(_Model):
    channel: Literal['scene_render']
    data: SceneRender


class WsSceneBuild(_Model):
    channel: Literal['scene_build']
    data: SceneBuild


class WsAppBuild(_Model):
    channel: Literal['app_build']
    data: AppBuild


class WsApps(_Model):
    channel: Literal['apps']
    data: AppEvent


WsMessage = Annotated[
    WsConversation
    | WsSession
    | WsRobotStatus
    | WsSettings
    | WsVolume
    | WsPose
    | WsVoice
    | WsSceneRender
    | WsSceneBuild
    | WsApps
    | WsAppBuild,
    Field(discriminator='channel'),
]


class Showing(_Model):
    """What a dashboard shows in its workspace: the app or the scene a spoken « celle-ci » means."""

    kind: Literal['app', 'scene']
    id: str = Field(max_length=64)
    since: float = 0.0  # epoch seconds (the dashboard's clock): when it began showing it, kept across reconnects


class WsClientMessage(_Model):
    """The one message the dashboard sends on /api/ws, on every (re)connect and whenever either part changes: the
    channels it wants beyond the default ones, and what it shows."""

    subscribe: list[Literal['pose', 'voice']]
    showing: Showing | None = None
