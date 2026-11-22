// @vitest-environment happy-dom
// A scene asked for: what its steps say, the view that shows them as the Mac prepares it, and the dialog around it
// (the request field, the build in the list, the scene played or opened once ready), against the Mac's real payloads.
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {
    sceneBuildSchema,
    sceneCheckSchema,
    sceneOutSchema,
    sceneVocabularySchema,
    type SceneBuild
} from '../src/api/schemas';
import {ApiError} from '../src/api/client';
import {cast} from '../src/scenes/text';
import {said, steps, Writing} from '../src/scenes/Writing';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const [WRITING, READY, REFUSED] = examples.SceneBuild.map((build) => sceneBuildSchema.parse(build));
const STORED: SceneBuild = {
    ...WRITING,
    write: 'done',
    stage: 'running',
    voice: 'running',
    scene_id: 'deux-fentes',
    title: 'Les deux fentes',
    voices_done: 7,
    voices_total: 19
};
const LISTED = [
    {id: 'fete-surprise', title: 'La fête surprise', builtin: true, errors: 0, segments: 1, rendered: 1},
    {id: 'deux-fentes', title: 'Les deux fentes', builtin: false, errors: 0, segments: 19, rendered: 7}
];

const mocks = vi.hoisted(() => ({
    list: vi.fn(),
    get: vi.fn(),
    write: vi.fn(),
    cancel: vi.fn(),
    builds: vi.fn(),
    playScene: vi.fn(),
    frame: null as ((build: unknown) => void) | null
}));
vi.mock('../src/scenes/api', () => ({
    scenesApi: {
        list: mocks.list,
        get: mocks.get,
        vocabulary: vi.fn(async () => sceneVocabularySchema.parse(examples.SceneVocabulary[0])),
        check: vi.fn(async () => sceneCheckSchema.parse(examples.SceneCheck[0])),
        write: mocks.write,
        cancel: mocks.cancel,
        builds: mocks.builds
    }
}));
vi.mock('../src/api/client', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../src/api/client')>()),
    api: {playScene: mocks.playScene, stopScene: vi.fn()}
}));
vi.mock('../src/live', () => {
    const session = {awake: true, session_id: 's', phase: 'idle', cost_usd: 0, last_latency_ms: null};
    const live = {
        link: 'up',
        session: {...session, manette: null, antenna_wake: true, scene: null},
        on: (channel: string, listener: (build: unknown) => void) => {
            if (channel === 'scene_build') mocks.frame = listener;
            return () => {};
        },
        onConversation: () => () => {}
    };
    return {useLive: () => live, useShowing: () => {}};
});

let root: Root | null = null;
const mount = async (element: ReturnType<typeof createElement>) => {
    if (!root) {
        const host = document.createElement('div');
        document.body.append(host);
        root = createRoot(host);
    }
    await act(async () => root?.render(element));
};
afterEach(async () => {
    await act(async () => root?.unmount());
    root = null;
    document.body.innerHTML = '';
    vi.clearAllMocks();
});
const button = (text: string) =>
    [...document.querySelectorAll<HTMLElement>('button, [role="button"]')].find((el) =>
        el.textContent?.startsWith(text)
    );
const click = (text: string) => act(async () => button(text)?.click());
const states = () =>
    [...document.querySelectorAll<HTMLElement>('[data-step]')].map((el) => `${el.dataset.step}:${el.dataset.state}`);
const page = () => document.body.textContent ?? '';

