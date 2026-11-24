// The scene's text: a textarea whose letters are drawn by a copy of its lines underneath, painted as the parser reads
// them (each character in its colour, the moves and the sounds apart, what is not said dimmed), with the line numbers,
// each segment's voice and the errors. Both scroll as one and wrap alike, so every mark sits on its line. A right
// click opens « Insérer » where it points, its button at the caret (a touch screen or the keyboard has no right
// click); a click on a line's number plays the scene from there; the errors are listed under the text (a click
// selects the line).
import {useMemo, useRef, useState, type ChangeEvent, type MouseEvent} from 'react';
import {Box, Button, Stack, Typography} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import {keyframes} from '@mui/material/styles';
import type {SceneCheck, SceneVocabulary} from '../api/schemas';
import {useWords} from '../i18n';
import {FONT, MONO, tint, v} from '../theme';
import {Insert} from './Insert';
import {colourOf, colours, insert, lineRange, marks, paint, snippet} from './text';
import {SCENES} from './words';

const LINE_PX = 21;
const GUTTER_PX = 56; // the numbers, then the voice bar
const PAD = '8px';
const WRAP = {m: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', tabSize: 4, letterSpacing: 0} as const;
const breathe = keyframes`50% { opacity: 0.3; }`;
// Colour and background only: a bold or a padding would move the letters off the textarea's caret.
const ROWS = {
    p: `${PAD} ${PAD} calc(${PAD} + ${LINE_PX}px) 0`,
    userSelect: 'none',
    '& > div': {...WRAP, position: 'relative', pl: `${GUTTER_PX}px`, color: v('ink')},
    '& > div > span:first-of-type': {
        position: 'absolute',
        left: 0,
        width: GUTTER_PX - 24,
        textAlign: 'right',
        fontSize: 12,
        color: v('ink-3')
    },
    '& [data-warning] > span:first-of-type': {color: v('attention')},
    '& [data-error]': {bgcolor: tint('fault', 12), '& > span:first-of-type': {color: v('fault')}},
    '& [data-current]': {
        bgcolor: tint('speak', 18),
        '& > span:first-of-type': {color: v('speak'), fontWeight: 700}
    },
    '& [data-hover] > span:first-of-type': {color: v('ink')},
    '& [data-hover] > span:first-of-type::before': {content: '"▶"', position: 'absolute', left: 4, fontSize: 9},
    '& [data-p="aside"]': {color: v('aside')},
    '& [data-p="move"]': {color: v('move'), bgcolor: v('move-bg'), borderRadius: '3px'},
    '& [data-p="still"]': {color: v('still'), bgcolor: v('still-bg'), borderRadius: '3px'},
    '& [data-p="sound"]': {color: v('sound')},
    '& [data-voice]::after': {content: '""', position: 'absolute', left: GUTTER_PX - 12, top: 0, bottom: 0, width: 4},
    '& [data-voice="ready"]::after': {bgcolor: 'var(--who)', borderRadius: 2},
    '& [data-voice="missing"]::after': {border: '1px dashed var(--who)', borderRadius: 2},
    '& [data-first]::after': {top: 4},
    '& [data-last]::after': {bottom: 4},
    '&[data-rendering] [data-voice="missing"]::after': {animation: `${breathe} 1.6s infinite`}
} as const;

/** The offset in `text` under the pointer, read from the painted copy (the textarea tells nothing of it). */
const offsetAt = (area: HTMLTextAreaElement, text: string, x: number, y: number): number | null => {
    area.style.pointerEvents = 'none';
    const caret = document.caretPositionFromPoint?.(x, y);
    area.style.pointerEvents = '';
    const row = caret?.offsetNode.parentElement?.closest<HTMLElement>('[data-line]');
    const words = row?.querySelector('[data-words]');
    if (!caret || !row || !words?.contains(caret.offsetNode)) return null;
    const before = document.createRange();
    before.setStart(words, 0);
    before.setEnd(caret.offsetNode, caret.offset);
    const [start, end] = lineRange(text, Number(row.dataset.line));
    return Math.min(start + before.toString().length, end); // an empty line's \u200b: clamped to its end
};

interface EditorProps {
    text: string;
    onText: (text: string) => void;
    check: SceneCheck | null;
    checked: string; // the text `check` was made on: its marks follow their lines while the text changes
    vocabulary: SceneVocabulary | null;
    current: number | null; // the line the robot is playing
    rendering: boolean;
    onPlayFrom: (line: number) => void; // a click on a line's number
}

/** The line whose row is under `y` (a point on the screen), or null. */
const rowAt = (box: HTMLElement | null, y: number): number | null => {
    const rows = box?.querySelectorAll<HTMLElement>('[data-line]') ?? [];
    const row = [...rows].find((r) => r.getBoundingClientRect().bottom > y);
    return row && row.getBoundingClientRect().top <= y ? Number(row.dataset.line) : null;
};

export const Editor = ({text, onText, check, checked, vocabulary, current, rendering, onPlayFrom}: EditorProps) => {
    const area = useRef<HTMLTextAreaElement>(null);
    const scroller = useRef<HTMLDivElement>(null);
    const t = useWords(SCENES);
    const [menu, setMenu] = useState<{top: number; left: number} | null>(null);
    const [hover, setHover] = useState<number | null>(null); // the line number under the pointer
    /** The line whose number is under the pointer, or null when it points at the text. */
    const numberAt = (event: MouseEvent<HTMLTextAreaElement>) =>
        event.clientX - event.currentTarget.getBoundingClientRect().left < GUTTER_PX - 14
            ? rowAt(scroller.current, event.clientY)
            : null;
    const palette = useMemo(() => colours(text), [text]);
    const painted = useMemo(() => paint(text, palette), [text, palette]);
    const lines = marks(check, checked, text);
    const issues = [
        ...(check?.errors ?? []).map((issue) => ({...issue, error: true})),
        ...(check?.warnings ?? []).map((issue) => ({...issue, error: false}))
    ].sort((a, b) => a.line - b.line);

    /** The caret at [start, end), without scrolling to it. */
    const select = (start: number, end: number) => {
        area.current?.setSelectionRange(start, end);
        area.current?.focus({preventScroll: true});
    };

    const goTo = (line: number) => {
        select(...lineRange(text, line));
        const row = scroller.current?.querySelector<HTMLElement>(`[data-line="${line}"]`);
        if (scroller.current && row) scroller.current.scrollTop = Math.max(0, row.offsetTop - 3 * LINE_PX);
    };

    /** A right click: the caret where it points (a selection it falls in is kept, to be replaced), and the menu. */
    const open = (event: MouseEvent<HTMLTextAreaElement>) => {
        const el = event.currentTarget;
        event.preventDefault();
        const at = offsetAt(el, text, event.clientX, event.clientY);
        if (at !== null && !(el.selectionStart < el.selectionEnd && el.selectionStart <= at && at <= el.selectionEnd))
            el.setSelectionRange(at, at);
        setMenu({top: event.clientY, left: event.clientX});
    };

    const put = (group: keyof SceneVocabulary, word: string) => {
        setMenu(null);
        const el = area.current;
        const top = scroller.current?.scrollTop ?? 0;
        const next = insert(text, el?.selectionStart ?? 0, el?.selectionEnd ?? 0, snippet(group, word));
        onText(next.text);
        requestAnimationFrame(() => {
            select(next.caret, next.caret);
            if (scroller.current) scroller.current.scrollTop = top;
        });
    };

    return (
        <Stack spacing={1} sx={{flex: 1, minWidth: 0, minHeight: 0}}>
            {vocabulary && (
                <Insert
                    at={menu}
                    vocabulary={vocabulary}
                    onClose={() => {
                        setMenu(null);
                        const el = area.current; // the focus back where the right click left the caret
                        if (el) requestAnimationFrame(() => select(el.selectionStart, el.selectionEnd));
                    }}
                    onPick={put}
                />
            )}
            <Box
                ref={scroller}
                sx={{
                    flex: 1,
                    minHeight: 240,
                    overflow: 'auto',
                    border: `1px solid ${v('line')}`,
                    borderRadius: '12px',
                    font: `14px/${LINE_PX}px ${FONT}`,
                    fontVariationSettings: MONO,
                    bgcolor: v('raised'),
                    backgroundImage: `linear-gradient(to right, ${v('gutter')} ${GUTTER_PX - 18}px, transparent 0)`
                }}
            >
                <Box sx={{position: 'relative', minHeight: '100%'}}>
                    <Box aria-hidden data-rendering={rendering || undefined} sx={ROWS}>
                        {painted.map(({who, parts}, i) => {
                            const mark = lines.get(i + 1);
                            return (
                                <div
                                    key={i}
                                    data-line={i + 1}
                                    data-voice={mark?.voice}
                                    data-first={mark?.first || undefined}
                                    data-last={mark?.last || undefined}
                                    data-warning={mark?.warning || undefined}
                                    data-error={mark?.error || undefined}
                                    data-current={current === i + 1 || undefined}
                                    data-hover={hover === i + 1 || undefined}
                                    style={{'--who': colourOf(palette, mark?.who ?? who)} as object}
                                >
                                    <span>{i + 1}</span>
                                    <span data-words>
                                        {parts.map(([part, kind], j) =>
                                            kind === 'said' ? (
                                                part
                                            ) : (
                                                <span
                                                    key={j}
                                                    data-p={kind}
                                                    style={
                                                        kind === 'name' ? {color: colourOf(palette, who)} : undefined
                                                    }
                                                >
                                                    {part}
                                                </span>
                                            )
                                        )}
                                        {parts.every(([part]) => !part) && '​'}
                                    </span>
                                </div>
                            );
                        })}
                    </Box>
                    <Box
                        component="textarea"
                        ref={area}
                        value={text}
                        spellCheck={false}
                        aria-label={t.text}
                        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => onText(e.target.value)}
                        onContextMenu={open}
                        onMouseMove={(e) => setHover(numberAt(e))}
                        onMouseLeave={() => setHover(null)}
                        onMouseDown={(e) => {
                            const line = numberAt(e);
                            if (line === null || e.button !== 0) return;
                            e.preventDefault(); // the caret stays where it was
                            onPlayFrom(line);
                        }}
                        title={hover === null ? undefined : t.playFrom(hover)}
                        sx={{
                            cursor: hover === null ? 'text' : 'pointer',
                            ...WRAP,
                            position: 'absolute',
                            inset: 0,
                            width: '100%',
                            height: '100%',
                            p: `${PAD} ${PAD} ${PAD} ${GUTTER_PX}px`,
                            border: 0,
                            outline: 0,
                            resize: 'none',
                            overflow: 'hidden',
                            font: 'inherit',
                            fontVariationSettings: 'inherit',
                            color: 'transparent',
                            caretColor: v('ink'),
                            bgcolor: 'transparent',
                            '&::selection': {bgcolor: tint('speak', 32)}
                        }}
                    />
                </Box>
            </Box>
            <Stack direction="row" spacing={1} alignItems="flex-start">
                <Button
                    size="small"
                    variant="outlined"
                    color="inherit"
                    startIcon={<AddRounded />}
                    disabled={!vocabulary}
                    onClick={(e) => {
                        const {left, bottom} = e.currentTarget.getBoundingClientRect();
                        setMenu({top: bottom, left});
                    }}
                >
                    {t.insert}
                </Button>
                <Typography variant="caption" sx={{color: v('ink-3')}}>
                    {t.insertHint}
                </Typography>
            </Stack>
            {issues.length > 0 && (
                <Stack
                    sx={{maxHeight: 132, overflow: 'auto', p: 1, borderRadius: '10px', bgcolor: tint('fault', 7)}}
                    role="list"
                    aria-label={t.issues}
                >
                    {issues.map((issue, i) => (
                        <Typography
                            key={i}
                            role="listitem"
                            variant="body2"
                            onClick={() => goTo(issue.line)}
                            sx={{
                                cursor: 'pointer',
                                px: 1,
                                py: 0.25,
                                borderRadius: '6px',
                                color: v(issue.error ? 'fault' : 'attention'),
                                '&:hover': {bgcolor: v('hover')}
                            }}
                        >
                            {t.issue(issue.line, issue.message)}
                        </Typography>
                    ))}
                </Stack>
            )}
        </Stack>
    );
};
