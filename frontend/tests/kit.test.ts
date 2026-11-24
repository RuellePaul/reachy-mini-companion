// @vitest-environment happy-dom
// The apps' toolbox (mac_server/assets/kit.js, `window.reachy`), which the Mac puts before every page it serves:
// what it sends is what the dashboard's bridge takes, what it is told is what the dashboard posts, and « Jouer »
// ends once, with the robot or without it. Run here as a page would run it, its lists filled as the Mac fills them.
import {readFileSync} from 'fs';
import * as path from 'path';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {bridgeMessageSchema, EMOTIONS, GESTURES, manetteProfileSchema} from '../src/api/schemas';
import {colour, type Token} from '../src/theme';

const SOURCE = readFileSync(path.resolve(__dirname, '../../mac_server/assets/kit.js'), 'utf8')
    .replace('__EMOTIONS__', JSON.stringify(EMOTIONS))
    .replace('__GESTURES__', JSON.stringify(GESTURES))
    .replace('__PROFILES__', JSON.stringify(manetteProfileSchema.options));

interface Pad {
    gauche: boolean;
    droite: boolean;
    antennes: {gauche: number; droite: number};
    corps: number;
    lacet: number;
    tangage: number;
    roulis: number;
    avant: number;
    cote: number;
    haut: number;
    voix: number;
    t: number;
}
interface Kit {
    version: number;
    couleurs: Record<string, string>;
    REPRISE_MS: Record<string, number>;
    etat: {eveille: boolean | null; manette: 'non' | 'attente' | 'oui'; profil: string | null};
    pad: Pad;
    quand: (name: string, listener: (...said: never[]) => void) => () => void;
    message: (type: string, fields?: object) => object | null;
    envoyer: (type: string, fields?: object) => boolean;
    manette: (profile: string | null) => boolean;
    lancer: (
        profile: string,
        then: {attendre?: (title: string) => void; partir?: (held: boolean, why: string) => void}
    ) => {sans: () => void};
    feter: (name: string) => void;
    regard: (yaw: number, pitch: number, roll?: number) => boolean;
    antennes: (left: number, right: number) => boolean;
    rejouer: (start: number, end: number, loops: number) => boolean;
    emotion: (name: string) => boolean;
    geste: (name: string) => boolean;
    pastilles: (options: {robot?: HTMLElement; manette?: HTMLElement; tenue?: string}) => void;
    hasard: (seed: number) => () => number;
}

let sent: {type: string; [field: string]: unknown}[] = [];
/** A page's own copy of the toolbox, as its frame would run it. */
const kit = (): Kit => {
    new Function(SOURCE)();
    return (window as unknown as {reachy: Kit}).reachy;
};
/** What the dashboard posts to the frame. */
const told = (data: object) =>
    window.dispatchEvent(new MessageEvent('message', {data: {reachy: 1, ...data}, source: window.parent}));
const PAD = {left: false, right: false, antennas: {left: 0, right: 0}, body: 0, voice: 0, t: 0};
const HEAD = {yaw: 0, pitch: 0, roll: 0, x: 0, y: 0, z: 0};

beforeEach(() => {
    sent = [];
    vi.spyOn(window.parent, 'postMessage').mockImplementation(((message: never) => void sent.push(message)) as never);
});
afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
});

