// The English of what the Mac labels in French: the catalogue's entries, the settings' options, a scene's
// vocabulary. Keyed by the Mac's own label (`mac.en.json`; tests/test_cross.py fails on one it lacks); a text it
// does not know (a title someone wrote, an error) stays as the Mac sent it.
import type {Catalog, SceneVocabulary} from './api/schemas';
import {lang, type Lang} from './i18n';
import english from './mac.en.json';

const ENGLISH: Record<string, string> = english;

/** A label of the Mac's, in the language shown: whoever shows it follows the language (`useLang`). */
export const mac = (label: string, to: Lang = lang()): string => (to === 'en' ? (ENGLISH[label] ?? label) : label);

/** The catalogue as the palette shows and searches it. A sentence said to the robot stays French: it hears French. */
export const macCatalog = (catalog: Catalog, to: Lang): Catalog => ({
    groups: catalog.groups.map((group) => ({...group, label: mac(group.label, to)})),
    entries: catalog.entries.map((entry) => ({
        ...entry,
        label: mac(entry.label, to),
        description: mac(entry.description, to)
    }))
});

/** A scene's vocabulary as the insert menu shows and searches it; the word a pick writes is the grammar's own. */
export const macVocabulary = (vocabulary: SceneVocabulary, to: Lang): SceneVocabulary =>
    Object.fromEntries(
        Object.entries(vocabulary).map(([group, list]) => [
            group,
            list.map((word) => ({...word, label: mac(word.label, to), family: mac(word.family, to)}))
        ])
    ) as SceneVocabulary;
