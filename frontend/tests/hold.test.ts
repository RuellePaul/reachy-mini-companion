// The manette's hold as a value and a transition (src/apps/hold.ts), with no React, no timer and no Mac: each rule
// it keeps, case by case, then what no sequence of events may do.
import {describe, expect, it} from 'vitest';
import type {ManetteProfile} from '../src/api/schemas';
import {rest, step, TOUCH_MS, type Effect, type Hold, type HoldEvent, type MacSaid} from '../src/apps/hold';

const AWAKE: MacSaid = {awake: true, closing: false, manette: null};
const ASLEEP: MacSaid = {awake: false, closing: false, manette: null};

/** The hold after each event in turn, and everything the steps said to do, in order. */
const run = (hold: Hold, ...events: HoldEvent[]): {hold: Hold; effects: Effect[]} =>
    events.reduce(
        (so, event) => {
            const next = step(so.hold, event);
            return {hold: next.hold, effects: [...so.effects, ...next.effects]};
        },
        {hold, effects: [] as Effect[]}
    );
const ask = (profile: ManetteProfile | null, now = 0): HoldEvent => ({type: 'ask', profile, now});
const touch = (now = 0): HoldEvent => ({type: 'touch', now});
const said = (mac: MacSaid, now = 0): HoldEvent => ({type: 'session', now, ...mac});
const robot = (
    state: 'on' | 'returning' | 'off' | 'refused',
    profile: ManetteProfile | null = null,
    why: string | null = null
): HoldEvent => ({type: 'robot', state, profile, why});
/** « Jouer »: the human's click in the app, then what the page asks at once. */
const play = (profile: ManetteProfile, now = 0): HoldEvent[] => [touch(now), ask(profile, now)];
const sent = (effects: Effect[]) =>
    effects.flatMap((effect) => (effect.type === 'send' ? [[effect.profile, effect.opened]] : []));
const told = (effects: Effect[]) =>
    effects.flatMap((effect) => (effect.type === 'tell' ? [`${effect.state} ${effect.profile ?? '-'}`] : []));
const kinds = (effects: Effect[]) => effects.map((effect) => effect.type);

describe('the human’s yes', () => {
    it('grants at once what the page asks right after their gesture in the app', () => {
        const {hold, effects} = run(rest(AWAKE), ...play('volant', 1000));
        expect([hold.ask, hold.grant, hold.held, hold.open, hold.owed]).toEqual([null, 'volant', false, 1, 1]);
        expect(sent(effects)).toEqual([['volant', 1]]);
        expect(told(effects)).toEqual(['asking volant', 'asking volant']); // asked, then getting into place
    });

    it('keeps an ask no gesture met until one comes, the latest ask being the one that waits', () => {
        const waiting = run(rest(AWAKE), ask('gachettes', 0), said(AWAKE, 60_000), ask('tete', 61_000));
        expect([waiting.hold.ask, waiting.hold.grant]).toEqual(['tete', null]);
        expect(sent(waiting.effects)).toEqual([]);
        const granted = step(waiting.hold, touch(90_000));
        expect([granted.hold.ask, granted.hold.grant]).toEqual([null, 'tete']);
        expect(sent(granted.effects)).toEqual([['tete', 1]]);
    });

    it('takes a gesture older than five seconds for no yes, and one just in time for one', () => {
        const late = run(rest(AWAKE), touch(0), ask('tete', TOUCH_MS + 1));
        expect([late.hold.ask, late.hold.grant]).toEqual(['tete', null]);
        const onTime = run(rest(AWAKE), touch(0), ask('tete', TOUCH_MS));
        expect(onTime.hold.grant).toBe('tete');
    });

    it('drops the ask the page gives up, and tells it off', () => {
        const {hold, effects} = run(rest(AWAKE), ask('tete', 0), ask(null, 10));
        expect(hold.ask).toBeNull();
        expect(told(effects)).toEqual(['asking tete', 'off -']);
        expect(sent(effects)).toEqual([]); // nothing was held: nothing to let go at the Mac
    });

    it('clears the last error at a new ask', () => {
        const refused = run(rest(AWAKE), ...play('volant'), robot('refused', null, 'scene'));
        expect(refused.hold.error).toContain('Une scène se joue');
        expect(step(refused.hold, ask('volant', 60_000)).hold.error).toBe('');
    });
});

