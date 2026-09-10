"""
Shared control/state for an in-progress or postponed mod-sync restart
countdown -- lets the panel (main.py, always-running) observe and steer
a countdown happening inside mod_restart.py (a separate, periodically
triggered process). No other IPC channel exists between them, so this
is plain files on disk, same pattern as automation.py's pause flag.

Files:
  countdown_state.json    -- written by mod_restart.py while a 10-minute
                              countdown is actively running. Absent when
                              no countdown is in progress.
  postponed_restart.json  -- written when a restart has been postponed
                              to a specific future time. Absent when
                              nothing is scheduled.
  countdown_command.json  -- written by the panel to signal pause/
                              resume/postpone. Consumed (read + deleted)
                              by mod_restart.py's countdown loop, so
                              each command fires exactly once.
"""

__version__ = "4.1.0"

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import platform_compat as pc


def _data_dir():
    env_override = os.environ.get("PZPANEL_DATA_DIR", "").strip()
    return Path(env_override) if env_override else pc.get_default_data_dir()


def _p(name):
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return str(d / name)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _clear(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# -- active countdown state ------------------------------------------------

def get_countdown_state():
    return _read_json(_p("countdown_state.json"))


def write_countdown_state(target_epoch, mods, paused=False, paused_remaining=None):
    _write_json(_p("countdown_state.json"), {
        "active": True,
        "target_epoch": target_epoch,
        "mods": mods,
        "paused": paused,
        "paused_remaining": paused_remaining,
    })


def clear_countdown_state():
    _clear(_p("countdown_state.json"))


# -- postponed restart state ------------------------------------------------

def get_postponed_state():
    return _read_json(_p("postponed_restart.json"))


def write_postponed_state(target_epoch, mods):
    _write_json(_p("postponed_restart.json"), {
        "scheduled": True,
        "target_epoch": target_epoch,
        "mods": mods,
    })


def clear_postponed_state():
    _clear(_p("postponed_restart.json"))


# -- commands: panel -> mod_restart.py --------------------------------------

def send_command(command, target_epoch=None):
    """command: 'pause' | 'resume' | 'postpone' | 'cancel'"""
    _write_json(_p("countdown_command.json"), {"command": command, "target_epoch": target_epoch})


def read_and_clear_command():
    """Returns the pending command dict, or None. Deletes after reading."""
    path = _p("countdown_command.json")
    cmd = _read_json(path)
    if cmd is not None:
        _clear(path)
    return cmd


# -- formatting --------------------------------------------------------------

def format_remaining(seconds):
    """'mm:ss', or just 'Ns' under a minute."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    mm, ss = divmod(seconds, 60)
    return f"{mm:02d}:{ss:02d}"


# -- postpone hour options ----------------------------------------------------

def compute_postpone_options(now=None, max_entries=25, min_lead_minutes=10):
    if now is None:
        now = datetime.now().astimezone()
    today = now.date()

    candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if (candidate - now) < timedelta(minutes=min_lead_minutes):
        candidate += timedelta(hours=1)

    options = []
    for i in range(max_entries):
        dt = candidate + timedelta(hours=i)
        options.append({
            "epoch": int(dt.timestamp()),
            "label": dt.strftime("%H:%M"),
            "tomorrow": dt.date() != today,
        })
    return options
