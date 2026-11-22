// A state in a word after a dot of its colour: a tool's outcome in the conversation, an app being built.
import {Box} from '@mui/material';
import {keyframes} from '@mui/material/styles';
import {v, type Token} from './theme';

const beat = keyframes`0%, 100% { opacity: 0.25; } 50% { opacity: 1; }`;
const WORD = {display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: 12, fontWeight: 600} as const;

export const Signal = ({tone, label, busy = false}: {tone: Token; label: string; busy?: boolean}) => (
    <Box component="span" sx={{...WORD, fontStyle: 'normal', whiteSpace: 'nowrap', color: v(tone)}}>
        <Box
            component="span"
            sx={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                bgcolor: 'currentColor',
                animation: busy ? `${beat} 1.2s infinite` : 'none'
            }}
        />
        {label}
    </Box>
);
