// @vitest-environment happy-dom
// The scene editor: its text helpers, the editor's marks and insert menu, and the dialog's live analysis
// (debounced), robot progress and waiting play, against the Mac's real payloads (contract-examples).
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeAll, beforeEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {
    sceneCheckSchema,
    sceneOutSchema,
    sceneVocabularySchema,
    type RobotEvent,
    type SceneCheck,
    type SceneOut,
    type SceneRender
} from '../src/api/schemas';
import {ApiError} from '../src/api/client';
import {Editor} from '../src/scenes/Editor';
import {colourOf, colours, insert, lineRange, marks, paint, snippet} from '../src/scenes/text';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
const AREA = 'textarea[aria-label="Texte de la scène"]'; // the scene's text, not the field that asks for one

const VOCABULARY = sceneVocabularySchema.parse(examples.SceneVocabulary[0]);
const CHECK = sceneCheckSchema.parse(examples.SceneCheck[0]);

const mocks = vi.hoisted(() => ({
    check: vi.fn(),
    get: vi.fn(),
    save: vi.fn(),
    emotions: vi.fn(),
    remove: vi.fn(),
    playScene: vi.fn(),
    stopScene: vi.fn(),
    conversation: null as ((event: RobotEvent) => void) | null,
    render: null as ((progress: SceneRender) => void) | null
}));
vi.mock('../src/scenes/api', () => ({
    scenesApi: {
        list: vi.fn().mockResolvedValue({
            scenes: [
                {id: 'fete-surprise', title: 'La fête surprise', builtin: true, errors: 0, segments: 1, rendered: 1},
                {id: 'croissant', title: 'Le croissant disparu', builtin: true, errors: 0, segments: 1, rendered: 1}
            ]
        }),
        get: mocks.get,
        save: mocks.save,
        vocabulary: vi.fn(async () => VOCABULARY),
        check: mocks.check,
        emotions: mocks.emotions,
        remove: mocks.remove,
        builds: vi.fn(async () => ({builds: []}))
    }
}));
vi.mock('../src/api/client', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../src/api/client')>()),
    api: {playScene: mocks.playScene, stopScene: mocks.stopScene}
}));
vi.mock('../src/live', () => {
    const live = {
        // stable across renders, as the real context's callbacks and snapshot are
        link: 'up',
        session: {
            awake: true,
            session_id: 's',
            phase: 'idle',
            cost_usd: 0,
            last_latency_ms: null,
            manette: null,
            antenna_wake: true,
            scene: null
        },
        on: (channel: string, listener: (progress: SceneRender) => void) => {
            if (channel === 'scene_render') mocks.render = listener;
            return () => {};
        },
        onConversation: (listener: (event: RobotEvent) => void) => {
            mocks.conversation = listener;
            return () => {};
        }
    };
    return {useLive: () => live, useShowing: () => {}};
});

describe('text helpers', () => {
    it('finds a line, writes a word, and spaces an insertion', () => {
        const text = 'un\ndeux\ntrois';
        expect(lineRange(text, 2)).toEqual([3, 7]);
        expect(lineRange(text, 3)).toEqual([8, 13]);
        expect(lineRange(text, 9)).toEqual([13, 13]);
        expect([snippet('emotions', 'sad1'), snippet('sounds', 'rire'), snippet('voices', 'Kore')]).toEqual([
            '[sad1]',
            '<rire>',
            '(Kore)'
        ]);
        expect(insert('Bonjour.Fin', 8, 8, '[sad1]')).toEqual({text: 'Bonjour. [sad1]Fin', caret: 15});
        expect(insert('Bonjour. ', 9, 9, '<rire>')).toEqual({text: 'Bonjour. <rire>', caret: 15});
        expect(insert('Bonjour, toi', 7, 7, '[sad1]').text).toBe('Bonjour [sad1], toi'); // said as before
        expect(insert('joues-tu?', 8, 8, '[sad1]').text).toBe('joues-tu [sad1]?');
    });

    it("keeps a check's marks on their lines while lines are added above them", () => {
        const say = CHECK.cues.find((cue) => cue.kind === 'say');
        if (!say) throw new Error('the example check has a say cue');
        const check: SceneCheck = {
            ...CHECK,
            cues: [
                {...say, line: 2, end_line: 3, ready: true},
                {...say, line: 5, end_line: null, ready: false}
            ],
            errors: [{line: 5, message: 'x'}],
            warnings: []
        };
        const text = 'a\nb\nc\n\nd';
        expect([...marks(check, text, text)]).toEqual([
            [2, {voice: 'ready', who: 'NARRATEUR', first: true, last: false}],
            [3, {voice: 'ready', who: 'NARRATEUR', first: false, last: true}],
            [5, {voice: 'missing', who: 'NARRATEUR', first: true, last: true, error: true}]
        ]);
        const typed = marks(check, text, 'a\nNEW\nb\nc\n\nd'); // a line typed under the first, not yet checked
        expect([...typed.keys()]).toEqual([3, 4, 6]);
        expect(marks(check, text, 'a\nb!\nc\n\nd').has(2)).toBe(false); // the line being edited waits for its check
    });

    it('paints each line as the parser reads it, each character in the colour of its rank in the cast', () => {
        const text =
            '@MARCO (Algenib, -60) : bourru\n@LUCIE (Leda) : rieuse\n\nIl part. [sad1]\n\n' +
            'MARCO (fort) : [pause 1] Oh <rire> toi\nsuite\n\n// note';
        const palette = colours(text);
        expect([...palette.keys()]).toEqual(['NARRATEUR', 'MARCO', 'LUCIE']);
        const lines = paint(text, palette);
        expect(lines.map((line) => line.who)).toEqual([
            'MARCO',
            'LUCIE',
            null,
            'NARRATEUR',
            null,
            'MARCO',
            'MARCO',
            null,
            null
        ]);
        expect(lines[5].parts).toEqual([
            ['MARCO', 'name'],
            [' (fort) : ', 'aside'],
            ['[pause 1]', 'still'],
            [' Oh ', 'said'],
            ['<rire>', 'sound'],
            [' toi', 'said']
        ]);
        expect(lines[3].parts.at(-1)).toEqual(['[sad1]', 'move']);
        expect(lines.map((line) => line.parts.map(([part]) => part).join(''))).toEqual(text.split('\n')); // every letter
        const tight = 'LÉA : Bonjour\u00a0\u00a0\n@LÉA (Kore) : x\n\nLÉA : Encore.';
        const read = paint(tight, colours(tight));
        expect(read[0].parts.map(([part]) => part).join('')).toBe('LÉA : Bonjour\u00a0\u00a0'); // kept: it wraps
        expect([read[0].parts[0][1], read[3].who]).toEqual(['name', 'LÉA']); // in capitals: a role, as the parser reads it
        const above = paint('Léa : Bonjour\n\n@LÉA (Kore) : x', colours('Léa : Bonjour\n\n@LÉA (Kore) : x'));
        expect([above[0].who, above[0].parts[0][1]]).toEqual(['NARRATEUR', 'said']); // not cast yet: narration
        expect(colourOf(palette, 'MARCO')).not.toBe(colourOf(palette, 'LUCIE'));
    });
});

