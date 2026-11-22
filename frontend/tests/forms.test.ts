// @vitest-environment happy-dom
// The composer and the settings form as a browser runs them: React DOM in happy-dom, with the line to the Mac
// and its HTTP calls replaced, so what is typed and clicked meets the components' own logic.
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {act, createElement, type ReactElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {ApiError} from '../src/api/client';
import {
    PERSONALITY_MAX,
    settingsFieldSchema,
    settingsViewSchema,
    type SessionSnapshot,
    type SettingsField,
    type SettingsView
} from '../src/api/schemas';
import {lang} from '../src/i18n';
import {PERSONALITIES} from '../src/settings/personalities';
import {SettingsDrawer} from '../src/settings/SettingsDrawer';
import {Composer} from '../src/transcript/Composer';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true; // act() outside a test renderer

const AWAKE: SessionSnapshot = {
    awake: true,
    session_id: 's-1',
    phase: 'listening',
    cost_usd: 0,
    last_latency_ms: null,
    manette: null,
    antenna_wake: true,
    scene: null,
    hushed: false
};
const ASLEEP: SessionSnapshot = {...AWAKE, awake: false, session_id: null, phase: null};
const VIEW: SettingsView = settingsViewSchema.parse(examples.SettingsView[0]);

const mocks = vi.hoisted(() => ({
    link: 'up',
    session: null as SessionSnapshot | null,
    saveSettings: vi.fn(),
    say: vi.fn(),
    command: vi.fn()
}));
vi.mock('../src/live', () => ({
    useLive: () => ({
        link: mocks.link,
        session: mocks.session,
        settings: VIEW,
        saveSettings: mocks.saveSettings,
        robotStatus: 'live',
        volume: 40,
        saveVolume: vi.fn()
    })
}));
vi.mock('../src/api/client', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../src/api/client')>()),
    api: {say: mocks.say, session: mocks.command}
}));

let root: Root | null = null;
afterEach(async () => {
    await act(async () => root?.unmount());
    root = null;
    document.body.textContent = '';
    vi.resetAllMocks();
    mocks.link = 'up';
});

const render = async (element: ReactElement, session: SessionSnapshot) => {
    mocks.session = session;
    const container = document.createElement('div');
    document.body.appendChild(container);
    const mounted = createRoot(container);
    root = mounted;
    await act(async () => mounted.render(element));
};

/** What the browser does when someone types: the field's value changes, then `input` fires. */
const type = (field: HTMLInputElement | HTMLTextAreaElement, value: string) =>
    act(async () => {
        Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), 'value')?.set?.call(field, value);
        field.dispatchEvent(new Event('input', {bubbles: true}));
    });
const press = (field: Element, key: string) =>
    act(async () => {
        field.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true}));
    });
const click = (button: HTMLButtonElement | undefined) => act(async () => button?.click());

const deferred = () => {
    const settle: {resolve: () => void; reject: (reason: unknown) => void} = {resolve: () => {}, reject: () => {}};
    const promise = new Promise<void>((resolve, reject) => Object.assign(settle, {resolve, reject}));
    return {promise, ...settle};
};

