// A scene written at someone's ask, as it is prepared (the `scene_build` channel): its three steps (Claude writes the
// play; then, side by side, Claude adds its emotions and Gemini renders its voices), its cast, the play as it is
// written, and how it ended.
import {useEffect, useMemo, useRef, type ReactNode} from 'react';
import {Alert, Box, Button, CircularProgress, Stack, Typography} from '@mui/material';
import {keyframes} from '@mui/material/styles';
import CheckRounded from '@mui/icons-material/CheckRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import EditNoteRounded from '@mui/icons-material/EditNoteRounded';
import ReplayRounded from '@mui/icons-material/ReplayRounded';
import SmartToyRounded from '@mui/icons-material/SmartToyRounded';
import StopRounded from '@mui/icons-material/StopRounded';
import type {SceneBuild} from '../api/schemas';
import {elapsed, useSeconds} from '../elapsed';
import {formatUsd} from '../format';
import {useWords, wordsNow} from '../i18n';
import {FONT, MONO, tint, v, type Token} from '../theme';
import {cast, colourOf, colours, paint} from './text';
import {SCENES} from './words';

const blink = keyframes`50% { opacity: 0; }`;

type State = SceneBuild['write'];
interface StepView {
    key: 'write' | 'stage' | 'voice';
    title: string;
    state: State;
    detail: string;
    share?: number; // of the way, 0 to 1: a bar under the detail
}

/** The three steps of a build, each with its state and what it is doing, or has done, in a few words: in the
 * language shown. */
export const steps = (build: SceneBuild): StepView[] => {
    const t = wordsNow(SCENES);
    const stopped = build.status === 'cancelled';
    const claude =
        build.phase === 'writing' && build.text
            ? t.claudeWrites(build.text.length)
            : build.phase === 'thinking'
              ? t.claudeThinks(build.thinking_tokens)
              : t.claudePrepares;
    const voices = `${build.voices_done}/${build.voices_total}`;
    return [
        {
            key: 'write',
            title: t.writeStep.title,
            state: build.write,
            detail: {
                waiting: t.writeStep.waiting,
                running: `${build.round > 1 ? t.writeStep.second : ''}${claude}`,
                done: t.writeStep.done(build.voices_total, cast(build.text).length),
                failed: stopped ? t.writeStep.cancelled : t.writeStep.failed
            }[build.write]
        },
        {
            key: 'stage',
            title: t.stageStep.title,
            state: build.stage,
            detail: {
                waiting: t.stageStep.waiting,
                running: t.stageStep.running,
                done: t.stageStep.done(build.moves),
                failed: stopped ? t.stageStep.cancelled : t.stageStep.failed
            }[build.stage]
        },
        {
            key: 'voice',
            title: t.voiceStep.title,
            state: build.voice,
            share: build.voices_total ? build.voices_done / build.voices_total : 0,
            detail: {
                waiting: t.voiceStep.waiting,
                running: t.voiceStep.running(voices),
                done: t.voiceStep.done(build.voices_total),
                failed: stopped ? t.voiceStep.cancelled(voices) : t.voiceStep.failed(voices)
            }[build.voice]
        }
    ];
};

/** A build in a word or two, for its row in the list: the step it is at, or how it ended. */
export const said = (build: SceneBuild): {label: string; tone: Token; busy: boolean} => {
    const t = wordsNow(SCENES).row;
    if (build.status === 'ready') return {label: t.ready, tone: 'listen', busy: false};
    if (build.status === 'failed') return {label: t.failed, tone: 'fault', busy: false};
    if (build.status === 'cancelled') return {label: t.cancelled, tone: 'ink-3', busy: false};
    const label =
        build.write !== 'done'
            ? t.writing
            : build.voice === 'running'
              ? t.voices(build.voices_done, build.voices_total)
              : t.staging;
    return {label, tone: 'attention', busy: true};
};

const TONE: Record<State, Token> = {waiting: 'ink-3', running: 'attention', done: 'listen', failed: 'fault'};

/** A step's mark: its number while it waits, a turning ring while it runs, a tick or a cross once it has ended. */
const Mark = ({n, state}: {n: number; state: State}) => (
    <Box
        aria-hidden
        sx={{
            width: 26,
            height: 26,
            flexShrink: 0,
            display: 'grid',
            placeItems: 'center',
            borderRadius: '50%',
            fontSize: 12,
            fontWeight: 700,
            color: v(state === 'waiting' ? 'ink-3' : state === 'running' ? 'attention' : 'page'),
            bgcolor: state === 'done' || state === 'failed' ? v(TONE[state]) : 'transparent',
            border: state === 'waiting' ? `1px solid ${v('line-strong')}` : 0,
            '& > *': {gridArea: '1 / 1'}
        }}
    >
        {state === 'running' && <CircularProgress size={26} thickness={3} sx={{color: v('attention')}} />}
        {state === 'done' ? (
            <CheckRounded sx={{fontSize: 18}} />
        ) : state === 'failed' ? (
            <CloseRounded sx={{fontSize: 18}} />
        ) : (
            <span>{n}</span>
        )}
    </Box>
);

