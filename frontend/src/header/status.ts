// What the dashboard says of the robot: its state in a word, and in a few more what can be done about it. One
// function of the line, the robot's health and the session.
import type {RobotStatus, SessionSnapshot} from '../api/schemas';
import {wordsNow} from '../i18n';
import type {Link} from '../link';
import {HEADER} from './words';

export interface Look {
    label: string;
    detail: string;
}

/** In the language shown: a component that shows it follows the language (`useLang`). */
export const describe = (link: Link, status: RobotStatus | null, session: SessionSnapshot): Look => {
    const t = wordsNow(HEADER);
    if (link === 'connecting') return {label: t.linking, detail: ''};
    if (link === 'down') return t.macDown;
    // A scene mutes the conversation and deafens the wake antennas: a wake or a sleep ends it.
    if (session.scene !== null) return t.scene(session.awake);
    if (session.awake && session.hushed) return t.hushed;
    if (session.awake) return t.phase[session.phase ?? 'idle'];
    if (status === 'live') return t.asleep(session.antenna_wake);
    if (status === 'idle') return t.stopped;
    if (status === 'off') return t.off;
    return {label: t.loading, detail: ''};
};

/** What the robot's button does now: wake it, put it to sleep, or nothing (it cannot be reached). */
export const actionOf = (link: Link, status: RobotStatus | null, session: SessionSnapshot) =>
    link !== 'up' ? null : session.awake ? ('sleep' as const) : status === 'live' ? ('wake' as const) : null;
