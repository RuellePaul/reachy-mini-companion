// @vitest-environment happy-dom
// The dashboard's two languages: the choice (the browser's own, kept in its storage), the texts (each subject's
// `words.ts`, one shape for both), the Mac's labels in English (`mac.ts`), and a guard: no text the dashboard writes
// itself is left outside a `words.ts`, where the other language would never reach it.
import {readdirSync, readFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import ts from 'typescript';
import {describe, expect, it, vi} from 'vitest';
import {formatClock, formatCount, formatDuration, formatPercent, formatUsd} from '../src/format';
import {describe as robotStatus} from '../src/header/status';
import {lang, LANGS, setLang, words, wordsNow} from '../src/i18n';
import {mac, macCatalog} from '../src/mac';
import english from '../src/mac.en.json';
import {PERSONALITIES} from '../src/settings/personalities';

const SRC = `${resolve(__dirname, '../src')}/`;
// Where a text may be written: the dictionaries, and the characters (their prompt is French: the robot's language).
const WRITTEN = /(^|\/)words\.ts$|^settings\/personalities\.ts$|^i18n\.ts$/;
const NOT_SHOWN = new Set(['NBSP', 'CANCELLED']); // a sign every figure keeps; a text of the Mac's, matched
const SHOWN = new Set(['aria-label', 'label', 'title', 'placeholder', 'alt', 'helperText', 'action', 'hint']);
const FRENCH = /[àâçéèêëîïôùûüÀÂÇÉÈÊ«» ]|\p{L}’\p{L}/u;

const files = (dir: string): string[] =>
    readdirSync(dir, {withFileTypes: true}).flatMap((entry) =>
        entry.isDirectory() ? files(join(dir, entry.name)) : /\.tsx?$/.test(entry.name) ? [join(dir, entry.name)] : []
    );

/** Each text a source writes for someone to read, with its line: what JSX shows as it is, what a shown attribute is
 * given as a plain string, and any string that is French by its letters or its signs. */
const written = (name: string, text: string): string[] => {
    const kind = name.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const source = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, kind);
    const found: string[] = [];
    const add = (node: ts.Node, piece: string) =>
        found.push(`${name}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1} ${piece.trim()}`);
    const visit = (node: ts.Node) => {
        if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && NOT_SHOWN.has(node.name.text)) return;
        if (ts.isJsxText(node)) {
            if (/\p{L}{2}/u.test(node.text)) add(node, node.text);
        } else if (ts.isJsxAttribute(node) && node.initializer && ts.isStringLiteral(node.initializer)) {
            if (SHOWN.has(node.name.getText()) && /\p{L}{2}/u.test(node.initializer.text)) add(node, node.getText());
        } else if (
            ts.isStringLiteral(node) ||
            ts.isNoSubstitutionTemplateLiteral(node) ||
            ts.isTemplateHead(node) ||
            ts.isTemplateMiddle(node) ||
            ts.isTemplateTail(node)
        ) {
            if (FRENCH.test(node.text)) add(node, node.text);
        }
        ts.forEachChild(node, visit);
    };
    visit(source);
    return found;
};