const Step = ({n, step}: {n: number; step: StepView}) => {
    const running = step.state === 'running';
    return (
        <Box
            data-step={step.key}
            data-state={step.state}
            sx={{
                flex: 1,
                minWidth: 0,
                p: 1.5,
                borderRadius: '12px',
                border: `1px solid ${v(running ? 'line-strong' : 'line')}`,
                bgcolor: running ? v('raised') : 'transparent',
                transition: 'background-color 200ms, border-color 200ms'
            }}
        >
            <Stack direction="row" spacing={1.25} alignItems="center">
                <Mark n={n} state={step.state} />
                <Typography sx={{fontWeight: 700, color: v(step.state === 'waiting' ? 'ink-3' : 'ink')}}>
                    {step.title}
                </Typography>
            </Stack>
            <Typography
                variant="body2"
                sx={{
                    mt: 1,
                    minHeight: 20,
                    color: v(step.state === 'failed' ? 'fault' : 'ink-2'),
                    fontVariantNumeric: 'tabular-nums'
                }}
            >
                {step.detail}
            </Typography>
            <Box sx={{mt: 1, height: 3, borderRadius: 2, bgcolor: v('line'), overflow: 'hidden'}}>
                <Box
                    sx={{
                        height: 1,
                        borderRadius: 2,
                        bgcolor: v(TONE[step.state]),
                        width: `${100 * (step.state === 'done' ? 1 : step.state === 'waiting' ? 0 : (step.share ?? 1))}%`,
                        transition: 'width 300ms',
                        ...(running && step.share === undefined ? {animation: `${blink} 1.6s infinite`} : {})
                    }}
                />
            </Box>
        </Box>
    );
};

/** The play, read-only, painted as the editor paints it; while it is written, a caret after its last word and the
 * view kept on it. */
const Script = ({text, writing, empty}: {text: string; writing: boolean; empty: string}) => {
    const label = useWords(SCENES).play;
    const box = useRef<HTMLDivElement>(null);
    const palette = useMemo(() => colours(text), [text]);
    const painted = useMemo(() => paint(text, palette), [text, palette]);
    const followed = useRef(false);
    useEffect(() => {
        if (writing) box.current?.scrollTo({top: box.current.scrollHeight});
        else if (followed.current) box.current?.scrollTo({top: 0}); // written: read from its first line
        followed.current = writing;
    }, [text, writing]);
    return (
        <Box
            ref={box}
            aria-label={label}
            sx={{
                flex: 1,
                minHeight: 220,
                overflow: 'auto',
                py: 1.5,
                border: `1px solid ${v('line')}`,
                borderRadius: '12px',
                bgcolor: v('raised'),
                font: `14px/21px ${FONT}`,
                fontVariationSettings: MONO,
                '& > div': {
                    m: 0,
                    px: 2,
                    whiteSpace: 'pre-wrap',
                    overflowWrap: 'anywhere',
                    color: v('ink'),
                    borderLeft: '3px solid transparent'
                },
                '& > div[data-who]': {borderLeftColor: 'var(--who)'},
                '& [data-p="aside"]': {color: v('aside')},
                '& [data-p="move"]': {color: v('move'), bgcolor: v('move-bg'), borderRadius: '3px'},
                '& [data-p="still"]': {color: v('still'), bgcolor: v('still-bg'), borderRadius: '3px'},
                '& [data-p="sound"]': {color: v('sound')}
            }}
        >
            {!text && (
                <Typography variant="body2" sx={{px: 2, color: v('ink-3'), fontVariationSettings: 'normal'}}>
                    {empty}
                </Typography>
            )}
            {text &&
                painted.map(({who, parts}, i) => (
                    <div key={i} data-who={who ?? undefined} style={{'--who': colourOf(palette, who)} as object}>
                        {parts.map(([part, kind], j) =>
                            kind === 'said' ? (
                                part
                            ) : (
                                <span
                                    key={j}
                                    data-p={kind}
                                    style={kind === 'name' ? {color: colourOf(palette, who)} : undefined}
                                >
                                    {part}
                                </span>
                            )
                        )}
                        {writing && i === painted.length - 1 ? (
                            <Box
                                component="span"
                                sx={{color: v('attention'), animation: `${blink} 1s steps(1) infinite`}}
                            >
                                ▍
                            </Box>
                        ) : (
                            parts.every(([part]) => !part) && '​'
                        )}
                    </div>
                ))}
        </Box>
    );
};

interface Props {
    build: SceneBuild;
    onCancel: () => void;
    onOpen: (sceneId: string) => void; // in its editor
    onPlay: (sceneId: string) => void; // on the robot, its editor open on it
    onRetry: (request: string) => void;
    error?: string | null; // why a cancel or a play was refused
}

