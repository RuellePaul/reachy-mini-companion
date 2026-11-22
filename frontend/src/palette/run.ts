// What a palette entry does once chosen: on the robot, in a surface, in the composer or in the settings drawer.
import {ApiError, type api} from '../api/client';
import type {CatalogEntry, SettingsField} from '../api/schemas';
import {wordsNow} from '../i18n';
import {ask, type Surface} from '../surfaces';
import {PALETTE} from './words';

export interface Targets {
    api: Pick<typeof api, 'session' | 'tryMove' | 'playScene' | 'say'>;
    setSurface: (surface: Surface) => void;
    openSettings: (field: SettingsField) => void;
    compose: (text: string) => void;
}

/** `preview` plays a move in the studio's 3D, or opens a scene in its editor: a preview never reaches the robot. */
export const runEntry = async ({action, label}: CatalogEntry, preview: boolean, to: Targets): Promise<void> => {
    if (action.kind === 'try' && preview) to.setSurface({kind: 'studio', move: action.move});
    else if (action.kind === 'try') await to.api.tryMove(action.move);
    else if (action.kind === 'scene' && preview)
        to.setSurface({kind: 'scenes', sceneId: action.scene_id, asked: ask()});
    else if (action.kind === 'scene') await to.api.playScene(action.scene_id);
    else if (preview) throw new ApiError(0, wordsNow(PALETTE).noPreview(label));
    else if (action.kind === 'session') await to.api.session(action.command);
    else if (action.kind === 'app') to.setSurface({kind: 'apps', appId: action.app_id ?? undefined, asked: ask()});
    else if (action.kind === 'say') await to.api.say(action.text);
    else if (action.kind === 'compose') to.compose(action.text);
    else to.openSettings(action.field);
};
