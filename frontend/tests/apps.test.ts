// @vitest-environment happy-dom
// The app bridge as the dashboard runs it: what it takes from an app (Zod, the frame's first document only), how
// it paces it toward the Mac, and the sandboxed frame itself, rendered by React DOM into happy-dom.
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {
    appListSchema,
    bridgeMessageSchema,
    EMOTIONS,
    GESTURES,
    type AppBuild,
    type SessionSnapshot
} from '../src/api/schemas';
import {AppFrame} from '../src/apps/AppFrame';
import Apps, {applyEvent, when} from '../src/apps/Apps';
import {ApiError} from '../src/api/client';
import {DASHBOARD, framed, motionOf, MOVE_GAP_MS, PACE_MS, pacer, pageOf, type Motion} from '../src/apps/bridge';
import {Build, doing} from '../src/apps/Build';
import {elapsed} from '../src/elapsed';
import {keeping} from '../src/surfaces';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const mocks = vi.hoisted(() => ({
    session: {awake: true, hushed: false, phase: 'idle'} as Pick<SessionSnapshot, 'awake' | 'hushed' | 'phase'>,
    link: 'up' as 'up' | 'down',
    bridge: vi.fn(),
    list: vi.fn(),
    html: vi.fn(),
    update: vi.fn(),
    rename: vi.fn(),
    remove: vi.fn(),
    cancel: vi.fn(),
    manette: vi.fn(),
    setQuiet: vi.fn(),
    showing: null as string | null,
    conversation: new Set<(event: never) => void>(), // the listeners on the robot's events
    channels: new Map<string, Set<(payload: never) => void>>() // what the live line's `on` was handed
}));
vi.mock('../src/live', () => {
    const on = (channel: string, listener: (payload: never) => void) => {
        const listeners = mocks.channels.get(channel) ?? new Set();
        mocks.channels.set(channel, listeners.add(listener));
        return () => void listeners.delete(listener);
    };
    return {
        useLive: () => ({
            session: mocks.session,
            link: mocks.link,
            on,
            onConversation: (listener: (event: never) => void) => {
                mocks.conversation.add(listener);
                return () => void mocks.conversation.delete(listener);
            },
            want: () => () => {}
        }),
        useShowing: (kind: string, id: string | null) => void (mocks.showing = id && `${kind}:${id}`)
    };
});
vi.mock('../src/apps/api', () => ({
    appsApi: {
        bridge: mocks.bridge,
        list: mocks.list,
        html: mocks.html,
        update: mocks.update,
        rename: mocks.rename,
        remove: mocks.remove,
        cancel: mocks.cancel,
        manette: mocks.manette,
        setQuiet: mocks.setQuiet
    }
}));

/** Clicks the button whose text is `text`: an icon button has none. */
const press = (text: string, scope: ParentNode = document) =>
    act(async () =>
        [...scope.querySelectorAll<HTMLElement>('button')].find((el) => el.textContent?.trim() === text)?.click()
    );
/** What the Mac sends on a channel of the live line. */
const emit = (channel: string, payload: unknown) =>
    act(async () => mocks.channels.get(channel)?.forEach((listener) => listener(payload as never)));

describe('bridge messages', () => {
    const names = readFileSync(resolve(__dirname, '../../robot_agent/animation_names.py'), 'utf8');
    const keys = (dict: string): string[] => {
        const body = new RegExp(`^${dict}: Final\\[[^\\n]*\\] = \\{\\n([\\s\\S]*?)\\n\\}`, 'm').exec(names);
        if (!body) throw new Error(`no ${dict} in robot_agent/animation_names.py`);
        return [...body[1].matchAll(/^ {4}'([a-z_0-9]+)':/gm)].map((found) => found[1]);
    };

    it('name exactly the emotions and gestures the robot plays', () => {
        expect([...EMOTIONS]).toEqual(keys('EMOTIONS'));
        expect([...GESTURES]).toEqual(keys('GESTURES'));
    });

    it('accept version 1 within its bounds', () => {
        for (const message of [
            {reachy: 1, type: 'emotion', name: 'cheerful1'},
            {reachy: 1, type: 'gesture', name: 'nod'},
            {reachy: 1, type: 'look', yaw: -30, pitch: 20},
            {reachy: 1, type: 'antennas', left: 60, right: -60}
        ]) {
            expect(bridgeMessageSchema.safeParse(message).success, JSON.stringify(message)).toBe(true);
        }
    });

    it('refuse an unknown name, a number out of bounds, another version or type', () => {
        for (const message of [
            {reachy: 1, type: 'emotion', name: 'furious1'},
            {reachy: 1, type: 'gesture', name: 'moonwalk'},
            {reachy: 1, type: 'look', yaw: 31, pitch: 0},
            {reachy: 1, type: 'look', yaw: 0, pitch: -20.5},
            {reachy: 1, type: 'look', yaw: 0},
            {reachy: 1, type: 'antennas', left: -61, right: 0},
            {reachy: 1, type: 'look', yaw: Number.NaN, pitch: 0},
            {reachy: 2, type: 'emotion', name: 'cheerful1'},
            {type: 'emotion', name: 'cheerful1'},
            {reachy: 1, type: 'say', text: 'bonjour'},
            {reachy: 1, type: 'robot', awake: true},
            'cheerful1',
            null
        ]) {
            expect(bridgeMessageSchema.safeParse(message).success, JSON.stringify(message)).toBe(false);
        }
    });

    it('are taken from the frame only, and only while its page is open', () => {
        const frame = {} as Window;
        const event = (data: unknown, source: unknown = frame) => new MessageEvent('message', {data, source} as never);
        const look = {reachy: 1, type: 'look', yaw: -30, pitch: 20};
        expect(motionOf(event(look), frame, true)).toEqual({kind: 'look', yaw: -30, pitch: 20});
        expect(motionOf(event({reachy: 1, type: 'gesture', name: 'nod', extra: 1}), frame, true)).toEqual({
            kind: 'gesture',
            name: 'nod'
        });
        expect(motionOf(event({reachy: 1, type: 'antennas', left: 1, right: 2}), frame, true)).toEqual({
            kind: 'antennas',
            left: 1,
            right: 2
        });
        expect(motionOf(event(look, {}), frame, true)).toBeNull(); // another window
        expect(motionOf(event(look, null), frame, true)).toBeNull();
        expect(motionOf(event(look), frame, false)).toBeNull(); // its page not loaded yet, or gone
        expect(motionOf(event(look), null, true)).toBeNull();
        expect(motionOf(event({...look, yaw: 99}), frame, true)).toBeNull();
    });
});

