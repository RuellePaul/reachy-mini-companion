// The suite reads the dashboard in French, whatever language the machine that runs it speaks; a test that picks
// English leaves French to the next.
import {afterEach} from 'vitest';
import {setLang} from '../src/i18n';

setLang('fr');
afterEach(() => setLang('fr'));
