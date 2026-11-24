// One app running: its sandboxed frame and the bridge that lets it move the robot.
import {useCallback, useEffect, useRef, useState} from 'react';
import {Alert, Box, Button} from '@mui/material';
import {useWords} from '../i18n';
import {useLive} from '../live';
import {v} from '../theme';
import {appsApi} from './api';
import {DASHBOARD, framed, manetteOf, motionOf, pacer, pageOf, PACE_MS, touchOf} from './bridge';
import {ManetteBar, useManette} from './Manette';
import {APPS} from './words';

const tell = (frame: HTMLIFrameElement | null, awake: boolean) =>
    frame?.contentWindow?.postMessage({reachy: 1, type: 'robot', awake}, '*'); // an opaque origin has no name
const newKey = () =>
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');

/**
 * The page in `<iframe sandbox="allow-scripts" srcdoc>`: an opaque origin, no other permission. The bridge opens
 * when the page's own document says, with this mount's key, that it has loaded (`framed`), and is cut when it says
 * it goes or a second `load` comes: the app navigated away, and the frame goes.
 */
export const AppFrame = ({id, title, html}: {id: string; title: string; html: string}) => {
    const {session, onConversation} = useLive();
    const t = useWords(APPS);
    const frame = useRef<HTMLIFrameElement>(null);
    const loads = useRef(0);
    const open = useRef(false); // the page's own document has loaded and not gone
    const awake = useRef(session.awake);
    const [gone, setGone] = useState(false);
    const [key, setKey] = useState(newKey); // a key per mount: a page that left holds an old one
    const manette = useManette(id, frame);
    const ask = useRef(manette.request);
    const touch = useRef(manette.touch);
    const release = useRef(manette.release);
    useEffect(() => {
        ask.current = manette.request;
        touch.current = manette.touch;
        release.current = manette.release;
    }, [manette.request, manette.touch, manette.release]);

    // The robot's word on a replay the page asked for: when the taped stretch itself starts, and when it ends. One
    // still playing when the frame goes is stopped: the robot is left as it was found.
    const replaying = useRef(false);
    useEffect(
        () =>
            onConversation((event) => {
                if (event.kind !== 'replay') return;
                replaying.current = event.state === 'started';
                if (!open.current) return;
                const {state, seconds, loops} = event;
                frame.current?.contentWindow?.postMessage({reachy: 1, type: 'replay', state, seconds, loops}, '*');
            }),
        [onConversation]
    );
    useEffect(
        () => () => {
            if (replaying.current)
                void appsApi.bridge(id, {kind: 'replay', loops: 0}, DASHBOARD).catch(() => undefined);
            replaying.current = false;
        },
        [id]
    );

    useEffect(() => {
        awake.current = session.awake;
        if (open.current) tell(frame.current, session.awake);
    }, [session.awake]);

    const leave = useCallback(() => {
        open.current = false;
        release.current(); // the page left: it holds nothing any more
        setGone(true);
    }, []);

    useEffect(() => {
        const pace = pacer((motion) => void appsApi.bridge(id, motion, DASHBOARD).catch(() => undefined));
        const timer = setInterval(pace.tick, PACE_MS);
        const onMessage = (event: MessageEvent) => {
            const from = frame.current?.contentWindow;
            const page = pageOf(event, from, key);
            if (page === false) return leave();
            if (page && loads.current <= 1) {
                open.current = true;
                return tell(frame.current, awake.current);
            }
            if (touchOf(event, from, key)) return touch.current();
            const wanted = manetteOf(event, from, open.current);
            if (wanted !== undefined) return ask.current(wanted);
            const motion = motionOf(event, from, open.current);
            // Out of sight, nothing: its timers still tick, and would keep the robot from the dashboard in use. A
            // replay stopped is the one thing a hidden page may still ask.
            const stop = motion?.kind === 'replay' && motion.loops === 0;
            if (motion && awake.current && (stop || document.visibilityState === 'visible')) pace.push(motion);
        };
        window.addEventListener('message', onMessage);
        return () => {
            clearInterval(timer);
            window.removeEventListener('message', onMessage);
        };
    }, [id, key, leave]);

    const onLoad = () => {
        loads.current += 1;
        if (loads.current > 1) return leave();
        frame.current?.contentWindow?.focus(); // most games read the keyboard on their document
    };
    const reload = () => {
        loads.current = 0;
        setGone(false);
        setKey(newKey());
    };

    return (
        <Box
            sx={{flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0}}
            onClick={() => frame.current?.contentWindow?.focus()} // not at pointerdown: the pressed button takes it
        >
            <ManetteBar hold={manette} title={title} />
            {gone ? (
                <Alert
                    severity="warning"
                    sx={{borderRadius: 0}}
                    action={
                        <Button color="inherit" onClick={reload}>
                            {t.reload}
                        </Button>
                    }
                >
                    {t.gone}
                </Alert>
            ) : (
                <iframe
                    key={key}
                    ref={frame}
                    title={title}
                    sandbox="allow-scripts"
                    srcDoc={framed(html, key)}
                    onLoad={onLoad}
                    style={{flex: 1, width: '100%', minHeight: 480, border: 0, background: v('screen')}}
                />
            )}
        </Box>
    );
};
