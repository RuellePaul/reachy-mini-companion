// The settings draft, the volume's requests, the Vite proxy's key guards and the theme (figures: format.test.ts).
import {readFileSync, readdirSync} from 'node:fs';
import {resolve} from 'node:path';
import type {ClientRequest, IncomingMessage} from 'http';
import type {ProxyOptions} from 'vite';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {settingsViewSchema} from '../src/api/schemas';
import {coalescer} from '../src/header/coalesce';
import {describe as look} from '../src/header/status';
import {changedFields, draftOf, editsSince} from '../src/settings/draft';
import {theme, v} from '../src/theme';
import viteConfig, {fromDashboard, localHost, robotOnly} from '../vite.config';

describe('settings draft', () => {
    const stored = settingsViewSchema.parse(examples.SettingsView[0]).settings;

    it('lets untouched fields follow the server and keeps the edits', () => {
        const edits = {personality: 'Curieux.'};
        const moved = {...stored, voice: 'Kore'};
        expect(draftOf(moved, edits)).toEqual({...stored, voice: 'Kore', personality: 'Curieux.'});
    });

    it('saves only the fields that differ from what the Mac holds', () => {
        expect(changedFields(stored, draftOf(stored, {name: 'Default', voice: stored.voice}))).toEqual({
            name: 'Default'
        });
        expect(changedFields(stored, stored)).toEqual({});
    });

    it('compares an object field by its values: rebuilt equal, it is no change', () => {
        const rebuilt = draftOf(stored, {tuning: structuredClone(stored.tuning)});
        expect(changedFields(stored, rebuilt)).toEqual({});
        const faster = {...stored.tuning, idle: {amplitude: 1, speed: 1.5}};
        expect(changedFields(stored, draftOf(stored, {tuning: faster}))).toEqual({tuning: faster});
        expect(editsSince({tuning: faster}, {tuning: structuredClone(faster)})).toEqual({});
    });

    it('keeps what was typed while a save was in flight, and drops what it saved', () => {
        const sent = {personality: 'Curieux.', name: stored.name};
        expect(editsSince(sent, {...sent, name: 'Robert'})).toEqual({name: 'Robert'});
        expect(editsSince(sent, {...sent, personality: 'Curieux et drôle.'})).toEqual({
            personality: 'Curieux et drôle.'
        });
        expect(editsSince(sent, sent)).toEqual({}); // an edit equal to the stored value goes too: it follows again
    });
});

describe('volume requests', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    const DELAY = 150;
    const setup = () => {
        const requests: {value: number; end: () => void; fail: () => void}[] = [];
        const send = (value: number) =>
            new Promise<void>((end, fail) => requests.push({value, end, fail: () => fail(new Error('500'))}));
        const settled = vi.fn();
        const push = coalescer<number>(DELAY);
        return {
            requests,
            sent: () => requests.map((r) => r.value),
            settled,
            push: (v: number) => push(v, send, settled)
        };
    };

    it('sends a burst of keyboard steps once, after the debounce', async () => {
        const t = setup();
        for (let value = 51; value <= 70; value++) {
            t.push(value); // a held arrow key: MUI commits each step
            await vi.advanceTimersByTimeAsync(30);
        }
        expect(t.sent()).toEqual([]);
        await vi.advanceTimersByTimeAsync(DELAY);
        expect(t.sent()).toEqual([70]);
    });

    it('keeps one request in flight and sends only the latest value next', async () => {
        const t = setup();
        for (const value of [10, 20, 30]) {
            t.push(value);
            await vi.advanceTimersByTimeAsync(DELAY);
        }
        expect(t.sent()).toEqual([10]);
        t.requests[0].end();
        await vi.advanceTimersByTimeAsync(0);
        expect(t.sent()).toEqual([10, 30]);
        expect(t.settled).not.toHaveBeenCalled();
        t.requests[1].end();
        await vi.advanceTimersByTimeAsync(0);
        expect(t.settled).toHaveBeenCalledTimes(1);
    });

    it('settles only after the last request, not while the thumb still moves', async () => {
        const t = setup();
        t.push(10);
        await vi.advanceTimersByTimeAsync(DELAY);
        t.push(20); // still moving when the first request ends
        t.requests[0].end();
        await vi.advanceTimersByTimeAsync(0);
        expect(t.settled).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(DELAY);
        expect(t.sent()).toEqual([10, 20]);
        t.requests[1].fail(); // a failed request settles too, and does not stop the next one
        await vi.advanceTimersByTimeAsync(0);
        expect(t.settled).toHaveBeenCalledTimes(1);
        t.push(25);
        await vi.advanceTimersByTimeAsync(DELAY);
        expect(t.sent()).toEqual([10, 20, 25]);
    });
});

