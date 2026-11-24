// « Porcelaine et graphite »: the robot's own materials, its matte white shell and its graphite antennas and lenses,
// in one dark scheme. The only bright colours are the signals: the phases, the statuses, the cast of a scene. Every
// colour is a CSS variable.
import {createTheme} from '@mui/material/styles';

const TOKENS = {
    page: '#16181B',
    plate: '#1D2024',
    raised: '#25292E',
    sunken: '#121417',
    line: '#30353C',
    'line-strong': '#474D56',
    ink: '#EBEDF0',
    'ink-2': '#A0A7B1',
    'ink-3': '#8A919C', // the faintest text still reads at 4.5:1 on the brightest surface, raised
    lens: '#EBEDF0',
    'on-lens': '#16181B',
    hover: 'rgba(235, 237, 240, 0.06)',
    selected: 'rgba(235, 237, 240, 0.10)',
    focus: '#EBEDF0',
    shadow: '0 0 0 1px rgba(0, 0, 0, 0.4), 0 24px 48px -12px rgba(0, 0, 0, 0.7)',
    glass: 'rgba(31, 34, 38, 0.76)',
    listen: '#3DCB8E',
    think: '#A592FF',
    speak: '#FFCA3B',
    sleep: '#8EA8CB',
    attention: '#F2A541',
    fault: '#F26B5E',
    ghost: '#8A96F0',
    screen: '#121417', // an app's frame before it paints
    'on-signal': '#FFFFFF',
    stage: 'radial-gradient(ellipse at 50% 38%, #262A30 0%, #17191C 72%, #111316 100%)',
    grid: '#252A31',
    'grid-major': '#353C46',
    gutter: '#1A1D21',
    aside: '#757D89',
    move: '#C9BEFF',
    'move-bg': 'rgba(117, 130, 235, 0.2)',
    still: '#9FB3C8',
    'still-bg': 'rgba(160, 174, 192, 0.14)',
    sound: '#F2C46D',
    'cast-n': '#A0AEC0',
    'cast-0': '#7CB7FF',
    'cast-1': '#F0A6CA',
    'cast-2': '#86D6A9',
    'cast-3': '#F4A07A',
    'cast-4': '#6DD3D6',
    'cast-5': '#E3A2F5',
    'cast-6': '#C7D06B',
    'cast-7': '#D8B98A'
};

export type Token = keyof typeof TOKENS;
/** A colour of the theme, for `sx` and `style`: `v('ink')` is `var(--r-ink)`. */
export const v = (token: Token) => `var(--r-${token})`;
/** A token's colour at `percent` % over what is under it. */
export const tint = (token: Token, percent: number) => `color-mix(in srgb, ${v(token)} ${percent}%, transparent)`;
/** A token's value, for what cannot read a CSS variable: the studio's WebGL. */
export const colour = (token: Token) => TOKENS[token];
const VARS = Object.fromEntries(Object.entries(TOKENS).map(([k, x]) => [`--r-${k}`, x]));

/** The page's side gutter, from the robot's strip down to the composer: 16 px on a phone or a tablet, 24 beside the
 *  robot's column (at 900 px, where the strip becomes the column). */
export const GUTTER = {xs: 2, md: 3} as const;

/** The robot speaks in Recursive's casual letters, people in its linear ones, a script in its monospace. */
export const CASUAL = '"CASL" 1, "MONO" 0';
export const MONO = '"MONO" 1, "CASL" 0';
const LINEAR = '"CASL" 0, "MONO" 0';
export const FONT = '"Recursive Variable", ui-sans-serif, system-ui, -apple-system, sans-serif';

const palette = (t: typeof TOKENS) => ({
    mode: 'dark' as const,
    background: {default: t.page, paper: t.raised},
    divider: t.line,
    primary: {main: t.lens, contrastText: t['on-lens']},
    secondary: {main: t.ghost, contrastText: t['on-lens']},
    success: {main: t.listen},
    warning: {main: t.attention},
    error: {main: t.fault},
    info: {main: t.sleep},
    text: {primary: t.ink, secondary: t['ink-2'], disabled: t['ink-3']},
    action: {hover: t.hover, selected: t.selected, disabledBackground: t.sunken, disabled: t['ink-3']}
});