describe('a robot asleep', () => {
    it('is woken once for the ask the gesture met, which is granted as it wakes, however long that took', () => {
        const waking = run(rest(ASLEEP), ...play('tete', 0), touch(500), ask('tete', 600));
        expect(kinds(waking.effects).filter((kind) => kind === 'wake')).toHaveLength(1);
        expect([waking.hold.ask, waking.hold.agreed, waking.hold.woken]).toEqual(['tete', true, 'asked']);
        expect(sent(waking.effects)).toEqual([]); // nothing is asked of a robot that sleeps
        const awake = step(waking.hold, said(AWAKE, 15_000)); // the click is long past its five seconds
        expect([awake.hold.grant, awake.hold.woken, awake.hold.agreed]).toEqual(['tete', 'yes', false]);
        expect(sent(awake.effects)).toEqual([['tete', 1]]);
    });

    it('is put back to sleep when the frame goes, if this frame woke it and it has not slept since', () => {
        const woken = run(rest(ASLEEP), ...play('tete'), said(AWAKE, 2000)).hold;
        expect(kinds(step(woken, {type: 'gone'}).effects)).toEqual(['send', 'sleep']); // the hold let go, then sleep
        const again = run(woken, {type: 'release'}, said(ASLEEP, 60_000), said(AWAKE, 90_000)).hold; // someone else's
        expect(again.woken).toBe('no');
        expect(step(again, {type: 'gone'}).effects).toEqual([]);
        const found = run(rest(AWAKE), ...play('tete')).hold; // awake as it was found
        expect(kinds(step(found, {type: 'gone'}).effects)).toEqual(['send']);
    });

    it('is put back to sleep by a frame that goes while it still wakes', () => {
        const waking = run(rest(ASLEEP), ...play('tete')).hold;
        expect(step(waking, {type: 'gone'}).effects).toEqual([{type: 'sleep'}]);
    });

    it.each([
        ['the Mac refuses the wake', {type: 'wake-failed', error: 'Le robot n’est pas lancé.'}, 'pas lancé'],
        ['it is not awake in time', {type: 'wake-timeout'}, 'ne se réveille pas']
    ] as const)(
        'is given up on when %s: the page is told off, why is said, nothing is left to put back',
        (_, end, why) => {
            const {hold, effects} = run(rest(ASLEEP), ...play('volant'), end);
            expect([hold.ask, hold.woken]).toEqual([null, 'no']);
            expect(hold.error).toContain(why);
            expect(told(effects).at(-1)).toBe('off -');
            expect(step(hold, {type: 'gone'}).effects).toEqual([]);
            expect(step(hold, end).hold).toBe(hold); // said twice, or late: nothing more
        }
    );

    it('waits for a robot falling asleep to sleep, then wakes it if the gesture is still fresh', () => {
        const closing = {...AWAKE, closing: true};
        const waiting = run(rest(closing), ...play('gachettes', 0));
        expect(waiting.effects.filter((effect) => effect.type !== 'tell')).toEqual([]);
        expect(kinds(step(waiting.hold, said(ASLEEP, 2000)).effects)).toEqual(['wake']);
        expect(step(waiting.hold, said(ASLEEP, TOUCH_MS + 1)).effects).toEqual([]); // too late: it waits for a gesture
    });

    it('keeps the yes of a wake for the ask it was given to: another profile asked meanwhile waits for its own', () => {
        const other = run(rest(ASLEEP), ...play('gachettes', 0), ask('tete', 9000), said(AWAKE, 10_000));
        expect([other.hold.ask, other.hold.grant, other.hold.woken]).toEqual(['tete', null, 'yes']);
        expect(step(other.hold, touch(11_000)).hold.grant).toBe('tete');
    });
});

