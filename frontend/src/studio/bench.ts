// The 3D bench: one canvas, the robots side by side on a grid marked in centimetres, orbit controls and the camera
// presets. Plain three.js; the robot is robot.glb (its URDF tree), z up, facing +x.
import * as THREE from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {MeshoptDecoder} from 'three/addons/libs/meshopt_decoder.module.js';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {toCreasedNormals} from 'three/addons/utils/BufferGeometryUtils.js';
import {colour} from '../theme';
import {makeRig, type Rig} from './rig';

export type View = 'front' | 'side' | 'top' | 'free';
interface Robot {
    root: THREE.Object3D;
    rig: Rig;
}

const SPACING = 0.24; // m between robots compared side by side
const TARGET = new THREE.Vector3(0, 0, 0.19);
const DIRECTIONS = {front: [1, 0, 0.12], side: [0, -1, 0.12], top: [0.02, 0, 1]} as const;
const TINTS: Record<number, string> = {0xffffff: '#E9ECF2', 0: '#2D3748'}; // matte white shell, dark antennas

// Asked with the studio's chunk (prefetched once the dashboard is idle): opening the studio only parses it.
const fetchModel = () => fetch('/robot.glb').then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(r.status)));
let bytes: Promise<ArrayBuffer> | null = fetchModel();
bytes.catch(() => (bytes = null));
let model: Promise<THREE.Group> | null = null;

/** The robot, parsed once per page: smooth normals per shared geometry, matte materials. */
export const loadModel = (): Promise<THREE.Group> =>
    (model ??= (async () => {
        const data = await (bytes ?? fetchModel());
        await MeshoptDecoder.ready;
        const {scene} = await new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).parseAsync(data, '');
        const shared = new Map<object, THREE.BufferGeometry | THREE.Material>();
        const once = <T extends THREE.BufferGeometry | THREE.Material>(key: T, make: () => T): T =>
            (shared.get(key) ?? shared.set(key, make()).get(key)) as T;
        scene.traverse((o) => {
            if (!(o instanceof THREE.Mesh)) return;
            o.geometry = once(o.geometry, () => toCreasedNormals(o.geometry, Math.PI / 4.5));
            const {color, opacity} = o.material as THREE.MeshStandardMaterial;
            const tint = TINTS[color.getHex()] ?? color;
            o.material = once(
                o.material,
                () => new THREE.MeshStandardMaterial({color: tint, roughness: 0.82, opacity})
            );
            o.material.transparent = opacity < 1;
        });
        return scene;
    })().catch((err) => {
        model = null;
        throw err;
    }));

const shadowTexture = () => {
    const canvas = Object.assign(document.createElement('canvas'), {width: 64, height: 64});
    const g = canvas.getContext('2d') as CanvasRenderingContext2D;
    const gradient = g.createRadialGradient(32, 32, 4, 32, 32, 32);
    gradient.addColorStop(0, 'rgba(0,0,0,0.55)');
    gradient.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = gradient;
    g.fillRect(0, 0, 64, 64);
    return new THREE.CanvasTexture(canvas);
};

export class Bench {
    readonly robots: Robot[] = [];
    ghost: Robot | null = null;
    /** Called before every frame is drawn: pose the robots there. */
    onFrame = () => {};
    onOrbit = () => {};
    private readonly renderer: THREE.WebGLRenderer;
    private readonly scene = new THREE.Scene();
    private readonly camera = new THREE.PerspectiveCamera(30, 1, 0.01, 20);
    private readonly controls: OrbitControls;
    private readonly floor = new THREE.Group();
    private readonly ring = new THREE.Mesh(new THREE.RingGeometry(0.09, 0.1, 64), new THREE.MeshBasicMaterial());
    private readonly shadow = new THREE.MeshBasicMaterial({map: shadowTexture(), transparent: true, depthWrite: false});
    private readonly shadowShape = new THREE.PlaneGeometry(0.3, 0.3);
    // Pushed back in depth: where it matches the robot it hides, only the difference shows.
    private readonly ghostMaterial = new THREE.MeshStandardMaterial({
        transparent: true,
        opacity: 0.35,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: 4,
        polygonOffsetUnits: 4
    });
    // This bench's copies of the model's materials: a renderer keeps itself alive through every material it drew
    // until that material is disposed, so the page-lifetime originals must never be drawn.
    private readonly materials = new Map<THREE.Material, THREE.Material>();
    private readonly resize = new ResizeObserver(() => this.fit());
    private labels: HTMLElement[] = [];
    private view: View = 'free';
    private glide: THREE.Vector3 | null = null; // where the camera is going

