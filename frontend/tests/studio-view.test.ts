// @vitest-environment happy-dom
// The studio as a user drives it, 3D aside (the bench needs WebGL): it opens on the palette's move, walks the lists
// with the keyboard, tries a move on the robot, and keeps a tuning for the next wake.
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {catalogSchema, settingsViewSchema, type TryMove} from '../src/api/schemas';
import '../src/studio/Studio'; // its libraries load once, at collect; each test imports the studio afresh (`open`)

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const VIEW = settingsViewSchema.parse(examples.SettingsView[0]);
const FRAMES = examples.PreviewFrames[0];
const mocks = vi.hoisted(() => ({
    tryMove: vi.fn(),
    session: vi.fn(),
    saveSettings: vi.fn(),
    awake: false,
    view: null as unknown,
    stage: {} as {robots?: unknown[]; labels?: string[]},
    previews: [] as string[]
}));
vi.mock('../src/studio/RobotView', () => ({
    Stage: (props: object) => {
        mocks.stage = props;
        return null;
    }
}));
vi.mock('../src/api/client', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../src/api/client')>()),
    api: {catalog: async () => catalogSchema.parse(examples.Catalog[0]), tryMove: mocks.tryMove, session: mocks.session}
}));
vi.mock('../src/live', () => ({
    useLive: () => ({
        settings: mocks.view,
        session: {awake: mocks.awake},
        link: 'up',
        saveSettings: mocks.saveSettings,
        want: () => () => {},
        on: () => () => {}
    })
}));

const nod: TryMove = {kind: 'gesture', name: 'nod', mode: null, tuning: {amplitude: 1, speed: 1}, seconds: 6};

describe('the studio', () => {
    let root: Root | null = null;
    beforeEach(() => {
        vi.resetModules(); // each test is a new page: the studio's previews are asked again
        mocks.view = VIEW;
        mocks.previews.length = 0;
        mocks.awake = false;
        vi.stubGlobal(
            'fetch',
            vi.fn(async (path: string) => {
                mocks.previews.push(path);
                return new Response(JSON.stringify(FRAMES));
            })
        );
    });
    afterEach(() => {
        act(() => root?.unmount());
        root = null;
        document.body.innerHTML = '';
        vi.unstubAllGlobals();
        vi.clearAllMocks();
    });

    const open = async (move?: TryMove) => {
        const {default: Studio} = await import('../src/studio/Studio');
        root = createRoot(document.body.appendChild(document.createElement('div')));
        await act(async () => root?.render(createElement(Studio, {onClose: () => {}, move})));
    };
    const selected = () => document.querySelector('.MuiListItemButton-root.Mui-selected')?.textContent;
    const key = async (k: string) => {
        await act(async () => window.dispatchEvent(new KeyboardEvent('keydown', {key: k, bubbles: true})));
    };
    const click = async (text: string) => {
        const button = [...document.querySelectorAll<HTMLElement>('button, [role="button"]')].find(
            (b) => b.textContent === text
        );
        await act(async () => button?.click());
    };
    const status = () => document.querySelector('[role="status"], [role="alert"]')?.textContent;

    it('opens on the move the palette asked for, and walks the list with the arrows', async () => {
        await open(nod);
        expect(document.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('Gestes');
        expect(selected()).toContain('Oui de la tête');
        expect(selected()).toContain('Dis : “fais oui de la tête”');
        expect(mocks.previews.at(-1)).toBe('/api/studio/preview?kind=gesture&name=nod');
        await key('ArrowDown');
        expect(selected()).toContain('Non de la tête');
        expect(mocks.previews.at(-1)).toBe('/api/studio/preview?kind=gesture&name=shake');
    });

    it("tries the current setting's animation on the robot, and says why it could not", async () => {
        const {ApiError} = await import('../src/api/client');
        mocks.tryMove.mockRejectedValue(new ApiError(409, "Le robot n'est pas lancé : démarre-le avec make run."));
        await open();
        expect(selected()).toContain('Posé');
        expect(selected()).toContain('actuel');
        await key('e');
        const tuning = VIEW.settings.tuning.idle;
        expect(mocks.tryMove).toHaveBeenCalledWith({kind: 'animation', name: 'calm', mode: 'idle', tuning, seconds: 8});
        expect(status()).toBe("Le robot n'est pas lancé : démarre-le avec make run.");
    });

    it('compares every variant of the mode on one bench', async () => {
        await open();
        await key('c');
        const variants = VIEW.options.animations.idle;
        expect(mocks.stage.robots).toHaveLength(variants.length);
        expect(mocks.stage.labels).toEqual(variants.map((v) => v.label));
    });

    it('keeps a variant for the next wake, then offers to restart the conversation', async () => {
        mocks.awake = true;
        await open();
        await click('Curieux');
        expect(mocks.previews.at(-1)).toContain('name=curious&mode=idle');
        await click('Garder ce réglage');
        expect(mocks.saveSettings).toHaveBeenCalledWith({idle_animation: 'curious', tuning: VIEW.settings.tuning});
        expect(status()).toBe('Appliqué au prochain réveil.');
        await click('Relancer la conversation');
        expect(mocks.session).toHaveBeenCalledWith('restart');
    });

    it('holds a tuning moved and not kept as unsaved work, not a variant only looked at', async () => {
        await open();
        const {keeping} = await import('../src/surfaces');
        await click('Curieux');
        expect(keeping()).toBe(false); // browsing the variants loses nothing
        const amplitude = document.querySelector<HTMLInputElement>('input[aria-label="Amplitude"]');
        const before = amplitude?.value;
        await act(async () =>
            amplitude?.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}))
        );
        expect(document.querySelector<HTMLInputElement>('input[aria-label="Amplitude"]')?.value).not.toBe(before);
        expect(keeping()).toBe(true); // what the robot launches meanwhile is offered beside it, not shown over it
        await click('Garder ce réglage');
        expect(keeping()).toBe(false);
    });
});
