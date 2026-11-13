"""Test setup: make the app modules importable without a container.

The app code lives in app/ and is imported as top-level modules (that is how the
container runs it). webapp.py creates /data/config at import time, which does not
exist on a developer machine, so the import is done with mkdir under /data skipped.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

_real_mkdir = Path.mkdir


def _mkdir_outside_data(self, *args, **kwargs):
    if str(self).startswith("/data"):
        return None
    return _real_mkdir(self, *args, **kwargs)


@pytest.fixture(scope="session")
def webapp_module():
    with mock.patch.object(Path, "mkdir", _mkdir_outside_data):
        import webapp
    return webapp


@pytest.fixture
def plugin():
    import vibecodeplugin
    return vibecodeplugin
