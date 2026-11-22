// The app bridge, version 1 (docs/CONTRACTS.md § App bridge): which messages the dashboard takes from an app, and
// how it paces them toward the Mac. No React here: the tests drive it with their own frame and clock.
import {bridgeMessageSchema, type ManetteProfile} from '../api/schemas';

export type Motion =
    | {kind: 'emotion' | 'gesture'; name: string}
    | {kind: 'look'; yaw: number; pitch: number; roll?: number}
    | {kind: 'antennas'; left: number; right: number}
    | {kind: 'replay'; start?: number; end?: number; loops: number};

/**
 * The frame's srcdoc: the page, with a script of the dashboard's own right after its CSP. It says, with this mount's
 * key, that its document has loaded (a trusted `load`) or goes (a `pagehide` not into the back-forward cache, heard
 * before the page's own listeners), and each time the human clicks or presses a key in it (a trusted `pointerdown`
 * or `keydown`, heard first too: the yes to the manette the page then asks for). It is gone before the page's
 * scripts run: the page never reads the key, and a document it goes to never had it.
 */
export const framed = (html: string, key: string) =>
    html.replace(
        /^(?:<meta [^>]*>)?/,
        (csp) =>
            `${csp}<script>{const p=parent,k="${key}",say=(here)=>p.postMessage({reachy:1,type:'page',key:k,here},'*');` +
            "addEventListener('load',(e)=>e.isTrusted&&say(true));" +
            "addEventListener('pagehide',(e)=>e.persisted||say(false),true);" +
            "const t=(e)=>e.isTrusted&&!e.repeat&&p.postMessage({reachy:1,type:'touch',key:k},'*');" +
            "addEventListener('pointerdown',t,true);addEventListener('keydown',t,true);" +
            'document.currentScript.remove()}</script>'
    );

/**
 * What the dashboard's script in the frame says (`framed`): true once the page has loaded, false as it goes (posted
 * from a document going away, it comes with no source: the key alone tells it); undefined for any other message.
 */
export const pageOf = (event: MessageEvent, frame: Window | null | undefined, key: string): boolean | undefined => {
    const data: unknown = event.data;
    if (!frame || typeof data !== 'object' || data === null) return undefined;
    const said = data as {reachy?: unknown; type?: unknown; key?: unknown; here?: unknown};
    if (said.reachy !== 1 || said.type !== 'page' || said.key !== key) return undefined;
    if (said.here !== true) return false;
    return event.source === frame ? true : undefined;
};

/** Whether the message is the dashboard's script in the frame saying the human clicked or pressed a key there. */
export const touchOf = (event: MessageEvent, frame: Window | null | undefined, key: string): boolean => {
    const said = event.data as {reachy?: unknown; type?: unknown; key?: unknown} | null;
    return !!frame && event.source === frame && said?.reachy === 1 && said.type === 'touch' && said.key === key;
};

/** An app's message, parsed; null unless it comes from the frame while its page is open. */
const messageOf = (event: MessageEvent, frame: Window | null | undefined, open: boolean) => {
    if (!open || !frame || event.source !== frame) return null;
    const parsed = bridgeMessageSchema.safeParse(event.data);
    return parsed.success ? parsed.data : null;
};

/** The manette an app asks for (a profile) or lets go (null); undefined when the message is not about it. */
export const manetteOf = (
    event: MessageEvent,
    frame: Window | null | undefined,
    open: boolean
): ManetteProfile | null | undefined => {
    const message = messageOf(event, frame, open);
    return message?.type === 'manette' ? message.profile : undefined;
};

/** The motion an app's message asks for; null unless it comes from the frame while its page is open, and parses. */
export const motionOf = (event: MessageEvent, frame: Window | null | undefined, open: boolean): Motion | null => {
    const message = messageOf(event, frame, open);
    if (!message || message.type === 'manette') return null;
    if (message.type === 'look') return {kind: 'look', yaw: message.yaw, pitch: message.pitch, roll: message.roll};
    if (message.type === 'antennas') return {kind: 'antennas', left: message.left, right: message.right};
    if (message.type === 'replay')
        return {kind: 'replay', start: message.start, end: message.end, loops: message.loops};
    return {kind: message.type, name: message.name};
};

/** This dashboard, drawn as it loads: the Mac follows one dashboard's app at a time (`BridgeIn.dashboard`). */
export const DASHBOARD = crypto.getRandomValues(new Uint32Array(1))[0].toString(36);

export const PACE_MS = 200; // looks and antennas: the latest of each kind, one message per tick (the Mac takes 6/s)
export const MOVE_GAP_MS = 1600; // an emotion or a gesture at a time (the Mac takes one per 1.5 s)
const MOVES_WAITING = 2; // more are dropped: the game has moved on

/**
 * Paces an app's motions: a burst of looks becomes its latest, and an emotion is not cut short by the look that
 * came with it (looks and antennas wait while a move plays, then are dropped). Call `tick` every PACE_MS.
 */
export const pacer = (send: (motion: Motion) => void, now: () => number = Date.now) => {
    const poses = new Map<Motion['kind'], Motion>();
    const moves: Motion[] = [];
    let moved = -Infinity;
    return {
        push: (motion: Motion) => {
            if (motion.kind === 'look' || motion.kind === 'antennas') poses.set(motion.kind, motion);
            else if (moves.length < MOVES_WAITING) moves.push(motion);
        },
        tick: () => {
            const t = now();
            if (t - moved < MOVE_GAP_MS) return poses.clear();
            const move = moves.shift();
            if (move) {
                moved = t;
                poses.clear();
                return send(move);
            }
            const [kind, pose] = poses.entries().next().value ?? [];
            if (kind && pose) {
                poses.delete(kind);
                send(pose);
            }
        }
    };
};