describe('the language', () => {
    it('is the one picked, kept by the browser and told to the page', () => {
        expect(LANGS.map(({value}) => value)).toEqual(['fr', 'en']);
        setLang('en');
        expect(lang()).toBe('en');
        expect(localStorage.getItem('reachy.lang')).toBe('en');
        expect(document.documentElement.lang).toBe('en');
        setLang('fr');
        expect(localStorage.getItem('reachy.lang')).toBe('fr');
        expect(document.documentElement.lang).toBe('fr');
    });

    it('starts in the browser’s language until one is picked', async () => {
        const start = async (browser: string, kept?: string) => {
            localStorage.clear();
            if (kept) localStorage.setItem('reachy.lang', kept);
            vi.stubGlobal('navigator', {language: browser});
            vi.resetModules();
            return (await import('../src/i18n')).lang();
        };
        expect([await start('fr-CA'), await start('de-DE'), await start('en-US', 'fr')]).toEqual(['fr', 'en', 'fr']);
        vi.unstubAllGlobals();
    });

    it('gives a subject’s texts in the language shown, read when they are said', () => {
        const both = words(
            {hello: 'Bonjour', to: (name: string) => `À ${name}`},
            {hello: 'Hello', to: (name) => `To ${name}`}
        );
        expect([wordsNow(both).hello, wordsNow(both).to('Reachy')]).toEqual(['Bonjour', 'À Reachy']);
        setLang('en');
        expect([wordsNow(both).hello, wordsNow(both).to('Reachy')]).toEqual(['Hello', 'To Reachy']);
    });

    it('says the robot’s state and writes its figures in English', () => {
        const asleep = {awake: false, phase: null, scene: null, hushed: false, antenna_wake: true} as never;
        const french = robotStatus('up', 'live', asleep).label;
        setLang('en');
        expect([french, robotStatus('up', 'live', asleep).label]).toEqual(['En veille', 'Asleep']);
        expect([formatCount(10_400), formatDuration(1.25), formatDuration(125), formatClock(12.4)]).toEqual([
            '10.4k',
            '1.3 s',
            '2 min 05 s',
            '12.4 s'
        ]);
        expect([formatUsd(0.47), formatUsd(13), formatPercent(70)]).toEqual(['$0.47', '$13', '70%']);
    });
});

describe('the Mac’s labels', () => {
    it('are shown in English from their French, and left as sent when unknown or in French', () => {
        expect(mac('Réveiller le robot')).toBe('Réveiller le robot');
        expect(mac('Réveiller le robot', 'en')).toBe('Wake the robot');
        expect(mac('Mon app à moi', 'en')).toBe('Mon app à moi');
        setLang('en');
        expect(mac('Posé (repos)')).toBe('Composed (idle)');
    });

    it('translates the catalogue’s labels and descriptions, never what is said to the robot', () => {
        const entry = {
            id: 'session:sleep',
            group: 'session',
            label: 'Mettre en veille',
            description: 'Il termine la conversation et se rendort.',
            say: 'dors',
            tool: 'goto_sleep',
            action: {kind: 'session', command: 'sleep'},
            preview: false
        };
        const catalog = {groups: [{key: 'session', label: 'Conversation'}], entries: [entry]} as never;
        const shown = macCatalog(catalog, 'en');
        expect(shown.entries[0]).toMatchObject({
            label: 'Put to sleep',
            description: 'It ends the conversation and goes back to sleep.',
            say: 'dors'
        });
        expect(macCatalog(catalog, 'fr')).toEqual(catalog);
    });

    it('have an English each, with the signs English keeps', () => {
        for (const [french, text] of Object.entries(english)) {
            expect(text, french).not.toBe('');
            expect(text, french).not.toMatch(/[«» ]| [:;!?](\s|$)/);
        }
    });
});

describe('the dashboard’s own texts', () => {
    it('are all in a words.ts: none is written where the other language cannot reach it', () => {
        const all = files(SRC)
            .map((file) => file.slice(SRC.length))
            .filter((name) => !WRITTEN.test(name))
            .flatMap((name) => written(name, readFileSync(join(SRC, name), 'utf8')));
        expect(all).toEqual([]);
    });

    it('finds a text in JSX, in a shown attribute and in a French string', () => {
        const sample = [
            'const a = <p>Annuler</p>;',
            'const b = <input aria-label="Titre" type="text" />;',
            "const c = 'Reachy dort : réveille-le.';",
            "const d = <Box sx={{color: 'transparent'}}>{t.cancel}</Box>;",
            'const e = `${n} s`;'
        ].join('\n');
        expect(written('sample.tsx', sample).map((found) => found.split(' ')[0])).toEqual([
            'sample.tsx:1',
            'sample.tsx:2',
            'sample.tsx:3'
        ]);
    });

    it('give each character an English name, accent and temper, and keep its French prompt', () => {
        for (const one of PERSONALITIES) {
            expect([one.en.name, one.en.accent, one.en.trait].every(Boolean), one.name).toBe(true);
            expect(one.text).toMatch(/[éèàêç]/);
        }
    });
});
