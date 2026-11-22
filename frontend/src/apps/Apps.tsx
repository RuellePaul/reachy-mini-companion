// The apps surface: the list, a field to ask for a new app, and the open app with its actions. Loaded on demand.
import {useCallback, useEffect, useRef, useState, type ReactNode} from 'react';
import {
    Alert,
    Box,
    Button,
    Dialog,
    DialogActions,
    DialogTitle,
    IconButton,
    List,
    ListItemButton,
    Stack,
    Tooltip,
    Typography,
    useMediaQuery,
    useTheme
} from '@mui/material';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import DeleteOutlineRounded from '@mui/icons-material/DeleteOutlineRounded';
import DriveFileRenameOutlineRounded from '@mui/icons-material/DriveFileRenameOutlineRounded';
import RecordVoiceOverRounded from '@mui/icons-material/RecordVoiceOverRounded';
import RestoreRounded from '@mui/icons-material/RestoreRounded';
import VoiceOverOffRounded from '@mui/icons-material/VoiceOverOffRounded';
import {ApiError, errorText} from '../api/client';
import {Ask} from '../Ask';
import type {AppEvent, AppSummary} from '../api/schemas';
import {useLive, useShowing} from '../live';
import {Signal} from '../Signal';
import {useWords, wordsNow} from '../i18n';
import {Sticker} from '../Sticker';
import {SurfaceBar} from '../SurfaceBar';
import type {SurfaceProps} from '../surfaces';
import {v, type Token} from '../theme';
import {appsApi} from './api';
import {AppFrame} from './AppFrame';
import {Build} from './Build';
import {APPS} from './words';

const TONE: Record<AppSummary['status'], Token> = {building: 'attention', ready: 'listen', failed: 'fault'};

const CANCELLED = 'Modification annulée.'; // mac_server/apps.CANCELLED: the page was left as it was
/** The viewer's calendar day of an instant, as a number of days: a local midnight starts the next one. */
const dayOf = (seconds: number) => {
    const at = new Date(seconds * 1000);
    return Date.UTC(at.getFullYear(), at.getMonth(), at.getDate()) / 86_400_000;
};
/** « aujourd'hui », « hier », « il y a 3 jours », then « le 20 oct. », with its year when it is not this one; in
 * English, « today », « yesterday », « 3 days ago », then « on Oct 20 ». */
export const when = (seconds: number, now = Date.now() / 1000): string => {
    const t = wordsNow(APPS);
    const ago = dayOf(now) - dayOf(seconds);
    if (ago < 7) return new Intl.RelativeTimeFormat(t.dates, {numeric: 'auto'}).format(-Math.max(0, ago), 'day');
    const at = new Date(seconds * 1000);
    const year = at.getFullYear() === new Date(now * 1000).getFullYear() ? undefined : 'numeric';
    return t.onDay(at.toLocaleDateString(t.dates, {day: 'numeric', month: 'short', year}));
};

/** An app's state: said only when it is not simply ready. */
const State = ({app}: {app: AppSummary}) => {
    const t = useWords(APPS);
    return app.status === 'ready' ? null : (
        <Signal tone={TONE[app.status]} label={t.status[app.status]} busy={app.status === 'building'} />
    );
};

/** The list after an `apps` event: the app replaced or added, or removed; newest change first, as the Mac sorts. */
export const applyEvent = (apps: AppSummary[], event: AppEvent): AppSummary[] => {
    const others = apps.filter((app) => app.id !== event.app.id);
    return event.deleted ? others : [...others, event.app].sort((a, b) => b.updated_at - a.updated_at);
};

const Note = ({children, pictured = false}: {children: ReactNode; pictured?: boolean}) => (
    <Stack alignItems="center" justifyContent="center" spacing={2} sx={{flex: 1, p: 3, color: v('ink-3')}}>
        {pictured && <Sticker name="apps" size={152} />}
        <Typography sx={{maxWidth: 360, textAlign: 'center'}}>{children}</Typography>
    </Stack>
);

