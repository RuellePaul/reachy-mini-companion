"""The mode animations (exactly the variants of `animation_names.VARIANTS`) and the gestures.

A variant is a pure function t -> channels: head pitch, yaw, roll (degrees, +pitch looks down, +yaw left),
x, z (metres) and the right and left antennas (degrees forward off neutral). `animate` scales them by the
settings' amplitude and speed and builds the pose the motion thread sends. Everything stays slow: the robot's
motors are as loud as a voice in its own microphone (and not in the echo canceller's reference),
so the talking variants move the head 3° at most, antennas still; `wobble.py` adds Pollen's wobble and
breathing over them, only while the comfort noise covers the microphone. The dashboard's 3D preview samples this
same code on the Mac: nothing here may be copied elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy.spatial.transform import Rotation

Pose = tuple[np.ndarray, np.ndarray]
Channels = np.ndarray  # [pitch, yaw, roll, x, z, right, left]
TAU = 2.0 * np.pi
# Neutral antennas are not (0, 0). The joints are mirrored: (R + a, L - a) moves both forward alike.
NEUTRAL_R, NEUTRAL_L = -0.1745, 0.1745
NEUTRAL = np.array([NEUTRAL_R, NEUTRAL_L])


def pose(pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0, x: float = 0.0, z: float = 0.0) -> np.ndarray:
    """A head pose as the SDK's create_head_pose builds it: degrees (+pitch looks down, +yaw left), metres."""
    out = np.eye(4)
    out[:3, :3] = Rotation.from_euler('xyz', [roll, pitch, yaw], degrees=True).as_matrix()
    out[:3, 3] = (x, 0.0, z)
    return out


def antennas(forward: float = 0.0, right: float = 0.0, left: float = 0.0) -> np.ndarray:
    """Antennas off neutral, in degrees: `forward` moves both (negative droops them), `right`/`left` one."""
    return np.array([NEUTRAL_R + np.radians(forward + right), NEUTRAL_L - np.radians(forward + left)])


def ch(
    pitch: float = 0.0,
    yaw: float = 0.0,
    roll: float = 0.0,
    x: float = 0.0,
    z: float = 0.0,
    forward: float = 0.0,
    right: float = 0.0,
    left: float = 0.0,
) -> Channels:
    return np.array([pitch, yaw, roll, x, z, forward + right, forward + left])


def to_pose(c: Channels) -> Pose:
    return pose(c[0], c[1], c[2], c[3], c[4]), antennas(right=c[5], left=c[6])


@dataclass(frozen=True)
class Tuning:
    """The settings' factors for one mode (animation_names.AMPLITUDE_RANGE, SPEED_RANGE)."""

    amplitude: float = 1.0
    speed: float = 1.0


def animate(mode: str, key: str, tuning: Tuning = Tuning()) -> Callable[[float], Pose]:
    """The pose at t of a mode's variant, as the robot plays it with these settings."""
    return lambda t: to_pose(channels(mode, key, tuning, t))


def channels(mode: str, key: str, tuning: Tuning, t: float) -> Channels:
    return tuning.amplitude * VARIANTS[mode][key](tuning.speed * t)


def smoothstep(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3.0 - 2.0 * x)


def _wave(amplitude: float, hz: float, t: float, phase: float = 0.0) -> float:
    return amplitude * float(np.sin(TAU * hz * t + phase))


def _swing(t: float, period: float) -> float:
    """-1 .. 1, dwelling at each end: a gaze that rests on one side, then crosses to the other."""
    return float(np.tanh(3.0 * np.sin(TAU * t / period)) / np.tanh(3.0))


def _bump(t: float, period: float, width: float) -> float:
    """0 .. 1 once per period, a smooth bump `width` seconds long."""
    phase = (t % period) / width
    return float(np.sin(np.pi * phase) ** 2) if phase < 1.0 else 0.0


def _still(t: float) -> Channels:
    return ch()


# Idle: waiting to be spoken to. Slow drifts, antennas under 3°/s.
def _idle_calm(t: float) -> Channels:
    return ch(pitch=_wave(1.0, 0.2, t, 0.5), z=_wave(0.003, 0.2, t), forward=_wave(3.0, 0.15, t))


def _idle_curious(t: float) -> Channels:
    head = {'pitch': _wave(3.0, 0.05, t, 1.0), 'yaw': _wave(10.0, 0.07, t), 'roll': _wave(3.0, 0.06, t)}
    return ch(**head, right=_wave(4.0, 0.1, t), left=_wave(2.0, 0.08, t, 1.0))


