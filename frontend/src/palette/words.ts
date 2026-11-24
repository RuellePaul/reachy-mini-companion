// What the palette says around the catalogue's entries, which are the Mac's.
import {words} from '../i18n';

export const PALETTE = words(
    {
        robot: 'le robot',
        ask: (name: string) => `Que doit faire ${name} ?`,
        search: 'Chercher une action',
        say: 'Dis',
        quoted: (sentence: string) => `« ${sentence} »`,
        open: 'Ouvrir',
        preview: 'Aperçu',
        previewing: (label: string) => `Aperçu : ${label}`,
        noPreview: (label: string) => `Pas d’aperçu 3D pour « ${label} ».`,
        nothing: (query: string) => `Rien ne correspond à « ${query} ». Essaie un autre mot : geste, musique, scène…`,
        choose: 'choisir',
        run: 'lancer',
        preview3d: 'aperçu 3D'
    },
    {
        robot: 'the robot',
        ask: (name: string) => `What should ${name} do?`,
        search: 'Search for an action',
        say: 'Say',
        quoted: (sentence: string) => `“${sentence}”`,
        open: 'Open',
        preview: 'Preview',
        previewing: (label: string) => `Preview: ${label}`,
        noPreview: (label: string) => `No 3D preview for “${label}”.`,
        nothing: (query: string) => `Nothing matches “${query}”. Try another word: gesture, music, scene…`,
        choose: 'choose',
        run: 'run',
        preview3d: '3D preview'
    }
);