describe('what the toolbox sends', () => {
    it('is what the bridge takes, bounded: every kind, at its limits and past them', () => {
        const reachy = kit();
        const asked = [
            reachy.message('emotion', {name: EMOTIONS[0]}),
            reachy.message('gesture', {name: GESTURES[0]}),
            reachy.message('look', {yaw: 99, pitch: -99, roll: 99}),
            reachy.message('look', {}),
            reachy.message('antennas', {left: -99, right: 99}),
            reachy.message('replay', {start: 1794000001000, end: 1794000003500, loops: 99}),
            reachy.message('replay', {loops: 0}),
            reachy.message('manette', {profile: null}),
            ...manetteProfileSchema.options.map((profile) => reachy.message('manette', {profile}))
        ];
        for (const message of asked)
            expect(bridgeMessageSchema.safeParse(message).success, JSON.stringify(message)).toBe(true);
        expect(asked[2]).toMatchObject({yaw: 30, pitch: -20, roll: 20});
        expect(asked[4]).toMatchObject({left: -60, right: 60});
        expect(asked[5]).toMatchObject({loops: 8});
        expect(reachy.message('emotion', {name: 'colere'})).toBeNull();
        expect(reachy.message('gesture', {name: EMOTIONS[0]})).toBeNull();
        expect(reachy.message('manette', {profile: 'pieds'})).toBeNull();
        expect(reachy.message('fly')).toBeNull();
    });

    it('speaks of the screen’s sides: its left antenna is the robot’s right one', () => {
        const reachy = kit();
        reachy.antennes(10, -20);
        reachy.regard(-30, 20, 5);
        expect(sent).toEqual([
            {reachy: 1, type: 'antennas', left: -20, right: 10},
            {reachy: 1, type: 'look', yaw: -30, pitch: 20, roll: 5}
        ]);
    });

    it('sends no move while a body is lent, any with the ear alone, and a replay at any time', () => {
        const reachy = kit();
        reachy.manette('tete');
        expect(reachy.geste('nod')).toBe(false); // asking: the robot gets into place
        told({type: 'manette', state: 'on', profile: 'tete'});
        expect(reachy.regard(0, 0)).toBe(false);
        expect(reachy.rejouer(1000, 3500, 4)).toBe(true); // the robot waits for its body itself
        reachy.manette(null);
        expect(reachy.geste('nod')).toBe(true);
        reachy.manette('voix');
        told({type: 'manette', state: 'on', profile: 'voix'});
        expect(reachy.regard(10, 0)).toBe(true); // nothing of the body is lent
        expect(sent.map((message) => message.type)).toEqual([
            'manette',
            'replay',
            'manette',
            'gesture',
            'manette',
            'look'
        ]);
    });
});

describe('what the toolbox is told', () => {
    it('keeps the pad in the screen’s sides, the head’s yaw with the base’s, and says each press once', () => {
        const reachy = kit();
        const presses: string[] = [];
        reachy.quand('appui', ((side: string) => presses.push(side)) as never);
        told({type: 'pad', ...PAD, head: HEAD, right: true}); // no manette: not a pad of this page's
        expect(reachy.pad.gauche).toBe(false);
        reachy.manette('tete+voix');
        told({type: 'manette', state: 'on', profile: 'tete+voix'});
        const head = {yaw: 12, pitch: 8, roll: -3, x: 5, y: 20, z: -10};
        const pad = {
            ...PAD,
            right: true,
            antennas: {left: 0, right: 9},
            body: 30,
            head,
            voice: 0.4,
            t: 1794000001234.5
        };
        told({type: 'pad', ...pad});
        told({type: 'pad', ...pad}); // still pushed: no second press
        expect(reachy.pad).toEqual({
            gauche: true, // the robot's right antenna, at the screen's left
            droite: false,
            antennes: {gauche: 9, droite: 0},
            corps: 30,
            lacet: 42,
            tangage: 8,
            roulis: -3,
            avant: 5,
            cote: 20,
            haut: -10,
            voix: 0.4,
            t: 1794000001234.5
        });
        expect(presses).toEqual(['gauche']);
        told({type: 'manette', state: 'off', profile: null});
        expect(reachy.pad).toMatchObject({gauche: false, voix: 0});
        expect(reachy.etat).toEqual({eveille: null, manette: 'non', profil: null});
    });

    it('says the robot’s replay as it starts and ends, and its waking', () => {
        const reachy = kit();
        const said: object[] = [];
        reachy.quand('rejeu', ((replay: object) => said.push(replay)) as never);
        reachy.quand('robot', ((awake: boolean) => said.push({awake})) as never);
        told({type: 'robot', awake: false});
        told({type: 'robot', awake: false}); // said again: no news
        told({type: 'robot', awake: true});
        told({type: 'replay', state: 'started', seconds: 2.5, loops: 4});
        told({type: 'replay', state: 'ended', seconds: null, loops: null});
        window.dispatchEvent(
            new MessageEvent('message', {data: {reachy: 1, type: 'robot', awake: false}, source: null})
        );
        expect(said).toEqual([
            {awake: false},
            {awake: true},
            {etat: 'debut', secondes: 2.5, tours: 4},
            {etat: 'fin', secondes: 0, tours: 0}
        ]);
        expect(reachy.etat.eveille).toBe(true); // a message from elsewhere than the dashboard is no word of the robot's
    });
});

