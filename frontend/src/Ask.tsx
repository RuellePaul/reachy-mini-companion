// A text field and its button: what to build (an app, a scene), what to change, a new name.
import {useState, type Ref} from 'react';
import {Button, Stack, TextField} from '@mui/material';
import {errorText} from './api/client';
import {useKeep} from './surfaces';

interface AskProps {
    label: string;
    action: string;
    onSend: (text: string) => Promise<unknown>;
    initial?: string;
    max?: number;
    inputRef?: Ref<HTMLTextAreaElement>;
    stacked?: boolean; // the button under the field, in a narrow column
    onCancel?: () => void; // Escape: the field goes, not the surface
}

/** A text field and its button: what to build, what to change, a new name. Enter sends, Shift+Enter breaks. */
export const Ask = ({
    label,
    action,
    onSend,
    initial = '',
    max = 2000,
    inputRef,
    stacked = false,
    onCancel
}: AskProps) => {
    const [text, setText] = useState(initial);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    useKeep(text.trim() !== '' && text !== initial);
    const submit = async () => {
        if (!text.trim() || busy) return;
        setBusy(true);
        setError('');
        try {
            await onSend(text.trim());
            setText('');
        } catch (err) {
            setError(errorText(err));
        } finally {
            setBusy(false);
        }
    };
    return (
        <Stack direction={stacked ? 'column' : 'row'} spacing={1} alignItems={stacked ? 'flex-end' : 'flex-start'}>
            <TextField
                size="small"
                fullWidth
                multiline
                maxRows={4}
                label={label}
                value={text}
                inputRef={inputRef}
                error={Boolean(error)}
                helperText={error || undefined}
                onChange={(event) => setText(event.target.value)}
                onKeyDown={(event) => {
                    if (event.key === 'Escape' && onCancel) {
                        event.stopPropagation();
                        return onCancel();
                    }
                    if (event.key !== 'Enter' || event.shiftKey) return;
                    event.preventDefault();
                    void submit();
                }}
                slotProps={{htmlInput: {maxLength: max}}}
            />
            <Button
                variant="contained"
                disabled={busy || !text.trim()}
                onClick={() => void submit()}
                sx={{minHeight: 40, flexShrink: 0}}
            >
                {action}
            </Button>
        </Stack>
    );
};
