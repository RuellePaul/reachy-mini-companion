// The robot in 3D: the studio's stage, one robot or several side by side.
import {useEffect, useRef, useState} from 'react';
import {Box, ButtonBase, CircularProgress, Typography} from '@mui/material';
import {Matrix4} from 'three';
import type {PoseFrame, PreviewFrames} from '../api/schemas';
import {useWords} from '../i18n';
import {Bench, loadModel, type View} from './bench';
import {v} from '../theme';
import {frameAt, NEUTRAL_ANTENNAS} from './rig';
import {STUDIO} from './words';

export interface StageProps {
    robots: (PreviewFrames | null)[]; // side by side, each playing its frames, or (null) the measured pose, or at rest
    time: () => number; // read at every frame drawn, as `pose`
    pose: () => PoseFrame | null;
    ghost?: boolean; // the measured pose, translucent, over the first robot
    stale?: boolean; // the measured pose is old: greyed
    view: View;
    onOrbit?: () => void;
    labels?: string[]; // a clickable name at each robot's feet
    picked?: number | null;
    onPick?: (index: number) => void;
    height?: number | string;
}

// A bench lit from above; the names under the robots compared; the robot when its measured pose is old.
const BENCH = {position: 'relative', overflow: 'hidden', background: v('stage')};
const TAG = {
    position: 'absolute',
    left: 0,
    top: 0,
    px: 1,
    py: 0.25,
    mt: 1,
    borderRadius: '6px',
    pointerEvents: 'auto',
    font: 'inherit',
    fontSize: 12,
    whiteSpace: 'nowrap'
};
const GREYED = 'grayscale(1) brightness(0.6)';
const REST = new Matrix4();
const head = new Matrix4();

const draw = (bench: Bench, {robots, time, pose}: StageProps) => {
    const t = time();
    const measured = pose();
    bench.robots.forEach(({rig}, i) => {
        const frames = robots[i];
        if (frames) {
            const {antennas, body} = frameAt(frames, t, head);
            rig.preview(head, antennas, body);
        } else if (measured) rig.measured(measured);
        else rig.preview(REST, NEUTRAL_ANTENNAS, 0);
    });
    if (bench.ghost) bench.ghost.root.visible = measured !== null;
    if (bench.ghost && measured) bench.ghost.rig.measured(measured);
};

/** The 3D bench as a React component; nothing re-renders per frame (the bench reads `time` and `pose`). */
export const Stage = (props: StageProps) => {
    const {robots, ghost = false, stale = false, view, labels = [], picked = null, onPick, height = 360} = props;
    const t = useWords(STUDIO);
    const canvas = useRef<HTMLCanvasElement>(null);
    const tags = useRef<HTMLDivElement>(null);
    const latest = useRef(props);
    const [bench, setBench] = useState<Bench | null>(null);
    const [failed, setFailed] = useState(false);
    const labelKey = labels.join('\n');

    useEffect(() => {
        latest.current = props;
    });
    useEffect(() => {
        let alive = true;
        let made: Bench | null = null;
        loadModel()
            .then((model) => {
                if (!alive || !canvas.current) return;
                const b = (made = new Bench(canvas.current, model)); // throws when the browser gives no WebGL
                b.onFrame = () => draw(b, latest.current);
                b.onOrbit = () => latest.current.onOrbit?.();
                setBench(b);
                performance.mark('studio:3d');
            })
            .catch(() => alive && setFailed(true));
        return () => {
            alive = false;
            made?.dispose();
        };
    }, []);
    useEffect(() => bench?.layout(robots.length, ghost), [bench, robots.length, ghost]);
    useEffect(() => bench?.label([...(tags.current?.children ?? [])] as HTMLElement[]), [bench, labelKey]);
    useEffect(() => bench?.pick(picked), [bench, picked, robots.length]);
    useEffect(() => bench?.setView(view), [bench, view]);

    return (
        <Box sx={{...BENCH, height}}>
            <canvas
                ref={canvas}
                aria-label={t.canvas}
                style={{width: '100%', height: '100%', display: 'block', filter: stale ? GREYED : ''}}
            />
            <Box ref={tags} sx={{position: 'absolute', inset: 0, pointerEvents: 'none'}}>
                {labels.map((label, i) => (
                    <ButtonBase
                        key={label}
                        onClick={() => onPick?.(i)}
                        sx={{
                            ...TAG,
                            fontWeight: i === picked ? 680 : 520,
                            color: i === picked ? v('on-lens') : v('ink-2'),
                            bgcolor: i === picked ? v('lens') : 'transparent'
                        }}
                    >
                        {label}
                    </ButtonBase>
                ))}
            </Box>
            {!bench && (
                <Box sx={{position: 'absolute', inset: 0, display: 'grid', placeItems: 'center'}}>
                    {failed ? (
                        <Typography color="text.secondary">{t.noCanvas}</Typography>
                    ) : (
                        <CircularProgress size={24} sx={{color: v('ink-3')}} />
                    )}
                </Box>
            )}
        </Box>
    );
};
