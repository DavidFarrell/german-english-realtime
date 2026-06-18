"""Tiny persisted-settings helper for the bridge (just the guard toggle for now).

Stored as JSON in the user's home (`~/.debbiedavidapp.json`) so it survives app rebuilds/reinstalls
(the packaged app runs the bridge from the repo, but settings are user state, not repo state). All
reads/writes are defensive: any IO/parse error degrades to the default, never raises.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_PATH = Path(os.path.expanduser("~")) / ".debbiedavidapp.json"


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        _PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def get_guard_enabled(default: bool = True) -> bool:
    val = _load().get("guard_enabled", default)
    return bool(val) if isinstance(val, bool) else default


def set_guard_enabled(enabled: bool) -> None:
    data = _load()
    data["guard_enabled"] = bool(enabled)
    _save(data)
