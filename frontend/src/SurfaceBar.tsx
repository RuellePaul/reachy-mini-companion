// The bar over each workspace (studio, scenes, apps): its picture and name, and the way back to the conversation.
import {Box, IconButton, Stack, Typography} from '@mui/material';
import CloseRounded from '@mui/icons-material/CloseRounded';
import {useWords} from './i18n';
import {Sticker, type StickerName} from './Sticker';
import {v} from './theme';
import {FRAME} from './words';

interface Props {
    title: string;
    sticker: StickerName;
    onClose: () => void;
}

export const SurfaceBar = ({title, sticker, onClose}: Props) => {
    const t = useWords(FRAME);
    return (
        <Stack
            direction="row"
            alignItems="center"
            spacing={1.5}
            sx={{
                height: 56,
                px: {xs: 1.5, sm: 2.5},
                flexShrink: 0,
                bgcolor: v('plate'),
                borderBottom: `1px solid ${v('line')}`
            }}
        >
            <Stack direction="row" alignItems="center" spacing={1}>
                <Sticker name={sticker} size={34} />
                <Typography variant="h5" component="h2" noWrap>
                    {title}
                </Typography>
            </Stack>
            <Box sx={{flex: 1}} />
            <IconButton aria-label={t.close} onClick={onClose} sx={{mr: -1}}>
                <CloseRounded />
            </IconButton>
        </Stack>
    );
};
