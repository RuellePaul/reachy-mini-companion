// @vitest-environment happy-dom
// A creation's preview (src/apps/Build.tsx): the page so far, fetched again as it grows and once more when writing
// stops, shown in a frame that runs no script, against a mocked `app_build` channel and draft route; the channel
// carries every build in flight (two at most), each panel only its own.
import {act, createElement} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import type {AppBuild} from '../src/api/schemas';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const mocks = vi.hoisted(() => ({
    progress: (() => {}) as (build: AppBuild) => void,
    drafts: 0 // how many times the draft was fetched
}));
vi.mock('../src/live', () => ({
    useLive: () => ({
        on: (_channel: string, listener: (build: AppBuild) => void) => {
            mocks.progress = listener;
            return () => {};
        }
    })
}));
vi.mock('../src/apps/api', () => ({
    appsApi: {
        draft: async () => `<p>brouillon ${++mocks.drafts}</p><script>parent.postMessage(1, '*')</script>`,
        cancel: async () => {}
    }
}));

import {Build} from '../src/apps/Build';

const build = (over: Partial<AppBuild>): AppBuild => ({
    app_id: 'a1',
    kind: 'create',
    phase: 'writing',
    round: 1,
    started_at: 0,
    thinking_tokens: 0,
    chars: 0,
    tail: '',
    edits: [],
    problems: [],
    cost_usd: null,
    ...over
});

let host: HTMLDivElement;
let root: Root;
beforeEach(async () => {
    vi.useFakeTimers({toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval']});
    vi.setSystemTime(100_000);
    mocks.drafts = 0;
    host = document.createElement('div');
    root = createRoot(host);
    await act(async () => root.render(createElement(Build, {id: 'a1'})));
});
afterEach(async () => {
    await act(async () => root.unmount());
    vi.useRealTimers();
});

const comes = (over: Partial<AppBuild>) => act(async () => mocks.progress(build(over)));
const later = (ms: number) => act(async () => void vi.advanceTimersByTime(ms));
const preview = () => host.querySelector<HTMLIFrameElement>('iframe[title="Aperçu de la page en cours"]');

it('shows the page so far without its scripts, fetched again only once it has grown', async () => {
    await comes({chars: 1000});
    expect(mocks.drafts).toBe(0);
    expect(preview()).toBeNull();
    await comes({chars: 2000});
    expect(mocks.drafts).toBe(1);
    expect(preview()?.getAttribute('sandbox')).toBe(''); // no script, no form, no navigation
    expect(preview()?.getAttribute('srcdoc')).toContain('brouillon 1');
    await later(1000);
    await comes({chars: 3600}); // grown enough, but too soon
    expect(mocks.drafts).toBe(1);
    await later(2000);
    await comes({chars: 3700});
    expect(mocks.drafts).toBe(2);
    expect(preview()?.getAttribute('srcdoc')).toContain('brouillon 2');
});

it('fetches the whole page once writing stops, then no more', async () => {
    await comes({chars: 2000});
    await later(1000);
    await comes({chars: 3000}); // the last 1000 characters, too few and too soon to be fetched while written
    expect(mocks.drafts).toBe(1);
    await comes({chars: 3000, phase: 'checking'});
    expect(mocks.drafts).toBe(2);
    expect(preview()?.getAttribute('srcdoc')).toContain('brouillon 2');
    for (let s = 0; s < 30; s++) {
        await later(1000);
        await comes({chars: 3000, phase: 'checking'});
    }
    expect(mocks.drafts).toBe(2);
});

it('shows a page shorter than the growth step once it is written', async () => {
    await comes({chars: 800});
    expect(preview()).toBeNull();
    await comes({chars: 800, phase: 'checking'});
    expect(preview()?.getAttribute('srcdoc')).toContain('brouillon 1');
});

it('takes nothing from the progress of another app built at the same time', async () => {
    await comes({phase: 'thinking', thinking_tokens: 500, cost_usd: 0.01});
    const shown = host.textContent;
    expect(shown).toContain('Claude réfléchit');
    await comes({app_id: 'b2', phase: 'writing', chars: 5000, tail: 'CODE DE B', started_at: 50, cost_usd: 3});
    expect(host.textContent).toBe(shown); // neither its phase, its code, its time nor its cost
    expect(mocks.drafts).toBe(0); // nor this app's draft fetched on the other's count
});
