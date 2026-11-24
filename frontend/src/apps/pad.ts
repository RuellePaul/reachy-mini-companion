// The robot as an app's controller, on the dashboard's side: from its measured pose (the `pose` channel, ~30 frames
// a second), what the app is told each frame. Its antennas are buttons (pushed past 4°, let go under 1.5°, from where
// they rest once the robot holds still: upright, or lowered as in sleep for « paume »), its base a wheel, its head a
// joystick: in the angles of the bridge's `look`, and where a hand carries it (mm). Each pad says when it was measured, on the robot's clock: what a `replay` is cut by.
// A profile that listens (« voix ») adds its microphone's level (the `voice` channel); alone, it lends no body.
// No React here: the tests drive it with frames of their own.
import type {ManetteProfile, PoseFrame} from '../api/schemas';
import {wordsNow} from '../i18n';
import {APPS} from './words';

export const PRESS_DEG = 4;
export const RELEASE_DEG = 1.5;
const REST_FRAMES = 8; // averaged for the rest, once the robot is still (~0.3 s)
const NEUTRAL: [number, number] = [-0.1745, 0.1745]; // where the antennas are held, [right, left] (animations.NEUTRAL)
const LOWERED: [number, number] = [-3.05, 3.05]; // « paume »: where they lie in sleep (motion.SLEEP_ANTENNAS)
const REST_NEAR = (2 * Math.PI) / 180; // a measured rest this close to it is taken; else someone pushes already
const REST_STILL = (0.5 * Math.PI) / 180; // and the frames that measured it this steady

/** What the app gets in each `pad` message, with `voice`, the ear's latest level, added by the hold (Manette.tsx).
 *  Left and right are the robot's own; angles in degrees. */
export interface Pad {
    left: boolean; // an antenna pushed by hand: a button held
    right: boolean;
    // How far each is pushed (positive: forward; a lowered one, « paume », back up toward upright, and negative
    // further down).
    antennas: {left: number; right: number};
    body: number; // the base from where it rested, as `look`'s yaw: positive toward the page's right
    // Relative to the base, from its rest. The angles as `look` takes them (pitch up); x, y, z in mm: toward the
    // person facing it, toward the page's right, up.
    head: {yaw: number; pitch: number; roll: number; x: number; y: number; z: number};
    t: number; // when it was measured, ms on the robot's clock (0 for a profile that lends no body)
}

export type Body = 'gachettes' | 'volant' | 'paume' | 'tete';

/** What a profile lends: a body (none for « voix » alone), and the ear. */
export const lent = (profile: ManetteProfile): {body: Body | null; ear: boolean} => {
    const [first, second] = profile.split('+');
    return {body: first === 'voix' ? null : (first as Body), ear: first === 'voix' || second === 'voix'};
};

/** The pad of a profile that lends no body: nothing pushed, nothing turned. */
export const STILL: Pad = {
    left: false,
    right: false,
    antennas: {left: 0, right: 0},
    body: 0,
    head: {yaw: 0, pitch: 0, roll: 0, x: 0, y: 0, z: 0},
    t: 0
};

const deg = (rad: number) => (rad * 180) / Math.PI;
const round = (value: number) => Math.round(value * 10) / 10 || 0; // never -0

/** The head relative to the base, from the head pose (4x4, row-major, world frame, metres) and the base yaw. */
export const headAngles = (m: number[], body: number): Pad['head'] => {
    const [c, s] = [Math.cos(-body), Math.sin(-body)];
    const r = (i: number, j: number) => m[i * 4 + j];
    // R_rel = Rz(-body) · R, its first column and last row: enough for yaw, pitch and roll (extrinsic xyz).
    const r00 = c * r(0, 0) - s * r(1, 0);
    const r10 = s * r(0, 0) + c * r(1, 0);
    const yaw = Math.atan2(r10, r00);
    const pitch = Math.asin(Math.max(-1, Math.min(1, -r(2, 0))));
    const roll = Math.atan2(r(2, 1), r(2, 2));
    const [x, y] = [c * r(0, 3) - s * r(1, 3), s * r(0, 3) + c * r(1, 3)];
    return {
        yaw: round(deg(yaw)),
        pitch: round(-deg(pitch)),
        roll: round(deg(roll)),
        x: round(1000 * x),
        y: round(1000 * y),
        z: round(1000 * r(2, 3))
    };
};

