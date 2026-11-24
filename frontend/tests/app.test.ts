// @vitest-environment happy-dom
// The dashboard around its surfaces, rendered whole: what a surface that cannot show leaves on screen, and the unsaved
// work a surface holds, never replaced without a word.
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {ApiError} from '../src/api/client';
import {
    catalogSchema,
    sceneCheckSchema,
    sceneOutSchema,
    sceneVocabularySchema,
    settingsViewSchema,
    type RobotEvent,
    type SessionSnapshot
} from '../src/api/schemas';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const mocks = vi.hoisted(() => ({get: vi.fn(), stopScene: vi.fn(), heard: new Set<(event: RobotEvent) => void>()}));
vi.mock('../src/scenes/api', () => ({
    scenesApi: {
        list: vi.fn(async () => ({
            scenes: [
                {id: 'croissant', title: 'Le croissant disparu', builtin: true, errors: 0, segments: 1, rendered: 1},
                {id: 'fete-surprise', title: 'La fête surprise', builtin: true, errors: 0, segments: 1, rendered: 1}
            ]
        })),
        get: mocks.get,
        vocabulary: vi.fn(async () => sceneVocabularySchema.parse(examples.SceneVocabulary[0])),
        check: vi.fn(async () => sceneCheckSchema.parse(examples.SceneCheck[0])),
        builds: vi.fn(async () => ({builds: []}))
    }
}));
vi.mock('../src/api/client', async (importOriginal) => {
    const actual = await importOriginal<typeof import('../src/api/client')>();
    const catalog = async () => catalogSchema.parse(examples.Catalog[0]);
    return {...actual, api: {...actual.api, catalog, stopScene: mocks.stopScene}};
});
const ASLEEP: SessionSnapshot = {
    awake: false,
    session_id: null,
    phase: null,
    cost_usd: 0,
    last_latency_ms: null,
    manette: null,
    antenna_wake: true,
    scene: null,
    hushed: false
};
vi.mock('../src/live', () => {
    const live = {
        link: 'up',
        robotStatus: 'live',
        session: ASLEEP,
        volume: 40,
        settings: settingsViewSchema.parse(examples.SettingsView[0]),
        saveSettings: () => {},
        saveVolume: () => {},
        onConversation: (listener: (event: RobotEvent) => void) => {
            mocks.heard.add(listener);
            return () => mocks.heard.delete(listener);
        },
        on: () => () => {},
        want: () => () => {},
        show: () => {},
        shown: () => null
    };
    return {useLive: () => live, useShowing: () => {}};
});
vi.mock('../src/studio/RobotView', () => ({Stage: () => null})); // the bench needs WebGL
// The apps' chunk cannot be fetched: its dynamic import rejects, as on a dropped Wi-Fi or a restarting dev server.
vi.mock('../src/apps/Apps', () => {
    throw new TypeError('Failed to fetch dynamically imported module: /src/apps/Apps.tsx');
});

beforeAll(async () => {
    await import('../src/App');
    await import('../src/scenes/Scenes'); // its chunk loaded once, as a browser keeps it
    await import('../src/studio/Studio');
}, 120_000);