describe('the pacer', () => {
    const setup = () => {
        let now = 0;
        const sent: Motion[] = [];
        const pace = pacer(
            (motion) => sent.push(motion),
            () => now
        );
        const tick = (ms = PACE_MS) => {
            now += ms;
            pace.tick();
        };
        return {pace, sent, tick};
    };
    const look = (yaw: number): Motion => ({kind: 'look', yaw, pitch: 0});
    const antennas: Motion = {kind: 'antennas', left: 10, right: 10};
    const emotion = (name: string): Motion => ({kind: 'emotion', name});

    it('sends the latest look of a burst, one pose per tick', () => {
        const {pace, sent, tick} = setup();
        for (let yaw = -30; yaw <= 30; yaw += 5) pace.push(look(yaw));
        pace.push(antennas);
        tick();
        tick();
        tick();
        expect(sent).toEqual([look(30), antennas]);
    });

    it('plays a move first, and drops the poses that would cut it short', () => {
        const {pace, sent, tick} = setup();
        pace.push(look(10));
        pace.push(emotion('cheerful1'));
        pace.push(antennas);
        tick();
        expect(sent).toEqual([emotion('cheerful1')]);
        pace.push(look(20));
        tick(MOVE_GAP_MS - PACE_MS);
        expect(sent).toHaveLength(1);
        pace.push(look(-20));
        tick();
        expect(sent).toEqual([emotion('cheerful1'), look(-20)]);
    });

    it('spaces moves and drops them when too many wait', () => {
        const {pace, sent, tick} = setup();
        const names = () => sent.map((m) => m.kind === 'emotion' && m.name);
        for (const name of ['cheerful1', 'sad1', 'amazed1']) pace.push(emotion(name));
        tick();
        pace.push(emotion('oops1'));
        tick(MOVE_GAP_MS - 1);
        expect(names()).toEqual(['cheerful1']);
        tick(1);
        tick(MOVE_GAP_MS);
        tick(MOVE_GAP_MS);
        expect(names()).toEqual(['cheerful1', 'sad1', 'oops1']); // amazed1 came while two were waiting
    });

    it('sends no more than the Mac takes from an app, a tick that runs late included', () => {
        const source = readFileSync(resolve(__dirname, '../../mac_server/apps.py'), 'utf8');
        const rates = /^RATES = \{([^}]*)\}/m.exec(source)?.[1] ?? '';
        const found = [...rates.matchAll(/'(\w+)': \((\d+), ([\d.]+)\)/g)];
        const rate = new Map(found.map(([, kind, count, window]) => [kind, [Number(count), Number(window) * 1000]]));
        const of = (kind: string) => {
            const [count, windowMs] = rate.get(kind) ?? [];
            if (count === undefined || windowMs === undefined)
                throw new Error(`no ${kind} in mac_server/apps.py RATES`);
            return {count, windowMs};
        };
        for (const kind of ['look', 'antennas']) {
            const {count, windowMs} = of(kind);
            // One pose of a kind per tick at most: the ticks of a window, one more if the first of them ran late.
            expect(Math.floor(windowMs / PACE_MS) + 1, kind).toBeLessThanOrEqual(count);
        }
        const {count, windowMs} = of('move'); // emotions and gestures share it
        expect(MOVE_GAP_MS).toBeGreaterThan(windowMs / count);
    });
});

describe('the list', () => {
    const [one, building] = appListSchema.parse(examples.AppList[0]).apps;

    it('follows the apps events', () => {
        const ready = {...building, status: 'ready' as const, updated_at: building.updated_at + 100};
        expect(applyEvent([one, building], {app: ready, open: false, deleted: false})).toEqual([ready, one]);
        expect(applyEvent([one, building], {app: one, open: false, deleted: true})).toEqual([building]);
        expect(applyEvent([], {app: one, open: true, deleted: false})).toEqual([one]);
    });
});

