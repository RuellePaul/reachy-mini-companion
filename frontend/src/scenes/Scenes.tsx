// The scene editor (a full-screen dialog, loaded on demand): a field to ask for a scene (Claude writes it: its
// steps show in place of the editor while it is prepared), the scenes; the open one's title, its state and its
// « ⋮ » menu; one toolbar (« Ajouter des émotions », the robot's play); the text with its live analysis and its voices
// (rendered on every save).
import {useCallback, useEffect, useRef, useState} from 'react';
import {
    Alert,
    Box,
    Button,
    Dialog,
    DialogActions,
    DialogTitle,
    IconButton,
    LinearProgress,
    List,
    ListItemButton,
    Menu,
    MenuItem,
    Stack,
    TextField,
    Typography,
    useMediaQuery,
    useTheme
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import MoreVertRounded from '@mui/icons-material/MoreVertRounded';
import SaveRounded from '@mui/icons-material/SaveRounded';
import {api, errorText} from '../api/client';
import type {
    RobotEvent,
    SceneBuild,
    SceneCheck,
    SceneList,
    SceneOut,
    SceneRender,
    SceneVocabulary
} from '../api/schemas';
import {SCENE_REQUEST_MAX} from '../api/schemas';
import {Ask} from '../Ask';
import {useWords, wordsNow} from '../i18n';
import {useLive, useShowing} from '../live';
import {Signal} from '../Signal';
import {SurfaceBar} from '../SurfaceBar';
import {useKeep, type SurfaceProps} from '../surfaces';
import {v} from '../theme';
import {scenesApi} from './api';
import {Editor} from './Editor';
import {Emotions} from './Emotions';
import {Play, type Player} from './Play';
import {said, Writing} from './Writing';
import {SCENES} from './words';

const CHECK_MS = 300;
const ASKED_MS = 15_000; // a play queued and no « started » yet: the robot did not take it (the command's TTL is 30 s)

interface Stage {
    id: string;
    line: number | null;
    asked?: boolean;
    replacing?: boolean;
}

/** The stage once the robot says it plays `scene` (null: none); the line kept while the scene is the same. */
const onStage = (now: Stage | null, scene: string | null): Stage | null =>
    scene === null ? null : now?.id === scene ? now : {id: scene, line: null};

/** The stage once a snapshot says the robot plays `scene`. A play asked waits for the robot's answer, unless the
 * snapshot is that answer: the asked scene on stage, and no run of it replaced (a jump) that could still be the one. */
const followed = (now: Stage | null, scene: string | null): Stage | null =>
    !now?.asked ? onStage(now, scene) : scene === now.id && !now.replacing ? {id: scene, line: null} : now;

/** A line as it is said: without its sounds (<rire>). */
const spoken = (text: string | null) =>
    (text ?? '')
        .replace(/<[^>]*>/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();

/** What the robot does with a scene, in the language shown when it does it. */
const robotLine = (event: Extract<RobotEvent, {kind: 'scene'}>, who = ''): string => {
    const t = wordsNow(SCENES).robot;
    return {
        started: t.started(event.title),
        line: t.line(event.title, event.line),
        waiting: t.waiting(who, spoken(event.message)),
        ended: t.ended(event.title),
        stopped: t.stopped(event.title, event.message),
        failed: t.failed(event.title, event.message)
    }[event.state];
};

/** The builds after a `scene_build` frame: that build replaced, or added first. */
const built = (builds: SceneBuild[], build: SceneBuild): SceneBuild[] =>
    builds.some((b) => b.id === build.id) ? builds.map((b) => (b.id === build.id ? build : b)) : [build, ...builds];

/** Opened on `sceneId` (the palette's « Ouvrir », or a scene the robot plays: `playing`) or on `buildId` (a scene
 * the robot writes at someone's ask); asked again while open (a new `asked`), it shows that one again. */
export default function Scenes({
    onClose,
    sceneId,
    buildId,
    asked,
    playing: launched
}: SurfaceProps & {sceneId?: string; buildId?: string; asked?: number; playing?: boolean}) {
    const {on, onConversation, session} = useLive();
    const t = useWords(SCENES);
    const mobile = useMediaQuery(useTheme().breakpoints.down('md'));
    const [scenes, setScenes] = useState<SceneList['scenes']>([]);
    const [id, setId] = useState<string | null>(sceneId ?? null);
    const [builds, setBuilds] = useState<SceneBuild[]>([]); // scenes being written, and those that ended meanwhile
    const [writing, setWriting] = useState<string | null>(buildId ?? null); // the build shown in place of the editor
    const build = builds.find((b) => b.id === writing) ?? null;
    const [draft, setDraft] = useState({title: '', text: ''});
    const [saved, setSaved] = useState({title: '', text: '', builtin: false, edited: false});
    const [[check, checked], setCheck] = useState<[SceneCheck | null, string]>([null, '']); // and the text it read
    const [vocabulary, setVocabulary] = useState<SceneVocabulary | null>(null);
    const [render, setRender] = useState<SceneRender | null>(null);
    const [rendered, setRendered] = useState(0); // voices rendered meanwhile: the check reads which lines are ready
    const [robot, setRobot] = useState<string | null>(null);
    // The scene the robot plays, whichever is open, and its line; `asked` while a play waits for its « started »,
    // `replacing` while the run it replaces (a jump) has not ended yet.
    const [stage, setStage] = useState<Stage | null>(() =>
        onStage(null, launched && sceneId ? sceneId : session.scene)
    );
    const playing = stage !== null && stage.id === id;
    const [more, setMore] = useState<HTMLElement | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const [pane, setPane] = useState(0); // the controls' key: a new one per scene the user picks, not per save
    const [deleting, setDeleting] = useState(false);
    const [leaving, setLeaving] = useState<(() => void) | null>(null); // another scene asked over unsaved changes
    const checks = useRef(0);
    const lists = useRef(0);
    const player = useRef<Player>(null);
    const changed = draft.title !== saved.title || draft.text !== saved.text; // what leaving it would lose
    const dirty = id === null || changed;
    const show = (next: string | null) => {
        setId(next);
        setWriting(null);
        setPane((n) => n + 1);
    };
    const [shown, setShown] = useState(asked);
    if (asked !== shown) {
        setShown(asked);
        if (buildId) setWriting(buildId);
        else if (sceneId) show(sceneId);
        if (sceneId && launched) setStage({id: sceneId, line: null});
    }
    // Each snapshot (a reconnect reads it again) says which scene plays: what an event missed meanwhile is put right,
    // and the caption of a scene it takes off the stage (or puts on) goes with it.
    const [snapshot, setSnapshot] = useState(session);
    if (session !== snapshot) {
        setSnapshot(session);
        setStage((now) => followed(now, session.scene));
        if (followed(stage, session.scene)?.id !== stage?.id) setRobot(null);
    }
    useShowing('scene', writing ? (build?.scene_id ?? null) : id);
    useKeep(changed);

    const fail = useCallback((err: unknown) => setNotice(errorText(err)), []);
    /** The scenes as the Mac has them now, the voices rendered meanwhile counted (an answer a later read overtook is
     * dropped); `open`: the first one shown if none is. */
    const refresh = useCallback((open = true) => {
        const asked = ++lists.current;
        return scenesApi.list().then((list) => {
            if (asked === lists.current) setScenes(list.scenes);
            if (open) setId((current) => current ?? list.scenes[0]?.id ?? null);
        });
    }, []);
    const loaded = useCallback((scene: SceneOut) => {
        setDraft({title: scene.title, text: scene.text});
        setSaved({title: scene.title, text: scene.text, builtin: scene.builtin, edited: scene.edited});
        setCheck([scene.check, scene.text]);
        setRender(null);
    }, []);

    useEffect(() => {
        refresh().catch(fail);
        scenesApi.vocabulary().then(setVocabulary, () => {});
        scenesApi.builds().then(
            (known) => setBuilds((now) => known.builds.reduceRight(built, now)),
            () => {}
        );
    }, [refresh, fail]);

    // A build's frames: the list reads the scenes again when one is stored, and when it ends.
    const stored = useRef(new Map<string, string>());
    useEffect(
        () =>
            on('scene_build', (frame) => {
                setBuilds((now) => built(now, frame));
                const mark = `${frame.scene_id}/${frame.status}/${frame.stage}`;
                if (frame.scene_id && stored.current.get(frame.id) !== mark) refresh(false).catch(() => {});
                stored.current.set(frame.id, mark);
            }),
        [on, refresh]
    );

    useEffect(() => {
        if (id === null) return;
        let current = true; // the answer for a scene left is dropped: its text never lands under another
        scenesApi.get(id).then(
            (scene) => current && loaded(scene),
            (err: unknown) => current && fail(err)
        );
        return () => {
            current = false;
        };
    }, [id, loaded, fail]);

    useEffect(() => {
        const asked = ++checks.current;
        const text = draft.text;
        const timer = setTimeout(() => {
            scenesApi.check(text).then(
                (fresh) => asked === checks.current && setCheck([fresh, text]),
                () => {}
            );
        }, CHECK_MS);
        return () => clearTimeout(timer);
    }, [draft.text, rendered]);

    useEffect(
        () =>
            on('scene_render', (progress) => {
                refresh(false).catch(() => {}); // every scene's row follows its render
                if (progress.scene_id !== id) return;
                setRender(progress);
                setCheck(([current, read]) => [current && {...current, rendered: progress.done}, read]);
                setRendered((n) => n + 1);
            }),
        [on, id, refresh]
    );

    useEffect(
        () =>
            onConversation((event) => {
                if (event.kind !== 'scene') return;
                const said = check?.cues.find((c) => c.kind === 'say' && c.line === event.line);
                setRobot(robotLine(event, said?.character ?? ''));
                const on = ['started', 'line', 'waiting'].includes(event.state);
                const line = event.state === 'line' || event.state === 'waiting' ? event.line : null;
                // an end while a play is asked: the run it replaces, once; else the play itself, refused or dropped
                setStage((now) =>
                    on ? {id: event.scene_id, line} : now?.replacing ? {...now, replacing: false} : null
                );
            }),
        [onConversation, check]
    );

    // Stable: the play's effect depends on it, and asks again whenever it changes. A play asked over a scene on stage
    // replaces it; over a play still asked, the run that one was replacing, if any (the asked play is dropped silently).
    const onPlaying = useCallback(
        (on: boolean) =>
            setStage((now) =>
                on && id ? {id, line: null, asked: true, replacing: !!now && (!now.asked || !!now.replacing)} : null
            ),
        [id]
    );

    useEffect(() => {
        if (!stage?.asked) return;
        const timer = setTimeout(() => {
            setStage((now) => (now?.asked ? null : now));
            setNotice(wordsNow(SCENES).notStarted);
        }, ASKED_MS);
        return () => clearTimeout(timer);
    }, [stage]);

    /** The draft saved if it changed (a built-in saves as a copy that shadows it); the scene's id. */
    const save = async (): Promise<string> => {
        if (!dirty && id !== null) return id;
        const scene = await scenesApi.save(id, draft.title.trim() || t.untitled, draft.text);
        setSaved({title: scene.title, text: scene.text, builtin: scene.builtin, edited: scene.edited});
        setDraft((d) => ({...d, title: scene.title})); // « Sans titre » for an empty one
        setCheck([scene.check, scene.text]);
        setId(scene.id);
        await refresh();
        return scene.id;
    };

    const attempt = (action: () => Promise<unknown>) => {
        setNotice(null);
        action().catch((err) => setNotice(errorText(err)));
    };

    const remove = () =>
        attempt(async () => {
            setDeleting(false);
            if (id === null) return;
            await scenesApi.remove(id);
            if (saved.builtin)
                loaded(await scenesApi.get(id)); // as shipped again
            else show(null);
            await refresh();
        });
    const create = () => {
        show(null);
        setDraft({title: t.newScene, text: t.newSceneText});
        setSaved({title: t.newScene, text: t.newSceneText, builtin: false, edited: false}); // untouched, nothing lost
    };
    /** Another scene, or a new one: asked first over unsaved changes. */
    const leave = (go: () => void) => (changed ? setLeaving(() => go) : go());
    /** A scene asked for: its build is under way whatever is shown; it shows once nothing unsaved is in the way. */
    const write = async (request: string) => {
        const started = await scenesApi.write(request);
        setBuilds((now) => built(now, started));
        leave(() => setWriting(started.id));
    };
    const playBuilt = (scene: string) =>
        attempt(async () => {
            await api.playScene(scene);
            show(scene);
            setStage({id: scene, line: null, asked: true});
        });

    const rendering = render !== null && render.done < render.total && !render.error;
    // In the list: the scenes being written, and the one whose build is shown, in place of its scene's own row.
    const listed = builds.filter((b) => b.status === 'building' || b.id === writing);
    const list = (
        <Stack
            sx={{
                flexShrink: 0,
                bgcolor: v('plate'),
                ...(mobile
                    ? {maxHeight: 200, borderBottom: `1px solid ${v('line')}`}
                    : {width: 280, borderRight: `1px solid ${v('line')}`})
            }}
        >
            <Stack spacing={1} sx={{p: 1.5, pb: 0.5}}>
                <Ask label={t.describe} action={t.write} stacked max={SCENE_REQUEST_MAX} onSend={write} />
                <Button fullWidth size="small" color="inherit" startIcon={<AddRounded />} onClick={() => leave(create)}>
                    {t.newScene}
                </Button>
            </Stack>
            <List sx={{flex: 1, overflow: 'auto', px: 1, py: 1}}>
                {listed.map((b) => (
                    <ListItemButton
                        key={b.id}
                        selected={b.id === writing}
                        onClick={() => b.id !== writing && leave(() => setWriting(b.id))}
                        sx={{flexDirection: 'column', alignItems: 'stretch', gap: 0.75, py: 1, px: 1.5}}
                    >
                        <Typography variant="body2" noWrap sx={{fontWeight: 600}}>
                            {b.title ?? b.request}
                        </Typography>
                        <Signal {...said(b)} />
                    </ListItemButton>
                ))}
                {scenes
                    .filter((scene) => !listed.some((b) => b.scene_id === scene.id))
                    .map((scene) => (
                        <ListItemButton
                            key={scene.id}
                            selected={!writing && scene.id === id}
                            onClick={() => (writing || scene.id !== id) && leave(() => show(scene.id))}
                            sx={{flexDirection: 'column', alignItems: 'stretch', gap: 0.75, py: 1, px: 1.5}}
                        >
                            <Typography variant="body2" sx={{fontWeight: 600}}>
                                {scene.title}
                            </Typography>
                            <Readiness errors={scene.errors} done={scene.rendered} total={scene.segments} />
                        </ListItemButton>
                    ))}
            </List>
        </Stack>
    );

    const status = !check
        ? ''
        : check.errors.length
          ? t.cannotPlay(t.errors(check.errors.length))
          : t.voicesReady(t.voices(check.rendered, check.segments), dirty);
    return (
        <Dialog open fullScreen onClose={onClose}>
            <SurfaceBar title={t.title} sticker="scenes" onClose={onClose} />
            <Box sx={{display: 'flex', flexDirection: mobile ? 'column' : 'row', flex: 1, minHeight: 0}}>
                {list}
                <Stack
                    spacing={2}
                    sx={{
                        flex: 1,
                        minWidth: 0,
                        minHeight: 0,
                        px: {xs: 2, md: 4},
                        pt: {xs: 2, md: 3},
                        pb: 2,
                        overflow: 'auto'
                    }}
                >
                    {build ? (
                        <Writing
                            build={build}
                            error={notice}
                            onCancel={() => attempt(() => scenesApi.cancel(build.id))}
                            onOpen={(scene) => leave(() => show(scene))}
                            onPlay={playBuilt}
                            onRetry={(request) => attempt(() => write(request))}
                        />
                    ) : (
                        <>
                            <Box>
                                <Stack direction="row" spacing={1} alignItems="center">
                                    <TextField
                                        variant="standard"
                                        placeholder={t.titleField}
                                        value={draft.title}
                                        onChange={(e) => setDraft((d) => ({...d, title: e.target.value}))}
                                        slotProps={{
                                            htmlInput: {maxLength: 120, 'aria-label': t.titleField},
                                            input: {disableUnderline: true}
                                        }}
                                        sx={{
                                            flex: 1,
                                            minWidth: 0,
                                            '& input': {
                                                fontSize: 24,
                                                fontWeight: 700,
                                                letterSpacing: '-0.01em',
                                                py: 0.25
                                            }
                                        }}
                                    />
                                    {dirty && (
                                        <Button
                                            variant="contained"
                                            startIcon={<SaveRounded />}
                                            onClick={() => attempt(save)}
                                        >
                                            {t.save}
                                        </Button>
                                    )}
                                    {id !== null && (!saved.builtin || saved.edited) && (
                                        <IconButton aria-label={t.more} onClick={(e) => setMore(e.currentTarget)}>
                                            <MoreVertRounded />
                                        </IconButton>
                                    )}
                                </Stack>
                                <Typography variant="body2" sx={{color: v(check?.errors.length ? 'fault' : 'ink-2')}}>
                                    {status}
                                    {robot && (
                                        <Box
                                            component="span"
                                            aria-live="polite"
                                            sx={{color: v('speak'), fontWeight: 600}}
                                        >
                                            {status && '. '}
                                            {robot}
                                        </Box>
                                    )}
                                </Typography>
                            </Box>
                            {rendering && (
                                <LinearProgress variant="determinate" value={(100 * render.done) / render.total} />
                            )}
                            {render?.error && <Alert severity="error">{render.error}</Alert>}
                            {notice && (
                                <Alert severity="warning" onClose={() => setNotice(null)}>
                                    {notice}
                                </Alert>
                            )}
                            <Stack
                                direction="row"
                                spacing={1}
                                useFlexGap
                                alignItems="flex-start"
                                sx={{flexWrap: 'wrap'}}
                            >
                                <Emotions
                                    key={`e${pane}`}
                                    title={draft.title}
                                    text={draft.text}
                                    onText={(text) => setDraft((d) => ({...d, text}))}
                                />
                                <Box sx={{flex: 1}} />
                                <Play
                                    key={pane}
                                    saved={save}
                                    cast={check?.cast.map((member) => member.name) ?? []}
                                    playing={playing}
                                    onPlaying={onPlaying}
                                    ref={player}
                                />
                            </Stack>
                            <Editor
                                text={draft.text}
                                onText={(text) => setDraft((d) => ({...d, text}))}
                                check={check}
                                checked={checked}
                                vocabulary={vocabulary}
                                current={playing ? stage.line : null}
                                rendering={rendering}
                                onPlayFrom={(from) => player.current?.playFrom(from)}
                            />
                        </>
                    )}
                </Stack>
            </Box>
            <Menu anchorEl={more} open={!!more} onClose={() => setMore(null)}>
                <MenuItem
                    onClick={() => {
                        setMore(null);
                        setDeleting(true);
                    }}
                    sx={{color: v('fault')}}
                >
                    {saved.builtin ? t.restoreOriginal : t.deleteScene}
                </MenuItem>
            </Menu>
            <Dialog open={leaving !== null} onClose={() => setLeaving(null)}>
                <DialogTitle>{t.discardAsk}</DialogTitle>
                <DialogActions>
                    <Button color="inherit" onClick={() => setLeaving(null)}>
                        {t.cancel}
                    </Button>
                    <Button
                        variant="contained"
                        color="error"
                        onClick={() => {
                            leaving?.();
                            setLeaving(null);
                        }}
                    >
                        {t.discard}
                    </Button>
                </DialogActions>
            </Dialog>
            <Dialog open={deleting} onClose={() => setDeleting(false)}>
                <DialogTitle>{saved.builtin ? t.restoreAsk(saved.title) : t.deleteAsk(saved.title)}</DialogTitle>
                <DialogActions>
                    <Button color="inherit" onClick={() => setDeleting(false)}>
                        {t.cancel}
                    </Button>
                    <Button variant="contained" color="error" onClick={remove}>
                        {saved.builtin ? t.restore : t.delete}
                    </Button>
                </DialogActions>
            </Dialog>
        </Dialog>
    );
}

/** A scene's voices in the list: a bar of those ready, or its errors. */
const Readiness = ({errors, done, total}: {errors: number; done: number; total: number}) => {
    const t = useWords(SCENES);
    return errors ? (
        <Typography variant="caption" sx={{color: v('fault'), fontWeight: 600}}>
            {t.errors(errors)}
        </Typography>
    ) : (
        <Stack direction="row" alignItems="center" spacing={1}>
            <Box sx={{flex: 1, maxWidth: 96, height: 3, borderRadius: 2, bgcolor: v('line'), overflow: 'hidden'}}>
                <Box
                    sx={{
                        height: 1,
                        width: `${total ? (100 * done) / total : 0}%`,
                        bgcolor: v(done === total ? 'listen' : 'ink-3')
                    }}
                />
            </Box>
            <Typography variant="caption" sx={{color: v('ink-3'), fontVariantNumeric: 'tabular-nums'}}>
                {t.voices(done, total)}
            </Typography>
        </Stack>
    );
};
