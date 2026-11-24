// Above the conversation: the palette, opened like a search field, the three workspaces and the settings, each by
// its name and its picture of the robot.
import {Box, Button, ButtonBase, IconButton, Stack, Tooltip, Typography} from '@mui/material';
import SearchRounded from '@mui/icons-material/SearchRounded';
import {useWords} from '../i18n';
import type {Surface} from '../surfaces';
import {useLive} from '../live';
import {Sticker} from '../Sticker';
import {GUTTER, v} from '../theme';
import {HEADER} from './words';

interface Props {
    compact: boolean;
    onSettings: () => void;
    onPalette: () => void;
    onSurface: (surface: Surface) => void;
}

const PLACES = ['studio', 'scenes', 'apps', 'settings'] as const;

const mac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

export const Header = ({compact, onSettings, onPalette, onSurface}: Props) => {
    const {settings} = useLive();
    const t = useWords(HEADER);
    const name = settings?.settings.name ?? 'Reachy';
    return (
        <Stack
            component="nav"
            direction="row"
            alignItems="center"
            spacing={compact ? 0 : 0.5}
            sx={{width: 1, maxWidth: 808, mx: 'auto', px: GUTTER, py: compact ? 1 : 1.5, flexShrink: 0}}
        >
            <ButtonBase
                aria-label={t.openPalette}
                aria-keyshortcuts="Meta+K"
                onClick={onPalette}
                sx={{
                    flex: compact ? 1 : '0 1 360px',
                    mr: compact ? 0.5 : 0,
                    minWidth: 0,
                    height: 40,
                    px: 1.5,
                    gap: 1,
                    justifyContent: 'flex-start',
                    borderRadius: '10px',
                    bgcolor: v('raised'),
                    border: `1px solid ${v('line')}`,
                    color: v('ink-3'),
                    transition: 'border-color 120ms',
                    '&:hover': {borderColor: v('line-strong')}
                }}
            >
                <SearchRounded fontSize="small" sx={{color: v('ink-2')}} />
                <Typography variant="body2" noWrap sx={{flex: 1, textAlign: 'left', color: 'inherit'}}>
                    {t.whatShould(compact ? undefined : name)}
                </Typography>
                {!compact && <kbd>{mac ? '⌘ K' : 'Ctrl K'}</kbd>}
            </ButtonBase>
            <Box sx={{flex: compact ? 'none' : 1, minWidth: 8}} />
            {PLACES.map((kind) => {
                const {label, hint} = t.places[kind];
                const open = () => (kind === 'settings' ? onSettings() : onSurface({kind}));
                return compact ? (
                    <IconButton key={kind} aria-label={label} onClick={open} sx={{width: 40, height: 40, p: 0.5}}>
                        <Sticker name={kind} size={30} />
                    </IconButton>
                ) : (
                    <Tooltip key={kind} title={hint}>
                        <Button
                            color="inherit"
                            startIcon={<Sticker name={kind} size={28} />}
                            onClick={open}
                            sx={{pl: 0.75, pr: 1.25, '&:last-of-type': {mr: -1.25}}}
                        >
                            {label}
                        </Button>
                    </Tooltip>
                );
            })}
        </Stack>
    );
};
