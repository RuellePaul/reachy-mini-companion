// Every figure the dashboard shows comes from src/format.ts, in one short format: « 10,4k », « 1,2M », « 1,3 s »,
// « 3 min », « 0,47 $ ». No other source formats a number or glues a unit to one.
import {readdirSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {describe, expect, it} from 'vitest';
import {formatClock, formatCount, formatDuration, formatFixed, formatUsd} from '../src/format';

const SRC = new URL('../src/', import.meta.url).pathname;
const files = (dir: string): string[] =>
    readdirSync(dir, {withFileTypes: true}).flatMap((entry) =>
        entry.isDirectory() ? files(join(dir, entry.name)) : /\.tsx?$/.test(entry.name) ? [join(dir, entry.name)] : []
    );
// A number formatted in place, or a unit written after an interpolated value: « ${ms} ms », « {cost} $ », « ${a}° ».
const OWN_FORMAT =
    /toLocaleString\(|NumberFormat\(|\}(?:(?: |\\u00a0)(?:ms|s|min|minutes?|Ko|%|\$(?!\{))|°)(?![\p{L}\d_’'])/u;

describe('figures', () => {
    it('counts in short: units, then thousands and millions to the tenth', () => {
        expect([0, 950, 999.4, 999.6, 2500, 10_400, 10_000, 999_949, 999_950, 1_234_567].map(formatCount)).toEqual([
            '0',
            '950',
            '999',
            '1k',
            '2,5k',
            '10,4k',
            '10k',
            '999,9k',
            '1M',
            '1,2M'
        ]);
    });

    it('says a duration to the tenth below 10 s, in seconds, then in minutes', () => {
        expect([0, 0.48, 1.25, 4, 9.96, 42.7, 60, 125, 180, 3600].map(formatDuration)).toEqual([
            '0 s',
            '0,5 s',
            '1,3 s',
            '4 s',
            '10 s',
            '43 s',
            '1 min',
            '2 min 05 s',
            '3 min',
            '60 min'
        ]);
        expect(formatDuration(-15)).toBe('−15 s');
    });

    it('gives a cost two significant digits, whole dollars from 10 $', () => {
        expect([0, 0.004, 0.0123, 0.45, 1.339, 1.5, 12.6].map(formatUsd)).toEqual([
            '0 $',
            '0,004 $',
            '0,012 $',
            '0,45 $',
            '1,3 $',
            '1,5 $',
            '13 $'
        ]);
    });

    it('keeps every decimal of a clock or a factor, which tick in place', () => {
        expect([formatClock(0), formatClock(12.44), formatFixed(1, 2), formatFixed(-0.5, 2)]).toEqual([
            '0,0 s',
            '12,4 s',
            '1,00',
            '−0,50'
        ]);
    });

    it('is the only place a number is formatted', () => {
        const own = files(SRC)
            .filter((file) => !file.endsWith('/format.ts'))
            .flatMap((file) =>
                readFileSync(file, 'utf8')
                    .split('\n')
                    .flatMap((line, n) => (OWN_FORMAT.test(line) ? [`${file.slice(SRC.length)}:${n + 1}`] : []))
            );
        expect(own).toEqual([]);
    });

    it('tells a unit glued in place from text that only follows a value', () => {
        const lines = [
            'value={`${session.last_latency_ms} ms`}',
            "`${minutes} minute${minutes > 1 ? 's' : ''}`",
            '` · ≈\\u00a0${cost}\\u00a0$`',
            "const a = `${t.toLocaleString('fr-FR')}`;",
            '`Ta conversation avec ${name} s’écrira ici.`',
            '`${word.label} ${word.word}`',
            'width: `${total ? (100 * done) / total : 0}%`'
        ];
        expect(lines.map((line) => OWN_FORMAT.test(line))).toEqual([true, true, true, true, false, false, false]);
    });
});
