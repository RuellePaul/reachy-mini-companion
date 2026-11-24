import {describe, expect, it} from 'vitest';
import examples from './contract-examples.json';
import {robotEventSchema, type RobotEvent} from '../src/api/schemas';
import {EMPTY, MAX_ROWS, reduce, type Row, type Transcript} from '../src/transcript/reducer';
import {follower, STICK_PX, type Scroller} from '../src/transcript/follow';

const S = 's-1';
const run = (events: RobotEvent[], from: Transcript = EMPTY): Transcript => events.reduce(reduce, from);
const delta = (speaker: 'user' | 'robot', turn_id: string, text: string, session_id = S): RobotEvent =>
    ({kind: `${speaker}_delta`, session_id, turn_id, text}) as RobotEvent;
const call = (call_id: string, name: string, args: Record<string, unknown> = {}): RobotEvent => ({
    kind: 'tool_call',
    session_id: S,
    call_id,
    name,
    args
});
const result = (call_id: string, name: string, fields: Partial<Extract<RobotEvent, {kind: 'tool_result'}>>) =>
    ({
        kind: 'tool_result',
        session_id: S,
        call_id,
        name,
        status: 'ok',
        message: '',
        data: {track: null, image_base64: null, volume: null},
        ...fields
    }) as RobotEvent;

