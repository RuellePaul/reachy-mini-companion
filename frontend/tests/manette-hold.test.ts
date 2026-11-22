// @vitest-environment happy-dom
// The manette's hold in the dashboard (src/apps/Manette.tsx): the human's OK (their own gesture in the app), a robot
// asleep woken for it and left as it was found, the lease, a profile switch, a refusal,
// letting go, what the Mac's session says, and the robot's pose and its ear's level streamed to the app as pads,
// against a mocked line to the Mac.
import {act, createElement, useEffect} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {poseFrameSchema, type PoseFrame, type RobotEvent} from '../src/api/schemas';
import {LEASE_MS, ManetteBar, TOUCH_MS, useManette, WAKE_MS, type ManetteHold} from '../src/apps/Manette';
import {held as heldAs} from '../src/apps/pad';
import examples from './contract-examples.json';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const mocks = vi.hoisted(() => {
    const pose = new Set<(frame: unknown) => void>(); // the dashboard's listeners on the `pose` channel
    const voice = new Set<(heard: unknown) => void>(); // and on the `voice` channel
    const wanted: string[] = []; // the optional channels it asks the Mac for
    return {
        conversation: (() => {}) as (event: RobotEvent) => void,
        session: {awake: true, phase: 'idle' as string | null, manette: null as string | null},
        sessions: [] as string[], // the wakes and sleeps the Mac took
        refusal: '', // why the Mac refuses a wake (its detail), if it does
        posts: [] as [string | null, boolean][],
        fail: 0, // the next posts the Mac refuses
        gate: null as Promise<void> | null, // while set, the Mac answers once it resolves
        pose,
        voice,
        wanted,
        // Stable, as the provider's are: a render never restarts the stream.
        on: (channel: string, listener: (frame: unknown) => void) => {
            const listeners = channel === 'pose' ? pose : channel === 'voice' ? voice : null;
            listeners?.add(listener);
            return () => void listeners?.delete(listener);
        },
        want: (channel: string) => {
            wanted.push(channel);
            return () => void wanted.splice(wanted.indexOf(channel), 1);
        }
    };
});
vi.mock('../src/live', () => ({
    useLive: () => ({
        on: mocks.on,
        want: mocks.want,
        onConversation: (listener: (event: RobotEvent) => void) => {
            mocks.conversation = listener;
            return () => {};
        },
        session: mocks.session
    })
}));
vi.mock('../src/api/client', async (original) => {
    const client = await original<typeof import('../src/api/client')>();
    return {
        ...client,
        api: {
            session: async (action: string) => {
                if (action === 'wake' && mocks.refusal) throw new client.ApiError(409, mocks.refusal);
                mocks.sessions.push(action);
            }
        }
    };
});
vi.mock('../src/apps/api', () => ({
    appsApi: {
        manette: async (_id: string, profile: string | null, grant = false) => {
            if (mocks.gate) await mocks.gate;
            mocks.posts.push([profile, grant]);
            if (mocks.fail > 0) {
                mocks.fail -= 1;
                throw new Error('Le Mac est injoignable.');
            }
        }
    }
}));

const told: object[] = [];
const frame = {current: {contentWindow: {postMessage: (message: object) => told.push(message)}}} as never;
const seen: {hold: ManetteHold | null} = {hold: null};
const held = (): ManetteHold => {
    if (!seen.hold) throw new Error('not rendered');
    return seen.hold;
};
const Harness = () => {
    const current = useManette('a1', frame);
    useEffect(() => {
        seen.hold = current;
    });
    return null;
};
const event = (
    state: 'on' | 'off' | 'returning' | 'refused',
    profile: string | null = null,
    why: string | null = null
) => act(async () => mocks.conversation({kind: 'manette', session_id: 's', state, profile, why} as RobotEvent));
/** « Jouer »: the human's click in the app, then what the page asks at once. */
const play = async (profile: 'gachettes' | 'volant' | 'paume' | 'tete' | 'voix' | 'tete+voix') => {
    await act(async () => held().touch());
    await act(async () => held().request(profile));
};
const lastTold = () => told[told.length - 1] as {type: string; state: string; profile: string | null};
const still = poseFrameSchema.parse(examples.WsMessage.find((message) => message.channel === 'pose')?.data);
const pushed = (right: number): PoseFrame => ({...still, a: [still.a[0] + (right * Math.PI) / 180, still.a[1]]});
const stream = (frames: PoseFrame[]) =>
    act(async () => frames.forEach((pose) => mocks.pose.forEach((listener) => listener(pose))));
const hear = (level: number) => act(async () => mocks.voice.forEach((listener) => listener({level})));
const pads = () => told.filter((message) => (message as {type: string}).type === 'pad');

