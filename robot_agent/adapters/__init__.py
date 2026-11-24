"""What the session and the tools need from the hardware. `adapters.robot` implements it on a Reachy Mini and
tests implement it with fakes, so neither the session nor the tools import `reachy_mini`."""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np


class Audio(Protocol):
    """Microphone in, speaker out. Both pipelines are opened once at boot and stay up between sessions."""

    def get_input_samplerate(self) -> int: ...
    def get_output_samplerate(self) -> int: ...
    def get_audio_sample(self) -> np.ndarray | None: ...  # blocks ~16 ms on a running mic: call it in a thread
    def push_audio_sample(self, samples: np.ndarray) -> None: ...  # float32 mono, never empty
    def clear_output(self) -> None: ...  # drop what is queued for the speaker
    def play_sound(self, path: str) -> None: ...  # blocking, like stop_sound: call them in a thread
    def stop_sound(self) -> None: ...  # cut what play_sound started


class Body(Protocol):
    """The motion thread (`motion.MotionRunner`). Every call returns at once."""

    def set_mode(self, mode: str, blend_s: float = ...) -> None: ...
    def turn_body(self, degrees: float, speed: float = 1.0) -> float: ...  # returns the clamped target
    def gesture(self, name: str) -> bool: ...  # False for an unknown gesture
    def play_move(self, move: Any) -> None: ...  # a recorded move: .duration, .evaluate(t)
    def stop_moves(self) -> None: ...
    def moving_until(self) -> float: ...  # when the move and the body turn under way end (monotonic)


class Camera(Protocol):
    def read_jpeg(self) -> bytes | None: ...  # blocking; None when there is no picture