describe('the editor', () => {
    let root: Root | null = null;
    afterEach(() => {
        act(() => root?.unmount());
        root = null;
        document.body.innerHTML = '';
    });

    const mount = async (element: ReturnType<typeof createElement>) => {
        const host = document.createElement('div');
        document.body.append(host);
        root = createRoot(host);
        await act(async () => root?.render(element));
    };

    const TEXT = '@NARRATEUR (Charon) : conteur\n\nIl était une fois.\n\nMARCO : Je suis (en riant) content.';
    const errors: SceneCheck = {
        ...CHECK,
        errors: [{line: 5, message: 'une indication de jeu se met au début de la réplique : MARCO (en riant) : …'}],
        warnings: [{line: 3, message: '[tourne 160] : limité à 100°'}]
    };

    const props = {onText: () => {}, checked: TEXT, vocabulary: VOCABULARY, rendering: false, onPlayFrom: () => {}};

    it('marks the lines whose voice is ready, and those still to render, with no legend', async () => {
        const say = CHECK.cues.find((cue) => cue.kind === 'say');
        if (!say) throw new Error('the example check has a say cue');
        const check: SceneCheck = {
            ...CHECK,
            errors: [],
            warnings: [],
            cues: [
                {...say, line: 3, end_line: null, ready: true},
                {...say, line: 5, end_line: null, ready: false, key: 'other'}
            ]
        };
        await mount(createElement(Editor, {...props, text: TEXT, check, current: null}));
        const voice = (line: number) =>
            document.querySelector(`[data-line="${line}"]`)?.getAttribute('data-voice') ?? null;
        expect([voice(1), voice(3), voice(5)]).toEqual([null, 'ready', 'missing']);
        expect(document.body.textContent).not.toContain('voix prête');
    });

    it('marks the lines in error and selects one from the list', async () => {
        await mount(createElement(Editor, {...props, text: TEXT, check: errors, current: 3}));
        const marked = [...document.querySelectorAll('[data-error]')].map((el) => el.getAttribute('data-line'));
        expect(marked).toEqual(['5']);
        const items = [...document.querySelectorAll('[role="listitem"]')];
        expect(items.map((el) => el.textContent)).toEqual([
            'ligne 3 : [tourne 160] : limité à 100°',
            'ligne 5 : une indication de jeu se met au début de la réplique : MARCO (en riant) : …'
        ]);
        await act(async () => (items[1] as HTMLElement).click());
        const area = document.querySelector(AREA) as HTMLTextAreaElement;
        expect([area.selectionStart, area.selectionEnd]).toEqual(lineRange(TEXT, 5));
    });

    const openMenu = async () => {
        const area = document.querySelector(AREA) as HTMLTextAreaElement;
        await act(async () => area.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true})));
    };
    const items = () => [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')];

    it('inserts a word of the vocabulary at the caret, and leaves the view where it was', async () => {
        let view: HTMLElement | null = null;
        const onText = vi.fn(() => view && (view.scrollTop = 999)); // the browser following the caret to the end
        await mount(createElement(Editor, {...props, text: TEXT, onText, check: null, current: null}));
        const area = document.querySelector(AREA) as HTMLTextAreaElement;
        view = area.parentElement?.parentElement as HTMLElement;
        area.setSelectionRange(lineRange(TEXT, 3)[0], lineRange(TEXT, 3)[0]);
        view.scrollTop = 120;
        await openMenu();
        expect(document.body.textContent).toContain('Joie'); // the moves by the feeling they show
        await act(async () =>
            items()
                .find((el) => el.textContent?.includes('Joyeux'))
                ?.click()
        );
        expect(onText).toHaveBeenCalledWith(TEXT.replace('Il était', '[cheerful1] Il était'));
        await act(async () => new Promise<void>((done) => requestAnimationFrame(() => done())));
        expect([view?.scrollTop, area.selectionStart]).toEqual([120, lineRange(TEXT, 3)[0] + '[cheerful1] '.length]);
    });

    it('finds a word of any kind by searching, accents aside', async () => {
        await mount(createElement(Editor, {...props, text: TEXT, check: null, current: null}));
        await openMenu();
        expect(items().map((el) => el.textContent)).toEqual(['Joyeux']); // the first kind
        const search = document.querySelector<HTMLInputElement>('input[placeholder^="Chercher"]');
        await act(async () => {
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(search, 'TETE');
            search?.dispatchEvent(new Event('input', {bubbles: true}));
        });
        expect(items().map((el) => el.textContent)).toEqual(['Oui de la tête']);
    });

    it('opens « Insérer » from a button too, for a touch screen or the keyboard: no right click there', async () => {
        const onText = vi.fn();
        await mount(createElement(Editor, {...props, text: TEXT, onText, check: null, current: null}));
        const area = document.querySelector(AREA) as HTMLTextAreaElement;
        area.setSelectionRange(lineRange(TEXT, 3)[0], lineRange(TEXT, 3)[0]);
        const button = [...document.querySelectorAll('button')].find((el) => el.textContent === 'Insérer');
        expect(button, 'a button « Insérer »').toBeDefined();
        await act(async () => button?.click());
        await act(async () =>
            items()
                .find((el) => el.textContent?.includes('Joyeux'))
                ?.click()
        );
        expect(onText).toHaveBeenCalledWith(TEXT.replace('Il était', '[cheerful1] Il était'));
    });

    it('lets a phone scroll to the kinds its width cannot show', async () => {
        const happyDOM = (window as unknown as {happyDOM: {setViewport: (size: object) => void}}).happyDOM;
        happyDOM.setViewport({width: 375, height: 812});
        // The browser's verdict at 375 px: the first kind is in view, the last one is not.
        class Seen {
            constructor(private readonly report: (entries: {isIntersecting: boolean}[]) => void) {}
            observe(tab: Element) {
                this.report([{isIntersecting: tab === tab.parentElement?.firstElementChild}]);
            }
            disconnect() {}
        }
        vi.stubGlobal('IntersectionObserver', Seen);
        try {
            await mount(createElement(Editor, {...props, text: TEXT, check: null, current: null}));
            await openMenu();
            const next = document.querySelector('.MuiTabScrollButton-root:not(.Mui-disabled)');
            const shown = (el: Element | null): boolean =>
                !el || (getComputedStyle(el).display !== 'none' && shown(el.parentElement));
            expect(next, 'a button to scroll to the next kinds').not.toBeNull();
            expect(shown(next)).toBe(true);
        } finally {
            vi.unstubAllGlobals();
            happyDOM.setViewport({width: 1024, height: 768});
        }
    });
});