describe('the manette hold', () => {
    let root: Root;
    beforeEach(async () => {
        vi.useFakeTimers();
        told.length = 0;
        mocks.posts.length = 0;
        mocks.fail = 0;
        mocks.gate = null;
        mocks.session.manette = null;
        mocks.session.awake = true;
        mocks.session.phase = 'idle';
        mocks.refusal = '';
        mocks.sessions.length = 0;
        mocks.pose.clear();
        mocks.voice.clear();
        mocks.wanted.length = 0;
        root = createRoot(document.createElement('div'));
        await act(async () => root.render(createElement(Harness)));
    });
    afterEach(async () => {
        await act(async () => root.unmount());
        vi.useRealTimers();
    });
    const tick = (ms: number) => act(async () => void vi.advanceTimersByTime(ms));

    it('grants at once what the app asks right after the human’s click in it, then renews', async () => {
        await act(async () => held().touch()); // « Jouer »
        await act(async () => held().request('volant'));
        expect(held().asked).toBeNull();
        expect(held().granted).toBe('volant');
        expect(lastTold()).toMatchObject({type: 'manette', state: 'asking', profile: 'volant'}); // getting into place
        await tick(2 * LEASE_MS);
        expect(mocks.posts).toEqual([
            ['volant', true],
            ['volant', false],
            ['volant', false]
        ]);
        await event('on', 'volant');
        expect(held().holding).toBe(true);
        expect(lastTold()).toMatchObject({state: 'on', profile: 'volant'});
        expect(mocks.sessions).toEqual([]); // awake as it was found: nothing to wake, nothing to put back to sleep
    });

    it('keeps an ask that follows no gesture of the human’s until they act in the app', async () => {
        await act(async () => held().request('gachettes')); // as the page loads, or on a timer of its own
        expect(held().asked).toBe('gachettes');
        expect(lastTold()).toMatchObject({state: 'asking', profile: 'gachettes'});
        await tick(TOUCH_MS + 1000);
        expect(mocks.posts).toEqual([]);
        await act(async () => held().request('tete')); // the latest ask is the one that waits
        await act(async () => held().touch());
        expect(held().asked).toBeNull();
        expect(mocks.posts).toEqual([['tete', true]]);
    });

    it('takes a click older than five seconds for no yes: the ask waits for another', async () => {
        await act(async () => held().touch());
        await tick(TOUCH_MS + 1);
        await act(async () => held().request('tete'));
        expect(held().granted).toBeNull();
        expect(held().asked).toBe('tete');
        await act(async () => held().request(null)); // the page gives up
        expect(held().asked).toBeNull();
        expect(lastTold()).toMatchObject({state: 'off', profile: null});
    });

    it('grants again an app that lets go and asks again in one go', async () => {
        await play('volant');
        await event('on', 'volant');
        await act(async () => {
            held().touch(); // « R »: the game over and the new one, two messages before React renders
            held().request(null);
            held().request('volant');
        });
        await tick(0);
        expect(held().granted).toBe('volant');
        expect(mocks.posts).toEqual([
            ['volant', true],
            [null, false],
            ['volant', true]
        ]);
    });

    it('wakes a sleeping robot for the app that asks, and puts it back to sleep when the app is left', async () => {
        mocks.session.awake = false;
        mocks.session.phase = null;
        await act(async () => root.render(createElement(Harness)));
        await play('tete');
        expect(mocks.sessions).toEqual(['wake']);
        expect(held().waking).toBe(true);
        expect(mocks.posts).toEqual([]); // nothing is asked of a robot that sleeps
        await tick(3000); // its wake move, its connection
        expect(mocks.sessions).toEqual(['wake']);
        mocks.session.awake = true;
        mocks.session.phase = 'idle';
        await act(async () => root.render(createElement(Harness)));
        expect(held().waking).toBe(false);
        expect(mocks.posts).toEqual([['tete', true]]);
        await event('on', 'tete');
        await act(async () => held().release()); // the game is over: it stays awake while the app is shown
        expect(mocks.sessions).toEqual(['wake']);
        await act(async () => root.unmount()); // the app is left: as it was found
        expect(mocks.sessions).toEqual(['wake', 'sleep']);
        root = createRoot(document.createElement('div'));
        await act(async () => root.render(createElement(Harness)));
    });

    it('waits for a robot falling asleep to sleep, then wakes it', async () => {
        mocks.session.phase = 'closing';
        await act(async () => root.render(createElement(Harness)));
        await play('gachettes');
        expect(mocks.sessions).toEqual([]);
        expect(mocks.posts).toEqual([]);
        mocks.session.awake = false;
        mocks.session.phase = null;
        await act(async () => root.render(createElement(Harness)));
        expect(mocks.sessions).toEqual(['wake']);
    });

    it('gives up on a robot that does not wake, or that the Mac says is not running: the page is told off, and why is said', async () => {
        mocks.session.awake = false;
        await act(async () => root.render(createElement(Harness)));
        await play('volant');
        await tick(WAKE_MS);
        expect(held().asked).toBeNull();
        expect(held().error).toContain('ne se réveille pas');
        expect(lastTold()).toMatchObject({state: 'off', profile: null});
        mocks.refusal = "Le robot n'est pas lancé\u00a0: démarre-le avec make run."; // `make run` not running: 409
        await play('volant');
        await tick(0);
        expect(held().asked).toBeNull();
        expect(held().error).toBe(mocks.refusal); // the Mac's own word, at once
        expect(lastTold()).toMatchObject({state: 'off', profile: null});
        expect(mocks.sessions).toEqual(['wake']); // the first one's only
        expect(mocks.posts).toEqual([]);
        await act(async () => root.unmount()); // neither wake came: nothing to put back to sleep
        expect(mocks.sessions).toEqual(['wake']);
        root = createRoot(document.createElement('div'));
        await act(async () => root.render(createElement(Harness)));
    });

    it('holds the yes that started a wake through it, however long the robot takes to wake', async () => {
        mocks.session.awake = false;
        mocks.session.phase = null;
        await act(async () => root.render(createElement(Harness)));
        await play('tete');
        await tick(WAKE_MS - 1000); // a slow wake: the click is long past its five seconds
        expect(held().waking).toBe(true);
        mocks.session.awake = true;
        mocks.session.phase = 'idle';
        await act(async () => root.render(createElement(Harness)));
        expect(mocks.posts).toEqual([['tete', true]]);
        expect(held().error).toBe('');
    });

    it('leaves awake a robot that slept again and that someone else woke: only its own wake is put back', async () => {
        mocks.session.awake = false;
        await act(async () => root.render(createElement(Harness)));
        await play('volant');
        mocks.session.awake = true;
        await act(async () => root.render(createElement(Harness))); // woken for this app
        await act(async () => held().release());
        mocks.session.awake = false; // it fell asleep by itself
        await act(async () => root.render(createElement(Harness)));
        mocks.session.awake = true; // an antenna, the header's button: not this app's doing
        await act(async () => root.render(createElement(Harness)));
        await act(async () => root.unmount());
        expect(mocks.sessions).toEqual(['wake']);
        root = createRoot(document.createElement('div'));
        await act(async () => root.render(createElement(Harness)));
    });

    it('tells an app that asks again for the profile it holds where that hold is', async () => {
        await play('gachettes');
        told.length = 0;
        await act(async () => held().request('gachettes')); // granted, the robot still getting into place
        expect(told).toEqual([{reachy: 1, type: 'manette', state: 'asking', profile: 'gachettes'}]);
        await event('on', 'gachettes');
        told.length = 0;
        await act(async () => held().request('gachettes')); // a new round's « Jouer »
        expect(told).toEqual([{reachy: 1, type: 'manette', state: 'on', profile: 'gachettes'}]);
        expect(held().asked).toBeNull();
    });

    it('switches profile without letting go in between', async () => {
        await play('gachettes');
        await event('on', 'gachettes');
        await play('volant');
        await event('off', null, 'profile'); // the robot gives the first one back on its way to the second
        await event('on', 'volant');
        expect(held().granted).toBe('volant');
        expect(held().holding).toBe(true);
        expect(mocks.posts.filter(([profile]) => profile === null)).toEqual([]);
    });

    it.each([
        ['volant', 'ramène sa base face à toi'],
        ['paume', 'ramène sa base face à toi'], // its antennas stayed under torque: only the base was freed
        ['tete', 'ramène sa base et sa tête face à toi'] // the head, freed, is taken back within reach of its base
    ] as const)(
        'lets go, and says all the robot waits for before its torque comes back (%s)',
        async (profile, words) => {
            const bar = document.createElement('div');
            const barRoot = createRoot(bar);
            const shown = async () => {
                await act(async () => barRoot.render(createElement(ManetteBar, {hold: held(), title: 'Kart'})));
                return bar.textContent ?? '';
            };
            await play(profile);
            await event('on', profile);
            await act(async () => held().release());
            await tick(0);
            expect(mocks.posts[mocks.posts.length - 1]).toEqual([null, false]);
            expect(lastTold()).toMatchObject({state: 'off', profile: null});
            await event('returning', profile);
            const text = await shown();
            expect(text).toContain(words);
            expect(text.includes('tête')).toBe(profile === 'tete'); // a head under torque follows its base: never asked
            expect(held().returning).toBe(profile);
            await event('off', null, 'app');
            expect(held().returning).toBeNull();
            expect(await shown()).not.toContain('Lâche Reachy');
            await act(async () => barRoot.unmount());
        }
    );

    it.each(['gachettes', 'volant'])(
        'keeps a hold granted again (%s) while the robot still gives the last one back',
        async (again) => {
            await play('volant');
            await event('on', 'volant');
            await act(async () => held().release()); // the game is over
            await tick(0);
            await event('returning', 'volant'); // the robot waits, freed, to be let go within reach
            await act(async () => held().touch()); // « Rejouer »
            await act(async () => held().request(again as 'gachettes' | 'volant')); // waits behind its exit
            await tick(0);
            const sent = mocks.posts.length;
            await event('off', null, 'app'); // the end of the hold let go before
            await tick(0);
            expect(mocks.posts.slice(sent)).toEqual([]);
            expect(held().granted).toBe(again);
            await event('on', again);
            expect(held().holding).toBe(true);
            expect(lastTold()).toMatchObject({state: 'on', profile: again});
        }
    );

    it.each(['said', 'lost in a reconnect'])(
        'keeps a grant made while the frame before this one still gives its hold back, that hold’s off %s',
        async (end) => {
            mocks.session.manette = 'volant'; // the app shown before held it: closed, its let-go sent
            await act(async () => root.render(createElement(Harness)));
            await event('returning', 'volant'); // the robot waits for the hand on its base
            await play('volant'); // the robot's grant waits behind that exit
            await tick(0);
            const sent = mocks.posts.length;
            if (end === 'said') await event('off', null, 'app'); // the end of the other frame's hold
            mocks.session.manette = null;
            await act(async () => root.render(createElement(Harness)));
            await tick(0);
            expect(mocks.posts.slice(sent)).toEqual([]);
            expect(held().granted).toBe('volant');
            await event('refused', null, 'scene'); // this grant's own end
            expect(held().granted).toBeNull();
            expect(held().error).toContain('Une scène se joue');
        }
    );

    it.each(['late', 'on time, its end lost'])(
        'takes an on said again for a hold it let go (%s) as that hold’s, and the next as the new grant’s',
        async (answer) => {
            await play('volant');
            await event('on', 'volant');
            await tick(LEASE_MS); // a renewal: the robot answers it on
            if (answer !== 'late') await event('on', 'volant');
            await act(async () => held().release());
            await play('volant'); // « Rejouer »
            await tick(0);
            const sent = mocks.posts.length;
            const ons = told.length;
            if (answer === 'late') await event('on', 'volant'); // the hold let go is still in force
            expect(held().holding).toBe(false);
            await event('returning', 'volant');
            if (answer === 'late') await event('off', null, 'app'); // its end, else lost on the way
            await tick(0);
            expect(mocks.posts.slice(sent)).toEqual([]);
            expect(held().granted).toBe('volant');
            expect(told.slice(ons)).toEqual([]); // the app is told nothing until the robot holds its grant
            await event('on', 'volant');
            expect(held().holding).toBe(true);
            expect(lastTold()).toMatchObject({state: 'on', profile: 'volant'});
        }
    );

    it('takes each off for the oldest hold not yet ended, then ends the one in force', async () => {
        const grant = async (profile: 'gachettes' | 'volant' | 'tete') => {
            await play(profile);
        };
        await grant('volant');
        await event('on', 'volant');
        await act(async () => held().release());
        await grant('tete');
        await act(async () => held().release()); // let go while the robot still returns the first
        await grant('gachettes');
        await tick(0);
        const sent = mocks.posts.length;
        await event('off', null, 'app'); // volant's
        await event('off', null, 'app'); // tete's, stopped before anything was freed
        expect(held().granted).toBe('gachettes');
        await event('on', 'gachettes');
        expect(held().holding).toBe(true);
        await event('off', null, 'scene'); // the hold in force
        await tick(0);
        expect(held().granted).toBeNull();
        expect(mocks.posts.slice(sent)).toEqual([[null, false]]);
    });

    it('waits for no end of a grant the Mac refused: the next one is answered', async () => {
        mocks.fail = 1; // asleep: the Mac refuses it, the robot never gets it
        await play('volant');
        await tick(0);
        expect(held().granted).toBeNull();
        await play('volant');
        await event('refused', null, 'scene');
        expect(held().granted).toBeNull();
        expect(held().error).toContain('Une scène se joue');
    });

    it('counts no hold of another app for a grant the Mac refused: that hold over, its off lost, the next refusal is said', async () => {
        mocks.session.manette = 'volant'; // another dashboard's app holds the robot, and renews it
        await act(async () => root.render(createElement(Harness)));
        mocks.fail = 1; // 409: another app holds it
        await play('gachettes');
        await tick(0);
        expect(held().granted).toBeNull();
        await event('on', 'volant'); // that app's renewal, answered on
        mocks.session.manette = null; // its hold over, its off lost in a reconnect
        await act(async () => root.render(createElement(Harness)));
        await play('gachettes'); // « Rejouer »
        await event('refused', null, 'scene');
        expect(held().granted).toBeNull();
        expect(held().error).toContain('Une scène se joue');
    });

    it('keeps the hold the Mac said in force counted for a grant of another profile made while the first is refused', async () => {
        mocks.session.manette = 'volant'; // the app shown before held it: closed, its let-go sent
        await act(async () => root.render(createElement(Harness)));
        let answer = () => {};
        mocks.gate = new Promise<void>((resolve) => (answer = resolve)); // the Mac answers slowly
        await play('gachettes');
        await play('tete'); // another profile, granted before the first is answered
        mocks.fail = 1; // the first grant refused
        mocks.gate = null;
        await act(async () => answer());
        await tick(0);
        expect(held().granted).toBe('tete');
        await event('off', null, 'app'); // the end of the hold given back
        expect(held().granted).toBe('tete');
        await event('refused', null, 'scene'); // this grant's own end
        expect(held().granted).toBeNull();
        expect(held().error).toContain('Une scène se joue');
    });

    it.each(['event', 'snapshot'])(
        'counts no older hold once the robot holds the one in force (its %s)',
        async (word) => {
            await play('volant');
            await act(async () => held().release()); // so soon the Mac kept only the off: the robot got nothing
            await play('tete');
            if (word === 'event') await event('on', 'tete');
            else {
                mocks.session.manette = 'tete'; // the on lost in a reconnect, put right
                await act(async () => root.render(createElement(Harness)));
            }
            expect(held().holding).toBe(true);
            await event('off', null, 'scene');
            expect(held().granted).toBeNull();
        }
    );

    it('tells the human to let go while the robot waits, freed, between two profiles', async () => {
        const bar = document.createElement('div');
        const barRoot = createRoot(bar);
        const shown = async () => {
            await act(async () => barRoot.render(createElement(ManetteBar, {hold: held(), title: 'Kart'})));
            return bar.textContent ?? '';
        };
        await play('volant');
        await event('on', 'volant');
        await play('tete'); // the robot gives the base back first, once let go within reach
        await event('returning', 'volant');
        expect(await shown()).toContain('Lâche Reachy');
        expect(await shown()).toContain('Rendre la main'); // the new grant's lease runs: it can still be given back
        await event('off', null, 'profile');
        expect(await shown()).toContain('Reachy se met en place');
        await event('on', 'tete');
        expect(await shown()).toContain(heldAs('tete'));
        await act(async () => barRoot.unmount());
    });

    it.each(['let go', 'given back on a switch'])(
        'keeps asking to let go above an ask that waits while the robot waits, freed, for the hand (a hold %s)',
        async (how) => {
            const bar = document.createElement('div');
            const barRoot = createRoot(bar);
            const shown = async () => {
                await act(async () => barRoot.render(createElement(ManetteBar, {hold: held(), title: 'Kart'})));
                return bar.textContent ?? '';
            };
            await play('volant');
            await event('on', 'volant');
            if (how === 'let go') {
                await act(async () => held().request(null)); // the game over: the page lets go
            } else {
                await play('tete'); // the robot gives the base back first, once let go within reach
            }
            await event('returning', 'volant');
            await tick(TOUCH_MS + 1); // the human's last click in the app is old
            await act(async () => held().request(how === 'let go' ? 'volant' : 'gachettes')); // asked by the page alone
            const text = await shown();
            expect(text).toContain('Lâche Reachy et ramène sa base face à toi');
            expect(text).toContain('« Kart » veut se servir de Reachy');
            expect(text).toContain('Clique dans l’app pour le lui prêter.');
            expect(text.indexOf('Lâche')).toBeLessThan(text.indexOf('Kart')); // above: what the ask waits for is under
            expect(text.includes('Rendre la main')).toBe(how !== 'let go'); // while a grant runs
            await act(async () => barRoot.unmount());
        }
    );

    it('shows the controller in force once the Mac says the robot holds it, the switch’s events lost', async () => {
        const bar = document.createElement('div');
        const barRoot = createRoot(bar);
        const shown = async () => {
            await act(async () => barRoot.render(createElement(ManetteBar, {hold: held(), title: 'Kart'})));
            return bar.textContent ?? '';
        };
        await play('volant');
        await event('on', 'volant');
        await play('tete');
        await event('returning', 'volant');
        expect(await shown()).toContain('Lâche Reachy');
        mocks.session.manette = 'tete'; // the off and the on lost in a reconnect; the snapshot re-read
        await act(async () => root.render(createElement(Harness)));
        expect(held().holding).toBe(true);
        const text = await shown();
        expect(text).toContain(heldAs('tete'));
        expect(text).toContain('Rendre la main');
        await act(async () => barRoot.unmount());
    });

    it.each(['let go', 'forgotten', 'granted by the frame before this one'])(
        'stops asking to let go of a hold %s once the Mac says it is over, its off lost in a reconnect',
        async (hold) => {
            const bar = document.createElement('div');
            const barRoot = createRoot(bar);
            const shown = async () => {
                await act(async () => barRoot.render(createElement(ManetteBar, {hold: held(), title: 'Kart'})));
                return bar.textContent ?? '';
            };
            if (hold !== 'granted by the frame before this one') {
                await play('volant');
                await event('on', 'volant');
            }
            mocks.session.manette = 'volant'; // the Mac's session, from the on to the off
            await act(async () => root.render(createElement(Harness)));
            if (hold === 'let go') await act(async () => held().release()); // « Rendre la main »
            if (hold === 'forgotten') mocks.fail = 1; // a renewal lost: the dashboard forgets, the robot's lease runs out
            await tick(LEASE_MS);
            await event('returning', 'volant'); // its base past reach: it waits, freed, to be let go
            expect(await shown()).toContain('Lâche Reachy');
            mocks.session.manette = null; // taken back; its off lost, the session re-read
            await act(async () => root.render(createElement(Harness)));
            expect(held().returning).toBeNull();
            expect(await shown()).not.toContain('Lâche Reachy');
            await act(async () => barRoot.unmount());
        }
    );

    it('reads the antennas of a robot held under the palm from where they lie lowered', async () => {
        const lowered = (right = 0): PoseFrame => ({...still, a: [-3.05 + (right * Math.PI) / 180, 3.05]});
        await play('paume');
        await event('on', 'paume');
        await stream(new Array<PoseFrame>(8).fill(lowered())); // as in sleep: where « paume » holds them
        expect(pads()[0]).toMatchObject({left: false, right: false, antennas: {left: 0, right: 0}});
        await stream([lowered(7)]); // a finger lifts the right one a little
        expect(pads()[pads().length - 1]).toMatchObject({right: true, left: false, antennas: {right: 7, left: 0}});
        await act(async () => held().release());
    });

    it('streams the robot’s pose to the app as pads only while it holds, from where its antennas rest', async () => {
        await play('gachettes');
        expect(mocks.wanted).toEqual([]); // granted, the robot still getting into place: no pose asked of the Mac
        await event('on', 'gachettes');
        expect(mocks.wanted).toEqual(['pose']);
        await stream(new Array<PoseFrame>(8).fill(still)); // where the antennas rest, measured
        expect(pads()).toEqual([
            {
                reachy: 1,
                type: 'pad',
                left: false,
                right: false,
                antennas: {left: 0, right: 0},
                body: 0,
                head: {yaw: 0, pitch: 0, roll: 0, x: 0, y: 0, z: 0},
                t: still.t, // when the robot measured it, on its own clock
                voice: 0 // a profile that does not listen hears nothing
            }
        ]);
        await stream([pushed(10)]); // the right antenna pushed forward by hand
        expect(pads()[pads().length - 1]).toMatchObject({right: true, left: false, antennas: {right: 10, left: 0}});
        await act(async () => held().release());
        expect(mocks.wanted).toEqual([]);
        expect(mocks.pose.size).toBe(0);
    });

    it('streams the ear alone as one pad per level: no body is lent, no pose is asked for', async () => {
        await play('voix');
        await event('on', 'voix');
        expect(mocks.wanted).toEqual(['voice']);
        await hear(0.4);
        await hear(0.7);
        expect(pads().map((pad) => (pad as {voice: number}).voice)).toEqual([0.4, 0.7]);
        expect(pads()[0]).toMatchObject({left: false, right: false, body: 0, head: {yaw: 0, pitch: 0, roll: 0}});
        await act(async () => held().release());
        expect(mocks.wanted).toEqual([]);
        expect(mocks.voice.size).toBe(0);
    });

    it('gives a body that listens its pads at the pose’s pace, each with the ear’s latest level', async () => {
        await play('tete+voix');
        await event('on', 'tete+voix');
        expect([...mocks.wanted].sort()).toEqual(['pose', 'voice']);
        await hear(0.5); // before the rest is measured: kept, no pad of its own
        expect(pads()).toEqual([]);
        await stream(new Array<PoseFrame>(8).fill(still));
        await hear(0.2);
        await stream([still, still]);
        expect(pads().map((pad) => (pad as {voice: number}).voice)).toEqual([0.5, 0.2, 0.2]);
    });

    it('says what an ask waits for: the human’s own gesture in the app, or the robot waking', async () => {
        const bar = document.createElement('div');
        const barRoot = createRoot(bar);
        const shown = async () => {
            await act(async () => barRoot.render(createElement(ManetteBar, {hold: held(), title: 'Soleil'})));
            return bar.textContent ?? '';
        };
        await act(async () => held().request('voix')); // no click came before it
        expect(await shown()).toContain('aucun son ne quitte le robot');
        expect(await shown()).toContain('Clique dans l’app pour le lui prêter.');
        expect(bar.querySelectorAll('button')).toHaveLength(0); // nothing to answer above the app
        await act(async () => held().request('tete+voix'));
        expect(await shown()).toContain('Elle écoute aussi');
        mocks.session.awake = false;
        await act(async () => root.render(createElement(Harness)));
        await act(async () => held().touch());
        expect(await shown()).toBe('Reachy se réveille pour jouer…');
        await act(async () => barRoot.unmount());
    });

    it('says the robot could not take its still pose, and lets go', async () => {
        await play('tete');
        await event('off', null, 'pose'); // the still pose not reached: nothing freed, the body taken back
        expect(held().granted).toBeNull();
        expect(held().error).toBe('Reachy n’a pas pu se mettre en place\u00a0: réessaie.');
        expect(lastTold()).toMatchObject({type: 'manette', state: 'off', profile: null});
    });

    it('says why the robot refuses: a scene plays, or it falls asleep', async () => {
        for (const [why, text] of [
            ['scene', 'Une scène se joue'],
            ['sleep', 'Reachy s’endort']
        ]) {
            await play('volant');
            await event('refused', null, why);
            expect(held().granted).toBeNull();
            expect(held().error).toContain(text);
        }
    });

    it('keeps its French whole on a narrow bar: a no-break space inside « » and before : ; ! ?', async () => {
        const bar = document.createElement('div');
        const barRoot = createRoot(bar);
        const shown: string[] = [];
        const show = async () => {
            await act(async () => barRoot.render(createElement(ManetteBar, {hold: held(), title: 'Kart'})));
            shown.push(bar.textContent ?? '');
        };
        for (const profile of ['gachettes', 'volant', 'paume', 'tete'] as const) {
            await act(async () => held().request(profile));
            await show(); // what the ask waits for: the human's own gesture in the app
            await act(async () => held().touch());
            await event('on', profile);
            await show(); // the controller in force
            await act(async () => held().release());
            await event('returning', profile);
            await show(); // let go: what the robot waits for
            await event('off', null, 'app');
        }
        for (const [state, why] of [
            ['off', 'pose'],
            ['refused', 'scene'],
            ['refused', 'sleep']
        ] as const) {
            await play('volant');
            await event(state, null, why);
            await show(); // why it is over
        }
        await act(async () => barRoot.unmount());
        expect(shown[0]).toContain('« Kart »');
        for (const text of shown) expect(text).not.toMatch(/« | »| [:;!?]/);
    });

    it('puts its buttons under the words on a bar too narrow for both, the words keeping a readable width', async () => {
        const bar = document.body.appendChild(document.createElement('div'));
        const barRoot = createRoot(bar);
        const layouts: string[][] = [];
        const show = async () => {
            await act(async () => barRoot.render(createElement(ManetteBar, {hold: held(), title: 'Kart'})));
            const alert = bar.querySelector<HTMLElement>('.MuiAlert-root') as HTMLElement;
            const words = alert.querySelector<HTMLElement>('.MuiAlert-message') as HTMLElement;
            layouts.push([getComputedStyle(alert).flexWrap, getComputedStyle(words).flexBasis]);
        };
        await act(async () => held().request('volant'));
        await show(); // what the ask waits for: words only
        await act(async () => held().touch());
        await show(); // getting into place: « Rendre la main »
        await act(async () => held().release());
        await event('returning', 'volant');
        await show(); // let go: « Rendre la main »
        await act(async () => barRoot.unmount());
        bar.remove();
        expect(layouts).toEqual(new Array(3).fill(['wrap', '16em'])); // a line of words, then the buttons at its end
    });

    it('shows a hold it forgot and granted again once the robot says it, and tells the app once per hold', async () => {
        await play('volant');
        await event('on', 'volant');
        mocks.session.manette = 'volant';
        await act(async () => root.render(createElement(Harness)));
        mocks.fail = 1; // a renewal lost: the dashboard forgets, the robot still holds until its lease runs out
        await tick(LEASE_MS);
        expect(held().granted).toBeNull();
        await play('volant'); // the session still says volant: only the robot's word shows it
        await event('on', 'volant'); // its answer to the grant: the renewal refused never reached it, nor owes an on
        expect(held().holding).toBe(true);
        for (let i = 0; i < 2; i++) {
            await tick(LEASE_MS);
            await event('on', 'volant'); // its answer to each renewal
        }
        expect(told.filter((message) => (message as {state?: string}).state === 'on')).toHaveLength(2);
    });

    it.each(['renewal', 'grant'])(
        'lets go last when the frame goes: after the post on its way, in place of a %s queued behind it',
        async (queued) => {
            await play('volant');
            await event('on', 'volant');
            let answer = () => {};
            mocks.gate = new Promise<void>((resolve) => (answer = resolve)); // the Mac answers slowly from here
            await tick(LEASE_MS); // a renewal on its way
            if (queued === 'renewal') {
                await tick(LEASE_MS); // the next one, queued behind it
            } else {
                await play('tete'); // a switch: its grant would free the head
            }
            mocks.gate = null;
            await act(async () => root.unmount()); // the apps surface closed
            await act(async () => answer());
            expect(mocks.posts).toEqual([
                ['volant', true],
                ['volant', false],
                [null, false]
            ]);
            root = createRoot(document.createElement('div')); // for the unmount after each test
        }
    );

    it('lets go when the Mac says the robot no longer holds, an off lost in a reconnect', async () => {
        await play('gachettes');
        mocks.session.manette = 'gachettes';
        await act(async () => root.render(createElement(Harness))); // a new session snapshot
        expect(held().holding).toBe(true); // the on was lost: put right
        mocks.session.manette = null;
        await act(async () => root.render(createElement(Harness)));
        expect(held().granted).toBeNull();
    });

    it.each(['before the robot says it returns', 'after it says it', 'after a switch let go'])(
        'keeps a grant made again %s, with the session as the Mac says it',
        async (when) => {
            // The Mac's session holds the profile from its on to its off (tests/mac/test_robot.py), each event first.
            const relay = async (state: 'on' | 'off' | 'returning', profile: string | null, why: string | null) => {
                await event(state, profile, why);
                if (state === 'returning') return;
                mocks.session.manette = profile;
                await act(async () => root.render(createElement(Harness)));
            };
            const grant = async (profile: 'gachettes' | 'volant' | 'tete') => {
                await play(profile);
                await tick(0);
            };
            await grant('volant');
            await relay('on', 'volant', null);
            if (when === 'after a switch let go') await grant('tete'); // its base past reach: it waits to be let go
            await act(async () => held().release()); // « Rendre la main »
            await tick(0);
            if (when.startsWith('before')) await grant('volant'); // « Rejouer »
            await relay('returning', 'volant', null);
            if (!when.startsWith('before')) await grant(when === 'after it says it' ? 'volant' : 'gachettes');
            const again = held().granted;
            const sent = mocks.posts.length;
            if (when === 'after a switch let go') await relay('off', null, 'profile');
            await relay('off', null, 'app');
            expect(mocks.posts.slice(sent)).toEqual([]);
            expect(held().granted).toBe(again);
            await relay('on', again, null);
            expect(held().holding).toBe(true);
        }
    );

    it.each(['held', 'let go', 'forgotten'])(
        'counts the end of a hold %s that the Mac says is over, its off lost: the next grant’s refusal is said',
        async (hold) => {
            await play('volant');
            await event('on', 'volant');
            mocks.session.manette = 'volant';
            await act(async () => root.render(createElement(Harness)));
            if (hold === 'let go') await act(async () => held().release());
            if (hold === 'forgotten') {
                mocks.fail = 1; // a renewal refused: the dashboard forgets the hold, the robot's lease runs out
                await tick(LEASE_MS);
            }
            mocks.session.manette = null; // the robot gave it back (a lease, a sleep…), its off lost in a reconnect
            await act(async () => root.render(createElement(Harness)));
            expect(held().granted).toBeNull();
            await play('volant'); // « Rejouer »
            await event('refused', null, 'scene');
            expect(held().granted).toBeNull();
            expect(held().error).toContain('Une scène se joue');
        }
    );
});
