import {useEffect, useState, type ReactNode} from 'react';
import {Box, Button, CircularProgress, Collapse, Drawer, IconButton, Stack, TextField, Typography} from '@mui/material';
import CloseRounded from '@mui/icons-material/CloseRounded';
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded';
import RestartAltRounded from '@mui/icons-material/RestartAltRounded';
import {api, errorText} from '../api/client';
import {
    IDLE_SLEEP_MAX_MIN,
    NAME_MAX,
    PERSONALITY_MAX,
    type Settings,
    type SettingsField,
    type SettingsView
} from '../api/schemas';
import {formatDuration} from '../format';
import {VolumeControl} from '../header/VolumeControl';
import {LANGS, setLang, useLang, useWords} from '../i18n';
import {useLive} from '../live';
import {mac} from '../mac';
import {Sticker} from '../Sticker';
import {v} from '../theme';
import {FRAME} from '../words';
import {changedFields, draftOf, editsSince} from './draft';
import {PersonalityPicker} from './PersonalityPicker';
import {Field, SelectField, SwitchField} from './SelectField';
import {VoicePicker} from './VoicePicker';
import {SETTINGS} from './words';

const SLEEP_MINUTES = [0, 1, 2, 3, 5, 10, 15, 30, IDLE_SLEEP_MAX_MIN];
const ANIMATIONS = [
    {mode: 'idle', field: 'idle_animation'},
    {mode: 'listening', field: 'listening_animation'},
    {mode: 'talking', field: 'talking_animation'}
] as const;
const APP_MODELS = ['opus', 'sonnet', 'haiku'] as const;
const APP_EFFORTS = ['low', 'medium', 'high', 'xhigh'] as const;

const Section = ({title, hint, children}: {title: string; hint?: string; children: ReactNode}) => (
    <Stack component="section" spacing={2} sx={{py: 3, '& + &': {borderTop: `1px solid ${v('line')}`}}}>
        <Box>
            <Typography variant="h6" component="h3">
                {title}
            </Typography>
            {hint && (
                <Typography variant="body2" sx={{color: v('ink-2'), mt: 0.25}}>
                    {hint}
                </Typography>
            )}
        </Box>
        {children}
    </Stack>
);

interface Props {
    open: boolean;
    onClose: () => void;
    /** The field to show first: the drawer opens on it, focused. */
    field?: SettingsField;
}

export const SettingsDrawer = ({open, onClose, field}: Props) => {
    const {settings} = useLive();
    const t = useWords(SETTINGS);
    const close = useWords(FRAME).close;
    return (
        <Drawer anchor="right" open={open} onClose={onClose} slotProps={{paper: {sx: {width: {xs: '100%', sm: 480}}}}}>
            <Stack
                direction="row"
                alignItems="center"
                spacing={1.5}
                sx={{px: 3, pt: 2, pb: 2, borderBottom: `1px solid ${v('line')}`}}
            >
                <Sticker name="settings" size={56} />
                <Box sx={{flex: 1}}>
                    <Typography variant="h4" component="h2">
                        {t.title}
                    </Typography>
                    <Typography variant="body2" sx={{color: v('ink-2'), mt: 0.25}}>
                        {t.applies}
                    </Typography>
                </Box>
                <IconButton aria-label={close} onClick={onClose} sx={{mr: -1, alignSelf: 'flex-start'}}>
                    <CloseRounded />
                </IconButton>
            </Stack>
            {settings ? (
                <SettingsForm view={settings} onClose={onClose} field={field} />
            ) : (
                <Box sx={{p: 6, display: 'grid', placeItems: 'center', color: v('ink-3')}}>
                    <CircularProgress size={24} />
                </Box>
            )}
        </Drawer>
    );
};

