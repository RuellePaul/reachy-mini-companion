// What the robot's column and the bar above the conversation say.
import type {Phase} from '../api/schemas';
import {words} from '../i18n';

interface Look {
    label: string;
    detail: string;
}

export const HEADER = words(
    {
        phase: {
            connecting: {label: 'Se réveille…', detail: 'Il ouvre la conversation.'},
            reconnecting: {label: 'Se reconnecte…', detail: 'Il rétablit la liaison avec Gemini.'},
            idle: {label: 'En attente', detail: 'Parle-lui, ou écris-lui.'},
            listening: {label: 'À l’écoute', detail: 'Il t’entend.'},
            thinking: {label: 'Réfléchit', detail: 'Il prépare sa réponse.'},
            speaking: {label: 'Parle', detail: 'Il te répond.'},
            closing: {label: 'S’endort…', detail: 'La conversation se termine.'}
        } as Record<Phase, Look>,
        linking: 'Connexion…',
        macDown: {label: 'Mac injoignable', detail: 'Reconnexion en cours.'},
        scene: (awake: boolean) => ({
            label: 'Joue une scène',
            detail: `Tu la suis dans Scènes. ${awake ? 'L’endormir' : 'Le réveiller'} l’arrête.`
        }),
        hushed: {label: 'Se tait', detail: 'Pendant la partie. Une antenne touchée lui rend la parole.'},
        asleep: (antennas: boolean) => ({
            label: 'En veille',
            detail: antennas
                ? 'Bouge une antenne pour le réveiller.'
                : 'Il bouge : les antennes le réveilleront au repos.'
        }),
        stopped: {label: 'Agent arrêté', detail: 'Lance make run.'},
        off: {label: 'Hors ligne', detail: 'Éteint ou hors réseau.'},
        loading: 'Chargement…',
        wake: 'Réveiller',
        sleep: 'Endormir',
        volume: 'Volume',
        robotVolume: 'Volume du robot',
        cost: 'Cette conversation',
        costHint: 'Coût estimé de la conversation en cours (tarifs Gemini Live).',
        latency: 'Dernier tour',
        latencyHint:
            'Latence du dernier tour, mesurée sur le robot : de la fin de ta phrase au premier son de sa réponse.',
        openPalette: 'Ouvrir la palette',
        whatShould: (name?: string) => (name ? `Que doit faire ${name} ?` : 'Que doit-il faire ?'),
        places: {
            studio: {label: 'Studio', hint: 'Ses mouvements en 3D, avant le robot'},
            scenes: {label: 'Scènes', hint: 'Les pièces qu’il joue'},
            apps: {label: 'Apps', hint: 'Les pages qui le font bouger'},
            settings: {label: 'Réglages', hint: 'Son nom, sa voix, sa personnalité'}
        }
    },
    {
        phase: {
            connecting: {label: 'Waking up…', detail: 'It is opening the conversation.'},
            reconnecting: {label: 'Reconnecting…', detail: 'It is restoring its link with Gemini.'},
            idle: {label: 'Waiting', detail: 'Talk to it, or write to it.'},
            listening: {label: 'Listening', detail: 'It hears you.'},
            thinking: {label: 'Thinking', detail: 'It is preparing its answer.'},
            speaking: {label: 'Speaking', detail: 'It is answering you.'},
            closing: {label: 'Falling asleep…', detail: 'The conversation is ending.'}
        },
        linking: 'Connecting…',
        macDown: {label: 'Mac unreachable', detail: 'Reconnecting.'},
        scene: (awake: boolean) => ({
            label: 'Playing a scene',
            detail: `Follow it in Scenes. ${awake ? 'Putting it to sleep' : 'Waking it up'} stops it.`
        }),
        hushed: {label: 'Keeping quiet', detail: 'While you play. Touch an antenna to give it its voice back.'},
        asleep: (antennas: boolean) => ({
            label: 'Asleep',
            detail: antennas
                ? 'Move an antenna to wake it up.'
                : 'It is moving: the antennas will wake it once it rests.'
        }),
        stopped: {label: 'Agent stopped', detail: 'Run make run.'},
        off: {label: 'Offline', detail: 'Switched off, or off the network.'},
        loading: 'Loading…',
        wake: 'Wake up',
        sleep: 'Put to sleep',
        volume: 'Volume',
        robotVolume: 'The robot’s volume',
        cost: 'This conversation',
        costHint: 'Estimated cost of the conversation under way (Gemini Live prices).',
        latency: 'Last turn',
        latencyHint:
            'Latency of the last turn, measured on the robot: from the end of your sentence to the first sound of its answer.',
        openPalette: 'Open the palette',
        whatShould: (name?: string) => (name ? `What should ${name} do?` : 'What should it do?'),
        places: {
            studio: {label: 'Studio', hint: 'Its moves in 3D, before the robot'},
            scenes: {label: 'Scenes', hint: 'The plays it performs'},
            apps: {label: 'Apps', hint: 'The pages that make it move'},
            settings: {label: 'Settings', hint: 'Its name, its voice, its personality'}
        }
    }
);
