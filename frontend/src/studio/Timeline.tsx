// Under the 3D: play/pause, the playhead (yellow) and the time. Re-renders a few times a second only.
import {useEffect, useState} from 'react';
import {IconButton, Slider, Stack, Typography} from '@mui/material';
import PauseRounded from '@mui/icons-material/PauseRounded';
import PlayArrowRounded from '@mui/icons-material/PlayArrowRounded';
import {formatClock} from '../format';
import {useWords} from '../i18n';
import {v} from '../theme';
import type {Clock} from './clock';
import {STUDIO} from './words';

export const Timeline = ({clock}: {clock: Clock}) => {
    const t = useWords(STUDIO);
    const [, setTick] = useState(0);
    useEffect(() => {
        const timer = setInterval(() => setTick((n) => n + 1), 100);
        return () => clearInterval(timer);
    }, []);
    const time = clock.now();
    return (
        <Stack direction="row" alignItems="center" spacing={1.5} sx={{flex: 1, minWidth: 0}}>
            <IconButton aria-label={clock.playing ? t.pause : t.play} onClick={() => clock.toggle()}>
                {clock.playing ? <PauseRounded /> : <PlayArrowRounded />}
            </IconButton>
            <Slider
                size="small"
                min={0}
                max={clock.duration || 1}
                step={0.01}
                value={time}
                onChange={(_, value) => clock.seek(value as number)}
                aria-label={t.playhead}
                sx={{
                    flex: 1,
                    color: v('ink'),
                    '& .MuiSlider-thumb': {transition: 'none', width: 12, height: 12},
                    '& .MuiSlider-track': {transition: 'none'}
                }}
            />
            <Typography
                variant="caption"
                sx={{
                    minWidth: 88,
                    textAlign: 'right',
                    whiteSpace: 'nowrap',
                    fontVariantNumeric: 'tabular-nums',
                    color: v('ink-2')
                }}
            >
                {formatClock(time)} / {formatClock(clock.duration)}
            </Typography>
        </Stack>
    );
};
