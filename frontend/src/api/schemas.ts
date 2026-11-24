// Minimal mirrors of mac_server/contracts.py — the server's Pydantic models are the definition.
// frontend/tests/contracts.test.ts parses every example the server produces with these schemas.
import {z} from 'zod';

/** The robot as an app's controller: antennas as buttons; + the base freed; « paume »: the base freed, the antennas
 * lowered as in sleep; + the head freed. « voix »: its microphone's level lent too, or alone (nothing of the body is
 * lent). */
export const manetteProfileSchema = z.enum([
    'gachettes',
    'volant',
    'paume',
    'tete',
    'voix',
    'gachettes+voix',
    'volant+voix',
    'paume+voix',
    'tete+voix'
]);

// The limits of contracts.py that the forms enforce; tests/contracts.test.ts reads them there.
export const NAME_MAX = 40;
export const PERSONALITY_MAX = 1500;
export const SAY_MAX = 1000;
export const SCENE_REQUEST_MAX = 2000;
export const IDLE_SLEEP_MAX_MIN = 60;

export const phaseSchema = z.enum([
    'connecting',
    'reconnecting',
    'idle',
    'listening',
    'thinking',
    'speaking',
    'closing'
]);
export const robotStatusSchema = z.enum(['off', 'idle', 'live']);

const tuningSchema = z.object({amplitude: z.number(), speed: z.number()});
const range = z.tuple([z.number(), z.number()]);

export const settingsSchema = z.object({
    name: z.string(),
    voice: z.string(),
    personality: z.string(),
    idle_animation: z.string(),
    listening_animation: z.string(),
    talking_animation: z.string(),
    tuning: z.object({idle: tuningSchema, listening: tuningSchema, talking: tuningSchema}),
    web_search: z.boolean(),
    gaze_tracking: z.boolean(),
    idle_sleep_minutes: z.number().int(),
    apps_model: z.enum(['haiku', 'sonnet', 'opus']),
    apps_effort: z.enum(['low', 'medium', 'high', 'xhigh'])
});

const optionSchema = z.object({key: z.string(), label: z.string()});
const voiceSchema = z.object({name: z.string(), gender: z.enum(['female', 'male']), descriptor: z.string()});

export const settingsViewSchema = z.object({
    settings: settingsSchema,
    system_prompt: z.string(),
    web_prompt: z.string(),
    options: z.object({
        voices: z.array(voiceSchema),
        animations: z.record(z.string(), z.array(optionSchema)),
        tuning: z.record(z.string(), z.object({amplitude: range, speed: range}))
    })
});

const trackSchema = z.object({title: z.string(), artist: z.string(), uri: z.string().nullable()});
const toolDataSchema = z.object({
    track: trackSchema.nullable(),
    image_base64: z.string().nullable(),
    volume: z.number().nullable()
});
const tokensSchema = z.object({text: z.number(), audio: z.number(), image: z.number()});

const event = {session_id: z.string()};
const turn = {...event, turn_id: z.string(), text: z.string()};

export const robotEventSchema = z.discriminatedUnion('kind', [
    z.object({...event, kind: z.literal('session_start')}),
    z.object({...event, kind: z.literal('session_end'), reason: z.string()}),
    z.object({...turn, kind: z.literal('user_delta')}),
    z.object({...turn, kind: z.literal('user_end'), typed: z.boolean()}),
    z.object({...turn, kind: z.literal('robot_delta')}),
    z.object({...turn, kind: z.literal('robot_end')}),
    z.object({
        ...event,
        kind: z.literal('tool_call'),
        call_id: z.string(),
        name: z.string(),
        args: z.record(z.string(), z.unknown())
    }),
    z.object({
        ...event,
        kind: z.literal('tool_result'),
        call_id: z.string(),
        name: z.string(),
        status: z.enum(['ok', 'error', 'refused', 'cancelled']),
        message: z.string(),
        data: toolDataSchema
    }),
    z.object({...event, kind: z.literal('phase'), phase: phaseSchema}),
    z.object({
        ...event,
        kind: z.literal('usage'),
        prompt: tokensSchema,
        response: tokensSchema,
        total_tokens: z.number(),
        cost_usd: z.number().nullable(),
        session_cost_usd: z.number().nullable()
    }),
    z.object({...event, kind: z.literal('latency'), turn_id: z.string(), ms: z.number()}),
    z.object({
        ...event,
        kind: z.literal('scene'),
        scene_id: z.string(),
        title: z.string(),
        state: z.enum(['started', 'line', 'waiting', 'ended', 'stopped', 'failed']),
        line: z.number().nullable(),
        message: z.string().nullable()
    }),
    z.object({
        ...event,
        kind: z.literal('manette'),
        state: z.enum(['on', 'returning', 'off', 'refused']),
        profile: manetteProfileSchema.nullable(),
        why: z.enum(['app', 'lease', 'scene', 'sleep', 'profile', 'pose']).nullable()
    }),
    z.object({
        ...event,
        kind: z.literal('replay'),
        state: z.enum(['started', 'ended']),
        seconds: z.number().nullable(),
        loops: z.number().nullable()
    }),
    z.object({...event, kind: z.literal('hush'), on: z.boolean(), why: z.enum(['mac', 'antenna', 'say']).nullable()})
]);

