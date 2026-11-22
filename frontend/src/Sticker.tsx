// Pollen Robotics' and Seeed Studio's drawings of Reachy (public/stickers, origins in its SOURCES.md): the robot
// itself, and one costume per place of the dashboard. Decorative: a word always says the same thing beside it.
import {Box} from '@mui/material';

const FILE = {
    reachy: 'reachy.svg',
    settings: 'settings.svg',
    studio: 'studio.webp',
    scenes: 'scenes.webp',
    apps: 'apps.svg'
} as const;

export type StickerName = keyof typeof FILE;

export const Sticker = ({name, size}: {name: StickerName; size: number}) => (
    <Box
        component="img"
        src={`/stickers/${FILE[name]}`}
        alt=""
        draggable={false}
        sx={{display: 'block', width: size, height: size, objectFit: 'contain', flexShrink: 0, userSelect: 'none'}}
    />
);
