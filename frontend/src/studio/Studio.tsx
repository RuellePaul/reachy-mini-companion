// The studio (loaded on demand): the robot in 3D, playing a move before the real robot does (Aperçu, sampled by the
// Mac from the robot's own motion code) or as measured live (Miroir), and the tuning of each mode's animation.
import {useEffect, useRef, useState} from 'react';
import {Box, Button, Dialog, IconButton, Stack, ToggleButton, Tooltip, Typography, useMediaQuery} from '@mui/material';
import KeyboardRounded from '@mui/icons-material/KeyboardRounded';
import ThreeSixtyRounded from '@mui/icons-material/ThreeSixtyRounded';
import VideocamRounded from '@mui/icons-material/VideocamRounded';
import VerticalAlignBottomRounded from '@mui/icons-material/VerticalAlignBottomRounded';
import ViewInArRounded from '@mui/icons-material/ViewInArRounded';
import {Matrix4} from 'three';
import {api, errorText} from '../api/client';
import type {CatalogEntry, PreviewFrames, Settings, TryMove} from '../api/schemas';
import {useWords} from '../i18n';
import {useLive} from '../live';
import {mac} from '../mac';
import {SurfaceBar} from '../SurfaceBar';
import {useKeep, type SurfaceProps} from '../surfaces';
import {v} from '../theme';
import {useMirror, usePreviews} from './api';
import type {View} from './bench';
import {Clock} from './clock';
import {Choice, glass, Panel, type Item, type Mode, type Notice, type Shelf, type Tuning} from './Panel';
import {NEUTRAL_ANTENNAS} from './rig';
import {Stage} from './RobotView';
import {Timeline} from './Timeline';
import {STUDIO} from './words';

const VIEWS: [View, string, typeof VideocamRounded][] = [
    ['front', '1', VideocamRounded],
    ['side', '2', ViewInArRounded],
    ['top', '3', VerticalAlignBottomRounded],
    ['free', '4', ThreeSixtyRounded]
];
const REST: PreviewFrames = {hz: 30, head: [new Matrix4().toArray()], antennas: [NEUTRAL_ANTENNAS], body: [0]};

// An animation previews (and is tried) for 8 s, its mode's tuning applied.
const animation = (mode: Mode, name: string, tuning: Tuning): TryMove => ({
    kind: 'animation',
    name,
    mode,
    tuning,
    seconds: 8
});
const shelfOf = (move?: TryMove | null): Shelf => (move?.kind === 'animation' || !move ? 'animations' : move.kind);
const isField = (target: EventTarget | null) =>
    target instanceof Element && !!target.closest('input, textarea, select');

