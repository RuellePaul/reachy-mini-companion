// What the apps workspace says, and the manette's bar over an app. An app's own page is as its builder wrote it.
import {formatCount} from '../format';
import {words} from '../i18n';
import type {Body} from './pad';

export const APPS = words(
    {
        title: 'Apps',
        status: {building: 'En construction', ready: 'Prête', failed: 'Échec'},
        dates: 'fr-FR',
        onDay: (day: string) => `le ${day}`,
        changeFailed: (why: string) => `Modification échouée : ${why}`,
        back: 'Retour à la liste',
        shipped: 'fournie avec Reachy',
        shippedShort: 'fournie',
        changed: (when: string) => `modifiée ${when}`,
        quiet: 'Reachy se tait pendant le jeu',
        listens: 'Reachy écoute pendant le jeu',
        quietLabel: 'Silence pendant le jeu',
        rename: 'Renommer',
        restoreShipped: 'Rétablir l’app fournie',
        restore: 'Rétablir',
        delete: 'Supprimer',
        newName: 'Nouveau nom',
        whatToChange: 'Que faut-il changer ?',
        change: 'Modifier',
        removeAsk: (title: string, builtin: boolean) =>
            builtin ? `Rétablir « ${title} » telle que fournie ?` : `Supprimer « ${title} » ?`,
        cancel: 'Annuler',
        describe: 'Décris une nouvelle app',
        create: 'Créer',
        none: 'Aucune app : décris-en une.',
        choose: 'Choisis une app, ou décris-en une nouvelle.',
        // A build under way
        apiCost: 'Ce que coûterait la construction au tarif de l’API : l’abonnement Claude la couvre.',
        atApiRates: (cost: string) => `${cost} au tarif API`,
        phase: {
            starting: 'Claude se prépare',
            thinking: 'Claude réfléchit',
            writing: 'Claude écrit la page',
            reading: 'Claude lit la page',
            editing: 'Claude modifie la page',
            checking: 'La page est lancée une fois pour la vérifier'
        },
        tokens: (n: number) => ` (~${formatCount(n)} jetons)`,
        chars: (n: number) => ` (${formatCount(n)} caractères)`,
        repair: 'Réparation : ',
        building: 'En construction',
        refusedOnce: (problems: string[]) => `Refusée une première fois : ${problems.join(' ; ')}`,
        thinkingFirst: 'Claude réfléchit avant d’écrire : la page s’écrira ici.',
        writtenHere: 'La page s’écrira ici.',
        draft: 'Aperçu de la page en cours',
        // The frame
        reload: 'Recharger',
        gone: 'L’app a quitté sa page : elle est arrêtée et ne peut plus faire bouger Reachy.',
        // The manette
        notWaking: 'Reachy ne se réveille pas : la partie se joue sans lui.',
        noPose: 'Reachy n’a pas pu se mettre en place : réessaie.',
        refused: {
            scene: 'Une scène se joue : la manette est refusée.',
            sleep: 'Reachy s’endort : la manette est refusée.'
        },
        asks: {
            gachettes: 'veut se servir de Reachy comme manette : ses antennes deviennent des boutons.',
            volant: 'veut se servir de Reachy comme volant : le moteur de sa base est relâché, tourne-la à la main.',
            paume: 'veut Reachy sous ta paume : sa base est relâchée, ses antennes baissées sont des boutons sous tes doigts.',
            tete: 'veut se servir de la tête de Reachy comme manette : la tête et la base sont relâchées, reste à côté.'
        } satisfies Record<Body, string>,
        asksEar: 'veut écouter par le micro de Reachy : le niveau du son seulement, aucun son ne quitte le robot.',
        asksEarToo: 'Elle écoute aussi le niveau du son par son micro.',
        held: {
            gachettes: 'Manette : les antennes sont des boutons.',
            volant: 'Volant : tourne la base de Reachy.',
            paume: 'Sous ta paume : tourne la base de Reachy, tes doigts sur ses antennes.',
            tete: 'Manette : bouge la tête de Reachy.'
        } satisfies Record<Body, string>,
        heldEar: 'Reachy écoute : l’app reçoit le niveau du son, rien d’autre.',
        heldEarToo: 'Il écoute aussi le niveau du son.',
        giveBack: 'Rendre la main',
        letGo: (head: boolean) =>
            `Lâche Reachy et ramène ${head ? 'sa base et sa tête' : 'sa base'} face à toi : il reprend son couple dès qu’il ne bouge plus.`,
        wakingToPlay: 'Reachy se réveille pour jouer…',
        lend: (title: string, asks: string) => `« ${title} » ${asks} Clique dans l’app pour le lui prêter.`,
        settling: 'Reachy se met en place…'
    },
    {
        title: 'Apps',
        status: {building: 'Being built', ready: 'Ready', failed: 'Failed'},
        dates: 'en-US',
        onDay: (day: string) => `on ${day}`,
        changeFailed: (why: string) => `The change failed: ${why}`,
        back: 'Back to the list',
        shipped: 'shipped with Reachy',
        shippedShort: 'shipped',
        changed: (when: string) => `changed ${when}`,
        quiet: 'Reachy keeps quiet during the game',
        listens: 'Reachy listens during the game',
        quietLabel: 'Quiet during the game',
        rename: 'Rename',
        restoreShipped: 'Restore the shipped app',
        restore: 'Restore',
        delete: 'Delete',
        newName: 'New name',
        whatToChange: 'What should change?',
        change: 'Change',
        removeAsk: (title: string, builtin: boolean) =>
            builtin ? `Restore “${title}” as it shipped?` : `Delete “${title}”?`,
        cancel: 'Cancel',
        describe: 'Describe a new app',
        create: 'Create',
        none: 'No app yet: describe one.',
        choose: 'Pick an app, or describe a new one.',
        apiCost: 'What the build would cost at API rates: the Claude subscription covers it.',
        atApiRates: (cost: string) => `${cost} at API rates`,
        phase: {
            starting: 'Claude is getting ready',
            thinking: 'Claude is thinking',
            writing: 'Claude is writing the page',
            reading: 'Claude is reading the page',
            editing: 'Claude is changing the page',
            checking: 'The page is run once to check it'
        },
        tokens: (n: number) => ` (~${formatCount(n)} tokens)`,
        chars: (n: number) => ` (${formatCount(n)} characters)`,
        repair: 'Repair: ',
        building: 'Being built',
        refusedOnce: (problems: string[]) => `Refused a first time: ${problems.join('; ')}`,
        thinkingFirst: 'Claude is thinking before it writes: the page will be written here.',
        writtenHere: 'The page will be written here.',
        draft: 'Preview of the page so far',
        reload: 'Reload',
        gone: 'The app left its page: it is stopped and can no longer move Reachy.',
        notWaking: 'Reachy is not waking up: the game goes on without it.',
        noPose: 'Reachy could not get into place: try again.',
        refused: {
            scene: 'A scene is playing: the controller is refused.',
            sleep: 'Reachy is falling asleep: the controller is refused.'
        },
        asks: {
            gachettes: 'wants to use Reachy as a controller: its antennas become buttons.',
            volant: 'wants to use Reachy as a steering wheel: the motor of its base is released, turn it by hand.',
            paume: 'wants Reachy under your palm: its base is released, its lowered antennas are buttons under your fingers.',
            tete: 'wants to use Reachy’s head as a controller: the head and the base are released, stay beside it.'
        },
        asksEar: 'wants to listen through Reachy’s microphone: the sound level only, no sound leaves the robot.',
        asksEarToo: 'It also listens to the sound level through its microphone.',
        held: {
            gachettes: 'Controller: the antennas are buttons.',
            volant: 'Steering wheel: turn Reachy’s base.',
            paume: 'Under your palm: turn Reachy’s base, your fingers on its antennas.',
            tete: 'Controller: move Reachy’s head.'
        },
        heldEar: 'Reachy is listening: the app gets the sound level, nothing else.',
        heldEarToo: 'It also listens to the sound level.',
        giveBack: 'Take it back',
        letGo: (head: boolean) =>
            `Let go of Reachy and bring ${head ? 'its base and its head' : 'its base'} back to face you: it takes its torque back as soon as it stops moving.`,
        wakingToPlay: 'Reachy is waking up to play…',
        lend: (title: string, asks: string) => `“${title}” ${asks} Click in the app to lend it.`,
        settling: 'Reachy is getting into place…'
    }
);
