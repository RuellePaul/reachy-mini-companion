// The studio's panel: what plays in 3D (a mode's animations, the gestures, the emotions), how pronounced and how
// fast, and the two ways out: try it on the robot now, or keep it for the next wake.
import type {ReactNode} from 'react';
import {Box, Button, CircularProgress, List, ListItemButton, ListItemText, Slider, Stack} from '@mui/material';
import {Tab, Tabs, ToggleButton, ToggleButtonGroup, Typography} from '@mui/material';
import RestartAltRounded from '@mui/icons-material/RestartAltRounded';
import SmartToyRounded from '@mui/icons-material/SmartToyRounded';
import type {TryMove} from '../api/schemas';
import {formatFixed} from '../format';
import {useWords} from '../i18n';
import {v} from '../theme';
import {STUDIO} from './words';

export type Mode = 'idle' | 'listening' | 'talking';
export type Shelf = 'animations' | 'gesture' | 'emotion';
export type Tuning = TryMove['tuning'];
export interface Item {
    key: string;
    label: string;
    hint?: string | null; // a sentence that gets it from the robot
    current?: boolean; // the mode's setting
    move: TryMove;
}
export interface Notice {
    text: string;
    error?: boolean;
    restart?: boolean; // saved: offer to restart the conversation if it is on
}

const factor = (v: number) => `×${formatFixed(v, 2)}`;

// The selected row stays in view (↑ ↓, a pick in the 3D), inside a list that scrolls on its own only.
const reveal = (row: HTMLDivElement | null) => {
    const list = row?.parentElement;
    if (row && list && list.scrollHeight > list.clientHeight) row.scrollIntoView({block: 'nearest'});
};

export const glass = {
    bgcolor: v('glass'),
    backdropFilter: 'blur(10px) saturate(1.4)',
    borderRadius: '12px',
    boxShadow: `0 0 0 1px ${v('line')}`
};

/** One of a few, as toggle buttons: [value, what shows, tooltip]. */
export const Choice = <T,>(p: {
    value: T;
    options: [T, ReactNode, string?][];
    onChange: (v: T) => void;
    sx?: object;
}) => (
    <ToggleButtonGroup
        exclusive
        size="small"
        value={p.value}
        sx={p.sx ?? glass}
        onChange={(_, v: T | null) => v !== null && p.onChange(v)}
    >
        {p.options.map(([v, label, title]) => (
            <ToggleButton key={String(v)} value={v as string} title={title}>
                {label}
            </ToggleButton>
        ))}
    </ToggleButtonGroup>
);

/** A factor slider: it moves freely (its value shown on the thumb), the preview follows once it is let go. */
const Factor = ({label, value, range = [0.5, 1.5], onCommit}: FactorProps) => (
    <Stack direction="row" alignItems="center" spacing={2}>
        <Typography variant="body2" sx={{width: 84, flexShrink: 0, fontWeight: 560}}>
            {label}
        </Typography>
        <Slider
            key={value}
            size="small"
            color="primary"
            min={range[0]}
            max={range[1]}
            step={0.05}
            defaultValue={value}
            valueLabelDisplay="auto"
            valueLabelFormat={factor}
            onChangeCommitted={(_, v) => onCommit(v as number)}
            slotProps={{input: {'aria-label': label}}}
        />
        <Typography
            variant="body2"
            sx={{width: 46, flexShrink: 0, textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: v('ink-2')}}
        >
            {factor(value)}
        </Typography>
    </Stack>
);
interface FactorProps {
    label: string;
    value: number;
    range?: [number, number];
    onCommit: (value: number) => void;
}

interface Props {
    shelf: Shelf;
    onShelf: (shelf: Shelf) => void;
    mode: Mode;
    onMode: (mode: Mode) => void;
    items: Item[];
    selected: number;
    onChoose: (item: Item) => void;
    tuning: Tuning;
    ranges?: {amplitude: [number, number]; speed: [number, number]};
    onTuning: (tuning: Tuning) => void;
    busy: boolean;
    notice: Notice | null;
    onTry: () => void;
    onKeep?: () => void; // absent: nothing new to keep
    onRestart?: () => void;
}

