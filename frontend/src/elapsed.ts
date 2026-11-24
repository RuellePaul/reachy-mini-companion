// The time a build has taken so far (an app's, a scene's), counted here from when the Mac says it started.
import {useEffect, useState} from 'react';
import {formatDuration} from './format';

/** « 42 s », « 2 min 05 s »: whole seconds gone by, so the count never runs ahead of the clock. */
export const elapsed = (seconds: number): string => formatDuration(Math.max(0, Math.floor(seconds)));

/** Seconds since `since` (epoch seconds), ticking until `until` (when it ended), if given. */
export const useSeconds = (since: number | undefined, until?: number | null): number => {
    const [now, setNow] = useState(() => Date.now() / 1000);
    useEffect(() => {
        if (until) return;
        const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
        return () => clearInterval(timer);
    }, [until]);
    return since === undefined ? 0 : (until ?? now) - since;
};