describe('the lease and the robot’s word', () => {
    it('renews a hold every second, and only a hold', () => {
        const idle = rest(AWAKE);
        expect(step(idle, {type: 'lease'})).toEqual({hold: idle, effects: []});
        expect(step(idle, {type: 'lease'}).hold).toBe(idle); // the same value when nothing changed
        const {hold, effects} = run(idle, ...play('volant'), {type: 'lease'}, {type: 'lease'});
        expect(sent(effects)).toEqual([
            ['volant', 1],
            ['volant', 0],
            ['volant', 0]
        ]);
        expect(hold.owed).toBe(3);
    });

    it('tells the page on once per hold, at the robot’s first on', () => {
        const {hold, effects} = run(
            rest(AWAKE),
            ...play('volant'),
            robot('on', 'volant'),
            {type: 'lease'},
            robot('on', 'volant')
        );
        expect([hold.held, hold.open, hold.owed]).toEqual([true, 1, 0]);
        expect(told(effects).filter((word) => word.startsWith('on'))).toEqual(['on volant']);
    });

    it('tells a page that asks again for the profile it holds where that hold is', () => {
        const placing = run(rest(AWAKE), ...play('gachettes'));
        expect(told(step(placing.hold, ask('gachettes', 50)).effects)).toEqual(['asking gachettes']);
        const held = step(placing.hold, robot('on', 'gachettes')).hold;
        expect(step(held, ask('gachettes', 50))).toEqual({
            hold: held,
            effects: [{type: 'tell', state: 'on', profile: 'gachettes'}]
        });
    });

    it('says why the robot ends a hold, and lets go', () => {
        for (const [state, why, text] of [
            ['refused', 'scene', 'Une scène se joue'],
            ['refused', 'sleep', 'Reachy s’endort'],
            ['off', 'pose', 'n’a pas pu se mettre en place']
        ] as const) {
            const {hold, effects} = run(rest(AWAKE), ...play('tete'), robot(state, null, why));
            expect([hold.grant, hold.open]).toEqual([null, 0]);
            expect(hold.error).toContain(text);
            expect(told(effects).at(-1)).toBe('off -');
        }
        const scene = run(rest(AWAKE), ...play('tete'), robot('on', 'tete'), robot('off', null, 'scene'));
        expect([scene.hold.grant, scene.hold.error]).toEqual([null, '']); // taken back for a scene: no fault to say
        expect(sent(scene.effects).at(-1)).toEqual([null, 0]);
    });

    it('shows what the robot waits for once let go, until its next word', () => {
        const waiting = run(
            rest(AWAKE),
            ...play('tete'),
            robot('on', 'tete'),
            {type: 'release'},
            robot('returning', 'tete')
        );
        expect(waiting.hold.returning).toBe('tete');
        expect(step(waiting.hold, robot('off', null, 'app')).hold.returning).toBeNull();
        expect(step(waiting.hold, said({...AWAKE, manette: 'tete'}, 0)).hold.returning).toBe('tete');
        const lost = run(waiting.hold, said({...AWAKE, manette: 'tete'}), said(AWAKE)); // its off lost: the Mac says none
        expect(lost.hold.returning).toBeNull();
    });
});

