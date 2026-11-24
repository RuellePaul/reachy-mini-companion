// The dashboard's surfaces besides the conversation, and what opens them: no router, one state in App.
import {useEffect} from 'react';
import type {TryMove} from './api/schemas';

/** `asked` numbers each request to show something (from `ask`): asked again, it shows again, even if the user had
 * moved on to something else in the meantime. */
export type Surface =
    | {kind: 'studio'; move?: TryMove}
    // playing: the robot plays it; buildId: a scene being written at someone's ask, shown as it is prepared
    | {kind: 'scenes'; sceneId?: string; buildId?: string; asked?: number; playing?: boolean}
    | {kind: 'apps'; appId?: string; asked?: number}
    | null;

let asks = 0;
export const ask = () => ++asks;

/** The props every surface takes: how it asks to close. */
export interface SurfaceProps {
    onClose: () => void;
}

const kept = new Set<symbol>();

/** Called by a surface with its unsaved work (a draft, a request typed): what the robot launches meanwhile is
 * offered, never shown over it. */
export const useKeep = (unsaved: boolean) =>
    useEffect(() => {
        if (!unsaved) return;
        const key = Symbol('unsaved');
        kept.add(key);
        return () => void kept.delete(key);
    }, [unsaved]);

/** Some surface holds unsaved work. */
export const keeping = () => kept.size > 0;