// The editor's chunk (MUI and the editor) is compiled once, here: on a loaded machine the first test that imported it
// went past the 5 s a test is given.
beforeAll(async () => {
    await import('../src/scenes/Scenes');
}, 60_000);

describe('the dialog', () => {
    let root: Root | null = null;
    beforeEach(() => {
        vi.useFakeTimers();
        mocks.get.mockImplementation(async () => sceneOutSchema.parse(examples.SceneOut[0]));
        mocks.check.mockResolvedValue(CHECK);
    });
    afterEach(() => {
        act(() => root?.unmount());
        root = null;
        document.body.innerHTML = '';
        vi.useRealTimers();
    });

    it('analyses the text once typing pauses, and shows what the robot plays', async () => {
        mocks.check.mockResolvedValue({...CHECK, errors: [{line: 2, message: 'crochet mal fermé'}]});
        const {default: Scenes} = await import('../src/scenes/Scenes');
        const host = document.createElement('div');
        document.body.append(host);
        root = createRoot(host);
        await act(async () => root?.render(createElement(Scenes, {onClose: () => {}, sceneId: 'fete-surprise'})));
        await act(async () => vi.advanceTimersByTimeAsync(0));
        mocks.check.mockClear();
        const area = document.querySelector(AREA) as HTMLTextAreaElement;
        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
        for (const text of ['a', 'ab', 'ab [sad']) {
            await act(async () => {
                setter?.call(area, text);
                area.dispatchEvent(new Event('input', {bubbles: true}));
                await vi.advanceTimersByTimeAsync(100);
            });
        }
        expect(mocks.check).not.toHaveBeenCalled();
        await act(async () => vi.advanceTimersByTimeAsync(300));
        expect(mocks.check.mock.calls).toEqual([['ab [sad']]);
        expect(document.body.textContent).toContain('ligne 2 : crochet mal fermé');
        expect(document.body.textContent).toContain('elle ne peut pas se jouer');
        const event = {
            kind: 'scene',
            session_id: 's',
            scene_id: 'fete-surprise',
            title: 'La fête surprise',
            state: 'line',
            line: 1,
            message: null
        } as const;
        await act(async () => mocks.conversation?.(event));
        expect(document.body.textContent).toContain('Le robot joue « La fête surprise » : ligne 1.');
        expect([...document.querySelectorAll('[data-current]')].map((el) => el.getAttribute('data-line'))).toEqual([
            '1'
        ]);
    });
});