describe('holds that end in the order they were granted', () => {
    it('switches profile without letting go: the off on the way belongs to the first', () => {
        const {hold, effects} = run(
            rest(AWAKE),
            ...play('gachettes'),
            robot('on', 'gachettes'),
            ...play('volant'),
            robot('off', null, 'profile'),
            robot('on', 'volant')
        );
        expect([hold.grant, hold.held, hold.open]).toEqual(['volant', true, 1]);
        expect(sent(effects).filter(([profile]) => profile === null)).toEqual([]);
    });

    it('keeps a hold granted again while the robot still gives the last one back', () => {
        const again = run(
            rest(AWAKE),
            ...play('volant'),
            robot('on', 'volant'),
            {type: 'release'},
            robot('returning', 'volant'),
            ...play('volant'),
            robot('off', null, 'app') // the end of the hold let go, not of the new one
        );
        expect([again.hold.grant, again.hold.held, again.hold.open]).toEqual(['volant', false, 1]);
        const on = step(again.hold, robot('on', 'volant'));
        expect(on.hold.held).toBe(true);
        expect(told(on.effects)).toEqual(['on volant']);
    });

    it('takes each off for the oldest hold not yet ended, then ends the one in force', () => {
        const three = run(
            rest(AWAKE),
            ...play('volant'),
            robot('on', 'volant'),
            {type: 'release'},
            ...play('tete'),
            {type: 'release'},
            ...play('gachettes')
        );
        expect(three.hold.open).toBe(3);
        const two = run(three.hold, robot('off', null, 'app'), robot('off', null, 'app'));
        expect([two.hold.grant, two.hold.open]).toEqual(['gachettes', 1]);
        const over = run(two.hold, robot('on', 'gachettes'), robot('off', null, 'scene'));
        expect([over.hold.grant, over.hold.open]).toEqual([null, 0]);
    });

    it('takes an on said again for a hold it let go as that hold’s, and the next as the new grant’s', () => {
        const renewed = run(rest(AWAKE), ...play('volant'), robot('on', 'volant'), {type: 'lease'}); // an on is owed
        const again = run(renewed.hold, {type: 'release'}, ...play('volant'));
        expect(again.hold.stale).toEqual({profile: 'volant', ons: 1});
        const late = step(again.hold, robot('on', 'volant')); // the hold let go, still in force, answers its renewal
        expect([late.hold.held, late.effects]).toEqual([false, []]);
        const mine = run(late.hold, robot('off', null, 'app'), robot('on', 'volant'));
        expect(mine.hold.held).toBe(true);
        expect(told(mine.effects)).toEqual(['on volant']);
    });

    it('counts no older hold once the robot holds the one in force, by its word or by the Mac’s', () => {
        const start = run(rest(AWAKE), ...play('volant'), {type: 'release'}, ...play('tete')).hold; // the robot got only tete
        expect(start.open).toBe(2);
        for (const word of [robot('on', 'tete'), said({...AWAKE, manette: 'tete'})]) {
            const held = step(start, word);
            expect([held.hold.held, held.hold.open]).toEqual([true, 1]);
            expect(told(held.effects)).toEqual(['on tete']);
            expect(step(held.hold, robot('off', null, 'scene')).hold.grant).toBeNull();
        }
    });

    it('counts the hold the Mac says in force as the oldest, when this frame grants with none of its own open', () => {
        const before: MacSaid = {...AWAKE, manette: 'volant'}; // the frame shown before's, still given back
        const granted = run(rest(before), ...play('volant'));
        expect(sent(granted.effects)).toEqual([['volant', 2]]);
        expect([granted.hold.open, granted.hold.held]).toEqual([2, false]); // the Mac's word is that hold's, not ours
        const kept = step(granted.hold, robot('off', null, 'app')); // its end
        expect([kept.hold.grant, kept.hold.open]).toEqual(['volant', 1]);
        const lost = step(granted.hold, said(AWAKE)); // or its off lost: the Mac says none
        expect([lost.hold.grant, lost.hold.open]).toEqual(['volant', 1]);
    });
});

describe('what the Mac refuses, and what it says', () => {
    it('forgets a grant the Mac refused, with the hold counted for it: the next one is answered', () => {
        const refused = run(rest({...AWAKE, manette: 'volant'}), ...play('gachettes'), {
            type: 'failed',
            profile: 'gachettes',
            opened: 2,
            error: 'Une autre app se sert déjà de Reachy comme manette.'
        });
        expect([refused.hold.grant, refused.hold.open, refused.hold.owed]).toEqual([null, 0, 0]);
        expect(refused.hold.error).toContain('autre app');
        expect(told(refused.effects).at(-1)).toBe('off -');
    });

    it('keeps the hold in force counted when the grant refused was replaced by another profile since', () => {
        const {hold} = run(rest({...AWAKE, manette: 'volant'}), ...play('gachettes'), ...play('tete'), {
            type: 'failed',
            profile: 'gachettes',
            opened: 2,
            error: 'refusé'
        });
        expect([hold.grant, hold.open, hold.error]).toEqual(['tete', 2, '']);
    });

    it('forgets a hold whose renewal the Mac refused: the robot keeps it until its lease runs out', () => {
        const held = run(rest(AWAKE), ...play('volant'), robot('on', 'volant'), {type: 'lease'}).hold;
        const {hold, effects} = step(held, {
            type: 'failed',
            profile: 'volant',
            opened: 0,
            error: 'Le Mac est injoignable.'
        });
        expect([hold.grant, hold.held, hold.open, hold.owed]).toEqual([null, false, 1, 0]);
        expect(hold.stale).toEqual({profile: 'volant', ons: 0}); // it may still say on, and owes its off
        expect(told(effects)).toEqual(['off -']);
        expect(step(hold, {type: 'failed', profile: null, opened: 0, error: 'x'}).hold).toBe(hold); // a let-go lost
    });

    it('takes the Mac’s word for an on or an off lost in a reconnect', () => {
        const granted = run(rest(AWAKE), ...play('gachettes')).hold;
        const on = step(granted, said({...AWAKE, manette: 'gachettes'}));
        expect(on.hold.held).toBe(true);
        expect(told(on.effects)).toEqual(['on gachettes']);
        const off = step(on.hold, said(AWAKE));
        expect([off.hold.grant, off.hold.open]).toEqual([null, 0]);
        expect(sent(off.effects)).toEqual([[null, 0]]);
        expect(step(on.hold, said({...AWAKE, manette: 'gachettes'})).hold).toBe(on.hold); // said again: nothing new
    });

    it('lets go as the frame goes, the hold’s own last word', () => {
        const held = run(rest(AWAKE), ...play('volant'), robot('on', 'volant')).hold;
        expect(step(held, {type: 'gone'}).effects).toEqual([{type: 'send', profile: null, opened: 0}]);
        expect(step(rest(AWAKE), {type: 'gone'}).effects).toEqual([]);
    });
});

