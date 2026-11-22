// What the robot launches by voice that has a surface here (an app asked for, a scene it plays), and how the
// dashboard shows it: at once in its surface, or offered beside unsaved work, which it never replaces.
import type {AppEvent, RobotEvent, SceneBuild} from './api/schemas';
import {wordsNow} from './i18n';
import type {Showing} from './link';
import {ask, type Surface} from './surfaces';
import {FRAME} from './words';

type Shown = Pick<Showing, 'kind' | 'id'> | null;

/** Something launched, offered rather than shown: « Voir » shows it; `stop`, a scene playing, « Arrêter » too. */
export interface Offer {
    text: string;
    surface: Surface;
    stop?: boolean;
}

export type Launch = {show: Surface} | {offer: Offer} | null;

const showOr = (surface: Surface, text: string, unsaved: boolean, stop = false): Launch =>
    unsaved ? {offer: {text, surface, stop}} : {show: surface};

/** An `apps` event: an app asked for by voice shows while it is built (`voiced` remembers it); once built, if it is
 * not on screen any more, it is offered. */
export const appLaunch = (
    {app, open, deleted}: AppEvent,
    shown: Shown,
    voiced: Set<string>,
    unsaved: boolean
): Launch => {
    const there = shown?.kind === 'app' && shown.id === app.id;
    const surface: Surface = {kind: 'apps', appId: app.id, asked: ask()};
    if (deleted) voiced.delete(app.id);
    if (open) {
        if (app.status === 'building') voiced.add(app.id);
        return there ? null : showOr(surface, wordsNow(FRAME).opens(app.title), unsaved);
    }
    if (deleted || app.status === 'building' || !voiced.delete(app.id) || there) return null;
    return {offer: {text: wordsNow(FRAME).built(app.title, app.status === 'ready' && !app.error), surface}};
};

/** A `scene_build` frame: a scene asked for by voice shows as it is prepared, once (`seen` remembers it); one asked
 * for in the dashboard is already where it was asked. */
export const writeLaunch = (build: SceneBuild, seen: Set<string>, unsaved: boolean): Launch => {
    if (!build.voiced || build.status !== 'building' || seen.has(build.id)) return null;
    seen.add(build.id);
    return showOr({kind: 'scenes', buildId: build.id, asked: ask()}, wordsNow(FRAME).writes, unsaved);
};

/** A conversation event: a scene the robot starts shows in its editor, playing, unless it is there already. */
export const sceneLaunch = (event: RobotEvent, shown: Shown, unsaved: boolean): Launch => {
    if (event.kind !== 'scene' || event.state !== 'started') return null;
    if (shown?.kind === 'scene' && shown.id === event.scene_id) return null; // played from its editor
    const surface: Surface = {kind: 'scenes', sceneId: event.scene_id, asked: ask(), playing: true};
    return showOr(surface, wordsNow(FRAME).plays(event.title), unsaved, true);
};

/** Whether this event ends the scene an offer is about: the offer goes with it. */
export const sceneOver = (offer: Offer | null, event: RobotEvent): boolean =>
    event.kind === 'scene' &&
    ['ended', 'stopped', 'failed'].includes(event.state) &&
    offer?.surface?.kind === 'scenes' &&
    offer.surface.sceneId === event.scene_id;
