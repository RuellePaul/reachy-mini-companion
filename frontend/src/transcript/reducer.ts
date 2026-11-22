// The transcript as a pure function of the `conversation` events and of the session the `session` snapshot says goes
// on. Rows are keyed by session, so a turn or call id reused by the next conversation starts a row of its own.
import type {RobotEvent, ToolStatus, Track} from '../api/schemas';

export const MAX_ROWS = 500;

export interface TurnRow {
    kind: 'user' | 'robot';
    id: string;
    text: string;
    done: boolean; // its `*_end` arrived, or its session ended
    typed: boolean; // a dashboard `say`, not speech
}

export interface ToolRow {
    kind: 'tool';
    id: string;
    name: string;
    args: Record<string, unknown>;
    status: ToolStatus | 'pending' | 'unanswered'; // unanswered: its session ended before its result came
    message: string; // French, what happened: the transcript shows an error's or a refusal's
    track: Track | null;
    image: string | null; // base64 JPEG (look)
}

export interface SessionRow {
    kind: 'session';
    id: string;
    ended: boolean;
    reason: string | null;
    costUsd: number | null; // the session's total, from its last `usage`
}

export type Row = TurnRow | ToolRow | SessionRow;

export interface Transcript {
    rows: Row[];
    costs: Record<string, number>; // latest session_cost_usd of each running session
}

export const EMPTY: Transcript = {rows: [], costs: {}};

/** The session the robot is in, from the `session` snapshot (null: none). */
export interface Live {
    kind: 'live';
    session_id: string | null;
}

type TurnEvent = Extract<RobotEvent, {kind: 'user_delta' | 'user_end' | 'robot_delta' | 'robot_end'}>;

const find = (rows: Row[], id: string): Row | undefined => rows[rows.findLastIndex((row) => row.id === id)];

const cap = (rows: Row[]): Row[] => (rows.length > MAX_ROWS ? rows.slice(rows.length - MAX_ROWS) : rows);

/** Replaces the row with the same id, or appends it. */
const put = (rows: Row[], row: Row): Row[] => {
    const index = rows.findLastIndex((r) => r.id === row.id);
    if (index < 0) return cap([...rows, row]);
    const next = rows.slice();
    next[index] = row;
    return next;
};

// The end of a session closes what a lost event left open in it: no row of it stays in progress.
const settle = (rows: Row[], over: (id: string) => boolean): Row[] =>
    rows.map((row) => {
        if (!over(row.id) || row.kind === 'session') return row;
        if (row.kind === 'tool') return row.status === 'pending' ? {...row, status: 'unanswered'} : row;
        return row.done ? row : {...row, done: true};
    });

// Deltas append; an `*_end` carries the canonical text and finalises the row. An empty turn gets no row.
const turn = (rows: Row[], event: TurnEvent): Row[] => {
    const kind = event.kind === 'user_delta' || event.kind === 'user_end' ? 'user' : 'robot';
    const end = event.kind === 'user_end' || event.kind === 'robot_end';
    const id = `${event.session_id}/${kind}/${event.turn_id}`;
    const found = find(rows, id);
    const previous = found?.kind === kind ? found : null;
    const text = end ? event.text || previous?.text || '' : (previous?.text ?? '') + event.text;
    if (!text && !previous) return rows;
    const typed = (event.kind === 'user_end' && event.typed) || (previous?.typed ?? false);
    return put(rows, {kind, id, text, done: end || (previous?.done ?? false), typed});
};

export function reduce(state: Transcript, event: RobotEvent | Live): Transcript {
    if (event.kind === 'live') {
        // A session no longer going on settles even if its end was missed (the socket down, the robot gone).
        const live = event.session_id;
        const rows = settle(state.rows, (id) => live === null || !id.startsWith(`${live}/`));
        return rows.every((row, i) => row === state.rows[i]) ? state : {...state, rows};
    }
    const sid = event.session_id;
    switch (event.kind) {
        case 'session_start':
            return {
                ...state,
                rows: put(state.rows, {kind: 'session', id: `${sid}/start`, ended: false, reason: null, costUsd: null})
            };
        case 'session_end': {
            const {[sid]: costUsd = null, ...costs} = state.costs;
            const row: SessionRow = {kind: 'session', id: `${sid}/end`, ended: true, reason: event.reason, costUsd};
            const rows = settle(state.rows, (id) => id.startsWith(`${sid}/`));
            return {rows: put(rows, row), costs};
        }
        case 'user_delta':
        case 'user_end':
        case 'robot_delta':
        case 'robot_end':
            return {...state, rows: turn(state.rows, event)};
        case 'tool_call': {
            const id = `${sid}/tool/${event.call_id}`;
            if (find(state.rows, id)) return state;
            const row: ToolRow = {
                kind: 'tool',
                id,
                name: event.name,
                args: event.args,
                status: 'pending',
                message: '',
                track: null,
                image: null
            };
            return {...state, rows: put(state.rows, row)};
        }
        case 'tool_result': {
            const id = `${sid}/tool/${event.call_id}`;
            const found = find(state.rows, id);
            const args = found?.kind === 'tool' ? found.args : {};
            const {track, image_base64: image} = event.data;
            const row: ToolRow = {
                kind: 'tool',
                id,
                name: event.name,
                args,
                status: event.status,
                message: event.message,
                track,
                image
            };
            return {...state, rows: put(state.rows, row)};
        }
        case 'usage':
            if (event.session_cost_usd === null) return state;
            return {...state, costs: {...state.costs, [sid]: event.session_cost_usd}};
        default:
            return state; // phase and latency reach the header through the `session` snapshot
    }
}