describe('the dashboard', () => {
    let root: Root | null = null;
    afterEach(async () => {
        await act(async () => root?.unmount());
        root = null;
        document.body.innerHTML = '';
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
        mocks.stopScene.mockReset();
    });
    const buttons = () => [...document.querySelectorAll<HTMLElement>('button, [role="button"]')];
    const button = (text: string) => buttons().find((el) => el.textContent === text);
    const listed = (title: string) => buttons().find((el) => el.textContent?.startsWith(title));
    const settle = () => act(async () => new Promise((done) => setTimeout(done, 30)));
    const render = async () => {
        const {App} = await import('../src/App');
        const host = document.createElement('div');
        document.body.append(host);
        root = createRoot(host);
        await act(async () => root?.render(createElement(App)));
    };

    // Each test renders the whole dashboard and steps through real timers: seconds on a loaded runner. A test cut off
    // keeps running into the next test's page, and every test after it fails.
    it('gives each test the time a loaded runner takes', ({task}) => {
        expect(task.timeout).toBeGreaterThanOrEqual(30_000);
    });

    it('stays usable when a surface cannot load, and says so in its place', async () => {
        vi.spyOn(console, 'error').mockImplementation(() => {}); // React reports the error it caught
        await render();
        await act(async () => button('Apps')?.click());
        await settle();
        expect(document.body.textContent).toContain('Réveiller');
        expect(document.querySelector('[role="dialog"]')?.textContent).toContain('n’a pas pu s’ouvrir');
        await act(async () => button('Fermer')?.click());
        await settle();
        expect(document.body.textContent).not.toContain('n’a pas pu s’ouvrir');
        expect(button('Réveiller')).toBeTruthy();
    });

    const area = () =>
        document.querySelector('.MuiDialog-root textarea[aria-label="Texte de la scène"]') as HTMLTextAreaElement;
    const type = (field: HTMLInputElement | HTMLTextAreaElement, value: string) =>
        act(async () => {
            const kind = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement : HTMLInputElement;
            Object.getOwnPropertyDescriptor(kind.prototype, 'value')?.set?.call(field, value);
            field.dispatchEvent(new Event('input', {bubbles: true}));
        });
    const MINE = 'MON TRAVAIL NON ENREGISTRÉ';
    const editScene = async () => {
        mocks.get.mockImplementation(async (id: string) =>
            sceneOutSchema.parse({...examples.SceneOut[0], id, title: id, text: `TEXTE DE ${id}`, builtin: false})
        );
        await render();
        await act(async () => button('Scènes')?.click());
        await settle();
        expect(area().value).toBe('TEXTE DE croissant');
        await type(area(), MINE);
    };

    it('asks before the palette replaces an unsaved scene, as before a launch by voice', async () => {
        await editScene();
        await act(async () => void window.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', metaKey: true})));
        await settle();
        const search = document.querySelector('input[aria-label="Chercher une action"]') as HTMLInputElement;
        await type(search, 'fete surprise');
        const preview = new KeyboardEvent('keydown', {key: 'Enter', shiftKey: true, bubbles: true});
        await act(async () => void search.dispatchEvent(preview));
        await settle();
        expect(area().value).toBe(MINE);
        expect(document.body.textContent).toContain('Ce qui n’est pas enregistré sera perdu.');
        await act(async () => button('Voir quand même')?.click());
        await settle();
        expect(area().value).toBe('TEXTE DE fete-surprise');
    });

    it('shows a move from the palette in the studio without a word: its unsaved tuning stays', async () => {
        vi.stubGlobal('fetch', async () => new Response(JSON.stringify(examples.PreviewFrames[0])));
        vi.stubGlobal('requestIdleCallback', (run: () => void) => run()); // the studio's chunk prefetched, as in Chrome
        await render();
        await settle();
        await act(async () => button('Studio')?.click());
        await settle();
        const amplitude = () => document.querySelector<HTMLInputElement>('input[aria-label="Amplitude"]');
        const before = amplitude()?.value;
        await act(async () =>
            amplitude()?.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}))
        );
        const tuned = amplitude()?.value;
        expect(tuned).not.toBe(before);
        await act(async () => void window.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', metaKey: true})));
        await settle();
        const search = document.querySelector('input[aria-label="Chercher une action"]') as HTMLInputElement;
        await type(search, 'oui de la tete');
        await act(
            async () =>
                void search.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', shiftKey: true, bubbles: true}))
        );
        await settle();
        expect(document.body.textContent).not.toContain('Ce qui n’est pas enregistré sera perdu.');
        expect(document.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('Gestes');
        await act(async () => document.querySelector<HTMLElement>('[role="tab"]')?.click()); // back to the animations
        expect(amplitude()?.value).toBe(tuned);
    });

    const escape = (inside: Element | null) =>
        act(async () => void inside?.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
    const surface = () => document.querySelector('.MuiDialog-root');

    it('asks before Escape or « Fermer » closes an unsaved scene, not an untouched one', async () => {
        await editScene();
        await escape(area());
        await settle();
        expect(area().value).toBe(MINE);
        expect(document.body.textContent).toContain('Ce qui n’est pas enregistré sera perdu.');
        await act(async () => document.querySelector<HTMLElement>('[aria-label="Fermer"]')?.click());
        await settle();
        expect(area().value).toBe(MINE);
        await act(async () => button('Fermer quand même')?.click());
        await settle();
        expect(surface()).toBeNull();

        await act(async () => button('Scènes')?.click()); // untouched: nothing to lose, nothing asked
        await settle();
        await escape(area());
        await settle();
        expect(surface()).toBeNull();
    });

    it('offers a scene the robot starts in place of « Fermer quand même », and asks again before showing it', async () => {
        await editScene();
        await escape(area());
        await settle();
        expect(button('Fermer quand même')).toBeTruthy();
        await tell('started');
        await settle();
        expect(document.body.textContent).toContain('Reachy joue « La fête surprise ».');
        expect(button('Fermer quand même')).toBeUndefined();
        await act(async () => button('Voir')?.click());
        await settle();
        expect(area().value).toBe(MINE);
        expect(button('Voir quand même')).toBeTruthy();
    });

    it('closes the studio’s help on Escape, then the studio', async () => {
        vi.stubGlobal('fetch', async () => new Response(JSON.stringify(examples.PreviewFrames[0])));
        await render();
        await act(async () => button('Studio')?.click());
        await settle();
        const help = () => document.querySelector('[aria-label="Raccourcis clavier (?)"]');
        await act(async () => void window.dispatchEvent(new KeyboardEvent('keydown', {key: '?'})));
        await settle();
        expect(help()?.getAttribute('aria-labelledby')).toBeTruthy(); // the help is open
        await escape(help());
        await settle();
        expect(surface()).not.toBeNull();
        expect(help()?.getAttribute('aria-labelledby')).toBeFalsy();
        await escape(help());
        await settle();
        expect(surface()).toBeNull();
    });

    it('asks before the scene list replaces an unsaved scene, not an untouched new one', async () => {
        await editScene();
        await act(async () => listed('La fête surprise')?.click());
        await settle();
        expect(area().value).toBe(MINE);
        await act(async () => button('Annuler')?.click());
        await settle();
        expect(area().value).toBe(MINE);
        await act(async () => button('Nouvelle scène')?.click());
        await settle();
        expect(area().value).toBe(MINE);
        await act(async () => button('Abandonner')?.click());
        await settle();
        expect(area().value).toContain('@NARRATEUR');
        await act(async () => listed('La fête surprise')?.click()); // the template, as it was: nothing to lose
        await settle();
        expect(area().value).toBe('TEXTE DE fete-surprise');
    });

    // The robot starts « La fête surprise » by voice while a scene is being edited: it is offered, not shown.
    const tell = (state: 'started' | 'ended') =>
        act(async () => {
            const event = {kind: 'scene', session_id: 's', scene_id: 'fete-surprise', title: 'La fête surprise'};
            mocks.heard.forEach((listener) => listener({...event, state, line: null, message: null} as RobotEvent));
        });
    const offer = () => document.querySelector('.MuiSnackbar-root')?.textContent ?? '';
    const offeredBesideMine = async () => {
        await editScene();
        await tell('started');
        await settle();
        expect(offer()).toMatch(/^Reachy joue «\s?La fête surprise\s?»\./);
        expect(area().value).toBe(MINE);
    };

    it('stops the scene the robot plays from its offer, which then goes', async () => {
        mocks.stopScene.mockResolvedValue(undefined);
        await offeredBesideMine();
        await act(async () => button('Arrêter')?.click());
        await settle();
        expect(mocks.stopScene).toHaveBeenCalledTimes(1);
        expect(button('Arrêter')).toBeUndefined();
        expect(area().value).toBe(MINE);
    });

    it('no longer offers a scene once it is over', async () => {
        await offeredBesideMine();
        await tell('ended');
        await act(async () => new Promise((done) => setTimeout(done, 500))); // the snackbar's exit
        expect(offer()).toBe('');
        expect(mocks.stopScene).not.toHaveBeenCalled();
    });

    it('says why a stop from the offer failed, and keeps « Arrêter » to try again', async () => {
        mocks.stopScene.mockRejectedValueOnce(new ApiError(503, 'Le robot ne répond pas.'));
        await offeredBesideMine();
        await act(async () => button('Voir')?.click()); // asked first: the draft would go
        expect(offer()).toContain('Ce qui n’est pas enregistré sera perdu.');
        await act(async () => button('Arrêter')?.click());
        await settle();
        expect(offer()).toBe('Le robot ne répond pas.ArrêterVoir');
        mocks.stopScene.mockResolvedValueOnce(undefined);
        await act(async () => button('Arrêter')?.click());
        await settle();
        expect(mocks.stopScene).toHaveBeenCalledTimes(2);
        expect(button('Arrêter')).toBeUndefined();
    });

    it('lines up the robot’s strip, the header, the conversation and the composer on one gutter', async () => {
        const happyDOM = (window as unknown as {happyDOM: {setViewport: (size: object) => void}}).happyDOM;
        const gutters: Record<number, (string | null)[]> = {};
        try {
            for (const width of [375, 768, 1280]) {
                happyDOM.setViewport({width, height: 900});
                await render();
                const sides = (el: Element | null | undefined) => {
                    const style = getComputedStyle(el as Element);
                    return `${style.paddingLeft} ${style.paddingRight}`;
                };
                const words = [...document.querySelectorAll('p')].find((p) => p.closest('nav, form, aside') === null);
                gutters[width] = [
                    // the strip on a phone or a tablet; the robot's column beside a computer's has a gutter of its own
                    width < 900 ? sides(document.querySelector('header')) : null,
                    sides(document.querySelector('nav')),
                    sides(words?.parentElement?.parentElement), // the conversation, its invitation here
                    sides(document.querySelector('form'))
                ];
                await act(async () => root?.unmount());
                root = null;
                document.body.innerHTML = '';
            }
        } finally {
            happyDOM.setViewport({width: 1024, height: 768});
        }
        expect(gutters).toEqual({
            375: new Array(4).fill('16px 16px'),
            768: new Array(4).fill('16px 16px'),
            1280: [null, ...new Array(3).fill('24px 24px')]
        });
    });
});
