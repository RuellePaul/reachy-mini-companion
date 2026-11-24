// The Mac's HTTP API. The Vite proxy adds the API key; every JSON response is parsed with its Zod schema.
import type {z, ZodTypeAny} from 'zod';
import {wordsNow} from '../i18n';
import {FRAME} from '../words';
import {
    catalogSchema,
    dashboardStateSchema,
    settingsViewSchema,
    volumeSchema,
    type SceneOptions,
    type Settings,
    type TryMove
} from './schemas';

/** A failed call. `message` is the Mac's own `detail` when it sent one, in French; else ours, in the language shown. */
export class ApiError extends Error {
    constructor(
        readonly status: number,
        message: string
    ) {
        super(message);
    }
}

export const errorText = (err: unknown): string => (err instanceof ApiError ? err.message : wordsNow(FRAME).unexpected);

// FastAPI errors are `{detail: string}`, or a list of violations (422) whose `msg` the Mac writes in French.
const detailOf = (body: unknown): string | null => {
    const detail = (body as {detail?: unknown} | null)?.detail;
    if (typeof detail === 'string') return detail;
    if (!Array.isArray(detail)) return null;
    const messages = detail.map((v: {msg?: unknown}) => v?.msg).filter((m): m is string => typeof m === 'string');
    return messages.length ? wordsNow(FRAME).joined(messages) : null;
};

/** One call; a failure is an ApiError with its message. Subjects build their own API modules on it. */
export async function send(method: string, path: string, body?: unknown): Promise<Response> {
    let res: Response;
    try {
        res = await fetch(path, {
            method,
            headers: body === undefined ? undefined : {'Content-Type': 'application/json'},
            body: body === undefined ? undefined : JSON.stringify(body)
        });
    } catch {
        throw new ApiError(0, wordsNow(FRAME).macSilent);
    }
    if (!res.ok) {
        const detail = detailOf(await res.json().catch(() => null));
        throw new ApiError(res.status, detail ?? wordsNow(FRAME).macRefused(res.status));
    }
    return res;
}

/** One call whose JSON answer is parsed with `schema`. */
export async function read<S extends ZodTypeAny>(schema: S, method: string, path: string, body?: unknown) {
    const res = await send(method, path, body);
    const parsed = schema.safeParse(await res.json().catch(() => undefined));
    if (!parsed.success) {
        console.warn(`[api] ${method} ${path}: unexpected response`, parsed.error.issues);
        throw new ApiError(res.status, wordsNow(FRAME).macUnread);
    }
    return parsed.data as z.infer<S>;
}

export const api = {
    state: () => read(dashboardStateSchema, 'GET', '/api/state'),
    settings: () => read(settingsViewSchema, 'GET', '/api/settings'),
    saveSettings: (changes: Partial<Settings>) => read(settingsViewSchema, 'PATCH', '/api/settings', changes),
    setVolume: (volume: number) => read(volumeSchema, 'PUT', '/api/volume', {volume}),
    session: async (action: 'wake' | 'sleep' | 'restart') => void (await send('POST', `/api/session/${action}`)),
    say: async (text: string) => void (await send('POST', '/api/session/say', {text})),
    catalog: () => read(catalogSchema, 'GET', '/api/catalog'),
    tryMove: async (move: TryMove) => void (await send('POST', '/api/studio/try', move)),
    playScene: async (id: string, options?: SceneOptions) =>
        void (await send('POST', `/api/scenes/${encodeURIComponent(id)}/play`, options)),
    stopScene: async () => void (await send('POST', '/api/scenes/stop'))
};
