// What the dashboard's frame says, whatever is open: unsaved work, what the robot launches, a failure.
import {words} from './i18n';

export const FRAME = words(
    {
        close: 'Fermer',
        show: 'Voir',
        stop: 'Arrêter',
        losing: 'Ce qui n’est pas enregistré sera perdu.',
        showAnyway: 'Voir quand même',
        closeAnyway: 'Fermer quand même',
        dashboardFailed: 'Le tableau de bord n’a pas pu s’afficher',
        surfaceFailed: 'Cette fenêtre n’a pas pu s’ouvrir',
        reloadToRetry: 'Recharge la page pour réessayer.',
        reload: 'Recharger la page',
        opens: (title: string) => `Reachy ouvre « ${title} ».`,
        built: (title: string, ready: boolean) => `« ${title} » ${ready ? 'est prête' : "n'a pas abouti"}.`,
        writes: 'Reachy écrit une scène.',
        plays: (title: string) => `Reachy joue « ${title} ».`,
        unexpected: 'Erreur inattendue.',
        macSilent: 'Le Mac ne répond pas.',
        macRefused: (status: number) => `Le Mac a refusé la demande (erreur ${status}).`,
        macUnread: 'Le Mac a envoyé une réponse inattendue.',
        joined: (messages: string[]) => messages.join(' ; ')
    },
    {
        close: 'Close',
        show: 'Show',
        stop: 'Stop',
        losing: 'Anything unsaved will be lost.',
        showAnyway: 'Show anyway',
        closeAnyway: 'Close anyway',
        dashboardFailed: 'The dashboard could not be displayed',
        surfaceFailed: 'This window could not open',
        reloadToRetry: 'Reload the page to try again.',
        reload: 'Reload the page',
        opens: (title: string) => `Reachy is opening “${title}”.`,
        built: (title: string, ready: boolean) => `“${title}” ${ready ? 'is ready' : 'could not be built'}.`,
        writes: 'Reachy is writing a scene.',
        plays: (title: string) => `Reachy is playing “${title}”.`,
        unexpected: 'Unexpected error.',
        macSilent: 'The Mac is not answering.',
        macRefused: (status: number) => `The Mac refused the request (error ${status}).`,
        macUnread: 'The Mac sent an unexpected answer.',
        joined: (messages: string[]) => messages.join('; ')
    }
);
