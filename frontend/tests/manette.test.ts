// The robot as an app's controller, on the dashboard's side (src/apps/pad.ts, src/apps/bridge.ts): its antennas as
// buttons with their hysteresis, its base and its head in `look`'s angles, and what a page may ask.
import {describe, expect, it} from 'vitest';
import type {PoseFrame} from '../src/api/schemas';
import {framed, manetteOf, motionOf, touchOf} from '../src/apps/bridge';
import {antennaRest, antennasHeld, ask, headAngles, held, lent, pad, PRESS_DEG, RELEASE_DEG} from '../src/apps/pad';

const NEUTRAL: [number, number] = [-0.1745, 0.1745]; // [right, left], as the daemon sends them
const rad = (degrees: number) => (degrees * Math.PI) / 180;
const identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
const frame = (right = 0, left = 0, body = 0, m = identity): PoseFrame => ({
    t: 0,
    j: [body, 0, 0, 0, 0, 0, 0],
    m,
    a: [NEUTRAL[0] + rad(right), NEUTRAL[1] - rad(left)] // forward is + on the right, − on the left
});
const rest = (read: ReturnType<typeof pad>) => {
    for (let n = 0; n < 8; n++) read(frame());
};

describe('the antennas as buttons', () => {
    it('measures their rest first, then presses past 4° and lets go under 1.5°', () => {
        const read = pad();
        expect(read(frame())).toBeNull(); // still measuring where they rest
        rest(read);
        expect(read(frame(0, PRESS_DEG - 0.5))?.left).toBe(false);
        expect(read(frame(0, PRESS_DEG + 0.5))).toMatchObject({left: true, right: false});
        expect(read(frame(0, RELEASE_DEG + 0.5))?.left).toBe(true); // between the two: still held
        expect(read(frame(0, RELEASE_DEG - 0.5))?.left).toBe(false);
        expect(read(frame(-6, 0))).toMatchObject({right: true, antennas: {right: -6, left: 0}}); // pulled back too
    });

    it('gives each antenna pushed forward as positive: the left one is mirrored', () => {
        const read = pad();
        rest(read);
        expect(read(frame(0, 6))).toMatchObject({left: true, right: false, antennas: {left: 6, right: 0}});
        expect(read(frame(0, -6))).toMatchObject({left: true, antennas: {left: -6, right: 0}});
    });
});

describe('the antennas lowered under the palm', () => {
    const LOWERED: [number, number] = [-3.05, 3.05]; // [right, left], where they lie in sleep (motion.SLEEP_ANTENNAS)
    const turn = (angle: number) => angle - 2 * Math.PI * Math.round(angle / (2 * Math.PI)); // as a joint tells it
    const lowered = (right = 0, left = 0): PoseFrame => ({
        ...frame(),
        a: [turn(LOWERED[0] + rad(right)), turn(LOWERED[1] - rad(left))]
    });
    const settle = (read: ReturnType<typeof pad>) => {
        for (let n = 0; n < 8; n++) read(lowered());
    };

    it('rest where « paume » holds them: nothing is pressed until a finger pushes', () => {
        expect(antennasHeld('paume')).toEqual(LOWERED);
        expect(antennasHeld('volant')).toEqual(NEUTRAL);
        const read = pad('paume');
        settle(read);
        expect(read(lowered())).toMatchObject({left: false, right: false, antennas: {left: 0, right: 0}});
        expect(read(lowered(PRESS_DEG + 1, 0))).toMatchObject({right: true, left: false}); // lifted back up a little
        const upright = pad(); // a body whose antennas are held upright takes lowered ones for pushed all the way
        settle(upright);
        expect(upright(lowered())).toMatchObject({left: true, right: true});
    });

    it('reads a push past the half turn as the few degrees it is, not as a turn', () => {
        const read = pad('paume');
        settle(read);
        const pushed = lowered(-9, -9); // each 9° further down: the right one past −π, the left one past π
        expect(pushed.a[0]).toBeGreaterThan(3); // told from the other side of the turn
        expect(pushed.a[1]).toBeLessThan(-3);
        expect(read(pushed)).toMatchObject({left: true, right: true, antennas: {left: -9, right: -9}});
        expect(read(lowered(-1, -1))).toMatchObject({left: false, right: false}); // let go under 1.5°
    });

    it('takes their measured rest when steady near it, across the half turn too', () => {
        const sagging: [number, number][] = Array.from({length: 8}, () => [LOWERED[0] - rad(1), LOWERED[1] + rad(1.5)]);
        const rest = antennaRest(sagging, LOWERED);
        expect(rest[0]).toBeCloseTo(LOWERED[0] - rad(1));
        expect(rest[1]).toBeCloseTo(LOWERED[1] + rad(1.5));
        const pressed: [number, number][] = Array.from({length: 8}, () => [turn(LOWERED[0] - rad(8)), LOWERED[1]]);
        expect(antennaRest(pressed, LOWERED)).toEqual(LOWERED); // a finger already on it: where it is held
    });
});

