// @vitest-environment happy-dom
// The studio's stage on the committed robot.glb in a browser that gives no WebGL context: it says so, in place of
// the spinner.
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {act, createElement} from 'react';
import {createRoot} from 'react-dom/client';
import {expect, it, vi} from 'vitest';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

// three.js r187's WebGLRenderer when the browser refuses it a context (no GPU, acceleration off, too many contexts).
vi.mock('three', async (importOriginal) => ({
    ...(await importOriginal<typeof import('three')>()),
    WebGLRenderer: class {
        constructor() {
            throw new Error('Error creating WebGL context.');
        }
    }
}));

it('says the robot cannot be shown in 3D when the browser gives no WebGL context', async () => {
    const bytes = readFileSync(resolve(__dirname, '../public/robot.glb'));
    const body = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    vi.stubGlobal(
        'fetch',
        vi.fn(async () => ({ok: true, arrayBuffer: async () => body}))
    );
    const paint = {createRadialGradient: () => ({addColorStop: () => {}}), fillRect: () => {}}; // the floor's shadow
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(paint as never);
    const {Stage} = await import('../src/studio/RobotView');
    const {loadModel} = await import('../src/studio/bench');
    const root = createRoot(document.body.appendChild(document.createElement('div')));
    await act(async () =>
        root.render(createElement(Stage, {robots: [null], time: () => 0, pose: () => null, view: 'front'}))
    );
    await act(async () => void (await loadModel())); // parsed: the bench is built, and fails, right after
    expect(document.querySelector('[role="progressbar"]')).toBeNull();
    expect(document.body.textContent).toContain('Le robot en 3D n’a pas pu s’afficher.');
    act(() => root.unmount());
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});
