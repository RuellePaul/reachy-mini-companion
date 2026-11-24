import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {CssBaseline, ThemeProvider} from '@mui/material';
import '@fontsource-variable/recursive/full.css';
import {App} from './App';
import {Boundary} from './Boundary';
import {wordsNow} from './i18n';
import {LiveProvider} from './live';
import {theme} from './theme';
import {FRAME} from './words';

const root = document.getElementById('root');
if (!root) throw new Error('#root is missing from index.html');

createRoot(root).render(
    <StrictMode>
        <ThemeProvider theme={theme}>
            <CssBaseline />
            <Boundary title={wordsNow(FRAME).dashboardFailed}>
                <LiveProvider>
                    <App />
                </LiveProvider>
            </Boundary>
        </ThemeProvider>
    </StrictMode>
);
