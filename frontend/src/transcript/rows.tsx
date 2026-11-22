// The conversation set like a play's script: who speaks in the margin, their words beside it (the robot in casual
// letters, people in linear ones), and what the robot does as stage directions under its lines.
import {memo} from 'react';
import {Box, Divider, Stack, Typography} from '@mui/material';
import MusicNoteRounded from '@mui/icons-material/MusicNoteRounded';
import {formatDegrees, formatDuration, formatPercent, formatUsd} from '../format';
import {wordsNow, useWords} from '../i18n';
import {mac} from '../mac';
import {Signal} from '../Signal';
import {CASUAL, v, type Token} from '../theme';
import type {Row, SessionRow, ToolRow, TurnRow} from './reducer';
import {TRANSCRIPT} from './words';

const TONE: Record<ToolRow['status'], Token> = {
    pending: 'ink-3',
    unanswered: 'ink-3',
    ok: 'listen',
    error: 'fault',
    refused: 'attention',
    cancelled: 'ink-3'
};

/** The script's two columns: the speaker in the margin, the words beside it; one column on a phone. */
export const SCRIPT = {
    display: 'grid',
    gridTemplateColumns: {xs: 'minmax(0, 1fr)', sm: '88px minmax(0, 1fr)'},
    columnGap: '20px',
    rowGap: '2px'
} as const;
const MARGIN = {
    pt: {sm: '4px'},
    textAlign: {sm: 'right'},
    color: v('ink-2'),
    fontWeight: 620,
    overflowWrap: 'anywhere'
} as const;
const OBLIQUE = 'oblique 10deg';

/** The catalogue's labels by entry id (« gesture:nod » → « Oui de la tête »): the palette's names, as the Mac wrote them. */
export type Names = ReadonlyMap<string, string>;
const text = (value: unknown) => (typeof value === 'string' ? value : '');
const figure = (value: unknown, format: (n: number) => string) =>
    typeof value === 'number' && Number.isFinite(value) ? format(value) : text(value);

/**
 * A call as the transcript says it: what the robot did, in the palette's words, then with what, each figure in its
 * unit, in the language shown. A value the catalogue does not name shows as the robot sent it.
 */
export const toolLine = (
    name: string,
    args: Record<string, unknown>,
    names: Names
): {title: string; detail: string} => {
    const t = wordsNow(TRANSCRIPT);
    const title = t.tools[name] ?? name;
    const named = (id: string) => mac(names.get(id) ?? '');
    switch (name) {
        case 'do_gesture':
            return {title, detail: named(`gesture:${text(args.name)}`) || text(args.name)};
        case 'play_emotion':
            return {title, detail: named(`emotion:${text(args.name)}`) || text(args.name)};
        case 'turn_body':
            return {title, detail: figure(args.angle, formatDegrees)};
        case 'set_volume':
            return {title, detail: figure(args.level, formatPercent)};
        case 'play_music': {
            const kind = text(args.kind) || (args.query ? 'track' : 'liked'); // the robot's own default
            return {title: named(`spotify:play:${kind}`) || title, detail: text(args.query)};
        }
        case 'music': {
            const action = text(args.action);
            const value =
                action === 'seek'
                    ? figure(args.amount, formatDuration)
                    : action === 'volume'
                      ? figure(args.amount, formatPercent)
                      : (t.switches[text(args.value)] ?? text(args.value));
            const label = named(`spotify:${action}`);
            return label ? {title: label, detail: value} : {title, detail: [action, value].join(' ').trim()};
        }
        case 'app': {
            const what = [text(args.name), text(args.request)].filter(Boolean).join(t.colon);
            return {title: t.appActions[text(args.action)] ?? title, detail: what};
        }
        case 'play_scene':
            return {title, detail: text(args.title)};
        case 'write_scene':
            return {title, detail: text(args.request)};
        default: // goto_sleep and look take nothing
            return {title, detail: ''};
    }
};