const field = {
    borderRadius: 10,
    backgroundColor: v('raised'),
    '& .MuiOutlinedInput-notchedOutline': {borderColor: v('line')},
    '&:hover .MuiOutlinedInput-notchedOutline': {borderColor: v('line-strong')},
    '&.Mui-focused .MuiOutlinedInput-notchedOutline': {borderColor: v('ink'), borderWidth: 1.5},
    '&.Mui-disabled': {backgroundColor: v('sunken')},
    '&.Mui-disabled .MuiOutlinedInput-notchedOutline': {borderColor: v('line')}
};
const popup = {backgroundColor: v('raised'), border: `1px solid ${v('line')}`, boxShadow: v('shadow')};

export const theme = createTheme({
    palette: palette(TOKENS),
    shape: {borderRadius: 10},
    typography: {
        fontFamily: FONT,
        fontWeightRegular: 400,
        fontWeightMedium: 560,
        fontWeightBold: 680,
        h1: {fontSize: 48, fontWeight: 720, lineHeight: 1.1, letterSpacing: '-0.02em'},
        h2: {fontSize: 36, fontWeight: 700, lineHeight: 1.15, letterSpacing: '-0.015em'},
        h3: {fontSize: 24, fontWeight: 680, lineHeight: 1.25, letterSpacing: '-0.01em'},
        h4: {fontSize: 21, fontWeight: 680, lineHeight: 1.3, letterSpacing: '-0.005em'},
        h5: {fontSize: 18, fontWeight: 660, lineHeight: 1.35},
        h6: {fontSize: 16, fontWeight: 660, lineHeight: 1.4},
        subtitle1: {fontSize: 16, fontWeight: 560, lineHeight: 1.5},
        subtitle2: {fontSize: 14, fontWeight: 620, lineHeight: 1.45},
        body1: {fontSize: 16, lineHeight: 1.55},
        body2: {fontSize: 14, lineHeight: 1.5},
        caption: {fontSize: 12, lineHeight: 1.45, letterSpacing: 0},
        overline: {fontSize: 12, fontWeight: 620, lineHeight: 1.45, letterSpacing: 0, textTransform: 'none'},
        button: {fontSize: 14, fontWeight: 620, textTransform: 'none', letterSpacing: 0}
    },
    components: {
        MuiCssBaseline: {
            styleOverrides: {
                ':root': {...VARS, colorScheme: 'dark'},
                '*': {scrollbarWidth: 'thin', scrollbarColor: `${v('line-strong')} transparent`},
                body: {
                    backgroundColor: v('page'),
                    color: v('ink'),
                    fontVariationSettings: LINEAR,
                    WebkitFontSmoothing: 'antialiased',
                    MozOsxFontSmoothing: 'grayscale',
                    fontOpticalSizing: 'auto'
                },
                '::selection': {backgroundColor: tint('speak', 32)},
                '.MuiButtonBase-root.Mui-focusVisible, :focus-visible': {
                    outline: `2px solid ${v('focus')}`,
                    outlineOffset: 2
                },
                '.MuiInputBase-root .Mui-focusVisible, input:focus-visible, textarea:focus-visible': {outline: 'none'},
                kbd: {
                    fontFamily: FONT,
                    fontVariationSettings: MONO,
                    fontSize: 11,
                    lineHeight: '18px',
                    minWidth: 20,
                    padding: '0 5px',
                    display: 'inline-block',
                    textAlign: 'center',
                    borderRadius: 5,
                    color: v('ink-2'),
                    backgroundColor: v('sunken'),
                    border: `1px solid ${v('line')}`
                },
                '@media (prefers-reduced-motion: reduce)': {
                    '*, *::before, *::after': {
                        animationDuration: '0.01ms !important',
                        transitionDuration: '0.01ms !important'
                    }
                }
            }
        },
        MuiButtonBase: {defaultProps: {disableRipple: true}},
        MuiButton: {
            defaultProps: {disableElevation: true},
            styleOverrides: {
                root: {borderRadius: 10, minHeight: 36, padding: '6px 14px', whiteSpace: 'nowrap'},
                sizeSmall: {minHeight: 30, padding: '4px 10px', fontSize: 13},
                sizeLarge: {minHeight: 44, padding: '10px 18px', fontSize: 15},
                contained: {'&.Mui-disabled': {backgroundColor: v('sunken'), color: v('ink-3')}},
                containedPrimary: {'&:hover': {backgroundColor: v('ink-2')}},
                outlined: {borderColor: v('line-strong'), '&:hover': {backgroundColor: v('hover')}},
                outlinedInherit: {borderColor: v('line-strong')},
                text: {'&:hover': {backgroundColor: v('hover')}},
                startIcon: {marginRight: 6, '& > *:nth-of-type(1)': {fontSize: 18}}
            }
        },
        MuiIconButton: {
            styleOverrides: {
                root: {borderRadius: 10, color: v('ink-2'), '&:hover': {backgroundColor: v('hover'), color: v('ink')}}
            }
        },
        MuiOutlinedInput: {styleOverrides: {root: field, input: {'&::placeholder': {color: v('ink-3'), opacity: 1}}}},
        MuiInputLabel: {styleOverrides: {root: {color: v('ink-2'), '&.Mui-focused': {color: v('ink')}}}},
        MuiFormHelperText: {styleOverrides: {root: {marginLeft: 2, marginTop: 6, fontSize: 12, color: v('ink-2')}}},
        MuiSelect: {styleOverrides: {icon: {color: v('ink-2')}}},
        MuiSlider: {
            styleOverrides: {
                root: {height: 4, padding: '13px 0'},
                rail: {backgroundColor: v('line-strong'), opacity: 1},
                track: {border: 0},
                thumb: {
                    width: 16,
                    height: 16,
                    backgroundColor: v('raised'),
                    border: '2px solid currentColor',
                    boxShadow: 'none',
                    '&:hover, &.Mui-focusVisible': {boxShadow: `0 0 0 6px ${v('hover')}`},
                    '&.Mui-active': {boxShadow: `0 0 0 8px ${v('selected')}`},
                    '&::before': {boxShadow: 'none'}
                },
                valueLabel: {
                    fontSize: 12,
                    fontWeight: 600,
                    borderRadius: 6,
                    padding: '2px 6px',
                    backgroundColor: v('ink'),
                    color: v('page')
                }
            }
        },
        MuiSwitch: {
            styleOverrides: {
                switchBase: {'&.Mui-checked + .MuiSwitch-track': {backgroundColor: v('ink'), opacity: 1}},
                thumb: {boxShadow: 'none'},
                track: {backgroundColor: v('line-strong'), opacity: 1}
            }
        },
        MuiTabs: {
            styleOverrides: {
                root: {minHeight: 44},
                indicator: {height: 2, borderRadius: 2, backgroundColor: v('ink')}
            }
        },
        MuiTab: {
            styleOverrides: {
                root: {
                    minHeight: 44,
                    minWidth: 0,
                    padding: '10px 14px',
                    fontWeight: 600,
                    color: v('ink-2'),
                    '&.Mui-selected': {color: v('ink')}
                }
            }
        },
        MuiToggleButtonGroup: {
            styleOverrides: {
                root: {padding: 3, gap: 2, borderRadius: 10, backgroundColor: v('sunken')},
                grouped: {border: 0, borderRadius: '7px !important', margin: 0}
            }
        },
        MuiToggleButton: {
            styleOverrides: {
                root: {
                    border: 0,
                    padding: '4px 10px',
                    minHeight: 30,
                    fontSize: 13,
                    fontWeight: 600,
                    textTransform: 'none',
                    color: v('ink-2'),
                    '&:hover': {backgroundColor: v('hover'), color: v('ink')},
                    '&.Mui-selected, &.Mui-selected:hover': {
                        color: v('ink'),
                        backgroundColor: v('raised'),
                        boxShadow: `0 0 0 1px ${v('line')}, 0 1px 2px rgba(0, 0, 0, 0.08)`
                    }
                }
            }
        },
        MuiChip: {
            styleOverrides: {
                root: {borderRadius: 6, height: 24, fontSize: 12, fontWeight: 560},
                outlined: {borderColor: v('line-strong')},
                label: {paddingLeft: 8, paddingRight: 8},
                sizeSmall: {height: 22}
            }
        },
        MuiTooltip: {
            defaultProps: {disableInteractive: true},
            styleOverrides: {
                tooltip: {
                    fontSize: 12,
                    lineHeight: 1.45,
                    fontWeight: 500,
                    padding: '6px 10px',
                    borderRadius: 8,
                    backgroundColor: v('ink'),
                    color: v('page'),
                    maxWidth: 320
                }
            }
        },
        MuiPaper: {styleOverrides: {root: {backgroundImage: 'none'}}},
        MuiDialog: {
            styleOverrides: {
                paper: {...popup, borderRadius: 16},
                paperFullScreen: {borderRadius: 0, border: 0, boxShadow: 'none', backgroundColor: v('page')}
            }
        },
        MuiBackdrop: {
            styleOverrides: {root: {backgroundColor: 'rgba(16, 18, 21, 0.42)'}, invisible: {background: 'none'}}
        },
        MuiDialogTitle: {styleOverrides: {root: {fontSize: 18, fontWeight: 660, padding: '20px 24px 8px'}}},
        MuiDialogActions: {styleOverrides: {root: {padding: '12px 20px 18px'}}},
        MuiDrawer: {styleOverrides: {paper: {backgroundColor: v('page'), borderLeft: `1px solid ${v('line')}`}}},
        MuiPopover: {styleOverrides: {paper: {...popup, borderRadius: 12}}},
        MuiMenu: {styleOverrides: {paper: {...popup, borderRadius: 12}, list: {padding: 6}}},
        MuiMenuItem: {styleOverrides: {root: {borderRadius: 8, fontSize: 14, minHeight: 36}}},
        MuiListSubheader: {
            styleOverrides: {root: {fontSize: 12, fontWeight: 620, lineHeight: '30px', color: v('ink-2')}}
        },
        MuiListItemButton: {
            styleOverrides: {
                root: {
                    borderRadius: 8,
                    // A lit row is brighter than its surface: its faintest text takes the second ink to stay readable.
                    '&:hover': {backgroundColor: v('hover'), '--r-ink-3': v('ink-2')},
                    '&.Mui-selected, &.Mui-selected:hover': {backgroundColor: v('selected'), '--r-ink-3': v('ink-2')}
                }
            }
        },
        MuiListItemText: {
            styleOverrides: {
                primary: {fontSize: 14, fontWeight: 560},
                secondary: {fontSize: 12, color: v('ink-2')}
            }
        },
        MuiAlert: {
            styleOverrides: {
                root: {borderRadius: 10, fontSize: 14, alignItems: 'flex-start', padding: '4px 12px'},
                icon: {padding: '8px 0', marginRight: 10},
                message: {padding: '8px 0'}
            }
        },
        MuiLinearProgress: {
            styleOverrides: {
                root: {height: 3, borderRadius: 2, backgroundColor: v('line')},
                bar: {borderRadius: 2, backgroundColor: v('ink')}
            }
        },
        MuiCircularProgress: {defaultProps: {color: 'inherit'}},
        MuiSnackbarContent: {
            styleOverrides: {
                root: {backgroundColor: v('ink'), color: v('page'), borderRadius: 10, fontSize: 14, fontWeight: 500}
            }
        },
        MuiDivider: {styleOverrides: {root: {borderColor: v('line')}}}
    }
});
