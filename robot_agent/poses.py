"""The lent body's pose, sent to the Mac as datagrams: one whole measure each, HZ a second while an app holds the
body. The daemon's state stream goes over TCP, which waits for the Mac's acknowledgements: on a weak radio the box is
at times a second late handing them to the robot, and the stream stops though every measure could still leave.
A datagram waits for nothing, and one lost is replaced by the next. The Mac
reads the layout here: dependency-free."""

from __future__ import annotations

import hashlib
import hmac
import struct
from collections.abc import Sequence

HZ = 30.0  # the pace of the daemon's stream, which the Mac reads when no datagram comes (mac_server/studio.py)
TAG = 8  # bytes: the measure's hash under the API key, first: a stray datagram is no pose
_LAYOUT = struct.Struct('<d25f')  # the robot's clock (s), the 7 head joints, the head (4x4, row-major), 2 antennas
SIZE = TAG + _LAYOUT.size

Measure = tuple[float, list[float], list[float], list[float]]  # at, joints, head, antennas


def _tag(key: str, body: bytes) -> bytes:
    return hashlib.blake2b(body, key=key.encode()[:64], digest_size=TAG).digest()


def pack(key: str, at: float, joints: Sequence[float], head: Sequence[float], antennas: Sequence[float]) -> bytes:
    """One measure as a datagram: `joints` the base's yaw then the platform's six motors (rad), `head` its pose in
    the world (16 values, row-major), `antennas` [right, left] (rad), `at` on the robot's clock (s)."""
    body = _LAYOUT.pack(at, *joints, *head, *antennas)
    return _tag(key, body) + body


def unpack(key: str, data: bytes) -> Measure | None:
    """The measure a datagram carries; None unless it is whole and was packed under this key."""
    if len(data) != SIZE or not hmac.compare_digest(data[:TAG], _tag(key, data[TAG:])):
        return None
    at, *values = _LAYOUT.unpack(data[TAG:])
    return at, values[:7], values[7:23], values[23:]