/** The open app: title, status, actions, and the page itself once it has one. */
const Viewer = ({app, onBack, onDone}: {app: AppSummary; onBack?: () => void; onDone: (app: AppSummary) => void}) => {
    const t = useWords(APPS);
    const version = `${app.id}:${app.updated_at}`;
    const shipped = app.builtin && !app.edited; // as shipped with the robot: nothing of yours to delete
    const [page, setPage] = useState<{version: string; html: string | null; error: string}>();
    const [renaming, setRenaming] = useState(false);
    const [deleting, setDeleting] = useState(false);
    const [error, setError] = useState('');

    useEffect(() => {
        let current = true;
        appsApi.html(app.id).then(
            (html) => current && setPage({version, html, error: ''}),
            (err: unknown) => {
                const missing = err instanceof ApiError && err.status === 404; // still being built, or failed
                if (current) setPage({version, html: null, error: missing ? '' : errorText(err)});
            }
        );
        return () => {
            current = false;
        };
    }, [app.id, version]);

    const shown = page?.version === version ? page : null;
    const remove = () => {
        setDeleting(false);
        appsApi.remove(app.id).catch((err: unknown) => setError(errorText(err)));
    };
    const toggleQuiet = () => {
        appsApi.setQuiet(app.id, !app.quiet).then(onDone, (err: unknown) => setError(errorText(err)));
    };
    const failure =
        app.status === 'failed' || app.error === CANCELLED ? app.error : app.error && t.changeFailed(app.error);
    return (
        <Box
            sx={{
                flex: 1,
                display: 'flex',
                flexDirection: 'column',
                minWidth: 0,
                minHeight: 0,
                px: {xs: 1.5, md: 3},
                pb: {xs: 1.5, md: 3}
            }}
        >
            <Stack direction="row" alignItems="center" spacing={1} sx={{pt: 2, pb: 1.5}}>
                {onBack && (
                    <IconButton aria-label={t.back} onClick={onBack} sx={{ml: -1}}>
                        <ArrowBackRounded />
                    </IconButton>
                )}
                <Box sx={{minWidth: 0}}>
                    <Typography variant="h3" component="h3" noWrap>
                        {app.title}
                    </Typography>
                    <Stack
                        direction="row"
                        columnGap={1.5}
                        alignItems="center"
                        useFlexGap
                        sx={{flexWrap: 'wrap', color: v('ink-3')}}
                    >
                        <State app={app} />
                        <Typography variant="caption">
                            {shipped ? t.shipped : t.changed(when(app.updated_at))}
                        </Typography>
                    </Stack>
                </Box>
                <Box sx={{flex: 1}} />
                <Tooltip title={app.quiet ? t.quiet : t.listens}>
                    <IconButton aria-label={t.quietLabel} aria-pressed={app.quiet} onClick={toggleQuiet}>
                        {app.quiet ? <VoiceOverOffRounded /> : <RecordVoiceOverRounded />}
                    </IconButton>
                </Tooltip>
                <Tooltip title={t.rename}>
                    <IconButton aria-label={t.rename} onClick={() => setRenaming((on) => !on)}>
                        <DriveFileRenameOutlineRounded />
                    </IconButton>
                </Tooltip>
                {!shipped && (
                    <Tooltip title={app.builtin ? t.restoreShipped : t.delete}>
                        <IconButton aria-label={app.builtin ? t.restore : t.delete} onClick={() => setDeleting(true)}>
                            {app.builtin ? <RestoreRounded /> : <DeleteOutlineRounded />}
                        </IconButton>
                    </Tooltip>
                )}
            </Stack>
            <Box sx={{pb: 1.5}}>
                {renaming && (
                    <Ask
                        key="rename"
                        label={t.newName}
                        action={t.rename}
                        initial={app.title}
                        max={60}
                        onCancel={() => setRenaming(false)}
                        onSend={async (title) => {
                            onDone(await appsApi.rename(app.id, title));
                            setRenaming(false);
                        }}
                    />
                )}
                {/* Hidden, never unmounted: a change typed and not sent outlives a rename and a build. */}
                <Box hidden={renaming || app.status === 'building'}>
                    <Ask
                        label={t.whatToChange}
                        action={t.change}
                        onSend={async (request) => onDone(await appsApi.update(app.id, request))}
                    />
                </Box>
            </Box>
            {(error || failure) && (
                <Alert severity={app.status === 'failed' ? 'error' : 'warning'} sx={{mb: 1.5}}>
                    {error || failure}
                </Alert>
            )}
            <Box
                sx={{
                    flex: 1,
                    minHeight: 0,
                    display: 'flex',
                    flexDirection: 'column',
                    borderRadius: '14px',
                    overflow: 'hidden',
                    border: `1px solid ${v('line')}`,
                    bgcolor: v('sunken')
                }}
            >
                {app.status === 'building' && shown?.html && <Build id={app.id} compact />}
                {shown?.html ? (
                    <AppFrame key={version} id={app.id} title={app.title} html={shown.html} />
                ) : app.status === 'building' ? (
                    <Build id={app.id} />
                ) : (
                    <Note>{shown?.error ?? ''}</Note>
                )}
            </Box>
            <Dialog open={deleting} onClose={() => setDeleting(false)}>
                <DialogTitle>{t.removeAsk(app.title, app.builtin)}</DialogTitle>
                <DialogActions>
                    <Button color="inherit" onClick={() => setDeleting(false)}>
                        {t.cancel}
                    </Button>
                    <Button variant="contained" color={app.builtin ? 'primary' : 'error'} onClick={remove}>
                        {app.builtin ? t.restore : t.delete}
                    </Button>
                </DialogActions>
            </Dialog>
        </Box>
    );
};

