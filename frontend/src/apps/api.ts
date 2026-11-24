// The apps' routes on the Mac (docs/CONTRACTS.md), every answer parsed with its Zod schema.
import {read, send} from '../api/client';
import {appHtmlSchema, appListSchema, appSummarySchema} from '../api/schemas';
import type {ManetteProfile} from '../api/schemas';
import type {Motion} from './bridge';

const path = (id: string, rest = '') => `/api/apps/${encodeURIComponent(id)}${rest}`;

export const appsApi = {
    list: async () => (await read(appListSchema, 'GET', '/api/apps')).apps,
    create: (request: string) => read(appSummarySchema, 'POST', '/api/apps', {request}),
    update: (id: string, request: string) => read(appSummarySchema, 'POST', path(id, '/update'), {request}),
    rename: (id: string, title: string) => read(appSummarySchema, 'PATCH', path(id), {title}),
    setQuiet: (id: string, quiet: boolean) => read(appSummarySchema, 'POST', path(id, '/quiet'), {quiet}),
    remove: async (id: string) => void (await send('DELETE', path(id))),
    html: async (id: string) => (await read(appHtmlSchema, 'GET', path(id, '/html'))).html,
    draft: async (id: string) => (await read(appHtmlSchema, 'GET', path(id, '/draft'))).html,
    cancel: async (id: string) => void (await send('POST', path(id, '/cancel'))),
    bridge: async (id: string, motion: Motion, dashboard: string) =>
        void (await send('POST', path(id, '/bridge'), {v: 1, motion, dashboard})),
    manette: async (id: string, profile: ManetteProfile | null, grant = false) =>
        void (await send('POST', path(id, '/manette'), {profile, grant}))
};
