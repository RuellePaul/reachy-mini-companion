// @vitest-environment happy-dom
// The palette over the real catalogue the Mac builds (contract-examples.json): the search, the grouping and
// the keyboard, as a browser runs them.
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {catalogSchema, type CatalogEntry} from '../src/api/schemas';
import {Palette} from '../src/palette/Palette';
import {runEntry} from '../src/palette/run';
import {flatten, fold, move, score, sections} from '../src/palette/search';
import {ApiError, errorText} from '../src/api/client';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const CATALOG = catalogSchema.parse(examples.Catalog[0]);
const labels = (query: string) => flatten(sections(CATALOG, query)).map((entry) => entry.label);

const mocks = vi.hoisted(() => ({catalog: vi.fn()}));
vi.mock('../src/api/client', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../src/api/client')>()),
    api: {catalog: mocks.catalog}
}));

describe('search', () => {
    it('ignores accents and case', () => {
        expect(fold('Révérence ÉMUE')).toBe('reverence emue');
        expect(score('emerveille', 'Émerveillé')).toBeGreaterThan(0);
        expect(labels('tete')).toContain('Oui de la tête');
    });

    it('is fuzzy, and ranks word starts and runs first', () => {
        expect(score('rvrnc', 'Révérence')).toBeGreaterThan(0);
        expect(score('xyz', 'Révérence')).toBe(0);
        expect(score('danse', 'Petite danse')).toBeGreaterThan(score('danse', 'd a n s e'));
        expect(labels('reverence')[0]).toBe('Révérence');
    });

    it('reads a long description by whole words only, never fuzzily', () => {
        expect(labels('musique')).toContain('Reprendre la musique');
        expect(labels('musique').some((label) => label.includes('(repos)'))).toBe(false);
        expect(labels('aleatoire')).toContain('Mes titres likés'); // « en aléatoire » in its description
    });

    it('finds an entry by the sentence that gets it', () => {
        expect(labels('fais oui')[0]).toBe('Oui de la tête');
    });

    it('ranks an exact or fuller match first when several match as well', () => {
        expect(labels('Surpris')[0]).toBe('Surpris'); // the emotion, not the gesture « Surprise »
        expect(labels('Danse')[0]).toBe('Danse'); // the emotion, not the gesture « Petite danse »
        expect(labels('Volume')[0]).toBe('Volume'); // the robot's, not « Volume de Spotify »
        expect(labels('baisse le volume')[0]).toBe('Volume');
        expect(labels('reprends')[0]).toBe('Reprendre la musique'); // not « Reprends mon livre audio »
    });

    it('lists a whole group by its name', () => {
        expect(labels('spotify')).toEqual(expect.arrayContaining(['Pause', 'Morceau suivant', 'Mes titres likés']));
        expect(labels('reglages')).toEqual(expect.arrayContaining(['Nom du robot', 'Voix', 'Volume']));
        expect(labels('spotify')[0]).toBe('Volume de Spotify'); // its own name first
    });

    it('groups in the catalogue order, best group first when searching', () => {
        const all = sections(CATALOG, '');
        expect(all.map((s) => s.key)).toEqual(
            CATALOG.groups.map((g) => g.key).filter((k) => all.some((s) => s.key === k))
        );
        expect(all.reduce((n, s) => n + s.entries.length, 0)).toBe(CATALOG.entries.length);
        expect(sections(CATALOG, 'triste')[0].key).toBe('emotions');
    });

    it('walks the list with the arrows, wrapping', () => {
        expect(move(0, 'ArrowDown', 3)).toBe(1);
        expect(move(2, 'ArrowDown', 3)).toBe(0);
        expect(move(0, 'ArrowUp', 3)).toBe(2);
        expect(move(1, 'End', 3)).toBe(2);
        expect(move(5, 'a', 3)).toBe(2);
        expect(move(0, 'ArrowDown', 0)).toBe(0);
    });
});

describe('running an entry', () => {
    const entry = (id: string) => CATALOG.entries.find((e) => e.id === id) as CatalogEntry;
    /** What running it called (each target with its arguments), and how it ended. */
    const run = async (id: string, preview: boolean) => {
        const api = {session: vi.fn(), tryMove: vi.fn(), playScene: vi.fn(), say: vi.fn()};
        const targets = {api, setSurface: vi.fn(), openSettings: vi.fn(), compose: vi.fn()};
        const ended = await runEntry(entry(id), preview, targets).then(() => 'ok', errorText);
        const all = {
            ...api,
            setSurface: targets.setSurface,
            openSettings: targets.openSettings,
            compose: targets.compose
        };
        const called = Object.entries(all).flatMap(([name, fn]) => fn.mock.calls.map((args) => [name, ...args]));
        return {ended, called};
    };

    it('maps each kind of action, played for real or previewed, to what it does', async () => {
        const gesture = CATALOG.entries.find((e) => e.action.kind === 'try') as CatalogEntry;
        const nod = gesture.action.kind === 'try' && gesture.action.move;
        const asked = expect.any(Number);
        expect(await run(gesture.id, false)).toEqual({ended: 'ok', called: [['tryMove', nod]]});
        expect(await run(gesture.id, true)).toEqual({
            ended: 'ok',
            called: [['setSurface', {kind: 'studio', move: nod}]]
        });
        expect(await run('scene:fete-surprise', false)).toEqual({
            ended: 'ok',
            called: [['playScene', 'fete-surprise']]
        });
        expect(await run('scene:fete-surprise', true)).toEqual({
            ended: 'ok',
            called: [['setSurface', {kind: 'scenes', sceneId: 'fete-surprise', asked}]]
        });
        expect(await run('session:sleep', false)).toEqual({ended: 'ok', called: [['session', 'sleep']]});
        expect(await run('app:snake', false)).toEqual({
            ended: 'ok',
            called: [['setSurface', {kind: 'apps', appId: 'snake', asked}]]
        });
        expect(await run('apps:create', false)).toEqual({
            ended: 'ok',
            called: [['setSurface', {kind: 'apps', appId: undefined, asked}]]
        });
        expect(await run('spotify:pause', false)).toEqual({
            ended: 'ok',
            called: [['say', 'Mets la musique en pause.']]
        });
        expect(await run('spotify:play:track', false)).toEqual({ended: 'ok', called: [['compose', 'Mets le titre ']]});
        expect(await run('settings:volume', false)).toEqual({ended: 'ok', called: [['openSettings', 'volume']]});
    });

    it('never reaches the robot for a preview of what has none', async () => {
        for (const id of [
            'session:sleep',
            'app:snake',
            'apps:create',
            'spotify:pause',
            'spotify:queue',
            'settings:name'
        ]) {
            expect(await run(id, true)).toEqual({ended: `Pas d’aperçu 3D pour « ${entry(id).label} ».`, called: []});
        }
    });

    it('numbers each ask, so the same app or scene asked twice shows twice', async () => {
        const [first, second] = [await run('app:snake', false), await run('app:snake', false)];
        expect(second.called[0][1].asked).toBeGreaterThan(first.called[0][1].asked);
    });
});