export const sessionSnapshotSchema = z.object({
    awake: z.boolean(),
    session_id: z.string().nullable(),
    phase: phaseSchema.nullable(),
    cost_usd: z.number(),
    last_latency_ms: z.number().nullable(),
    manette: manetteProfileSchema.nullable(),
    antenna_wake: z.boolean(), // asleep: a wiggled antenna wakes it (not while a try, a scene or the sleep move moves it)
    scene: z.string().nullable(), // the scene the robot plays, awake or asleep
    hushed: z.boolean() // silent while an app is played (its hush events)
});

export const volumeSchema = z.object({volume: z.number().int().min(0).max(100)});

export const dashboardStateSchema = z.object({
    robot_status: robotStatusSchema,
    session: sessionSnapshotSchema,
    volume: z.number().int()
});

const matrix = z.array(z.number()).length(16);

export const previewFramesSchema = z.object({
    hz: z.number(),
    head: z.array(matrix),
    antennas: z.array(z.tuple([z.number(), z.number()])),
    body: z.array(z.number())
});

export const poseFrameSchema = z.object({
    t: z.number(),
    j: z.array(z.number()).length(7),
    m: matrix,
    a: z.tuple([z.number(), z.number()])
});

const sceneCueSchema = z.object({
    line: z.number(),
    end_line: z.number().nullable(),
    kind: z.enum(['say', 'emotion', 'gesture', 'turn', 'pause']),
    character: z.string().nullable(),
    text: z.string().nullable(),
    voice: z.string().nullable(),
    style: z.string().nullable(),
    key: z.string().nullable(),
    ready: z.boolean(),
    name: z.string().nullable(),
    degrees: z.number().nullable(),
    seconds: z.number().nullable()
});

export const sceneCheckSchema = z.object({
    cast: z.array(z.object({name: z.string(), voice: z.string(), acting: z.string(), angle: z.number().nullable()})),
    cues: z.array(sceneCueSchema),
    errors: z.array(z.object({line: z.number(), message: z.string()})),
    warnings: z.array(z.object({line: z.number(), message: z.string()})),
    segments: z.number(),
    rendered: z.number()
});

const sceneSummarySchema = z.object({
    id: z.string(),
    title: z.string(),
    builtin: z.boolean(),
    errors: z.number(),
    segments: z.number(),
    rendered: z.number()
});

export const sceneListSchema = z.object({scenes: z.array(sceneSummarySchema)});

export const sceneOutSchema = z.object({
    id: z.string(),
    title: z.string(),
    text: z.string(),
    builtin: z.boolean(),
    check: sceneCheckSchema,
    edited: z.boolean()
});

/** The optional body of POST /api/scenes/{id}/play (the dashboard sends it, never reads it). */
export interface SceneOptions {
    moves: boolean;
    rehearse: string | null;
    start?: number | null; // a line: the play starts at the voice written there
}

export const sceneEmotionsSchema = z.object({text: z.string(), added: z.number()});

const wordsSchema = z.array(z.object({word: z.string(), label: z.string(), family: z.string()}));
export const sceneVocabularySchema = z.object({
    emotions: wordsSchema,
    gestures: wordsSchema,
    turns: wordsSchema,
    pauses: wordsSchema,
    sounds: wordsSchema,
    voices: wordsSchema
});

const sceneRenderSchema = z.object({
    scene_id: z.string(),
    done: z.number(),
    total: z.number(),
    error: z.string().nullable()
});