describe('« Jouer »', () => {
    const play = (reachy: Kit, profile = 'volant') => {
        const said: (string | boolean)[] = [];
        const game = reachy.lancer(profile, {
            attendre: (title) => said.push(title),
            partir: (held, why) => said.push(held, why)
        });
        return {said, game};
    };

    it('plays without a robot at once, asking for nothing', () => {
        const {said} = play(kit());
        expect(said).toEqual([false, 'aucun']);
        expect(sent).toEqual([]);
    });

    it('asks for the manette in the click itself, waits while the robot gets into place, and starts once', () => {
        const reachy = kit();
        told({type: 'robot', awake: true});
        const {said} = play(reachy);
        expect(sent).toEqual([{reachy: 1, type: 'manette', profile: 'volant'}]);
        expect(said).toEqual(['Reachy se met en place…']);
        told({type: 'manette', state: 'asking', profile: 'volant'});
        told({type: 'manette', state: 'on', profile: 'volant'});
        told({type: 'manette', state: 'on', profile: 'volant'}); // said again at each renewal
        expect(said).toEqual(['Reachy se met en place…', true, 'oui']);
        expect(reachy.etat).toEqual({eveille: true, manette: 'oui', profil: 'volant'});
        expect(play(reachy).said).toEqual([true, 'oui']); // a new round: it holds it already
    });

    it('asks a sleeping robot too, says it wakes, and plays without it if it does not come', () => {
        const reachy = kit();
        told({type: 'robot', awake: false});
        const first = play(reachy);
        expect(first.said).toEqual(['Reachy se réveille…']);
        told({type: 'robot', awake: true});
        expect(first.said).toEqual(['Reachy se réveille…', 'Reachy se met en place…']);
        told({type: 'manette', state: 'off', profile: null});
        expect(first.said.slice(-2)).toEqual([false, 'absent']); // asked, and it did not come
        told({type: 'manette', state: 'on', profile: 'volant'}); // late, and of no ask still waiting
        expect(first.said).toHaveLength(4);
    });

    it('leaves the robot at « Jouer sans Reachy », and forgets a « Jouer » another one replaces', () => {
        const reachy = kit();
        told({type: 'robot', awake: true});
        const first = play(reachy);
        const second = play(reachy, 'tete');
        told({type: 'manette', state: 'asking', profile: 'tete'});
        second.game.sans();
        second.game.sans(); // pressed twice
        expect(first.said).toEqual(['Reachy se met en place…']); // replaced: it never starts
        expect(second.said).toEqual(['Reachy se met en place…', false, 'sans']); // the player's own choice
        expect(sent.at(-1)).toEqual({reachy: 1, type: 'manette', profile: null});
        expect(reachy.etat.manette).toBe('non');
    });
});

describe('the end of a game', () => {
    it('gives the manette back, then plays the emotion once the robot has its body again', () => {
        vi.useFakeTimers();
        const reachy = kit();
        reachy.manette('tete');
        told({type: 'manette', state: 'on', profile: 'tete'});
        reachy.feter('proud1');
        expect(sent.at(-1)).toEqual({reachy: 1, type: 'manette', profile: null});
        vi.advanceTimersByTime(reachy.REPRISE_MS.tete - 1);
        expect(sent.at(-1)?.type).toBe('manette');
        vi.advanceTimersByTime(1);
        expect(sent.at(-1)).toEqual({reachy: 1, type: 'emotion', name: 'proud1'});
        reachy.feter('bow'); // no manette held, a gesture: at once
        expect(sent.at(-1)).toEqual({reachy: 1, type: 'gesture', name: 'bow'});
    });
});

