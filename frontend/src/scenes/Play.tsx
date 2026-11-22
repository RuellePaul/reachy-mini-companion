// The robot's play: one button, « Jouer sur le robot » or « Arrêter », whole or rehearsed (its menu, the options
// in force shown beside it). Asked while the scene's voices are being rendered, the play waits for them on the
// `scene_render` channel, then is asked again on its own; and again after a reconnect, which may have missed them.
import {useEffect, useImperativeHandle, useRef, useState, type Ref} from 'react';
import {Alert, Button, ButtonGroup, Checkbox, Chip, CircularProgress, ListItemIcon, ListSubheader} from '@mui/material';
import {Menu, MenuItem, Stack, Typography} from '@mui/material';
import ArrowDropDownRounded from '@mui/icons-material/ArrowDropDownRounded';
import CheckRounded from '@mui/icons-material/CheckRounded';
import SmartToyRounded from '@mui/icons-material/SmartToyRounded';
import StopRounded from '@mui/icons-material/StopRounded';
import {api, ApiError, errorText} from '../api/client';
import type {SceneOptions} from '../api/schemas';
import {useWords, wordsNow} from '../i18n';
import {useLive} from '../live';
import {v} from '../theme';
import {SCENES} from './words';

type Asked = [id: string, options: SceneOptions];

const rendering = (err: unknown) => err instanceof ApiError && err.status === 409 && /se préparent/.test(err.message);
/** The play queued (true), or refused while its voices render (the Mac's 409 « se préparent »): to ask again. */
const queued = (asked: Asked) =>
    api.playScene(...asked).then(
        () => true,
        (err) => (rendering(err) ? false : Promise.reject(err))
    );

interface PlayProps {
    saved: () => Promise<string>; // saves the draft if it changed; the id to play
    cast: string[]; // the characters the human may say (« Répéter »)
    playing: boolean; // the robot plays this scene
    onPlaying: (playing: boolean) => void; // what the robot is about to do, before it says so
    ref?: Ref<Player>;
}

/** What the editor asks of the play: the scene from one of its lines (a click on its number), with the options set. */
export interface Player {
    playFrom: (line: number) => void;
}

/** Mounted per scene the user picks: another scene starts with its own options. Saving a new scene (which gives it
 * its id) keeps it mounted, so a first « Jouer » shows its answer. */