describe('the date of a change', () => {
    it('counts the days where the viewer is, not in UTC', () => {
        const zone = process.env.TZ;
        process.env.TZ = 'Europe/Paris'; // UTC+1, UTC+2 in summer: midnight here is the evening before in UTC
        try {
            const at = (iso: string) => Date.parse(iso) / 1000;
            expect(when(at('2026-11-03T00:30:00+01:00'), at('2026-11-03T09:00:00+01:00'))).toBe('aujourd’hui');
            expect(when(at('2026-11-02T23:30:00+01:00'), at('2026-11-03T00:30:00+01:00'))).toBe('hier');
            expect(when(at('2026-07-03T01:30:00+02:00'), at('2026-07-03T12:00:00+02:00'))).toBe('aujourd’hui');
            expect(when(at('2026-03-28T12:00:00+01:00'), at('2026-03-30T08:00:00+02:00'))).toBe('avant-hier');
        } finally {
            if (zone === undefined) delete process.env.TZ;
            else process.env.TZ = zone;
        }
    });

    it('gives an older change its date after « le », and its year when it is not this one', () => {
        const zone = process.env.TZ;
        process.env.TZ = 'Europe/Paris';
        try {
            const at = (iso: string) => Date.parse(iso) / 1000;
            expect(when(at('2026-10-20T12:00:00+02:00'), at('2026-11-03T09:00:00+01:00'))).toBe('le 20 oct.');
            expect(when(at('2025-11-01T12:00:00+01:00'), at('2026-11-03T09:00:00+01:00'))).toBe('le 1 nov. 2025');
            expect(when(at('2026-12-20T12:00:00+01:00'), at('2027-01-05T09:00:00+01:00'))).toBe('le 20 déc. 2026');
        } finally {
            if (zone === undefined) delete process.env.TZ;
            else process.env.TZ = zone;
        }
    });
});

describe("the dashboard's script in the page", () => {
    // happy-dom runs no script: the one `framed` writes runs here alone, in a stand-in for the frame's window.
    const KEY = 'ab'.repeat(16);
    const run = () => {
        const srcdoc = framed(
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'"><p>jeu</p>',
            KEY
        );
        const script = /<script>([\s\S]*?)<\/script>/.exec(srcdoc)?.[1] ?? '';
        const posted: [unknown, unknown][] = [];
        const heard: {type: string; listener: (event: object) => void; capture: unknown}[] = [];
        let removed = false;
        const parent = {postMessage: (message: unknown, origin: unknown) => posted.push([message, origin])};
        const addEventListener = (type: string, listener: (event: object) => void, capture?: unknown) =>
            heard.push({type, listener, capture});
        const document = {currentScript: {remove: () => void (removed = true)}};
        new Function('parent', 'addEventListener', 'document', script)(parent, addEventListener, document);
        const fire = (type: string, event: object) =>
            heard.filter((l) => l.type === type).forEach((l) => l.listener(event));
        return {posted, heard, fire, removed: () => removed};
    };
    /** What the dashboard reads in a message the script posted, as if it came from the frame (`pageOf`). */
    const read = ([message, origin]: [unknown, unknown]) => {
        const frame = {} as Window;
        return [pageOf(new MessageEvent('message', {data: message, source: frame} as never), frame, KEY), origin];
    };

    it('says its page loaded, on a trusted load only', () => {
        const script = run();
        script.fire('load', {isTrusted: false}); // the page's own dispatchEvent: it may not open the bridge
        expect(script.posted).toEqual([]);
        script.fire('load', {isTrusted: true});
        expect(script.posted.map(read)).toEqual([[true, '*']]); // an opaque origin has no name
    });

    it('says its page goes, heard before the page, unless it goes into the back-forward cache', () => {
        const script = run();
        expect(script.heard.find((l) => l.type === 'pagehide')?.capture).toBe(true);
        script.fire('pagehide', {persisted: true});
        expect(script.posted).toEqual([]);
        script.fire('pagehide', {persisted: false});
        expect(script.posted.map(read)).toEqual([[false, '*']]);
    });

    it('is gone before the page runs: the page never reads the key', () => {
        expect(run().removed()).toBe(true);
    });
});

