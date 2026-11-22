// @vitest-environment happy-dom
// The transcript as the browser shows it: Transcript.tsx rendered by React DOM into happy-dom,
// laid out by hand and fed conversation events, so its own wiring is under test — the layout effect and the
// ResizeObserver that stick to the bottom, the scroll handler that tells the reader's scroll apart, the rows.
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, describe, expect, it, vi} from 'vitest';
import examples from './contract-examples.json';
import {catalogSchema, type RobotEvent, type RobotStatus, type SessionSnapshot} from '../src/api/schemas';
import type {Link} from '../src/link';
import {toolLine} from '../src/transcript/rows';
import {Transcript} from '../src/transcript/Transcript';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true; // act() outside a test renderer

type Listener = (event: RobotEvent) => void;
interface Line {
    link: Link;
    robotStatus: RobotStatus | null;
    session: SessionSnapshot;
}
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
const live = vi.hoisted(() => ({listeners: new Set<Listener>(), line: null as Line | null}));
vi.mock('../src/live', () => {
    const onConversation = (listener: Listener) => {
        live.listeners.add(listener);
        return () => {
            live.listeners.delete(listener);
        };
    };
    return {useLive: () => ({settings: null, onConversation, ...live.line})};
});
const CATALOG = catalogSchema.parse(examples.Catalog[0]); // the catalogue the Mac builds: the palette's names
vi.mock('../src/api/client', async (importOriginal) => {
    const actual = await importOriginal<typeof import('../src/api/client')>();
    return {...actual, api: {...actual.api, catalog: async () => CATALOG}};
});

const S = 's-1';
const ROW = 72; // px, every row
const VIEW = 400; // px, the scroller's height

const turns = (count: number, from = 0): RobotEvent[] =>
    Array.from({length: count}, (_, i) => ({
        kind: 'user_delta',
        session_id: S,
        turn_id: `u${from + i}`,
        text: `tour ${from + i}`
    }));
const call = (call_id: string, query: string): RobotEvent => ({
    kind: 'tool_call',
    session_id: S,
    call_id,
    name: 'play_music',
    args: {query}
});
const played = (call_id: string, message: string, track: {title: string; artist: string} | null): RobotEvent => ({
    kind: 'tool_result',
    session_id: S,
    call_id,
    name: 'play_music',
    status: 'ok',
    message,
    data: {track: track && {...track, uri: null}, image_base64: null, volume: null}
});

/** Notified at the next frame, with the sizes the layout below gives the elements it observes. */
class FakeResizeObserver {
    static live = new Set<FakeResizeObserver>();
    readonly reported = new Map<Element, number | undefined>(); // nothing reported yet: the first frame does
    constructor(readonly callback: ResizeObserverCallback) {
        FakeResizeObserver.live.add(this);
    }
    observe(target: Element) {
        this.reported.set(target, undefined);
    }
    unobserve(target: Element) {
        this.reported.delete(target);
    }
    disconnect() {
        this.reported.clear();
        FakeResizeObserver.live.delete(this);
    }
}
vi.stubGlobal('ResizeObserver', FakeResizeObserver);

let root: Root | null = null;
afterEach(async () => {
    await act(async () => root?.unmount());
    root = null;
    document.body.textContent = '';
    vi.restoreAllMocks();
});

/**
 * Renders the transcript and lays its scroller out the way a browser does: rows of ROW px, scrollTop clamped,
 * scrollIntoView moving it, and at each frame the scroll event its last move queued, then the resize
 * observations.
 */