export const Play = ({saved, cast, playing, onPlaying, ref}: PlayProps) => {
    const {on, link} = useLive();
    const t = useWords(SCENES);
    const [options, setOptions] = useState<SceneOptions>({moves: true, rehearse: null});
    const [waiting, setWaiting] = useState<Asked | null>(null); // a play being asked
    const [refused, setRefused] = useState(false); // ...that waits for its voices
    const [message, setMessage] = useState<string | null>(null);
    const [menu, setMenu] = useState<HTMLElement | null>(null);
    const reask = useRef<(() => void) | null>(null); // asks for the play waiting, if any

    const run = async (action: () => Promise<unknown>) => {
        setMessage(null);
        setWaiting(null);
        setRefused(false);
        await action().catch((err) => setMessage(errorText(err)));
    };
    const play = (start: number | null = null) => run(async () => setWaiting([await saved(), {...options, start}]));
    useImperativeHandle(ref, () => ({playFrom: play}));
    const stop = () =>
        run(async () => {
            onPlaying(false);
            await api.stopScene();
        });
    const pick = (change: Partial<SceneOptions>) => {
        setOptions((o) => ({...o, ...change}));
        setMenu(null);
    };

    // Listening first, then asking: a render that ends meanwhile is not missed. One request at a time (a final frame
    // that comes during one asks again after it); a play refused once more keeps waiting; nothing is heard or asked
    // once this play is over (cancelled, or replaced by another).
    useEffect(() => {
        if (!waiting) return;
        let open = true;
        let asking = false;
        let again = false;
        const ask = () => {
            if (!open) return;
            if (asking) return void (again = true);
            [asking, again] = [true, false];
            queued(waiting).then(
                (played) => {
                    asking = false;
                    if (!open) return;
                    if (!played && again) return ask();
                    setRefused(!played);
                    setMessage(played ? null : wordsNow(SCENES).voicesComing);
                    if (played) setWaiting(null);
                    if (played) onPlaying(true);
                },
                (err) => {
                    if (!open) return;
                    setWaiting(null);
                    setMessage(errorText(err));
                }
            );
        };
        const off = on('scene_render', ({scene_id, done, total, error}) => {
            if (scene_id !== waiting[0] || (done < total && !error)) return;
            if (!error) return ask();
            setWaiting(null);
            setMessage(error);
        });
        reask.current = ask;
        ask();
        return () => {
            open = false;
            reask.current = null;
            off();
        };
    }, [on, waiting, onPlaying]);

    // The last frame of a render that ended while the socket was down went to nobody: back up, the play asks again.
    const up = link === 'up';
    const down = useRef(!up);
    useEffect(() => {
        if (up && down.current) reask.current?.();
        down.current = !up;
    }, [up]);

    return (
        <Stack spacing={0.75} alignItems="flex-end" sx={{minWidth: 0}}>
            <Stack direction="row" spacing={1} useFlexGap alignItems="center" sx={{flexWrap: 'wrap'}}>
                {options.rehearse && (
                    <Chip size="small" label={t.rehearsal(options.rehearse)} onDelete={() => pick({rehearse: null})} />
                )}
                {!options.moves && <Chip size="small" label={t.noMoves} onDelete={() => pick({moves: true})} />}
                {playing ? (
                    <Button
                        variant="contained"
                        startIcon={<StopRounded />}
                        onClick={stop}
                        sx={{
                            bgcolor: v('fault'),
                            color: v('on-signal'),
                            '&:hover': {bgcolor: v('fault'), opacity: 0.9}
                        }}
                    >
                        {t.stop}
                    </Button>
                ) : waiting && refused ? (
                    <Button
                        variant="outlined"
                        color="inherit"
                        startIcon={<CircularProgress size={16} />}
                        onClick={() => run(async () => {})}
                    >
                        {t.voicesCancel}
                    </Button>
                ) : (
                    <ButtonGroup variant="contained" disabled={!!waiting}>
                        <Button
                            startIcon={waiting ? <CircularProgress size={16} /> : <SmartToyRounded />}
                            onClick={() => play()}
                        >
                            {t.playOnRobot}
                        </Button>
                        <Button
                            aria-label={t.playOptions}
                            onClick={(e) => setMenu(e.currentTarget)}
                            sx={{px: 0.5, minWidth: 36, borderLeftColor: `${v('ink-2')} !important`}}
                        >
                            <ArrowDropDownRounded />
                        </Button>
                    </ButtonGroup>
                )}
            </Stack>
            <Menu anchorEl={menu} open={!!menu} onClose={() => setMenu(null)}>
                <ListSubheader>{t.rehearse}</ListSubheader>
                {[null, ...cast].map((name) => (
                    <MenuItem key={name ?? ''} dense onClick={() => pick({rehearse: name})}>
                        <ListItemIcon>{options.rehearse === name && <CheckRounded fontSize="small" />}</ListItemIcon>
                        {name ?? t.nobody}
                    </MenuItem>
                ))}
                <MenuItem dense onClick={() => pick({moves: !options.moves})}>
                    <ListItemIcon>
                        <Checkbox size="small" edge="start" checked={!options.moves} tabIndex={-1} disableRipple />
                    </ListItemIcon>
                    {t.noExpressive}
                </MenuItem>
            </Menu>
            {options.rehearse && (
                <Typography variant="caption" sx={{color: v('ink-2'), textAlign: 'right', maxWidth: 360}}>
                    {t.rehearseHint(options.rehearse)}
                </Typography>
            )}
            {message && (
                <Alert severity={refused ? 'info' : 'warning'} onClose={() => setMessage(null)} sx={{py: 0}}>
                    {message}
                </Alert>
            )}
        </Stack>
    );
};
