from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


def config_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    if os.name == "nt":
        root = Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    legacy = root / "claude-fleet"
    return legacy if legacy.is_dir() else root / "quotapool"


def state_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    if os.name == "nt":
        root = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        legacy = root / "claude-fleet" / "state"
        return legacy if legacy.is_dir() else root / "Quotapool" / "state"
    root = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    legacy = root / "claude-fleet"
    return legacy if legacy.is_dir() else root / "quotapool"


def runtime_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("FLEET_RUNTIME_CONFIG")
    return Path(configured).expanduser() if configured else config_dir(env) / "runtime.json"


def default_db_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("FLEET_DB")
    return Path(configured).expanduser() if configured else state_dir(env) / "fleet.sqlite3"