const mount = async (line: Line = {link: 'up', robotStatus: 'live', session: AWAKE}) => {
    live.line = line;
    const container = document.createElement('div');
    document.body.appendChild(container);
    const mounted = createRoot(container);
    root = mounted;
    await act(async () => mounted.render(createElement(Transcript)));
    const scroller = container.firstChild as HTMLElement;
    const content = scroller.firstChild as HTMLElement;

    let top = 0;
    let view = VIEW;
    let grown = 0; // px gained inside the rows (an image loading)
    let scrolled = false; // a scroll event waits for the next frame
    const contentHeight = () => content.childNodes.length * ROW + grown;
    const height = () => Math.max(view, contentHeight());
    const scrollTo = (value: number) => {
        const next = Math.max(0, Math.min(value, height() - view));
        if (next === top) return;
        top = next;
        scrolled = true;
    };
    Object.defineProperties(scroller, {
        clientHeight: {get: () => view},
        scrollHeight: {get: height},
        scrollTop: {get: () => top, set: scrollTo}
    });
    const sizeOf = (target: Element) => (target === scroller ? view : target === content ? contentHeight() : 0);
    // happy-dom's scrollIntoView does nothing; a browser's scrolls the element into view, reader or not.
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function (this: Element, arg) {
        const index = [...content.children].findIndex((row) => row.contains(this));
        if (index < 0) return;
        const top = index * ROW;
        const bottom = top + ROW + (index === content.children.length - 1 ? grown : 0);
        const block = typeof arg === 'object' ? (arg.block ?? 'start') : arg === false ? 'end' : 'start';
        const at = {
            start: top,
            end: bottom - view,
            center: (top + bottom - view) / 2,
            nearest: Math.min(Math.max(scroller.scrollTop, bottom - view), top)
        };
        scroller.scrollTop = at[block];
    });

    const frame = () =>
        act(() => {
            scrollTo(top); // a view that grew clamps scrollTop, which is a scroll too
            if (scrolled) {
                scrolled = false;
                scroller.dispatchEvent(new Event('scroll'));
            }
            for (const observer of FakeResizeObserver.live) {
                const entries = [...observer.reported]
                    .filter(([target, size]) => size !== sizeOf(target))
                    .map(([target]) => {
                        observer.reported.set(target, sizeOf(target));
                        return {target} as ResizeObserverEntry;
                    });
                if (entries.length) observer.callback(entries, observer as unknown as ResizeObserver);
            }
        });
    await frame();

    return {
        scroller,
        rows: () => [...content.childNodes] as HTMLElement[],
        /** Each event as its own WebSocket message, a frame after each. */
        push: async (...events: RobotEvent[]) => {
            for (const event of events) {
                await act(async () => live.listeners.forEach((listener) => listener(event)));
                await frame();
            }
        },
        /** The `session` snapshot as the dashboard holds it now (a reconnect re-reads it). */
        snapshot: async (session: SessionSnapshot) => {
            live.line = {...line, session};
            await act(async () => mounted.render(createElement(Transcript)));
            await frame();
        },
        readerScrollsTo: async (value: number) => {
            scroller.scrollTop = value;
            await frame();
        },
        imageLoads: async (px: number) => {
            grown += px;
            await frame();
        },
        windowResizesTo: async (px: number) => {
            view = px;
            await frame();
        },
        gap: () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight
    };
};

