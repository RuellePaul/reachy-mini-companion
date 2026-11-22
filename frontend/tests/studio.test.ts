// @vitest-environment happy-dom
// The studio's plumbing: the playback clock, the preview requests (asked once, asked again after a failure), and the
// measured pose it asks the Mac for only while it shows it.
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, describe, expect, it, vi} from 'vitest';
import type {PoseFrame, TryMove} from '../src/api/schemas';
import {preview, previewPath, useMirror} from '../src/studio/api';
import {Clock} from '../src/studio/clock';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const live = vi.hoisted(() => {
    const listeners = new Set<(frame: PoseFrame) => void>();
    const on = (_: string, listener: (frame: PoseFrame) => void) => {
        listeners.add(listener);
        return () => void listeners.delete(listener);
    };
    return {unwant: () => {}, want: (_: string) => () => {}, on, listeners};
});
vi.mock('../src/live', () => ({useLive: () => live}));

const nod: TryMove = {kind: 'gesture', name: 'nod', mode: null, tuning: {amplitude: 1, speed: 1}, seconds: 6};
const tuning = {amplitude: 1.2, speed: 0.9};
const curious: TryMove = {kind: 'animation', name: 'curious', mode: 'idle', tuning, seconds: 8};
const frames = {hz: 30, head: [[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]], antennas: [[0, 0]], body: [0]};
const pose: PoseFrame = {t: 1, j: [0, 0, 0, 0, 0, 0, 0], m: frames.head[0], a: [0, 0]};

describe('the clock', () => {
    it('plays, loops, pauses and seeks within the frames', () => {
        let ms = 0;
        const clock = new Clock(() => ms);
        clock.restart(2);
        ms = 1500;
        expect(clock.now()).toBeCloseTo(1.5);
        ms = 2500;
        expect(clock.now()).toBeCloseTo(0.5); // looped
        clock.toggle();
        ms = 9000;
        expect([clock.playing, clock.now()]).toEqual([false, 0.5]);
        clock.seek(5);
        expect(clock.now()).toBe(2);
        clock.seek(1);
        clock.toggle();
        ms = 9250;
        expect(clock.now()).toBeCloseTo(1.25);
    });
});

describe('previews', () => {
    afterEach(() => vi.unstubAllGlobals());

    it('ask the Mac with the move as query parameters', () => {
        expect(previewPath(nod)).toBe('/api/studio/preview?kind=gesture&name=nod');
        expect(previewPath(curious)).toBe(
            '/api/studio/preview?kind=animation&name=curious&mode=idle&amplitude=1.2&speed=0.9&seconds=8'
        );
    });

    it('are asked once per move, and again after a failure', async () => {
        const fetch = vi.fn().mockResolvedValueOnce(new Response('{"detail": "Le Mac a refusé."}', {status: 503}));
        fetch.mockImplementation(async () => new Response(JSON.stringify(frames)));
        vi.stubGlobal('fetch', fetch);
        await expect(preview(nod)).rejects.toThrow('Le Mac a refusé.');
        expect(await preview(nod)).toEqual(frames);
        expect(await preview(nod)).toEqual(frames);
        expect(fetch).toHaveBeenCalledTimes(2);
    });
});

describe('the mirror', () => {
    let root: Root | null = null;
    afterEach(() => {
        act(() => root?.unmount());
        vi.useRealTimers();
    });

    it('wants the pose channel only while active, and keeps the latest frame', () => {
        vi.useFakeTimers();
        const unwant = vi.fn();
        const want = vi.spyOn(live, 'want').mockReturnValue(unwant);
        const seen: {mirror?: ReturnType<typeof useMirror>} = {};
        const Probe = ({active}: {active: boolean}) => {
            seen.mirror = useMirror(active);
            return null;
        };
        root = createRoot(document.createElement('div'));
        act(() => root?.render(createElement(Probe, {active: false})));
        expect(want).not.toHaveBeenCalled();
        act(() => root?.render(createElement(Probe, {active: true})));
        expect(want).toHaveBeenCalledWith('pose');
        act(() => live.listeners.forEach((listener) => listener(pose)));
        act(() => vi.advanceTimersByTime(500));
        expect(seen.mirror?.pose()).toEqual(pose);
        expect(seen.mirror?.status).toBe('live');
        act(() => vi.advanceTimersByTime(2000));
        expect(seen.mirror?.status).toBe('stale'); // no frame for 2 s: the robot is greyed
        act(() => root?.render(createElement(Probe, {active: false})));
        expect(want).toHaveBeenCalledTimes(1);
        expect(unwant).toHaveBeenCalledTimes(1);
        expect(live.listeners.size).toBe(0);
    });
});
