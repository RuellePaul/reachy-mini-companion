// What the conversation says around the words exchanged: who speaks, what the robot did, how it ended.
import {words} from '../i18n';

export const TRANSCRIPT = words(
    {
        // A tool's title; Spotify's and the apps' are named by what they are asked, these when it is unknown.
        tools: {
            do_gesture: 'Geste',
            play_emotion: 'Émotion',
            turn_body: 'Rotation du corps',
            set_volume: 'Volume',
            goto_sleep: 'Mise en veille',
            look: 'Regard',
            play_music: 'Musique',
            music: 'Spotify',
            app: 'App',
            play_scene: 'Jouer une scène',
            write_scene: 'Écrire une scène'
        } as Record<string, string>,
        appActions: {create: 'Créer une app', update: 'Modifier une app', open: 'Ouvrir une app'} as Record<
            string,
            string
        >,
        switches: {on: 'en marche', off: 'arrêtée', track: 'de ce morceau'} as Record<string, string>, // shuffle, repeat
        colon: ' : ',
        status: {
            pending: 'en cours',
            unanswered: 'sans réponse',
            ok: 'fait',
            error: 'erreur',
            refused: 'refusé',
            cancelled: 'annulé'
        },
        ended: {
            goto_sleep: 'endormi à la demande',
            dashboard: 'endormi depuis le tableau de bord',
            idle: 'mis en veille après inactivité',
            connection_lost: 'connexion à Gemini perdue',
            refused: 'Gemini a refusé la connexion',
            shutdown: 'agent arrêté',
            error: 'erreur'
        } as Record<string, string>,
        you: 'Toi',
        typed: 'au clavier',
        seen: 'Ce que voit le robot',
        start: 'Début de la conversation',
        end: 'Fin de la conversation',
        writtenHere: (name: string) => `Ta conversation avec ${name} s’écrira ici.`,
        fallingAsleep: 'Il s’endort…',
        talk: 'Parle-lui, ou écris-lui.',
        wake: 'Bouge une antenne, ou appuie sur Réveiller.',
        macDown: 'Mac injoignable',
        writeTo: (name: string) => `Écris à ${name}…`,
        wakeToWrite: 'Réveille-le pour lui écrire',
        runToWrite: 'Lance son agent pour lui écrire',
        powerToWrite: 'Allume-le pour lui écrire',
        message: 'Message au robot',
        send: 'Envoyer'
    },
    {
        tools: {
            do_gesture: 'Gesture',
            play_emotion: 'Emotion',
            turn_body: 'Body turn',
            set_volume: 'Volume',
            goto_sleep: 'Sleep',
            look: 'Look',
            play_music: 'Music',
            music: 'Spotify',
            app: 'App',
            play_scene: 'Play a scene',
            write_scene: 'Write a scene'
        },
        appActions: {create: 'Create an app', update: 'Change an app', open: 'Open an app'},
        switches: {on: 'on', off: 'off', track: 'this track'},
        colon: ': ',
        status: {
            pending: 'in progress',
            unanswered: 'no answer',
            ok: 'done',
            error: 'error',
            refused: 'refused',
            cancelled: 'cancelled'
        },
        ended: {
            goto_sleep: 'put to sleep on request',
            dashboard: 'put to sleep from the dashboard',
            idle: 'asleep after a long silence',
            connection_lost: 'connection to Gemini lost',
            refused: 'Gemini refused the connection',
            shutdown: 'agent stopped',
            error: 'error'
        },
        you: 'You',
        typed: 'typed',
        seen: 'What the robot sees',
        start: 'Start of the conversation',
        end: 'End of the conversation',
        writtenHere: (name: string) => `Your conversation with ${name} will be written here.`,
        fallingAsleep: 'It is falling asleep…',
        talk: 'Talk to it, or write to it.',
        wake: 'Move an antenna, or press Wake up.',
        macDown: 'Mac unreachable',
        writeTo: (name: string) => `Write to ${name}…`,
        wakeToWrite: 'Wake it up to write to it',
        runToWrite: 'Start its agent to write to it',
        powerToWrite: 'Switch it on to write to it',
        message: 'Message to the robot',
        send: 'Send'
    }
);