export const Panel = (p: Props) => {
    const t = useWords(STUDIO);
    return (
        <Stack
            sx={{
                width: {md: 380},
                minHeight: {xs: 480, md: 0},
                flexShrink: 0,
                borderLeft: {md: `1px solid ${v('line')}`},
                borderTop: {xs: `1px solid ${v('line')}`, md: 0},
                bgcolor: v('page')
            }}
        >
            <Tabs
                value={p.shelf}
                onChange={(_, shelf: Shelf) => p.onShelf(shelf)}
                variant="fullWidth"
                sx={{px: 1, borderBottom: `1px solid ${v('line')}`}}
            >
                {Object.entries(t.shelves).map(([shelf, label]) => (
                    <Tab key={shelf} value={shelf} label={label} />
                ))}
            </Tabs>
            {p.shelf === 'animations' && (
                <Box sx={{px: 2, pt: 2}}>
                    <Choice
                        value={p.mode}
                        onChange={p.onMode}
                        options={Object.entries(t.modes) as [Mode, string][]}
                        sx={{display: 'flex', bgcolor: v('sunken'), '& > *': {flex: 1}}}
                    />
                </Box>
            )}
            <List sx={{flex: 1, minHeight: 120, overflowY: 'auto', px: 1, py: 1}}>
                {p.items.map((item, i) => (
                    <ListItemButton
                        key={item.key}
                        ref={i === p.selected ? reveal : undefined}
                        selected={i === p.selected}
                        onClick={() => p.onChoose(item)}
                        sx={{py: 0.75, px: 1.5, gap: 1}}
                    >
                        <ListItemText
                            primary={item.label}
                            secondary={item.hint && t.say(item.hint)}
                            slotProps={{secondary: {sx: {color: v('ink-3')}}}}
                            sx={{my: 0}}
                        />
                        {item.current && (
                            <Typography
                                variant="caption"
                                sx={{
                                    px: 0.75,
                                    borderRadius: '6px',
                                    fontWeight: 620,
                                    color: v('ink-2'),
                                    bgcolor: v('sunken')
                                }}
                            >
                                {t.current}
                            </Typography>
                        )}
                    </ListItemButton>
                ))}
            </List>
            <Stack spacing={1.25} sx={{p: 2, borderTop: `1px solid ${v('line')}`}}>
                {p.shelf === 'animations' && (
                    <Box sx={{pb: 0.5}}>
                        <Factor
                            label={t.amplitude}
                            value={p.tuning.amplitude}
                            range={p.ranges?.amplitude}
                            onCommit={(amplitude) => p.onTuning({...p.tuning, amplitude})}
                        />
                        <Factor
                            label={t.speed}
                            value={p.tuning.speed}
                            range={p.ranges?.speed}
                            onCommit={(speed) => p.onTuning({...p.tuning, speed})}
                        />
                    </Box>
                )}
                <Button
                    variant="contained"
                    size="large"
                    startIcon={<SmartToyRounded />}
                    onClick={p.onTry}
                    disabled={p.selected < 0}
                    title={t.tryHint}
                >
                    {t.tryIt}
                </Button>
                {p.shelf === 'animations' && (
                    <Button variant="outlined" color="inherit" size="large" onClick={p.onKeep} disabled={!p.onKeep}>
                        {t.keep}
                    </Button>
                )}
                <Box sx={{minHeight: 24, display: 'flex', alignItems: 'center', gap: 1}}>
                    {p.busy && <CircularProgress size={14} sx={{color: v('ink-3')}} />}
                    <Typography
                        variant="body2"
                        role={p.notice?.error ? 'alert' : 'status'}
                        sx={{flex: 1, color: p.notice?.error ? v('fault') : v('ink-2')}}
                    >
                        {p.notice?.text}
                    </Typography>
                    {p.onRestart && (
                        <Button size="small" color="inherit" startIcon={<RestartAltRounded />} onClick={p.onRestart}>
                            {t.restart}
                        </Button>
                    )}
                </Box>
            </Stack>
        </Stack>
    );
};
