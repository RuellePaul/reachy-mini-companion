"""Loaded before any test module: the suite runs on the robot's defaults, whatever the shell exported."""

import os
from pathlib import Path

import pytest

# Read at import into config, and bound as the session's default: a shell left from a hardware test
# would turn the stand-in off for every session the robot opens.
os.environ.pop('MIC_MUTE_WHILE_TALKING', None)


@pytest.fixture(autouse=True)
def no_shipped_apps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The apps shipped with the robot are listed only for the tests that ask for them: an empty store is empty."""
    from mac_server import apps  # noqa: PLC0415

    monkeypatch.setattr(apps, 'ASSETS', tmp_path / 'no-shipped-apps')
