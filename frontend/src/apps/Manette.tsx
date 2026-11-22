// The manette in the dashboard, around its hold (hold.ts: one value, one transition, and the rules it keeps): this
// file is the shell. It turns what happens into the hold's events (the page's ask, the human's gesture in the app,
// the robot's word, the Mac's session, the lease's second), does what the hold's effects say (a word to the page, a
// post to the Mac, a wake, a sleep), streams the robot's measured pose and its ear's level to the page as `pad`
// messages while the robot holds, and shows the bar above the frame, with « Rendre la main ». The lease: granted
// once, then renewed every second while it holds; closing the app lets the robot go within 3 s.
import {useCallback, useEffect, useState, useSyncExternalStore, type RefObject} from 'react';
import {Alert, Button} from '@mui/material';
import SportsEsportsRounded from '@mui/icons-material/SportsEsportsRounded';
import {api, errorText} from '../api/client';
import type {ManetteProfile} from '../api/schemas';
import {useWords} from '../i18n';
import {useLive} from '../live';
import {appsApi} from './api';
import {rest, step, type Effect, type Hold, type HoldEvent} from './hold';
import {ask, held as heldAs, lent, pad, STILL} from './pad';
import {APPS} from './words';

export {TOUCH_MS} from './hold';
export const LEASE_MS = 1000; // the Mac's command lives 3 s: two renewals may be lost
export const WAKE_MS = 20_000; // a robot asked to wake and not awake by then is not coming

/** A message to the app's frame (an opaque origin: posted to '*', its own window checked by the bridge). */
const postTo = (frame: RefObject<HTMLIFrameElement | null>, message: object) =>
    frame.current?.contentWindow?.postMessage({reachy: 1, ...message}, '*');

export interface ManetteHold {
    asked: ManetteProfile | null; // the app asks: the human has not acted in it, or the robot is being woken
    waking: boolean; // the robot was asleep: it is woken for this ask
    granted: ManetteProfile | null; // the human said yes: the lease runs
    holding: boolean; // the robot holds still as that controller
    returning: ManetteProfile | null; // the hold let go: the robot waits to be released within reach, then takes it
    error: string;
    request: (profile: ManetteProfile | null) => void; // what the app's frame asked
    touch: () => void; // the human clicked or pressed a key in the app's frame
    release: () => void;
}

/**
 * A hold kept outside React's render: `dispatch` steps it at once (an event never reads a value one render late),
 * tells whoever listens, and does what the step says. The posts to the Mac go one after the other, in the order
 * they were asked; a hold's post still queued when the frame goes is never sent, the let-go that follows it is.
 */
const drive = (first: Hold, id: string, post: (message: object) => void) => {
    let hold = first;
    let queue: Promise<unknown> = Promise.resolve();
    let gone = 0; // times the frame went
    const listeners = new Set<() => void>();
    const send = (profile: ManetteProfile | null, opened: number) => {
        const at = gone;
        queue = queue
            .then(() => (profile && gone !== at ? undefined : appsApi.manette(id, profile, opened > 0)))
            .catch((err: unknown) => dispatch({type: 'failed', profile, opened, error: errorText(err)}));
    };
    const perform = (effect: Effect) => {
        if (effect.type === 'tell') post({type: 'manette', state: effect.state, profile: effect.profile});
        else if (effect.type === 'send') send(effect.profile, effect.opened);
        else if (effect.type === 'sleep') void api.session('sleep').catch(() => undefined);
        else api.session('wake').catch((err: unknown) => dispatch({type: 'wake-failed', error: errorText(err)}));
    };
    const dispatch = (event: HoldEvent): void => {
        if (event.type === 'gone') gone += 1;
        const next = step(hold, event);
        if (next.hold !== hold) {
            hold = next.hold;
            listeners.forEach((listener) => listener());
        }
        next.effects.forEach(perform);
    };
    return {
        read: () => hold,
        listen: (listener: () => void) => {
            listeners.add(listener);
            return () => void listeners.delete(listener);
        },
        dispatch
    };
};