const sceneStepSchema = z.enum(['waiting', 'running', 'done', 'failed']);
/** A scene written at someone's ask: Claude writes the play, then its emotions and its voices side by side. */
export const sceneBuildSchema = z.object({
    id: z.string(),
    request: z.string(),
    voiced: z.boolean(),
    status: z.enum(['building', 'ready', 'failed', 'cancelled']),
    error: z.string().nullable(),
    warning: z.string().nullable(),
    started_at: z.number(),
    ended_at: z.number().nullable(),
    scene_id: z.string().nullable(),
    title: z.string().nullable(),
    write: sceneStepSchema,
    stage: sceneStepSchema,
    voice: sceneStepSchema,
    phase: z.enum(['starting', 'thinking', 'writing']),
    thinking_tokens: z.number(),
    round: z.number().int(),
    problems: z.array(z.string()),
    text: z.string(),
    moves: z.number(),
    voices_done: z.number(),
    voices_total: z.number(),
    cost_usd: z.number().nullable()
});
export const sceneBuildsSchema = z.object({builds: z.array(sceneBuildSchema)});

export const appSummarySchema = z.object({
    id: z.string(),
    title: z.string(),
    status: z.enum(['building', 'ready', 'failed']),
    created_at: z.number(),
    updated_at: z.number(),
    error: z.string().nullable(),
    quiet: z.boolean(), // played among people: the robot keeps silent while it is on screen
    builtin: z.boolean(), // shipped with the robot: never deleted
    edited: z.boolean() // a shipped app changed: deleting it brings it back as shipped
});

export const appListSchema = z.object({apps: z.array(appSummarySchema)});
export const appHtmlSchema = z.object({id: z.string(), html: z.string()});
const appEventSchema = z.object({app: appSummarySchema, open: z.boolean(), deleted: z.boolean()});
export const appBuildSchema = z.object({
    app_id: z.string(),
    kind: z.enum(['create', 'update']),
    phase: z.enum(['starting', 'thinking', 'writing', 'reading', 'editing', 'checking']),
    round: z.number(),
    started_at: z.number(),
    thinking_tokens: z.number(),
    chars: z.number(),
    tail: z.string(),
    edits: z.array(z.object({old: z.string(), new: z.string()})),
    problems: z.array(z.string()),
    cost_usd: z.number().nullable()
});

// The names robot_agent/animation_names.py defines (tests/apps.test.ts reads them there).
const words = (text: string) => text.split(' ') as [string, ...string[]];
export const EMOTIONS = words(
    'cheerful1 enthusiastic1 laughing1 loving1 grateful1 proud1 success1 amazed1 surprised1 curious1 inquiring1 thoughtful1 understanding1 confused1 uncertain1 oops1 shy1 sad1 tired1 boredom1 fear1 welcoming1 yes1 no1 dance1'
);
export const GESTURES = words('nod shake tilt_left tilt_right look_up look_down wiggle bow think surprise dance');

/** What an app may send through the bridge (version 1): checked here, then again by the Mac. `manette` asks for the
 * robot as its controller (a profile) or lets it go (null): never passed to the Mac without the human's OK. */
export const bridgeMessageSchema = z.discriminatedUnion('type', [
    z.object({reachy: z.literal(1), type: z.literal('manette'), profile: manetteProfileSchema.nullable()}),
    z.object({reachy: z.literal(1), type: z.literal('emotion'), name: z.enum(EMOTIONS)}),
    z.object({reachy: z.literal(1), type: z.literal('gesture'), name: z.enum(GESTURES)}),
    z.object({
        reachy: z.literal(1),
        type: z.literal('look'),
        yaw: z.number().min(-30).max(30),
        pitch: z.number().min(-20).max(20),
        roll: z.number().min(-20).max(20).optional()
    }),
    z.object({
        reachy: z.literal(1),
        type: z.literal('antennas'),
        left: z.number().min(-60).max(60),
        right: z.number().min(-60).max(60)
    }),
    z.object({
        reachy: z.literal(1),
        type: z.literal('replay'),
        start: z.number().optional(),
        end: z.number().optional(),
        loops: z.number().int().min(0).max(8)
    })
]);

