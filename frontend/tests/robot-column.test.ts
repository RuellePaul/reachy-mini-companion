// @vitest-environment happy-dom
// The robot's column as a browser runs it: React DOM in happy-dom, the line to the Mac replaced and its HTTP calls
// answered by a fake fetch, so a click meets the request it sends and the Mac's refusal the words it shows.
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {ApiError} from '../src/api/client';
import {settingsViewSchema, type SessionSnapshot} from '../src/api/schemas';
import {Presence} from '../src/header/Presence';
import {setLang} from '../src/i18n';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true; // act() outside a test renderer

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
const AWAKE: SessionSnapshot = {...ASLEEP, awake: true, session_id: 's-1', phase: 'listening'};
const VIEW = settingsViewSchema.parse(examples.SettingsView[0]);

const mocks = vi.hoisted(() => ({
    link: 'up' as string,
    robotStatus: 'live' as string | null,
    session: null as unknown,
    fetch: vi.fn(),
    saveVolume: vi.fn()
}));
vi.mock('../src/live', () => ({
    useLive: () => ({
        link: mocks.link,
        robotStatus: mocks.robotStatus,
        session: mocks.session,
        settings: VIEW,
        volume: 40,
        saveVolume: mocks.saveVolume
    })
}));

let root: Root | null = null;
beforeEach(() => {
    mocks.fetch.mockImplementation(async () => new Response(null, {status: 204}));
    vi.stubGlobal('fetch', mocks.fetch);
});
afterEach(async () => {
    await act(async () => root?.unmount());
    root = null;
    document.body.textContent = '';
    vi.unstubAllGlobals();
    vi.useRealTimers();
    mocks.fetch.mockReset();
    mocks.saveVolume.mockReset();
    [mocks.link, mocks.robotStatus] = ['up', 'live'];
});

const render = async (session: SessionSnapshot, compact = false) => {
    await act(async () => root?.unmount());
    mocks.session = session;
    const container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(createElement(Presence, {compact})));
};
const button = (text: string) =>
    [...document.querySelectorAll('button')].find((b) => b.textContent === text) as HTMLButtonElement | undefined;
const click = (b: HTMLButtonElement | undefined) => act(async () => b?.click());
const sent = () => mocks.fetch.mock.calls.map(([path, init]) => `${(init as RequestInit).method} ${path}`);

describe('the robot column', () => {
    for (const compact of [false, true])
        it(`puts an awake robot to sleep and wakes a sleeping one (${compact ? 'the phone strip' : 'the column'})`, async () => {
            await render(AWAKE, compact);
            expect(button('Réveiller')).toBeUndefined();
            await click(button('Endormir'));
            expect(sent()).toEqual(['POST /api/session/sleep']);
            await render(ASLEEP, compact);
            expect(button('Endormir')).toBeUndefined();
            await click(button('Réveiller'));
            expect(sent()).toEqual(['POST /api/session/sleep', 'POST /api/session/wake']);
        });

    it('offers no button while the Mac is unreachable or the agent does not run', async () => {
        const cases = [
            ['down', 'live', AWAKE],
            ['down', 'live', ASLEEP],
            ['connecting', 'live', ASLEEP],
            ['up', 'off', ASLEEP],
            ['up', 'idle', ASLEEP],
            ['up', null, ASLEEP] // the robot's health not read yet
        ] as const;
        for (const [link, status, session] of cases) {
            [mocks.link, mocks.robotStatus] = [link, status];
            await render(session);
            const shown = [button('Réveiller'), button('Endormir')].filter(Boolean);
            expect(shown, `${link} ${status} ${session.awake ? 'awake' : 'asleep'}`).toEqual([]);
        }
    });

    it("says why the Mac refused a wake, in the Mac's own words", async () => {
        const REFUSED = 'Le robot est déjà réveillé.';
        mocks.fetch.mockImplementation(async () => new Response(JSON.stringify({detail: REFUSED}), {status: 409}));
        await render(ASLEEP);
        await click(button('Réveiller'));
        expect(document.querySelector('.MuiSnackbar-root')?.textContent).toBe(REFUSED);
    });
});

describe('the volume slider', () => {
    const slider = () => document.querySelector('input[aria-label="Volume du robot"]') as HTMLInputElement;
    const step = () =>
        act(async () => void slider().dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true})));

    it('sends the volume it is left on, once, and says why the Mac refused it', async () => {
        vi.useFakeTimers();
        mocks.saveVolume.mockRejectedValue(new ApiError(503, 'Le robot ne répond pas.'));
        await render(ASLEEP);
        for (let i = 0; i < 3; i++) await step();
        expect(slider().value).toBe('43'); // the value moved to shows at once
        expect(mocks.saveVolume).not.toHaveBeenCalled(); // still moving: nothing sent
        await act(async () => void vi.advanceTimersByTime(200));
        expect(mocks.saveVolume.mock.calls).toEqual([[43]]);
        expect(document.querySelector('.MuiSnackbar-root')?.textContent).toBe('Le robot ne répond pas.');
    });

    it('follows the language the moment it is picked, words and figures alike', async () => {
        await render({...AWAKE, cost_usd: 0.47});
        expect(button('Endormir')).toBeDefined();
        expect(document.body.textContent).toContain('0,47\u00a0$');
        await act(async () => setLang('en'));
        expect(button('Endormir')).toBeUndefined();
        expect(button('Put to sleep')).toBeDefined();
        expect(document.body.textContent).toContain('$0.47');
    });

    it('is greyed while the Mac is unreachable or the robot is off', async () => {
        for (const [link, status] of [
            ['down', 'live'],
            ['connecting', 'live'],
            ['up', 'off']
        ] as const) {
            [mocks.link, mocks.robotStatus] = [link, status];
            await render(ASLEEP);
            expect(slider().disabled, `${link} ${status}`).toBe(true);
        }
        [mocks.link, mocks.robotStatus] = ['up', 'idle']; // the agent stopped: its daemon still takes the volume
        await render(ASLEEP);
        expect(slider().disabled).toBe(false);
    });
});
