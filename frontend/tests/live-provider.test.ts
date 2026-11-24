// @vitest-environment happy-dom
// LiveProvider as the dashboard mounts it: the one WebSocket (a fake in place of the browser's) and the Mac's HTTP
// reads replaced, so what the socket delivers reaches the transcript's and the channels' listeners, and what the
// dashboard wants and shows reaches the socket.
import {act, createElement, useEffect} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {settingsViewSchema, wsMessageSchema, type RobotEvent, type WsMessage} from '../src/api/schemas';
import {LiveProvider, useLive, useShowing} from '../src/live';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true; // act() outside a test renderer

const VIEW = settingsViewSchema.parse(examples.SettingsView[0]);
const message = (channel: WsMessage['channel'], nth = 0): WsMessage =>
    wsMessageSchema.parse(examples.WsMessage.filter((m) => m.channel === channel)[nth]);

const mocks = vi.hoisted(() => ({
    sockets: [] as {send: ReturnType<typeof vi.fn>; open: () => void; receive: (m: unknown) => void}[],
    saveSettings: vi.fn(),
    setVolume: vi.fn()
}));
vi.mock('../src/api/client', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../src/api/client')>()),
    api: {
        state: () => new Promise(() => {}), // the re-read never lands: only the socket speaks
        settings: () => new Promise(() => {}),
        saveSettings: mocks.saveSettings,
        setVolume: mocks.setVolume
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
    close() {
        this.readyState = 3;
    }
}

type Live = ReturnType<typeof useLive>;
const seen: {live: Live | null} = {live: null};
const live = (): Live => {
    if (!seen.live) throw new Error('not rendered');
    return seen.live;
};
const Harness = () => {
    const current = useLive();
    useEffect(() => {
        seen.live = current;
    });
    return null;
};
const Showing = ({id}: {id: string | null}) => {
    useShowing('scene', id);
    return null;
};

const said = () => mocks.sockets[0].send.mock.calls.map(([raw]) => JSON.parse(raw as string));
const deliver = (data: unknown) => act(async () => mocks.sockets[0].receive(data));

describe('LiveProvider', () => {
    let root: Root;
    beforeEach(async () => {
        mocks.sockets.length = 0;
        mocks.saveSettings.mockReset();
        mocks.setVolume.mockReset();
        vi.stubGlobal('WebSocket', FakeSocket);
        root = createRoot(document.createElement('div'));
        await act(async () => root.render(createElement(LiveProvider, null, createElement(Harness))));
        await act(async () => mocks.sockets[0].open());
    });
    afterEach(async () => {
        await act(async () => root.unmount());
        vi.unstubAllGlobals();
    });

    it('hands every conversation event to the listeners still registered', async () => {
        const first: RobotEvent[] = [];
        const second: RobotEvent[] = [];
        live().onConversation((event) => first.push(event));
        const stop = live().onConversation((event) => second.push(event));
        const event = message('conversation');
        await deliver(event);
        stop();
        await deliver(event);
        expect(first).toEqual([event.data, event.data]);
        expect(second).toEqual([event.data]);
    });

    it('hands each event channel to its own listeners only', async () => {
        const heard: [string, unknown][] = [];
        const conversation = vi.fn();
        live().onConversation(conversation);
        for (const channel of ['apps', 'app_build', 'scene_render', 'pose'] as const)
            live().on(channel, (data) => heard.push([channel, data]));
        const sent = [message('app_build'), message('apps'), message('app_build', 1), message('scene_render')];
        for (const each of [...sent, message('pose')]) await deliver(each);
        expect(heard).toEqual([...sent, message('pose')].map((each) => [each.channel, each.data]));
        expect(conversation).not.toHaveBeenCalled();
    });

    it('keeps what the state channels say', async () => {
        for (const channel of ['session', 'robot_status', 'volume', 'settings'] as const)
            await deliver(message(channel));
        expect(live().session).toEqual(message('session').data);
        expect(live().robotStatus).toBe('live');
        expect(live().volume).toBe(70);
        expect(live().settings).toEqual(message('settings').data);
        expect(live().link).toBe('up');
    });

    it('asks for the pose while one component wants it, and stops once none does', async () => {
        const first = live().want('pose');
        const second = live().want('pose');
        second();
        first();
        expect(said().map((each) => each.subscribe)).toEqual([[], ['pose'], ['pose'], ['pose'], []]);
    });

    it('tells the Mac what is shown, once per change, until it is gone', async () => {
        const scene = (id: string | null) =>
            createElement(LiveProvider, null, createElement(Harness), createElement(Showing, {id}));
        await act(async () => root.render(scene('fete')));
        const since = live().shown()?.since;
        act(() => live().show({kind: 'scene', id: 'fete'})); // the same again: nothing to tell, `since` kept
        expect(live().shown()).toEqual({kind: 'scene', id: 'fete', since});
        await act(async () => root.render(scene(null)));
        await act(async () => root.render(createElement(LiveProvider, null, createElement(Harness))));
        expect(said().map((each) => each.showing)).toEqual([null, {kind: 'scene', id: 'fete', since}, null]);
        expect(live().shown()).toBeNull();
    });

    it('tells the Mac nothing is shown once the component showing a scene is gone', async () => {
        await act(async () =>
            root.render(createElement(LiveProvider, null, createElement(Harness), createElement(Showing, {id: 'fete'})))
        );
        expect(live().shown()).toMatchObject({kind: 'scene', id: 'fete'});
        await act(async () => root.render(createElement(LiveProvider, null, createElement(Harness)))); // closed
        expect(live().shown()).toBeNull();
        expect(said().at(-1)?.showing).toBeNull(); // « celle-ci » no longer means it
    });

    it('applies a saved setting or volume unless a broadcast came after the request', async () => {
        const later = {...VIEW, settings: {...VIEW.settings, name: 'Plus récent'}};
        mocks.saveSettings.mockImplementation(async () => {
            mocks.sockets[0].receive({channel: 'settings', data: later}); // the broadcast beats the reply
            return VIEW;
        });
        await act(async () => live().saveSettings({name: 'Reachy'}));
        expect(live().settings).toEqual(later);
        mocks.setVolume.mockImplementation(async () => {
            mocks.sockets[0].receive({channel: 'volume', data: {volume: 70}}); // likewise
            return {volume: 55};
        });
        await act(async () => live().saveVolume(55));
        expect(live().volume).toBe(70);
        mocks.setVolume.mockResolvedValue({volume: 55}); // no broadcast meanwhile: the reply is the news
        await act(async () => live().saveVolume(55));
        expect(live().volume).toBe(55);
    });
});