    constructor(
        private readonly canvas: HTMLCanvasElement,
        private readonly model: THREE.Group
    ) {
        this.renderer = new THREE.WebGLRenderer({canvas, antialias: true, alpha: true});
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        this.camera.up.set(0, 0, 1);
        this.camera.position.set(0.75, -0.55, 0.42);
        this.controls = Object.assign(new OrbitControls(this.camera, canvas), {enableDamping: true, maxDistance: 6});
        this.controls.target.copy(TARGET);
        this.controls.addEventListener('start', () => {
            this.glide = null;
            this.onOrbit();
        });
        this.ring.visible = false;
        this.ring.position.z = 0.0008;
        const sun = new THREE.DirectionalLight('#ffffff', 2.4);
        sun.position.set(0.4, -0.3, 1.5);
        const fill = new THREE.DirectionalLight('#dfe6ff', 0.9);
        fill.position.set(1, 0.6, 0.3);
        this.scene.add(new THREE.HemisphereLight('#ffffff', '#1a2233', 1.3), sun, fill, this.floor);
        this.resize.observe(canvas);
        this.renderer.setAnimationLoop(() => {
            this.onFrame();
            if (this.glide && this.camera.position.lerp(this.glide, 0.18).distanceTo(this.glide) < 1e-4)
                this.glide = null;
            this.controls.update();
            this.place();
            this.renderer.render(this.scene, this.camera);
        });
    }

    /** `count` robots side by side, the first one with a ghost (the measured pose) if asked; in the theme's colours. */
    layout(count: number, ghost: boolean) {
        for (const r of [...this.robots.splice(0), ...(this.ghost ? [this.ghost] : [])]) this.scene.remove(r.root);
        this.clearFloor();
        for (let i = 0; i < count; i++) {
            const robot = this.add((i - (count - 1) / 2) * SPACING);
            const shadow = new THREE.Mesh(this.shadowShape, this.shadow);
            shadow.position.set(0, robot.root.position.y, 0.0004);
            this.floor.add(shadow);
            this.robots.push(robot);
        }
        this.ghost = ghost ? this.add(0) : null;
        this.ghost?.root.traverse((o) => o instanceof THREE.Mesh && Object.assign(o, {material: this.ghostMaterial}));
        const size = Math.max(0.6, count * SPACING + 0.3);
        const [minor, major] = [colour('grid'), colour('grid-major')];
        const fine = new THREE.GridHelper(size, Math.round(size * 100), minor, minor);
        const coarse = new THREE.GridHelper(size, Math.round(size * 20), major, major);
        (this.ring.material as THREE.MeshBasicMaterial).color.set(colour('speak'));
        this.ghostMaterial.color.set(colour('ghost'));
        for (const grid of [fine, coarse]) grid.rotation.x = Math.PI / 2;
        coarse.position.z = 0.0002;
        this.floor.add(fine, coarse, this.ring);
        this.fit();
    }

    /** Elements kept under each robot, in order. */
    label(elements: HTMLElement[]) {
        this.labels = elements;
    }

    pick(index: number | null) {
        this.ring.visible = index !== null && index >= 0 && this.robots.length > 1;
        this.ring.position.y = this.robots[index ?? 0]?.root.position.y ?? 0;
    }

    setView(view: View) {
        this.view = view;
        this.fit();
    }

    dispose() {
        this.renderer.setAnimationLoop(null);
        this.resize.disconnect();
        this.controls.dispose();
        this.clearFloor();
        for (const item of [this.ring.geometry, this.ring.material, this.shadow.map, this.shadow, this.shadowShape])
            item?.dispose();
        this.ghostMaterial.dispose();
        for (const material of this.materials.values()) material.dispose();
        this.renderer.dispose();
        this.renderer.forceContextLoss();
    }

    private clearFloor() {
        for (const child of this.floor.children) if (child instanceof THREE.GridHelper) child.dispose();
        this.floor.clear();
    }

    private add(y: number): Robot {
        const root = this.model.clone(true);
        root.position.y = y;
        const own = (m: THREE.Material) =>
            (this.materials.get(m) ?? this.materials.set(m, m.clone()).get(m)) as THREE.Material;
        root.traverse((o) => o instanceof THREE.Mesh && (o.material = own(o.material)));
        this.scene.add(root);
        return {root, rig: makeRig(root)};
    }

    /** The renderer follows the canvas's size; a preset view frames every robot. */
    private fit() {
        const {clientWidth: w, clientHeight: h} = this.canvas;
        if (!w || !h) return;
        this.renderer.setSize(w, h, false);
        this.camera.aspect = w / h;
        this.camera.updateProjectionMatrix();
        if (this.view === 'free') return;
        const tan = Math.tan((this.camera.fov * Math.PI) / 360);
        const halfWidth = Math.max(1, this.robots.length) * SPACING * 0.5 + 0.06;
        const distance = Math.max(0.3 / tan, halfWidth / (tan * this.camera.aspect)) * 1.05;
        this.controls.target.copy(TARGET);
        this.glide = new THREE.Vector3(...DIRECTIONS[this.view]).normalize().multiplyScalar(distance).add(TARGET);
    }

    /** Each label under its robot's feet. */
    private place() {
        const {clientWidth: w, clientHeight: h} = this.canvas;
        this.labels.forEach((label, i) => {
            const at = new THREE.Vector3(0.11, this.robots[i]?.root.position.y ?? 0, 0).project(this.camera);
            label.style.transform = `translate(-50%, 0) translate(${((at.x + 1) / 2) * w}px, ${((1 - at.y) / 2) * h}px)`;
        });
    }
}