/** An angle within ±π: a lowered antenna pushed past the half turn is told from the other side of it. */
const wrap = (rad: number) => rad - 2 * Math.PI * Math.round(rad / (2 * Math.PI));

/** Where a body's antennas are held, [right, left]: upright, or lowered as in sleep for « paume ». */
export const antennasHeld = (body: Body): [number, number] => (body === 'paume' ? LOWERED : NEUTRAL);

/** Where the antennas rest: the frames' average if steady and near where they are held, else where they are held. */
export const antennaRest = (seen: [number, number][], held: [number, number] = NEUTRAL): [number, number] =>
    held.map((target, k) => {
        const off = seen.map((a) => wrap(a[k] - target));
        const mean = off.reduce((sum, a) => sum + a, 0) / off.length;
        const steady = Math.max(...off) - Math.min(...off) <= REST_STILL;
        return steady && Math.abs(mean) <= REST_NEAR ? target + mean : target;
    }) as [number, number];

/** A pad that reads the frames of a body held: `read(frame)` gives the pad, or null while the rest is still being
 *  measured. */
export const pad = (body: Body = 'gachettes') => {
    const seen: PoseFrame[] = [];
    let rest: {antennas: [number, number]; body: number; head: Pad['head']} | null = null;
    let held = {left: false, right: false};
    return (frame: PoseFrame): Pad | null => {
        if (!rest) {
            seen.push(frame);
            if (seen.length < REST_FRAMES) return null;
            const last = seen[seen.length - 1];
            rest = {
                antennas: antennaRest(
                    seen.map((f) => [f.a[0], f.a[1]]),
                    antennasHeld(body)
                ),
                body: deg(last.j[0]),
                head: headAngles(last.m, last.j[0]) // « tete » rests in the sleep pose, 24° down: the stick's centre
            };
        }
        // Antennas come as [right, left], mirrored: forward is + for the right, − for the left (animations.antennas).
        const right = deg(wrap(frame.a[0] - rest.antennas[0]));
        const left = -deg(wrap(frame.a[1] - rest.antennas[1]));
        const pressed = (push: number, was: boolean) => Math.abs(push) > (was ? RELEASE_DEG : PRESS_DEG);
        held = {left: pressed(left, held.left), right: pressed(right, held.right)};
        const head = headAngles(frame.m, frame.j[0]);
        return {
            ...held,
            antennas: {left: round(left), right: round(right)},
            body: round(deg(frame.j[0]) - rest.body),
            head: {
                yaw: round(head.yaw - rest.head.yaw),
                pitch: round(head.pitch - rest.head.pitch),
                roll: round(head.roll - rest.head.roll),
                x: round(head.x - rest.head.x),
                y: round(head.y - rest.head.y),
                z: round(head.z - rest.head.z)
            },
            t: frame.t
        };
    };
};

/** What the human is asked before the robot becomes this controller, in the language shown. */
export const ask = (profile: ManetteProfile): string => {
    const t = wordsNow(APPS);
    const {body, ear} = lent(profile);
    if (!body) return t.asksEar;
    return ear ? `${t.asks[body]} ${t.asksEarToo}` : t.asks[body];
};

/** The controller in force, in a few words, in the language shown. */
export const held = (profile: ManetteProfile): string => {
    const t = wordsNow(APPS);
    const {body, ear} = lent(profile);
    if (!body) return t.heldEar;
    return ear ? `${t.held[body]} ${t.heldEarToo}` : t.held[body];
};
