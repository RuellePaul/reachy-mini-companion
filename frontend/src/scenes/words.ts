// What the scenes workspace says. A scene's own text is the human's to write: only the one a new scene starts with
// is here, in the play-script grammar, whose words (NARRATEUR, the gestures, the sounds) are French.
import {formatCount} from '../format';
import {words} from '../i18n';

const count = (n: number, one: string, many: string) => `${formatCount(n)} ${n > 1 ? many : one}`;

const NEW_SCENE = `@NARRATEUR (Charon, 0) : conteur malicieux
@REACHY (Puck, 40) : le petit robot, enjoué

Il était une fois un petit robot.

REACHY : [enthusiastic1] Bonjour tout le monde !
`;

export const SCENES = words(
    {
        title: 'Scènes',
        newScene: 'Nouvelle scène',
        newSceneText: NEW_SCENE,
        untitled: 'Sans titre',
        describe: 'Décris une nouvelle scène',
        write: 'Écrire',
        errors: (n: number) => `${n} erreur${n > 1 ? 's' : ''}`,
        voices: (done: number, total: number) => `${done}/${total} voix`,
        cannotPlay: (errors: string) => `${errors} : elle ne peut pas se jouer`,
        voicesReady: (voices: string, changed: boolean) => `${voices} prêtes${changed ? ', scène modifiée' : ''}`,
        robot: {
            started: (title: string) => `Le robot joue « ${title} ».`,
            line: (title: string, line: number | null) => `Le robot joue « ${title} » : ligne ${line}.`,
            waiting: (who: string, said: string) => `À toi${who && ` (${who})`} : ${said}`,
            ended: (title: string) => `« ${title} » : jouée jusqu'au bout.`,
            stopped: (title: string, why: string | null) => `« ${title} » : ${why ?? 'arrêtée.'}`,
            failed: (title: string, why: string | null) => `« ${title} » : ${why ?? 'échec.'}`
        },
        notStarted: "Le robot n'a pas lancé la scène : est-il allumé et connecté ?",
        titleField: 'Titre',
        save: 'Enregistrer',
        more: 'Plus',
        restoreOriginal: "Rétablir l'original",
        deleteScene: 'Supprimer la scène',
        discardAsk: 'Abandonner ce qui n’est pas enregistré ?',
        cancel: 'Annuler',
        discard: 'Abandonner',
        restoreAsk: (title: string) => `Rétablir « ${title} » tel qu'à l'origine ?`,
        deleteAsk: (title: string) => `Supprimer « ${title} » ?`,
        restore: 'Rétablir',
        delete: 'Supprimer',
        // Writing: a scene asked for, as it is prepared
        apiCost: 'Ce que coûterait l’écriture au tarif de l’API : l’abonnement Claude la couvre.',
        atApiRates: (cost: string) => `${cost} au tarif API`,
        claudeWrites: (chars: number) => `Claude écrit la pièce (${count(chars, 'caractère', 'caractères')})`,
        claudeThinks: (tokens: number) => `Claude réfléchit${tokens ? ` (~${count(tokens, 'jeton', 'jetons')})` : ''}`,
        claudePrepares: 'Claude se prépare',
        writeStep: {
            title: 'Écriture',
            waiting: 'En attente',
            second: 'Deuxième écriture : ',
            done: (lines: number, people: number) =>
                `${count(lines, 'réplique', 'répliques')}, ${count(people, 'personnage', 'personnages')}`,
            cancelled: 'Annulée',
            failed: 'Sans pièce jouable'
        },
        stageStep: {
            title: 'Mise en scène',
            waiting: 'Après l’écriture',
            running: 'Claude choisit les émotions',
            done: (moves: number) =>
                moves ? count(moves, 'mouvement ajouté', 'mouvements ajoutés') : 'Aucun mouvement ajouté',
            cancelled: 'Annulée',
            failed: 'Sans émotions'
        },
        voiceStep: {
            title: 'Voix',
            waiting: 'Après l’écriture',
            running: (voices: string) => `${voices} répliques rendues`,
            done: (total: number) => count(total, 'voix prête', 'voix prêtes'),
            cancelled: (voices: string) => `Annulées à ${voices}`,
            failed: (voices: string) => `Interrompues à ${voices}`
        },
        row: {
            ready: 'Prête',
            failed: 'Échec',
            cancelled: 'Annulée',
            writing: 'Écriture',
            voices: (done: number, total: number) => `Voix ${done}/${total}`,
            staging: 'Mise en scène'
        },
        play: 'La pièce',
        readyIn: (time: string, voiced: boolean) =>
            voiced ? `Prête en ${time} : Reachy l’annonce, puis la joue.` : `Prête en ${time}.`,
        stoppedKept: 'Arrêtée : la scène écrite est gardée, telle qu’elle était.',
        cancelledBefore: 'Annulée avant d’être écrite.',
        asked: (voiced: boolean): string => (voiced ? 'SCÈNE DEMANDÉE À LA VOIX' : 'SCÈNE DEMANDÉE'),
        aNewScene: 'Une nouvelle scène',
        quoted: (text: string) => `« ${text} »`,
        steps: 'Étapes',
        refusedOnce: (problems: string[]) => `Refusée une première fois : ${problems.join(' ; ')}`,
        playOnRobot: 'Jouer sur le robot',
        openScene: 'Ouvrir la scène',
        askAgain: 'Redemander',
        cast: 'Distribution',
        noPlay: 'Aucune pièce n’a été écrite.',
        thinkingFirst: 'Claude réfléchit avant d’écrire : la pièce s’écrira ici.',
        writtenHere: 'La pièce s’écrira ici.',
        // The editor
        text: 'Texte de la scène',
        playFrom: (line: number) => `Jouer à partir de la ligne ${line}`,
        insert: 'Insérer',
        insertHint:
            'au curseur, ou d’un clic droit dans le texte : une émotion, un geste, une pause, un son ou une voix. Clic sur un numéro de ligne : le robot joue à partir de là, même en pleine scène.',
        issues: 'Erreurs',
        issue: (line: number, message: string) => `ligne ${line} : ${message}`,
        // The play
        voicesComing: 'Les voix se préparent : la scène se jouera dès qu’elles seront prêtes.',
        rehearsal: (who: string) => `Répétition : ${who}`,
        noMoves: 'Sans mouvements',
        stop: 'Arrêter',
        voicesCancel: 'Voix en préparation, annuler',
        playOptions: 'Options du jeu',
        rehearse: 'Répéter un rôle (tu dis ses répliques)',
        nobody: 'Personne : le robot joue tout',
        noExpressive: 'Sans mouvements expressifs',
        rehearseHint: (who: string) =>
            `Sur les répliques de ${who}, le robot se tait : touche une antenne pour passer à la suite.`,
        // The emotions
        textChanged: 'Le texte a changé pendant que Claude lisait : relance-le.',
        nothingToAdd: "Claude n'a rien trouvé à ajouter.",
        reading: 'Claude lit la scène…',
        addEmotions: 'Ajouter des émotions',
        added: (n: number) => `${n} émotion${n > 1 ? 's' : ''} ajoutée${n > 1 ? 's' : ''}, voix inchangées`,
        undo: 'Annuler',
        // The insert menu
        groups: {
            emotions: 'Émotions',
            gestures: 'Gestes',
            turns: 'Tourner',
            pauses: 'Silences',
            sounds: 'Sons',
            voices: 'Voix'
        },
        notes: {
            turns: '+ à gauche, - à droite. Un angle par personnage : @CRÉON (Gacrux, -60) : … — le robot se tourne vers lui dès qu’il parle.',
            voices: 'Dans la distribution : @NOM (Voix) : jeu.'
        } as Partial<Record<string, string>>,
        search: 'Chercher : colère, oui, rire, Kore…',
        nothing: (query: string) => `Rien ne correspond à « ${query} ».`
    },
    {
        title: 'Scenes',
        newScene: 'New scene',
        newSceneText: `@NARRATEUR (Charon, 0): mischievous storyteller
@REACHY (Puck, 40): the little robot, cheerful

Once upon a time there was a little robot.

REACHY: [enthusiastic1] Hello everyone!
`,
        untitled: 'Untitled',
        describe: 'Describe a new scene',
        write: 'Write',
        errors: (n: number) => `${n} error${n > 1 ? 's' : ''}`,
        voices: (done: number, total: number) => `${done}/${total} voices`,
        cannotPlay: (errors: string) => `${errors}: it cannot be played`,
        voicesReady: (voices: string, changed: boolean) => `${voices} ready${changed ? ', scene changed' : ''}`,
        robot: {
            started: (title: string) => `The robot is playing “${title}”.`,
            line: (title: string, line: number | null) => `The robot is playing “${title}”: line ${line}.`,
            waiting: (who: string, said: string) => `Your turn${who && ` (${who})`}: ${said}`,
            ended: (title: string) => `“${title}”: played to the end.`,
            stopped: (title: string, why: string | null) => `“${title}”: ${why ?? 'stopped.'}`,
            failed: (title: string, why: string | null) => `“${title}”: ${why ?? 'failed.'}`
        },
        notStarted: 'The robot did not start the scene: is it switched on and connected?',
        titleField: 'Title',
        save: 'Save',
        more: 'More',
        restoreOriginal: 'Restore the original',
        deleteScene: 'Delete the scene',
        discardAsk: 'Discard what is not saved?',
        cancel: 'Cancel',
        discard: 'Discard',
        restoreAsk: (title: string) => `Restore “${title}” as it shipped?`,
        deleteAsk: (title: string) => `Delete “${title}”?`,
        restore: 'Restore',
        delete: 'Delete',
        apiCost: 'What the writing would cost at API rates: the Claude subscription covers it.',
        atApiRates: (cost: string) => `${cost} at API rates`,
        claudeWrites: (chars: number) => `Claude is writing the play (${count(chars, 'character', 'characters')})`,
        claudeThinks: (tokens: number) =>
            `Claude is thinking${tokens ? ` (~${count(tokens, 'token', 'tokens')})` : ''}`,
        claudePrepares: 'Claude is getting ready',
        writeStep: {
            title: 'Writing',
            waiting: 'Waiting',
            second: 'Second writing: ',
            done: (lines: number, people: number) =>
                `${count(lines, 'line', 'lines')}, ${count(people, 'character', 'characters')}`,
            cancelled: 'Cancelled',
            failed: 'No play that can be played'
        },
        stageStep: {
            title: 'Staging',
            waiting: 'After the writing',
            running: 'Claude is choosing the emotions',
            done: (moves: number) => (moves ? count(moves, 'move added', 'moves added') : 'No move added'),
            cancelled: 'Cancelled',
            failed: 'Without emotions'
        },
        voiceStep: {
            title: 'Voices',
            waiting: 'After the writing',
            running: (voices: string) => `${voices} lines rendered`,
            done: (total: number) => count(total, 'voice ready', 'voices ready'),
            cancelled: (voices: string) => `Cancelled at ${voices}`,
            failed: (voices: string) => `Interrupted at ${voices}`
        },
        row: {
            ready: 'Ready',
            failed: 'Failed',
            cancelled: 'Cancelled',
            writing: 'Writing',
            voices: (done: number, total: number) => `Voices ${done}/${total}`,
            staging: 'Staging'
        },
        play: 'The play',
        readyIn: (time: string, voiced: boolean) =>
            voiced ? `Ready in ${time}: Reachy announces it, then plays it.` : `Ready in ${time}.`,
        stoppedKept: 'Stopped: the scene written is kept, as it was.',
        cancelledBefore: 'Cancelled before it was written.',
        asked: (voiced: boolean) => (voiced ? 'SCENE ASKED FOR BY VOICE' : 'SCENE ASKED FOR'),
        aNewScene: 'A new scene',
        quoted: (text: string) => `“${text}”`,
        steps: 'Steps',
        refusedOnce: (problems: string[]) => `Refused a first time: ${problems.join('; ')}`,
        playOnRobot: 'Play on the robot',
        openScene: 'Open the scene',
        askAgain: 'Ask again',
        cast: 'Cast',
        noPlay: 'No play was written.',
        thinkingFirst: 'Claude is thinking before it writes: the play will be written here.',
        writtenHere: 'The play will be written here.',
        text: 'The scene’s text',
        playFrom: (line: number) => `Play from line ${line}`,
        insert: 'Insert',
        insertHint:
            'at the caret, or with a right click in the text: an emotion, a gesture, a pause, a sound or a voice. Click a line number: the robot plays from there, even in the middle of a scene.',
        issues: 'Errors',
        issue: (line: number, message: string) => `line ${line}: ${message}`,
        voicesComing: 'The voices are being prepared: the scene will play as soon as they are ready.',
        rehearsal: (who: string) => `Rehearsal: ${who}`,
        noMoves: 'No moves',
        stop: 'Stop',
        voicesCancel: 'Voices in preparation, cancel',
        playOptions: 'Play options',
        rehearse: 'Rehearse a part (you say its lines)',
        nobody: 'Nobody: the robot plays everything',
        noExpressive: 'Without expressive moves',
        rehearseHint: (who: string) => `On ${who}’s lines the robot keeps quiet: touch an antenna to move on.`,
        textChanged: 'The text changed while Claude was reading: run it again.',
        nothingToAdd: 'Claude found nothing to add.',
        reading: 'Claude is reading the scene…',
        addEmotions: 'Add emotions',
        added: (n: number) => `${n} emotion${n > 1 ? 's' : ''} added, voices unchanged`,
        undo: 'Undo',
        groups: {
            emotions: 'Emotions',
            gestures: 'Gestures',
            turns: 'Turn',
            pauses: 'Silences',
            sounds: 'Sounds',
            voices: 'Voices'
        },
        notes: {
            turns: '+ to the left, - to the right. An angle per character: @CREON (Gacrux, -60): … — the robot turns to it whenever it speaks.',
            voices: 'In the cast: @NAME (Voice): how it plays.'
        },
        search: 'Search: anger, yes, laugh, Kore…',
        nothing: (query: string) => `Nothing matches “${query}”.`
    }
);
