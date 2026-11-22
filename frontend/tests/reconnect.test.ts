// @vitest-environment happy-dom
// What the scenes show, and tell the Mac they show, after the dashboard's socket drops and comes back, through the
// real LiveProvider and line (a fake WebSocket, the Mac's reads faked): whatever the event channels carried meanwhile
// was missed.
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {
    dashboardStateSchema,
    sceneOutSchema,
    sceneVocabularySchema,
    settingsViewSchema,
    type DashboardState,
    type RobotEvent
} from '../src/api/schemas';
import {ApiError} from '../src/api/client';
import {LiveProvider} from '../src/live';
import {Play} from '../src/scenes/Play';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const VIEW = settingsViewSchema.parse(examples.SettingsView[0]);
const AWAKE = dashboardStateSchema.parse(examples.DashboardState[0]);
const playing = (scene: string | null): DashboardState => ({...AWAKE, session: {...AWAKE.session, scene}});

const mocks = vi.hoisted(() => ({
    sockets: [] as {
        send: ReturnType<typeof vi.fn>;
        open: () => void;
        receive: (m: unknown) => void;
        drop: () => void;
    }[],
    state: vi.fn(),
    playScene: vi.fn()
}));
vi.mock('../src/api/client', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../src/api/client')>()),
    api: {state: mocks.state, settings: async () => VIEW, playScene: mocks.playScene, stopScene: vi.fn()}
}));
vi.mock('../src/scenes/api', () => ({
    scenesApi: {
        list: vi.fn(async () => ({
            scenes: [
                {id: 'fete-surprise', title: 'La fête surprise', builtin: true, errors: 0, segments: 1, rendered: 1}
            ]
        })),
        get: vi.fn(async () => sceneOutSchema.parse(examples.SceneOut[0])),
        save: vi.fn(),
        vocabulary: vi.fn(async () => sceneVocabularySchema.parse(examples.SceneVocabulary[0])),
        check: vi.fn(async () => examples.SceneCheck[0]),
        emotions: vi.fn(),
        builds: vi.fn(async () => ({builds: []}))
    }
}));

class FakeSocket {
    readyState = 0;
    onopen: (() => void) | null = null;
    onmessage: ((event: {data: string}) => void) | null = null;
    onclose: (() => void) | null = null;
    send = vi.fn();
    constructor() {
        mocks.sockets.push(this);
    }
    open() {
        this.readyState = 1;
        this.onopen?.();
    }
    receive(data: unknown) {
        this.onmessage?.({data: JSON.stringify(data)});
    }
    drop() {
        this.readyState = 3;
        this.onclose?.();
    }
    close() {
        this.readyState = 3;
    }
}

const last = () => mocks.sockets[mocks.sockets.length - 1];
const settle = () => act(async () => void (await vi.advanceTimersByTimeAsync(400))); // reads land, checks run
const deliver = (data: unknown) => act(async () => last().receive(data));
const scene = {kind: 'scene', session_id: 's', scene_id: 'fete-surprise', title: 'La fête surprise'} as const;
const robot = (state: 'started' | 'ended') =>
    deliver({channel: 'conversation', data: {...scene, state, line: null, message: null} satisfies RobotEvent});
const buttons = () => [...document.querySelectorAll('button')].map((b) => b.textContent?.trim());
const click = (text: string) =>
    act(async () => [...document.querySelectorAll('button')].find((b) => b.textContent?.startsWith(text))?.click());

/** The socket drops (the network lost, a 1013), whatever goes out meanwhile is missed, then the line reconnects with
 * the Mac's state as `now`. */
const reconnect = async (now: DashboardState) => {
    const before = mocks.sockets.length;
    await act(async () => last().drop());
    mocks.state.mockResolvedValue(now);
    await act(async () => void (await vi.advanceTimersByTimeAsync(1600))); // the first retry, its jitter included
    expect(mocks.sockets).toHaveLength(before + 1);
    await act(async () => last().open());
    await settle();
};