describe('the dialog, asked and saved', () => {
    let root: Root | null = null;
    beforeEach(() => {
        mocks.get.mockImplementation(async () => sceneOutSchema.parse(examples.SceneOut[0]));
        mocks.check.mockResolvedValue(CHECK);
        mocks.playScene.mockResolvedValue(undefined); // the robot takes the scene; a test that wants a refusal says so
    });
    afterEach(async () => {
        await act(async () => root?.unmount());
        root = null;
        document.body.innerHTML = '';
        vi.clearAllMocks();
    });

    const show = async (props: {sceneId?: string; asked?: number}) => {
        const {default: Scenes} = await import('../src/scenes/Scenes');
        if (!root) {
            const host = document.createElement('div');
            document.body.append(host);
            root = createRoot(host);
        }
        await act(async () => root?.render(createElement(Scenes, {onClose: () => {}, ...props})));
    };
    const click = (text: string) =>
        act(async () =>
            [...document.querySelectorAll<HTMLElement>('button, [role="button"]')]
                .find((el) => el.textContent?.startsWith(text))
                ?.click()
        );

    it('shows the scene the palette asks for while open, again after the user picked another', async () => {
        await show({sceneId: 'fete-surprise', asked: 1});
        expect(mocks.get).toHaveBeenLastCalledWith('fete-surprise');
        await show({sceneId: 'croissant', asked: 2});
        expect(mocks.get).toHaveBeenLastCalledWith('croissant');
        await click('La fête surprise');
        expect(mocks.get).toHaveBeenLastCalledWith('fete-surprise');
        await show({sceneId: 'croissant', asked: 3}); // the same scene, asked again
        expect(mocks.get).toHaveBeenLastCalledWith('croissant');
    });

    const text = () => document.querySelector<HTMLTextAreaElement>(AREA)?.value;
    /** ⋮, its one item (named by how it starts), then the dialog's button. */
    const removeWith = async (item: string, confirm: string) => {
        await act(async () => document.querySelector<HTMLElement>('button[aria-label="Plus"]')?.click());
        await act(async () =>
            [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')]
                .find((el) => el.textContent?.startsWith(item))
                ?.click()
        );
        await act(async () =>
            [...document.querySelectorAll<HTMLElement>('.MuiDialogActions-root button')]
                .find((el) => el.textContent === confirm)
                ?.click()
        );
    };

    it("deletes the owner's scene once confirmed, says why the Mac refused, and shows the first one listed", async () => {
        mocks.get.mockImplementation(async (id: string) =>
            sceneOutSchema.parse({...examples.SceneOut[0], id, title: id, text: `TEXT ${id}`, builtin: id !== 'mine'})
        );
        let refusals = 1; // no value queued: one left unused would answer the next test
        mocks.remove.mockImplementation(async () => {
            if (refusals-- > 0) throw new ApiError(404, 'La scène « mine » n’existe pas.');
        });
        await show({sceneId: 'mine'});
        expect(text()).toBe('TEXT mine');
        await removeWith('Supprimer la scène', 'Supprimer');
        expect(mocks.remove).toHaveBeenCalledExactlyOnceWith('mine');
        expect(document.querySelector('.MuiAlert-message')?.textContent).toBe('La scène « mine » n’existe pas.');
        expect(text()).toBe('TEXT mine'); // still there
        await removeWith('Supprimer la scène', 'Supprimer');
        expect(mocks.remove).toHaveBeenLastCalledWith('mine');
        expect(mocks.get).toHaveBeenLastCalledWith('fete-surprise'); // gone: the first one listed in its place
        expect(text()).toBe('TEXT fete-surprise');
    });

    it('restores a built-in scene the owner changed, and shows it as shipped', async () => {
        let shipped = false;
        mocks.get.mockImplementation(async () =>
            sceneOutSchema.parse({
                ...examples.SceneOut[0],
                text: shipped ? 'AS SHIPPED' : 'EDITED',
                builtin: true,
                edited: !shipped
            })
        );
        mocks.remove.mockImplementation(async () => void (shipped = true));
        await show({sceneId: 'fete-surprise'});
        expect(text()).toBe('EDITED');
        await removeWith('Rétablir', 'Rétablir'); // « Rétablir l'original »
        expect(mocks.remove).toHaveBeenCalledExactlyOnceWith('fete-surprise');
        expect(text()).toBe('AS SHIPPED'); // the same scene, open as shipped
        expect(document.querySelector('button[aria-label="Plus"]')).toBeNull(); // nothing more to restore
    });

    it("shows what a new scene's first « Jouer sur le robot » answers, in the controls that stay", async () => {
        mocks.save.mockImplementation(async (_id: null, title: string, text: string) =>
            sceneOutSchema.parse({...examples.SceneOut[0], id: 'nouvelle-scene', title, text, builtin: false})
        );
        mocks.playScene.mockRejectedValue(new ApiError(409, 'Les voix de cette scène ne sont pas prêtes.'));
        await show({});
        await click('Nouvelle scène');
        await click('Jouer sur le robot');
        expect(mocks.save).toHaveBeenCalledWith(null, 'Nouvelle scène', expect.stringContaining('@REACHY'));
        expect(mocks.playScene).toHaveBeenCalledWith('nouvelle-scene', {moves: true, rehearse: null, start: null});
        expect(document.body.textContent).toContain('Les voix de cette scène ne sont pas prêtes.');
    });

    /** Types `value` into the scene's text, as a keyboard would. */
    const typeText = (value: string) =>
        act(async () => {
            const area = document.querySelector(AREA) as HTMLTextAreaElement;
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set?.call(area, value);
            area.dispatchEvent(new Event('input', {bubbles: true}));
        });

    it('saves the edits of a saved scene on « Enregistrer », and on « Jouer sur le robot » before playing', async () => {
        mocks.save.mockImplementation(async (id: string, title: string, text: string) =>
            sceneOutSchema.parse({...examples.SceneOut[0], id, title, text, builtin: true, edited: true})
        );
        await show({sceneId: 'fete-surprise'});
        const edited = `${text()}\nNARRATEUR : Fin.`;
        await typeText(edited);
        await click('Enregistrer');
        expect(mocks.save).toHaveBeenCalledExactlyOnceWith('fete-surprise', 'La fête surprise', edited);
        expect(document.body.textContent).not.toContain('Enregistrer'); // saved: nothing left to save
        mocks.save.mockClear();
        await typeText(`${edited}\nNARRATEUR : Rideau.`);
        await click('Jouer sur le robot');
        expect(mocks.save).toHaveBeenCalledExactlyOnceWith(
            'fete-surprise',
            'La fête surprise',
            `${edited}\nNARRATEUR : Rideau.`
        );
        expect(mocks.playScene).toHaveBeenCalledExactlyOnceWith('fete-surprise', {
            moves: true,
            rehearse: null,
            start: null
        });
        expect(mocks.save.mock.invocationCallOrder[0]).toBeLessThan(mocks.playScene.mock.invocationCallOrder[0]);
    });

    it('rehearses a character: the options go to the robot', async () => {
        await show({sceneId: 'fete-surprise'});
        expect(document.body.textContent).not.toContain('touche une antenne'); // no character: a play
        const option = async (text: string) => {
            await act(async () => document.querySelector<HTMLElement>('[aria-label="Options du jeu"]')?.click());
            const items = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')];
            expect(items.map((el) => el.textContent)).toEqual([
                'Personne : le robot joue tout',
                'NARRATEUR',
                'Sans mouvements expressifs'
            ]);
            await act(async () => items.find((el) => el.textContent === text)?.click());
        };
        await option('NARRATEUR');
        expect(document.body.textContent).toContain(
            'Sur les répliques de NARRATEUR, le robot se tait : touche une antenne pour passer à la suite.'
        );
        await option('Sans mouvements expressifs');
        expect(document.body.textContent).toContain('Répétition : NARRATEURSans mouvements'); // the options in force
        await click('Jouer sur le robot');
        expect(mocks.playScene).toHaveBeenCalledWith('fete-surprise', {
            moves: false,
            rehearse: 'NARRATEUR',
            start: null
        });
    });

    it('plays on its own once the voices it waited for are rendered', async () => {
        const preparing = new ApiError(
            409,
            'Les voix de « La fête surprise » se préparent (1/2) : réessaie dans une minute.'
        );
        mocks.playScene.mockResolvedValue(undefined).mockRejectedValueOnce(preparing).mockRejectedValueOnce(preparing);
        await show({sceneId: 'fete-surprise'});
        await click('Jouer sur le robot');
        expect(document.body.textContent).toContain('la scène se jouera dès qu’elles seront prêtes');
        const progress = {scene_id: 'fete-surprise', done: 1, total: 2, error: null};
        await act(async () => mocks.render?.(progress));
        expect(mocks.playScene).toHaveBeenCalledTimes(1);
        await act(async () => mocks.render?.({...progress, done: 2})); // refused once more: it keeps waiting
        expect(mocks.playScene).toHaveBeenCalledTimes(2);
        expect(document.body.textContent).toContain('la scène se jouera dès qu’elles seront prêtes');
        await act(async () => {
            mocks.render?.({...progress, done: 2}); // two final frames back to back: one request
            mocks.render?.({...progress, done: 2});
        });
        expect(mocks.playScene).toHaveBeenCalledTimes(3);
        expect(document.body.textContent).toContain('Arrêter'); // queued: the robot is about to play
        expect(mocks.playScene).toHaveBeenLastCalledWith('fete-surprise', {moves: true, rehearse: null, start: null});
        await act(async () => mocks.render?.({...progress, done: 2}));
        expect(mocks.playScene).toHaveBeenCalledTimes(3); // played: no longer waiting
    });

    it('shows one button: « Arrêter » while the robot plays the scene, « Jouer » otherwise', async () => {
        await show({sceneId: 'fete-surprise'});
        const event = {kind: 'scene', session_id: 's', scene_id: 'fete-surprise', title: 'La fête surprise'} as const;
        const buttons = () => [...document.querySelectorAll('button')].map((b) => b.textContent?.trim());
        expect(buttons()).toContain('Jouer sur le robot');
        expect(buttons()).not.toContain('Arrêter');
        await act(async () => mocks.conversation?.({...event, state: 'started', line: null, message: null}));
        expect(buttons()).toContain('Arrêter');
        expect(buttons()).not.toContain('Jouer sur le robot');
        await act(async () => mocks.conversation?.({...event, state: 'ended', line: null, message: null}));
        expect(buttons()).toContain('Jouer sur le robot');
    });

    it('stops the scene the robot plays from the editor, and says why a stop failed', async () => {
        mocks.stopScene
            .mockResolvedValue(undefined)
            .mockRejectedValueOnce(new ApiError(503, 'Le robot ne répond pas.'));
        await show({sceneId: 'fete-surprise'});
        const event = {kind: 'scene', session_id: 's', scene_id: 'fete-surprise', title: 'La fête surprise'} as const;
        const playing = () => mocks.conversation?.({...event, state: 'started', line: null, message: null});
        await act(async () => playing());
        await click('Arrêter');
        expect(mocks.stopScene).toHaveBeenCalledTimes(1);
        expect(document.querySelector('[role="alert"]')?.textContent).toBe('Le robot ne répond pas.');
        await act(async () => playing()); // still playing, as the robot says
        await click('Arrêter');
        expect(mocks.stopScene).toHaveBeenCalledTimes(2);
        expect(document.querySelector('[role="alert"]')).toBeNull();
    });

    it('stops waiting, with its reason, when the voices it waited for fail to render', async () => {
        mocks.playScene.mockRejectedValue(
            new ApiError(409, 'Les voix de « La fête surprise » se préparent (1/2) : réessaie.')
        );
        await show({sceneId: 'fete-surprise'});
        await click('Jouer sur le robot');
        expect(document.body.textContent).toContain('Voix en préparation, annuler');
        const progress = {scene_id: 'fete-surprise', done: 1, total: 2};
        await act(async () => mocks.render?.({...progress, error: 'La voix de MARCO a échoué.'}));
        expect(document.querySelector('[role="alert"]')?.textContent).toBe('La voix de MARCO a échoué.');
        expect(document.body.textContent).not.toContain('Voix en préparation, annuler');
        await act(async () => mocks.render?.({...progress, done: 2, error: null})); // no play waits any more
        expect(mocks.playScene).toHaveBeenCalledTimes(1);
    });

    it('shows none of another scene’s render: neither its progress, its voice failure nor its count', async () => {
        const out = sceneOutSchema.parse(examples.SceneOut[0]);
        const check = {...out.check, errors: [], segments: 4, rendered: 3};
        mocks.get.mockResolvedValue({...out, check});
        mocks.check.mockResolvedValue(check);
        await show({sceneId: 'fete-surprise'});
        expect(document.body.textContent).toContain('3/4\u00a0voix prêtes');
        // another scene, saved just before, renders its voices meanwhile
        const other = {scene_id: 'croissant', done: 1, total: 4, error: null};
        await act(async () => mocks.render?.(other));
        expect(document.querySelector('.MuiLinearProgress-root')).toBeNull();
        expect(document.body.textContent).toContain('3/4\u00a0voix prêtes');
        await act(async () => mocks.render?.({...other, error: 'La voix de MARCO a échoué.'}));
        expect(document.body.textContent).not.toContain('La voix de MARCO a échoué.');
    });

    it("gives « Jouer » back, with the robot's reason, when the robot does not start the play asked", async () => {
        await show({sceneId: 'fete-surprise'});
        const event = {kind: 'scene', session_id: 's', scene_id: 'fete-surprise', title: 'La fête surprise'} as const;
        const buttons = () => [...document.querySelectorAll('button')].map((b) => b.textContent?.trim());
        const reason = 'Le robot se réveille : réessaie dans un instant.';
        vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout']});
        try {
            await click('Jouer sur le robot');
            expect(buttons()).toContain('Arrêter'); // queued
            await act(async () => mocks.conversation?.({...event, state: 'failed', line: null, message: reason}));
            expect(buttons()).toContain('Jouer sur le robot');
            await act(async () => void vi.advanceTimersByTime(15_000));
            expect(document.body.textContent).toContain(reason);
            expect(document.body.textContent).not.toContain('est-il allumé');
        } finally {
            vi.useRealTimers();
        }
    });

    it('says why « Jouer » came back when the robot never starts the play asked, and only then', async () => {
        await show({sceneId: 'fete-surprise'});
        const event = {kind: 'scene', session_id: 's', scene_id: 'fete-surprise', title: 'La fête surprise'} as const;
        const buttons = () => [...document.querySelectorAll('button')].map((b) => b.textContent?.trim());
        const alert = () => document.querySelector('[role="alert"]')?.textContent ?? '';
        const wait = (ms: number) => act(async () => void vi.advanceTimersByTime(ms));
        vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout']});
        try {
            await click('Jouer sur le robot');
            await wait(14_000);
            await act(async () => mocks.conversation?.({...event, state: 'started', line: null, message: null}));
            await wait(30_000); // taken in time: it plays, nothing to say
            expect(buttons()).toContain('Arrêter');
            expect(alert()).toBe('');
            await act(async () => mocks.conversation?.({...event, state: 'ended', line: null, message: null}));

            await click('Jouer sur le robot'); // the command queued, never taken
            await wait(14_999);
            expect(buttons()).toContain('Arrêter');
            expect(alert()).toBe('');
            await wait(1);
            expect(buttons()).toContain('Jouer sur le robot');
            expect(alert()).toMatch(/pas lancé la scène\u00a0: est-il allumé et connecté\u00a0\?$/);
        } finally {
            vi.useRealTimers();
        }
    });

    it('keeps « Arrêter » through the end of the run a jump replaces, not past the end of the jump itself', async () => {
        await show({sceneId: 'fete-surprise'});
        const event = {kind: 'scene', session_id: 's', scene_id: 'fete-surprise', title: 'La fête surprise'} as const;
        const buttons = () => [...document.querySelectorAll('button')].map((b) => b.textContent?.trim());
        const area = document.querySelector(AREA) as HTMLTextAreaElement;
        const rects = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
            this: HTMLElement
        ) {
            const line = Number(this.dataset.line);
            return {left: 0, top: line ? (line - 1) * 21 : 0, bottom: line ? line * 21 : 2000, right: 800} as DOMRect;
        });
        await act(async () => mocks.conversation?.({...event, state: 'line', line: 1, message: null}));
        await act(async () =>
            area.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: 10, clientY: 2}))
        );
        rects.mockRestore();
        expect(mocks.playScene).toHaveBeenLastCalledWith('fete-surprise', {moves: true, rehearse: null, start: 1});
        const stopped = (message: string) => mocks.conversation?.({...event, state: 'stopped', line: null, message});
        await act(async () => stopped('Arrêtée par une autre scène.')); // the run the jump replaces
        expect(buttons()).toContain('Arrêter');
        await act(async () => stopped('Arrêtée par le réveil du robot.')); // the jump, dropped before it started
        expect(buttons()).toContain('Jouer sur le robot');
    });

    it('plays the scene from a line when its number is clicked, even while the robot plays', async () => {
        await show({sceneId: 'fete-surprise'});
        const area = document.querySelector(AREA) as HTMLTextAreaElement;
        const rect = (top: number, bottom: number) => ({left: 0, top, bottom, right: 800}) as DOMRect;
        const rects = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
            this: HTMLElement
        ) {
            const line = Number(this.dataset.line);
            return line ? rect((line - 1) * 21, line * 21) : rect(0, 2000);
        });
        await act(async () => mocks.conversation?.({kind: 'scene', session_id: 's', scene_id: 'fete-surprise',
            title: 'La fête surprise', state: 'line', line: 1, message: null})); // prettier-ignore
        await act(async () =>
            area.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: 10, clientY: 3}))
        );
        expect(area.title).toBe('Jouer à partir de la ligne 1');
        await act(async () =>
            area.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: 10, clientY: 2}))
        );
        expect(mocks.playScene).toHaveBeenLastCalledWith('fete-surprise', {moves: true, rehearse: null, start: 1});
        await act(async () =>
            area.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: 300, clientY: 2}))
        );
        expect(mocks.playScene).toHaveBeenCalledTimes(1); // a click in the text only places the caret
        rects.mockRestore();
    });

    it('asks again when the voices end while a request is on its way and that request is refused', async () => {
        const preparing = new ApiError(409, 'Les voix de « La fête surprise » se préparent (1/2) : réessaie.');
        let refuse: (err: Error) => void = () => {};
        mocks.playScene
            .mockResolvedValue(undefined)
            .mockRejectedValueOnce(preparing)
            .mockImplementationOnce(() => new Promise((_, no) => (refuse = no)));
        await show({sceneId: 'fete-surprise'});
        await click('Jouer sur le robot');
        const done = {scene_id: 'fete-surprise', done: 2, total: 2, error: null};
        await act(async () => mocks.render?.(done)); // a request goes
        await act(async () => mocks.render?.(done)); // the last voice ends while it is on its way
        expect(mocks.playScene).toHaveBeenCalledTimes(2);
        await act(async () => refuse(preparing)); // refused all the same: asked once more
        expect(mocks.playScene).toHaveBeenCalledTimes(3);
        expect(document.body.textContent).toContain('Arrêter');
    });

    it('keeps « Arrêter » for the scene the robot plays when another one is opened, then back', async () => {
        await show({sceneId: 'fete-surprise'});
        await act(async () => mocks.conversation?.({kind: 'scene', session_id: 's', scene_id: 'fete-surprise',
            title: 'La fête surprise', state: 'line', line: 1, message: null})); // prettier-ignore
        const buttons = () => [...document.querySelectorAll('button')].map((b) => b.textContent?.trim());
        await click('Le croissant disparu');
        expect(buttons()).toContain('Jouer sur le robot');
        await click('La fête surprise');
        expect(buttons()).toContain('Arrêter');
        expect([...document.querySelectorAll('[data-current]')].map((el) => el.getAttribute('data-line'))).toEqual([
            '1'
        ]);
    });

    it('drops an emotions answer that comes once another scene is open', async () => {
        let answer: (value: {text: string; added: number}) => void = () => {};
        mocks.emotions.mockReturnValue(new Promise((done) => (answer = done)));
        await show({sceneId: 'fete-surprise'});
        await click('Ajouter des émotions');
        await click('Le croissant disparu');
        const before = (document.querySelector(AREA) as HTMLTextAreaElement).value;
        await act(async () => answer({text: 'MARCO : [sad1] Autre chose.', added: 1}));
        expect((document.querySelector(AREA) as HTMLTextAreaElement).value).toBe(before);
    });

    it('drops a scene that comes once another is open, so « Enregistrer » never writes it there', async () => {
        const answers: Record<string, (scene: SceneOut) => void> = {};
        mocks.get.mockImplementation((id: string) => new Promise((done) => (answers[id] = done)));
        const scene = (id: string, text: string) =>
            sceneOutSchema.parse({...examples.SceneOut[0], id, title: id, text, builtin: false});
        await show({sceneId: 'fete-surprise'});
        await act(async () => answers['fete-surprise'](scene('fete-surprise', 'TEXTE FETE')));
        await click('Le croissant disparu');
        await click('La fête surprise');
        await click('Le croissant disparu'); // its answer comes first, then the one for the scene left
        await act(async () => answers['croissant'](scene('croissant', 'TEXTE CROISSANT')));
        await act(async () => answers['fete-surprise'](scene('fete-surprise', 'TEXTE FETE')));
        expect(document.querySelector('.Mui-selected')?.textContent).toContain('Le croissant disparu');
        expect((document.querySelector(AREA) as HTMLTextAreaElement).value).toBe('TEXTE CROISSANT');
    });

    it('adds emotions with Claude: the draft replaced, unsaved, and put back by « Annuler »', async () => {
        await show({sceneId: 'fete-surprise'});
        const area = () => document.querySelector(AREA) as HTMLTextAreaElement;
        const before = area().value;
        mocks.emotions.mockResolvedValue({text: `${before}\n[sad1] Fin.`, added: 1});
        await click('Ajouter des émotions');
        expect(mocks.emotions).toHaveBeenCalledWith('La fête surprise', before);
        expect(area().value).toBe(`${before}\n[sad1] Fin.`);
        expect(document.body.textContent).toContain('1 émotion ajoutée, voix inchangées');
        expect(mocks.save).not.toHaveBeenCalled();
        await click('Annuler');
        expect(area().value).toBe(before);
    });

    it('keeps what was typed while Claude read, and says why nothing was added or what failed', async () => {
        let answer: (value: {text: string; added: number}) => void = () => {};
        mocks.emotions.mockReturnValue(new Promise((done) => (answer = done)));
        await show({sceneId: 'fete-surprise'});
        const sent = text();
        await click('Ajouter des émotions');
        const typed = `${sent}\nNARRATEUR : Tapé pendant ce temps.`;
        await typeText(typed);
        await act(async () => answer({text: `${sent}\n[sad1] Fin.`, added: 1}));
        expect(text()).toBe(typed); // Claude's answer, for the text sent, never replaces the newer one
        expect(document.body.textContent).toContain('Le texte a changé pendant que Claude lisait');
        mocks.emotions.mockResolvedValue({text: typed, added: 0});
        await click('Ajouter des émotions');
        expect(text()).toBe(typed);
        expect(document.body.textContent).toContain('rien trouvé à ajouter');
        mocks.emotions.mockRejectedValue(new ApiError(503, 'Claude ne répond pas.'));
        await click('Ajouter des émotions');
        expect(text()).toBe(typed);
        expect(document.body.textContent).toContain('Claude ne répond pas.');
        expect(mocks.save).not.toHaveBeenCalled();
    });

    it("shows the human's turn, the line highlighted", async () => {
        await show({sceneId: 'fete-surprise'});
        const event = {kind: 'scene', session_id: 's', scene_id: 'fete-surprise', title: 'La fête surprise'} as const;
        const current = () =>
            [...document.querySelectorAll('[data-current]')].map((el) => el.getAttribute('data-line'));
        await act(async () =>
            mocks.conversation?.({...event, state: 'waiting', line: 1, message: 'Il était une fois… <rire>'})
        );
        expect(document.body.textContent).toContain('À toi (NARRATEUR) : Il était une fois…');
        expect(current()).toEqual(['1']);
        await act(async () => mocks.conversation?.({...event, state: 'ended', line: null, message: null}));
        expect(current()).toEqual([]);
    });
});

