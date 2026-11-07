import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GALAHAD_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture()
def engine(home):
    from galahad.engine import Engine
    e = Engine(load_plugins=False)
    yield e
    e.stop()


@pytest.fixture()
def full_engine(home):
    """Engine with every built-in plugin loaded."""
    from galahad.engine import Engine
    e = Engine(load_plugins=True)
    e.start()
    yield e
    e.stop()


def call(e, plugin, cmd, **args):
    return e.registry.execute(plugin, cmd, args)