describe('transcript reducer', () => {
    it('groups deltas by turn and finalises the turn with its end', () => {
        const {rows} = run([
            delta('user', 'u1', 'Bonjour '),
            delta('robot', 'r1', 'Salut'),
            delta('user', 'u1', 'robot'),
            {kind: 'user_end', session_id: S, turn_id: 'u1', text: 'Bonjour robot.', typed: false},
            delta('robot', 'r1', ' toi'),
            {kind: 'robot_end', session_id: S, turn_id: 'r1', text: 'Salut toi !'}
        ]);
        expect(rows).toEqual([
            {kind: 'user', id: 's-1/user/u1', text: 'Bonjour robot.', done: true, typed: false},
            {kind: 'robot', id: 's-1/robot/r1', text: 'Salut toi !', done: true, typed: false}
        ]);
    });

    it('shows the deltas while the turn runs, and keeps them when the end carries no text', () => {
        const running = run([delta('user', 'u1', 'Mets '), delta('user', 'u1', 'du jazz')]);
        expect(running.rows).toEqual([
            {kind: 'user', id: 's-1/user/u1', text: 'Mets du jazz', done: false, typed: false}
        ]);
        const ended = run([{kind: 'user_end', session_id: S, turn_id: 'u1', text: '', typed: false}], running);
        expect(ended.rows[0]).toMatchObject({text: 'Mets du jazz', done: true});
    });

    it('marks a typed turn and skips an empty one', () => {
        const {rows} = run([
            {kind: 'user_end', session_id: S, turn_id: 'u1', text: 'Danse !', typed: true},
            {kind: 'user_end', session_id: S, turn_id: 'u2', text: '', typed: false}
        ]);
        expect(rows).toEqual([{kind: 'user', id: 's-1/user/u1', text: 'Danse !', done: true, typed: true}]);
    });

    it('keeps the turns of two sessions apart even when their ids repeat', () => {
        const {rows} = run([delta('user', 'u1', 'Un', 's-1'), delta('user', 'u1', 'Deux', 's-2')]);
        expect(rows.map((row) => row.kind !== 'session' && row.kind !== 'tool' && row.text)).toEqual(['Un', 'Deux']);
    });

    it('completes a tool chip with its result', () => {
        const pending = run([call('c1', 'turn_body', {degrees: 30})]);
        expect(pending.rows[0]).toMatchObject({
            kind: 'tool',
            name: 'turn_body',
            status: 'pending',
            args: {degrees: 30}
        });
        const {rows} = run([result('c1', 'turn_body', {status: 'refused', message: "Personne n'a parlé."})], pending);
        expect(rows).toHaveLength(1);
        expect(rows[0]).toMatchObject({status: 'refused', message: "Personne n'a parlé.", args: {degrees: 30}});
    });

    it('reads the track and the image from the structured data, never from the message', () => {
        const track = {title: 'Around the World', artist: 'Daft Punk', uri: null};
        const {rows} = run([
            call('c1', 'play_music', {query: 'Daft Punk'}),
            result('c1', 'play_music', {
                message: 'Lecture lancée : autre chose.',
                data: {track, image_base64: null, volume: null}
            }),
            call('c2', 'look'),
            result('c2', 'look', {data: {track: null, image_base64: '/9j/AAAA', volume: null}})
        ]);
        expect(rows[0]).toMatchObject({status: 'ok', track, image: null});
        expect(rows[1]).toMatchObject({status: 'ok', track: null, image: '/9j/AAAA'});
    });

    it('never takes a track from the sentence, even when the data has none', () => {
        const {rows} = run([
            call('c1', 'play_music', {query: 'Daft Punk'}),
            result('c1', 'play_music', {message: 'Lecture lancée : One More Time, Daft Punk.'})
        ]);
        expect(rows[0]).toMatchObject({status: 'ok', track: null});
    });

    it('creates the chip from a result whose call it never saw', () => {
        const {rows} = run([result('c9', 'music', {status: 'error', message: 'Spotify ne répond pas.'})]);
        expect(rows).toEqual([
            {
                kind: 'tool',
                id: 's-1/tool/c9',
                name: 'music',
                args: {},
                status: 'error',
                message: 'Spotify ne répond pas.',
                track: null,
                image: null
            }
        ]);
    });

    it('marks the start and the end of a session, with the reason and the final cost', () => {
        const usage = (session_cost_usd: number): RobotEvent => ({
            kind: 'usage',
            session_id: S,
            prompt: {text: 0, audio: 0, image: 0},
            response: {text: 0, audio: 0, image: 0},
            total_tokens: 0,
            cost_usd: 0,
            session_cost_usd
        });
        const state = run([
            {kind: 'session_start', session_id: S},
            usage(0.01),
            usage(0.042),
            {kind: 'session_end', session_id: S, reason: 'idle'}
        ]);
        expect(state.rows).toEqual([
            {kind: 'session', id: 's-1/start', ended: false, reason: null, costUsd: null},
            {kind: 'session', id: 's-1/end', ended: true, reason: 'idle', costUsd: 0.042}
        ]);
        expect(state.costs).toEqual({});
    });

    it('closes what a lost event left open in a session when it ends, and nothing else', () => {
        const ended = run([
            {kind: 'session_start', session_id: S},
            delta('user', 'u1', 'Regarde'),
            call('c1', 'look'),
            delta('robot', 'r1', 'Je regarde'),
            delta('user', 'u1', 'Ailleurs', 's-2'),
            {kind: 'session_end', session_id: S, reason: 'goto_sleep'}
        ]);
        const states = (rows: Row[]) =>
            rows.map((row) => (row.kind === 'tool' ? row.status : row.kind === 'session' ? row.id : row.done));
        expect(states(ended.rows)).toEqual(['s-1/start', true, 'unanswered', true, false, 's-1/end']);
        const late = run([result('c1', 'look', {status: 'ok'})], ended); // a result that comes after all
        expect(states(late.rows)[2]).toBe('ok');
    });

    it('settles every session but the one the snapshot says goes on, and changes nothing when none is open', () => {
        const open = run([
            {kind: 'session_start', session_id: S},
            call('c1', 'look'),
            delta('robot', 'r1', 'Je regarde'),
            delta('user', 'u1', 'Ailleurs', 's-2')
        ]);
        const states = (rows: Row[]) =>
            rows.map((row) => (row.kind === 'tool' ? row.status : row.kind === 'session' ? row.id : row.done));
        const going = reduce(open, {kind: 'live', session_id: 's-2'});
        expect(states(going.rows)).toEqual(['s-1/start', 'unanswered', true, false]);
        expect(reduce(going, {kind: 'live', session_id: 's-2'})).toBe(going); // nothing left to settle: no render
        expect(states(reduce(going, {kind: 'live', session_id: null}).rows)).toEqual([
            's-1/start',
            'unanswered',
            true,
            true
        ]);
    });

    it('caps the rows, dropping the oldest', () => {
        const events = Array.from({length: MAX_ROWS + 20}, (_, i) => delta('user', `u${i}`, `tour ${i}`));
        const {rows} = run(events);
        expect(rows).toHaveLength(MAX_ROWS);
        expect(rows[0]).toMatchObject({text: 'tour 20'});
        expect(rows.at(-1)).toMatchObject({text: `tour ${MAX_ROWS + 19}`});
    });

    it('turns the server examples into the expected rows', () => {
        const events = (examples.RobotEvent as unknown[]).map((e) => robotEventSchema.parse(e));
        const kinds = run(events).rows.map((row: Row) =>
            row.kind === 'tool'
                ? `tool:${row.name}:${row.status}`
                : row.kind === 'session'
                  ? `session:${row.ended}`
                  : row.kind
        );
        expect(kinds).toEqual([
            'session:false',
            'user',
            'tool:play_music:ok',
            'tool:look:ok',
            'tool:music:error',
            'robot',
            'session:true'
        ]);
    });
});

