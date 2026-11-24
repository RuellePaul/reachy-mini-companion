"""Constants and environment of the robot agent. `__main__` loads the repo-root .env before this is imported."""

from __future__ import annotations

import ipaddress
import os
from typing import Final
from urllib.parse import urlsplit


def _flag(name: str, default: bool) -> bool:
    """An on/off switch: unset means `default`; 0, false, no and off (any case) mean off."""
    value = os.environ.get(name, '').strip().lower()
    return default if not value else value not in ('0', 'false', 'no', 'off')


# ----- Environment -----
GEMINI_API_KEY: Final = os.environ.get('GEMINI_API_KEY', '').strip()


def _mac_urls() -> tuple[str, ...]:
    """Where the Mac may answer, best first: the address this agent's SSH came from, the Mac's addresses when it was
    launched (`MAC_AGENT_HOSTS`, from make up and make run), then MAC_AGENT_URL's (one or several, comma-separated).
    IPs, never mDNS names; IPv6 link-local ones need a zone: skipped."""
    fixed = [url.strip().rstrip('/') for url in os.environ.get('MAC_AGENT_URL', '').split(',') if url.strip()]
    try:
        port = urlsplit(fixed[0]).port if fixed else None
    except ValueError:
        port = None
    port = port or int(os.environ.get('MAC_AGENT_PORT', '').strip() or 7777)
    hosts = [*os.environ.get('SSH_CLIENT', '').split()[:1], *os.environ.get('MAC_AGENT_HOSTS', '').split()]
    found = [f'http://{host}:{port}' for host in hosts if _ipv4(host)]
    return tuple(dict.fromkeys([*found, *fixed]))


def _ipv4(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address) and not host.startswith(
            ('127.', '169.254.')
        )
    except ValueError:
        return False


MAC_AGENT_URLS: Final = _mac_urls()
MAC_AGENT_API_KEY: Final = os.environ.get('MAC_AGENT_API_KEY', '').strip()
DAEMON_URL: Final = 'http://localhost:8000'  # the Reachy daemon runs on the robot, next to this agent

# ----- Gemini Live -----
MODEL: Final = 'gemini-3.8-live'
GEMINI_INPUT_SR: Final = 16000
GEMINI_OUTPUT_SR: Final = 24000
# Server VAD, the only judge of turns.
VAD_SILENCE_MS: Final = 800  # 100-200 ms splits one sentence into several turns
PREFIX_PADDING_MS: Final = 800  # less clips the first words, worst during barge-in
VAD_START_SENSITIVITY: Final = 'START_SENSITIVITY_HIGH'
VAD_END_SENSITIVITY: Final = 'END_SENSITIVITY_LOW'
# Every turn re-bills the whole context: compact it early (compression needs trigger_tokens).
COMPRESSION_TRIGGER_TOKENS: Final = 20_000
# The input transcription (what the dashboard shows and the tool guard sees): French, with these words biased.
TRANSCRIPTION_LANGUAGE: Final = 'fr-FR'
TRANSCRIPTION_VOCABULARY: Final = ('Reachy', 'Pollen Robotics', 'endors-toi', 'rendors-toi', 'Spotify')
COMPRESSION_TARGET_TOKENS: Final = 8_000

# ----- Conversation -----
HUMAN_GRACE_S: Final = 10.0  # a tool runs only if a human spoke or typed this recently
REPLY_TIMEOUT_S: Final = 6.0  # thinking with no reply: proactive audio may decline to answer
WATCHDOG_TICK_S: Final = 0.1
RECONNECT_BACKOFF_S: Final = (0.5, 2.0, 5.0)  # one wait per attempt; more failures end the conversation
RECONNECT_PROVEN_S: Final = 30.0  # a connection alive this long (or with a completed turn) resets the budget
GO_AWAY_URGENT_S: Final = 5.0  # with less time left, reconnect at once rather than at a quiet point
CONNECT_TIMEOUT_S: Final = 10.0  # a setup never completed by then is a failed attempt
CLOSE_TIMEOUT_S: Final = 2.0
LIVE_WAIT_S: Final = 5.0  # how long a `look` image or a note waits for a (re)connection; a typed say waits it out

# ----- Audio -----
# While the robot is audible (its audio, its wake move, a gesture, an emotion or a body turn, each then
# SELF_NOISE_TAIL_S), Gemini hears ComfortNoise instead of the microphone: its motors are as loud as a voice in
# its own microphone and are not in the echo canceller's reference. The
# trade: no barge-in while the robot talks or moves. MIC_MUTE_WHILE_TALKING=0 streams the raw microphone,
# barge-in and motor noise included.
MIC_MUTE_WHILE_TALKING: Final = _flag('MIC_MUTE_WHILE_TALKING', default=True)
SELF_NOISE_TAIL_S: Final = 1.0  # the talking-to-rest blend is the loudest moment
SPEAKER_DELAY_S: Final = 0.2  # a chunk pushed plays this late
MIC_LEVEL_LOG: Final = _flag('MIC_LEVEL_LOG', default=False)  # log the raw mic level every 2 s (diagnostic)
MIC_RETRY_S: Final = 0.01
# Drain the backlog until a pull costs time.
MIC_DRAIN_BUDGET_S: Final = 0.25
MIC_DRAIN_CAUGHT_UP_S: Final = 0.008
MIC_DRAIN_MAX_CHUNKS: Final = 400
MIC_APPSINK_BUFFERS: Final = 200  # reachy_mini's appsink keeps the newest 200 chunks (max-buffers, drop)
ERROR_LOG_EVERY_S: Final = 5.0  # rate limit of the logs of a failure that repeats

# ----- Motion -----
MOTION_HZ: Final = 30
WAKE_S: Final = 1.0  # sleep pose to animation: the antennas swing ~2.9 rad, under the 8 rad/s joint limit
SLEEP_S: Final = 1.0
SLEEP_SETTLE_S: Final = 1.0  # or the settling reads as an antenna wiggle
BODY_YAW_LIMIT_DEG: Final = 100.0  # with the head's own yaw, the world yaw stays clear of ±180°
BODY_TURN_MIN_S: Final = 1.5  # the body motor overloads on fast turns
BODY_TURN_SPEED_DEG_S: Final = 90.0
SCENE_TURN_SPEED: Final = 1.2  # a scene turns 20 % faster: the owner's call, watched against the overload above
ANTENNA_POLL_HZ: Final = 30
ANTENNA_WAKE_RAD: Final = 0.04  # a flick under torque deflects an antenna 5-6°

# ----- Mac -----
SETTINGS_TIMEOUT_S: Final = 2.0  # at wake, no retry: a dead Mac sends the robot back to sleep
MAC_PROBE_TIMEOUT_S: Final = 0.5  # a TCP connect to one of the Mac's addresses: tens of ms on a LAN
MAC_TIMEOUT_S: Final = 10.0
EVENT_TIMEOUT_S: Final = 2.0
EVENT_QUEUE_MAX: Final = 500
HEARTBEAT_S: Final = 3.0
COMMAND_WAIT_S: Final = 20
COMMAND_RETRY_S: Final = 2.0

# ----- Emotions and camera -----
EMOTIONS_RETRY_S: Final = (30.0, 600.0)  # first and longest wait between two loads of the library
LOOK_WAIT_S: Final = 0.6  # frames come every 33 ms and a pull waits 20: retry to a deadline
LOOK_MAX_EDGE: Final = 768
LOOK_JPEG_QUALITY: Final = 80
