// The dashboard's Zod schemas against payloads the server itself produced
// (mac_server/contracts.py examples(), exported by scripts/export_contract_examples.py), and against every
// value of the server's enums and the limits the forms enforce, read from contracts.py.
import {readFileSync} from 'node:fs';
import {describe, expect, it} from 'vitest';
import {z, type ZodTypeAny} from 'zod';
import examples from './contract-examples.json';
import {
    appBuildSchema,
    appHtmlSchema,
    appListSchema,
    appSummarySchema,
    catalogSchema,
    previewFramesSchema,
    sceneBuildSchema,
    sceneBuildsSchema,
    sceneCheckSchema,
    sceneEmotionsSchema,
    sceneListSchema,
    sceneOutSchema,
    sceneVocabularySchema,
    dashboardStateSchema,
    IDLE_SLEEP_MAX_MIN,
    manetteProfileSchema,
    NAME_MAX,
    PERSONALITY_MAX,
    phaseSchema,
    robotEventSchema,
    robotStatusSchema,
    SAY_MAX,
    SCENE_REQUEST_MAX,
    sessionSnapshotSchema,
    settingsFieldSchema,
    settingsSchema,
    settingsViewSchema,
    tryMoveSchema,
    volumeSchema,
    wsMessageSchema
} from '../src/api/schemas';

type Json = Record<string, unknown>;

const schemas: Record<string, ZodTypeAny> = {
    AppHtml: appHtmlSchema,
    AppList: appListSchema,
    Catalog: catalogSchema,
    PreviewFrames: previewFramesSchema,
    SceneBuild: sceneBuildSchema,
    SceneBuilds: sceneBuildsSchema,
    SceneCheck: sceneCheckSchema,
    SceneEmotions: sceneEmotionsSchema,
    SceneList: sceneListSchema,
    SceneOut: sceneOutSchema,
    SceneVocabulary: sceneVocabularySchema,
    SettingsView: settingsViewSchema,
    RobotEvent: robotEventSchema,
    SessionSnapshot: sessionSnapshotSchema,
    DashboardState: dashboardStateSchema,
    Volume: volumeSchema,
    WsMessage: wsMessageSchema
};
const examplesOf = (name: string) => (examples as Record<string, Json[]>)[name];

const expectParses = (schema: ZodTypeAny, value: unknown) => {
    const parsed = schema.safeParse(value);
    expect(parsed.success, `${JSON.stringify(value)}: ${JSON.stringify(parsed.error?.issues)}`).toBe(true);
};

describe('server examples', () => {
    it('cover every schema the dashboard reads', () => {
        expect(Object.keys(examples).sort()).toEqual(Object.keys(schemas).sort());
    });

    for (const [name, schema] of Object.entries(schemas)) {
        it(`${name} parses every example`, () => {
            const list = examplesOf(name);
            expect(list.length).toBeGreaterThan(0);
            for (const example of list) expectParses(schema, example);
        });
    }

    it('reads a tool result track from its structured field', () => {
        const event = robotEventSchema.parse(
            examplesOf('RobotEvent').find((e) => e.kind === 'tool_result' && e.name === 'play_music')
        );
        expect(event.kind === 'tool_result' && event.data.track?.title).toBe('One More Time');
    });
});