describe('transcript view', () => {
    it('follows new rows at the bottom, and leaves a reader who scrolled up where they are', async () => {
        const view = await mount();
        await view.push(...turns(10));
        expect(view.rows()).toHaveLength(10);
        expect(view.scroller.scrollTop).toBe(10 * ROW - VIEW);
        expect(view.gap()).toBe(0);

        await view.readerScrollsTo(view.scroller.scrollTop - 200);
        const top = view.scroller.scrollTop;
        await view.push(...turns(2, 10));
        expect(view.scroller.scrollTop).toBe(top);
        expect(view.gap()).toBe(200 + 2 * ROW);

        await view.readerScrollsTo(view.scroller.scrollHeight);
        await view.push(...turns(1, 12));
        expect(view.gap()).toBe(0);
    });

    it('lets a slow drag up get away while the robot talks', async () => {
        const view = await mount();
        await view.push(...turns(10));
        const start = view.scroller.scrollTop;
        for (let i = 0; i < 30; i++) {
            await view.readerScrollsTo(view.scroller.scrollTop - 10); // a slow trackpad or finger, or ArrowUp
            await view.push({kind: 'robot_delta', session_id: S, turn_id: 'r1', text: ` mot${i}`});
        }
        expect(view.scroller.scrollTop).toBe(start - 300);
    });

    it('follows what grows without a new row (an image, a smaller window) only while at the bottom', async () => {
        const view = await mount();
        await view.push(...turns(10));
        await view.imageLoads(300);
        expect(view.gap()).toBe(0);
        await view.windowResizesTo(300);
        expect(view.gap()).toBe(0);
        await view.windowResizesTo(VIEW); // the bottom clamps scrollTop up: still the bottom
        await view.push(...turns(1, 10));
        expect(view.gap()).toBe(0);

        await view.readerScrollsTo(view.scroller.scrollTop - 200);
        const top = view.scroller.scrollTop;
        await view.imageLoads(300);
        await view.windowResizesTo(250);
        expect(view.scroller.scrollTop).toBe(top);
    });

    it('shows the Spotify track from its structured fields, and nothing read out of the sentence', async () => {
        const view = await mount();
        await view.push(
            call('c1', 'Daft Punk'),
            played('c1', 'Lecture lancée : autre chose.', {title: 'One More Time', artist: 'Daft Punk'}),
            call('c2', 'Justice'),
            played('c2', 'Lecture lancée : Genesis, Justice.', null),
            call('c3', 'Air'),
            played('c3', 'Lecture lancée : Air.', {title: 'Air', artist: ''}) // an artist: its name alone
        );
        const [withTrack, withoutTrack, anArtist] = view.rows().map((row) => row.textContent);
        expect(withTrack).toContain('One More Time — Daft Punk');
        expect(withTrack).not.toContain('autre chose');
        expect(withoutTrack).toContain('Lancer un titre Justice');
        expect(withoutTrack).not.toContain('Genesis');
        expect(anArtist).toContain('Air');
        expect(anArtist).not.toContain('—');
    });

    it('says each call in the palette’s words, each figure with its unit, and no name the code uses', async () => {
        const view = await mount();
        const calls: [string, Record<string, unknown>, string][] = [
            ['do_gesture', {name: 'tilt_left'}, 'Geste Tête penchée à gauche'],
            ['play_emotion', {name: 'enthusiastic1'}, 'Émotion Enthousiaste'],
            ['turn_body', {angle: 45}, 'Rotation du corps 45°'],
            ['turn_body', {angle: -33.333333}, 'Rotation du corps \u221233°'],
            ['set_volume', {level: 70}, 'Volume 70\u00a0%'],
            ['goto_sleep', {}, 'Mise en veille'],
            ['look', {}, 'Regard'],
            ['play_music', {query: 'Daft Punk', kind: 'artist'}, 'Lancer un artiste Daft Punk'],
            ['play_music', {}, 'Mes titres likés'],
            ['music', {action: 'seek', amount: 30}, 'Avancer ou reculer 30\u00a0s'],
            ['music', {action: 'seek', amount: -15}, 'Avancer ou reculer \u221215\u00a0s'],
            ['music', {action: 'volume', amount: 40}, 'Volume de Spotify 40\u00a0%'],
            ['music', {action: 'shuffle', value: 'on'}, 'Lecture aléatoire en marche'],
            ['music', {action: 'repeat', value: 'track'}, 'Répétition de ce morceau'],
            ['music', {action: 'device', value: 'le salon'}, "Changer d'appareil le salon"],
            ['music', {action: 'next'}, 'Morceau suivant'],
            ['app', {action: 'create', request: 'fais-moi un Snake'}, 'Créer une app fais-moi un Snake'],
            ['app', {action: 'update', name: 'Snake', request: 'un niveau'}, 'Modifier une app Snake\u00a0: un niveau'],
            ['app', {action: 'open', name: 'morpion'}, 'Ouvrir une app morpion'],
            ['play_scene', {title: 'la fête'}, 'Jouer une scène la fête']
        ];
        await view.push(
            ...calls.map(
                ([name, args], n): RobotEvent => ({kind: 'tool_call', session_id: S, call_id: `c${n}`, name, args})
            )
        );
        expect(view.rows().map((row) => row.querySelector('p')?.textContent)).toEqual(calls.map(([, , line]) => line));
    });

    it('names every gesture, emotion and Spotify request the robot can make, and every tool it has', () => {
        const names = new Map(CATALOG.entries.map((entry) => [entry.id, entry.label]));
        for (const {id, label, tool} of CATALOG.entries) {
            const [kind, key, more] = id.split(':');
            const said =
                kind === 'gesture'
                    ? toolLine('do_gesture', {name: key}, names).detail
                    : kind === 'emotion'
                      ? toolLine('play_emotion', {name: key}, names).detail
                      : kind === 'spotify' && key === 'play'
                        ? toolLine('play_music', {kind: more, query: 'x'}, names).title
                        : kind === 'spotify'
                          ? toolLine('music', {action: key}, names).title
                          : label;
            expect(said, id).toBe(label);
            if (tool) expect(toolLine(tool, {}, names).title, tool).not.toMatch(/^[a-z_]+$/);
        }
    });

    it('shows why a tool failed or was refused, what the robot saw, and a turn typed at the keyboard', async () => {
        const view = await mount();
        const result = (
            call_id: string,
            name: string,
            status: 'ok' | 'error' | 'refused',
            message = ''
        ): RobotEvent => ({
            kind: 'tool_result',
            session_id: S,
            call_id,
            name,
            status,
            message,
            data: {track: null, image_base64: name === 'look' ? '/9j/AAAA' : null, volume: null}
        });
        await view.push(
            {kind: 'user_end', session_id: S, turn_id: 'u1', text: 'Mets de la musique', typed: true},
            call('c1', 'Daft Punk'),
            result('c1', 'play_music', 'error', 'Spotify ne répond pas.'),
            {kind: 'tool_call', session_id: S, call_id: 'c2', name: 'do_gesture', args: {name: 'nod'}},
            result('c2', 'do_gesture', 'refused', 'Une scène se joue.'),
            {kind: 'user_end', session_id: S, turn_id: 'u2', text: 'Regarde', typed: false},
            {kind: 'tool_call', session_id: S, call_id: 'c3', name: 'look', args: {}},
            result('c3', 'look', 'ok', 'Voici ce que je vois.')
        );
        const [typed, failed, refused, spoken, seen] = view.rows();
        expect(typed.textContent).toBe('Toi au clavierMets de la musique');
        expect(failed.textContent).toContain('Spotify ne répond pas.');
        expect(refused.textContent).toContain('Une scène se joue.');
        expect(spoken.textContent).toBe('ToiRegarde');
        expect(seen.textContent).not.toContain('Voici ce que je vois.'); // a success says nothing more than « fait »
        const image = seen.querySelector('img');
        expect(image?.getAttribute('alt')).toBe('Ce que voit le robot');
        expect(image?.getAttribute('src')).toBe('data:image/jpeg;base64,/9j/AAAA');
        expect(view.rows().filter((row) => row.querySelector('img'))).toEqual([seen]);
    });

    it('closes the chip a lost result left « en cours » when the conversation ends', async () => {
        const view = await mount();
        await view.push(
            {kind: 'session_start', session_id: S},
            {kind: 'tool_call', session_id: S, call_id: 'c1', name: 'look', args: {}},
            {kind: 'session_end', session_id: S, reason: 'goto_sleep'}
        );
        expect(view.rows()[1].textContent).toContain('sans réponse');
        expect(view.rows()[1].textContent).not.toContain('en cours');
    });

    it('settles a session the snapshot says is over when its end was missed, and only that one', async () => {
        const view = await mount();
        await view.push(
            {kind: 'session_start', session_id: S},
            {kind: 'tool_call', session_id: S, call_id: 'c1', name: 'look', args: {}}
        );
        expect(view.rows()[1].textContent).toContain('en cours');
        await view.snapshot(ASLEEP); // the socket was down when it ended: the reconnect reads the robot asleep
        expect(view.rows()[1].textContent).toContain('sans réponse');
        await view.push(
            {kind: 'session_start', session_id: 's-2'},
            {kind: 'tool_call', session_id: 's-2', call_id: 'c1', name: 'look', args: {}}
        );
        await view.snapshot({...AWAKE, session_id: 's-2'});
        expect(view.rows()[3].textContent).toContain('en cours'); // the session going on is left alone
    });

    it("lets a screen reader read a conversation's start and its end, with the reason and the cost", async () => {
        const view = await mount();
        const tokens = {text: 0, audio: 0, image: 0};
        await view.push(
            {kind: 'session_start', session_id: S},
            {
                kind: 'usage',
                session_id: S,
                prompt: tokens,
                response: tokens,
                total_tokens: 0,
                cost_usd: 0.356,
                session_cost_usd: 0.356
            },
            {kind: 'session_end', session_id: S, reason: 'goto_sleep'}
        );
        const [start, end] = view.rows();
        expect(start.textContent).toBe('Début de la conversation');
        expect(end.textContent).toBe('Fin de la conversation · endormi à la demande · 0,36\u00a0$');
        // The content of a separator is presentational: assistive technologies skip it.
        for (const row of [start, end]) expect(row.closest('[role="separator"]')).toBeNull();
    });

    it("says every end reason of contracts.py in French, Gemini's refusal apart from a lost connection", async () => {
        const contracts = readFileSync(resolve(__dirname, '../../mac_server/contracts.py'), 'utf8');
        const reasons = [...(/^EndReason = Literal\[([^\]]*)\]/m.exec(contracts)?.[1] ?? '').matchAll(/'([^']*)'/g)];
        expect(reasons.length).toBeGreaterThan(1);
        const view = await mount();
        const said = new Map<string, string>();
        for (const [, reason] of reasons) {
            const sid = `s-${reason}`;
            await view.push({kind: 'session_start', session_id: sid}, {kind: 'session_end', session_id: sid, reason});
            const label = (view.rows().at(-1)?.textContent ?? '').split(' · ')[1];
            expect(label, reason).not.toContain(reason); // never the raw value
            said.set(reason, label);
        }
        expect(said.get('refused')).toBe('Gemini a refusé la connexion');
        expect(said.get('connection_lost')).toBe('connexion à Gemini perdue');
    });

    it.each([
        ['awake', {link: 'up', robotStatus: 'live', session: AWAKE}, 'Parle-lui, ou écris-lui'],
        ['falling asleep', {link: 'up', robotStatus: 'live', session: {...AWAKE, phase: 'closing'}}, 'Il s’endort'],
        ['asleep', {link: 'up', robotStatus: 'live', session: ASLEEP}, 'appuie sur Réveiller'],
        ['agent stopped', {link: 'up', robotStatus: 'idle', session: ASLEEP}, 'Lance make run'],
        ['robot off', {link: 'up', robotStatus: 'off', session: ASLEEP}, 'Éteint ou hors réseau'],
        ['Mac lost', {link: 'down', robotStatus: 'live', session: AWAKE}, 'Reconnexion en cours']
    ] satisfies [string, Line, string][])('says what can be done before the first row: %s', async (_, line, words) => {
        const view = await mount(line);
        expect(view.scroller.textContent).toContain(words);
        if (line.session !== ASLEEP) expect(view.scroller.textContent).not.toContain('Réveiller');
    });
});