describe('the robot held under the palm', () => {
    it('is a body like the others: its pill says so, no move goes while it is lent, and the end waits for it', () => {
        vi.useFakeTimers();
        const reachy = kit();
        const manette = document.createElement('span');
        reachy.pastilles({manette});
        told({type: 'robot', awake: true});
        expect(reachy.manette('paume+voix')).toBe(true); // the palm, the fingers, and the ear
        told({type: 'manette', state: 'on', profile: 'paume+voix'});
        expect(manette.textContent).toBe('Manette : paume = volant, doigts = boutons');
        expect(reachy.regard(10, 0)).toBe(false); // its body is the app's
        told({type: 'pad', ...PAD, head: HEAD, body: -14, left: true, antennas: {left: -8, right: 0}});
        expect([reachy.pad.corps, reachy.pad.droite, reachy.pad.antennes.droite]).toEqual([-14, true, -8]);
        reachy.feter('cheerful1');
        vi.advanceTimersByTime(reachy.REPRISE_MS.paume - 1);
        expect(sent.at(-1)).toEqual({reachy: 1, type: 'manette', profile: null});
        vi.advanceTimersByTime(1);
        expect(sent.at(-1)).toEqual({reachy: 1, type: 'emotion', name: 'cheerful1'});
    });
});

describe('what a page shows with it', () => {
    it('keeps the two pills of the robot and the manette, French signs with their words', () => {
        const reachy = kit();
        const [robot, manette] = [document.createElement('span'), document.createElement('span')];
        reachy.pastilles({robot, manette});
        expect([robot.textContent, manette.textContent]).toEqual(['Robot : en attente', 'Manette : non']);
        told({type: 'robot', awake: false});
        reachy.manette('gachettes');
        expect([robot.textContent, robot.dataset.etat]).toEqual(['Robot endormi', 'dort']);
        expect([manette.textContent, manette.dataset.etat]).toEqual(['Manette : Reachy se réveille', 'attente']);
        told({type: 'robot', awake: true});
        expect(manette.textContent).toBe('Manette : Reachy se met en place');
        told({type: 'manette', state: 'on', profile: 'gachettes'});
        expect([robot.textContent, manette.textContent]).toEqual(['Robot réveillé', 'Manette : antennes = boutons']);
    });

    it('has the dashboard’s colours, as CSS variables and for a canvas', () => {
        const reachy = kit();
        const same: Record<string, Token> = {
            page: 'page',
            plaque: 'plate',
            relief: 'raised',
            creux: 'sunken',
            trait: 'line',
            'trait-fort': 'line-strong',
            encre: 'ink',
            'encre-2': 'ink-2',
            'encre-3': 'ink-3',
            ecoute: 'listen',
            pense: 'think',
            parle: 'speak',
            sommeil: 'sleep',
            attention: 'attention',
            faute: 'fault'
        };
        expect(Object.keys(reachy.couleurs).sort()).toEqual(Object.keys(same).sort());
        for (const [name, token] of Object.entries(same)) expect(reachy.couleurs[name], name).toBe(colour(token));
        expect(document.head.textContent).toContain(`--encre:${colour('ink')}`);
    });

    it('draws the same numbers from the same seed', () => {
        const reachy = kit();
        const [a, b, c] = [reachy.hasard(7), reachy.hasard(7), reachy.hasard(8)];
        const first = [a(), a(), a()];
        expect([b(), b(), b()]).toEqual(first);
        expect([c(), c(), c()]).not.toEqual(first);
        expect(first.every((n) => n >= 0 && n < 1)).toBe(true);
    });
});