describe('the rest', () => {
    it('is where the antennas are held unless they rest steadily near it: someone pushing already is no rest', () => {
        const pushed: [number, number][] = Array.from({length: 8}, () => [NEUTRAL[0], NEUTRAL[1] - rad(7)]);
        expect(antennaRest(pushed)).toEqual(NEUTRAL); // the left one pushed 7° all along: its held position
        const settled: [number, number][] = Array.from({length: 8}, () => [NEUTRAL[0] + rad(1), NEUTRAL[1]]);
        expect(antennaRest(settled)[0]).toBeCloseTo(NEUTRAL[0] + rad(1)); // a P-only servo off its target by 1°
        const wobbling: [number, number][] = Array.from({length: 8}, (_, n) => [
            NEUTRAL[0] + rad(n % 2 ? 2 : 0),
            NEUTRAL[1]
        ]);
        expect(antennaRest(wobbling)).toEqual(NEUTRAL); // near it on average, but moving: a finger on it, no rest
    });

    it('counts the head and the base from where they rest: the sleep pose is the stick at its centre', () => {
        const down = (deg: number) => {
            const [c, s] = [Math.cos(rad(deg)), Math.sin(rad(deg))];
            return [c, 0, s, 0, 0, 1, 0, 0, -s, 0, c, 0, 0, 0, 0, 1];
        };
        const read = pad();
        for (let n = 0; n < 8; n++) read(frame(0, 0, 0, down(24))); // « tete » rests 24° down
        expect(read(frame(0, 0, 0, down(24)))?.head).toEqual({yaw: 0, pitch: 0, roll: 0, x: 0, y: 0, z: 0});
        expect(read(frame(0, 0, 0, down(14)))?.head.pitch).toBe(10); // lifted 10° by hand: up
    });

    it('counts the base, the yaw and the roll from where they rest too', () => {
        const turned = (yaw: number, roll: number) => {
            const [cy, sy, cr, sr] = [Math.cos(rad(yaw)), Math.sin(rad(yaw)), Math.cos(rad(roll)), Math.sin(rad(roll))];
            return [cy, -sy * cr, sy * sr, 0, sy, cy * cr, -cy * sr, 0, 0, sr, cr, 0, 0, 0, 0, 1]; // Rz(yaw) · Rx(roll)
        };
        const read = pad();
        for (let n = 0; n < 8; n++) read(frame(0, 0, rad(20), turned(35, 8))); // base 20°, head 15° off it, rolled 8°
        expect(read(frame(0, 0, rad(20), turned(35, 8)))).toMatchObject({body: 0, head: {yaw: 0, pitch: 0, roll: 0}});
        expect(read(frame(0, 0, rad(30), turned(55, 3)))).toMatchObject({
            body: 10,
            head: {yaw: 10, pitch: 0, roll: -5}
        });
    });
});

