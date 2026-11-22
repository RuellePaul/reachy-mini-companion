import {lazy, Suspense, useCallback, useEffect, useRef, useState} from 'react';
import {Box, Button, Portal, Snackbar, useMediaQuery} from '@mui/material';
import {api, errorText} from './api/client';
import type {CatalogEntry, SettingsField} from './api/schemas';
import {Boundary} from './Boundary';
import {Header} from './header/Header';
import {useWords} from './i18n';
import {Presence} from './header/Presence';
import {useLive} from './live';
import {Palette} from './palette/Palette';
import {runEntry} from './palette/run';
import {SettingsDrawer} from './settings/SettingsDrawer';
import {v} from './theme';
import {appLaunch, sceneLaunch, sceneOver, writeLaunch, type Launch, type Offer} from './launch';
import {ask, keeping, type Surface} from './surfaces';
import {Composer} from './transcript/Composer';
import {Transcript} from './transcript/Transcript';
import {FRAME} from './words';

// Each surface is its own chunk: the first paint pays neither three.js nor the editors. The studio's chunk is
// fetched once the dashboard is idle and kept: once there it mounts directly, without the ~300 ms React holds a
// lazy component's first reveal.
let studioModule: typeof import('./studio/Studio') | null = null;
const loadStudio = () => import('./studio/Studio').then((module) => (studioModule = module));
const LazyStudio = lazy(loadStudio);
const Scenes = lazy(() => import('./scenes/Scenes'));
const Apps = lazy(() => import('./apps/Apps'));

/**
 * One page: the robot's column (its picture, its state, its volume), the conversation beside it, the settings in a drawer,
 * and the workspaces (studio, scenes, apps) as full-screen dialogs over it. Cmd+K opens the palette, from which
 * everything else is reachable. On a phone the robot's column is a strip above the conversation.
 */
