// The dashboard's view of the Mac: what the line (./link) learns, and the two writes the dashboard makes.
import {createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode} from 'react';
import {api} from './api/client';
import type {RobotEvent, RobotStatus, SessionSnapshot, Settings, SettingsView} from './api/schemas';
import {newsCounter, openLink, type EventMessage, type Link, type Optional, type Showing} from './link';

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

type Listener = (event: RobotEvent) => void;
type Data<C extends EventMessage['channel']> = Extract<EventMessage, {channel: C}>['data'];

interface Live {
    link: Link;
    robotStatus: RobotStatus | null; // null until the first read
    session: SessionSnapshot;
    volume: number | null;
    settings: SettingsView | null;
    /** PATCH /api/settings. The Mac broadcasts the result too; the reply shows only if no broadcast beat it. */
    saveSettings: (changes: Partial<Settings>) => Promise<void>;
    /** PUT /api/volume, likewise. */
    saveVolume: (volume: number) => Promise<void>;
    /** Conversation events go to listeners rather than into this context: only the transcript reads them. */
    onConversation: (listener: Listener) => () => void;
    /** Likewise for the other event channels; the function returned unsubscribes. */
    on: <C extends 'pose' | 'voice' | 'scene_render' | 'scene_build' | 'apps' | 'app_build'>(
        channel: C,
        listener: (data: Data<C>) => void
    ) => () => void;
    /** Ask the Mac for an optional channel (the studio's `pose`) while the function returned is not called. */
    want: (channel: Optional) => () => void;
    /** Tell the Mac what the workspace shows (null: nothing); `shown()` reads it back. */
    show: (item: Omit<Showing, 'since'> | null) => void;
    shown: () => Showing | null;
}

const LiveContext = createContext<Live | null>(null);

export const LiveProvider = ({children}: {children: ReactNode}) => {
    const [link, setLink] = useState<Link>('connecting');
    const [robotStatus, setRobotStatus] = useState<RobotStatus | null>(null);
    const [session, setSession] = useState<SessionSnapshot>(ASLEEP);
    const [volume, setVolume] = useState<number | null>(null);
    const [settings, setSettings] = useState<SettingsView | null>(null);
    const listeners = useRef(new Set<Listener>());
    const channels = useRef(new Map<string, Set<(data: never) => void>>());
    const wanted = useRef(new Map<Optional, number>());
    const showing = useRef<Showing | null>(null);
    const line = useRef<ReturnType<typeof openLink> | null>(null);
    const [news] = useState(newsCounter);

    useEffect(() => {
        line.current = openLink(news, {
            link: setLink,
            robotStatus: setRobotStatus,
            session: setSession,
            volume: setVolume,
            settings: setSettings,
            conversation: (event) => listeners.current.forEach((listener) => listener(event)),
            event: (message) =>
                channels.current.get(message.channel)?.forEach((listener) => listener(message.data as never)),
            wanted: () => [...wanted.current].filter(([, count]) => count > 0).map(([channel]) => channel),
            showing: () => showing.current
        });
        return () => {
            line.current?.();
            line.current = null;
        };
    }, [news]);

    const saveSettings = useCallback(
        async (changes: Partial<Settings>) => {
            const quiet = news.quiet('settings');
            const view = await api.saveSettings(changes);
            if (quiet()) setSettings(view);
        },
        [news]
    );
    const saveVolume = useCallback(
        async (value: number) => {
            const quiet = news.quiet('volume');
            const saved = await api.setVolume(value);
            if (quiet()) setVolume(saved.volume);
        },
        [news]
    );

    const onConversation = useCallback((listener: Listener) => {
        listeners.current.add(listener);
        return () => {
            listeners.current.delete(listener);
        };
    }, []);

    const on = useCallback(
        <C extends 'pose' | 'voice' | 'scene_render' | 'scene_build' | 'apps' | 'app_build'>(
            channel: C,
            listener: (data: Data<C>) => void
        ) => {
            const set = channels.current.get(channel) ?? new Set();
            channels.current.set(channel, set);
            set.add(listener as (data: never) => void);
            return () => {
                set.delete(listener as (data: never) => void);
            };
        },
        []
    );

    const want = useCallback((channel: Optional) => {
        wanted.current.set(channel, (wanted.current.get(channel) ?? 0) + 1);
        line.current?.resubscribe();
        return () => {
            wanted.current.set(channel, Math.max(0, (wanted.current.get(channel) ?? 1) - 1));
            line.current?.resubscribe();
        };
    }, []);

    const show = useCallback((item: Omit<Showing, 'since'> | null) => {
        if (item?.kind === showing.current?.kind && item?.id === showing.current?.id) return;
        showing.current = item && {...item, since: Date.now() / 1000};
        line.current?.resubscribe();
    }, []);
    const shown = useCallback(() => showing.current, []);

    const value = useMemo(
        () => ({
            link,
            robotStatus,
            session,
            volume,
            settings,
            saveSettings,
            saveVolume,
            onConversation,
            on,
            want,
            show,
            shown
        }),
        [link, robotStatus, session, volume, settings, saveSettings, saveVolume, onConversation, on, want, show, shown]
    );
    return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
};

/** Tells the Mac that this app or scene is on screen while the component shows it (id null: nothing). */
export const useShowing = (kind: Showing['kind'], id: string | null) => {
    const {show} = useLive();
    useEffect(() => {
        show(id === null ? null : {kind, id});
        return () => show(null);
    }, [show, kind, id]);
};

export const useLive = (): Live => {
    const live = useContext(LiveContext);
    if (!live) throw new Error('useLive must be used inside LiveProvider');
    return live;
};
