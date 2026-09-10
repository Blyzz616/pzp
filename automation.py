"""
Pause switch for automated mod-sync restarts.

A plain marker file rather than a database -- this only needs a single
boolean shared between two separate processes: the panel (main.py) and
the mod-checker (mod_restart.py), which runs as its own systemd oneshot
service on a timer (Linux) or equivalent scheduled task (Windows).
"""

__version__ = "4.1.0"

import os
from pathlib import Path

import platform_compat as pc

_FLAG_NAME = "automation_paused.flag"


def _flag_path():
    """OS-aware path for the pause flag file."""
    env_override = os.environ.get("PZPANEL_DATA_DIR", "").strip()
    if env_override:
        return Path(env_override) / _FLAG_NAME
    return pc.get_default_data_dir() / _FLAG_NAME


def is_paused():
    return _flag_path().exists()


def set_paused(paused: bool):
    path = _flag_path()
    if paused:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("paused\n", encoding="utf-8")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
