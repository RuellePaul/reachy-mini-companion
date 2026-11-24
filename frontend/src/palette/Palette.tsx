import {useEffect, useMemo, useRef, useState, type KeyboardEvent} from 'react';
import {
    Box,
    Button,
    Dialog,
    InputBase,
    List,
    ListItemButton,
    ListSubheader,
    Typography,
    useMediaQuery
} from '@mui/material';
import EditNoteRounded from '@mui/icons-material/EditNoteRounded';
import SearchRounded from '@mui/icons-material/SearchRounded';
import ViewInArRounded from '@mui/icons-material/ViewInArRounded';
import {api, errorText} from '../api/client';
import type {Catalog, CatalogEntry} from '../api/schemas';
import {useLang, useWords} from '../i18n';
import {macCatalog} from '../mac';
import {v} from '../theme';
import {FRAME} from '../words';
import {flatten, move, sections} from './search';
import {PALETTE} from './words';

interface Props {
    open: boolean;
    onClose: () => void;
    /** Run the entry's action; `preview` plays it in the studio's 3D (a scene: opens it) instead of on the robot. */
    onRun: (entry: CatalogEntry, preview: boolean) => Promise<void>;
    /** Its name, for the search field to ask what it should do. */
    name?: string;
}

/** Cmd+K: everything the robot can do, searchable, runnable from the keyboard. */
export const Palette = ({open, onClose, onRun, name}: Props) => {
    const fullScreen = useMediaQuery('(max-width:599.95px)');
    return (
        <Dialog
            open={open}
            onClose={onClose}
            fullScreen={fullScreen}
            fullWidth
            maxWidth={false}
            slotProps={{
                paper: {
                    sx: {
                        alignSelf: 'flex-start',
                        width: 640,
                        mt: fullScreen ? 0 : '12vh',
                        maxHeight: fullScreen ? 'none' : '72vh',
                        overflow: 'hidden',
                        bgcolor: v('raised')
                    }
                }
            }}
        >
            {open && <Body onClose={onClose} onRun={onRun} fullScreen={fullScreen} name={name} />}
        </Dialog>
    );
};