export default function Studio({onClose, move}: SurfaceProps & {move?: TryMove}) {
    const {settings: view, session, link, saveSettings} = useLive();
    const t = useWords(STUDIO);
    const wide = useMediaQuery('(min-width:900px)');
    const [shelf, setShelf] = useState<Shelf>(shelfOf(move));
    const [mode, setMode] = useState<Mode>(move?.mode ?? 'idle');
    const [chosen, setChosen] = useState<TryMove | null>(move ?? null);
    const [drafts, setDrafts] = useState<Partial<Record<Mode, Tuning>>>({});
    const [mirroring, setMirroring] = useState(false);
    const [ghost, setGhost] = useState(false);
    const [compare, setCompare] = useState(false);
    const [camera, setCamera] = useState<View>('front');
    const [help, setHelp] = useState(false);
    const [notice, setNotice] = useState<Notice | null>(null);
    const [entries, setEntries] = useState<CatalogEntry[]>([]);
    const [clock] = useState(() => new Clock());
    const mirror = useMirror(mirroring || ghost);

    // The palette's Shift+Enter while the studio is open: show that move.
    const [opened, setOpened] = useState(move);
    if (move !== opened) {
        setOpened(move);
        if (move) {
            setChosen(move);
            setShelf(shelfOf(move));
            setMirroring(false);
            setCompare(false);
            if (move.mode) setMode(move.mode);
        }
    }

    useEffect(() => {
        performance.mark('studio:open');
        api.catalog().then(
            (catalog) => setEntries(catalog.entries),
            (err: unknown) => setNotice({text: errorText(err), error: true})
        );
    }, []);

    const saved = view?.settings;
    const tuningOf = (m: Mode): Tuning => drafts[m] ?? saved?.tuning[m] ?? {amplitude: 1, speed: 1};
    // A tuning moved and not kept is unsaved work; a variant only looked at is not.
    useKeep(
        Object.entries(drafts).some(([m, t]) => t && JSON.stringify(t) !== JSON.stringify(saved?.tuning[m as Mode]))
    );
    const currentOf = (m: Mode) => saved?.[`${m}_animation` as const];
    // An animation plays with its mode's tuning as drafted here (the palette sends the defaults).
    const pick: TryMove | null =
        chosen?.kind === 'animation' && chosen.mode
            ? animation(chosen.mode, chosen.name, tuningOf(chosen.mode))
            : (chosen ?? (saved ? animation('idle', saved.idle_animation, tuningOf('idle')) : null));
    const items: Item[] =
        shelf === 'animations'
            ? (view?.options.animations[mode] ?? []).map(({key, label}) => {
                  const move = animation(mode, key, tuningOf(mode));
                  return {key, label: mac(label), current: key === currentOf(mode), move};
              })
            : entries.flatMap(({action, label, say}) =>
                  action.kind === 'try' && action.move.kind === shelf
                      ? [{key: action.move.name, label: mac(label), hint: say, move: action.move}]
                      : []
              );
    const selected = items.findIndex(
        ({move: m}) => m.kind === pick?.kind && m.name === pick.name && m.mode === pick.mode
    );
    const comparing = compare && shelf === 'animations' && !mirroring;
    const previews = usePreviews(mirroring || !pick ? [] : comparing ? items.map((i) => i.move) : [pick]);
    const duration = Math.max(0, ...previews.frames.map((f) => (f.head.length - 1) / f.hz));
    useEffect(() => clock.restart(duration), [clock, previews.frames, duration]);

    const choose = (item?: Item) => {
        if (item) setChosen(item.move);
        setNotice(null);
    };
    const showAll = (all: boolean) => {
        setShelf('animations');
        setMirroring(false);
        setCompare(all);
    };
    const changeMode = (m: Mode) => {
        setMode(m);
        setChosen(animation(m, currentOf(m) ?? '', tuningOf(m)));
    };
    const run = async (action: () => Promise<unknown>, done: Notice) => {
        setNotice(null);
        try {
            await action();
            setNotice(done);
        } catch (err) {
            setNotice({text: errorText(err), error: true});
        }
    };
    const tryOnRobot = () => pick && run(() => api.tryMove(pick), {text: t.played});
    const keepable =
        pick?.kind === 'animation' && pick.mode && saved && link === 'up'
            ? pick.name !== currentOf(pick.mode) ||
              JSON.stringify(pick.tuning) !== JSON.stringify(saved.tuning[pick.mode])
            : false;
    const keep = () => {
        if (pick?.kind !== 'animation' || !pick.mode || !saved) return;
        const m = pick.mode;
        const changes: Partial<Settings> = {tuning: {...saved.tuning, [m]: pick.tuning}};
        changes[`${m}_animation` as const] = pick.name;
        const save = async () => {
            await saveSettings(changes);
            setDrafts((d) => ({...d, [m]: undefined}));
        };
        void run(save, {text: t.kept, restart: true});
    };
    const restart = () => run(() => api.session('restart'), {text: t.restartAsked});

    // Keyboard: read through a ref, so the listener stays put while the handler sees the current state.
    const onKey = (event: KeyboardEvent) => {
        if (event.metaKey || event.ctrlKey || event.altKey || isField(event.target)) return;
        const key = event.key.toLowerCase();
        const step = {arrowup: -1, arrowdown: 1}[key];
        const preset = VIEWS.find(([, k]) => k === key);
        if (key === ' ') clock.toggle();
        else if (step) choose(items[(Math.max(selected, step > 0 ? -1 : 0) + step + items.length) % items.length]);
        else if (preset) setCamera(preset[0]);
        else if (key === 'c') showAll(!comparing);
        else if (key === 'm') setMirroring(!mirroring);
        else if (key === 'g') setGhost(!ghost);
        else if (key === 'e') void tryOnRobot();
        else if (key === '?') setHelp(!help);
        else return;
        event.preventDefault();
    };
    const keys = useRef(onKey);
    useEffect(() => {
        keys.current = onKey;
    });
    useEffect(() => {
        const down = (event: KeyboardEvent) => keys.current(event);
        // A focused button would take Space as a click too: in the studio, Space is play/pause.
        const up = (event: KeyboardEvent) => event.key === ' ' && !isField(event.target) && event.preventDefault();
        window.addEventListener('keydown', down, true);
        window.addEventListener('keyup', up, true);
        return () => {
            window.removeEventListener('keydown', down, true);
            window.removeEventListener('keyup', up, true);
        };
    }, []);

    const waiting = mirroring && mirror.status !== 'live';
    const ghostOn = {...glass, color: v('ghost'), boxShadow: `inset 0 0 0 1.5px ${v('ghost')}`};
    const comparingOn = {bgcolor: v('lens'), color: v('on-lens'), '&:hover': {bgcolor: v('ink-2')}};
    return (
        <Dialog open fullScreen onClose={(_, why) => (help && why === 'escapeKeyDown' ? setHelp(false) : onClose())}>
            <SurfaceBar title={t.title} sticker="studio" onClose={onClose} />
            <Stack
                direction={wide ? 'row' : 'column'}
                sx={{flex: 1, minHeight: 0, overflowY: wide ? 'hidden' : 'auto'}}
            >
                <Box sx={{position: 'relative', flex: wide ? 1 : 'none', minWidth: 0, height: wide ? '100%' : '52vh'}}>
                    <Stage
                        robots={mirroring ? [null] : previews.frames.length ? previews.frames : [REST]}
                        time={() => clock.now()}
                        pose={mirror.pose}
                        ghost={ghost && !mirroring && !comparing}
                        stale={waiting}
                        view={camera}
                        onOrbit={() => setCamera('free')}
                        labels={comparing ? items.map((i) => i.label) : []}
                        picked={comparing ? selected : null}
                        onPick={(i) => choose(items[i])}
                        height="100%"
                    />
                    <Stack
                        direction="row"
                        spacing={1}
                        useFlexGap
                        flexWrap="wrap"
                        alignItems="center"
                        sx={{position: 'absolute', inset: 16, bottom: 'auto'}}
                    >
                        <Choice
                            value={camera}
                            onChange={setCamera}
                            options={VIEWS.map(([view, key, Icon]) => [
                                view,
                                wide ? t.views[view] : <Icon fontSize="small" />,
                                `${t.views[view]} (${key})`
                            ])}
                        />
                        <Choice
                            value={mirroring}
                            onChange={setMirroring}
                            options={[
                                [false, t.preview, t.previewHint],
                                [true, t.mirror, t.mirrorHint]
                            ]}
                        />
                        {!mirroring && !comparing && (
                            <ToggleButton
                                size="small"
                                value="ghost"
                                selected={ghost}
                                onChange={() => setGhost(!ghost)}
                                sx={{...glass, px: 1.5, minHeight: 36, '&.Mui-selected, &.Mui-selected:hover': ghostOn}}
                                title={t.ghostHint}
                            >
                                {t.ghost}
                            </ToggleButton>
                        )}
                        <Tooltip
                            open={help}
                            onOpen={() => setHelp(true)}
                            onClose={() => setHelp(false)}
                            placement="bottom-end"
                            title={
                                <Box
                                    component="dl"
                                    sx={{
                                        m: 0,
                                        py: 0.5,
                                        display: 'grid',
                                        gridTemplateColumns: 'auto 1fr',
                                        gap: '4px 12px'
                                    }}
                                >
                                    {t.help.map(([keys, what]) => (
                                        <Box key={keys} sx={{display: 'contents'}}>
                                            <Box component="dt" sx={{fontWeight: 700, whiteSpace: 'nowrap'}}>
                                                {keys}
                                            </Box>
                                            <Box component="dd" sx={{m: 0}}>
                                                {what}
                                            </Box>
                                        </Box>
                                    ))}
                                </Box>
                            }
                        >
                            <IconButton aria-label={t.shortcuts} sx={{...glass, ml: 'auto', width: 36, height: 36}}>
                                <KeyboardRounded fontSize="small" />
                            </IconButton>
                        </Tooltip>
                    </Stack>
                    {waiting && (
                        <Typography
                            variant="body2"
                            sx={{...glass, position: 'absolute', bottom: 16, left: 16, px: 2, py: 1, fontWeight: 560}}
                        >
                            {mirror.status === 'waiting' ? t.waitingPose : t.stalePose}
                        </Typography>
                    )}
                    {!mirroring && (
                        <Stack
                            direction="row"
                            alignItems="center"
                            spacing={1}
                            sx={{...glass, position: 'absolute', inset: 16, top: 'auto', pl: 0.5, pr: 1}}
                        >
                            <Timeline clock={clock} />
                            {shelf === 'animations' && (
                                <Button
                                    size="small"
                                    color="inherit"
                                    onClick={() => showAll(!comparing)}
                                    title={t.compareHint}
                                    sx={comparing ? comparingOn : undefined}
                                >
                                    {t.compare}
                                </Button>
                            )}
                        </Stack>
                    )}
                </Box>
                <Panel
                    shelf={shelf}
                    onShelf={setShelf}
                    mode={mode}
                    onMode={changeMode}
                    items={items}
                    selected={selected}
                    onChoose={choose}
                    tuning={tuningOf(mode)}
                    ranges={view?.options.tuning[mode]}
                    onTuning={(t) => setDrafts((d) => ({...d, [mode]: t}))}
                    busy={previews.busy}
                    notice={previews.error ? {text: previews.error, error: true} : notice}
                    onTry={() => void tryOnRobot()}
                    onKeep={keepable ? keep : undefined}
                    onRestart={notice?.restart && session.awake ? () => void restart() : undefined}
                />
            </Stack>
        </Dialog>
    );
}
