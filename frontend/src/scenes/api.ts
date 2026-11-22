// The scene routes of docs/CONTRACTS.md; every answer is parsed with its Zod schema. Play and stop are in api/client.
import {read, send} from '../api/client';
import {
    sceneBuildSchema,
    sceneBuildsSchema,
    sceneCheckSchema,
    sceneEmotionsSchema,
    sceneListSchema,
    sceneOutSchema,
    sceneVocabularySchema
} from '../api/schemas';
import {wordsNow} from '../i18n';
import {SCENES} from './words';

const path = (id: string) => `/api/scenes/${encodeURIComponent(id)}`;

export const scenesApi = {
    list: () => read(sceneListSchema, 'GET', '/api/scenes'),
    get: (id: string) => read(sceneOutSchema, 'GET', path(id)),
    save: (id: string | null, title: string, text: string) =>
        read(sceneOutSchema, id ? 'PUT' : 'POST', id ? path(id) : '/api/scenes', {title, text}),
    remove: async (id: string) => void (await send('DELETE', path(id))),
    check: (text: string) => read(sceneCheckSchema, 'POST', '/api/scenes/check', {title: 'brouillon', text}),
    vocabulary: () => read(sceneVocabularySchema, 'GET', '/api/scenes/vocabulary'),
    emotions: (title: string, text: string) =>
        read(sceneEmotionsSchema, 'POST', '/api/scenes/emotions', {title: title || wordsNow(SCENES).untitled, text}),
    /** A scene written at this ask: its build, followed on `scene_build`. */
    write: (request: string) => read(sceneBuildSchema, 'POST', '/api/scenes/write', {request}),
    builds: () => read(sceneBuildsSchema, 'GET', '/api/scenes/builds'),
    cancel: async (build: string) => void (await send('POST', `/api/scenes/builds/${encodeURIComponent(build)}/cancel`))
};
