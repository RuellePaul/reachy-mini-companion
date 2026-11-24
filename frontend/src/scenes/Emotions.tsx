// « Ajouter des émotions »: Claude reads the draft and the Mac writes its picks in, only where no word changes, so no
// voice is rendered again. The draft is replaced, not saved; « Annuler » puts it back while nobody has typed since.
import {useEffect, useRef, useState} from 'react';
import {Button, CircularProgress, Stack, Typography} from '@mui/material';
import AutoAwesomeRounded from '@mui/icons-material/AutoAwesomeRounded';
import {errorText} from '../api/client';
import {useWords} from '../i18n';
import {v} from '../theme';
import {scenesApi} from './api';
import {SCENES} from './words';

interface EmotionsProps {
    title: string;
    text: string;
    onText: (text: string) => void;
}

/** Mounted per scene: an answer for another scene is never written into this one. */
export const Emotions = ({title, text, onText}: EmotionsProps) => {
    const [busy, setBusy] = useState(false);
    const [done, setDone] = useState<{before: string; after: string; added: number} | null>(null);
    const [message, setMessage] = useState<string | null>(null);
    const t = useWords(SCENES);
    const latest = useRef<string | null>(text); // the draft now, read when Claude answers; null once unmounted
    useEffect(() => {
        latest.current = text;
    }, [text]);
    useEffect(() => () => void (latest.current = null), []);

    const add = async () => {
        const sent = text;
        setBusy(true);
        setMessage(null);
        setDone(null);
        try {
            const {text: after, added} = await scenesApi.emotions(title, sent);
            if (latest.current === null) return; // another scene is open: its text is not this one
            if (latest.current !== sent) setMessage(t.textChanged);
            else if (added === 0) setMessage(t.nothingToAdd);
            else {
                onText(after);
                setDone({before: sent, after, added});
            }
        } catch (err) {
            setMessage(errorText(err));
        } finally {
            setBusy(false);
        }
    };

    return (
        <Stack direction="row" spacing={1} useFlexGap alignItems="center" sx={{minWidth: 0, flexWrap: 'wrap'}}>
            <Button
                variant="outlined"
                color="inherit"
                startIcon={busy ? <CircularProgress size={16} /> : <AutoAwesomeRounded sx={{color: v('think')}} />}
                disabled={busy || !text.trim()}
                onClick={add}
            >
                {busy ? t.reading : t.addEmotions}
            </Button>
            {done && done.after === text && (
                <Typography variant="body2" role="status" sx={{color: v('ink-2')}}>
                    {t.added(done.added)} ·{' '}
                    <Button
                        size="small"
                        color="inherit"
                        onClick={() => onText(done.before)}
                        sx={{minWidth: 0, minHeight: 0, p: 0, verticalAlign: 'baseline', textDecoration: 'underline'}}
                    >
                        {t.undo}
                    </Button>
                </Typography>
            )}
            {message && (
                <Typography variant="body2" role="status" sx={{color: v('attention')}}>
                    {message}
                </Typography>
            )}
        </Stack>
    );
};