export const wsMessageSchema = z.discriminatedUnion('channel', [
    z.object({channel: z.literal('conversation'), data: robotEventSchema}),
    z.object({channel: z.literal('session'), data: sessionSnapshotSchema}),
    z.object({channel: z.literal('robot_status'), data: z.object({status: robotStatusSchema})}),
    z.object({channel: z.literal('settings'), data: settingsViewSchema}),
    z.object({channel: z.literal('volume'), data: volumeSchema}),
    z.object({channel: z.literal('pose'), data: poseFrameSchema}),
    z.object({channel: z.literal('voice'), data: z.object({level: z.number().min(0).max(1)})}),
    z.object({channel: z.literal('scene_render'), data: sceneRenderSchema}),
    z.object({channel: z.literal('scene_build'), data: sceneBuildSchema}),
    z.object({channel: z.literal('apps'), data: appEventSchema}),
    z.object({channel: z.literal('app_build'), data: appBuildSchema})
]);

export const tryMoveSchema = z.object({
    kind: z.enum(['gesture', 'emotion', 'animation']),
    name: z.string(),
    mode: z.enum(['idle', 'listening', 'talking']).nullable(),
    tuning: tuningSchema,
    seconds: z.number()
});

export const settingsFieldSchema = z.enum([
    'name',
    'voice',
    'personality',
    'idle_animation',
    'listening_animation',
    'talking_animation',
    'gaze_tracking',
    'web_search',
    'idle_sleep_minutes',
    'apps_model',
    'apps_effort',
    'volume'
]);

const catalogActionSchema = z.discriminatedUnion('kind', [
    z.object({kind: z.literal('session'), command: z.enum(['wake', 'sleep', 'restart'])}),
    z.object({kind: z.literal('try'), move: tryMoveSchema}),
    z.object({kind: z.literal('scene'), scene_id: z.string()}),
    z.object({kind: z.literal('app'), app_id: z.string().nullable()}),
    z.object({kind: z.literal('say'), text: z.string()}),
    z.object({kind: z.literal('compose'), text: z.string()}),
    z.object({kind: z.literal('settings'), field: settingsFieldSchema})
]);

export const catalogSchema = z.object({
    groups: z.array(z.object({key: z.string(), label: z.string()})),
    entries: z.array(
        z.object({
            id: z.string(),
            group: z.string(),
            label: z.string(),
            description: z.string(),
            say: z.string().nullable(),
            tool: z.string().nullable(),
            action: catalogActionSchema,
            preview: z.boolean()
        })
    )
});

// The dashboard's types are these inferences; nothing re-declares a contract by hand.
export type Phase = z.infer<typeof phaseSchema>;
export type RobotStatus = z.infer<typeof robotStatusSchema>;
export type Settings = z.infer<typeof settingsSchema>;
export type SettingsView = z.infer<typeof settingsViewSchema>;
export type VoiceOption = z.infer<typeof voiceSchema>;
export type Track = z.infer<typeof trackSchema>;
export type RobotEvent = z.infer<typeof robotEventSchema>;
export type ToolStatus = Extract<RobotEvent, {kind: 'tool_result'}>['status'];
export type SessionSnapshot = z.infer<typeof sessionSnapshotSchema>;
export type DashboardState = z.infer<typeof dashboardStateSchema>;
export type WsMessage = z.infer<typeof wsMessageSchema>;
export type TryMove = z.infer<typeof tryMoveSchema>;
export type Catalog = z.infer<typeof catalogSchema>;
export type CatalogEntry = Catalog['entries'][number];
export type CatalogAction = CatalogEntry['action'];
export type SettingsField = z.infer<typeof settingsFieldSchema>;
export type PreviewFrames = z.infer<typeof previewFramesSchema>;
export type PoseFrame = z.infer<typeof poseFrameSchema>;
export type SceneCheck = z.infer<typeof sceneCheckSchema>;
export type SceneList = z.infer<typeof sceneListSchema>;
export type SceneOut = z.infer<typeof sceneOutSchema>;
export type SceneVocabulary = z.infer<typeof sceneVocabularySchema>;
export type SceneRender = z.infer<typeof sceneRenderSchema>;
export type SceneBuild = z.infer<typeof sceneBuildSchema>;
export type AppSummary = z.infer<typeof appSummarySchema>;
export type AppEvent = z.infer<typeof appEventSchema>;
export type AppBuild = z.infer<typeof appBuildSchema>;
export type ManetteProfile = z.infer<typeof manetteProfileSchema>;
export type BridgeMessage = z.infer<typeof bridgeMessageSchema>;