describe('the base and the head', () => {
    it('gives the base as look gives its yaw, and the head relative to the base', () => {
        const read = pad();
        rest(read);
        expect(read(frame(0, 0, rad(30)))?.body).toBe(30);
        const yawed = (deg: number) => {
            const [c, s] = [Math.cos(rad(deg)), Math.sin(rad(deg))];
            return [c, -s, 0, 0, s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
        };
        expect(headAngles(yawed(50), rad(30))).toMatchObject({yaw: 20, pitch: 0, roll: 0}); // 50° in the world, 20° off
        const lookingDown = [
            Math.cos(rad(10)),
            0,
            Math.sin(rad(10)),
            0,
            0,
            1,
            0,
            0,
            -Math.sin(rad(10)),
            0,
            Math.cos(rad(10)),
            0,
            0,
            0,
            0,
            1
        ];
        expect(headAngles(lookingDown, 0).pitch).toBe(-10); // a robot pitch down is look's pitch down
    });
});

describe('the human’s own gesture in the app', () => {
    const win = {} as Window;
    const message = (data: unknown, source: unknown = win) => ({data, source}) as MessageEvent;

    it('is told by the dashboard’s script in the frame, with the mount’s key: a page cannot say it for itself', () => {
        const srcdoc = framed('<meta http-equiv="Content-Security-Policy" content="x"><p>jeu</p>', 'k1');
        expect(srcdoc).toContain("addEventListener('pointerdown',t,true);addEventListener('keydown',t,true);");
        expect(srcdoc).toContain('e.isTrusted&&!e.repeat'); // a click the page makes up, a key held: neither
        expect(srcdoc.indexOf('isTrusted')).toBeLessThan(srcdoc.indexOf('<p>jeu</p>')); // heard before the page's own
        expect(touchOf(message({reachy: 1, type: 'touch', key: 'k1'}), win, 'k1')).toBe(true);
        expect(touchOf(message({reachy: 1, type: 'touch', key: 'k2'}), win, 'k1')).toBe(false); // not this mount's key
        expect(touchOf(message({reachy: 1, type: 'touch', key: 'k1'}, {}), win, 'k1')).toBe(false); // another window
        expect(touchOf(message(null), win, 'k1')).toBe(false);
    });

    it('lets a page ask the robot to play back what a hand did, between two times of its clock', () => {
        const ask = {reachy: 1, type: 'replay', start: 1794000001000, end: 1794000003500, loops: 4};
        expect(motionOf(message(ask), win, true)).toEqual({kind: 'replay', start: ask.start, end: ask.end, loops: 4});
        expect(motionOf(message({reachy: 1, type: 'replay', loops: 0}), win, true)).toMatchObject({loops: 0}); // stop
        expect(motionOf(message({...ask, loops: 9}), win, true)).toBeNull();
        expect(motionOf(message(ask), win, false)).toBeNull(); // its page not open
    });
});

describe('where a hand carries the head', () => {
    const carried = (x: number, y: number, z: number, m = identity) => {
        const pose = [...m];
        [pose[3], pose[7], pose[11]] = [x, y, z]; // metres, in the world
        return pose;
    };

    it('says how far from its rest, in mm: toward the person, toward the page’s right, up; and when, on the robot’s clock', () => {
        const read = pad();
        for (let n = 0; n < 8; n++) read(frame(0, 0, 0, carried(0, 0, 0.15))); // where the head rests
        const swayed = read({...frame(0, 0, 0, carried(0.005, 0.02, 0.14)), t: 1794000001234.5});
        expect(swayed?.head).toEqual({yaw: 0, pitch: 0, roll: 0, x: 5, y: 20, z: -10}); // swung to a shoulder: no angle
        expect(swayed?.t).toBe(1794000001234.5);
    });

    it('counts it from the base, as the angles: a base turned by hand carries the head with it', () => {
        const quarter = [0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]; // the head turned with its base
        const {x, y, yaw} = headAngles(carried(0, 0.02, 0, quarter), rad(90)); // 2 cm ahead of the base, as it faces
        expect([x, y, yaw]).toEqual([20, 0, 0]);
    });
});

describe('what a profile lends', () => {
    it('lends a body, the ear, or both: « voix » alone lends no body', () => {
        expect(lent('volant')).toEqual({body: 'volant', ear: false});
        expect(lent('voix')).toEqual({body: null, ear: true});
        expect(lent('tete+voix')).toEqual({body: 'tete', ear: true});
        expect(lent('paume+voix')).toEqual({body: 'paume', ear: true});
    });

    it('says so to the human, before and while it holds', () => {
        expect(ask('voix')).toContain('aucun son ne quitte le robot');
        expect(ask('volant+voix')).toBe(`${ask('volant')} Elle écoute aussi le niveau du son par son micro.`);
        expect(held('voix')).toContain('Reachy écoute');
        expect(held('gachettes+voix')).toBe(`${held('gachettes')} Il écoute aussi le niveau du son.`);
        expect(ask('paume')).toContain('ses antennes baissées sont des boutons sous tes doigts');
        expect(held('paume')).toBe('Sous ta paume : tourne la base de Reachy, tes doigts sur ses antennes.');
    });
});

describe('what a page may ask', () => {
    const frameWindow = {} as Window;
    const message = (data: unknown, source: unknown = frameWindow) => ({data, source}) as MessageEvent;
    it('takes a profile or null from the first document only, and nothing else', () => {
        expect(manetteOf(message({reachy: 1, type: 'manette', profile: 'volant'}), frameWindow, true)).toBe('volant');
        expect(manetteOf(message({reachy: 1, type: 'manette', profile: null}), frameWindow, true)).toBeNull();
        expect(
            manetteOf(message({reachy: 1, type: 'manette', profile: 'molettes'}), frameWindow, true)
        ).toBeUndefined();
        expect(manetteOf(message({reachy: 1, type: 'manette', profile: 'volant'}), frameWindow, false)).toBeUndefined();
        expect(manetteOf(message({reachy: 1, type: 'gesture', name: 'nod'}), frameWindow, true)).toBeUndefined();
        expect(
            manetteOf(message({reachy: 1, type: 'manette', profile: 'volant'}, {}), frameWindow, true)
        ).toBeUndefined();
    });
});