describe('the dialog, its counts worded once', () => {
    let root: Root | null = null;
    beforeEach(() => {
        vi.useFakeTimers();
        mocks.get.mockImplementation(async () => sceneOutSchema.parse(examples.SceneOut[0]));
    });
    afterEach(() => {
        act(() => root?.unmount());
        root = null;
        document.body.innerHTML = '';
        vi.useRealTimers();
    });

    // The scene's row in the list and the status over its text, as the owner reads them side by side.
    const open = async (counts: {errors: number; segments: number; rendered: number}) => {
        const {scenesApi} = await import('../src/scenes/api');
        vi.mocked(scenesApi.list).mockResolvedValueOnce({
            scenes: [{id: 'fete-surprise', title: 'La fête surprise', builtin: true, ...counts}]
        });
        const errors = Array.from({length: counts.errors}, (_, i) => ({line: i + 1, message: 'crochet mal fermé'}));
        mocks.check.mockResolvedValue({...CHECK, errors, segments: counts.segments, rendered: counts.rendered});
        const {default: Scenes} = await import('../src/scenes/Scenes');
        const host = document.createElement('div');
        document.body.append(host);
        root = createRoot(host);
        await act(async () => root?.render(createElement(Scenes, {onClose: () => {}, sceneId: 'fete-surprise'})));
        await act(async () => vi.advanceTimersByTimeAsync(500));
        const row = [...document.querySelectorAll('[role="button"]')].find((el) =>
            el.textContent?.startsWith('La fête surprise')
        );
        const status = [...document.querySelectorAll('p')].find((el) => /voix|erreur/.test(el.textContent ?? ''));
        return {row: row?.textContent ?? '', status: status?.textContent ?? ''};
    };

    it.each([
        [1, '1\u00a0erreur'],
        [3, '3\u00a0erreurs']
    ])('words %i error(s) alike in the list and the status', async (errors, said) => {
        const {row, status} = await open({errors, segments: 5, rendered: 3});
        expect(row).toBe(`La fête surprise${said}`);
        expect(status).toBe(`${said}\u00a0: elle ne peut pas se jouer`);
    });

    it('words the voices ready alike in the list and the status', async () => {
        const {row, status} = await open({errors: 0, segments: 5, rendered: 3});
        expect(row).toBe('La fête surprise3/5\u00a0voix');
        expect(status).toBe('3/5\u00a0voix prêtes');
    });

    it('follows every render in the list, the open scene and the others alike', async () => {
        const {scenesApi} = await import('../src/scenes/api');
        const [fete, croissant] = [
            {id: 'fete-surprise', title: 'La fête surprise', builtin: true, errors: 0, segments: 2, rendered: 0},
            {id: 'croissant', title: 'Le croissant disparu', builtin: true, errors: 0, segments: 1, rendered: 0}
        ];
        vi.mocked(scenesApi.list).mockResolvedValueOnce({scenes: [fete, croissant]});
        mocks.check.mockResolvedValue({...CHECK, errors: [], segments: 2, rendered: 0});
        const {default: Scenes} = await import('../src/scenes/Scenes');
        root = createRoot(document.body.appendChild(document.createElement('div')));
        await act(async () => root?.render(createElement(Scenes, {onClose: () => {}, sceneId: 'fete-surprise'})));
        await act(async () => vi.advanceTimersByTimeAsync(500));
        const row = (title: string) =>
            [...document.querySelectorAll('[role="button"]')].find((el) => el.textContent?.startsWith(title))
                ?.textContent;
        expect([row('La fête'), row('Le croissant')]).toEqual([
            'La fête surprise0/2\u00a0voix',
            'Le croissant disparu0/1\u00a0voix'
        ]);
        // the Mac renders the voices of the scene just saved, then another's, one frame per voice
        vi.mocked(scenesApi.list).mockResolvedValueOnce({scenes: [{...fete, rendered: 2}, croissant]});
        mocks.check.mockResolvedValue({...CHECK, errors: [], segments: 2, rendered: 2});
        await act(async () => mocks.render?.({scene_id: 'fete-surprise', done: 2, total: 2, error: null}));
        await act(async () => vi.advanceTimersByTimeAsync(500));
        expect(document.body.textContent).toContain('2/2\u00a0voix prêtes');
        expect(row('La fête')).toBe('La fête surprise2/2\u00a0voix');
        vi.mocked(scenesApi.list).mockResolvedValueOnce({
            scenes: [
                {...fete, rendered: 2},
                {...croissant, rendered: 1}
            ]
        });
        await act(async () => mocks.render?.({scene_id: 'croissant', done: 1, total: 1, error: null}));
        await act(async () => vi.advanceTimersByTimeAsync(500));
        expect(row('Le croissant')).toBe('Le croissant disparu1/1\u00a0voix');
    });

    it('keeps the latest read of the list when an earlier answer lands after it', async () => {
        await open({errors: 0, segments: 2, rendered: 0});
        const {scenesApi} = await import('../src/scenes/api');
        const fete = {id: 'fete-surprise', title: 'La fête surprise', builtin: true, errors: 0, segments: 2};
        let late = (_: {scenes: (typeof fete & {rendered: number})[]}) => {};
        vi.mocked(scenesApi.list)
            .mockImplementationOnce(() => new Promise((resolve) => (late = resolve)))
            .mockResolvedValueOnce({scenes: [{...fete, rendered: 2}]});
        await act(async () => mocks.render?.({scene_id: 'fete-surprise', done: 1, total: 2, error: null}));
        await act(async () => mocks.render?.({scene_id: 'fete-surprise', done: 2, total: 2, error: null}));
        await act(async () => late({scenes: [{...fete, rendered: 1}]}));
        const row = [...document.querySelectorAll('[role="button"]')].find((el) =>
            el.textContent?.startsWith('La fête surprise')
        );
        expect(row?.textContent).toBe('La fête surprise2/2\u00a0voix');
    });

    it('ends the status once before what the robot says', async () => {
        await open({errors: 1, segments: 5, rendered: 3});
        const event = {kind: 'scene', session_id: 's', scene_id: 'fete-surprise', title: 'La fête surprise'} as const;
        await act(async () => mocks.conversation?.({...event, state: 'ended', line: null, message: null}));
        expect(document.body.textContent).toContain(
            '1\u00a0erreur\u00a0: elle ne peut pas se jouer. «\u00a0La fête surprise\u00a0»\u00a0: jouée jusqu'
        );
    });
});