describe('what a build says', () => {
    it('reads the cast as written: each name once, with its voice', () => {
        const text =
            '@NARRATEUR : conteur\n@Alice (Kore, 35) : vive\n@BOB (Puck) : lent\n@ALICE (Leda) : encore\n\nALICE : Oui.';
        expect(cast(text)).toEqual([
            {name: 'NARRATEUR', voice: ''},
            {name: 'ALICE', voice: 'Kore'},
            {name: 'BOB', voice: 'Puck'}
        ]);
    });

    it('words each step as it goes: waiting, at work, done', () => {
        expect(steps({...WRITING, phase: 'thinking', text: ''}).map((s) => [s.state, s.detail])).toEqual([
            ['running', 'Claude réfléchit (~1,2k\u00a0jetons)'],
            ['waiting', 'Après l’écriture'],
            ['waiting', 'Après l’écriture']
        ]);
        expect(steps(WRITING)[0].detail).toBe(`Claude écrit la pièce (${WRITING.text.length} caractères)`);
        expect(steps({...WRITING, round: 2, phase: 'starting'})[0].detail).toBe(
            'Deuxième écriture : Claude se prépare'
        );
        const stored = steps(STORED);
        expect(stored.map((s) => [s.key, s.state, s.detail])).toEqual([
            ['write', 'done', '19 répliques, 1 personnage'],
            ['stage', 'running', 'Claude choisit les émotions'],
            ['voice', 'running', '7/19 répliques rendues']
        ]);
        expect(stored[2].share).toBeCloseTo(7 / 19);
        expect(steps(READY).map((s) => s.detail)).toEqual([
            '19 répliques, 1 personnage',
            '12 mouvements ajoutés',
            '19 voix prêtes'
        ]);
        expect(steps({...READY, moves: 0, stage: 'failed'})[1].detail).toBe('Sans émotions');
        expect(steps({...STORED, status: 'cancelled', stage: 'failed', voice: 'failed'}).map((s) => s.detail)).toEqual([
            '19 répliques, 1 personnage',
            'Annulée',
            'Annulées à 7/19'
        ]);
    });

    it('says a build in a word for its row: the step it is at, then how it ended', () => {
        expect(said(WRITING)).toEqual({label: 'Écriture', tone: 'attention', busy: true});
        expect(said(STORED).label).toBe('Voix 7/19');
        expect(said({...STORED, voice: 'done'}).label).toBe('Mise en scène');
        expect([said(READY), said(REFUSED), said({...WRITING, status: 'cancelled'})].map((s) => s.label)).toEqual([
            'Prête',
            'Échec',
            'Annulée'
        ]);
    });
});

describe('the view of a scene being written', () => {
    const calls = {onCancel: vi.fn(), onOpen: vi.fn(), onPlay: vi.fn(), onRetry: vi.fn()};
    const view = (build: SceneBuild) => mount(createElement(Writing, {build, ...calls}));

    it('shows the three steps, the play as it is written, and stops on « Annuler »', async () => {
        await view(WRITING);
        expect(states()).toEqual(['write:running', 'stage:waiting', 'voice:waiting']);
        expect(page()).toContain('SCÈNE DEMANDÉE À LA VOIX');
        expect(page()).toContain(`« ${WRITING.request} »`);
        expect(page()).toContain('GAUCHE · Kore'); // the cast, as soon as it is written
        expect(document.querySelector('[aria-label="La pièce"]')?.textContent).toContain('Il est passé par moi.▍');
        await click('Annuler');
        expect(calls.onCancel).toHaveBeenCalledOnce();
        await view(STORED);
        expect(states()).toEqual(['write:done', 'stage:running', 'voice:running']); // side by side
        expect(document.querySelector('[aria-label="La pièce"]')?.textContent).not.toContain('▍');
    });

    it('offers the play on the robot and the editor once ready; by voice, the robot plays it itself', async () => {
        await view({...READY, voiced: false});
        expect(states()).toEqual(['write:done', 'stage:done', 'voice:done']);
        expect(button('Annuler')).toBeUndefined();
        await click('Jouer sur le robot');
        await click('Ouvrir la scène');
        expect([calls.onPlay.mock.calls, calls.onOpen.mock.calls]).toEqual([[['deux-fentes']], [['deux-fentes']]]);
        await view(READY);
        expect(page()).toContain('Reachy l’annonce, puis la joue.');
        expect(button('Jouer sur le robot')).toBeUndefined();
        expect(button('Ouvrir la scène')).toBeDefined();
    });

    it('says why it failed, what the first play was refused for, and asks again with the same words', async () => {
        await view(REFUSED);
        expect(states()[0]).toBe('write:failed');
        expect(document.querySelector('.MuiAlert-colorError')?.textContent).toBe(REFUSED.error);
        expect(page()).toContain(`Refusée une première fois : ${REFUSED.problems[0]}`);
        expect(button('Ouvrir la scène')).toBeUndefined(); // nothing was stored
        await click('Redemander');
        expect(calls.onRetry).toHaveBeenCalledWith(REFUSED.request);
        await view({...READY, warning: 'Sans émotions : Claude n’a rien proposé.', stage: 'failed'});
        expect(document.querySelector('.MuiAlert-colorWarning')?.textContent).toContain('Sans émotions');
    });
});

