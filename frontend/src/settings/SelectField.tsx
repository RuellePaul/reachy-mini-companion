import type {ReactNode} from 'react';
import {Box, MenuItem, Stack, Switch, TextField, Typography} from '@mui/material';
import {v} from '../theme';

interface Option<T> {
    value: T;
    label: string;
}

/** A field of the form: its label above it, what it means below it. */
export const Field = ({
    label,
    id,
    hint,
    children
}: {
    label: string;
    id: string;
    hint?: ReactNode;
    children: ReactNode;
}) => (
    <Stack spacing={0.75}>
        <Typography component="label" id={`${id}-label`} htmlFor={id} variant="body2" sx={{fontWeight: 600}}>
            {label}
        </Typography>
        {children}
        {hint && (
            <Typography variant="caption" sx={{color: v('ink-2')}}>
                {hint}
            </Typography>
        )}
    </Stack>
);

/** An on/off setting: what it is and what it does on the left, its switch on the right. */
export const SwitchField = ({
    label,
    id,
    hint,
    checked,
    onChange
}: {
    label: string;
    id: string;
    hint: string;
    checked: boolean;
    onChange: (checked: boolean) => void;
}) => (
    <Stack direction="row" alignItems="center" spacing={2}>
        <Box sx={{flex: 1}}>
            <Typography component="label" htmlFor={id} variant="body2" sx={{fontWeight: 600}}>
                {label}
            </Typography>
            <Typography variant="caption" component="div" sx={{color: v('ink-2')}}>
                {hint}
            </Typography>
        </Box>
        <Switch id={id} checked={checked} onChange={(e) => onChange(e.target.checked)} />
    </Stack>
);

interface Props<T extends string | number> {
    label: string;
    value: T;
    options: Option<T>[];
    onChange: (value: T) => void;
    hint?: string;
    id: string;
}

/** Every select of the form: a labelled list of options, typed by their values. */
export function SelectField<T extends string | number>({label, value, options, onChange, hint, id}: Props<T>) {
    return (
        <Field label={label} id={id} hint={hint}>
            <TextField
                select
                id={id}
                fullWidth
                size="small"
                value={value}
                slotProps={{select: {labelId: `${id}-label`}}}
                onChange={(e) => {
                    const picked = options.find((o) => String(o.value) === String(e.target.value));
                    if (picked) onChange(picked.value);
                }}
            >
                {options.map((o) => (
                    <MenuItem key={o.value} value={o.value}>
                        {o.label}
                    </MenuItem>
                ))}
            </TextField>
        </Field>
    );
}
