// Every figure the dashboard shows is written here, in one short format, French style: « 950 », « 10,4k », « 1,2M »,
// « 1,3 s », « 42 s », « 2 min 05 s », « 3 min », « 0,47 $ ». A decimal comma, a true minus, and a no-break space
// before a unit, so a line never ends between a number and what it measures. In English, the same with a decimal
// point, and the dollar and the percent sign where English puts them: « 10.4k », « 1.3 s », « $0.47 », « 70% ».
import {lang} from './i18n';

const NBSP = ' ';

const decimal = (value: number, options: Intl.NumberFormatOptions): string =>
    value.toLocaleString(lang() === 'fr' ? 'fr-FR' : 'en-US', {signDisplay: 'negative', ...options}).replace('-', '−');

/** A count, in short: « 950 », « 10,4k », « 1,2M ». */
export const formatCount = (n: number): string => {
    const size = Math.abs(n);
    if (size < 999.5) return decimal(n, {maximumFractionDigits: 0});
    if (size < 999_950) return `${decimal(n / 1e3, {maximumFractionDigits: 1})}k`;
    return `${decimal(n / 1e6, {maximumFractionDigits: 1})}M`;
};

/** A duration: to the tenth below 10 s (« 1,3 s », « 4 s »), « 42 s », then « 3 min » or « 2 min 05 s ». */
export const formatDuration = (seconds: number): string => {
    if (seconds < 0) return `−${formatDuration(-seconds)}`;
    if (seconds < 9.95) return `${decimal(seconds, {maximumFractionDigits: 1})}${NBSP}s`;
    const whole = Math.round(seconds);
    if (whole < 60) return `${whole}${NBSP}s`;
    const minutes = `${Math.floor(whole / 60)}${NBSP}min`;
    return whole % 60 ? `${minutes}${NBSP}${String(whole % 60).padStart(2, '0')}${NBSP}s` : minutes;
};

/** A cost in dollars, two significant digits, whole dollars from 10 $: « 0,012 $ », « 0,47 $ », « 13 $ ». */
export const formatUsd = (usd: number): string => {
    const figure = decimal(usd, Math.abs(usd) < 10 ? {maximumSignificantDigits: 2} : {maximumFractionDigits: 0});
    return lang() === 'fr' ? `${figure}${NBSP}$` : `$${figure}`;
};

/** A figure that ticks or lines up in place, with all its `digits`: a factor « ×1,00 ». */
export const formatFixed = (value: number, digits: number): string =>
    decimal(value, {minimumFractionDigits: digits, maximumFractionDigits: digits});

/** A playhead's time, to the tenth whatever its length: « 0,0 s », « 12,4 s ». */
export const formatClock = (seconds: number): string => `${formatFixed(seconds, 1)}${NBSP}s`;

/** A share out of 100, whole: « 70 % ». */
export const formatPercent = (value: number): string =>
    `${decimal(value, {maximumFractionDigits: 0})}${lang() === 'fr' ? NBSP : ''}%`;

/** An angle, whole degrees: « −33° ». */
export const formatDegrees = (value: number): string => `${decimal(value, {maximumFractionDigits: 0})}°`;
