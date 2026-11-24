// The dashboard's two languages. Every text it writes itself is in a `words.ts`, once per language and of one shape:
// the compiler refuses a text one of them lacks. What the Mac sends stays as the Mac wrote it, in French, and
// `mac.ts` holds the English of its labels. The choice is the browser's own: kept in its storage, and the browser's
// language until one is picked.
import {useSyncExternalStore} from 'react';

export type Lang = 'fr' | 'en';

export const LANGS: {value: Lang; label: string}[] = [
    {value: 'fr', label: 'Français'},
    {value: 'en', label: 'English'}
];

const KEY = 'reachy.lang';

const initial = (): Lang => {
    try {
        const kept = localStorage.getItem(KEY);
        if (kept === 'fr' || kept === 'en') return kept;
    } catch {
        // no storage here: the browser's language
    }
    return typeof navigator !== 'undefined' && navigator.language?.toLowerCase().startsWith('fr') ? 'fr' : 'en';
};

const listeners = new Set<() => void>();
let current = initial();
if (typeof document !== 'undefined') document.documentElement.lang = current;

/** The language now, for what is written outside a component: whoever shows it calls `useLang` or `useWords`. */
export const lang = (): Lang => current;

export const setLang = (next: Lang) => {
    if (next === current) return;
    current = next;
    try {
        localStorage.setItem(KEY, next);
    } catch {
        // no storage here: the choice lasts as long as the page
    }
    if (typeof document !== 'undefined') document.documentElement.lang = next;
    listeners.forEach((listener) => listener());
};

const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => void listeners.delete(listener);
};

/** The language, in a component: it renders again when the language changes. */
export const useLang = (): Lang => useSyncExternalStore(subscribe, lang);

/** Texts in both languages. */
export interface Words<T> {
    fr: T;
    en: T;
}

/** A subject's texts, French first; the English must have every text of the French, and no other. */
export const words = <T>(fr: T, en: NoInfer<T>): Words<T> => ({fr, en});

/** The texts in the language shown, in a component. */
export const useWords = <T>(both: Words<T>): T => both[useLang()];

/** The texts in the language shown, outside a component. */
export const wordsNow = <T>(both: Words<T>): T => both[current];
