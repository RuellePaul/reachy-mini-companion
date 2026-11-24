// A build under way, as Claude works (the `app_build` channel): what it is doing and for how long, the code being
// written with a preview of the page so far (a creation), or each edit as it lands (a retouch), and « Annuler ».
import {useEffect, useRef, useState} from 'react';
import {Alert, Box, Button, Stack, Typography} from '@mui/material';
import StopRounded from '@mui/icons-material/StopRounded';
import {errorText} from '../api/client';
import type {AppBuild} from '../api/schemas';
import {elapsed, useSeconds} from '../elapsed';
import {formatUsd} from '../format';
import {useWords, wordsNow} from '../i18n';
import {useLive} from '../live';
import {Signal} from '../Signal';
import {MONO, v} from '../theme';
import {appsApi} from './api';
import {APPS} from './words';

const PREVIEW_MS = 3000; // the draft is fetched again at most this often, and only once it has grown
const PREVIEW_GROWTH = 1500; // characters

/** What the build is doing, in a few words: its phase, and how much it has thought or written. */
export const doing = (build: AppBuild): string => {
    const t = wordsNow(APPS);
    const more =
        build.phase === 'thinking' && build.thinking_tokens
            ? t.tokens(build.thinking_tokens)
            : build.phase === 'writing' && build.chars
              ? t.chars(build.chars)
              : '';
    return `${build.round > 1 ? t.repair : ''}${t.phase[build.phase]}${more}`;
};

const Code = ({children, tone}: {children: string; tone?: 'fault' | 'listen'}) => (
    <Box
        component="pre"
        sx={{
            m: 0,
            p: 1,
            fontSize: 12,
            lineHeight: 1.45,
            fontVariationSettings: MONO,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            color: v('ink-2'),
            bgcolor: tone ? `color-mix(in srgb, ${v(tone)} 12%, transparent)` : 'transparent'
        }}
    >
        {children}
    </Box>
);

/** The latest progress of app `id`, null until the first one comes. */
const useBuild = (id: string): AppBuild | null => {
    const {on} = useLive();
    const [build, setBuild] = useState<AppBuild | null>(null);
    useEffect(() => on('app_build', (progress) => progress.app_id === id && setBuild(progress)), [on, id]);
    return build;
};

/** The page so far, fetched again as it grows, and its last part once writing stops; shown without its scripts. */
const useDraft = (id: string, chars: number, writing: boolean): string | null => {
    const [draft, setDraft] = useState<string | null>(null);
    const fetched = useRef({at: 0, chars: 0});
    useEffect(() => {
        if (chars < fetched.current.chars) fetched.current = {at: 0, chars: 0}; // written again from the start
        const grown = chars - fetched.current.chars;
        const due = writing ? grown >= PREVIEW_GROWTH && Date.now() - fetched.current.at >= PREVIEW_MS : grown > 0;
        if (!due) return;
        fetched.current = {at: Date.now(), chars};
        appsApi.draft(id).then(setDraft, () => {});
    }, [id, chars, writing]);
    return draft;
};

export const Build = ({id, compact = false}: {id: string; compact?: boolean}) => {
    const t = useWords(APPS);
    const build = useBuild(id);
    const seconds = useSeconds(build?.started_at);
    const draft = useDraft(id, build?.chars ?? 0, build?.phase === 'writing');
    const [error, setError] = useState('');
    const code = useRef<HTMLDivElement>(null);
    useEffect(() => {
        code.current?.scrollTo({top: code.current.scrollHeight});
    }, [build?.tail, build?.edits.length]);

    const cancel = () => appsApi.cancel(id).catch((err: unknown) => setError(errorText(err)));
    const head = (
        // A line too narrow moves what follows what Claude does to the next one, each part whole.
        <Stack direction="row" columnGap={1.5} alignItems="center" useFlexGap sx={{minHeight: 36, flexWrap: 'wrap'}}>
            <Signal tone="attention" busy label={build ? doing(build) : t.building} />
            <Typography variant="caption" sx={{color: v('ink-3'), fontVariantNumeric: 'tabular-nums'}}>
                {build ? elapsed(seconds) : ''}
                {build?.cost_usd ? (
                    <>
                        {' · '}
                        <span title={t.apiCost}>{t.atApiRates(formatUsd(build.cost_usd))}</span>
                    </>
                ) : null}
            </Typography>
            <Button
                size="small"
                color="inherit"
                startIcon={<StopRounded />}
                onClick={() => void cancel()}
                sx={{ml: 'auto'}} // at the end of its line, the first or the next
            >
                {t.cancel}
            </Button>
        </Stack>
    );
    const edits = build?.edits ?? [];
    const problems = build?.round === 2 && build.problems.length > 0 && (
        <Alert severity="warning" sx={{py: 0}}>
            {t.refusedOnce(build.problems)}
        </Alert>
    );
    if (compact)
        return (
            <Stack spacing={1} sx={{px: 1.5, py: 1, borderBottom: `1px solid ${v('line')}`, bgcolor: v('raised')}}>
                {head}
                {problems}
                {edits.length > 0 && (
                    <Box ref={code} sx={{maxHeight: 160, overflow: 'auto'}}>
                        {edits.map((edit, n) => (
                            <Box key={n} sx={{mb: 0.5}}>
                                <Code tone="fault">{edit.old}</Code>
                                <Code tone="listen">{edit.new}</Code>
                            </Box>
                        ))}
                    </Box>
                )}
                {error && <Alert severity="error">{error}</Alert>}
            </Stack>
        );
    return (
        <Stack spacing={1.5} sx={{flex: 1, minHeight: 0, p: 1.5}}>
            {head}
            {problems}
            {error && <Alert severity="error">{error}</Alert>}
            <Stack direction={{xs: 'column', md: 'row'}} spacing={1.5} sx={{flex: 1, minHeight: 0}}>
                <Box
                    ref={code}
                    sx={{flex: 1, minHeight: 160, overflow: 'auto', borderRadius: '10px', bgcolor: v('raised')}}
                >
                    {build?.tail ? (
                        <Code>{build.tail}</Code>
                    ) : (
                        <Typography variant="body2" sx={{p: 2, color: v('ink-3')}}>
                            {build?.phase === 'thinking' ? t.thinkingFirst : t.writtenHere}
                        </Typography>
                    )}
                    {edits.map((edit, n) => (
                        <Box key={n} sx={{mb: 0.5}}>
                            <Code tone="fault">{edit.old}</Code>
                            <Code tone="listen">{edit.new}</Code>
                        </Box>
                    ))}
                </Box>
                {draft && (
                    <Box
                        component="iframe"
                        title={t.draft}
                        sandbox=""
                        srcDoc={draft}
                        sx={{flex: 1, minHeight: 240, border: 0, borderRadius: '10px', bgcolor: v('screen')}}
                    />
                )}
            </Stack>
        </Stack>
    );
};
