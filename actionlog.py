"""
Append-only action log for PZ Panel: records every start/stop/restart
actually triggered through the panel or the mod-checker, with a
timestamp and a human-readable reason.

Only captures actions initiated through this codebase -- a raw
`systemctl restart pzserver` run manually from the CLI isn't logged,
since the initiator is what actually knows *why* the action happened.
"""

__version__ = "4.1.0"

import json
from datetime import datetime, timezone

LOG_PATH = "/opt/pzp/actions.jsonl"


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
        with open(LOG_PATH, "a", encoding="utf-8") as f:
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
        with open(LOG_PATH, "r", encoding="utf-8") as f:
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
