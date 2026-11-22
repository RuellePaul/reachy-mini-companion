// Every call the dashboard's API modules make, as it leaves for the Mac (method, path, body), written out in
// dashboard-calls.json: tests/mac/test_routes.py checks each one against the Mac's own routes and request models.
// A call changed here fails until that file is written again (`npx vitest run -u`), and then there if the Mac
// does not take it.
import {afterEach, expect, it, vi} from 'vitest';
import {api} from '../src/api/client';
import type {TryMove} from '../src/api/schemas';
import {appsApi} from '../src/apps/api';
import {scenesApi} from '../src/scenes/api';
import {preview} from '../src/studio/api';

const MOVE: TryMove = {
    kind: 'animation',
    name: 'curious',
    mode: 'listening',
    tuning: {amplitude: 1.2, speed: 0.8},
    seconds: 6
};

// Each function of each module, called as the dashboard calls it; a function called two ways is named twice.
const CALLS: [string, () => Promise<unknown>][] = [
    ['api.state', () => api.state()],
    ['api.settings', () => api.settings()],
    ['api.saveSettings', () => api.saveSettings({name: 'Robby', web_search: true})],
    ['api.setVolume', () => api.setVolume(40)],
    ['api.session wake', () => api.session('wake')],
    ['api.session sleep', () => api.session('sleep')],
    ['api.session restart', () => api.session('restart')],
    ['api.say', () => api.say('Bonjour')],
    ['api.catalog', () => api.catalog()],
    ['api.tryMove', () => api.tryMove(MOVE)],
    ['api.playScene', () => api.playScene('s1')],
    ['api.playScene with options', () => api.playScene('s1', {moves: false, rehearse: 'Alice', start: 3})],
    ['api.stopScene', () => api.stopScene()],
    ['appsApi.list', () => appsApi.list()],
    ['appsApi.create', () => appsApi.create('Un morpion')],
    ['appsApi.update', () => appsApi.update('a1', 'Plus grand')],
    ['appsApi.rename', () => appsApi.rename('a1', 'Morpion')],
    ['appsApi.setQuiet', () => appsApi.setQuiet('a1', false)],
    ['appsApi.remove', () => appsApi.remove('a1')],
    ['appsApi.html', () => appsApi.html('a1')],
    ['appsApi.draft', () => appsApi.draft('a1')],
    ['appsApi.cancel', () => appsApi.cancel('a1')],
    ['appsApi.bridge', () => appsApi.bridge('a1', {kind: 'look', yaw: -12, pitch: 5}, 'k3x9')],
    ['appsApi.manette', () => appsApi.manette('a1', 'volant', true)],
    ['appsApi.manette let go', () => appsApi.manette('a1', null)],
    ['scenesApi.list', () => scenesApi.list()],
    ['scenesApi.get', () => scenesApi.get('s1')],
    ['scenesApi.save new', () => scenesApi.save(null, 'La visite', 'ROBOT : Bonjour.')],
    ['scenesApi.save', () => scenesApi.save('s1', 'La visite', 'ROBOT : Bonjour.')],
    ['scenesApi.remove', () => scenesApi.remove('s1')],
    ['scenesApi.check', () => scenesApi.check('ROBOT : Bonjour.')],
    ['scenesApi.vocabulary', () => scenesApi.vocabulary()],
    ['scenesApi.emotions', () => scenesApi.emotions('', 'ROBOT : Bonjour.')],
    ['scenesApi.write', () => scenesApi.write('Deux fentes de Young débattent')],
    ['scenesApi.builds', () => scenesApi.builds()],
    ['scenesApi.cancel', () => scenesApi.cancel('b1')],
    ['studio preview', () => preview(MOVE)]
];

afterEach(() => vi.unstubAllGlobals());

it('calls every function of the API modules', () => {
    const called = new Set(CALLS.map(([name]) => name.split(' ')[0]));
    const modules = {api, appsApi, scenesApi};
    const exported = Object.entries(modules).flatMap(([module, fns]) =>
        Object.keys(fns).map((fn) => `${module}.${fn}`)
    );
    expect([...called].filter((name) => name !== 'studio').sort()).toEqual(exported.sort());
});

it('sends each call as dashboard-calls.json writes it, one request each', async () => {
    const sent: {call: string; method: string; path: string; body: unknown}[] = [];
    let call = '';
    vi.stubGlobal('fetch', async (path: string, init?: RequestInit) => {
        const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : null;
        sent.push({call, method: init?.method ?? 'GET', path, body});
        return new Response(null, {status: 503}); // refused: nothing to parse, the call ends at once
    });
    for (const [name, run] of CALLS) {
        call = name;
        await run().catch(() => undefined);
    }
    expect(sent.map((s) => s.call)).toEqual(CALLS.map(([name]) => name));
    await expect(`${JSON.stringify(sent, null, 4)}\n`).toMatchFileSnapshot('./dashboard-calls.json');
});
