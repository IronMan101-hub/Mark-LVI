"""Where GALAHAD keeps its files. `GALAHAD_HOME` overrides everything (tests)."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def home() -> Path:
    env = os.environ.get("GALAHAD_HOME")
    base = Path(env) if env else project_root() / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return home() / "galahad.db"


def log_dir() -> Path:
    d = home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def exports_dir() -> Path:
    d = home() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_plugins_dir() -> Path:
    d = home() / "user_plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d
