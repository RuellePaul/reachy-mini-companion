// What the robot launches by voice: shown at once in its surface, or offered beside unsaved work (src/launch.ts).
import {describe, expect, it} from 'vitest';
import examples from './contract-examples.json';
import {sceneBuildSchema, type AppSummary, type RobotEvent} from '../src/api/schemas';
import {appLaunch, sceneLaunch, sceneOver, writeLaunch} from '../src/launch';

const app = (status: AppSummary['status'], error: string | null = null): AppSummary => ({
    id: 'a1',
    title: 'Snake',
    status,
    created_at: 1,
    updated_at: 2,
    error,
    quiet: true,
    builtin: false,
    edited: false
});
const scene = (state: Extract<RobotEvent, {kind: 'scene'}>['state'], id = 'antigone'): RobotEvent => ({
    kind: 'scene',
    session_id: 's',
    scene_id: id,
    title: 'Antigone',
    state,
    line: null,
    message: null
});

describe('an app asked for by voice', () => {
    it('shows while it is built, then is only offered once built if the user moved on', () => {
        const voiced = new Set<string>();
        const opened = appLaunch({app: app('building'), open: true, deleted: false}, null, voiced, false);
        expect(opened).toMatchObject({show: {kind: 'apps', appId: 'a1'}});
        expect(appLaunch({app: app('ready'), open: false, deleted: false}, null, voiced, false)).toMatchObject({
            offer: {text: '« Snake » est prête.', surface: {kind: 'apps', appId: 'a1'}}
        });
        expect(appLaunch({app: app('ready'), open: false, deleted: false}, null, voiced, false)).toBeNull(); // once
    });

    it('is not offered once built while it is on screen, nor when the dashboard built it', () => {
        const voiced = new Set<string>();
        appLaunch({app: app('building'), open: true, deleted: false}, null, voiced, false);
        const onScreen = {kind: 'app', id: 'a1'} as const;
        expect(appLaunch({app: app('ready'), open: false, deleted: false}, onScreen, voiced, false)).toBeNull();
        expect(appLaunch({app: app('ready'), open: false, deleted: false}, null, new Set(), false)).toBeNull();
    });

    it('says a failed build or update, and never replaces unsaved work', () => {
        const voiced = new Set<string>();
        const kept = appLaunch({app: app('building'), open: true, deleted: false}, null, voiced, true);
        expect(kept).toMatchObject({offer: {text: 'Reachy ouvre « Snake ».', surface: {kind: 'apps', appId: 'a1'}}});
        const failed = appLaunch(
            {app: app('ready', 'Claude a échoué.'), open: false, deleted: false},
            null,
            voiced,
            true
        );
        expect(failed).toMatchObject({offer: {text: "« Snake » n'a pas abouti."}});
    });
});

describe('a scene the robot plays', () => {
    it('opens on it, playing, unless it is on screen already', () => {
        expect(sceneLaunch(scene('started'), null, false)).toMatchObject({
            show: {kind: 'scenes', sceneId: 'antigone', playing: true}
        });
        expect(sceneLaunch(scene('started'), {kind: 'scene', id: 'antigone'}, false)).toBeNull();
        expect(sceneLaunch(scene('line'), null, false)).toBeNull();
    });

    it('is offered with « Arrêter » beside a draft not saved', () => {
        expect(sceneLaunch(scene('started'), {kind: 'scene', id: 'other'}, true)).toMatchObject({
            offer: {text: 'Reachy joue « Antigone ».', stop: true, surface: {kind: 'scenes', sceneId: 'antigone'}}
        });
    });
});

describe('what the review of 2026-11-03 found', () => {
    it('opens nothing for an app already on screen, and forgets an app deleted mid-build', () => {
        const voiced = new Set<string>();
        const onScreen = {kind: 'app', id: 'a1'} as const;
        expect(appLaunch({app: app('building'), open: true, deleted: false}, onScreen, voiced, true)).toBeNull();
        appLaunch({app: app('building'), open: false, deleted: true}, null, voiced, false);
        expect(voiced.size).toBe(0);
        expect(appLaunch({app: app('ready'), open: false, deleted: false}, null, voiced, false)).toBeNull();
    });

    it('drops the offer of a scene once it is over', () => {
        const offer = sceneLaunch(scene('started'), null, true);
        const shown = offer && 'offer' in offer ? offer.offer : null;
        for (const over of ['ended', 'stopped', 'failed'] as const)
            expect(sceneOver(shown, scene(over)), over).toBe(true);
        for (const going of ['started', 'line', 'waiting'] as const)
            expect(sceneOver(shown, scene(going)), going).toBe(false);
        expect(sceneOver(shown, scene('ended', 'other'))).toBe(false);
    });
});

describe('a scene asked for by voice', () => {
    const writing = sceneBuildSchema.parse(examples.SceneBuild[0]); // voiced, being written
    const ready = sceneBuildSchema.parse(examples.SceneBuild[1]);

    it('shows as it is prepared, once, and is offered beside unsaved work', () => {
        const seen = new Set<string>();
        expect(writeLaunch(writing, seen, false)).toMatchObject({show: {kind: 'scenes', buildId: writing.id}});
        expect(writeLaunch({...writing, write: 'done'}, seen, false)).toBeNull(); // its later frames open nothing
        expect(writeLaunch({...writing, id: 'other'}, seen, true)).toMatchObject({
            offer: {text: 'Reachy écrit une scène.', surface: {kind: 'scenes', buildId: 'other'}}
        });
    });

    it('opens nothing when the dashboard asked for it, nor once it has ended', () => {
        expect(writeLaunch({...writing, voiced: false}, new Set(), false)).toBeNull(); // shown where it was asked
        expect(writeLaunch(ready, new Set(), false)).toBeNull(); // the robot plays it: that shows it (sceneLaunch)
    });
});