const Turn = ({row, robotName}: {row: TurnRow; robotName: string}) => {
    const t = useWords(TRANSCRIPT);
    const user = row.kind === 'user';
    return (
        <Box sx={SCRIPT}>
            <Typography variant="caption" component="div" sx={MARGIN}>
                {user ? t.you : robotName}
                {row.typed && (
                    <Box
                        component="span"
                        sx={{display: {xs: 'inline', sm: 'block'}, fontWeight: 400, color: v('ink-3')}}
                    >
                        {' '}
                        {t.typed}
                    </Box>
                )}
            </Typography>
            <Typography
                variant="body1"
                sx={{
                    fontVariationSettings: user ? undefined : CASUAL,
                    color: row.done ? v('ink') : v('ink-3'),
                    transition: 'color 240ms',
                    whiteSpace: 'pre-wrap',
                    overflowWrap: 'anywhere'
                }}
            >
                {row.text}
            </Typography>
        </Box>
    );
};

const Status = ({status}: {status: ToolRow['status']}) => (
    <Signal tone={TONE[status]} label={useWords(TRANSCRIPT).status[status]} busy={status === 'pending'} />
);

const Tool = ({row, names}: {row: ToolRow; names: Names}) => {
    const t = useWords(TRANSCRIPT);
    const failed = row.status === 'error' || row.status === 'refused';
    const {title, detail} = toolLine(row.name, row.args, names);
    return (
        <Box sx={SCRIPT}>
            <Box sx={{display: {xs: 'none', sm: 'block'}}} />
            <Box sx={{pl: 1.5, borderLeft: `2px solid ${v('line')}`, py: 0.25}}>
                <Stack direction="row" columnGap={1.25} rowGap={0.25} alignItems="baseline" flexWrap="wrap">
                    <Typography variant="body2" sx={{fontStyle: OBLIQUE, color: v('ink-2'), overflowWrap: 'anywhere'}}>
                        <Box component="span" sx={{fontWeight: 620, color: v('ink')}}>
                            {title}
                        </Box>
                        {detail && ` ${detail}`}
                    </Typography>
                    <Status status={row.status} />
                </Stack>
                {failed && row.message && (
                    <Typography
                        variant="body2"
                        sx={{mt: 0.25, color: v(row.status === 'error' ? 'fault' : 'attention')}}
                    >
                        {row.message}
                    </Typography>
                )}
                {row.track && (
                    <Stack direction="row" spacing={1} alignItems="center" sx={{mt: 0.75}}>
                        <Box
                            sx={{
                                width: 28,
                                height: 28,
                                borderRadius: '8px',
                                display: 'grid',
                                placeItems: 'center',
                                bgcolor: v('lens'),
                                color: v('on-lens'),
                                flexShrink: 0
                            }}
                        >
                            <MusicNoteRounded sx={{fontSize: 16}} />
                        </Box>
                        <Typography variant="body2" sx={{fontWeight: 560}}>
                            {row.track.artist ? `${row.track.title} — ${row.track.artist}` : row.track.title}
                        </Typography>
                    </Stack>
                )}
                {row.image && (
                    <Box
                        component="img"
                        src={`data:image/jpeg;base64,${row.image}`}
                        alt={t.seen}
                        sx={{
                            display: 'block',
                            mt: 1,
                            width: '100%',
                            maxWidth: 360,
                            borderRadius: '10px',
                            border: `1px solid ${v('line')}`
                        }}
                    />
                )}
            </Box>
        </Box>
    );
};

const Session = ({row}: {row: SessionRow}) => {
    const t = useWords(TRANSCRIPT);
    const parts = row.ended
        ? [t.end, t.ended[row.reason ?? ''] ?? row.reason, row.costUsd !== null && formatUsd(row.costUsd)]
        : [t.start];
    return (
        <Divider role="presentation" sx={{py: 1, '&::before, &::after': {borderColor: v('line')}}}>
            <Typography variant="caption" sx={{color: v('ink-3'), fontWeight: 560}}>
                {parts.filter(Boolean).join(' · ')}
            </Typography>
        </Divider>
    );
};

export const RowView = memo(function RowView({row, robotName, names}: {row: Row; robotName: string; names: Names}) {
    if (row.kind === 'tool') return <Tool row={row} names={names} />;
    if (row.kind === 'session') return <Session row={row} />;
    return <Turn row={row} robotName={robotName} />;
});
