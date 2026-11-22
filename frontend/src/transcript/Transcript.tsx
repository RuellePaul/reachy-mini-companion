import {useEffect, useLayoutEffect, useReducer, useRef, useState} from 'react';
import {Box, Typography} from '@mui/material';
import {api} from '../api/client';
import type {RobotStatus, SessionSnapshot} from '../api/schemas';
import {describe} from '../header/status';
import {wordsNow, useLang} from '../i18n';
import type {Link} from '../link';
import {useLive} from '../live';
import {CASUAL, GUTTER, v} from '../theme';
import {follower} from './follow';
import {EMPTY, reduce} from './reducer';
import {RowView, SCRIPT, type Names} from './rows';
import {TRANSCRIPT} from './words';

/** What the empty transcript says: how to start a conversation, or why none can start. Beside the robot's column,
 * which says why already, only what this space is for. */
const invitation = (
    link: Link,
    status: RobotStatus | null,
    session: SessionSnapshot,
    railed: boolean,
    name: string
) => {
    const t = wordsNow(TRANSCRIPT);
    if (railed) return t.writtenHere(name);
    if (link === 'up' && session.awake) return session.phase === 'closing' ? t.fallingAsleep : t.talk;
    if (link === 'up' && status === 'live') return t.wake;
    const {label, detail} = describe(link, status, session);
    return detail || label;
};

/** The names of what a call can ask for, read once the Mac answers. */
const useNames = (link: Link): Names => {
    const [names, setNames] = useState<Names>(() => new Map());
    const known = names.size > 0;
    useEffect(() => {
        if (link !== 'up' || known) return;
        let current = true;
        api.catalog().then(
            (catalog) => current && setNames(new Map(catalog.entries.map((entry) => [entry.id, entry.label]))),
            () => {} // the names the robot sent stay; the next link asks again
        );
        return () => {
            current = false;
        };
    }, [link, known]);
    return names;
};

/** `railed`: the robot's column is beside it (a computer), and says its state. */
export const Transcript = ({railed = false}: {railed?: boolean}) => {
    const {link, robotStatus, session, onConversation, settings} = useLive();
    const [{rows}, dispatch] = useReducer(reduce, EMPTY);
    const box = useRef<HTMLDivElement>(null);
    const content = useRef<HTMLDivElement>(null);
    const [follow] = useState(follower);
    const names = useNames(link);
    useLang(); // the invitation is written in the language shown

    useEffect(() => onConversation(dispatch), [onConversation]);
    useEffect(() => dispatch({kind: 'live', session_id: session.session_id}), [session.session_id]);

    // Every change of rows follows the bottom, whether or not it changes the height (at the cap it may not).
    useLayoutEffect(() => {
        if (box.current) follow.stick(box.current);
    }, [rows, follow]);

    // So does whatever resizes without new rows: an image loading, a smaller window, the composer growing.
    useEffect(() => {
        const scroller = box.current;
        const inner = content.current;
        if (!scroller || !inner) return;
        const observer = new ResizeObserver(() => follow.stick(scroller));
        observer.observe(inner);
        observer.observe(scroller);
        return () => observer.disconnect();
    }, [follow]);

    const robotName = settings?.settings.name ?? 'Reachy';
    return (
        <Box
            ref={box}
            onScroll={(e) => follow.scrolled(e.currentTarget)}
            sx={{
                flex: 1,
                minHeight: 0,
                overflowY: 'auto',
                maskImage: 'linear-gradient(to bottom, transparent 0, #000 20px)'
            }}
        >
            <Box
                ref={content}
                sx={{
                    maxWidth: 808,
                    minHeight: rows.length ? 0 : '100%',
                    mx: 'auto',
                    px: GUTTER,
                    py: 3,
                    display: 'flex',
                    flexDirection: 'column',
                    justifyContent: rows.length ? 'flex-start' : 'center',
                    gap: 2.5
                }}
            >
                {rows.length === 0 ? (
                    <Box sx={{...SCRIPT, pb: '12vh'}}>
                        <Box sx={{display: {xs: 'none', sm: 'block'}}} />
                        <Typography
                            variant="h4"
                            component="p"
                            sx={{fontVariationSettings: CASUAL, fontWeight: 560, color: v('ink-3'), maxWidth: 480}}
                        >
                            {invitation(link, robotStatus, session, railed, robotName)}
                        </Typography>
                    </Box>
                ) : (
                    rows.map((row) => <RowView key={row.id} row={row} robotName={robotName} names={names} />)
                )}
            </Box>
        </Box>
    );
};
