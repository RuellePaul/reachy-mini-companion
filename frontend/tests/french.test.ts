// The dashboard's French keeps its words together: a no-break space inside « » and before : ; ! ?, so a narrow screen
// never starts a line with « » », « : » or « ? ». Every string, template and JSX text of src/ is read as the compiler
// reads it; a scene's own text (the play-script a new scene starts with) is the human's to write, not ours.
import {readdirSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import ts from 'typescript';
import {describe, expect, it} from 'vitest';

const SRC = new URL('../src/', import.meta.url).pathname;
const SCRIPTS = new Set(['NEW_SCENE']);
const BROKEN = /« | »| [:;!?](?=[\s»"'`{$)]|$)/;

const files = (dir: string): string[] =>
    readdirSync(dir, {withFileTypes: true}).flatMap((entry) =>
        entry.isDirectory() ? files(join(dir, entry.name)) : /\.tsx?$/.test(entry.name) ? [join(dir, entry.name)] : []
    );

/** Each piece of text of a source that breaks a French word from its sign, with its line. */
const broken = (name: string, text: string): string[] => {
    const kind = name.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const source = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, kind);
    const found: string[] = [];
    const visit = (node: ts.Node) => {
        if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && SCRIPTS.has(node.name.text)) return;
        const piece =
            ts.isStringLiteral(node) ||
            ts.isNoSubstitutionTemplateLiteral(node) ||
            ts.isTemplateHead(node) ||
            ts.isTemplateMiddle(node) ||
            ts.isTemplateTail(node)
                ? text.slice(node.getStart(), node.getEnd())
                : ts.isJsxText(node)
                  ? text.slice(node.pos, node.end) // its leading spaces included: « {title} » starts with one
                  : null;
        if (piece !== null && BROKEN.test(piece)) {
            const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1;
            found.push(`${name}:${line} ${piece.trim()}`);
        }
        ts.forEachChild(node, visit);
    };
    visit(source);
    return found;
};

describe('the French of the dashboard', () => {
    it('keeps « » with their words, and a sign with the word before it', () => {
        const all = files(SRC).flatMap((file) => broken(file.slice(SRC.length), readFileSync(file, 'utf8')));
        expect(all).toEqual([]);
    });

    it('finds a plain space in a string, a template and JSX text, and nowhere in a scene’s own text', () => {
        const sample = [
            "const a = 'Reachy dort : réveille-le.';",
            'const b = `Reachy joue « ${title} ».`;',
            'const c = <p>Supprimer « {title} » ?</p>;',
            "const NEW_SCENE = '@NARRATEUR (Charon) : conteur';",
            'const d = `Reachy joue « ${title} » : ligne ${n}.`;'
        ].join('\n');
        expect(broken('sample.tsx', sample).map((found) => found.split(' ')[0])).toEqual([
            'sample.tsx:1',
            'sample.tsx:2', // before the title
            'sample.tsx:2', // after it
            'sample.tsx:3',
            'sample.tsx:3'
        ]);
    });
});