/** Mounted at each opening: the search starts empty and the catalogue is read afresh. */
const Body = ({onClose, onRun, fullScreen, name}: Omit<Props, 'open'> & {fullScreen: boolean}) => {
    const t = useWords(PALETTE);
    const close = useWords(FRAME).close;
    const language = useLang();
    const [read, setRead] = useState<Catalog | null>(null);
    const catalog = useMemo(() => read && macCatalog(read, language), [read, language]); // searched as shown
    const [query, setQuery] = useState('');
    const [index, setIndex] = useState(0);
    const [status, setStatus] = useState('');
    const listRef = useRef<HTMLUListElement>(null);

    useEffect(() => {
        api.catalog().then(setRead, (err: unknown) => setStatus(errorText(err)));
    }, []);

    const shown = useMemo(() => (catalog ? sections(catalog, query) : []), [catalog, query]);
    const flat = useMemo(() => flatten(shown), [shown]);
    const current = flat[Math.min(index, flat.length - 1)];

    useEffect(() => {
        listRef.current?.querySelector('[data-current="true"]')?.scrollIntoView({block: 'nearest'});
    }, [index, shown]);

    const run = async (entry: CatalogEntry, preview: boolean) => {
        setStatus(preview ? t.previewing(entry.label) : `${entry.label}…`);
        try {
            await onRun(entry, preview);
            onClose();
        } catch (err) {
            setStatus(errorText(err));
        }
    };

    const onKeyDown = (event: KeyboardEvent) => {
        if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
            event.preventDefault();
            setIndex((i) => move(i, event.key, flat.length));
        } else if (event.key === 'Enter' && current) {
            event.preventDefault();
            if (event.shiftKey && !current.preview) setStatus(t.noPreview(current.label));
            else void run(current, event.shiftKey);
        }
    };

    return (
        <>
            <Box
                sx={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 1.5,
                    px: 2.5,
                    height: 60,
                    flexShrink: 0,
                    borderBottom: `1px solid ${v('line')}`
                }}
            >
                <SearchRounded sx={{color: v('ink-2')}} />
                <InputBase
                    autoFocus
                    fullWidth
                    placeholder={t.ask(name ?? t.robot)}
                    value={query}
                    onChange={(e) => {
                        setQuery(e.target.value);
                        setIndex(0);
                    }}
                    onKeyDown={onKeyDown}
                    inputProps={{
                        'aria-label': t.search,
                        'aria-activedescendant': current ? `palette-${current.id}` : undefined
                    }}
                    sx={{fontSize: 18, '& input::placeholder': {color: v('ink-3'), opacity: 1}}}
                />
                {fullScreen && (
                    <Button color="inherit" onClick={onClose} sx={{mr: -1}}>
                        {close}
                    </Button>
                )}
            </Box>
            <List ref={listRef} role="listbox" sx={{flex: 1, overflowY: 'auto', px: 1, pt: 0, pb: 1, minHeight: 0}}>
                {shown.map((section) => (
                    <li key={section.key}>
                        <ul style={{padding: 0}}>
                            <ListSubheader sx={{px: 1.5, pt: 1, bgcolor: v('raised'), color: v('ink-3')}}>
                                {section.label}
                            </ListSubheader>
                            {section.entries.map((entry) => {
                                const here = flat.indexOf(entry);
                                const selected = entry === current;
                                const scene = entry.action.kind === 'scene';
                                return (
                                    <ListItemButton
                                        key={entry.id}
                                        id={`palette-${entry.id}`}
                                        role="option"
                                        aria-selected={selected}
                                        data-current={selected}
                                        selected={selected}
                                        onMouseMove={() => setIndex(here)}
                                        onClick={() => void run(entry, false)}
                                        sx={{
                                            alignItems: 'center',
                                            gap: 2,
                                            px: 1.5,
                                            py: 1,
                                            '&.Mui-selected': {bgcolor: v('selected')}
                                        }}
                                    >
                                        <Box sx={{flex: 1, minWidth: 0}}>
                                            <Typography variant="body2" sx={{fontWeight: 620}}>
                                                {entry.label}
                                            </Typography>
                                            <Typography variant="caption" component="div" sx={{color: v('ink-2')}}>
                                                {entry.description}
                                            </Typography>
                                            {entry.say && (
                                                <Typography
                                                    variant="caption"
                                                    component="div"
                                                    sx={{mt: 0.25, color: v('ink-3')}}
                                                >
                                                    {t.say}{' '}
                                                    <Box component="span" sx={{color: v('ink'), fontWeight: 560}}>
                                                        {t.quoted(entry.say)}
                                                    </Box>
                                                </Typography>
                                            )}
                                        </Box>
                                        {entry.preview && (
                                            <Button
                                                size="small"
                                                color="inherit"
                                                variant={selected ? 'outlined' : 'text'}
                                                startIcon={scene ? <EditNoteRounded /> : <ViewInArRounded />}
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    void run(entry, true);
                                                }}
                                                sx={{flexShrink: 0, color: v('ink-2'), borderColor: v('line-strong')}}
                                            >
                                                {scene ? t.open : t.preview}
                                            </Button>
                                        )}
                                    </ListItemButton>
                                );
                            })}
                        </ul>
                    </li>
                ))}
                {catalog && flat.length === 0 && (
                    <Typography sx={{px: 1.5, py: 3, color: v('ink-2')}}>{t.nothing(query)}</Typography>
                )}
            </List>
            <Box
                sx={{
                    px: 2.5,
                    py: 1.25,
                    borderTop: `1px solid ${v('line')}`,
                    display: {xs: status ? 'flex' : 'none', sm: 'flex'}, // a phone has no keys to hint at
                    alignItems: 'center',
                    gap: 2,
                    color: v('ink-2'),
                    flexShrink: 0
                }}
            >
                <Typography variant="caption" sx={{flex: 1, minWidth: 0}} role="status">
                    {status}
                </Typography>
                <Hint keys={['↑', '↓']} label={t.choose} />
                <Hint keys={['↵']} label={t.run} />
                <Hint keys={['⇧', '↵']} label={t.preview3d} />
            </Box>
        </>
    );
};

const Hint = ({keys, label}: {keys: string[]; label: string}) => (
    <Box
        component="span"
        sx={{
            display: {xs: 'none', sm: 'inline-flex'},
            alignItems: 'center',
            gap: 0.5,
            fontSize: 12,
            whiteSpace: 'nowrap'
        }}
    >
        {keys.map((key) => (
            <kbd key={key}>{key}</kbd>
        ))}
        <Box component="span" sx={{ml: 0.25}}>
            {label}
        </Box>
    </Box>
);