export const App = () => {
    const {on, onConversation, shown, settings: view} = useLive();
    const t = useWords(FRAME);
    const compact = !useMediaQuery('(min-width:900px)', {noSsr: true});
    const [settings, setSettings] = useState<{field?: SettingsField} | null>(null); // open, on that field
    const [paletteOpen, setPaletteOpen] = useState(false);
    const [surface, setSurface] = useState<Surface>(null);
    const [draft, setDraft] = useState<{text: string; asked: number} | null>(null); // for the composer to finish
    const [offer, setOffer] = useState<Offer | null>(null);
    const [losing, setLosing] = useState<'show' | 'close' | null>(null); // unsaved work would go: what confirms
    const voiced = useRef(new Set<string>()); // the apps the robot opened while they were being built
    const written = useRef(new Set<string>()); // the scenes asked by voice, shown once as they are written

    useEffect(() => {
        const idle = window.requestIdleCallback ?? ((run: () => void) => setTimeout(run, 1500));
        idle(() => void loadStudio());
    }, []);

    useEffect(() => {
        const onKey = (event: KeyboardEvent) => {
            if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
                event.preventDefault();
                setPaletteOpen((open) => !open);
            }
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, []);

    // What the robot launches shows in its surface at once, unless a surface holds unsaved work: then it is offered,
    // in place of any question asked about that work.
    const launch = useCallback((launched: Launch) => {
        if (launched && 'show' in launched) setSurface(launched.show);
        else if (launched) {
            setOffer(launched.offer);
            setLosing(null);
        }
    }, []);
    useEffect(
        () => on('apps', (event) => launch(appLaunch(event, shown(), voiced.current, keeping()))),
        [on, shown, launch]
    );
    useEffect(() => on('scene_build', (build) => launch(writeLaunch(build, written.current, keeping()))), [on, launch]);
    useEffect(
        () =>
            onConversation((event) => {
                setOffer((now) => (sceneOver(now, event) ? null : now)); // a scene over is no longer offered
                launch(sceneLaunch(event, shown(), keeping()));
            }),
        [onConversation, shown, launch]
    );

    // What the palette opens over unsaved work is asked first, as what the robot launches is: « Voir quand même »;
    // closing it too (Escape, « Fermer »): « Fermer quand même ». A move shown in the open studio replaces nothing
    // there: its tuning stays.
    const open = useCallback(
        (next: Surface) => {
            if (!keeping() || (next?.kind === 'studio' && surface?.kind === 'studio')) return setSurface(next);
            setOffer({text: '', surface: next});
            setLosing(next ? 'show' : 'close');
        },
        [surface]
    );
    const run = useCallback(
        (entry: CatalogEntry, preview: boolean) =>
            runEntry(entry, preview, {
                api,
                setSurface: open,
                openSettings: (field) => setSettings({field}),
                compose: (text) => setDraft({text, asked: ask()})
            }),
        [open]
    );

    // A stop refused keeps the scene's offer, its reason in place of its text: « Arrêter » again.
    const stopScene = () =>
        api.stopScene().then(
            () => setOffer(null),
            (err) => {
                setLosing(null);
                setOffer((now) => now && {...now, text: errorText(err)});
            }
        );

    const close = useCallback(() => open(null), [open]);
    const openSettings = useCallback(() => setSettings({}), []);
    return (
        <Box sx={{position: 'fixed', inset: 0, display: 'flex', flexDirection: compact ? 'column' : 'row'}}>
            <Presence compact={compact} />
            <Box
                component="main"
                sx={{flex: 1, minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column', bgcolor: v('page')}}
            >
                <Header
                    compact={compact}
                    onSettings={openSettings}
                    onPalette={() => setPaletteOpen(true)}
                    onSurface={setSurface}
                />
                <Transcript railed={!compact} />
                <Composer draft={draft} />
            </Box>
            <SettingsDrawer open={settings !== null} field={settings?.field} onClose={() => setSettings(null)} />
            <Palette open={paletteOpen} onClose={() => setPaletteOpen(false)} onRun={run} name={view?.settings.name} />
            {surface && (
                // A surface that cannot load or render fails alone: the conversation and the robot's column stay.
                <Boundary key={surface.kind} title={t.surfaceFailed} onClose={close}>
                    <Suspense fallback={null}>
                        {surface.kind === 'studio' &&
                            (studioModule ? (
                                <studioModule.default move={surface.move} onClose={close} />
                            ) : (
                                <LazyStudio move={surface.move} onClose={close} />
                            ))}
                        {surface.kind === 'scenes' && (
                            <Scenes
                                sceneId={surface.sceneId}
                                buildId={surface.buildId}
                                asked={surface.asked}
                                playing={surface.playing}
                                onClose={close}
                            />
                        )}
                        {surface.kind === 'apps' && (
                            <Apps appId={surface.appId} asked={surface.asked} onClose={close} />
                        )}
                    </Suspense>
                </Boundary>
            )}
            {/* At the page's root, as the dialogs are: inside the page's fixed box, it would show under them. */}
            <Portal>
                <Snackbar
                    open={offer !== null}
                    message={losing ? t.losing : offer?.text}
                    onClose={(_, why) => why !== 'clickaway' && setOffer(null)}
                    slotProps={{transition: {onExited: () => setLosing(null)}}}
                    autoHideDuration={20_000}
                    anchorOrigin={{vertical: 'bottom', horizontal: 'center'}}
                    action={
                        <>
                            {offer?.stop && (
                                <Button color="inherit" onClick={stopScene}>
                                    {t.stop}
                                </Button>
                            )}
                            <Button
                                color="inherit"
                                onClick={() => {
                                    if (keeping() && !losing) return setLosing('show');
                                    if (offer) setSurface(offer.surface);
                                    setOffer(null);
                                }}
                            >
                                {losing === 'show' ? t.showAnyway : losing === 'close' ? t.closeAnyway : t.show}
                            </Button>
                        </>
                    }
                />
            </Portal>
        </Box>
    );
};