describe('composer', () => {
    const composer = async (session = AWAKE) => {
        await render(createElement(Composer), session);
        return document.querySelector('textarea[aria-label="Message au robot"]') as HTMLTextAreaElement;
    };

    it('keeps what is typed while the previous message is on its way', async () => {
        const request = deferred();
        mocks.say.mockReturnValueOnce(request.promise);
        const field = await composer();
        await type(field, 'Bonjour');
        await press(field, 'Enter');
        expect(mocks.say).toHaveBeenCalledWith('Bonjour');
        await type(field, `${field.value}ça va ?`);
        await act(async () => request.resolve());
        expect(field.value).toBe('ça va ?');
    });

    it("gives a refused message back, ahead of what was typed since, with the Mac's own words", async () => {
        const request = deferred();
        mocks.say.mockReturnValueOnce(request.promise);
        const field = await composer();
        await type(field, 'Bonjour');
        await press(field, 'Enter');
        await type(field, `${field.value}Encore`);
        await act(async () => request.reject(new ApiError(409, 'Refusé par le Mac.')));
        expect(field.value).toBe('Bonjour\nEncore');
        expect(document.querySelector('[role="alert"]')?.textContent).toBe('Refusé par le Mac.');
    });

    it('takes the start of a sentence from the palette to finish, sends nothing, and takes it again', async () => {
        const draft = {text: 'Mets le titre ', asked: 1};
        await render(createElement(Composer, {draft}), AWAKE);
        const field = document.querySelector('textarea[aria-label="Message au robot"]') as HTMLTextAreaElement;
        expect(field.value).toBe('Mets le titre ');
        expect(document.activeElement).toBe(field);
        expect(field.selectionStart).toBe(field.value.length); // the caret where the user goes on
        expect(mocks.say).not.toHaveBeenCalled();
        field.blur();
        await act(async () => root?.render(createElement(Composer, {draft: {...draft, asked: 2}})));
        expect(field.value).toBe('Mets le titre '); // a start left as it was is replaced, not doubled
        expect(document.activeElement).toBe(field);
        await act(async () => root?.render(createElement(Composer, {draft: {text: 'Mets l’album ', asked: 3}})));
        expect(field.value).toBe('Mets l’album ');
    });

    it('keeps the message being typed when the palette starts a sentence: the start goes on a line after it', async () => {
        const field = await composer();
        await type(field, 'Raconte-moi la suite de l’histoire du phare');
        await act(async () => root?.render(createElement(Composer, {draft: {text: 'Mets le titre ', asked: 1}})));
        expect(field.value).toBe('Raconte-moi la suite de l’histoire du phare\nMets le titre ');
        expect(field.selectionStart).toBe(field.value.length);
        expect(mocks.say).not.toHaveBeenCalled();
    });

    it('takes no message while the robot falls asleep', async () => {
        const field = await composer({...AWAKE, phase: 'closing'});
        expect(field.disabled).toBe(true);
        expect(field.placeholder).toBe('Il s’endort…');
        await type(field, 'Attends, encore une chose');
        await press(field, 'Enter');
        expect(mocks.say).not.toHaveBeenCalled();
    });
});