describe('whatever happens', () => {
    const PROFILES: ManetteProfile[] = ['gachettes', 'volant', 'paume', 'tete', 'voix', 'tete+voix'];
    /** A small generator of its own: the same sequences at each run. */
    const dice = (seed: number) => () => {
        seed = (seed * 1664525 + 1013904223) >>> 0;
        return seed / 2 ** 32;
    };
    const events = (roll: () => number, now: number, gestures: boolean): HoldEvent => {
        const pick = <T>(of: readonly T[]): T => of[Math.floor(roll() * of.length)];
        const profile = () => pick([...PROFILES, null]);
        const kind = pick([
            'ask',
            'touch',
            'release',
            'robot',
            'session',
            'lease',
            'failed',
            'wake-failed',
            'wake-timeout'
        ] as const);
        if (kind === 'ask') return ask(profile(), now);
        if (kind === 'touch') return gestures ? touch(now) : {type: 'lease'};
        if (kind === 'robot')
            return robot(
                pick(['on', 'returning', 'off', 'refused']),
                profile(),
                pick(['app', 'profile', 'scene', 'pose', null])
            );
        if (kind === 'session') return said({awake: roll() < 0.7, closing: roll() < 0.2, manette: profile()}, now);
        if (kind === 'failed') return {type: 'failed', profile: profile(), opened: pick([0, 1, 2]), error: 'refusé'};
        if (kind === 'wake-failed') return {type: 'wake-failed', error: 'refusé'};
        return {type: kind};
    };

    it('never takes the robot, nor wakes it, without a gesture of the human’s in the app', () => {
        for (let seed = 1; seed <= 40; seed++) {
            const roll = dice(seed);
            let hold = rest(roll() < 0.5 ? AWAKE : ASLEEP);
            for (let n = 0; n < 300; n++) {
                const next = step(hold, events(roll, n * 700, false));
                hold = next.hold;
                expect(
                    next.effects.filter(
                        (effect) => effect.type === 'wake' || (effect.type === 'send' && effect.profile)
                    )
                ).toEqual([]);
            }
            expect(hold.grant).toBeNull();
        }
    });

    it('grants only at a gesture no older than five seconds, or as the robot wakes from one', () => {
        for (let seed = 1; seed <= 40; seed++) {
            const roll = dice(seed);
            let hold = rest(roll() < 0.5 ? AWAKE : ASLEEP);
            let touched = -Infinity;
            for (let n = 0; n < 400; n++) {
                const now = n * 900;
                const event = events(roll, now, true);
                if (event.type === 'touch') touched = now;
                const woke = hold.agreed; // a yes given while it slept, waiting on its wake
                const next = step(hold, event);
                for (const effect of next.effects) {
                    if (effect.type === 'wake') expect(now - touched).toBeLessThanOrEqual(TOUCH_MS);
                    if (effect.type === 'send' && effect.opened > 0)
                        expect(woke || now - touched <= TOUCH_MS).toBe(true);
                    if (effect.type === 'send' && effect.profile && !effect.opened)
                        expect(effect.profile).toBe(hold.grant);
                }
                hold = next.hold;
                expect(Math.min(hold.open, hold.owed, hold.stale.ons)).toBeGreaterThanOrEqual(0);
                expect(hold.held && !hold.grant).toBe(false); // nothing is held without a grant
            }
        }
    });
});
