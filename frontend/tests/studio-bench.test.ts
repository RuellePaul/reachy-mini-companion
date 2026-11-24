// @vitest-environment happy-dom
// The 3D bench's lifetime on the committed robot.glb, WebGL aside: once closed, nothing the page keeps (the model
// parsed once, its materials) holds on to its renderer.
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {Material, Mesh, Scene} from 'three';
import {expect, it, vi} from 'vitest';

// three.js r187's WebGLRenderer, as far as lifetimes go: the first time it draws a material it listens to that
// material's `dispose` with a closure over itself, and its own dispose() leaves those listeners in place.
const mocks = vi.hoisted(() => ({renderers: [] as {drawn: Set<Material>; loop: (() => void) | null}[]}));
vi.mock('three', async (importOriginal) => {
    const three = await importOriginal<typeof import('three')>();
    class Renderer {
        drawn = new Set<Material>();
        loop: (() => void) | null = null;
        constructor() {
            mocks.renderers.push(this);
        }
        forget = (event: {target: Material}) => {
            event.target.removeEventListener('dispose', this.forget);
            this.drawn.delete(event.target);
        };
        render(scene: Scene) {
            scene.traverse((object) => {
                const material = (object as Mesh).material as Material | undefined;
                if (!material || this.drawn.has(material)) return;
                material.addEventListener('dispose', this.forget);
                this.drawn.add(material);
            });
        }
        setAnimationLoop(loop: (() => void) | null) {
            this.loop = loop;
        }
        setPixelRatio() {}
        setSize() {}
        dispose() {}
        forceContextLoss() {}
    }
    return {...three, WebGLRenderer: Renderer};
});

it('lets a closed bench go: every material its renderer drew is disposed with it', async () => {
    const bytes = readFileSync(resolve(__dirname, '../public/robot.glb'));
    const body = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    vi.stubGlobal(
        'fetch',
        vi.fn(async () => ({ok: true, arrayBuffer: async () => body}))
    );
    const paint = {createRadialGradient: () => ({addColorStop: () => {}}), fillRect: () => {}}; // the floor's shadow
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(paint as never);
    const {Bench, loadModel} = await import('../src/studio/bench');
    const model = await loadModel();
    for (let opened = 0; opened < 2; opened++) {
        const canvas = document.body.appendChild(document.createElement('canvas'));
        const bench = new Bench(canvas, model);
        bench.layout(2, true);
        const renderer = mocks.renderers[opened];
        renderer.loop?.();
        expect(renderer.drawn.size).toBeGreaterThan(3); // the robots, the ghost, the floor
        bench.dispose();
        expect([...renderer.drawn].map((material) => material.type)).toEqual([]);
        canvas.remove();
    }
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});