describe('after a reconnect', () => {
    let root: Root;
    const mount = async (child: ReturnType<typeof createElement>) => {
        const host = document.createElement('div');
        document.body.append(host);
        root = createRoot(host);
        await act(async () => root.render(createElement(LiveProvider, null, child)));
        await act(async () => last().open());
        await settle();
    };
    const scenes = async () => {
        const {default: Scenes} = await import('../src/scenes/Scenes');
        await mount(createElement(Scenes, {onClose: () => {}, sceneId: 'fete-surprise'}));
    };
    beforeEach(() => {
        vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout']});
        mocks.sockets.length = 0;
        mocks.state.mockReset().mockResolvedValue(playing(null));
        mocks.playScene.mockReset().mockResolvedValue(undefined);
        vi.stubGlobal('WebSocket', FakeSocket);
    });
    afterEach(async () => {
        await act(async () => root.unmount());
        document.body.textContent = '';
        vi.unstubAllGlobals();
        vi.useRealTimers();
    });

    it('tells the Mac which scene the editor shows, again after a reconnect, and nothing once it is closed', async () => {
        const showing = () => JSON.parse(last().send.mock.lastCall?.[0] as string).showing; // the last word sent
        await scenes();
        expect(showing()).toMatchObject({kind: 'scene', id: 'fete-surprise'}); // what « joue celle-ci » means
        await reconnect(playing(null));
        expect(showing()).toMatchObject({kind: 'scene', id: 'fete-surprise'});
        await act(async () => root.render(createElement(LiveProvider, null))); // the surface closed
        expect(showing()).toBeNull();
    });

    it('asks again for a play that waited for its voices, whose last frame went out meanwhile', async () => {
        mocks.playScene.mockRejectedValueOnce(
            new ApiError(409, 'Les voix de « La fête surprise » se préparent (1/2).')
        );
        const onPlaying = vi.fn();
        await mount(createElement(Play, {saved: async () => 'fete-surprise', cast: [], playing: false, onPlaying}));
        await click('Jouer sur le robot');
        expect(document.body.textContent).toContain('la scène se jouera dès qu’elles seront prêtes');
        await reconnect(playing(null));
        expect(mocks.playScene).toHaveBeenCalledTimes(2);
        expect(onPlaying).toHaveBeenCalledWith(true);
        expect(document.body.textContent).not.toContain('Voix en préparation');
        await reconnect(playing(null)); // played: nothing asked any more
        expect(mocks.playScene).toHaveBeenCalledTimes(2);
    });

    it('gives « Jouer » back for a scene that ended meanwhile, and « Arrêter » for one that started', async () => {
        await scenes();
        await robot('started');
        await deliver({channel: 'session', data: playing('fete-surprise').session});
        expect(buttons()).toContain('Arrêter');
        await reconnect(playing(null)); // its `ended` went out while the socket was down
        expect(buttons()).not.toContain('Arrêter');
        expect(buttons()).toContain('Jouer sur le robot');
        await reconnect(playing('fete-surprise')); // its `started` went out while the socket was down
        expect(buttons()).toContain('Arrêter');
    });

    it('no longer says the robot plays a scene whose end went out meanwhile', async () => {
        await scenes();
        await robot('started');
        await deliver({channel: 'session', data: playing('fete-surprise').session});
        expect(document.body.textContent).toContain('Le robot joue « La fête surprise ».');
        await reconnect(playing(null));
        expect(buttons()).toContain('Jouer sur le robot');
        expect(document.body.textContent).not.toContain('Le robot joue');
    });

    it('follows a play asked whose « started » went out meanwhile, and says nothing failed', async () => {
        await scenes();
        await click('Jouer sur le robot');
        await reconnect(playing('fete-surprise')); // the robot took it, its `started` missed
        await act(async () => void (await vi.advanceTimersByTimeAsync(16_000))); // longer than a play may wait
        expect(document.body.textContent).not.toContain('pas lancé la scène');
        expect(buttons()).toContain('Arrêter');
    });

    it('keeps a jump asked through a snapshot that may still show the run it replaces', async () => {
        await scenes();
        await robot('started');
        await deliver({channel: 'session', data: playing('fete-surprise').session});
        const rects = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
            this: HTMLElement
        ) {
            const line = Number(this.dataset.line);
            return {left: 0, top: line ? (line - 1) * 21 : 0, bottom: line ? line * 21 : 2000, right: 800} as DOMRect;
        });
        const area = document.querySelector('textarea[aria-label="Texte de la scène"]') as HTMLTextAreaElement;
        await act(async () =>
            area.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: 10, clientY: 2}))
        );
        rects.mockRestore();
        expect(mocks.playScene).toHaveBeenLastCalledWith('fete-surprise', {moves: true, rehearse: null, start: 1});
        await reconnect(playing('fete-surprise')); // the old run, or the jump: only the robot's events can tell
        await robot('ended'); // the run the jump replaces
        expect(buttons()).toContain('Arrêter');
        await robot('ended'); // the jump itself, dropped before it started
        expect(buttons()).toContain('Jouer sur le robot');
    });

    it('keeps a play asked through a snapshot that says nothing plays yet, until the robot answers', async () => {
        await scenes();
        await click('Jouer sur le robot');
        expect(buttons()).toContain('Arrêter'); // queued
        await deliver({channel: 'session', data: {...playing(null).session, phase: 'thinking'}});
        expect(buttons()).toContain('Arrêter');
        await robot('started');
        await deliver({channel: 'session', data: playing('fete-surprise').session});
        await robot('ended');
        await deliver({channel: 'session', data: playing(null).session});
        expect(buttons()).toContain('Jouer sur le robot');
    });
});
