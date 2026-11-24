// The line to the Mac: what it re-reads after a (re)connect, and that a late reply never undoes a broadcast.
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {
    dashboardStateSchema,
    settingsViewSchema,
    type DashboardState,
    type SessionSnapshot,
    type SettingsView
} from '../src/api/schemas';
import {newsCounter, openLink, type Net, type Sink} from '../src/link';

class FakeSocket {
    onopen: (() => void) | null = null;
    onmessage: ((event: {data: string}) => void) | null = null;
    onclose: (() => void) | null = null;
    closed = false;
    open() {
        this.onopen?.();
    }
    receive(message: unknown) {
        this.onmessage?.({data: typeof message === 'string' ? message : JSON.stringify(message)});
    }
    close() {
        if (this.closed) return;
        this.closed = true;
        this.onclose?.();
    }
}

interface Pending<T> {
    resolve: (value: T) => void;
    reject: (reason: unknown) => void;
}

const pending = <T>(list: Pending<T>[]) =>
    new Promise<T>((resolve, reject) => {
        list.push({resolve, reject});
    });

const flush = async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
};

const state: DashboardState = dashboardStateSchema.parse(examples.DashboardState[0]);
const view: SettingsView = settingsViewSchema.parse(examples.SettingsView[0]);
const speaking: SessionSnapshot = {
    awake: true,
    session_id: 's-1',
    phase: 'speaking',
    cost_usd: 0,
    last_latency_ms: null,
    manette: null,
    antenna_wake: true,
    scene: null,
    hushed: false
};
const asleep: SessionSnapshot = {
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

const setup = () => {
    const sockets: FakeSocket[] = [];
    const stateReads: Pending<DashboardState>[] = [];
    const settingsReads: Pending<SettingsView>[] = [];
    const net: Net = {
        socket: () => {
            const socket = new FakeSocket();
            sockets.push(socket);
            return socket as unknown as WebSocket;
        },
        state: () => pending(stateReads),
        settings: () => pending(settingsReads)
    };
    const sink = {
        link: vi.fn(),
        robotStatus: vi.fn(),
        session: vi.fn(),
        volume: vi.fn(),
        settings: vi.fn(),
        conversation: vi.fn()
    } satisfies Sink;
    const close = openLink(newsCounter(), sink, net);
    return {sockets, stateReads, settingsReads, sink, close};
};

describe('line to the Mac', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    });
    afterEach(() => {
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it('re-reads the state and the settings when the socket opens', async () => {
        const {sockets, stateReads, settingsReads, sink, close} = setup();
        sockets[0].open();
        expect(sink.link).toHaveBeenLastCalledWith('up');
        stateReads[0].resolve(state);
        settingsReads[0].resolve(view);
        await flush();
        expect(sink.robotStatus).toHaveBeenCalledWith(state.robot_status);
        expect(sink.session).toHaveBeenCalledWith(state.session);
        expect(sink.volume).toHaveBeenCalledWith(state.volume);
        expect(sink.settings).toHaveBeenCalledWith(view);
        close();
    });

    it('does not let a late read undo a broadcast that came after its request', async () => {
        const {sockets, stateReads, settingsReads, sink, close} = setup();
        sockets[0].open();
        sockets[0].receive({channel: 'session', data: speaking});
        sockets[0].receive({channel: 'settings', data: {...view, system_prompt: 'plus récent'}});
        stateReads[0].resolve({robot_status: 'live', session: asleep, volume: 40});
        settingsReads[0].resolve(view);
        await flush();
        expect(sink.session.mock.calls).toEqual([[speaking]]);
        expect(sink.settings.mock.calls).toEqual([[{...view, system_prompt: 'plus récent'}]]);
        // The channels that stayed quiet take the read.
        expect(sink.robotStatus).toHaveBeenCalledWith('live');
        expect(sink.volume).toHaveBeenCalledWith(40);
        close();
    });

    it('does not let a late read undo a robot_status or volume broadcast', async () => {
        const {sockets, stateReads, settingsReads, sink, close} = setup();
        sockets[0].open();
        sockets[0].receive({channel: 'robot_status', data: {status: 'off'}});
        sockets[0].receive({channel: 'volume', data: {volume: 70}});
        stateReads[0].resolve({robot_status: 'live', session: asleep, volume: 40});
        settingsReads[0].resolve(view);
        await flush();
        expect(sink.robotStatus.mock.calls).toEqual([['off']]);
        expect(sink.volume.mock.calls).toEqual([[70]]);
        // The channels that stayed quiet take the read.
        expect(sink.session).toHaveBeenCalledWith(asleep);
        expect(sink.settings).toHaveBeenCalledWith(view);
        close();
    });

    it('ignores the read of a socket that has been replaced', async () => {
        const {sockets, stateReads, sink, close} = setup();
        sockets[0].open();
        sockets[0].close();
        expect(sink.link).toHaveBeenLastCalledWith('down');
        await vi.advanceTimersByTimeAsync(1500);
        sockets[1].open();
        stateReads[1].resolve({...state, session: speaking});
        stateReads[0].resolve({...state, session: asleep});
        await flush();
        expect(sink.session.mock.calls).toEqual([[speaking]]);
        close();
    });

    it('reconnects when the re-read fails, and reads again', async () => {
        const {sockets, stateReads, settingsReads, sink, close} = setup();
        sockets[0].open();
        stateReads[0].reject(new Error('500'));
        settingsReads[0].resolve(view);
        await flush();
        expect(sockets[0].closed).toBe(true);
        expect(sink.link).toHaveBeenLastCalledWith('down');
        await vi.advanceTimersByTimeAsync(1500);
        expect(sockets).toHaveLength(2);
        sockets[1].open();
        stateReads[1].resolve(state);
        settingsReads[1].resolve(view);
        await flush();
        expect(sink.robotStatus).toHaveBeenCalledWith(state.robot_status);
        expect(sockets[1].closed).toBe(false);
        close();
    });

    it('backs off while sockets close before their re-read lands, and starts over once one held', async () => {
        vi.spyOn(Math, 'random').mockReturnValue(0);
        const {sockets, stateReads, settingsReads, close} = setup();
        for (const [i, delay] of [1000, 2000, 4000].entries()) {
            sockets[i].open();
            sockets[i].close(); // accepted, then closed at once
            stateReads[i].resolve(state);
            settingsReads[i].resolve(view);
            await flush();
            await vi.advanceTimersByTimeAsync(delay - 1);
            expect(sockets).toHaveLength(i + 1);
            await vi.advanceTimersByTimeAsync(1);
            expect(sockets).toHaveLength(i + 2);
        }
        sockets[3].open();
        stateReads[3].resolve(state);
        settingsReads[3].resolve(view);
        await flush();
        sockets[3].close();
        await vi.advanceTimersByTimeAsync(1000);
        expect(sockets).toHaveLength(5);
        close();
    });

    it('tells the Mac the channels it wants and what it shows, at every (re)connect and on resubscribe', () => {
        const sockets: (FakeSocket & {readyState: number; send: ReturnType<typeof vi.fn>})[] = [];
        let showing: {kind: 'app' | 'scene'; id: string; since: number} | null = {kind: 'app', id: 'a1', since: 5};
        const net: Net = {
            socket: () => {
                const socket = Object.assign(new FakeSocket(), {readyState: 1, send: vi.fn()});
                sockets.push(socket);
                return socket as unknown as WebSocket;
            },
            state: () => new Promise(() => {}),
            settings: () => new Promise(() => {})
        };
        const sink = {
            link: vi.fn(),
            robotStatus: vi.fn(),
            session: vi.fn(),
            volume: vi.fn(),
            settings: vi.fn(),
            conversation: vi.fn(),
            wanted: () => ['pose' as const],
            showing: () => showing
        } satisfies Sink;
        const line = openLink(newsCounter(), sink, net);
        sockets[0].open();
        expect(JSON.parse(sockets[0].send.mock.calls[0][0])).toEqual({
            subscribe: ['pose'],
            showing: {kind: 'app', id: 'a1', since: 5}
        });
        showing = null;
        line.resubscribe();
        expect(JSON.parse(sockets[0].send.mock.calls[1][0])).toEqual({subscribe: ['pose'], showing: null});
        line();
    });

    it('ignores a message the schemas reject', () => {
        const {sockets, sink, close} = setup();
        sockets[0].open();
        sockets[0].receive({channel: 'session', data: {awake: 'oui'}});
        sockets[0].receive('pas du JSON');
        expect(sink.session).not.toHaveBeenCalled();
        expect(console.warn).toHaveBeenCalledTimes(2);
        close();
    });
});