// The examples carry one value of each enum, so a value known to one side only passes them, and the dashboard
// drops every message that carries it. The server's values come from contracts.py itself: each Literal, by its
// alias (`Phase`) or by its model's field (`SceneEvent.state`), and the members of each discriminated union.
const contracts = readFileSync(new URL('../../mac_server/contracts.py', import.meta.url), 'utf8');
const LITERALS = new Map<string, string[]>();
let model = '';
for (const [, owner, indent, name, values] of contracts.matchAll(
    /^class (\w+)\(|^( *)(\w+)\s*[:=][^\n]*?Literal\[([^\]]*)\]/gm
)) {
    if (owner) {
        model = owner; // the fields that follow are its own
        continue;
    }
    const quoted = [...values.matchAll(/'([^']*)'|"([^"]*)"/g)].map((q) => q[1] ?? q[2]);
    LITERALS.set(indent ? `${model}.${name}` : name, quoted);
}
const literal = (name: string): string[] => {
    const values = LITERALS.get(name);
    if (!values) throw new Error(`no ${name} Literal in mac_server/contracts.py`);
    return values;
};
/** Each discriminated union of contracts.py, and its members' values of the discriminator. */
const UNIONS = new Map(
    [...contracts.matchAll(/^(\w+) = Annotated\[([^,]*),\s*Field\(discriminator='(\w+)'\)/gm)].map(
        ([, alias, members, key]) => [alias, members.split('|').flatMap((member) => literal(`${member.trim()}.${key}`))]
    )
);
const sorted = (values: Iterable<unknown>) => [...values].map(String).sort();

type Path = (string | number)[];
type Carrier = [schema: string, carries: (example: Json) => boolean, path: Path];
const always = () => true;
/** The index of the catalogue example's first entry whose action is of that kind. */
const entry = (kind: string): number => {
    const index = (examplesOf('Catalog')[0].entries as Json[]).findIndex((e) => (e.action as Json).kind === kind);
    if (index < 0) throw new Error(`no catalogue entry whose action is ${kind}`);
    return index;
};
const catalogActionSchema = catalogSchema.shape.entries.element.shape.action;
type Union = z.ZodDiscriminatedUnion<string, z.ZodDiscriminatedUnionOption<string>[]>;
/** The Zod schema of a field of one member of a discriminated union. */
const field = (union: Union, member: string, name: string): ZodTypeAny => {
    const found = union.optionsMap.get(member);
    if (!found) throw new Error(`the dashboard reads no ${member}`);
    return found.shape[name];
};

/** Each enum the dashboard parses: the Zod schema it reads it with (null: a plain string), and where it travels (the
 * example that carries it, and the path to its field). */
const ENUMS: Record<string, {zod: ZodTypeAny | null; carriers: Carrier[]}> = {
    Phase: {
        zod: phaseSchema,
        carriers: [
            ['RobotEvent', (e) => e.kind === 'phase', ['phase']],
            ['SessionSnapshot', (s) => s.phase !== null, ['phase']],
            ['DashboardState', always, ['session', 'phase']],
            ['WsMessage', (m) => m.channel === 'session', ['data', 'phase']]
        ]
    },
    ToolStatus: {
        zod: field(robotEventSchema, 'tool_result', 'status'),
        carriers: [['RobotEvent', (e) => e.kind === 'tool_result', ['status']]]
    },
    EndReason: {zod: null, carriers: [['RobotEvent', (e) => e.kind === 'session_end', ['reason']]]},
    RobotStatus: {
        zod: robotStatusSchema,
        carriers: [
            ['DashboardState', always, ['robot_status']],
            ['WsMessage', (m) => m.channel === 'robot_status', ['data', 'status']]
        ]
    },
    ManetteProfile: {
        zod: manetteProfileSchema,
        carriers: [
            ['RobotEvent', (e) => e.kind === 'manette', ['profile']],
            ['SessionSnapshot', always, ['manette']],
            ['DashboardState', always, ['session', 'manette']],
            ['WsMessage', (m) => m.channel === 'session', ['data', 'manette']]
        ]
    },
    ManetteWhy: {
        zod: field(robotEventSchema, 'manette', 'why'),
        carriers: [['RobotEvent', (e) => e.kind === 'manette', ['why']]]
    },
    'ManetteEvent.state': {
        zod: field(robotEventSchema, 'manette', 'state'),
        carriers: [['RobotEvent', (e) => e.kind === 'manette', ['state']]]
    },
    'ReplayEvent.state': {
        zod: field(robotEventSchema, 'replay', 'state'),
        carriers: [['RobotEvent', (e) => e.kind === 'replay', ['state']]]
    },
    'HushEvent.why': {
        zod: field(robotEventSchema, 'hush', 'why'),
        carriers: [['RobotEvent', (e) => e.kind === 'hush' && e.why !== null, ['why']]]
    },
    'SceneEvent.state': {
        zod: field(robotEventSchema, 'scene', 'state'),
        carriers: [['RobotEvent', (e) => e.kind === 'scene', ['state']]]
    },
    AppModel: {
        zod: settingsSchema.shape.apps_model,
        carriers: [
            ['SettingsView', always, ['settings', 'apps_model']],
            ['WsMessage', (m) => m.channel === 'settings', ['data', 'settings', 'apps_model']]
        ]
    },
    AppEffort: {
        zod: settingsSchema.shape.apps_effort,
        carriers: [
            ['SettingsView', always, ['settings', 'apps_effort']],
            ['WsMessage', (m) => m.channel === 'settings', ['data', 'settings', 'apps_effort']]
        ]
    },
    'VoiceOption.gender': {
        zod: settingsViewSchema.shape.options.shape.voices.element.shape.gender,
        carriers: [
            ['SettingsView', always, ['options', 'voices', 0, 'gender']],
            ['WsMessage', (m) => m.channel === 'settings', ['data', 'options', 'voices', 0, 'gender']]
        ]
    },
    AppStatus: {
        zod: appSummarySchema.shape.status,
        carriers: [
            ['AppList', always, ['apps', 0, 'status']],
            ['WsMessage', (m) => m.channel === 'apps', ['data', 'app', 'status']]
        ]
    },
    'AppBuild.kind': {
        zod: appBuildSchema.shape.kind,
        carriers: [['WsMessage', (m) => m.channel === 'app_build', ['data', 'kind']]]
    },
    'AppBuild.phase': {
        zod: appBuildSchema.shape.phase,
        carriers: [['WsMessage', (m) => m.channel === 'app_build', ['data', 'phase']]]
    },
    'SceneBuild.status': {
        zod: sceneBuildSchema.shape.status,
        carriers: [
            ['SceneBuild', always, ['status']],
            ['SceneBuilds', always, ['builds', 0, 'status']],
            ['WsMessage', (m) => m.channel === 'scene_build', ['data', 'status']]
        ]
    },
    'SceneBuild.phase': {
        zod: sceneBuildSchema.shape.phase,
        carriers: [['SceneBuild', always, ['phase']]]
    },
    SceneStep: {
        zod: sceneBuildSchema.shape.write,
        carriers: [
            ['SceneBuild', always, ['write']],
            ['SceneBuild', always, ['stage']],
            ['SceneBuild', always, ['voice']]
        ]
    },
    'SceneCue.kind': {
        zod: sceneCheckSchema.shape.cues.element.shape.kind,
        carriers: [
            ['SceneCheck', always, ['cues', 0, 'kind']],
            ['SceneOut', always, ['check', 'cues', 0, 'kind']]
        ]
    },
    CatalogGroupKey: {
        zod: null,
        carriers: [
            ['Catalog', always, ['groups', 0, 'key']],
            ['Catalog', always, ['entries', 0, 'group']]
        ]
    },
    'SessionAction.command': {
        zod: field(catalogActionSchema, 'session', 'command'),
        carriers: [['Catalog', always, ['entries', entry('session'), 'action', 'command']]]
    },
    'TryMove.kind': {
        zod: tryMoveSchema.shape.kind,
        carriers: [['Catalog', always, ['entries', entry('try'), 'action', 'move', 'kind']]]
    },
    'TryMove.mode': {
        zod: tryMoveSchema.shape.mode,
        carriers: [['Catalog', always, ['entries', entry('try'), 'action', 'move', 'mode']]]
    },
    SettingsField: {
        zod: settingsFieldSchema,
        carriers: [['Catalog', always, ['entries', entry('settings'), 'action', 'field']]]
    }
};
// The robot or the Mac reads these: the dashboard sends them, or never sees them.
const NOT_PARSED = [
    'AppVoice.action',
    'Command.action',
    'MusicAction',
    'PlayKind',
    'Showing.kind',
    'TimelineCue.kind',
    'WsClientMessage.subscribe'
];

/** Each discriminated union the dashboard parses, and its Zod counterpart. */
const READ_UNIONS: Record<string, Union> = {
    RobotEvent: robotEventSchema,
    WsMessage: wsMessageSchema,
    CatalogAction: catalogActionSchema
};
const UNREAD_UNIONS = ['AppMotion']; // an app's moves: the Mac checks them

const withValue = (example: Json, path: Path, value: string): Json => {
    const copy = structuredClone(example);
    const parent = path.slice(0, -1).reduce((node, key) => node[key] as Json, copy as Record<string | number, unknown>);
    parent[path[path.length - 1]] = value;
    return copy;
};

const zodValues = (schema: ZodTypeAny): string[] => {
    const inner = schema instanceof z.ZodNullable ? schema.unwrap() : schema;
    expect(inner).toBeInstanceOf(z.ZodEnum);
    return sorted((inner as z.ZodEnum<[string, ...string[]]>).options);
};

describe('server enums', () => {
    it('each of contracts.py is parsed by the dashboard, or listed as never seen by it', () => {
        const enums = [...LITERALS].filter(([, values]) => values.length > 1).map(([name]) => name);
        expect(sorted(enums)).toEqual(sorted([...Object.keys(ENUMS), ...NOT_PARSED]));
        expect(sorted(UNIONS.keys())).toEqual(sorted([...Object.keys(READ_UNIONS), ...UNREAD_UNIONS]));
    });

    for (const [name, {carriers}] of Object.entries(ENUMS)) {
        it(`every ${name} of contracts.py parses wherever the dashboard reads it`, () => {
            const values = literal(name);
            expect(values.length).toBeGreaterThan(1);
            for (const [schema, carries, path] of carriers) {
                const example = examplesOf(schema).find(carries);
                expect(example, `an example of ${schema} that carries ${name}`).toBeDefined();
                for (const value of values) {
                    const variant = withValue(example as Json, path, value);
                    expectParses(schemas[schema], variant);
                    // A robot event reaches the dashboard inside a `conversation` message.
                    if (schema === 'RobotEvent')
                        expectParses(wsMessageSchema, {channel: 'conversation', data: variant});
                }
            }
        });
    }

    it('the dashboard knows no value the server does not define', () => {
        for (const [name, {zod}] of Object.entries(ENUMS)) {
            if (zod) expect(zodValues(zod), name).toEqual(sorted(literal(name)));
        }
    });

    it('the dashboard reads every member of each union, and no other', () => {
        for (const [alias, union] of Object.entries(READ_UNIONS)) {
            expect(sorted(union.optionsMap.keys()), alias).toEqual(sorted(UNIONS.get(alias) ?? []));
        }
    });
});

describe('server limits', () => {
    const constant = (name: string): number => {
        const found = new RegExp(`^${name}\\s*:\\s*Final\\s*=\\s*([\\d_]+)\\s*$`, 'm').exec(contracts);
        if (!found) throw new Error(`no "${name}: Final = <integer>" in mac_server/contracts.py`);
        return Number(found[1].replaceAll('_', ''));
    };

    it('the forms stop where the Mac does', () => {
        for (const [name, value] of Object.entries({
            NAME_MAX,
            PERSONALITY_MAX,
            SAY_MAX,
            SCENE_REQUEST_MAX,
            IDLE_SLEEP_MAX_MIN
        })) {
            expect(value, name).toBe(constant(name));
        }
    });
});