describe('the palette', () => {
    let root: Root | null = null;
    afterEach(() => {
        act(() => root?.unmount());
        root = null;
        document.body.innerHTML = '';
    });

    const open = async (onRun: (entry: CatalogEntry, preview: boolean) => Promise<void>) => {
        mocks.catalog.mockResolvedValue(CATALOG);
        const host = document.createElement('div');
        document.body.append(host);
        root = createRoot(host);
        await act(async () => root?.render(createElement(Palette, {open: true, onClose: () => {}, onRun})));
        return document.querySelector('input') as HTMLInputElement;
    };

    const type = async (input: HTMLInputElement, text: string) => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        await act(async () => {
            setter?.call(input, text);
            input.dispatchEvent(new Event('input', {bubbles: true}));
        });
    };

    const press = async (input: HTMLInputElement, key: string, shiftKey = false) => {
        await act(async () => {
            input.dispatchEvent(new KeyboardEvent('keydown', {key, shiftKey, bubbles: true}));
        });
    };

    it('runs the highlighted entry with Enter, after the arrows moved it', async () => {
        const onRun = vi.fn().mockResolvedValue(undefined);
        const input = await open(onRun);
        await type(input, 'tete');
        const expected = flatten(sections(CATALOG, 'tete'));
        await press(input, 'ArrowDown');
        await press(input, 'Enter');
        expect(onRun).toHaveBeenCalledWith(expected[1], false);
    });

    it('previews in 3D with Shift + Enter, only what the studio can show', async () => {
        const onRun = vi.fn().mockResolvedValue(undefined);
        const input = await open(onRun);
        await type(input, 'reverence');
        await press(input, 'Enter', true);
        expect(onRun).toHaveBeenLastCalledWith(expect.objectContaining({label: 'Révérence'}), true);
        onRun.mockClear();
        await type(input, 'relancer');
        await press(input, 'Enter', true);
        expect(onRun).not.toHaveBeenCalled(); // never run for real instead
        expect(document.body.textContent).toContain('Pas d’aperçu 3D pour « Relancer la conversation »');
    });

    it('previews from the entry’s own button, never running the entry on the robot as well', async () => {
        const onRun = vi.fn().mockResolvedValue(undefined);
        const click = async (label: string) => {
            const button = [...document.querySelectorAll<HTMLElement>('[role="option"] button')].find(
                (el) => el.textContent === label
            );
            expect(button, label).toBeDefined();
            await act(async () => button?.click());
        };
        const input = await open(onRun);
        await type(input, 'reverence');
        await click('Aperçu');
        expect(onRun.mock.calls).toEqual([[expect.objectContaining({label: 'Révérence'}), true]]);
        onRun.mockClear();
        await type(input, 'fete');
        await click('Ouvrir'); // a scene opens to be read: it is not played
        expect(onRun.mock.calls).toEqual([[expect.objectContaining({label: 'La fête surprise'}), true]]);
    });

    it('says what to do when nothing matches', async () => {
        const input = await open(vi.fn());
        await type(input, 'zzzzqq');
        expect(document.body.textContent).toContain('Rien ne correspond');
    });

    it('shows why a run failed in full, on a phone as on a computer', async () => {
        const why = 'Le robot n’est pas lancé : démarre-le avec make run.';
        const shown = (el: Element | null): boolean =>
            !el || (getComputedStyle(el).display !== 'none' && shown(el.parentElement));
        const happyDOM = (window as unknown as {happyDOM: {setViewport: (size: object) => void}}).happyDOM;
        for (const width of [375, 1280]) {
            happyDOM.setViewport({width, height: 800});
            const input = await open(vi.fn().mockRejectedValue(new ApiError(503, why)));
            await type(input, 'oui de la tete');
            await press(input, 'Enter');
            const status = document.querySelector('[role="dialog"] [role="status"]') as HTMLElement;
            expect(status.textContent).toBe(why);
            expect(shown(status), `at ${width} px`).toBe(true);
            expect(getComputedStyle(status).whiteSpace, `at ${width} px`).not.toBe('nowrap'); // never cut by « … »
            act(() => root?.unmount());
            root = null;
            document.body.innerHTML = '';
        }
        happyDOM.setViewport({width: 1024, height: 768});
    });
});