describe('the frame', () => {
    // happy-dom loads a srcdoc on its own, a moment after the frame is mounted: that is the first `load`. It runs no
    // script: the test says what the dashboard's own script in the page would (run above, on its own), with the key
    // it reads in the srcdoc.
    const realTimeout = globalThis.setTimeout;
    const settle = () => act(async () => void (await new Promise((done) => realTimeout(done, 30))));
    let root: Root | null = null;
    beforeEach(() => {
        vi.useFakeTimers();
        mocks.session = {awake: true, hushed: false, phase: 'idle'};
        mocks.bridge.mockResolvedValue(undefined);
    });
    afterEach(async () => {
        await act(async () => root?.unmount());
        root = null;
        document.body.innerHTML = '';
        vi.useRealTimers();
        vi.restoreAllMocks();
        vi.resetAllMocks();
    });

    const CSP_META = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">';
    const frameElement = () => createElement(AppFrame, {id: 'a1', title: 'Snake', html: `${CSP_META}<p>jeu</p>`});
    const render = async () => {
        const host = document.createElement('div');
        document.body.append(host);
        root = createRoot(host);
        await act(async () => root?.render(frameElement()));
        await settle();
    };
    const iframe = () => document.querySelector('iframe');
    const post = (data: unknown, source: unknown) =>
        act(async () => {
            window.dispatchEvent(new MessageEvent('message', {data, source} as never));
            await vi.advanceTimersByTimeAsync(PACE_MS);
        });
    const NOD = {reachy: 1, type: 'gesture', name: 'nod'};
    const keyOf = (frame: HTMLIFrameElement) => /k="([0-9a-f]{32})"/.exec(frame.getAttribute('srcdoc') ?? '')?.[1];
    /** What the dashboard's script in the page posts as its document loads (true) or goes (false). */
    const page = (frame: HTMLIFrameElement, here: boolean) =>
        post({reachy: 1, type: 'page', key: keyOf(frame), here}, frame.contentWindow);
    // Each happy-dom window binds its postMessage when it is made: spy on its class before the frame exists.
    const spyOnFrames = () => {
        const probe = document.body.appendChild(Object.assign(document.createElement('iframe'), {srcdoc: '-'}));
        const told = vi.spyOn(Object.getPrototypeOf(probe.contentWindow), 'postMessage');
        probe.remove();
        return told;
    };

    it("is sandboxed with scripts and nothing else, the dashboard's script right after the CSP", async () => {
        await render();
        const frame = iframe() as HTMLIFrameElement;
        expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
        expect(frame.hasAttribute('allow')).toBe(false);
        const srcdoc = frame.getAttribute('srcdoc') ?? '';
        expect(srcdoc.startsWith(`${CSP_META}<script>`)).toBe(true);
        expect(srcdoc.endsWith('</script><p>jeu</p>')).toBe(true);
        expect(srcdoc.match(/<script>/g)).toHaveLength(1);
        expect(frame.hasAttribute('src')).toBe(false);
    });

    it('gives nothing to a document that loads in place of its page: no robot state, no move, no manette', async () => {
        // An app that navigates before its own `load` fires one load only, the next document's: the frame's loads
        // cannot tell it from the page. Only the page's own script knows the key.
        const told = spyOnFrames();
        await render();
        const frame = iframe() as HTMLIFrameElement;
        expect(told).not.toHaveBeenCalled();
        await post(NOD, frame.contentWindow);
        await post({reachy: 1, type: 'manette', profile: 'tete'}, frame.contentWindow);
        await post({reachy: 1, type: 'page', key: 'f'.repeat(32), here: true}, frame.contentWindow);
        await post(NOD, frame.contentWindow);
        expect(mocks.bridge).not.toHaveBeenCalled();
        expect(told).not.toHaveBeenCalled();
        expect(document.body.textContent).not.toContain('« Snake »');

        await page(frame, true); // the page's own document, loaded
        expect(told).toHaveBeenLastCalledWith({reachy: 1, type: 'robot', awake: true}, '*');
        await post(NOD, frame.contentWindow);
        expect(mocks.bridge).toHaveBeenCalledExactlyOnceWith('a1', {kind: 'gesture', name: 'nod'}, DASHBOARD);
    });

    it('tells the app whether the robot is awake, once its page has loaded and on every change', async () => {
        const told = spyOnFrames();
        await render();
        await page(iframe() as HTMLIFrameElement, true);
        expect(told).toHaveBeenLastCalledWith({reachy: 1, type: 'robot', awake: true}, '*');
        mocks.session = {awake: false, hushed: false, phase: null};
        await act(async () => root?.render(frameElement()));
        expect(told).toHaveBeenLastCalledWith({reachy: 1, type: 'robot', awake: false}, '*');
        expect(document.querySelector('.MuiAlert-root')).toBeNull(); // no notice above the app: « Jouer » wakes it
        await post(NOD, iframe()?.contentWindow);
        expect(mocks.bridge).not.toHaveBeenCalled(); // asleep: nothing goes to the Mac
    });

    it('forwards its own messages only, and cuts the bridge when the app leaves its page', async () => {
        await render();
        const frame = iframe() as HTMLIFrameElement;
        await page(frame, true);
        await post(NOD, window);
        expect(mocks.bridge).not.toHaveBeenCalled();
        await post(NOD, frame.contentWindow);
        expect(mocks.bridge).toHaveBeenCalledExactlyOnceWith('a1', {kind: 'gesture', name: 'nod'}, DASHBOARD);

        await act(async () => void frame.dispatchEvent(new Event('load'))); // a second document: the app left
        expect(iframe()).toBeNull();
        expect(document.body.textContent).toContain('L’app a quitté sa page');
        await act(async () => vi.advanceTimersByTimeAsync(MOVE_GAP_MS));
        await post(NOD, frame.contentWindow);
        expect(mocks.bridge).toHaveBeenCalledTimes(1);

        const reload = [...document.querySelectorAll('button')].find((b) => b.textContent === 'Recharger');
        await act(async () => reload?.click());
        await settle();
        const fresh = iframe() as HTMLIFrameElement;
        expect(fresh.getAttribute('sandbox')).toBe('allow-scripts');
        expect(keyOf(fresh)).not.toBe(keyOf(frame)); // a key per mount: the page that left had the old one
        await post({reachy: 1, type: 'page', key: keyOf(frame), here: true}, fresh.contentWindow);
        await post(NOD, fresh.contentWindow);
        expect(mocks.bridge).toHaveBeenCalledTimes(1);
        await page(fresh, true);
        await post(NOD, fresh.contentWindow);
        expect(mocks.bridge).toHaveBeenCalledTimes(2);
    });

    it('lends the robot at the human’s click in the game, and gives the keyboard back to the game after the bar', async () => {
        const focused: string[] = [];
        const probe = document.body.appendChild(Object.assign(document.createElement('iframe'), {srcdoc: '-'}));
        vi.spyOn(Object.getPrototypeOf(probe.contentWindow), 'focus').mockImplementation(() => focused.push('game'));
        probe.remove();
        const onFocus = (event: FocusEvent) => focused.push((event.target as HTMLElement).textContent ?? '');
        document.addEventListener('focusin', onFocus);
        await render();
        await page(iframe() as HTMLIFrameElement, true); // the page's own document has loaded: its bridge is open
        const press = async (label: string) => {
            const button = [...document.querySelectorAll('button')].find((b) => b.textContent === label);
            if (!button) throw new Error(`no « ${label} »`);
            focused.length = 0;
            await act(async () => {
                button.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
                button.focus(); // the mousedown's default: the pressed button takes the focus
                button.click(); // then it goes, the bar's next state drawn without it
            });
            return focused.at(-1);
        };
        const frame = iframe() as HTMLIFrameElement;
        await post({reachy: 1, type: 'manette', profile: 'gachettes'}, frame.contentWindow); // no click before it
        expect(mocks.manette).not.toHaveBeenCalled();
        expect(document.body.textContent).toContain('Clique dans l’app pour le lui prêter.');
        await post({reachy: 1, type: 'touch', key: 'not the key'}, frame.contentWindow); // the page's own word
        expect(mocks.manette).not.toHaveBeenCalled();
        await post({reachy: 1, type: 'touch', key: keyOf(frame)}, frame.contentWindow); // the human's click, told
        expect(mocks.manette).toHaveBeenCalledExactlyOnceWith('a1', 'gachettes', true); // by the dashboard's script
        expect(await press('Rendre la main')).toBe('game');
        document.removeEventListener('focusin', onFocus);
    });

    it('tells the page when the robot starts and ends the replay it asked for, and stops one still playing when left', async () => {
        const told = spyOnFrames();
        const says = (event: object) =>
            act(async () => mocks.conversation.forEach((listener) => listener({session_id: 's1', ...event} as never)));
        await render();
        const frame = iframe() as HTMLIFrameElement;
        await page(frame, true);
        const ask = {reachy: 1, type: 'replay', start: 1794000001000, end: 1794000003500, loops: 4};
        await post(ask, frame.contentWindow);
        await act(async () => vi.advanceTimersByTimeAsync(PACE_MS));
        expect(mocks.bridge).toHaveBeenCalledExactlyOnceWith(
            'a1',
            {kind: 'replay', start: ask.start, end: ask.end, loops: 4},
            DASHBOARD
        );
        await says({kind: 'replay', state: 'started', seconds: 2.5, loops: 4});
        expect(told).toHaveBeenLastCalledWith(
            {reachy: 1, type: 'replay', state: 'started', seconds: 2.5, loops: 4},
            '*'
        );
        await act(async () => root?.unmount()); // the app is left while the robot plays
        expect(mocks.bridge).toHaveBeenLastCalledWith('a1', {kind: 'replay', loops: 0}, DASHBOARD);
        root = null;
    });

    it('cuts the bridge as its page goes, before the next document can post', async () => {
        await render();
        const frame = iframe() as HTMLIFrameElement;
        await page(frame, true);
        await post({reachy: 1, type: 'page', key: 'f'.repeat(32), here: false}, null);
        expect(iframe()).not.toBeNull(); // another key: not its page
        // The page's `pagehide`, before the next document loads; Chrome hands it over with no source.
        await post({reachy: 1, type: 'page', key: keyOf(frame), here: false}, null);
        expect(iframe()).toBeNull();
        expect(document.body.textContent).toContain('L’app a quitté sa page');
        await post(NOD, frame.contentWindow);
        expect(mocks.bridge).not.toHaveBeenCalled();
    });

    it('moves the robot only while the dashboard is on screen, saying which dashboard it is', async () => {
        await render();
        const frame = iframe() as HTMLIFrameElement;
        await page(frame, true);
        const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
        await post(NOD, frame.contentWindow);
        expect(mocks.bridge).not.toHaveBeenCalled(); // a tab in the background: the human is on another screen
        visibility.mockReturnValue('visible');
        await post(NOD, frame.contentWindow);
        expect(mocks.bridge).toHaveBeenCalledExactlyOnceWith('a1', {kind: 'gesture', name: 'nod'}, DASHBOARD);
        expect(DASHBOARD).toMatch(/^[0-9a-z]{1,32}$/);
    });
});