describe('the dialog, a scene asked for', () => {
    beforeEach(() => {
        mocks.list.mockResolvedValue({scenes: LISTED.slice(0, 1)});
        mocks.get.mockImplementation(async (id: string) => sceneOutSchema.parse({...examples.SceneOut[0], id}));
        mocks.builds.mockResolvedValue({builds: []});
        mocks.playScene.mockResolvedValue(undefined);
        mocks.cancel.mockResolvedValue(undefined);
    });
    const open = async (props: {sceneId?: string; buildId?: string; asked?: number} = {}) => {
        const {default: Scenes} = await import('../src/scenes/Scenes');
        await mount(createElement(Scenes, {onClose: () => {}, ...props}));
    };
    const ask = async (request: string) => {
        const field = document.querySelector<HTMLTextAreaElement>('textarea[maxlength="2000"]');
        if (!field) throw new Error('no field to describe a scene in');
        await act(async () => {
            Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), 'value')?.set?.call(field, request);
            field.dispatchEvent(new Event('input', {bubbles: true}));
        });
        await click('Écrire');
    };
    const frame = (build: SceneBuild) => act(async () => mocks.frame?.(build));
    const rows = () => [...document.querySelectorAll('[role="button"]')].map((el) => el.textContent);

    it('asks the Mac for the scene described, and shows it being prepared in place of the editor', async () => {
        const started: SceneBuild = {...WRITING, voiced: false, write: 'waiting', phase: 'starting', text: ''};
        mocks.write.mockResolvedValue(started);
        await open();
        expect(document.querySelector('textarea[aria-label="Texte de la scène"]')).not.toBeNull();
        await ask('Deux fentes de Young débattent');
        expect(mocks.write).toHaveBeenCalledWith('Deux fentes de Young débattent');
        expect(document.querySelector('textarea[aria-label="Texte de la scène"]')).toBeNull();
        expect(states()).toEqual(['write:waiting', 'stage:waiting', 'voice:waiting']);
        expect(rows()[0]).toBe(`${started.request}Écriture`); // its row, first in the list

        await frame({...WRITING, voiced: false});
        expect(states()[0]).toBe('write:running');
        mocks.list.mockResolvedValue({scenes: LISTED});
        await frame({...STORED, voiced: false});
        expect(states()).toEqual(['write:done', 'stage:running', 'voice:running']);
        expect(rows().filter((row) => row?.startsWith('Les deux fentes'))).toEqual(['Les deux fentesVoix 7/19']);
        await click('Annuler');
        expect(mocks.cancel).toHaveBeenCalledWith(WRITING.id);
    });

    it('plays the scene on the robot once ready, its editor open on it', async () => {
        mocks.write.mockResolvedValue({...WRITING, voiced: false});
        mocks.list.mockResolvedValue({scenes: LISTED});
        await open();
        await ask('Deux fentes');
        await frame({...READY, voiced: false});
        expect(page()).toContain('Prête en');
        await click('Jouer sur le robot');
        expect(mocks.playScene).toHaveBeenCalledWith('deux-fentes');
        expect(mocks.get).toHaveBeenLastCalledWith('deux-fentes');
        expect(document.querySelector('textarea[aria-label="Texte de la scène"]')).not.toBeNull();
        expect(states()).toEqual([]); // the build gave way to its scene
        expect(rows().filter((row) => row?.startsWith('Les deux fentes'))).toHaveLength(1);
    });

    it('opens on the scene the robot writes at someone’s ask, and says why the Mac refused another', async () => {
        mocks.builds.mockResolvedValue({builds: [STORED]});
        mocks.list.mockResolvedValue({scenes: LISTED});
        await open({buildId: STORED.id, asked: 1});
        expect(states()).toEqual(['write:done', 'stage:running', 'voice:running']);
        expect(page()).toContain('SCÈNE DEMANDÉE À LA VOIX');
        mocks.write.mockRejectedValue(new ApiError(409, 'Une scène est déjà en cours d’écriture.'));
        await ask('Une autre');
        expect(page()).toContain('Une scène est déjà en cours d’écriture.');
        expect(states()).toHaveLength(3); // the one under way is still shown
        await click('La fête surprise'); // a scene picked takes the place of the build
        expect(states()).toEqual([]);
        expect(mocks.get).toHaveBeenLastCalledWith('fete-surprise');
    });
});