// Mounted only while the drawer is open: every opening starts from what the Mac holds, with no edits.
const SettingsForm = ({view, onClose, field}: Omit<Props, 'open'> & {view: SettingsView}) => {
    const {link, session, saveSettings} = useLive();
    const t = useWords(SETTINGS);
    const language = useLang();
    const [edits, setEdits] = useState<Partial<Settings>>({});
    const [busy, setBusy] = useState(false);
    const [notice, setNotice] = useState<{text: string; error: boolean} | null>(null);
    const [showPrompt, setShowPrompt] = useState(false);

    const draft = draftOf(view.settings, edits);
    const changes = changedFields(view.settings, draft);
    const nameError = draft.name.trim() ? undefined : t.nameEmpty;
    const dirty = Object.keys(changes).length > 0;
    const canSave = link === 'up' && !busy && !nameError && dirty;
    const minutes = [...new Set([...SLEEP_MINUTES, draft.idle_sleep_minutes])].sort((a, b) => a - b);
    const prompt = [view.system_prompt, view.settings.web_search ? view.web_prompt : ''].filter(Boolean).join('\n\n');

    useEffect(() => {
        const asked = field && document.getElementById(`settings-${field}`);
        asked?.focus();
        asked?.scrollIntoView({block: 'center'});
    }, [field]);

    const set = <K extends keyof Settings>(key: K, value: Settings[K]) => {
        setEdits((current) => ({...current, [key]: value}));
        setNotice(null);
    };

    const run = async (action: () => Promise<void>, done: string) => {
        setBusy(true);
        setNotice(null);
        try {
            await action();
            setNotice({text: done, error: false});
        } catch (err) {
            setNotice({text: errorText(err), error: true});
        } finally {
            setBusy(false);
        }
    };
    // The fields stay editable during the request: what is typed meanwhile stays an edit, to be saved next.
    const saveEdits = async () => {
        const sent = edits;
        await saveSettings(changes);
        setEdits((current) => editsSince(sent, current));
    };
    const save = () => run(saveEdits, t.saved);
    // The robot wakes with what the form shows: unsaved edits are saved first.
    const restart = () =>
        run(async () => {
            if (dirty) await saveEdits();
            await api.session('restart');
        }, t.restartAsked(dirty));

    return (
        <>
            <Box sx={{flex: 1, minHeight: 0, overflowY: 'auto', px: 3}}>
                <Section title={t.identity}>
                    <Field label={t.name} id="settings-name" hint={nameError}>
                        <TextField
                            size="small"
                            id="settings-name"
                            value={draft.name}
                            error={nameError !== undefined}
                            slotProps={{htmlInput: {maxLength: NAME_MAX}}}
                            onChange={(e) => set('name', e.target.value)}
                        />
                    </Field>
                    <Field label={t.voice} id="settings-voice">
                        <VoicePicker
                            id="settings-voice"
                            value={draft.voice}
                            options={view.options.voices}
                            onChange={(voice) => set('voice', voice)}
                        />
                    </Field>
                </Section>
                <Section title={t.speaker} hint={t.speakerHint}>
                    <VolumeControl id="settings-volume" onError={(text) => setNotice({text, error: true})} />
                </Section>
                <Section title={t.personality} hint={t.personalityHint}>
                    <Field label={t.character} id="settings-character" hint={t.characterHint}>
                        <PersonalityPicker
                            id="settings-character"
                            value={draft.personality}
                            onChange={(text) => set('personality', text)}
                        />
                    </Field>
                    <TextField
                        multiline
                        id="settings-personality"
                        minRows={4}
                        maxRows={14}
                        value={draft.personality}
                        placeholder={t.personalityExample}
                        helperText={`${draft.personality.length} / ${PERSONALITY_MAX}`}
                        slotProps={{
                            htmlInput: {maxLength: PERSONALITY_MAX, 'aria-label': t.personality},
                            formHelperText: {sx: {textAlign: 'right', fontVariantNumeric: 'tabular-nums'}}
                        }}
                        onChange={(e) => set('personality', e.target.value)}
                    />
                </Section>
                <Section title={t.animations} hint={t.animationsHint}>
                    {ANIMATIONS.map(({mode, field: key}) => (
                        <SelectField
                            key={key}
                            id={`settings-${key}`}
                            label={t.modes[mode]}
                            value={draft[key]}
                            options={(view.options.animations[mode] ?? []).map((o) => ({
                                value: o.key,
                                label: mac(o.label)
                            }))}
                            onChange={(value) => set(key, value)}
                        />
                    ))}
                    <SwitchField
                        id="settings-gaze_tracking"
                        label={t.gaze}
                        hint={t.gazeHint}
                        checked={draft.gaze_tracking}
                        onChange={(on) => set('gaze_tracking', on)}
                    />
                </Section>
                <Section title={t.sleep}>
                    <SelectField
                        id="settings-idle_sleep_minutes"
                        label={t.sleepAfter}
                        value={draft.idle_sleep_minutes}
                        options={minutes.map((m) => ({value: m, label: m === 0 ? t.never : formatDuration(m * 60)}))}
                        hint={t.sleepHint}
                        onChange={(m) => set('idle_sleep_minutes', m)}
                    />
                    <SwitchField
                        id="settings-web_search"
                        label={t.web}
                        hint={t.webHint}
                        checked={draft.web_search}
                        onChange={(on) => set('web_search', on)}
                    />
                </Section>
                <Section title={t.apps} hint={t.appsHint}>
                    <SelectField
                        id="settings-apps_model"
                        label={t.model}
                        value={draft.apps_model}
                        options={APP_MODELS.map((value) => ({value, label: t.models[value]}))}
                        onChange={(model) => set('apps_model', model)}
                    />
                    <SelectField
                        id="settings-apps_effort"
                        label={t.effort}
                        value={draft.apps_effort}
                        options={APP_EFFORTS.map((value) => ({value, label: t.efforts[value]}))}
                        hint={t.effortHint}
                        onChange={(effort) => set('apps_effort', effort)}
                    />
                </Section>
                <Section title={t.dashboard}>
                    <SelectField
                        id="settings-language"
                        label={t.language}
                        value={language}
                        options={LANGS}
                        hint={t.languageHint}
                        onChange={setLang}
                    />
                </Section>
                <Section title={t.prompt} hint={t.promptHint}>
                    <Box>
                        <Button
                            size="small"
                            color="inherit"
                            variant="outlined"
                            endIcon={
                                <ExpandMoreRounded
                                    sx={{
                                        transform: showPrompt ? 'rotate(180deg)' : 'none',
                                        transition: 'transform 200ms'
                                    }}
                                />
                            }
                            aria-expanded={showPrompt}
                            onClick={() => setShowPrompt((shown) => !shown)}
                        >
                            {showPrompt ? t.hidePrompt : t.showPrompt}
                        </Button>
                        <Collapse in={showPrompt}>
                            <Typography
                                component="pre"
                                variant="caption"
                                sx={{
                                    m: 0,
                                    mt: 1.5,
                                    p: 2,
                                    maxHeight: 360,
                                    overflowY: 'auto',
                                    whiteSpace: 'pre-wrap',
                                    color: v('ink-2'),
                                    bgcolor: v('sunken'),
                                    borderRadius: '10px',
                                    fontFamily: 'inherit'
                                }}
                            >
                                {prompt}
                            </Typography>
                        </Collapse>
                    </Box>
                </Section>
            </Box>
            <Stack spacing={1.5} sx={{px: 3, py: 2, borderTop: `1px solid ${v('line')}`, bgcolor: v('page')}}>
                {notice && (
                    <Typography
                        variant="body2"
                        role={notice.error ? 'alert' : 'status'}
                        sx={{color: v(notice.error ? 'fault' : 'listen'), fontWeight: 560}}
                    >
                        {notice.text}
                    </Typography>
                )}
                <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap" justifyContent="flex-end">
                    {session.awake && (
                        <Button
                            variant="outlined"
                            color="inherit"
                            startIcon={<RestartAltRounded />}
                            disabled={busy || link !== 'up' || (dirty && nameError !== undefined)}
                            onClick={restart}
                            sx={{mr: 'auto', width: {xs: '100%', sm: 'auto'}}}
                        >
                            {dirty ? t.saveAndRestart : t.restart}
                        </Button>
                    )}
                    <Button color="inherit" onClick={onClose}>
                        {t.cancel}
                    </Button>
                    <Button variant="contained" disabled={!canSave} onClick={save}>
                        {t.save}
                    </Button>
                </Stack>
            </Stack>
        </>
    );
};