describe('the surface', () => {
    const [one, building] = appListSchema.parse(examples.AppList[0]).apps;
    let root: Root | null = null;
    afterEach(async () => {
        await act(async () => root?.unmount());
        root = null;
        document.body.innerHTML = '';
        vi.resetAllMocks();
        mocks.link = 'up';
        mocks.session = {awake: true, hushed: false, phase: 'idle'};
    });

    const show = async (props: {appId?: string; asked?: number; onClose?: () => void}) => {
        if (!root) {
            const host = document.createElement('div');
            document.body.append(host);
            root = createRoot(host);
        }
        await act(async () => root?.render(createElement(Apps, {onClose: () => {}, ...props})));
    };
    const heading = () => document.querySelector('h3')?.textContent;
    const item = (title: string) =>
        [...document.querySelectorAll('[role="button"], .MuiListItemButton-root')].find((el) =>
            el.textContent?.startsWith(title)
        ) as HTMLElement;
    const field = (label: string) =>
        [...document.querySelectorAll('.MuiTextField-root')]
            .find((el) => el.textContent?.includes(label))
            ?.querySelector('textarea:not([aria-hidden])') as HTMLTextAreaElement | undefined;
    const type = (target: HTMLTextAreaElement, text: string) =>
        act(async () => {
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set?.call(target, text);
            target.dispatchEvent(new Event('input', {bubbles: true}));
        });
    const enter = (target: HTMLTextAreaElement) =>
        act(async () => void target.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})));
    const helper = (label: string) =>
        [...document.querySelectorAll('.MuiTextField-root')]
            .find((el) => el.textContent?.includes(label))
            ?.querySelector('.MuiFormHelperText-root')?.textContent;
    const iconButton = (label: string) =>
        act(async () => (document.querySelector(`[aria-label="${label}"]`) as HTMLElement).click());
    const alert = () => document.querySelector('.MuiAlert-message')?.textContent;
    /** Any space as a plain one: the French spaces (no-break or not) are not what these tests are about. */
    const plain = (text: string | null | undefined) => text?.replace(/\s/g, ' ');

    it('shows the app asked for again, even after the user picked another one', async () => {
        mocks.list.mockResolvedValue([one, building]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        await show({appId: one.id, asked: 1});
        expect(heading()).toBe(one.title);
        expect(mocks.showing).toBe(`app:${one.id}`); // the Mac knows what « celle-ci » means
        await act(async () => item(building.title).click());
        expect(heading()).toBe(building.title);
        expect(mocks.showing).toBe(`app:${building.id}`);
        await show({appId: one.id, asked: 2}); // « ouvre-la » again: the same id, a new ask
        expect(heading()).toBe(one.title);
    });

    it('gives each app shown its own fields: what was typed for one is never sent to another', async () => {
        const other = {...building, status: 'ready' as const};
        mocks.list.mockResolvedValue([one, other]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        await show({appId: one.id, asked: 1});
        await type(field('Que faut-il changer') as HTMLTextAreaElement, 'ajoute des pommes dorées');
        await show({appId: other.id, asked: 2}); // the robot opens another app, and the owner looks at it
        expect(heading()).toBe(other.title);
        const retouch = field('Que faut-il changer') as HTMLTextAreaElement;
        expect(retouch.value).toBe('');
        await enter(retouch);
        expect(mocks.update).not.toHaveBeenCalled();

        await act(async () => item(one.title).click());
        await iconButton('Renommer');
        expect(field('Nouveau nom')?.value).toBe(one.title);
        await act(async () => item(other.title).click());
        expect(heading()).toBe(other.title);
        expect(field('Nouveau nom')).toBeUndefined(); // the rename was the other app's
        expect(mocks.rename).not.toHaveBeenCalled();
    });

    it('deletes the app shown once the deletion is confirmed, and says why the Mac refused', async () => {
        mocks.list.mockResolvedValue([one, building]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        mocks.remove.mockRejectedValueOnce(new ApiError(404, 'Cette app n’existe pas (ou plus).'));
        mocks.remove.mockResolvedValueOnce(undefined);
        const confirm = async () => {
            await iconButton('Supprimer');
            expect(document.querySelector('.MuiDialogTitle-root')?.textContent).toContain(one.title);
            await press('Supprimer'); // the dialog's: the icon button has no text
        };
        await show({appId: one.id, asked: 1});
        await confirm();
        expect(mocks.remove).toHaveBeenCalledExactlyOnceWith(one.id);
        expect(alert()).toBe('Cette app n’existe pas (ou plus).');
        await confirm();
        expect(mocks.remove).toHaveBeenLastCalledWith(one.id);
        await emit('apps', {app: one, open: false, deleted: true}); // the Mac says it is gone
        expect(heading()).toBeUndefined();
        expect(item(one.title)).toBeUndefined();
    });

    it('never deletes an app shipped with the robot: one changed is brought back as shipped', async () => {
        const shipped = appListSchema.parse(examples.AppList[0]).apps[2];
        const changed = {...shipped, title: 'Descente', edited: true};
        mocks.list.mockResolvedValue([one, shipped]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        mocks.remove.mockResolvedValue(undefined);
        await show({appId: shipped.id, asked: 1});
        expect(item(shipped.title).textContent).toContain('fournie');
        expect(document.querySelector('[aria-label="Supprimer"], [aria-label="Rétablir"]')).toBeNull();
        await emit('apps', {app: changed, open: false, deleted: false}); // renamed: yours now
        await iconButton('Rétablir');
        expect(plain(document.querySelector('.MuiDialogTitle-root')?.textContent)).toBe(
            'Rétablir « Descente » telle que fournie ?'
        );
        await press('Rétablir');
        expect(mocks.remove).toHaveBeenCalledExactlyOnceWith(shipped.id);
        await emit('apps', {app: shipped, open: false, deleted: false}); // the Mac says it is back
        expect(heading()).toBe(shipped.title);
        expect(document.querySelector('[aria-label="Rétablir"]')).toBeNull();
    });

    it('sends the change typed for the app shown, and says why the Mac refused it', async () => {
        mocks.list.mockResolvedValue([one]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        mocks.update.mockRejectedValueOnce(new ApiError(409, `« ${one.title} » est déjà en construction.`));
        mocks.update.mockResolvedValueOnce({...one, status: 'building', updated_at: one.updated_at + 1});
        await show({appId: one.id, asked: 1});
        const retouch = field('Que faut-il changer') as HTMLTextAreaElement;
        await type(retouch, '  plus vite ');
        await enter(retouch);
        expect(mocks.update).toHaveBeenCalledExactlyOnceWith(one.id, 'plus vite');
        expect(helper('Que faut-il changer')).toBe(`« ${one.title} » est déjà en construction.`);
        expect(retouch.value).toBe('  plus vite '); // kept, to send again
        await press('Modifier');
        expect(mocks.update).toHaveBeenLastCalledWith(one.id, 'plus vite');
        expect(field('Que faut-il changer')?.closest('[hidden]')).not.toBeNull(); // under way: its build is shown
        expect(document.body.textContent).toContain('En construction');
    });

    it('keeps the change typed for the app shown while it is retouched from elsewhere, by voice', async () => {
        mocks.list.mockResolvedValue([one]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        await show({appId: one.id, asked: 1});
        await type(field('Que faut-il changer') as HTMLTextAreaElement, 'ajoute des murs autour du terrain');
        await emit('apps', {app: {...one, status: 'building'}, open: true, deleted: false}); // « ajoute un niveau »
        expect(document.body.textContent).toContain('En construction');
        expect(field('Que faut-il changer')?.closest('[hidden]')).not.toBeNull();
        expect(keeping()).toBe(true); // unsaved still: what the robot launches meanwhile is offered beside it
        await emit('apps', {app: {...one, updated_at: one.updated_at + 1}, open: false, deleted: false});
        expect(field('Que faut-il changer')?.closest('[hidden]')).toBeNull();
        expect(field('Que faut-il changer')?.value).toBe('ajoute des murs autour du terrain');
        await iconButton('Renommer');
        await iconButton('Renommer'); // a rename opened and closed: the change typed stays too
        expect(field('Que faut-il changer')?.value).toBe('ajoute des murs autour du terrain');
    });

    it('keeps the robot silent during the app at its switch, with nothing to answer above the app', async () => {
        mocks.list.mockResolvedValue([one]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        mocks.setQuiet.mockResolvedValueOnce({...one, quiet: false, updated_at: one.updated_at + 1});
        mocks.session = {awake: true, hushed: true, phase: 'idle'};
        await show({appId: one.id, asked: 1});
        const pressed = () =>
            document.querySelector('[aria-label="Silence pendant le jeu"]')?.getAttribute('aria-pressed');
        expect(pressed()).toBe('true'); // judged by its builder: a game played in front of the robot
        await iconButton('Silence pendant le jeu');
        expect(mocks.setQuiet).toHaveBeenCalledExactlyOnceWith(one.id, false);
        expect(pressed()).toBe('false');
        expect(document.body.textContent).not.toContain('Lui rendre la parole'); // it speaks again once the app is left
    });

    it('renames the app shown, and says why the Mac refused the name', async () => {
        mocks.list.mockResolvedValue([one]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        mocks.rename.mockRejectedValueOnce(new ApiError(404, 'Cette app n’existe pas (ou plus).'));
        mocks.rename.mockResolvedValueOnce({...one, title: 'Serpent', updated_at: one.updated_at + 1});
        await show({appId: one.id, asked: 1});
        await iconButton('Renommer');
        const name = field('Nouveau nom') as HTMLTextAreaElement;
        await type(name, 'Serpent');
        await enter(name);
        expect(mocks.rename).toHaveBeenCalledExactlyOnceWith(one.id, 'Serpent');
        expect(helper('Nouveau nom')).toBe('Cette app n’existe pas (ou plus).');
        await press('Renommer');
        expect(mocks.rename).toHaveBeenLastCalledWith(one.id, 'Serpent');
        expect(heading()).toBe('Serpent');
        expect(field('Nouveau nom')).toBeUndefined(); // the field closes once renamed
    });

    it('shows a retouch above the page it changes: its edits, and why its first round was refused', async () => {
        mocks.list.mockResolvedValue([{...one, status: 'building'}]);
        mocks.html.mockResolvedValue('<p>jeu</p>'); // the page as it was: still played while it changes
        await show({appId: one.id, asked: 1});
        expect(document.querySelector('iframe[sandbox="allow-scripts"]')).not.toBeNull();
        await emit('app_build', {
            app_id: one.id,
            kind: 'update',
            phase: 'editing',
            round: 2,
            started_at: 0,
            thinking_tokens: 0,
            chars: 0,
            tail: '',
            edits: [{old: 'speed = 1', new: 'speed = 2'}],
            problems: ['La page ne se charge pas.'],
            cost_usd: null
        } satisfies AppBuild);
        expect(plain(document.body.textContent)).toContain('Réparation : Claude modifie la page');
        expect(plain(alert())).toBe('Refusée une première fois : La page ne se charge pas.');
        expect([...document.querySelectorAll('pre')].map((code) => code.textContent)).toEqual([
            'speed = 1',
            'speed = 2'
        ]);
        expect(document.querySelector('iframe[sandbox="allow-scripts"]')).not.toBeNull(); // still above the page
    });

    it('cancels a rename on Escape, and leaves the surface open', async () => {
        mocks.list.mockResolvedValue([one]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        const onClose = vi.fn();
        await show({appId: one.id, asked: 1, onClose});
        await iconButton('Renommer');
        const escape = new KeyboardEvent('keydown', {key: 'Escape', bubbles: true});
        await act(async () => void field('Nouveau nom')?.dispatchEvent(escape));
        expect(field('Nouveau nom')).toBeUndefined();
        expect(onClose).not.toHaveBeenCalled();
        expect(mocks.rename).not.toHaveBeenCalled();
    });

    it("keeps an app's date on one line beside its state: a narrow screen moves it under, whole", async () => {
        mocks.list.mockResolvedValue([building]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        await show({appId: building.id, asked: 1});
        const date = [...document.querySelectorAll<HTMLElement>('.MuiTypography-caption')].find((el) =>
            el.textContent?.startsWith('modifiée')
        ) as HTMLElement;
        expect(getComputedStyle(date.parentElement as HTMLElement).flexWrap).toBe('wrap'); // moved whole, not squeezed
    });

    it('says why the list could not be read, until a later read succeeds', async () => {
        mocks.list.mockRejectedValueOnce(new ApiError(500, 'Index des apps illisible.')).mockResolvedValue([one]);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        await show({});
        expect(alert()).toBe('Index des apps illisible.');
        mocks.link = 'down';
        await show({});
        mocks.link = 'up'; // the line is back: the list is read again
        await show({});
        expect(item(one.title)).toBeTruthy();
        expect(alert()).toBeUndefined();
    });

    it('keeps the apps events heard while the list was read: its answer may be older than they are', async () => {
        let answer = (_: (typeof one)[]) => {};
        mocks.list.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        await show({});
        const built = {...building, status: 'ready' as const, updated_at: building.updated_at + 5};
        await emit('apps', {app: built, open: false, deleted: false}); // the build ends
        await emit('apps', {app: one, open: false, deleted: true});
        await act(async () => answer([one, building])); // read before either
        expect(item(building.title).textContent).not.toContain('En construction');
        expect(item(one.title)).toBeUndefined();
    });

    it('takes the latest list read when an earlier answer lands after it', async () => {
        const answers: ((apps: (typeof one)[]) => void)[] = [];
        mocks.list.mockImplementation(() => new Promise((resolve) => answers.push(resolve)));
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        await show({});
        mocks.link = 'down';
        await show({});
        mocks.link = 'up'; // read again while the first read is on its way
        await show({});
        await act(async () => answers[1]([one, {...building, status: 'ready'}]));
        await act(async () => answers[0]([one, building]));
        expect(item(building.title).textContent).not.toContain('En construction');
    });

    it('reads the list again after a reconnect: a build that ended while the line was down shows ready', async () => {
        let now = [one, building];
        mocks.list.mockImplementation(async () => now);
        mocks.html.mockRejectedValue(new ApiError(404, 'pas de page'));
        await show({appId: building.id, asked: 1});
        expect(item(building.title).textContent).toContain('En construction');
        expect(document.querySelector('[role="dialog"]')?.textContent).toContain('Annuler');
        mocks.link = 'down';
        await show({appId: building.id, asked: 1});
        now = [one, {...building, status: 'ready', updated_at: building.updated_at + 60}]; // its event reached nobody
        mocks.link = 'up';
        await show({appId: building.id, asked: 1});
        expect(item(building.title).textContent).not.toContain('En construction');
        expect(document.querySelector('[role="dialog"]')?.textContent).not.toContain('Annuler');
    });

    it('opens on the field to describe a new app when asked without one', async () => {
        mocks.list.mockResolvedValue([one]);
        const focused = () => document.activeElement?.closest('.MuiTextField-root')?.textContent ?? '';
        await show({asked: 3});
        expect(focused()).toContain('Décris une nouvelle app');
        (document.activeElement as HTMLElement).blur();
        await show({asked: 4}); // asked again while open
        expect(focused()).toContain('Décris une nouvelle app');
    });
});

describe('a build as it goes', () => {
    const build = (over: Partial<AppBuild>): AppBuild => ({
        app_id: 'a',
        kind: 'create',
        phase: 'starting',
        round: 1,
        started_at: 0,
        thinking_tokens: 0,
        chars: 0,
        tail: '',
        edits: [],
        problems: [],
        cost_usd: null,
        ...over
    });

    it('says what Claude is doing, how much it has thought or written, and a repair', () => {
        expect(doing(build({phase: 'thinking', thinking_tokens: 10400}))).toBe('Claude réfléchit (~10,4k jetons)');
        expect(doing(build({phase: 'writing', chars: 6120}))).toBe('Claude écrit la page (6,1k caractères)');
        expect(doing(build({phase: 'editing', round: 2}))).toBe('Réparation : Claude modifie la page');
        expect(doing(build({phase: 'checking'}))).toBe('La page est lancée une fois pour la vérifier');
    });

    it('counts the time in seconds, then minutes, a number never apart from its unit', () => {
        expect([elapsed(0), elapsed(42.7), elapsed(125), elapsed(-3)]).toEqual([
            '0\u00a0s',
            '42\u00a0s',
            '2\u00a0min\u00a005\u00a0s',
            '0\u00a0s'
        ]);
    });

    it('keeps its time and cost on one line: a narrow screen moves them under what Claude does, whole', async () => {
        const host = document.body.appendChild(document.createElement('div'));
        const root = createRoot(host);
        await act(async () => root.render(createElement(Build, {id: 'a1'})));
        await emit('app_build', build({app_id: 'a1', phase: 'thinking', thinking_tokens: 10400, cost_usd: 0.12}));
        const time = [...host.querySelectorAll<HTMLElement>('.MuiTypography-caption')].find((el) =>
            el.textContent?.includes('$')
        ) as HTMLElement;
        // « 42 s », « 3 min » or « 2 min 05 s »: whichever the clock makes of it
        expect(time.textContent).toMatch(/^\d+(\u00a0s|\u00a0min(\u00a0\d+\u00a0s)?) · 0,12\u00a0\$ au tarif API$/);
        expect(getComputedStyle(time.parentElement as HTMLElement).flexWrap).toBe('wrap'); // moved whole, not squeezed
        await act(async () => root.unmount());
        host.remove();
    });

    it('says its cost is what the API would charge, which the Claude subscription covers', async () => {
        const host = document.body.appendChild(document.createElement('div'));
        const root = createRoot(host);
        await act(async () => root.render(createElement(Build, {id: 'a1'})));
        await emit('app_build', build({app_id: 'a1', phase: 'checking', round: 2, cost_usd: 0.004}));
        const cost = host.querySelector<HTMLElement>('[title]');
        expect(cost?.textContent).toBe('0,004\u00a0$ au tarif API');
        expect(cost?.title).toBe(
            'Ce que coûterait la construction au tarif de l’API\u00a0: l’abonnement Claude la couvre.'
        );
        await act(async () => root.unmount());
        host.remove();
    });

    it('is cancelled from its « Annuler », and says why the Mac refused', async () => {
        mocks.cancel.mockRejectedValueOnce(new ApiError(409, 'Cette app n’est pas en construction.'));
        const host = document.body.appendChild(document.createElement('div'));
        const root = createRoot(host);
        await act(async () => root.render(createElement(Build, {id: 'a1'})));
        await press('Annuler', host);
        expect(mocks.cancel).toHaveBeenCalledExactlyOnceWith('a1');
        expect(host.querySelector('.MuiAlert-message')?.textContent).toBe('Cette app n’est pas en construction.');
        await act(async () => root.unmount());
        host.remove();
    });
});