def _idle_dreamy(t: float) -> Channels:
    head = {'pitch': -4.0 + _wave(3.0, 0.05, t), 'yaw': _wave(6.0, 0.04, t), 'roll': _wave(4.0, 0.045, t, 0.7)}
    return ch(**head, z=0.004, forward=-6.0 + _wave(2.0, 0.07, t))


def _idle_breathing(t: float) -> Channels:
    return ch(pitch=_wave(1.5, 0.12, t), z=_wave(0.004, 0.12, t, -np.pi / 2), forward=_wave(2.0, 0.12, t, np.pi))


def _idle_sleepy(t: float) -> Channels:
    head = {'pitch': 7.0 + _wave(3.0, 0.04, t), 'roll': _wave(4.0, 0.03, t), 'z': -0.006 + _wave(0.002, 0.04, t)}
    return ch(**head, forward=-15.0 + _wave(3.0, 0.04, t))


def _idle_observer(t: float) -> Channels:
    side = _swing(t, 12.0)
    return ch(pitch=2.0 + _wave(1.0, 0.1, t), yaw=12.0 * side, right=2.0 * side, left=-2.0 * side)


def _idle_playful(t: float) -> Channels:
    head = {'roll': _wave(5.0, 0.1, t), 'yaw': _wave(6.0, 0.07, t, 1.0)}
    return ch(**head, right=_wave(6.0, 0.06, t), left=_wave(6.0, 0.06, t, np.pi))


# Listening: someone is talking. The antennas hold still and the head barely moves.
def _listening_attentive(t: float) -> Channels:
    return ch(pitch=3.0 + _wave(0.8, 0.3, t), yaw=_wave(1.5, 0.2, t), z=0.003, forward=5.0)


def _listening_curious(t: float) -> Channels:
    return ch(pitch=2.0, yaw=-3.0 + _wave(1.0, 0.2, t), roll=8.0 + _wave(1.0, 0.25, t), right=8.0, left=4.0)


def _listening_patient(t: float) -> Channels:
    nod = 0.5 + 0.5 * float(np.cos(TAU * 0.2 * t))
    return ch(pitch=4.0 + 2.0 * nod, z=-0.002 - 0.003 * nod, forward=-8.0)


def _listening_nodding(t: float) -> Channels:
    nod = _bump(t, 3.0, 0.8)
    return ch(pitch=2.0 + 4.0 * nod, z=-0.002 * nod, forward=5.0)


def _listening_tilted(t: float) -> Channels:
    head = {'pitch': 1.0, 'yaw': 4.0 + _wave(1.0, 0.12, t), 'roll': -8.5 + _wave(1.2, 0.15, t)}
    return ch(**head, right=2.0, left=10.0)


def _listening_focused(t: float) -> Channels:
    return ch(pitch=5.0 + _wave(0.6, 0.25, t), x=0.008, z=-0.003, forward=10.0)


# Talking: 3° of head at most and still antennas, since the robot hears its own motors.
def _talking_calm(t: float) -> Channels:
    return ch(pitch=_wave(1.5, 0.9, t), yaw=_wave(2.0, 0.4, t), z=_wave(0.0015, 0.9, t, 1.0))


def _talking_nod(t: float) -> Channels:
    nod = 0.5 - 0.5 * float(np.cos(TAU * 0.6 * t))
    return ch(pitch=2.5 * nod, yaw=_wave(1.0, 0.25, t), z=-0.0015 * nod)


def _talking_lean(t: float) -> Channels:
    return ch(pitch=2.0 + _wave(1.0, 0.5, t), yaw=_wave(1.5, 0.3, t), x=0.006, forward=4.0)


def _talking_expressive(t: float) -> Channels:
    return ch(pitch=_wave(1.8, 0.8, t), yaw=_wave(1.5, 0.5, t, 0.3), roll=_wave(1.2, 0.35, t, 1.1))


def _talking_thoughtful(t: float) -> Channels:
    return ch(pitch=-1.2 + _wave(0.8, 0.3, t), yaw=_wave(1.8, 0.15, t), roll=1.0)


