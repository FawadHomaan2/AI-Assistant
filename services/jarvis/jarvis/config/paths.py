"""Where Jarvis keeps its data.

Windows is the shipping target, but the core must import and run on Linux and
macOS so the platform-neutral two-thirds of the codebase stays testable in CI.
`JARVIS_DATA_DIR` overrides everything, which is what the tests use.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

APP_NAME = "Jarvis"


def _windows_base() -> tuple[Path, Path]:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return local / APP_NAME, roaming / APP_NAME


def _xdg_base() -> tuple[Path, Path]:
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return data / APP_NAME.lower(), config / APP_NAME.lower()


@lru_cache(maxsize=1)
def data_dir() -> Path:
    """Database, logs, models, cache."""
    if override := os.environ.get("JARVIS_DATA_DIR"):
        return Path(override)
    return (_windows_base() if sys.platform == "win32" else _xdg_base())[0]


@lru_cache(maxsize=1)
def config_dir() -> Path:
    """config.toml. Separate from data on Windows (roaming vs local)."""
    if override := os.environ.get("JARVIS_CONFIG_DIR"):
        return Path(override)
    if override := os.environ.get("JARVIS_DATA_DIR"):
        return Path(override)
    return (_windows_base() if sys.platform == "win32" else _xdg_base())[1]


def db_path() -> Path:
    return data_dir() / "jarvis.db"


def log_dir() -> Path:
    return data_dir() / "logs"


def models_dir() -> Path:
    return data_dir() / "models"


def config_file() -> Path:
    return config_dir() / "config.toml"


def ensure_dirs() -> None:
    """Create the directory tree. Safe to call repeatedly."""
    for path in (data_dir(), config_dir(), log_dir(), models_dir()):
        path.mkdir(parents=True, exist_ok=True)


def reset_cache() -> None:
    """Drop memoised paths. Tests call this after changing the environment."""
    data_dir.cache_clear()
    config_dir.cache_clear()
