// The editor's pure helpers: lines of the text, what the « Insérer » menu writes, each line painted as the parser
// reads it (who speaks, the moves, the sounds), and the marks a check puts on the lines (kept on their lines while the
// text changes under them).
import type {SceneCheck, SceneVocabulary} from '../api/schemas';

/** The [start, end) offsets of line `line` (1-based) in `text`. */
export const lineRange = (text: string, line: number): [number, number] => {
    let start = 0;
    for (let n = 1; n < line; n++) {
        const next = text.indexOf('\n', start);
        if (next < 0) return [text.length, text.length];
        start = next + 1;
    }
    const end = text.indexOf('\n', start);
    return [start, end < 0 ? text.length : end];
};

/** What the menu writes for a word: the body in brackets, a sound in chevrons, a voice in parentheses. */
export const snippet = (group: keyof SceneVocabulary, word: string): string =>
    group === 'sounds' ? `<${word}>` : group === 'voices' ? `(${word})` : `[${word}]`;

/** `text` with `piece` in place of [start, end) and one space beside it at most, the one the parser takes out with
 * it (« joues-tu [sad1]? » is said as before), and the caret after it. */
export const insert = (text: string, start: number, end: number, piece: string) => {
    const before = start > 0 && !/\s/.test(text[start - 1]) ? ' ' : '';
    const after = !before && end < text.length && !/\s/.test(text[end]) ? ' ' : '';
    const written = `${before}${piece}${after}`;
    return {text: text.slice(0, start) + written + text.slice(end), caret: start + written.length};
};

/** A line's marks: its voice (ready or to render, whose, and where its segment starts and ends), an error, a warning. */
export interface Mark {
    voice?: 'ready' | 'missing';
    who?: string | null;
    first?: boolean; // the first line of its segment
    last?: boolean;
    error?: boolean;
    warning?: boolean;
}

/**
 * The marks of `check`, made on `checked`, on the lines of `text`: the lines above and below an edit keep theirs
 * (shifted by the lines it added or removed) until the next check comes; those inside it have none meanwhile.
 */
export const marks = (check: SceneCheck | null, checked: string, text: string): Map<number, Mark> => {
    const old = checked.split('\n');
    const now = text.split('\n');
    let top = 0;
    while (top < old.length && top < now.length && old[top] === now[top]) top++;
    let bottom = 0;
    while (bottom < old.length - top && bottom < now.length - top && old.at(-1 - bottom) === now.at(-1 - bottom))
        bottom++;
    const at = (line: number) => (line <= top ? line : line > old.length - bottom ? line + now.length - old.length : 0);
    const out = new Map<number, Mark>();
    const mark = (line: number, add: Mark) => at(line) && out.set(at(line), {...out.get(at(line)), ...add});
    for (const cue of check?.cues ?? []) {
        if (cue.kind !== 'say') continue;
        const end = cue.end_line ?? cue.line;
        for (let line = cue.line; line <= end; line++) {
            const prior = out.get(at(line));
            const voice = prior?.voice === 'missing' || !cue.ready ? 'missing' : 'ready';
            const edges = {first: !!prior?.first || line === cue.line, last: !!prior?.last || line === end};
            mark(line, {voice, who: cue.character, ...edges});
        }
    }
    for (const warning of check?.warnings ?? []) mark(warning.line, {warning: true});
    for (const error of check?.errors ?? []) mark(error.line, {error: true});
    return out;
};

export const NARRATOR = 'NARRATEUR';
// One sober hue per character, in the order of the cast; the narrator stays neutral. None is the moves' violet nor
// the sounds' amber (theme.ts).
const HUES = [0, 1, 2, 3, 4, 5, 6, 7].map((i) => `var(--r-cast-${i})`);
const NEUTRAL = 'var(--r-cast-n)';
const CAST = /^\s*@\s*([^:()]{1,40}?)\s*(?:\(([^)]*)\))?\s*:(.*)$/;
const ROLE = /^([^\s:()[\]<@][^:()[\]<]{0,38}?)(\s*(?:\([^)]*\))?\s*[:\u2013\u2014]\s*)(.*)$/;
const TOKEN = /(\[[^[\]]*\]|\([^()]*\)|<[^<>]*>)/;

/** Each character's colour, from the cast lines in the order written. */
export const colours = (text: string): Map<string, string> => {
    const out = new Map<string, string>([[NARRATOR, NEUTRAL]]);
    for (const line of text.split('\n')) {
        const name = CAST.exec(line)?.[1].trim().toUpperCase();
        if (name && !out.has(name)) out.set(name, HUES[(out.size - 1) % HUES.length]);
    }
    return out;
};
export const colourOf = (palette: Map<string, string>, who: string | null | undefined) =>
    palette.get(who ?? NARRATOR) ?? NEUTRAL;

/** The cast as written, in order: each character's name and the voice it is given ('' when it names none). */
export const cast = (text: string): {name: string; voice: string}[] => {
    const out: {name: string; voice: string}[] = [];
    for (const line of text.split('\n')) {
        const found = CAST.exec(line);
        const name = found?.[1].trim().toUpperCase();
        if (name && !out.some((member) => member.name === name))
            out.push({name, voice: (found?.[2] ?? '').split(',')[0].trim()});
    }
    return out;
};

/** How a piece of a line shows: words said, a name, what is not said (cast, direction, comment), a move, a
 * silence or a turn, a sound of the voice. */
export type Paint = 'said' | 'name' | 'aside' | 'move' | 'still' | 'sound';
export interface Painted {
    who: string | null; // whose words the line holds (null: none)
    parts: [string, Paint][];
}

const body = (text: string): [string, Paint][] =>
    text
        .split(TOKEN)
        .filter(Boolean)
        .map((part) => [
            part,
            part[0] === '<'
                ? 'sound'
                : part[0] === '('
                  ? 'aside'
                  : part[0] !== '['
                    ? 'said'
                    : /^\[\s*(pause|silence|tourne|face au public)/i.test(part)
                      ? 'still'
                      : 'move'
        ]);

/** Every line as the parser reads it (mac_server/scene_text.py): a blank or a cast line ends a block, a role line
 * opens one (a name cast above it, or in capitals), other lines go on it; a block opened by none is the narrator's.
 * The parts of a line are all its characters, in order: the painted copy is the textarea's text. */
export const paint = (text: string, palette: Map<string, string>): Painted[] => {
    const known = new Set([NARRATOR]);
    let who: string | null = null;
    return text.split('\n').map((line): Painted => {
        const trimmed = line.trim();
        const lead = line.slice(0, line.length - line.trimStart().length);
        if (!trimmed || trimmed[0] === '@') who = null;
        if (!trimmed) return {who: null, parts: [[line, 'said']]};
        if (/^(#|\/\/)/.test(trimmed)) return {who: null, parts: [[line, 'aside']]};
        if (trimmed[0] === '@') {
            const cast = CAST.exec(line);
            if (!cast) return {who: null, parts: [[line, 'aside']]};
            const at = line.indexOf(cast[1]);
            known.add(cast[1].trim().toUpperCase());
            return {
                who: cast[1].trim().toUpperCase(),
                parts: [
                    [line.slice(0, at), 'aside'],
                    [cast[1], 'name'],
                    [line.slice(at + cast[1].length), 'aside']
                ]
            };
        }
        const role = ROLE.exec(line.slice(lead.length));
        const name = role?.[1].trim() ?? '';
        const letters = [...name].filter((c) => c.toLowerCase() !== c.toUpperCase());
        const capitals =
            letters.length >= 2 && letters.every((c) => c === c.toUpperCase()) && name.split(/\s+/).length <= 4;
        if (role && (known.has(name.toUpperCase()) || capitals)) {
            who = palette.has(name.toUpperCase()) ? name.toUpperCase() : NARRATOR; // not cast: the parser says so
            return {who, parts: [[lead + role[1], 'name'], [role[2], 'aside'], ...body(role[3])]};
        }
        who = who ?? NARRATOR;
        return {who, parts: body(line)};
    });
};