export const useManette = (id: string, frame: RefObject<HTMLIFrameElement | null>): ManetteHold => {
    const {on, onConversation, want, session} = useLive();
    const closing = session.phase === 'closing';
    const manette = session.manette ?? null;
    const post = useCallback((message: object) => postTo(frame, message), [frame]);
    // One per mount: the frame is keyed on its app (Apps.tsx), so `id` and `frame` are this mount's for good.
    const [driver] = useState(() => drive(rest({awake: session.awake, closing, manette}), id, post));
    const hold = useSyncExternalStore(driver.listen, driver.read);

    // What the Mac's session says: whether the robot is in conversation, and the profile it holds.
    useEffect(() => {
        driver.dispatch({type: 'session', awake: session.awake, closing, manette, now: Date.now()});
    }, [driver, session.awake, closing, manette]);

    // The robot's word.
    useEffect(
        () =>
            onConversation((event) => {
                if (event.kind !== 'manette') return;
                driver.dispatch({type: 'robot', state: event.state, profile: event.profile, why: event.why});
            }),
        [onConversation, driver]
    );

    // The lease: renewed every second while held; let go when the frame goes, the last word after the post on its way.
    useEffect(() => {
        const timer = setInterval(() => driver.dispatch({type: 'lease'}), LEASE_MS);
        return () => {
            clearInterval(timer);
            driver.dispatch({type: 'gone'});
        };
    }, [driver]);

    const waking = hold.woken === 'asked';
    useEffect(() => {
        if (!waking) return;
        const timer = setTimeout(() => driver.dispatch({type: 'wake-timeout'}), WAKE_MS);
        return () => clearTimeout(timer);
    }, [waking, driver]);

    // The pads: one per pose frame, with the ear's latest level; a profile that lends no body, one per level.
    const granted = hold.grant;
    const holding = hold.held;
    useEffect(() => {
        if (!holding || !granted) return;
        const {body, ear} = lent(granted);
        const offs: (() => void)[] = [];
        let voice = 0;
        if (ear)
            offs.push(
                want('voice'),
                on('voice', ({level}) => {
                    voice = level;
                    if (!body) post({type: 'pad', ...STILL, voice});
                })
            );
        if (body) {
            const read = pad(body);
            offs.push(
                want('pose'),
                on('pose', (frame) => {
                    const state = read(frame);
                    if (state) post({type: 'pad', ...state, voice});
                })
            );
        }
        return () => offs.forEach((off) => off());
    }, [holding, granted, on, want, post]);

    const request = useCallback(
        (profile: ManetteProfile | null) => driver.dispatch({type: 'ask', profile, now: Date.now()}),
        [driver]
    );
    const touch = useCallback(() => driver.dispatch({type: 'touch', now: Date.now()}), [driver]);
    const release = useCallback(() => driver.dispatch({type: 'release'}), [driver]);
    return {
        asked: hold.ask,
        waking: hold.ask !== null && hold.agreed && waking,
        granted,
        holding,
        returning: hold.returning,
        error: hold.error,
        request,
        touch,
        release
    };
};

// A bar too narrow for its words and its buttons side by side keeps a line of words and puts the buttons under them,
// at the end of the next line.
const BAR = {borderRadius: 0, flexWrap: 'wrap', '& .MuiAlert-message': {flex: '1 1 16em'}} as const;

/** Above the app's frame: what the robot waits for (a hold let go, or the one before a switch), then what an ask
 *  waits for (the robot waking, or the human's own gesture in the app), else the controller in force; « Rendre la
 *  main » while a grant runs. */
export const ManetteBar = ({hold, title}: {hold: ManetteHold; title: string}) => {
    const t = useWords(APPS);
    const giveBack = hold.granted && (
        <Button color="inherit" onClick={hold.release}>
            {t.giveBack}
        </Button>
    );
    const back = hold.returning && (
        <Alert severity="warning" sx={BAR} action={giveBack}>
            {t.letGo(lent(hold.returning).body === 'tete')}
        </Alert>
    );
    if (hold.asked)
        return (
            <>
                {back}
                <Alert severity="info" icon={<SportsEsportsRounded />} sx={BAR}>
                    {hold.waking ? t.wakingToPlay : t.lend(title, ask(hold.asked))}
                </Alert>
            </>
        );
    if (back) return back;
    if (hold.granted)
        return (
            <Alert severity="success" icon={<SportsEsportsRounded />} sx={BAR} action={giveBack}>
                {hold.holding ? heldAs(hold.granted) : t.settling}
            </Alert>
        );
    return hold.error ? (
        <Alert severity="warning" sx={BAR}>
            {hold.error}
        </Alert>
    ) : null;
};