/** Opened on `appId` when an app was asked for (`asked` numbers each ask: asked again, it shows again); asked without
 * one, on the field to describe a new app. Until the next ask, the list decides. */
export default function Apps({onClose, appId, asked}: SurfaceProps & {appId?: string; asked?: number}) {
    const {on, link} = useLive();
    const t = useWords(APPS);
    const narrow = useMediaQuery(useTheme().breakpoints.down('md'));
    const [apps, setApps] = useState<AppSummary[] | null>(null);
    const [error, setError] = useState('');
    const [pick, setPick] = useState({id: appId ?? null, asked});
    const selected = pick.asked === asked ? pick.id : (appId ?? null); // a new ask wins
    const choose = (id: string | null) => setPick({id, asked});
    // A new callback at each ask: React hands it the field again, so an ask while the field is already there focuses
    // it. Once per ask: back to the list on a phone, the keyboard stays down.
    const focused = useRef<number>(undefined);
    const describe = useCallback(
        (field: HTMLTextAreaElement | null) => {
            if (!field || asked === undefined || appId || focused.current === asked) return;
            focused.current = asked;
            field.focus();
        },
        [asked, appId]
    );

    const listed = useCallback((list: AppSummary[]) => {
        setApps(list);
        setError(''); // read: the reason an earlier read failed goes
    }, []);
    // The events heard while the list is read: its answer may be older than they are, so they are applied over it.
    const heard = useRef<AppEvent[] | null>(null);
    const reload = useCallback(() => {
        const events: AppEvent[] = [];
        heard.current = events;
        const latest = () => heard.current === events; // else a later read answers
        return appsApi
            .list()
            .then(
                (list) => latest() && listed(events.reduce(applyEvent, list)),
                (err: unknown) => latest() && setError(errorText(err))
            )
            .finally(() => {
                if (latest()) heard.current = null;
            });
    }, [listed]);
    useEffect(() => {
        if (link === 'up') void reload(); // at opening, and after a reconnect that may have missed events
    }, [link, reload]);
    useEffect(
        () =>
            on('apps', (event) => {
                heard.current?.push(event);
                setApps((list) => list && applyEvent(list, event));
            }),
        [on]
    );

    const upsert = (app: AppSummary) => {
        setApps((list) => applyEvent(list ?? [], {app, open: false, deleted: false}));
        choose(app.id);
    };
    const app = apps?.find((one) => one.id === selected) ?? null;
    useShowing('app', app?.id ?? null);
    return (
        <Dialog open fullScreen onClose={onClose}>
            <SurfaceBar title={t.title} sticker="apps" onClose={onClose} />
            <Box sx={{flex: 1, display: 'flex', minHeight: 0}}>
                {!(narrow && app) && (
                    <Stack
                        sx={{
                            flexShrink: 0,
                            width: narrow ? 1 : 320,
                            bgcolor: v('plate'),
                            borderRight: narrow ? 0 : `1px solid ${v('line')}`
                        }}
                    >
                        <Box sx={{p: 2, pb: 1}}>
                            <Ask
                                label={t.describe}
                                stacked
                                inputRef={describe}
                                action={t.create}
                                onSend={async (r) => upsert(await appsApi.create(r))}
                            />
                        </Box>
                        {error && (
                            <Alert severity="error" sx={{mx: 2}}>
                                {error}
                            </Alert>
                        )}
                        {apps?.length === 0 && <Note>{t.none}</Note>}
                        <List sx={{overflowY: 'auto', px: 1, py: 1}}>
                            {apps?.map((one) => (
                                <ListItemButton
                                    key={one.id}
                                    selected={one.id === selected}
                                    onClick={() => choose(one.id)}
                                    sx={{gap: 1.5, py: 1, px: 1.5, alignItems: 'baseline'}}
                                >
                                    <Typography variant="body2" sx={{flex: 1, minWidth: 0, fontWeight: 600}}>
                                        {one.title}
                                    </Typography>
                                    {one.status === 'ready' ? (
                                        <Typography variant="caption" sx={{color: v('ink-3'), flexShrink: 0}}>
                                            {one.builtin && !one.edited ? t.shippedShort : when(one.updated_at)}
                                        </Typography>
                                    ) : (
                                        <State app={one} />
                                    )}
                                </ListItemButton>
                            ))}
                        </List>
                    </Stack>
                )}
                {app ? (
                    <Viewer
                        key={app.id} // another app, other fields: nothing typed for one is sent to the next
                        app={app}
                        onBack={narrow ? () => choose(null) : undefined}
                        onDone={upsert}
                    />
                ) : (
                    !narrow && <Note pictured>{t.choose}</Note>
                )}
            </Box>
        </Dialog>
    );
}
