"""The Mac server's environment, read from the repo-root .env (variables already set win)."""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / '.env'  # the repo-root .env, the one the robot is sent
STATE_DIR = Path.home() / '.reachy'
CACHE_DIR = Path.home() / '.cache' / 'reachy'  # scene voices and emotion moves: re-fetchable
DEFAULT_ORIGINS = ('http://localhost:5077', 'http://127.0.0.1:5077')


def _computer_name() -> str:
    """What Spotify calls this Mac's own app: the macOS computer name, else the host name."""
    try:
        ran = subprocess.run(
            ['scutil', '--get', 'ComputerName'], capture_output=True, text=True, timeout=2, check=False
        )
        name = ran.stdout.strip() if ran.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        name = ''
    return name or socket.gethostname().removesuffix('.local')


@dataclass(frozen=True)
class Config:
    api_key: str = ''
    port: int = 7777
    cors_origins: tuple[str, ...] = DEFAULT_ORIGINS
    reachy_host: str = 'reachy-mini.local'
    reachy_port: int = 8000
    settings_path: Path = STATE_DIR / 'settings.json'
    spotify_token_path: Path = STATE_DIR / 'spotify.json'
    spotify_client_id: str = ''
    spotify_client_secret: str = ''
    spotify_redirect_uri: str = 'http://127.0.0.1:7777/api/spotify/callback'
    spotify_refresh_token: str = ''  # initial value only: a rotated token lives in spotify_token_path
    spotify_device_name: str = field(default_factory=_computer_name)
    gemini_api_key: str = ''  # scene voices (TTS)
    scenes_path: Path = STATE_DIR / 'scenes.json'  # the user's scenes; the built-ins ship in mac_server/assets
    apps_dir: Path = STATE_DIR / 'apps'  # one <id>.html per app, and index.json
    cache_dir: Path = CACHE_DIR


def load_config() -> Config:
    load_dotenv(ENV_PATH)
    env = os.environ.get
    origins = env('CORS_ORIGINS')
    state = Path(env('REACHY_STATE_DIR') or STATE_DIR).expanduser()  # scenes and apps (a test server uses its own)
    return Config(
        api_key=env('MAC_AGENT_API_KEY', ''),
        port=int(env('MAC_AGENT_PORT') or 7777),
        cors_origins=tuple(o.strip() for o in origins.split(',') if o.strip()) if origins else DEFAULT_ORIGINS,
        reachy_host=env('REACHY_HOST') or 'reachy-mini.local',
        reachy_port=int(env('REACHY_PORT') or 8000),
        settings_path=Path(env('SETTINGS_PATH') or STATE_DIR / 'settings.json').expanduser(),
        spotify_client_id=env('SPOTIFY_CLIENT_ID', ''),
        spotify_client_secret=env('SPOTIFY_CLIENT_SECRET', ''),
        spotify_redirect_uri=env('SPOTIFY_REDIRECT_URI') or Config.spotify_redirect_uri,
        spotify_refresh_token=env('SPOTIFY_REFRESH_TOKEN', ''),
        spotify_device_name=env('SPOTIFY_DEVICE_NAME') or _computer_name(),
        gemini_api_key=env('GEMINI_API_KEY', ''),
        scenes_path=state / 'scenes.json',
        apps_dir=state / 'apps',
        cache_dir=Path(env('REACHY_CACHE_DIR') or CACHE_DIR).expanduser(),
    )
