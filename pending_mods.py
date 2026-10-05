"""
Mods queued to be changed at the next server start or restart, rather
than immediately -- lets an admin queue a change while the server is
online (players mid-session) without forcing an immediate stop.

Two kinds of queued change:
  workshop_add -- add a Workshop ID to WorkshopItems= (so PZ downloads
                  it on next start)
  mods_enable  -- add a mod's internal string ID(s) to Mods= (so PZ
                  actually loads content that's already been downloaded
                  -- the easy-to-forget second step; see modcheck.py's
                  needs_enable/add_mods_line_items())

Both are applied automatically, cross-platform, right before the server
process actually starts -- see the two call sites that trigger an
actual start/restart: server_config.run_server_action() (manual
Start/Restart buttons, and the start leg of remove/readd/reorder) and
mod_restart.restart_server() (the automated mod-sync countdown
restart). Both call apply_pending() first.

Same JSON-file-with-atomic-rename pattern as removed_mods.py.
"""

__version__ = "5.0.1"

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import platform_compat as pc
from actionlog import log_action

_FILE_NAME = "pending_mods.json"


def _path():
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


def get_pending():
    """Returns the list, most-recently-queued first. Each entry has a
    'kind' field ('workshop_add' or 'mods_enable') -- older entries
    written before mods_enable existed default to 'workshop_add' via
    apply_pending()'s .get("kind", "workshop_add")."""
    return list(reversed(_read()))


def queue_workshop_add(mod_id, title, preview_url):
    """Queue adding mod_id to WorkshopItems= at the next start/restart."""
    entries = [e for e in _read()
               if not (e.get("mod_id") == str(mod_id) and e.get("kind", "workshop_add") == "workshop_add")]
    entries.append({
        "kind": "workshop_add",
        "mod_id": str(mod_id),
        "title": title,
        "preview_url": preview_url,
        "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _write(entries)


def queue_mods_enable(mod_id, title, preview_url, internal_ids):
    """
    Queue adding internal_ids to Mods= at the next start/restart --
    used once a Workshop item has actually been downloaded but isn't
    active yet (modcheck.check_for_updates()'s needs_enable/internal_ids).
    """
    entries = [e for e in _read()
               if not (e.get("mod_id") == str(mod_id) and e.get("kind") == "mods_enable")]
    entries.append({
        "kind": "mods_enable",
        "mod_id": str(mod_id),
        "title": title,
        "preview_url": preview_url,
        "internal_ids": list(internal_ids),
        "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _write(entries)


def cancel_pending(mod_id, kind=None):
    """
    Removes a queued change WITHOUT applying it. If kind is given, only
    that (mod_id, kind) pair is removed; otherwise every queued change
    for mod_id (both kinds) is removed -- used when a mod is manually
    added/removed directly, so a stale queue entry doesn't fire later
    against a WorkshopItems=/Mods= state that's since moved on.
    """
    def _keep(e):
        if e.get("mod_id") != str(mod_id):
            return True
        if kind is not None and e.get("kind", "workshop_add") != kind:
            return True
        return False
    entries = [e for e in _read() if _keep(e)]
    _write(entries)


def apply_pending():
    """
    Applies every queued change and clears the ones that succeed. Must
    be called right before the server process actually starts -- fast
    and a no-op if nothing is queued, the common case on most starts.

    An entry that fails to apply is NOT dropped, so it's retried on the
    next start instead of silently vanishing.

    Returns the list of mod_ids actually applied.
    """
    import modcheck  # local import: avoids this module needing modcheck at import time

    entries = _read()
    if not entries:
        return []

    applied = []
    still_pending = []
    for e in entries:
        mod_id = e.get("mod_id")
        kind = e.get("kind", "workshop_add")
        label = e.get("title") or mod_id
        try:
            if kind == "mods_enable":
                ids = e.get("internal_ids") or []
                if not ids:
                    raise ValueError("no internal_ids recorded for this queued entry")
                modcheck.add_mods_line_items(ids)
                log_action("modcheck", "apply-pending-mod",
                           f"Enabled (Mods=) at server start: {label} ({', '.join(ids)})")
            else:
                modcheck.add_workshop_item(mod_id)
                log_action("modcheck", "apply-pending-mod",
                           f"Added (WorkshopItems=) at server start: {label}")
            applied.append(mod_id)
        except Exception as ex:
            still_pending.append(e)
            log_action("modcheck", "apply-pending-mod-failed",
                       f"Failed to apply queued {kind} for {label}: {ex}")
    _write(still_pending)
    return applied
