import {useState} from 'react';
import {Slider, Stack, Typography} from '@mui/material';
import VolumeDownRounded from '@mui/icons-material/VolumeDownRounded';
import VolumeOffRounded from '@mui/icons-material/VolumeOffRounded';
import VolumeUpRounded from '@mui/icons-material/VolumeUpRounded';
import {errorText} from '../api/client';
import {useWords} from '../i18n';
import {useLive} from '../live';
import {coalescer} from './coalesce';
import {HEADER} from './words';

const DEBOUNCE_MS = 150;

const single = (value: number | number[]) => (Array.isArray(value) ? value[0] : value);

interface Props {
    onError: (message: string) => void;
    /** The slider's own id (the settings drawer focuses it). */
    id?: string;
}

/** The robot's volume. Until its last request settles it shows the user's value, not the broadcasts of its echoes. */
export const VolumeControl = ({onError, id}: Props) => {
    const {link, robotStatus, volume, saveVolume} = useLive();
    const t = useWords(HEADER);
    const [moving, setMoving] = useState<number | null>(null);
    const [push] = useState(() => coalescer<number>(DEBOUNCE_MS));
    const disabled = link !== 'up' || robotStatus === 'off' || volume === null;
    const shown = moving ?? volume ?? 0;
    const Icon = shown === 0 ? VolumeOffRounded : shown < 50 ? VolumeDownRounded : VolumeUpRounded;

    // Pointer and keyboard alike go through the debounce: MUI commits every arrow press, so commits send nothing.
    const change = (value: number) => {
        setMoving(value);
        push(
            value,
            (v) => saveVolume(v).catch((err) => onError(errorText(err))),
            () => setMoving(null)
        );
    };

    return (
        <Stack
            direction="row"
            spacing={1.5}
            alignItems="center"
            sx={{width: 1, color: disabled ? 'text.disabled' : 'text.secondary'}}
        >
            <Icon fontSize="small" />
            <Slider
                size="small"
                min={0}
                max={100}
                value={shown}
                disabled={disabled}
                aria-label={t.robotVolume}
                slotProps={{input: {id}}}
                onChange={(_, value) => change(single(value))}
                sx={{flex: 1}}
            />
            <Typography
                variant="body2"
                aria-hidden
                sx={{width: 28, textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: 'text.primary'}}
            >
                {volume === null ? '–' : shown}
            </Typography>
        </Stack>
    );
};