export const Writing = ({build, onCancel, onOpen, onPlay, onRetry, error}: Props) => {
    const t = useWords(SCENES);
    const seconds = useSeconds(build.started_at, build.ended_at);
    const building = build.status === 'building';
    const people = useMemo(() => cast(build.text), [build.text]);
    const palette = useMemo(() => colours(build.text), [build.text]);
    const sceneId = build.scene_id;
    let outcome: ReactNode = null;
    if (build.status === 'ready')
        outcome = (
            <Alert severity="success" icon={<CheckRounded />}>
                {t.readyIn(elapsed(seconds), build.voiced)}
            </Alert>
        );
    else if (build.status === 'failed') outcome = <Alert severity="error">{build.error}</Alert>;
    else if (build.status === 'cancelled')
        outcome = <Alert severity="info">{sceneId ? t.stoppedKept : t.cancelledBefore}</Alert>;
    return (
        <Stack spacing={2} sx={{flex: 1, minHeight: 0}}>
            <Stack direction="row" spacing={2} alignItems="flex-start">
                <Box sx={{flex: 1, minWidth: 0}}>
                    <Typography variant="caption" sx={{color: v('ink-3'), fontWeight: 700, letterSpacing: '0.08em'}}>
                        {t.asked(build.voiced)}
                    </Typography>
                    <Typography
                        component="h2"
                        sx={{fontSize: 24, fontWeight: 700, letterSpacing: '-0.01em', lineHeight: 1.25}}
                    >
                        {build.title ?? t.aNewScene}
                    </Typography>
                    <Typography variant="body2" sx={{mt: 0.5, color: v('ink-2')}}>
                        {t.quoted(build.request)}
                    </Typography>
                </Box>
                <Stack alignItems="flex-end" spacing={0.5} sx={{flexShrink: 0}}>
                    {building && (
                        <Button size="small" color="inherit" startIcon={<StopRounded />} onClick={onCancel}>
                            {t.cancel}
                        </Button>
                    )}
                    <Typography variant="caption" sx={{color: v('ink-3'), fontVariantNumeric: 'tabular-nums'}}>
                        {elapsed(seconds)}
                        {build.cost_usd ? (
                            <>
                                {' · '}
                                <span title={t.apiCost}>{t.atApiRates(formatUsd(build.cost_usd))}</span>
                            </>
                        ) : null}
                    </Typography>
                </Stack>
            </Stack>
            <Stack direction={{xs: 'column', sm: 'row'}} spacing={1.5} role="list" aria-label={t.steps}>
                {steps(build).map((step, n) => (
                    <Step key={step.key} n={n + 1} step={step} />
                ))}
            </Stack>
            {build.round > 1 && build.problems.length > 0 && (
                <Alert severity="warning" sx={{py: 0}}>
                    {t.refusedOnce(build.problems)}
                </Alert>
            )}
            {outcome}
            {build.warning && <Alert severity="warning">{build.warning}</Alert>}
            {error && <Alert severity="error">{error}</Alert>}
            {!building && (
                <Stack direction="row" spacing={1} useFlexGap sx={{flexWrap: 'wrap'}}>
                    {build.status === 'ready' && sceneId && !build.voiced && (
                        <Button variant="contained" startIcon={<SmartToyRounded />} onClick={() => onPlay(sceneId)}>
                            {t.playOnRobot}
                        </Button>
                    )}
                    {sceneId && (
                        <Button
                            variant={build.status === 'ready' && !build.voiced ? 'outlined' : 'contained'}
                            color={build.status === 'ready' && !build.voiced ? 'inherit' : 'primary'}
                            startIcon={<EditNoteRounded />}
                            onClick={() => onOpen(sceneId)}
                        >
                            {t.openScene}
                        </Button>
                    )}
                    {build.status !== 'ready' && (
                        <Button
                            variant={sceneId ? 'outlined' : 'contained'}
                            color={sceneId ? 'inherit' : 'primary'}
                            startIcon={<ReplayRounded />}
                            onClick={() => onRetry(build.request)}
                        >
                            {t.askAgain}
                        </Button>
                    )}
                </Stack>
            )}
            {people.length > 0 && (
                <Stack direction="row" spacing={1} useFlexGap sx={{flexWrap: 'wrap'}} aria-label={t.cast}>
                    {people.map(({name, voice}) => (
                        <Box
                            key={name}
                            sx={{
                                px: 1.25,
                                py: 0.5,
                                borderRadius: 999,
                                fontSize: 12,
                                border: `1px solid ${tint('ink', 14)}`,
                                color: v('ink-2')
                            }}
                        >
                            <Box component="span" sx={{fontWeight: 700, color: colourOf(palette, name)}}>
                                {name}
                            </Box>
                            {voice && ` · ${voice}`}
                        </Box>
                    ))}
                </Stack>
            )}
            <Script
                text={build.text}
                writing={build.write === 'running' && build.phase === 'writing'}
                empty={
                    build.write === 'failed' ? t.noPlay : build.phase === 'thinking' ? t.thinkingFirst : t.writtenHere
                }
            />
        </Stack>
    );
};
