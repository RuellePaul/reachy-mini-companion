import {useEffect, useRef, useState, type SyntheticEvent} from 'react';
import {Box, IconButton, InputBase, Typography} from '@mui/material';
import ArrowUpwardRounded from '@mui/icons-material/ArrowUpwardRounded';
import {api, errorText} from '../api/client';
import {SAY_MAX} from '../api/schemas';
import {useWords} from '../i18n';
import {useLive} from '../live';
import {GUTTER, v} from '../theme';
import {TRANSCRIPT} from './words';

/** The start of a sentence for the user to finish; `asked` numbers it, so the same one asked again comes back. */
interface Draft {
    text: string;
    asked: number;
}

/** Typing to the robot: a `say`, which the robot takes as a human turn. A refused message comes back to the box.
 * A `draft` (a palette entry that needs a parameter) takes the focus, on a line after what is typed: it replaces
 * only an earlier draft left as it was. Nothing is sent. */
export const Composer = ({draft = null}: {draft?: Draft | null}) => {
    const {link, robotStatus, session, settings} = useLive();
    const t = useWords(TRANSCRIPT);
    const [text, setText] = useState(draft?.text ?? '');
    const [drafted, setDrafted] = useState(draft);
    if (draft !== drafted) {
        setDrafted(draft);
        const typed = text.trim() && text !== drafted?.text ? text.trimEnd() : '';
        if (draft) setText(typed ? `${typed}\n${draft.text}` : draft.text);
    }
    const box = useRef<HTMLTextAreaElement>(null);
    useEffect(() => {
        const field = box.current;
        if (!draft || !field) return;
        field.focus();
        field.setSelectionRange(field.value.length, field.value.length);
    }, [draft]);
    const [sending, setSending] = useState(false);
    const [error, setError] = useState<{text: string; session: string | null} | null>(null);
    const closing = session.phase === 'closing'; // a say would reach a robot that no longer takes one
    const enabled = link === 'up' && session.awake && !closing;
    // An error belongs to the conversation it happened in: the next one starts without it.
    const shownError = error && !(session.awake && session.session_id !== error.session) ? error.text : null;

    // The box empties as the message leaves: what is typed meanwhile is the next message.
    const send = async (event?: SyntheticEvent) => {
        event?.preventDefault();
        const value = text.trim();
        if (!value || !enabled || sending) return;
        setSending(true);
        setError(null);
        setText('');
        try {
            await api.say(value);
        } catch (err) {
            setText((typed) => (typed ? `${value}\n${typed}` : value));
            setError({text: errorText(err), session: session.session_id});
        } finally {
            setSending(false);
        }
    };

    const placeholder =
        link !== 'up'
            ? t.macDown
            : closing
              ? t.fallingAsleep
              : session.awake
                ? t.writeTo(settings?.settings.name ?? 'Reachy')
                : robotStatus === 'live'
                  ? t.wakeToWrite
                  : robotStatus === 'idle'
                    ? t.runToWrite
                    : t.powerToWrite;
    return (
        <Box
            component="form"
            onSubmit={send}
            sx={{
                width: '100%',
                maxWidth: 808,
                mx: 'auto',
                px: GUTTER,
                pt: 1,
                pb: {xs: 'max(12px, env(safe-area-inset-bottom))', sm: 3},
                flexShrink: 0
            }}
        >
            {shownError && (
                <Typography role="alert" variant="body2" sx={{pb: 1, px: 0.5, color: v('fault')}}>
                    {shownError}
                </Typography>
            )}
            <Box
                sx={{
                    display: 'flex',
                    alignItems: 'flex-end',
                    gap: 1,
                    p: '6px 6px 6px 16px',
                    borderRadius: '14px',
                    bgcolor: enabled ? v('raised') : v('sunken'),
                    border: `1px solid ${v('line')}`,
                    transition: 'border-color 120ms, background-color 120ms',
                    '&:hover': {borderColor: enabled ? v('line-strong') : v('line')},
                    '&:focus-within': {borderColor: v('ink')}
                }}
            >
                <InputBase
                    fullWidth
                    multiline
                    maxRows={6}
                    value={text}
                    inputRef={box}
                    disabled={!enabled}
                    placeholder={placeholder}
                    inputProps={{maxLength: SAY_MAX, 'aria-label': t.message}}
                    onChange={(e) => {
                        setText(e.target.value);
                        setError(null);
                    }}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) void send(e);
                    }}
                    sx={{
                        py: '6px',
                        fontSize: 16,
                        lineHeight: 1.5,
                        '& textarea::placeholder': {color: v('ink-3'), opacity: 1}
                    }}
                />
                <IconButton
                    type="submit"
                    aria-label={t.send}
                    disabled={!enabled || sending || !text.trim()}
                    sx={{
                        width: 36,
                        height: 36,
                        flexShrink: 0,
                        bgcolor: v('lens'),
                        color: v('on-lens'),
                        '&:hover': {bgcolor: v('ink-2'), color: v('on-lens')},
                        '&.Mui-disabled': {bgcolor: 'transparent', color: v('ink-3')}
                    }}
                >
                    <ArrowUpwardRounded fontSize="small" />
                </IconButton>
            </Box>
        </Box>
    );
};