// When Chrome applies scroll anchoring is not pinned here: at the layout that any read of the geometry forces
// (`forced`), or only at the frame's own layout, after the layout effect has scrolled (`frame`). The follower
// must hold under both.
describe.each(['forced', 'frame'] as const)('transcript scroll, anchoring at the %s layout', (anchoring) => {
    const ROW = 72;

    /**
     * A scroller laid out the way Chrome does it: scrollTop clamped, a scroll event pending whenever it moves,
     * and scroll anchoring (unless `overflow-anchor: none`): rows dropped above the view move scrollTop with
     * them, at the layout `anchoring` says.
     */
    class ChromeScroller implements Scroller {
        readonly clientHeight = 400;
        readonly style = {overflowAnchor: ''};
        scrolled = false; // a scroll event waits for the next frame
        resized = false; // the ResizeObserver will fire
        private height = 400;
        private top = 0;
        private dropped = 0;

        /** The frame's own layout, which applies the scroll anchoring of the rows dropped since the last one. */
        layout() {
            if (this.dropped && this.style.overflowAnchor !== 'none') this.move(this.top - this.dropped);
            this.dropped = 0;
        }
        /** A read of the geometry forces a layout. */
        private read() {
            if (anchoring === 'forced') this.layout();
        }
        private move(top: number) {
            const next = Math.max(0, Math.min(top, this.height - this.clientHeight));
            if (next === this.top) return;
            this.top = next;
            this.scrolled = true;
        }
        get scrollHeight() {
            this.read();
            return this.height;
        }
        get scrollTop() {
            this.read();
            return this.top;
        }
        set scrollTop(top: number) {
            this.read();
            this.move(top);
        }
        get gap() {
            return this.scrollHeight - this.scrollTop - this.clientHeight;
        }
        /** The DOM changes: `added` px of rows at the bottom, `dropped` px of the oldest at the top (the cap). */
        change(added: number, dropped = 0) {
            this.height += added - dropped;
            this.dropped += dropped;
            if (added !== dropped) this.resized = true;
        }
    }

    const setup = () => ({follow: follower(), el: new ChromeScroller()});
    type Setup = ReturnType<typeof setup>;

    /** One frame: the scroll event, the layout, then the ResizeObserver (whose scroll lands in the next frame). */
    const frame = ({follow, el}: Setup) => {
        for (const step of ['scroll', 'layout', 'resize', 'scroll'] as const) {
            if (step === 'scroll' && el.scrolled) {
                el.scrolled = false;
                follow.scrolled(el);
            }
            if (step === 'layout') el.layout();
            if (step === 'resize' && el.resized) {
                el.resized = false;
                follow.stick(el);
            }
        }
    };
    /** The transcript renders rows: the DOM changes, its layout effect runs, then a frame. */
    const render = (t: Setup, added: number, dropped = 0) => {
        t.el.change(added, dropped);
        t.follow.stick(t.el);
        frame(t);
    };
    const readerScrollsTo = (t: Setup, top: number) => {
        t.el.scrollTop = top;
        frame(t);
    };
    /** 500 rows, the reader at the bottom. */
    const full = () => {
        const t = setup();
        render(t, 500 * ROW);
        return t;
    };

    it('follows new rows while the reader is at the bottom', () => {
        const t = full();
        expect(t.el.gap).toBe(0);
        render(t, ROW);
        render(t, 3 * ROW);
        expect(t.el.gap).toBe(0);
    });

    it('leaves a reader who moved up, by however little, where they are', () => {
        const t = full();
        readerScrollsTo(t, t.el.scrollTop - 1);
        const top = t.el.scrollTop;
        render(t, ROW);
        render(t, ROW);
        expect(t.el.scrollTop).toBe(top);
        expect(t.el.gap).toBe(1 + 2 * ROW);
    });

    it('lets a slow drag up get away while the robot talks', () => {
        const t = full();
        const start = t.el.scrollTop;
        for (let i = 0; i < 30; i++) {
            readerScrollsTo(t, t.el.scrollTop - 10); // a slow trackpad or finger, or ArrowUp's smooth scroll
            render(t, 0); // a robot_delta: the same rows, and the layout effect sticks
        }
        expect(t.el.scrollTop).toBe(start - 300);
    });

    it(`follows again once the reader is back within ${STICK_PX} px of the bottom`, () => {
        const t = full();
        readerScrollsTo(t, t.el.scrollTop - 200);
        readerScrollsTo(t, t.el.scrollTop + 200 - (STICK_PX - 1));
        render(t, ROW);
        expect(t.el.gap).toBe(0);
    });

    it('is not unstuck by its own scroll event arriving after more content', () => {
        const t = full();
        t.el.change(ROW);
        t.follow.stick(t.el); // the layout effect scrolls; its event waits for the frame
        t.el.change(300); // an image loads before that frame
        frame(t);
        expect(t.el.gap).toBe(0);
        render(t, ROW);
        expect(t.el.gap).toBe(0);
    });

    it('switches scroll anchoring off while it follows, and back on while the reader is up', () => {
        const t = full();
        expect(t.el.style.overflowAnchor).toBe('none');
        readerScrollsTo(t, t.el.scrollTop - 200);
        expect(t.el.style.overflowAnchor).toBe('auto');
        readerScrollsTo(t, t.el.scrollHeight);
        expect(t.el.style.overflowAnchor).toBe('none');
    });

    it('keeps following at the cap, where a row comes as one of the same height goes', () => {
        const t = full();
        for (let i = 0; i < 5; i++) {
            render(t, ROW, ROW);
            expect(t.el.gap).toBe(0);
        }
        const burst = setup();
        render(burst, 500 * ROW);
        render(burst, 30 * ROW, 30 * ROW);
        expect(burst.el.gap).toBe(0);
    });

    it("keeps a reader's rows in place when the cap drops the oldest, and follows again at the bottom", () => {
        const t = full();
        readerScrollsTo(t, t.el.scrollTop - 600);
        const top = t.el.scrollTop;
        render(t, ROW, ROW);
        expect(t.el.scrollTop).toBe(top - ROW); // the same rows in view, one row further from the top
        expect(t.el.gap).toBe(600 + ROW);
        readerScrollsTo(t, t.el.scrollHeight);
        render(t, ROW, ROW);
        render(t, 2 * ROW, ROW);
        expect(t.el.gap).toBe(0);
    });
});
