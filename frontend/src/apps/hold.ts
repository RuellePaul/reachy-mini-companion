// The manette's hold in the dashboard, as one value and one transition: no React, no timer, no network (Manette.tsx
// is its shell: it turns what happens into events and does what the effects say). Time comes in with the events.
//
// What the transition keeps true:
// - The human's yes is their own gesture in the app (« Jouer »): an ask is granted at a gesture no older than
//   TOUCH_MS, the one made just before it or the next one. A robot asleep is woken first, and the yes that started
//   the wake holds through it.
// - A robot this frame woke is put back to sleep when the frame goes: it is left as it was found. A wake that
//   failed, or a robot that slept again since, is nothing to put back.
// - The robot says when it holds still as the controller (an `on`, said again in answer to each renewal): only
//   then is the page told `on`, once per hold.
// - The robot ends its holds in the order they were granted, each grant it got with one off (or refused), before
//   it takes the next: an off belongs to the oldest hold not yet ended (`open`). Until then a hold let go may still
//   answer its renewals on (`stale`). A grant it never got (replaced in the Mac's queue, or come while it sleeps)
//   has no end: an on of the grant in force ends every older one.
// - A hold the Mac says is in force when this frame grants with none of its own open (the frame shown before's,
//   still given back) is the oldest: its off is not this grant's. The Mac refusing the grant, it is counted no more.
// - What the Mac's session says puts right a word of the robot's lost in a reconnect.
import type {ManetteProfile} from '../api/schemas';
import {wordsNow} from '../i18n';
import {APPS} from './words';

export const TOUCH_MS = 5000; // the human's click or key in the app, this fresh, is their yes to what it then asks

/** What the Mac's session says of the robot. */
export interface MacSaid {
    readonly awake: boolean;
    readonly closing: boolean; // awake, falling asleep: it takes no manette
    readonly manette: ManetteProfile | null; // the profile it holds, from its on to its off
}

export interface Hold {
    /** The app's ask that waits: for the human's own gesture in the app, or for the robot to wake. */
    readonly ask: ManetteProfile | null;
    /** The human's gesture met that ask while the robot slept: it is granted as soon as the robot wakes. */
    readonly agreed: boolean;
    /** When the human last clicked or pressed a key in the app (ms, the shell's clock). */
    readonly touchedAt: number;
    /** The wake this frame asked for: none (or failed, or slept off since), under way, or done: the robot is awake
     *  because of this frame, which puts it back to sleep as it goes. */
    readonly woken: 'no' | 'asked' | 'yes';
    /** The hold granted: its lease runs. */
    readonly grant: ManetteProfile | null;
    /** The robot said it holds still as that controller: the page was told `on`. */
    readonly held: boolean;
    /** Grants sent whose end the robot has not told yet: an off belongs to the oldest. */
    readonly open: number;
    /** The grant's words (it, then each renewal) the robot has not answered on yet. */
    readonly owed: number;
    /** A hold let go that the robot may still be in: its profile, and the ons it still owes to its renewals. */
    readonly stale: {readonly profile: ManetteProfile | null; readonly ons: number};
    /** A hold let go: the robot waits, freed, for the hand to let it go within reach. */
    readonly returning: ManetteProfile | null;
    /** Why the last ask or hold ended, for the human (French). */
    readonly error: string;
    /** What the Mac's session last said. */
    readonly mac: MacSaid;
}

export type HoldEvent =
    | {type: 'ask'; profile: ManetteProfile | null; now: number} // the page asks for a profile, or lets go (null)
    | {type: 'touch'; now: number} // the human clicked or pressed a key in the app
    | {type: 'release'} // « Rendre la main »
    | {
          type: 'robot'; // the robot's word (contracts.ManetteEvent)
          state: 'on' | 'returning' | 'off' | 'refused';
          profile: ManetteProfile | null;
          why: string | null;
      }
    | ({type: 'session'; now: number} & MacSaid) // what the Mac's session says now
    | {type: 'lease'} // a second went by: a hold is renewed
    | {type: 'failed'; profile: ManetteProfile | null; opened: number; error: string} // the Mac refused a send
    | {type: 'wake-failed'; error: string} // the Mac refused the wake
    | {type: 'wake-timeout'} // the robot asked to wake is not awake in time
    | {type: 'gone'}; // the frame goes

