"""
Tracks mods removed via the panel's Remove flow, so a suspected
misbehaving mod can be pulled out for testing and easily added back
later if it turns out not to be the culprit. Plain JSON file, same
pattern as the other small state files in this project (automation.py,
countdown_control.py).
"""

__version__ = "4.6.4"

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import platform_compat as pc

_FILE_NAME = "removed_mods.json"


def _path():
    """Cross-platform data dir -- this file predated the Windows port
    and was still hardcoded to /opt/pzp (broken on Windows). Matches
    the pattern used by automation.py and pending_mods.py."""
    env_override = os.environ.get("PZPANEL_DATA_DIR", "").strip()
    if env_override:
        return Path(env_override) / _FILE_NAME
    return pc.get_default_data_dir() / _FILE_NAME


def _read():
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write(entries):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    os.replace(tmp, str(path))


def get_removed_mods():
    """Returns the list, most-recently-removed first."""
    return list(reversed(_read()))


def record_removed(mod_id, title, preview_url):
    """Adds (or refreshes, if it was somehow already tracked) an entry."""
    entries = [e for e in _read() if e.get("mod_id") != str(mod_id)]
    entries.append({
        "mod_id": str(mod_id),
        "title": title,
        "preview_url": preview_url,
        "removed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _write(entries)


def clear_removed(mod_id):
    """Called when a mod is added back -- it's no longer 'previously
    removed'. Also safe to call from the normal Add-mod flow, in case
    someone manually re-adds an ID that happens to be tracked here."""
    entries = [e for e in _read() if e.get("mod_id") != str(mod_id)]
    _write(entries)
