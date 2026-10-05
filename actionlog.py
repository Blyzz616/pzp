"""
Append-only action log for PZ Panel: records every start/stop/restart
actually triggered through the panel or the mod-checker, with a
timestamp and a human-readable reason.

Only captures actions initiated through this codebase -- a raw
`systemctl restart pzserver` run manually from the CLI isn't logged,
since the initiator is what actually knows *why* the action happened.
"""

__version__ = "5.2.0"

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import platform_compat as pc

_FILE_NAME = "actions.jsonl"


def _path():
    env_override = os.environ.get("PZPANEL_DATA_DIR", "").strip()
    if env_override:
        return Path(env_override) / _FILE_NAME
    return pc.get_default_data_dir() / _FILE_NAME


def log_action(actor, action, reason):
    """
    actor: 'panel' or 'modcheck'. action: 'start'/'stop'/'restart'.
    reason: short human-readable string. Never raises -- a logging
    failure should never block the actual action it's describing.
    """
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "reason": reason,
    }
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"actionlog: failed to write log entry: {e}")


def read_recent(limit=200):
    """
    Returns the most recent `limit` entries, newest first. Tolerates a
    missing file (nothing logged yet) and skips any corrupt line rather
    than failing the whole read.
    """
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    entries = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries
