// The palette's search: fuzzy, accent- and case-blind, over what an entry says and what gets it said.
import type {Catalog, CatalogEntry} from '../api/schemas';

/** « Émotion » and « emotion » are the same word to someone typing fast on a phone. */
export const fold = (text: string): string =>
    text
        .normalize('NFD')
        .replace(/\p{Diacritic}/gu, '')
        .toLowerCase();

/**
 * How well `query` matches `text`, 0 meaning not at all: every query character must appear in order. A run
 * of consecutive characters and a match at the start of a word count more, so « hoch » ranks « hoche la
 * tête » above a word that merely contains h, o, c, h scattered. Between equal matches the shorter text wins (by
 * less than a point): « surpris » is « Surpris » before « Surprise ».
 */
export const score = (query: string, text: string): number => {
    const q = fold(query).replace(/\s+/g, '');
    if (!q) return 1;
    const t = fold(text);
    let total = 0;
    let from = 0;
    let run = 0;
    for (const char of q) {
        const at = t.indexOf(char, from);
        if (at < 0) return 0;
        run = at === from ? run + 1 : 1;
        const wordStart = at === 0 || /[\s'’«“(-]/.test(t[at - 1]);
        total += 1 + 2 * run + (wordStart ? 3 : 0);
        from = at + 1;
    }
    return total - 0.001 * (t.length - q.length);
};

/** Whether every word of the query starts a word of `text`: the description is long, so it is never fuzzy. */
const words = (query: string, text: string): boolean => {
    const mine = fold(text).split(/[^\p{L}\p{N}]+/u);
    return fold(query)
        .split(/\s+/)
        .filter(Boolean)
        .every((word) => mine.some((candidate) => candidate.startsWith(word)));
};

/** An entry's best score, weighted: its name matters most, then the sentence (both fuzzy), then the description
 * (by whole words only, or a long description would match almost any query). */
export const entryScore = (query: string, entry: CatalogEntry): number =>
    Math.max(
        score(query, entry.label) * 3,
        score(query, entry.say ?? '') * 2,
        query.trim() && words(query, entry.description) ? 1 + query.length : 0
    );

export interface Section {
    key: string;
    label: string;
    entries: CatalogEntry[];
}

/**
 * The entries that match, grouped in the catalogue's group order. With a query, each group is sorted by
 * score and the groups by their best entry; without one, everything shows in the catalogue's own order.
 * The group's name finds all its entries, after those that match by themselves.
 */
export const sections = (catalog: Catalog, query: string): Section[] => {
    const searching = query.trim() !== '';
    const groups = catalog.groups.map((group) => {
        const named = searching && words(query, group.label) ? 1 : 0;
        const scored = catalog.entries
            .filter((entry) => entry.group === group.key)
            .map((entry) => ({entry, score: searching ? Math.max(entryScore(query, entry), named) : 1}))
            .filter((item) => item.score > 0);
        if (searching) scored.sort((a, b) => b.score - a.score);
        return {...group, entries: scored.map((item) => item.entry), best: scored[0]?.score ?? 0};
    });
    const found = groups.filter((group) => group.entries.length > 0);
    if (searching) found.sort((a, b) => b.best - a.best);
    return found.map(({key, label, entries}) => ({key, label, entries}));
};

/** The entries in display order: what ↑ and ↓ walk through. */
export const flatten = (list: Section[]): CatalogEntry[] => list.flatMap((section) => section.entries);

/** The next highlighted index for a key, wrapping around; the same index for any other key. */
export const move = (index: number, key: string, count: number): number => {
    if (count === 0) return 0;
    if (key === 'ArrowDown') return (index + 1) % count;
    if (key === 'ArrowUp') return (index - 1 + count) % count;
    if (key === 'Home') return 0;
    if (key === 'End') return count - 1;
    return Math.min(index, count - 1);
};
