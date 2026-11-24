import {Box, ListItemText, MenuItem, TextField} from '@mui/material';
import {useLang, useWords} from '../i18n';
import {v} from '../theme';
import {PERSONALITIES} from './personalities';
import {SETTINGS} from './words';

const NONE = 'none';
const OWN = 'own';

interface Props {
    /** The personality as the form holds it. */
    value: string;
    onChange: (text: string) => void;
    id: string;
}

/** The ready-made characters: picking one writes its text as the personality, an edit like any other. The list
 * shows the one whose text is there, « Sur mesure » once a word of it changed, « Aucun » for none. */
export const PersonalityPicker = ({value, onChange, id}: Props) => {
    const t = useWords(SETTINGS);
    const english = useLang() === 'en';
    const text = value.trim();
    const shownAs = (p: (typeof PERSONALITIES)[number]) => (english ? p.en : p);
    const current = PERSONALITIES.find((p) => p.text === text);
    const shown = current?.name ?? (text ? OWN : NONE);

    return (
        <TextField
            select
            fullWidth
            id={id}
            size="small"
            value={shown}
            onChange={(e) => onChange(PERSONALITIES.find((p) => p.name === e.target.value)?.text ?? '')}
            slotProps={{
                select: {
                    labelId: `${id}-label`,
                    renderValue: () =>
                        current ? (
                            <>
                                {shownAs(current).name}
                                <Box component="span" sx={{color: v('ink-2'), ml: 1}}>
                                    {t.accent(shownAs(current).accent)}
                                </Box>
                            </>
                        ) : text ? (
                            t.own
                        ) : (
                            t.none
                        ),
                    MenuProps: {slotProps: {paper: {sx: {maxHeight: 420}}}}
                }
            }}
        >
            <MenuItem value={NONE}>
                <ListItemText primary={t.none} secondary={t.noneHint} />
            </MenuItem>
            {shown === OWN && (
                <MenuItem value={OWN} disabled>
                    <ListItemText primary={t.own} secondary={t.ownHint} />
                </MenuItem>
            )}
            {PERSONALITIES.map((p) => (
                <MenuItem key={p.name} value={p.name} sx={{whiteSpace: 'normal'}}>
                    <ListItemText
                        primary={shownAs(p).name}
                        secondary={t.accentAnd(shownAs(p).accent, shownAs(p).trait)}
                    />
                </MenuItem>
            ))}
        </TextField>
    );
};
