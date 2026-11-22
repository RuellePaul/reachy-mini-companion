// The form holds only the user's edits: an untouched field follows the server's broadcasts, an edited one
// keeps its edit, and a save sends the fields that still differ from what the Mac holds now.
import type {Settings} from '../api/schemas';

/** Equal by value: a field may be an object (the tuning), rebuilt at every edit. */
export const same = (a: unknown, b: unknown): boolean => {
    if (a === b) return true;
    if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
    const [x, y] = [a as Record<string, unknown>, b as Record<string, unknown>];
    return Object.keys({...x, ...y}).every((key) => same(x[key], y[key]));
};

export const draftOf = (server: Settings, edits: Partial<Settings>): Settings => ({...server, ...edits});

export const changedFields = (server: Settings, draft: Settings): Partial<Settings> =>
    Object.fromEntries(
        Object.entries(draft).filter(([key, value]) => !same(value, server[key as keyof Settings]))
    ) as Partial<Settings>;

/** The edits made since `before` (the edits a save started from): typed during the save, so not saved by it. */
export const editsSince = (before: Partial<Settings>, current: Partial<Settings>): Partial<Settings> =>
    Object.fromEntries(
        Object.entries(current).filter(([key, value]) => !same(value, before[key as keyof Settings]))
    ) as Partial<Settings>;
