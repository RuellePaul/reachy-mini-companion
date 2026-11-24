// What a render error leaves on screen: a word and a way out, in place of what failed, never a blank page.
import {Component, type ReactNode} from 'react';
import {Button, Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle, Typography} from '@mui/material';
import {wordsNow} from './i18n';
import {v} from './theme';
import {FRAME} from './words';

interface Props {
    title: string;
    /** Closes what failed, the rest staying; without it, only a reload is offered. */
    onClose?: () => void;
    children: ReactNode;
}

/** Catches what its children throw while rendering, a surface's chunk that could not be fetched included: React keeps a
 * lazy component's failed import, so only a reload fetches it again. */
export class Boundary extends Component<Props, {error: Error | null}> {
    state = {error: null as Error | null};

    static getDerivedStateFromError(error: unknown) {
        return {error: error instanceof Error ? error : new Error(String(error))};
    }

    render() {
        const {title, onClose, children} = this.props;
        const {error} = this.state;
        if (!error) return children;
        const t = wordsNow(FRAME);
        return (
            <Dialog open onClose={onClose}>
                <DialogTitle>{title}</DialogTitle>
                <DialogContent>
                    <DialogContentText>{t.reloadToRetry}</DialogContentText>
                    <Typography variant="caption" component="p" sx={{mt: 1, color: v('ink-3')}}>
                        {error.message}
                    </Typography>
                </DialogContent>
                <DialogActions>
                    {onClose && (
                        <Button color="inherit" onClick={onClose}>
                            {t.close}
                        </Button>
                    )}
                    <Button variant="contained" onClick={() => window.location.reload()}>
                        {t.reload}
                    </Button>
                </DialogActions>
            </Dialog>
        );
    }
}
