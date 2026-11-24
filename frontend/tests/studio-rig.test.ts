// The studio's kinematics on the committed robot.glb, against the SDK: studio-vectors.json holds head poses with the
// motor angles reachy_mini's own IK gives (and one frame measured on the robot).
import {readFileSync} from 'node:fs';
import {Matrix4, MathUtils, Quaternion, Vector3, type Object3D} from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {MeshoptDecoder} from 'three/addons/libs/meshopt_decoder.module.js';
import {beforeAll, describe, expect, it} from 'vitest';
import vectors from './studio-vectors.json';
import type {PreviewFrames} from '../src/api/schemas';
import {frameAt, makeRig, NEUTRAL_ANTENNAS} from '../src/studio/rig';

let robot: Object3D;

beforeAll(async () => {
    const bytes = readFileSync(new URL('../public/robot.glb', import.meta.url));
    await MeshoptDecoder.ready;
    const gltf = await new GLTFLoader()
        .setMeshoptDecoder(MeshoptDecoder)
        .parseAsync(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength), '');
    robot = gltf.scene;
});

const world = (root: Object3D, name: string) =>
    (root.getObjectByName(name) as Object3D).getWorldPosition(new Vector3());

/** How far the URDF head link lands from the SDK's head pose (mm, degrees), once the rods are placed. */
const headError = (root: Object3D, pose: number[]) => {
    root.updateMatrixWorld(true);
    const target = new Matrix4().fromArray(pose).transpose();
    target.elements[14] += 0.177;
    const head = (root.getObjectByName('head') as Object3D).matrixWorld;
    const mm = new Vector3().setFromMatrixPosition(head).distanceTo(new Vector3().setFromMatrixPosition(target)) * 1000;
    const angle = new Quaternion().setFromRotationMatrix(head).angleTo(new Quaternion().setFromRotationMatrix(target));
    return {mm, degrees: MathUtils.radToDeg(angle)};
};

const closing = (root: Object3D) =>
    [1, 2, 3, 4, 5].map((i) => world(root, `closing_${i}_1`).distanceTo(world(root, `closing_${i}_2`)) * 1000);

describe('the studio rig', () => {
    it('solves the motors as the SDK does, to a thousandth of a degree', () => {
        const rig = makeRig(robot.clone(true));
        for (const v of vectors.filter((v) => v.name !== 'measured_sleep')) {
            const yaw = v.head_joints[0];
            const inBody = new Matrix4().makeRotationZ(-yaw).multiply(new Matrix4().fromArray(v.head_pose).transpose());
            const angles = rig.preview(inBody, NEUTRAL_ANTENNAS, yaw);
            expect(angles, v.name).not.toBeNull();
            const errors = (angles ?? []).map((a, i) => MathUtils.radToDeg(Math.abs(a - v.head_joints[i + 1])));
            expect(Math.max(...errors), v.name).toBeLessThan(0.001);
        }
    });

    it('closes every rod on the head, previewed or measured', () => {
        const copy = robot.clone(true);
        const rig = makeRig(copy);
        for (const v of vectors) {
            rig.measured({t: 0, j: v.head_joints, m: v.head_pose, a: NEUTRAL_ANTENNAS});
            const tolerance = v.name === 'measured_sleep' ? 0.05 : 0.005; // the daemon's own FK/joint mismatch
            expect(Math.max(...closing(copy)), v.name).toBeLessThan(tolerance);
            const error = headError(copy, v.head_pose);
            expect(error.mm, v.name).toBeLessThan(tolerance * 2);
            expect(error.degrees, v.name).toBeLessThan(0.01);
        }
        const yawed = vectors.find((v) => v.name === 'yaw30_body40') as (typeof vectors)[number];
        const inBody = new Matrix4()
            .makeRotationZ(-0.6981317)
            .multiply(new Matrix4().fromArray(yawed.head_pose).transpose());
        rig.preview(inBody, NEUTRAL_ANTENNAS, 0.6981317);
        expect(Math.max(...closing(copy))).toBeLessThan(0.005);
        expect(headError(copy, yawed.head_pose).mm).toBeLessThan(0.01);
    });

    it('refuses a pose out of reach', () => {
        const rig = makeRig(robot.clone(true));
        expect(rig.preview(new Matrix4().makeTranslation(0, 0, 0.2), NEUTRAL_ANTENNAS, 0)).toBeNull();
    });

    it('turns the antenna joints opposite to their positions, as the SDK does', () => {
        const copy = robot.clone(true);
        const node = (name: string) => copy.getObjectByName(name) as Object3D;
        const rest = new Map(['right_antenna', 'left_antenna'].map((n) => [n, node(n).quaternion.clone()]));
        makeRig(copy).preview(new Matrix4(), [0.5, -0.25], 0);
        const angle = (name: string) => {
            const q = (rest.get(name) as Quaternion).clone().invert().multiply(node(name).quaternion);
            const axis = new Vector3(...(node(name).userData.axis as [number, number, number]));
            return 2 * Math.atan2(new Vector3(q.x, q.y, q.z).dot(axis), q.w);
        };
        expect(angle('right_antenna')).toBeCloseTo(-0.5, 5);
        expect(angle('left_antenna')).toBeCloseTo(0.25, 5);
    });
});

describe('frameAt', () => {
    const pitch = (degrees: number) => new Matrix4().makeRotationY(MathUtils.degToRad(degrees)).transpose().toArray();
    const frames: PreviewFrames = {
        hz: 10,
        head: [pitch(0), pitch(20)],
        antennas: [
            [0, 1],
            [1, 0]
        ],
        body: [0, 1]
    };

    it('slerps the head and lerps the rest between two frames, held at the ends', () => {
        const head = new Matrix4();
        const half = frameAt(frames, 0.05, head);
        expect(half).toEqual({antennas: [0.5, 0.5], body: 0.5});
        const expected = new Quaternion().setFromAxisAngle(new Vector3(0, 1, 0), MathUtils.degToRad(10));
        expect(new Quaternion().setFromRotationMatrix(head).angleTo(expected)).toBeLessThan(1e-6);
        expect(frameAt(frames, 3, head).body).toBe(1);
        expect(frameAt(frames, -1, head).body).toBe(0);
    });
});
