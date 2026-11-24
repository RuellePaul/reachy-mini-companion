// The « Insérer » menu, opened by a right click in the text or by its button: everything a scene may hold, by kind
// (the moves by the feeling they show, the voices by gender), searched accent-blind across every kind; a click, or
// Entrée on the first match of a search, writes it at the caret.
import {useMemo, useState} from 'react';
import {Box, Chip, Popover, Tab, Tabs, TextField, Typography} from '@mui/material';
import type {SceneVocabulary} from '../api/schemas';
import {useLang, useWords} from '../i18n';
import {macVocabulary} from '../mac';
import {fold} from '../palette/search';
import {v} from '../theme';
import {snippet} from './text';
import {SCENES} from './words';

type Group = keyof SceneVocabulary;
type Word = SceneVocabulary[Group][number] & {group: Group};

const PAPER = {
    mt: 0.5,
    width: 'min(760px, calc(100vw - 32px))',
    height: 'min(560px, 72vh)',
    display: 'flex',
    flexDirection: 'column'
};

/** The sections shown: a kind's own families, or, while searching, every kind's matches (best first). */
const sectionsOf = (
    vocabulary: SceneVocabulary,
    group: Group,
    query: string,
    groups: [Group, string][]
): [string, Word[]][] => {
    const words = (g: Group) => vocabulary[g].map((word) => ({...word, group: g}));
    const terms = fold(query).split(/\s+/).filter(Boolean);
    if (terms.length === 0) {
        const families = new Map<string, Word[]>();
        for (const word of words(group)) families.set(word.family, [...(families.get(word.family) ?? []), word]);
        return [...families];
    }
    const rank = (word: Word) => (fold(word.label).startsWith(terms[0]) ? 0 : 1);
    return groups
        .map(([g, title]): [string, Word[]] => [
            title,
            words(g)
                .filter((word) => terms.every((t) => fold(`${word.label} ${word.word} ${word.family}`).includes(t)))
                .sort((a, b) => rank(a) - rank(b))
        ])
        .filter(([, list]) => list.length > 0);
};

interface InsertProps {
    at: {top: number; left: number} | null; // where it was asked: the pointer
    vocabulary: SceneVocabulary;
    onClose: () => void;
    onPick: (group: Group, word: string) => void;
}

export const Insert = ({at, vocabulary: sent, onClose, onPick}: InsertProps) => {
    const t = useWords(SCENES);
    const language = useLang();
    const vocabulary = useMemo(() => macVocabulary(sent, language), [sent, language]); // searched as shown
    const groups = Object.entries(t.groups) as [Group, string][];
    const [group, setGroup] = useState<Group>('emotions');
    const [query, setQuery] = useState('');
    const sections = sectionsOf(vocabulary, group, query, groups);
    const pick = (word: Word) => {
        setQuery('');
        onPick(word.group, word.word);
    };
    return (
        <Popover
            open={!!at}
            anchorReference="anchorPosition"
            anchorPosition={at ?? undefined}
            onClose={onClose}
            disableRestoreFocus // the editor puts the focus back where the word went
            slotProps={{paper: {sx: PAPER}}}
        >
            <Box sx={{px: 2, pt: 2, borderBottom: `1px solid ${v('line')}`}}>
                <TextField
                    autoFocus
                    fullWidth
                    size="small"
                    placeholder={t.search}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && query.trim() && sections[0] && pick(sections[0][1][0])}
                />
                <Tabs
                    value={query ? false : group}
                    onChange={(_, value: Group) => {
                        setGroup(value);
                        setQuery('');
                    }}
                    variant="scrollable"
                    allowScrollButtonsMobile // the kinds a phone cannot fit are behind an arrow, not out of sight
                    sx={{minHeight: 40, '& .MuiTab-root': {minHeight: 40, px: 1.5}}}
                >
                    {groups.map(([g, label]) => (
                        <Tab
                            key={g}
                            value={g}
                            label={
                                <span>
                                    {label}
                                    <Box component="span" sx={{ml: 0.75, color: v('ink-3'), fontWeight: 500}}>
                                        {vocabulary[g].length}
                                    </Box>
                                </span>
                            }
                        />
                    ))}
                </Tabs>
            </Box>
            <Box sx={{flex: 1, overflow: 'auto', p: 2}}>
                <Box sx={{columnWidth: 330, columnGap: 3}}>
                    {sections.map(([title, words]) => (
                        <Box key={title || 'all'} sx={{breakInside: 'avoid', mb: 1.5}}>
                            <Typography
                                variant="overline"
                                component="div"
                                hidden={!title}
                                sx={{mb: 0.75, color: v('ink-2')}}
                            >
                                {title}
                            </Typography>
                            <Box sx={{display: 'flex', flexWrap: 'wrap', gap: 0.75}}>
                                {words.map((word) => (
                                    <Chip
                                        key={`${word.group}:${word.word}`}
                                        role="menuitem"
                                        size="small"
                                        variant="outlined"
                                        label={word.label}
                                        title={snippet(word.group, word.word)}
                                        onClick={() => pick(word)}
                                        sx={{
                                            bgcolor: v('raised'),
                                            '&:hover': {borderColor: v('ink'), bgcolor: v('hover')}
                                        }}
                                    />
                                ))}
                            </Box>
                        </Box>
                    ))}
                </Box>
                {!query && t.notes[group] && (
                    <Typography variant="caption" component="p" sx={{color: v('ink-2'), maxWidth: 560}}>
                        {t.notes[group]}
                    </Typography>
                )}
                {query && sections.length === 0 && <Typography color="text.secondary">{t.nothing(query)}</Typography>}
            </Box>
        </Popover>
    );
};
