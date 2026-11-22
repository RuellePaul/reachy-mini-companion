"""settings.json: {"settings": {...}, "volume": n}. Reads never fail; writes are atomic, change only what they set,
and never replace a file they could not read."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mac_server.contracts import Settings, SettingsPatch, Volume, merge_settings

log = logging.getLogger(__name__)

DEFAULT_VOLUME = 100


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def write_text_atomic(path: Path, text: str) -> None:
    """Temp file in the same directory, then rename: a reader sees the old file or the new one.

    mkstemp creates the file with mode 0600, which the rename keeps.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def parse_settings(data: object) -> tuple[Settings, list[str]]:
    """Settings from stored data, and the fields that fell back to their default (refused or unknown)."""
    if not isinstance(data, dict):
        return Settings(), ['settings']
    try:
        return Settings.model_validate(data), []
    except ValidationError as exc:
        bad = sorted({str(e['loc'][0]) for e in exc.errors() if e['loc']})
        try:
            return Settings.model_validate({k: v for k, v in data.items() if k not in bad}), bad
        except ValidationError:
            return Settings(), sorted(map(str, data))


class Unreadable(Exception):
    """settings.json (the message: its path) is there but is not {"settings": {...}, ...}: writing it would replace
    what was not read. Answered 500 in French (server.py)."""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _raw(self, *, writing: bool = False) -> dict[str, Any]:
        """The file as stored, {} when there is none. One that cannot be read reads as {}; `writing` then raises
        Unreadable."""
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raw, why = None, str(exc)
        else:
            why = 'not {"settings": {...}, "volume": n}'
        if isinstance(raw, dict) and isinstance(raw.get('settings', {}), dict):
            return raw
        log.warning('%s is unreadable (%s), %s', self.path, why, 'nothing written' if writing else 'using the defaults')
        if writing:
            raise Unreadable(self.path)
        return {}

    def _settings(self, raw: dict[str, Any]) -> tuple[Settings, list[str]]:
        settings, defaulted = parse_settings(raw.get('settings', {}))
        if defaulted:
            log.warning('%s: invalid %s, using the defaults', self.path, ', '.join(defaulted))
        return settings, defaulted

    def _volume(self, raw: dict[str, Any]) -> int:
        try:
            return Volume(volume=raw.get('volume', DEFAULT_VOLUME)).volume
        except ValidationError:
            log.warning('%s: invalid volume, using %d', self.path, DEFAULT_VOLUME)
            return DEFAULT_VOLUME

    def _write(self, raw: dict[str, Any], **changes: object) -> None:
        """`raw` with `changes` made, every other key kept as it was; a file there was not gets both keys."""
        write_json_atomic(self.path, {'settings': Settings().model_dump(), 'volume': DEFAULT_VOLUME} | raw | changes)

    def settings(self) -> Settings:
        with self._lock:
            return self._settings(self._raw())[0]

    def volume(self) -> int:
        with self._lock:
            return self._volume(self._raw())

    def update(self, patch: SettingsPatch) -> Settings:
        """Merge the changed fields into the stored settings; a stored value the rules refused stays as it is unless
        the patch sets it. Raises ValidationError or Unreadable, writing nothing."""
        with self._lock:
            raw = self._raw(writing=True)
            current, refused = self._settings(raw)
            settings = merge_settings(current, patch)
            stored = raw.get('settings', {})
            kept = {k: stored[k] for k in refused if k in stored and k not in patch.model_fields_set}
            self._write(raw, settings=settings.model_dump() | kept)
        log.info('settings changed: %s', ', '.join(sorted(patch.model_dump(exclude_unset=True))))
        return settings

    def set_volume(self, volume: int) -> None:
        """Raises Unreadable, writing nothing."""
        with self._lock:
            self._write(self._raw(writing=True), volume=volume)
