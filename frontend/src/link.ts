// The dashboard's line to the Mac, outside React: one WebSocket (/api/ws) that reconnects with backoff, and
// what it re-reads after every (re)connect. A reply never undoes a broadcast that arrived after its request.
import {api} from './api/client';
import {
    wsMessageSchema,
    type DashboardState,
    type RobotEvent,
    type RobotStatus,
    type SessionSnapshot,
    type SettingsView,
    type WsMessage
} from './api/schemas';

const RETRY_MS = 1000;
const MAX_RETRY_MS = 10_000;

/** `up` while the WebSocket is open: every action on the Mac needs it. */
export type Link = 'connecting' | 'up' | 'down';
/** Channels of events (delivered to listeners) rather than state (kept in context and re-read). */
export type EventMessage = Extract<
    WsMessage,
    {channel: 'conversation' | 'pose' | 'voice' | 'scene_render' | 'scene_build' | 'apps' | 'app_build'}
>;
const EVENTS: ReadonlySet<string> = new Set(['pose', 'voice', 'scene_render', 'scene_build', 'apps', 'app_build']);
const isEvent = (message: WsMessage): message is Exclude<EventMessage, {channel: 'conversation'}> =>
    EVENTS.has(message.channel);
/** The channels that carry state. */
export type Channel = Exclude<WsMessage['channel'], EventMessage['channel']>;
/** What a socket must ask for: everything else comes unasked. */
export type Optional = 'pose' | 'voice';
/** What the dashboard shows in its workspace (contracts.Showing): what a spoken « celle-ci » means. */
export interface Showing {
    kind: 'app' | 'scene';
    id: string;
    since: number; // epoch seconds: when it began to show, kept across reconnects
}

/**
 * Broadcasts heard per channel. Take `quiet(channel)` as a request leaves: a broadcast that arrives before the
 * reply may be newer than it, so the reply is applied only if the check still answers true.
 */
export const newsCounter = () => {
    const heard: Record<Channel, number> = {session: 0, robot_status: 0, settings: 0, volume: 0};
    return {
        heard: (channel: Channel) => {
            heard[channel] += 1;
        },
        quiet: (channel: Channel) => {
            const at = heard[channel];
            return () => heard[channel] === at;
        }
    };
};
export type News = ReturnType<typeof newsCounter>;

/** Where the line delivers what it learns. */
export interface Sink {
    link: (link: Link) => void;
    robotStatus: (status: RobotStatus) => void;
    session: (session: SessionSnapshot) => void;
    volume: (volume: number) => void;
    settings: (view: SettingsView) => void;
    conversation: (event: RobotEvent) => void;
    /** The other event channels (pose, scene_render, apps). */
    event?: (message: EventMessage) => void;
    /** The optional channels wanted now, and what is shown: sent on every (re)connect and on `resubscribe`. */
    wanted?: () => Optional[];
    showing?: () => Showing | null;
}

/** What the line uses of the outside world (tests pass fakes). */
export interface Net {
    socket: () => WebSocket;
    state: () => Promise<DashboardState>;
    settings: () => Promise<SettingsView>;
}

const wsUrl = () => `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/api/ws`;
const browser: Net = {socket: () => new WebSocket(wsUrl()), state: api.state, settings: api.settings};

/** Opens the line; the function returned closes it for good, its `resubscribe` re-sends what the sink wants and shows. */
export const openLink = (news: News, sink: Sink, net: Net = browser): (() => void) & {resubscribe: () => void} => {
    let active = true;
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;

    // Whatever was broadcast while the socket was down was missed: read it back. A failed read closes the
    // socket, so the reconnect reads again; only a complete read on a socket still open resets the backoff.
    const resync = (ws: WebSocket) => {
        const quiet = {
            status: news.quiet('robot_status'),
            session: news.quiet('session'),
            volume: news.quiet('volume'),
            settings: news.quiet('settings')
        };
        const current = () => active && socket === ws;
        const state = net.state().then((read) => {
            if (!current()) return;
            if (quiet.status()) sink.robotStatus(read.robot_status);
            if (quiet.session()) sink.session(read.session);
            if (quiet.volume()) sink.volume(read.volume);
        });
        const settings = net.settings().then((view) => {
            if (current() && quiet.settings()) sink.settings(view);
        });
        Promise.all([state, settings]).then(
            () => {
                if (current()) attempt = 0;
            },
            (err) => {
                console.warn('[ws] re-reading the state failed: reconnecting', err);
                ws.close();
            }
        );
    };

    const receive = (raw: unknown) => {
        let json: unknown;
        try {
            json = JSON.parse(String(raw));
        } catch {
            json = raw;
        }
        const parsed = wsMessageSchema.safeParse(json);
        if (!parsed.success) {
            console.warn('[ws] message ignored', parsed.error.issues);
            return;
        }
        const message = parsed.data;
        if (message.channel === 'conversation') {
            sink.conversation(message.data);
            return;
        }
        if (isEvent(message)) {
            sink.event?.(message);
            return;
        }
        news.heard(message.channel);
        if (message.channel === 'session') sink.session(message.data);
        else if (message.channel === 'robot_status') sink.robotStatus(message.data.status);
        else if (message.channel === 'settings') sink.settings(message.data);
        else sink.volume(message.data.volume);
    };

    const subscribe = (ws: WebSocket) => {
        const said = {subscribe: sink.wanted?.() ?? [], showing: sink.showing?.() ?? null};
        if (ws.readyState === 1) ws.send(JSON.stringify(said));
    };

    const connect = () => {
        const ws = net.socket();
        socket = ws;
        ws.onopen = () => {
            sink.link('up');
            subscribe(ws);
            resync(ws);
        };
        ws.onmessage = (event) => {
            if (active) receive(event.data);
        };
        // Only `close`: a failed attempt fires `error` and `close`, and counting both would double the backoff.
        ws.onclose = () => {
            if (!active) return;
            socket = null; // its reads still in flight are not the line's any more
            sink.link('down');
            attempt += 1;
            const delay = Math.min(RETRY_MS * 2 ** (attempt - 1), MAX_RETRY_MS) + Math.random() * 500;
            retry = setTimeout(connect, delay);
        };
    };

    connect();
    const close = () => {
        active = false;
        clearTimeout(retry);
        socket?.close();
    };
    return Object.assign(close, {resubscribe: () => socket && subscribe(socket)});
};
