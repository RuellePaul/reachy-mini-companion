// The studio's own calls to the Mac, and the measured pose it subscribes to.
import {useEffect, useRef, useState} from 'react';
import {errorText, read} from '../api/client';
import {previewFramesSchema, type PoseFrame, type PreviewFrames, type TryMove} from '../api/schemas';
import {useLive} from '../live';

const STALE_MS = 1500;
const previews = new Map<string, Promise<PreviewFrames>>();

export const previewPath = (move: TryMove): string => {
    const query = new URLSearchParams({kind: move.kind, name: move.name});
    if (move.kind === 'animation') {
        query.set('mode', move.mode ?? '');
        query.set('amplitude', String(move.tuning.amplitude));
        query.set('speed', String(move.tuning.speed));
        query.set('seconds', String(move.seconds));
    }
    return `/api/studio/preview?${query}`;
};

/** A move as the robot would play it (GET /api/studio/preview), asked once per page; a failure is asked again. */
export const preview = (move: TryMove): Promise<PreviewFrames> => {
    const path = previewPath(move);
    const known = previews.get(path);
    if (known) return known;
    const asked = read(previewFramesSchema, 'GET', path);
    previews.set(path, asked);
    asked.catch(() => previews.delete(path));
    return asked;
};

/** The previews of these moves, all or none; the last ones stay shown while the next are asked. */
export const usePreviews = (moves: TryMove[]) => {
    const key = JSON.stringify(moves);
    const [got, setGot] = useState({key: '[]', frames: [] as PreviewFrames[], error: null as string | null});
    useEffect(() => {
        let alive = true;
        Promise.all((JSON.parse(key) as TryMove[]).map(preview)).then(
            (frames) => alive && setGot({key, frames, error: null}),
            (err: unknown) => alive && setGot({key, frames: [], error: errorText(err)})
        );
        return () => {
            alive = false;
        };
    }, [key]);
    return {...got, busy: got.key !== key};
};

export type PoseStatus = 'waiting' | 'live' | 'stale';

/** The robot's measured pose while `active` (the Mac relays its daemon only while some studio wants it). */
export const useMirror = (active: boolean) => {
    const {want, on} = useLive();
    const latest = useRef<{frame: PoseFrame; at: number} | null>(null);
    const [status, setStatus] = useState<PoseStatus>('waiting');
    useEffect(() => {
        if (!active) return;
        const unwant = want('pose');
        const off = on('pose', (frame) => {
            latest.current = {frame, at: performance.now()};
        });
        const timer = setInterval(() => {
            const last = latest.current;
            setStatus(!last ? 'waiting' : performance.now() - last.at > STALE_MS ? 'stale' : 'live');
        }, 400);
        return () => {
            unwant();
            off();
            clearInterval(timer);
        };
    }, [active, want, on]);
    return {pose: () => latest.current?.frame ?? null, status};
};
