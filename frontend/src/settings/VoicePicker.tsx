import {useRef, useState, type MouseEvent} from 'react';
import {Box, IconButton, ListItemText, MenuItem, Stack, TextField, Tooltip} from '@mui/material';
import PlayArrowRounded from '@mui/icons-material/PlayArrowRounded';
import StopRounded from '@mui/icons-material/StopRounded';
import type {VoiceOption} from '../api/schemas';
import {useWords} from '../i18n';
import {mac} from '../mac';
import {v} from '../theme';
import {SETTINGS} from './words';

const sampleUrl = (name: string) => `/api/voices/${encodeURIComponent(name)}/sample`;

interface Props {
    value: string;
    options: VoiceOption[];
    onChange: (name: string) => void;
    id: string;
}

/** The voice list, each with its gender and character, and a sample to hear; one <audio> plays them all. */
export const VoicePicker = ({value, options, onChange, id}: Props) => {
    const t = useWords(SETTINGS);
    const audio = useRef<HTMLAudioElement>(null);
    const [playing, setPlaying] = useState<string | null>(null);
    const stop = () => setPlaying(null);

    const toggle = (event: MouseEvent, name: string) => {
        event.stopPropagation(); // a play button inside the menu must not pick the voice
        const element = audio.current;
        if (!element) return;
        element.pause();
        if (playing === name) return stop();
        element.src = sampleUrl(name);
        setPlaying(name);
        element.play().catch(() => setPlaying((current) => (current === name ? null : current)));
    };

    const playButton = (name: string) => (
        <Tooltip title={playing === name ? t.stop : t.hearSample}>
            <IconButton
                size="small"
                aria-label={t.hear(name)}
                sx={playing === name ? {color: v('ink'), bgcolor: v('selected')} : undefined}
                onMouseDown={(e) => e.stopPropagation()}
                onClick={(e) => toggle(e, name)}
            >
                {playing === name ? <StopRounded fontSize="small" /> : <PlayArrowRounded fontSize="small" />}
            </IconButton>
        </Tooltip>
    );

    const describe = (name: string) => {
        const voice = options.find((o) => o.name === name);
        if (!voice) return name;
        return (
            <>
                {voice.name}
                <Box component="span" sx={{color: v('ink-2'), ml: 1}}>
                    {t.gender[voice.gender].toLowerCase()}, {mac(voice.descriptor)}
                </Box>
            </>
        );
    };

    return (
        <Stack direction="row" spacing={1} alignItems="center">
            <TextField
                select
                fullWidth
                id={id}
                size="small"
                value={value}
                onChange={(e) => onChange(e.target.value)}
                slotProps={{
                    select: {
                        labelId: `${id}-label`,
                        renderValue: (selected) => describe(String(selected)),
                        MenuProps: {slotProps: {paper: {sx: {maxHeight: 420}}}}
                    }
                }}
            >
                {options.map((voice) => (
                    <MenuItem key={voice.name} value={voice.name}>
                        <ListItemText
                            primary={voice.name}
                            secondary={`${t.gender[voice.gender]}, ${mac(voice.descriptor)}`}
                        />
                        {playButton(voice.name)}
                    </MenuItem>
                ))}
            </TextField>
            {playButton(value)}
            <audio ref={audio} onEnded={stop} onError={stop} />
        </Stack>
    );
};
