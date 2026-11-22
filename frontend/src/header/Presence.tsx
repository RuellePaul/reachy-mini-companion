// The robot itself: its name, its picture, its state in a word and what can be done about it (wake it, put it to
// sleep), its volume and, in conversation, what the conversation costs. A column on a computer, a strip on a phone.
import {useState} from 'react';
import {Box, Button, Snackbar, Stack, Tooltip, Typography} from '@mui/material';
import BedtimeRounded from '@mui/icons-material/BedtimeRounded';
import WbSunnyRounded from '@mui/icons-material/WbSunnyRounded';
import {api, errorText} from '../api/client';
import {formatDuration, formatUsd} from '../format';
import {useWords} from '../i18n';
import {useLive} from '../live';
import {Sticker} from '../Sticker';
import {CASUAL, GUTTER, v} from '../theme';
import {actionOf, describe} from './status';
import {VolumeControl} from './VolumeControl';
import {HEADER} from './words';

const ACTION = {
    wake: {icon: <WbSunnyRounded />, variant: 'contained'},
    sleep: {icon: <BedtimeRounded />, variant: 'outlined'}
} as const;

const Figure = ({label, value, hint}: {label: string; value: string; hint: string}) => (
    <Tooltip describeChild title={hint} placement="right">
        <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <Typography variant="body2" color="text.secondary">
                {label}
            </Typography>
            <Typography variant="body2" sx={{fontWeight: 600, fontVariantNumeric: 'tabular-nums'}}>
                {value}
            </Typography>
        </Stack>
    </Tooltip>
);

export const Presence = ({compact}: {compact: boolean}) => {
    const {link, robotStatus, session, settings} = useLive();
    const [error, setError] = useState({text: '', open: false});
    const t = useWords(HEADER);
    const look = describe(link, robotStatus, session);
    const action = actionOf(link, robotStatus, session);
    const name = settings?.settings.name ?? 'Reachy';
    const act = () => action && api.session(action).catch((err) => setError({text: errorText(err), open: true}));
    const button = action && (
        <Button
            variant={ACTION[action].variant}
            color={action === 'sleep' ? 'inherit' : 'primary'}
            size={compact ? 'small' : 'large'}
            startIcon={ACTION[action].icon}
            fullWidth={!compact}
            onClick={act}
        >
            {t[action]}
        </Button>
    );
    const snack = (
        <Snackbar
            open={error.open}
            message={error.text}
            autoHideDuration={6000}
            onClose={() => setError((e) => ({...e, open: false}))}
        />
    );

    if (compact)
        return (
            <Stack
                component="header"
                direction="row"
                alignItems="center"
                spacing={1.5}
                sx={{px: GUTTER, py: 1.25, bgcolor: v('plate'), borderBottom: `1px solid ${v('line')}`}}
            >
                <Sticker name="reachy" size={44} />
                <Box sx={{flex: 1, minWidth: 0}}>
                    <Typography component="h1" variant="caption" color="text.secondary" noWrap sx={{display: 'block'}}>
                        {name}
                    </Typography>
                    <Typography variant="h5" aria-live="polite" noWrap sx={{fontVariationSettings: CASUAL}}>
                        {look.label}
                    </Typography>
                </Box>
                {button}
                {snack}
            </Stack>
        );

    return (
        <Stack
            component="aside"
            aria-label={name}
            sx={{
                width: {md: 288, lg: 320},
                flexShrink: 0,
                bgcolor: v('plate'),
                borderRight: `1px solid ${v('line')}`,
                px: 3,
                pt: 2,
                pb: 3,
                overflowY: 'auto'
            }}
        >
            <Typography component="h1" variant="h4" noWrap sx={{lineHeight: '40px'}}>
                {name}
            </Typography>
            <Box sx={{pt: 6, pb: 3, ml: -1.5}}>
                <Sticker name="reachy" size={168} />
            </Box>
            <Typography variant="h3" component="p" aria-live="polite" sx={{fontVariationSettings: CASUAL}}>
                {look.label}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{mt: 1, minHeight: 42}}>
                {look.detail}
            </Typography>
            <Box sx={{mt: 3, minHeight: 44}}>{button}</Box>
            <Box sx={{flex: 1, minHeight: 32}} />
            <Stack spacing={0.5} sx={{pt: 2, borderTop: `1px solid ${v('line')}`}}>
                <Typography variant="body2" color="text.secondary">
                    {t.volume}
                </Typography>
                <VolumeControl onError={(text) => setError({text, open: true})} />
            </Stack>
            {session.awake && link === 'up' && (
                <Stack spacing={0.75} sx={{mt: 2, pt: 2, borderTop: `1px solid ${v('line')}`}}>
                    <Figure label={t.cost} value={formatUsd(session.cost_usd)} hint={t.costHint} />
                    {session.last_latency_ms !== null && (
                        <Figure
                            label={t.latency}
                            value={formatDuration(session.last_latency_ms / 1000)}
                            hint={t.latencyHint}
                        />
                    )}
                </Stack>
            )}
            {snack}
        </Stack>
    );
};