describe('settings form', () => {
    const form = async (session = AWAKE) => {
        await render(createElement(SettingsDrawer, {open: true, onClose: () => undefined}), session);
        const label = [...document.querySelectorAll('label')].find((l) => l.textContent === 'Nom');
        return document.getElementById(label?.htmlFor ?? '') as HTMLInputElement;
    };
    const button = (text: string) =>
        [...document.querySelectorAll('button')].find((b) => b.textContent === text) as HTMLButtonElement | undefined;

    it('saves the unsaved edits before restarting, so the robot wakes with them', async () => {
        const name = await form();
        await type(name, 'Robert');
        expect(button('Relancer la conversation')).toBeUndefined();
        await click(button('Enregistrer et relancer'));
        expect(mocks.saveSettings).toHaveBeenCalledWith({name: 'Robert'});
        expect(mocks.command).toHaveBeenCalledWith('restart');
        expect(mocks.saveSettings.mock.invocationCallOrder[0]).toBeLessThan(mocks.command.mock.invocationCallOrder[0]);
    });

    it('saves only what changed with « Enregistrer », asleep, and says so', async () => {
        const request = deferred();
        mocks.saveSettings.mockReturnValueOnce(request.promise);
        const name = await form(ASLEEP);
        expect(button('Relancer la conversation')).toBeUndefined(); // a restart asleep would wake the robot
        expect(button('Enregistrer')?.disabled).toBe(true); // nothing to save yet
        await type(name, 'Robert');
        expect(button('Enregistrer et relancer')).toBeUndefined();
        await click(button('Enregistrer'));
        expect(mocks.saveSettings).toHaveBeenCalledWith({name: 'Robert'});
        expect(button('Enregistrer')?.disabled).toBe(true); // once at a time
        await act(async () => request.resolve());
        expect(document.querySelector('[role="status"]')?.textContent).toBe('Enregistré.');
        expect(mocks.command).not.toHaveBeenCalled();
    });

    it('says why the Mac refused a save, and keeps what was typed', async () => {
        mocks.saveSettings.mockRejectedValueOnce(new ApiError(422, 'Le nom est trop long.'));
        const name = await form(ASLEEP);
        await type(name, 'Robert');
        await click(button('Enregistrer'));
        expect(document.querySelector('[role="alert"]')?.textContent).toBe('Le nom est trop long.');
        expect(name.value).toBe('Robert');
        expect(button('Enregistrer')?.disabled).toBe(false);
    });

    it('saves nothing while the Mac is unreachable', async () => {
        mocks.link = 'down';
        const name = await form(ASLEEP);
        await type(name, 'Robert');
        expect(button('Enregistrer')?.disabled).toBe(true);
    });

    it('opens on the field the palette asks for, the robot volume included', async () => {
        await render(createElement(SettingsDrawer, {open: true, onClose: () => undefined, field: 'volume'}), AWAKE);
        const slider = document.querySelector('input[aria-label="Volume du robot"]');
        expect(slider).not.toBeNull();
        expect(document.activeElement).toBe(slider);
        await act(async () => root?.render(createElement(SettingsDrawer, {open: false, onClose: () => undefined})));
        await act(async () =>
            root?.render(createElement(SettingsDrawer, {open: true, onClose: () => undefined, field: 'personality'}))
        );
        expect(document.activeElement).toBe(document.querySelector('textarea[aria-label="Personnalité"]'));
    });

    it('writes a ready-made character in the personality, an edit to save like any other', async () => {
        await form(ASLEEP);
        const area = document.querySelector('textarea[aria-label="Personnalité"]') as HTMLTextAreaElement;
        const picker = document.getElementById('settings-character') as HTMLElement;
        const pick = async (name: string) => {
            await act(async () => {
                picker.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}));
            });
            const options = [...document.querySelectorAll<HTMLElement>('[role="option"]')];
            await act(async () => options.find((option) => option.textContent?.startsWith(name))?.click());
        };
        expect(picker.textContent).toBe('Aucun');
        const [german] = PERSONALITIES;
        await pick(german.name);
        expect(area.value).toBe(german.text);
        expect(picker.textContent).toBe(`${german.name}accent ${german.accent}`);
        expect(mocks.saveSettings).not.toHaveBeenCalled(); // nothing is sent before « Enregistrer »
        await type(area, `${german.text} Tu adores les trains.`);
        expect(picker.textContent).toBe('Sur mesure');
        await pick(PERSONALITIES[1].name); // a pick replaces the text, it never adds to it
        expect(area.value).toBe(PERSONALITIES[1].text);
        await click(button('Enregistrer'));
        expect(mocks.saveSettings).toHaveBeenCalledWith({personality: PERSONALITIES[1].text});
        await pick('Aucun');
        expect(area.value).toBe('');
    });

    it('changes the dashboard’s language at once, in this browser: nothing to save, nothing sent to the Mac', async () => {
        await form(ASLEEP);
        const picker = document.getElementById('settings-language') as HTMLElement;
        expect(picker.textContent).toBe('Français');
        await act(async () => {
            picker.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}));
        });
        const options = [...document.querySelectorAll<HTMLElement>('[role="option"]')];
        expect(options.map((option) => option.textContent)).toEqual(['Français', 'English']);
        await act(async () => options[1].click());
        expect(lang()).toBe('en');
        expect(picker.textContent).toBe('English');
        expect([...document.querySelectorAll('label')].some((label) => label.textContent === 'Name')).toBe(true);
        expect(button('Enregistrer')).toBeUndefined();
        expect(button('Save')?.disabled).toBe(true); // the choice is no edit of the robot's settings
        expect(mocks.saveSettings).not.toHaveBeenCalled();
    });

    it('offers about fifteen characters, each an accent that fits the personality field', () => {
        expect(PERSONALITIES.length).toBeGreaterThanOrEqual(15);
        expect(new Set(PERSONALITIES.map((p) => p.name)).size).toBe(PERSONALITIES.length);
        expect(new Set(PERSONALITIES.map((p) => p.accent)).size).toBe(PERSONALITIES.length);
        for (const {name, text} of PERSONALITIES) {
            expect(text.length, name).toBeLessThanOrEqual(PERSONALITY_MAX);
            expect(text, name).toBe(text.trim()); // the list knows a character by its text as the Mac keeps it
            expect(text.split('\n')[0], name).toMatch(/^Tu parles (français )?avec (un|l’)[^.]*accent/);
        }
    });

    it('has a control for every field the catalogue may open it on (contracts.py)', async () => {
        const contracts = readFileSync(resolve(__dirname, '../../mac_server/contracts.py'), 'utf8');
        const literal = /^SettingsField\s*=\s*Literal\[([^\]]*)\]/m.exec(contracts)?.[1] ?? '';
        const fields = [...literal.matchAll(/'([^']*)'/g)].map((quoted) => quoted[1]);
        expect([...settingsFieldSchema.options].sort()).toEqual([...fields].sort());
        await render(createElement(SettingsDrawer, {open: true, onClose: () => undefined}), AWAKE);
        for (const field of fields as SettingsField[]) {
            expect(document.getElementById(`settings-${field}`), field).not.toBeNull();
        }
    });

    it('restarts at once with nothing unsaved, and never with a name the Mac would refuse', async () => {
        const name = await form();
        await click(button('Relancer la conversation'));
        expect(mocks.command).toHaveBeenCalledWith('restart');
        expect(mocks.saveSettings).not.toHaveBeenCalled();
        await type(name, ' ');
        expect(button('Enregistrer et relancer')?.disabled).toBe(true);
    });
});
