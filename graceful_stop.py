#!/usr/bin/env python3
"""
ExecStop wrapper for pzserver.service (Linux only).

Sends RCON "quit" (save + graceful shutdown) and waits for the java
process to actually exit before returning, so systemd's follow-up
KillSignal (SIGTERM, sent automatically after ExecStop= commands finish)
never lands mid-save.

Falls back silently (exits 0) if RCON is unreachable or the process is
already gone -- there's nothing graceful left to do at that point, and
systemd's normal kill-signal handling takes over from here.

On Windows this script is not used -- pzpanel's platform_compat.py
handles graceful stop (RCON quit + wait + taskkill) directly inside
the panel process for both sc and process modes. If this script is
somehow invoked on Windows it exits 0 immediately.
"""

__version__ = "4.0.1"

import configparser
import os
import platform
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rcon import RCONClient, RCONError

if platform.system() == "Windows":
    sys.exit(0)

CONFIG_PATH = os.environ.get("PZPANEL_CONFIG", "/opt/pzp/pzpanel.ini")
MAX_WAIT_SECONDS = 90
POLL_INTERVAL = 2


def get_main_pid(unit):
    result = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", unit],
        capture_output=True, text=True, timeout=5,
    )
    pid = result.stdout.strip()
    return int(pid) if pid.isdigit() and pid != "0" else None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    host = cfg.get("rcon", "host", fallback="127.0.0.1")
    port = cfg.getint("rcon", "port", fallback=27015)
    password = cfg.get("rcon", "password", fallback="")
    unit = cfg.get("server", "unit", fallback="pzserver")

    pid = get_main_pid(unit)
    if pid is None:
        print("graceful_stop: no running pzserver process found, nothing to do")
        return 0

    try:
        with RCONClient(host, port, password, timeout=5.0) as client:
            client.command("quit")
            print("graceful_stop: sent RCON quit")
    except RCONError as e:
        print(f"graceful_stop: RCON quit failed ({e}), falling back to systemd's kill signal")
        return 0
    except Exception as e:
        print(f"graceful_stop: unexpected RCON error ({e}), falling back to systemd's kill signal")
        return 0

    waited = 0
    while pid_alive(pid) and waited < MAX_WAIT_SECONDS:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    if pid_alive(pid):
        print(f"graceful_stop: process still alive after {MAX_WAIT_SECONDS}s, "
              f"letting systemd's kill signal finish it")
    else:
        print(f"graceful_stop: process exited cleanly after {waited}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
