// @vitest-environment happy-dom
// Every ink of the theme reads at WCAG AA (4.5:1, the floor for the 12 to 16 px text it draws) on every surface the
// dashboard paints text on, and on a list row lit by the pointer or the selection.
import {act, createElement as h} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, describe, expect, it} from 'vitest';
import {CssBaseline, ListItemButton, ThemeProvider} from '@mui/material';
import {theme, v, type Token} from '../src/theme';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const INKS: Token[] = ['ink', 'ink-2', 'ink-3'];
const SURFACES: Token[] = ['page', 'plate', 'raised', 'sunken', 'gutter'];
const LIT: Token[] = ['page', 'plate', 'raised']; // where the lists are

/** [r, g, b, alpha] of a computed `#rrggbb` or `rgb[a](…)`. */
const rgba = (css: string): number[] => {
    const hex = /^#([0-9a-f]{6})$/i.exec(css.trim());
    if (hex) return [0, 2, 4].map((i) => parseInt(hex[1].slice(i, i + 2), 16)).concat(1);
    const [r, g, b, a = 1] = (/rgba?\(([^)]+)\)/.exec(css)?.[1] ?? '').split(',').map(Number);
    return [r, g, b, a];
};
const over = (base: number[], top: number[]) => base.slice(0, 3).map((x, i) => x + (top[i] - x) * top[3]);
const luminance = (rgb: number[]) => {
    const [r, g, b] = rgb.map((x) => (x / 255 <= 0.03928 ? x / 255 / 12.92 : ((x / 255 + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a: number[], b: number[]) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
};

describe('contrast', () => {
    let root: Root | null = null;
    afterEach(() => {
        act(() => root?.unmount());
        root = null;
        document.body.innerHTML = '';
    });

    const render = async (...children: ReturnType<typeof h>[]) => {
        const host = document.createElement('div');
        document.body.append(host);
        root = createRoot(host);
        await act(async () => root?.render(h(ThemeProvider, {theme}, h(CssBaseline), ...children)));
    };
    const ink = (token: Token, key: string) => h('span', {key, 'data-ink': token, style: {color: v(token)}}, 'Aa');
    const color = (el: Element, property: 'color' | 'backgroundColor') => rgba(getComputedStyle(el)[property]);

    it('every ink reads at 4.5:1 on every surface', async () => {
        await render(
            ...SURFACES.map((s) =>
                h('div', {key: s, 'data-surface': s, style: {background: v(s)}}, ...INKS.map((i) => ink(i, i)))
            )
        );
        const found = [...document.querySelectorAll<HTMLElement>('[data-ink]')].map((el) => {
            const surface = el.parentElement as HTMLElement;
            const r = ratio(color(el, 'color'), color(surface, 'backgroundColor'));
            return `${el.dataset.ink} on ${surface.dataset.surface}: ${r >= 4.5 ? 'AA' : r.toFixed(2)}`;
        });
        expect(found).toEqual(SURFACES.flatMap((s) => INKS.map((i) => `${i} on ${s}: AA`)));
    });

    it('a selected list row keeps its faintest ink at 4.5:1', async () => {
        await render(
            ...LIT.map((s) =>
                h(
                    'div',
                    {key: s, 'data-surface': s, style: {background: v(s)}},
                    h(ListItemButton, {selected: true}, ink('ink-3', 'ink-3'))
                )
            )
        );
        const found = [...document.querySelectorAll<HTMLElement>('[data-ink]')].map((el) => {
            const row = el.parentElement as HTMLElement;
            const surface = row.parentElement as HTMLElement;
            const lit = over(color(surface, 'backgroundColor'), color(row, 'backgroundColor'));
            const r = ratio(color(el, 'color'), lit);
            return `ink-3 on a row selected on ${surface.dataset.surface}: ${r >= 4.5 ? 'AA' : r.toFixed(2)}`;
        });
        expect(found).toEqual(LIT.map((s) => `ink-3 on a row selected on ${s}: AA`));
    });
});