describe('the theme', () => {
    it('is dark whatever the system prefers: one set of colours, no scheme to switch to', () => {
        expect(theme.palette.mode).toBe('dark');
        const page = readFileSync(resolve(__dirname, '../index.html'), 'utf8');
        expect(JSON.stringify(theme.components) + page).not.toContain('prefers-color-scheme');
    });

    it('paints with its tokens alone: no colour written elsewhere, the 3D model’s own and its lights aside', () => {
        const src = resolve(__dirname, '../src');
        const model = ['#E9ECF2', '#2D3748', 'rgba(0,0,0,0.55)', 'rgba(0,0,0,0)', '#ffffff', '#dfe6ff', '#1a2233'];
        const written = readdirSync(src, {recursive: true, encoding: 'utf8'})
            .filter((file) => /\.(ts|tsx|css)$/.test(file) && file !== 'theme.ts')
            .flatMap((file) =>
                [...readFileSync(resolve(src, file), 'utf8').matchAll(/#[0-9a-f]{6}\b|(?:rgb|hsl)a?\([^)]*\)/gi)]
                    .map(([found]) => `${file} ${found}`)
                    .filter((found) => !model.some((own) => found === `studio/bench.ts ${own}`))
            );
        expect(written).toEqual([]);
        const base = theme.components?.MuiCssBaseline?.styleOverrides as Record<string, {backgroundColor?: string}>;
        expect(base['::selection'].backgroundColor).toContain(v('speak'));
    });
});

describe('proxy key guards', () => {
    const request = (headers: Record<string, string>) => ({headers}) as unknown as IncomingMessage;

    it('never gives the key to a robot route, however the path is spelled', () => {
        for (const url of [
            '/api/robot/heartbeat',
            '/api//robot/commands?wait=20',
            '/api/x/../robot/events',
            '/api%2Frobot/heartbeat',
            '/API/Robot/events',
            '/api/robot'
        ]) {
            expect(robotOnly(url), url).toBe(true);
        }
        for (const url of ['/api/settings', '/api/ws', '/api/robotics', '/api/session/say']) {
            expect(robotOnly(url), url).toBe(false);
        }
    });

    it('trusts only hosts nobody else controls', () => {
        expect(localHost('localhost:5077')).toBe(true);
        expect(localHost('192.168.1.20:5077')).toBe(true);
        expect(localHost('[::1]:5077')).toBe(true);
        expect(localHost('dashboard.localhost')).toBe(true);
        expect(localHost('evil.example.com')).toBe(false);
    });

    it("adds the key to the dashboard's own requests only", () => {
        const host = '192.168.1.20:5077';
        expect(fromDashboard(request({host}), false)).toBe(true);
        expect(fromDashboard(request({host, origin: `http://${host}`}), true)).toBe(true);
        expect(fromDashboard(request({host}), true)).toBe(false);
        expect(fromDashboard(request({host, origin: 'http://evil.example.com'}), false)).toBe(false);
        expect(fromDashboard(request({host, 'sec-fetch-site': 'cross-site'}), false)).toBe(false);
        expect(fromDashboard(request({host: 'evil.example.com'}), false)).toBe(false);
        expect(fromDashboard(request({host, origin: 'null'}), false)).toBe(false);
    });

    it('are what the dev server proxies with, on HTTP and on WebSocket upgrades', () => {
        vi.stubEnv('MAC_AGENT_API_KEY', 'proxy-key');
        const api = viteConfig({mode: 'test', command: 'serve'}).server?.proxy?.['/api'] as ProxyOptions;
        vi.unstubAllEnvs();
        type Proxy = Parameters<NonNullable<ProxyOptions['configure']>>[0];
        const hooks = new Map<string, (proxyReq: ClientRequest, req: IncomingMessage) => void>();
        api.configure?.({on: (event: string, hook: never) => hooks.set(event, hook)} as unknown as Proxy, api);
        const keyFor = (event: 'proxyReq' | 'proxyReqWs', url: string, headers: Record<string, string>) => {
            const set = new Map<string, unknown>();
            const proxyReq = {setHeader: (name: string, value: unknown) => set.set(name, value)};
            hooks.get(event)?.(proxyReq as unknown as ClientRequest, {url, headers} as unknown as IncomingMessage);
            return set.get('X-API-Key');
        };
        const local = {host: 'localhost:5077', 'sec-fetch-site': 'same-origin'};
        const lan = {host: '192.168.1.20:5077', origin: 'http://192.168.1.20:5077'};

        expect(keyFor('proxyReq', '/api/settings', local)).toBe('proxy-key');
        expect(keyFor('proxyReqWs', '/api/ws', lan)).toBe('proxy-key');
        expect(keyFor('proxyReq', '/api/robot/commands', local)).toBeUndefined();
        expect(keyFor('proxyReqWs', '/api/robot/events', lan)).toBeUndefined();
        const foreign = {host: 'localhost:5077', origin: 'http://evil.example.com', 'sec-fetch-site': 'cross-site'};
        expect(keyFor('proxyReq', '/api/session/wake', foreign)).toBeUndefined();
        expect(keyFor('proxyReqWs', '/api/ws', foreign)).toBeUndefined();
        expect(keyFor('proxyReq', '/api/apps', {host: lan.host, origin: 'null'})).toBeUndefined(); // an app's frame
        expect(keyFor('proxyReqWs', '/api/ws', {host: lan.host})).toBeUndefined(); // an upgrade with no Origin
        expect(api.xfwd).toBe(true); // a phone never reaches the Mac's loopback-only routes as 127.0.0.1
    });
});

describe('the header', () => {
    const asleep = {
        awake: false,
        session_id: null,
        phase: null,
        cost_usd: 0,
        last_latency_ms: null,
        manette: null,
        scene: null,
        hushed: false
    };

    it('asks for a wiggled antenna only while one wakes the robot', () => {
        expect(look('up', 'live', {...asleep, antenna_wake: true}).detail).toBe('Bouge une antenne pour le réveiller.');
        const moving = look('up', 'live', {...asleep, antenna_wake: false}); // a try or a scene played asleep
        expect(moving.label).toBe('En veille');
        expect(moving.detail).not.toMatch(/antenne pour/);
    });

    it('says a scene plays, asleep or awake, where to follow it and what stops it', () => {
        const played = look('up', 'live', {...asleep, antenna_wake: false, scene: 'fete-surprise'});
        expect(played).toEqual({label: 'Joue une scène', detail: 'Tu la suis dans Scènes. Le réveiller l’arrête.'});
        const awake = {...asleep, awake: true, session_id: 's', phase: 'thinking' as const, antenna_wake: false};
        expect(look('up', 'live', {...awake, scene: 'fete-surprise'})).toEqual({
            label: 'Joue une scène',
            detail: 'Tu la suis dans Scènes. L’endormir l’arrête.'
        });
        expect(look('up', 'live', {...awake, scene: null}).label).toBe('Réfléchit'); // the curtain: its phase again
        expect(look('down', 'live', {...awake, scene: 'fete-surprise'}).label).toBe('Mac injoignable');
    });

    it('says the robot keeps silent during a game, and what gives its speech back', () => {
        const awake = {...asleep, awake: true, session_id: 's', phase: 'idle' as const, antenna_wake: false};
        expect(look('up', 'live', {...awake, hushed: true})).toEqual({
            label: 'Se tait',
            detail: 'Pendant la partie. Une antenne touchée lui rend la parole.'
        });
        expect(look('up', 'live', {...awake, hushed: false}).label).toBe('En attente');
    });

    it('tells a reconnect to Gemini mid-conversation from a wake', () => {
        const awake = {...asleep, awake: true, session_id: 's', antenna_wake: false};
        expect(look('up', 'live', {...awake, phase: 'connecting'}).label).toBe('Se réveille…');
        expect(look('up', 'live', {...awake, phase: 'reconnecting'})).toEqual({
            label: 'Se reconnecte…',
            detail: 'Il rétablit la liaison avec Gemini.'
        });
    });
});
