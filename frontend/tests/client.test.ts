// The API client's failures, each an ApiError with a French message: a Mac that does not answer, a refusal with
// its `detail` (a sentence, or a 422's list of violations) or without one, and an answer off contract.
import {afterEach, describe, expect, it, vi} from 'vitest';
import {api, ApiError, errorText} from '../src/api/client';

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

/** The Mac's answer to every request, or the failure of the request itself. */
const mac = (answer: () => Response) =>
    vi.stubGlobal(
        'fetch',
        vi.fn(async () => answer())
    );
const failure = async (call: Promise<unknown>): Promise<ApiError> => {
    const err: unknown = await call.then(
        () => null,
        (thrown: unknown) => thrown
    );
    if (!(err instanceof ApiError)) throw new Error(`not an ApiError: ${String(err)}`);
    return err;
};
/** Any space as a plain one: which French space separates the violations is not what these tests are about. */
const plain = (text: string) => text.replace(/\s/g, ' ');

describe('a failed call', () => {
    it('says the Mac does not answer when the request never reaches it', async () => {
        mac(() => {
            throw new TypeError('Failed to fetch');
        });
        const err = await failure(api.say('bonjour'));
        expect([err.status, err.message]).toEqual([0, 'Le Mac ne répond pas.']);
        expect(errorText(err)).toBe('Le Mac ne répond pas.');
    });

    it("gives the Mac's own sentence, or a 422's French violations one after the other", async () => {
        mac(() => new Response(JSON.stringify({detail: 'Le robot dort.'}), {status: 409}));
        expect((await failure(api.say('x'))).message).toBe('Le robot dort.');
        const violations = [{msg: 'Le nom est trop long.'}, {loc: ['body']}, {msg: 'Le texte est vide.'}];
        mac(() => new Response(JSON.stringify({detail: violations}), {status: 422}));
        const err = await failure(api.say('x'));
        expect(err.status).toBe(422);
        expect(plain(err.message)).toBe('Le nom est trop long. ; Le texte est vide.');
    });

    it('says the Mac refused, with its status, when it gave no reason', async () => {
        for (const body of ['<html>Bad Gateway</html>', JSON.stringify({detail: []}), JSON.stringify({})]) {
            mac(() => new Response(body, {status: 502}));
            const err = await failure(api.say('x'));
            expect([err.status, err.message]).toEqual([502, 'Le Mac a refusé la demande (erreur 502).']);
        }
    });

    it('refuses an answer its schema rejects, or one that is not JSON', async () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
        for (const body of ['{}', 'pas du JSON']) {
            mac(() => new Response(body, {status: 200}));
            const err = await failure(api.setVolume(50));
            expect([err.status, err.message]).toEqual([200, 'Le Mac a envoyé une réponse inattendue.']);
        }
        expect(warn).toHaveBeenCalledTimes(2); // the console says which call, and what did not fit
        mac(() => new Response(JSON.stringify({volume: 50}), {status: 200}));
        expect(await api.setVolume(50)).toEqual({volume: 50});
    });

    it('is « Erreur inattendue. » when it is not an ApiError', () => {
        expect(errorText(new TypeError('x'))).toBe('Erreur inattendue.');
    });
});
