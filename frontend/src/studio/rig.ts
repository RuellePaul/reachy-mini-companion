// The robot's joints from a head pose, solved on robot.glb's own URDF tree: closed-form IK for the six Stewart
// motors (horn ball one rod length from its head anchor), then one rotation per rod and one for the head.
import {Matrix4, Quaternion, Vector3, type Object3D} from 'three';
import type {PoseFrame, PreviewFrames} from '../api/schemas';

const HEAD_Z = 0.177; // the SDK's head pose is the URDF `head` link lowered by reachy_mini's head_z_offset
export const NEUTRAL_ANTENNAS: [number, number] = [-0.1745, 0.1745];

interface Joint {
    node: Object3D;
    parent: Object3D;
    rest: Quaternion;
    axis: Vector3;
}

export interface Rig {
    /** A preview frame: head pose in the body's frame, antennas [right, left], body yaw. The motor angles, or null
     * when the pose is out of reach (the robot keeps its last one). */
    preview: (head: Matrix4, antennas: ArrayLike<number>, body: number) => number[] | null;
    /** A measured frame: the motors as measured, the rods and the head from the measured head pose. */
    measured: (frame: PoseFrame) => void;
}

const wrap = (x: number) => Math.atan2(Math.sin(x), Math.cos(x));

/** Reads the geometry while every joint is at rest: call it on a freshly loaded or cloned model. */
export const makeRig = (root: Object3D): Rig => {
    root.updateMatrixWorld(true);
    const node = (name: string) => {
        const found = root.getObjectByName(name);
        if (!found) throw new Error(`robot.glb: no ${name}`);
        return found;
    };
    const joint = (name: string): Joint => {
        const n = node(name);
        const axis = new Vector3(...(n.userData.axis as [number, number, number]));
        return {node: n, parent: n.parent ?? root, rest: n.quaternion.clone(), axis};
    };
    const at = (name: string) => new Vector3().setFromMatrixPosition(node(name).matrixWorld);
    const inverse = (o: Object3D) => o.matrixWorld.clone().invert();
    const headInv = inverse(node('head'));
    const legs = [1, 2, 3, 4, 5, 6].map((i) => {
        const motor = joint(`stewart_${i}`);
        const rod = joint(`passive_${i}_x`);
        const end = at(i < 6 ? `closing_${i}_1` : 'passive_7_x'); // the rod's far end
        return {
            motor,
            rod,
            ball: at(rod.node.name).applyMatrix4(inverse(motor.node)), // in the motor's frame at angle 0
            length: end.distanceTo(at(rod.node.name)),
            anchor: at(i < 6 ? `closing_${i}_2` : 'passive_7_x').applyMatrix4(headInv), // on the head, its frame
            along: end.clone().applyMatrix4(inverse(rod.node)).normalize() // the rod at rest, in its own frame
        };
    });
    const neck = joint('passive_7_x');
    const neckToHead = headInv.clone().multiply(neck.node.matrixWorld);
    const body = joint('yaw_body');
    const antennas = [joint('right_antenna'), joint('left_antenna')];

    const q = new Quaternion();
    const M = new Matrix4(); // the URDF head link, world frame
    const Z = new Matrix4();
    const T = new Matrix4();
    const t = new Vector3();
    let angles = [0.6265, -0.6265, 0.6265, -0.6265, 0.6265, -0.6265]; // the last solution decides between two
    const set = (j: Joint, value: number) => j.node.quaternion.copy(j.rest).multiply(q.setFromAxisAngle(j.axis, value));
    // The joint's world matrix at angle 0, inverted (its parent's world matrix must be current).
    const zeroInv = (j: Joint) =>
        Z.multiplyMatrices(j.parent.matrixWorld, T.compose(j.node.position, j.rest, j.node.scale)).invert();
    const setAntennas = (a: ArrayLike<number>) => antennas.forEach((j, k) => set(j, -a[k])); // joint = -position

    const ik = (): number[] | null => {
        const out: number[] = [];
        for (const {ball: b, length, anchor} of legs) {
            const k = out.length;
            t.copy(anchor).applyMatrix4(M).applyMatrix4(zeroInv(legs[k].motor));
            // |t - Rz(θ) b| = length  <=>  A cos θ + B sin θ = K
            const K = (t.lengthSq() + b.lengthSq() - length * length) / 2 - t.z * b.z;
            const A = t.x * b.x + t.y * b.y;
            const B = t.y * b.x - t.x * b.y;
            const R = Math.hypot(A, B);
            if (Math.abs(K) > R) return null;
            const base = Math.atan2(B, A);
            const [r1, r2] = [wrap(base + Math.acos(K / R)), wrap(base - Math.acos(K / R))];
            out.push(Math.abs(wrap(r1 - angles[k])) <= Math.abs(wrap(r2 - angles[k])) ? r1 : r2);
        }
        return out;
    };

    const rods = () => {
        root.updateMatrixWorld(true);
        for (const {rod, anchor, along} of legs) {
            t.copy(anchor).applyMatrix4(M).applyMatrix4(zeroInv(rod)).normalize();
            rod.node.quaternion.copy(rod.rest).multiply(q.setFromUnitVectors(along, t));
            rod.node.updateMatrixWorld(true);
        }
        T.copy(zeroInv(neck)).multiply(M).multiply(neckToHead);
        neck.node.quaternion.copy(neck.rest).multiply(q.setFromRotationMatrix(T));
    };

    return {
        preview: (head, a, yaw) => {
            set(body, yaw);
            M.makeRotationZ(yaw).multiply(head);
            M.elements[14] += HEAD_Z;
            root.updateMatrixWorld(true);
            const solved = ik();
            if (!solved) return null;
            angles = solved;
            legs.forEach((leg, k) => set(leg.motor, solved[k]));
            setAntennas(a);
            rods();
            return solved;
        },
        measured: (frame) => {
            set(body, frame.j[0]);
            legs.forEach((leg, k) => set(leg.motor, frame.j[k + 1]));
            setAntennas(frame.a);
            M.fromArray(frame.m).transpose();
            M.elements[14] += HEAD_Z;
            rods();
        }
    };
};

const pa = new Vector3();
const pb = new Vector3();
const qa = new Quaternion();
const qb = new Quaternion();
const unit = new Vector3();
const B = new Matrix4();

/** The preview at `time` seconds, between its two nearest frames (the head slerped): into `head`, body frame. */
export const frameAt = (frames: PreviewFrames, time: number, head: Matrix4) => {
    const last = frames.head.length - 1;
    const x = Math.min(Math.max(time * frames.hz, 0), last);
    const i = Math.floor(x);
    const j = Math.min(i + 1, last);
    const f = x - i;
    head.fromArray(frames.head[i]).transpose().decompose(pa, qa, unit);
    B.fromArray(frames.head[j]).transpose().decompose(pb, qb, unit);
    head.compose(pa.lerp(pb, f), qa.slerp(qb, f), unit.set(1, 1, 1));
    const mix = (a: number, b: number) => a + (b - a) * f;
    const [ai, aj] = [frames.antennas[i], frames.antennas[j]];
    return {antennas: [mix(ai[0], aj[0]), mix(ai[1], aj[1])], body: mix(frames.body[i], frames.body[j])};
};