export type Effect =
    | {type: 'tell'; state: 'off' | 'asking' | 'on'; profile: ManetteProfile | null} // to the page
    | {type: 'send'; profile: ManetteProfile | null; opened: number} // to the Mac; `opened` > 0: a grant
    | {type: 'wake'}
    | {type: 'sleep'};

type Draft = {-readonly [K in keyof Hold]: Hold[K]};
const NONE = {profile: null, ons: 0} as const;

/** The hold of a frame that just came: nothing asked, nothing held. */
export const rest = (mac: MacSaid): Hold => ({
    ask: null,
    agreed: false,
    touchedAt: -Infinity,
    woken: 'no',
    grant: null,
    held: false,
    open: 0,
    owed: 0,
    stale: NONE,
    returning: null,
    error: '',
    mac
});

const same = (a: Hold, b: Hold): boolean =>
    (Object.keys(a) as (keyof Hold)[]).every((key) => key === 'stale' || key === 'mac' || a[key] === b[key]) &&
    a.stale.profile === b.stale.profile &&
    a.stale.ons === b.stale.ons &&
    a.mac.awake === b.mac.awake &&
    a.mac.closing === b.mac.closing &&
    a.mac.manette === b.mac.manette;

/** The hold after `event`, and what to do about it. The same value comes back when nothing changed. */
export const step = (hold: Hold, event: HoldEvent): {hold: Hold; effects: Effect[]} => {
    const next: Draft = {...hold};
    const effects: Effect[] = [];
    const tell = (state: 'off' | 'asking' | 'on', profile: ManetteProfile | null) =>
        effects.push({type: 'tell', state, profile});
    const send = (profile: ManetteProfile | null, opened = 0) => effects.push({type: 'send', profile, opened});
    /** Where the hold is, for a page that asks again or whose ask ends. */
    const tellHold = () => (next.grant ? tell(next.held ? 'on' : 'asking', next.grant) : tell('off', null));
    /** The hold in force is let go: the robot may still answer its renewals on. */
    const letGo = () => {
        if (next.held) next.stale = {profile: next.grant, ons: next.owed};
    };
    const forget = () => {
        next.grant = null;
        next.held = false;
        tell('off', null);
    };
    const release = () => {
        next.ask = null;
        next.agreed = false;
        letGo();
        if (next.grant) send(null);
        forget();
    };
    const giveUp = (why: string) => {
        next.ask = null;
        next.agreed = false;
        next.error = why;
        tellHold();
    };
    /** The ask becomes the hold: sent to the Mac once, as a grant. */
    const take = (profile: ManetteProfile) => {
        next.ask = null;
        next.agreed = false;
        letGo();
        const opened = next.open === 0 && next.mac.manette ? 2 : 1; // its grant, and the hold the Mac says in force
        if (opened === 2) next.stale = {profile: next.mac.manette, ons: 0};
        next.grant = profile;
        next.held = false;
        next.open += opened;
        next.owed = 1;
        send(profile, opened);
        tell('asking', profile);
    };
    /** An ask the human's gesture met is granted once the robot is in conversation; asleep, it is woken first
     *  (falling asleep: once it sleeps, if the gesture is still fresh). */
    const settle = (now: number) => {
        const profile = next.ask;
        if (!profile) return;
        const fresh = now - next.touchedAt <= TOUCH_MS;
        if (!fresh && !next.agreed) return;
        if (next.mac.awake && !next.mac.closing) take(profile);
        else if (!next.mac.awake && fresh) {
            next.agreed = true;
            if (next.woken === 'asked') return;
            next.woken = 'asked';
            effects.push({type: 'wake'});
        }
    };
    /** The wake this frame asked for did not come: the ask that waits is given up, the page plays without. */
    const unwoken = (why: string) => {
        if (next.woken !== 'asked') return;
        next.woken = 'no';
        if (next.ask) giveUp(why);
    };

    switch (event.type) {
        case 'ask':
            if (event.profile === null) release();
            else if (event.profile === next.grant)
                tellHold(); // asked again (a new round): told where it is
            else {
                if (event.profile !== next.ask) next.agreed = false;
                next.ask = event.profile;
                next.error = '';
                tell('asking', event.profile);
                settle(event.now);
            }
            break;
        case 'touch':
            next.touchedAt = event.now;
            settle(event.now);
            break;
        case 'release':
            release();
            break;
        case 'robot': {
            if (event.state === 'returning') {
                next.returning = event.profile;
                break;
            }
            next.returning = null;
            const {profile: was, ons} = next.stale;
            if (event.state === 'on' && event.profile === was && ons > 0) {
                next.stale = {profile: was, ons: ons - 1}; // a renewal of the hold let go, answered before its end
                break;
            }
            next.stale = NONE; // the hold let go has ended: the robot ends a hold before it says another
            if (event.state !== 'on') {
                const older = next.open > 1; // a hold let go before the one in force: it ends that one
                next.open = Math.max(0, next.open - 1);
                if (older) break;
            }
            if (!next.grant) break;
            if (event.state === 'on' && event.profile === next.grant) {
                next.open = 1; // every hold before it has ended
                next.owed = Math.max(0, next.owed - 1);
                if (next.held) break; // said again at each renewal: the page hears it once
                next.held = true;
                tell('on', event.profile);
            } else if (event.state !== 'on' && event.why !== 'profile') {
                if (event.state === 'refused')
                    next.error = wordsNow(APPS).refused[event.why === 'scene' ? 'scene' : 'sleep'];
                if (event.why === 'pose') next.error = wordsNow(APPS).noPose;
                release();
            }
            break;
        }
        case 'session': {
            const said = event.manette;
            const changed = said !== next.mac.manette;
            next.mac = {awake: event.awake, closing: event.closing, manette: said};
            if (next.woken === 'asked' && event.awake) next.woken = 'yes';
            else if (next.woken === 'yes' && !event.awake) next.woken = 'no'; // asleep again: nothing to put back
            settle(event.now);
            if (!changed) break;
            if (said && said === next.grant && !next.held) {
                next.stale = NONE; // its on was lost: every hold before it has ended
                next.open = 1;
                next.held = true;
                next.returning = null; // the robot holds: it waits for no hand any more
                tell('on', said);
            } else if (!said && (next.held || next.stale.profile)) {
                next.open = Math.max(0, next.open - 1); // the off of the hold it said, lost on the way: counted here
                if (next.held) release();
                next.stale = NONE;
            }
            if (!said) next.returning = null; // the Mac keeps the profile until the off: none, no hand is awaited
            break;
        }
        case 'lease':
            if (!next.grant) break;
            next.owed += 1;
            send(next.grant);
            break;
        case 'failed': {
            // Refused or unreachable: taken as never queued, with the hold before it counted for it, unless a grant of
            // another profile made since still waits behind that hold.
            const later = next.grant !== null && next.grant !== event.profile;
            if (event.opened) next.open = Math.max(0, next.open - (later ? 1 : event.opened));
            if (event.profile === null || next.grant !== event.profile) break;
            next.owed = Math.max(0, next.owed - 1); // a word that never reached the robot owes no on
            next.error = event.error;
            letGo(); // the robot still holds it until its lease runs out: let go, as « Rendre la main » does
            forget();
            break;
        }
        case 'wake-failed':
            unwoken(event.error);
            break;
        case 'wake-timeout':
            unwoken(wordsNow(APPS).notWaking);
            break;
        case 'gone':
            if (next.grant) send(null);
            if (next.woken !== 'no') effects.push({type: 'sleep'}); // woken for this app, or about to be
            next.woken = 'no';
            break;
    }
    return {hold: same(hold, next) ? hold : next, effects};
};