VARIANTS: dict[str, dict[str, Callable[[float], Channels]]] = {
    'idle': {
        'calm': _idle_calm,
        'curious': _idle_curious,
        'dreamy': _idle_dreamy,
        'breathing': _idle_breathing,
        'sleepy': _idle_sleepy,
        'observer': _idle_observer,
        'playful': _idle_playful,
        'still': _still,
    },
    'listening': {
        'attentive': _listening_attentive,
        'curious': _listening_curious,
        'patient': _listening_patient,
        'nodding': _listening_nodding,
        'tilted': _listening_tilted,
        'focused': _listening_focused,
        'still': _still,
    },
    'talking': {
        'calm': _talking_calm,
        'nod': _talking_nod,
        'lean': _talking_lean,
        'expressive': _talking_expressive,
        'thoughtful': _talking_thoughtful,
        'still': _still,
    },
}


@dataclass(frozen=True)
class Gesture:
    """A short move from keyframes, eased between them: (seconds, pitch, yaw, roll[, z mm, right, left]), angles in
    degrees, the antennas forward off neutral. Missing trailing values are 0."""

    keys: tuple[tuple[float, ...], ...]

    @property
    def duration(self) -> float:
        return self.keys[-1][0]

    def evaluate(self, t: float) -> Pose:
        for k0, k1 in zip(self.keys, self.keys[1:], strict=False):
            if t <= k1[0]:
                s = smoothstep((t - k0[0]) / (k1[0] - k0[0]))
                a, b = _padded(k0), _padded(k1)
                return _key_pose(a + (b - a) * s)
        return pose(), NEUTRAL


def _padded(key: tuple[float, ...]) -> np.ndarray:
    return np.array([*key[1:], *[0.0] * (7 - len(key))])


def _key_pose(v: np.ndarray) -> Pose:
    return to_pose(ch(pitch=v[0], yaw=v[1], roll=v[2], z=v[3] / 1000.0, right=v[4], left=v[5]))


def _dance() -> Gesture:
    """Four beats of sway: the head rolls against its yaw, the antennas take turns, a small bob on each beat."""
    keys: list[tuple[float, ...]] = [(0.0, 0, 0, 0)]
    for beat in range(8):
        side = 1 if beat % 2 == 0 else -1
        keys.append((0.25 + 0.5 * beat, 2, 12 * side, -8 * side, -4, 30 * side, -30 * side))
    keys.append((4.5, 0, 0, 0))
    return Gesture(tuple(keys))


GESTURES: dict[str, Gesture] = {
    'nod': Gesture(((0, 0, 0, 0), (0.25, 10, 0, 0), (0.5, -4, 0, 0), (0.75, 8, 0, 0), (1.0, 0, 0, 0))),
    'shake': Gesture(((0, 0, 0, 0), (0.25, 0, 15, 0), (0.6, 0, -15, 0), (0.9, 0, 10, 0), (1.15, 0, 0, 0))),
    'tilt_left': Gesture(((0, 0, 0, 0), (0.45, 0, 0, 15), (1.1, 0, 0, 15), (1.5, 0, 0, 0))),
    'tilt_right': Gesture(((0, 0, 0, 0), (0.45, 0, 0, -15), (1.1, 0, 0, -15), (1.5, 0, 0, 0))),
    'look_up': Gesture(((0, 0, 0, 0), (0.45, -20, 0, 0), (1.1, -20, 0, 0), (1.5, 0, 0, 0))),
    'look_down': Gesture(((0, 0, 0, 0), (0.45, 20, 0, 0), (1.1, 20, 0, 0), (1.5, 0, 0, 0))),
    'wiggle': Gesture(
        (
            (0, 0, 0, 0),
            (0.25, 0, 0, 4, 0, 25, -25),
            (0.55, 0, 0, -4, 0, -25, 25),
            (0.85, 0, 0, 4, 0, 25, -25),
            (1.15, 0, 0, -4, 0, -25, 25),
            (1.6, 0, 0, 0),
        )
    ),
    'bow': Gesture(((0, 0, 0, 0), (0.6, 18, 0, 0, -8, -20, -20), (1.3, 18, 0, 0, -8, -20, -20), (2.0, 0, 0, 0))),
    'think': Gesture(((0, 0, 0, 0), (0.5, -10, 12, 6, 0, 15, -5), (1.7, -10, 12, 6, 0, 15, -5), (2.2, 0, 0, 0))),
    'surprise': Gesture(((0, 0, 0, 0), (0.25, -8, 0, 0, 8, 30, 30), (0.85, -8, 0, 0, 8, 30, 30), (1.4, 0, 0, 0))),
    'dance': _dance(),
}
