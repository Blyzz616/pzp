"""
Tracks mods removed via the panel's Remove flow, so a suspected
misbehaving mod can be pulled out for testing and easily added back
later if it turns out not to be the culprit. Plain JSON file, same
pattern as the other small state files in this project (automation.py,
countdown_control.py).
"""

__version__ = "4.0.1"

import json
import os
from datetime import datetime, timezone

REMOVED_MODS_PATH = "/opt/pzp/removed_mods.json"


def _read():
    try:
        with open(REMOVED_MODS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write(entries):
    tmp = REMOVED_MODS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    os.replace(tmp, REMOVED_MODS_PATH)


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
