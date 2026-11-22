"""The antenna wake: a deflection from where the antennas were when armed fires once."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from robot_agent.antennas import AntennaWiggle


def test_a_deflection_fires_once_and_only_while_armed() -> None:
    wiggle = AntennaWiggle(SimpleNamespace(), on_wiggle=lambda: None)
    assert not wiggle.check((-3.05, 3.05))  # disarmed
    wiggle.arm()
    assert not wiggle.check((-3.05, 3.05))  # the baseline
    assert not wiggle.check((-3.03, 3.06))  # settling: under 0.04 rad
    assert wiggle.check((-3.05, 3.15))  # a flick on the left one
    assert not wiggle.check((-3.05, 3.25))  # fired: disarmed until armed again
    wiggle.arm()
    assert not wiggle.check((-3.05, 3.25))
    assert wiggle.check((-2.95, 3.25))


def test_the_thread_calls_back_and_survives_failed_reads() -> None:
    reads = iter([OSError('daemon restarting'), (0.0, 0.0), (0.0, 0.0), (0.3, 0.0)])
    fired = threading.Event()

    def read() -> tuple[float, float]:
        value = next(reads, (0.3, 0.0))
        if isinstance(value, Exception):
            raise value
        return value

    wiggle = AntennaWiggle(SimpleNamespace(get_present_antenna_joint_positions=read), on_wiggle=fired.set)
    wiggle.arm()
    wiggle.start()
    assert fired.wait(2.0)
    wiggle.stop()


def test_an_antenna_coming_back_to_its_command_never_fires() -> None:
    """Armed while a hand still holds an antenna, its release (the motor bringing it back) wakes nothing; a push off
    the command from there does."""
    command = (-3.05, 3.05)
    wiggle = AntennaWiggle(SimpleNamespace(), on_wiggle=lambda: None, commanded=lambda: command)
    wiggle.arm()
    assert not wiggle.check((1.5, -1.5))  # the baseline: still held
    assert not wiggle.check((-1.0, 1.0))  # let go: on its way back
    assert not wiggle.check((-3.06, 3.05))  # past its command by less than 0.04 rad
    assert not wiggle.check(command)  # at rest
    assert wiggle.check((-3.05, 3.15))  # a flick
