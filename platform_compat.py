"""
platform_compat.py -- cross-platform OS abstraction for pzpanel.

All OS-specific operations go here. The rest of the codebase imports
from this module and never calls subprocess with system-specific
commands directly.

Supported platforms:
  Linux  -- systemd (systemctl start/stop/restart/is-active <unit>)
  Windows -- two modes, set via [server] windows_mode in pzpanel.ini:
    sc      -- Windows Service (sc start/stop/query <service name>)
    process -- bare detached process (spawned by pzpanel, PID tracked
               in a JSON file alongside other state files)

On Linux the [server] unit key is the systemd unit name (e.g. "pzserver").
On Windows sc mode it is the service name.
On Windows process mode it is ignored; [server] windows_start_script is
the path to the batch file or exe to launch.

Process mode PID tracking:
  Start  -- spawn fully detached (DETACHED_PROCESS on Windows),
             write PID to pzp_server_pid.json in the data directory.
  Stop   -- RCON quit, wait up to GRACEFUL_STOP_TIMEOUT_SECONDS, then
             taskkill /F /PID <pid> if still alive.
  Status -- check PID from file via OpenProcess (non-intrusive); fall
             back to scanning for java.exe processes whose command line
             contains "ProjectZomboid" if PID file is stale/missing.
"""

__version__ = "4.0.1"

import configparser
import json
import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("pzpanel.platform_compat")

GRACEFUL_STOP_TIMEOUT_SECONDS = 60
GRACEFUL_STOP_POLL_INTERVAL = 2

_IS_WINDOWS = platform.system() == "Windows"
_IS_LINUX = platform.system() == "Linux"


def get_platform():
    """Returns 'windows' or 'linux'. Raises RuntimeError on anything else."""
    if _IS_WINDOWS:
        return "windows"
    if _IS_LINUX:
        return "linux"
    raise RuntimeError(f"Unsupported platform: {platform.system()}")


def get_default_data_dir():
    """
    Linux:   /opt/pzp
    Windows: %LOCALAPPDATA%\\pzpanel
    """
    if _IS_WINDOWS:
        local_app_data = os.environ.get("LOCALAPPDATA", "C:\\Users\\Public")
        return Path(local_app_data) / "pzpanel"
    return Path("/opt/pzp")


def get_default_config_path():
    """
    Linux:   /opt/pzp/pzpanel.ini
    Windows: same directory as this script
    """
    if _IS_WINDOWS:
        return Path(__file__).parent / "pzpanel.ini"
    return Path("/opt/pzp/pzpanel.ini")


def get_pid_file_path(data_dir=None):
    if data_dir is None:
        data_dir = get_default_data_dir()
    return Path(data_dir) / "pzp_server_pid.json"


def _windows_mode(cfg):
    return cfg.get("server", "windows_mode", fallback="sc").strip().lower()


def _windows_start_script(cfg):
    return cfg.get("server", "windows_start_script", fallback="").strip()


def _unit(cfg):
    return cfg.get("server", "unit", fallback="pzserver").strip()


def _data_dir(cfg):
    raw = cfg.get("paths", "data_dir", fallback="").strip()
    return Path(raw) if raw else get_default_data_dir()


# ---------------------------------------------------------------------------
# Linux -- systemd
# ---------------------------------------------------------------------------

_SYSTEMCTL = "/usr/bin/systemctl"
_SUDO = "/usr/bin/sudo"
_ACTION_TIMEOUTS = {"start": 20, "stop": 150, "restart": 150}


def _systemctl(action, unit, timeout=None):
    if timeout is None:
        timeout = _ACTION_TIMEOUTS.get(action, 30)
    return subprocess.run(
        [_SUDO, _SYSTEMCTL, action, unit],
        capture_output=True, text=True, timeout=timeout,
    )


def _systemctl_is_active(unit):
    try:
        r = subprocess.run(
            [_SYSTEMCTL, "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() or "unknown"
    except Exception as e:
        return f"error: {e}"


def _systemctl_active_since(unit):
    try:
        r = subprocess.run(
            [_SYSTEMCTL, "show", "-p", "ActiveEnterTimestamp", "--value", unit],
            capture_output=True, text=True, timeout=5,
        )
        value = r.stdout.strip()
    except Exception as err:
        log.warning("Could not query systemd for %s start time: %s", unit, err)
        return None
    if not value or value == "n/a":
        return None
    naive = value.rsplit(" ", 1)[0] if " " in value else value
    from datetime import datetime
    try:
        dt = datetime.strptime(naive, "%a %Y-%m-%d %H:%M:%S")
        return time.mktime(dt.timetuple())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Windows -- sc mode
# ---------------------------------------------------------------------------

def _sc_action(action, service, timeout=30):
    return subprocess.run(
        ["sc", action, service],
        capture_output=True, text=True, timeout=timeout,
    )


def _sc_query(service):
    try:
        r = subprocess.run(
            ["sc", "query", service],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("STATE"):
                parts = line.split()
                if len(parts) >= 3:
                    return parts[-1].upper()
        return "UNKNOWN"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Windows -- process mode
# ---------------------------------------------------------------------------

def _write_pid(pid, data_dir):
    path = get_pid_file_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid}), encoding="utf-8")


def _read_pid(data_dir):
    path = get_pid_file_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("pid", 0)) or None
    except Exception:
        return None


def _clear_pid(data_dir):
    try:
        get_pid_file_path(data_dir).unlink(missing_ok=True)
    except Exception:
        pass


def _pid_alive_windows(pid):
    import ctypes
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def _find_pz_pid_by_scan():
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "name='java.exe'", "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.splitlines():
            if "ProjectZomboid" in line:
                parts = line.strip().split(",")
                if len(parts) >= 3:
                    try:
                        return int(parts[-1].strip())
                    except ValueError:
                        continue
    except Exception as e:
        log.warning("PZ process scan failed: %s", e)
    return None


def _spawn_detached_windows(script_path):
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    script_path = str(script_path)
    if script_path.lower().endswith((".bat", ".cmd")):
        cmd = ["cmd", "/c", "start", "", "/B", script_path]
    else:
        cmd = [script_path]
    try:
        proc = subprocess.Popen(
            cmd,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.pid
    except Exception as e:
        log.error("Failed to spawn PZ server process: %s", e)
        return None


def _stop_process_windows(pid, data_dir, rcon_quit_fn=None):
    if rcon_quit_fn is not None:
        try:
            rcon_quit_fn()
            log.info("platform_compat: RCON quit sent")
        except Exception as e:
            log.warning("platform_compat: RCON quit failed (%s), proceeding to wait+kill", e)

    waited = 0
    while waited < GRACEFUL_STOP_TIMEOUT_SECONDS:
        if not _pid_alive_windows(pid):
            log.info("platform_compat: process %d exited after %ds", pid, waited)
            _clear_pid(data_dir)
            return
        time.sleep(GRACEFUL_STOP_POLL_INTERVAL)
        waited += GRACEFUL_STOP_POLL_INTERVAL

    log.warning("platform_compat: process %d still alive after %ds, force-killing",
                pid, GRACEFUL_STOP_TIMEOUT_SECONDS)
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10)
    except Exception as e:
        log.error("platform_compat: taskkill failed: %s", e)
    _clear_pid(data_dir)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def server_start(cfg):
    plat = get_platform()
    if plat == "linux":
        return _systemctl("start", _unit(cfg))
    mode = _windows_mode(cfg)
    if mode == "sc":
        return _sc_action("start", _unit(cfg), timeout=20)
    script = _windows_start_script(cfg)
    if not script:
        raise RuntimeError("[server] windows_start_script must be set for windows_mode=process")
    data_dir = _data_dir(cfg)
    pid = _spawn_detached_windows(script)
    if pid:
        _write_pid(pid, data_dir)
        log.info("platform_compat: spawned PZ process PID %d", pid)
    return pid


def server_stop(cfg, rcon_quit_fn=None):
    plat = get_platform()
    if plat == "linux":
        return _systemctl("stop", _unit(cfg), timeout=150)
    mode = _windows_mode(cfg)
    if mode == "sc":
        return _sc_action("stop", _unit(cfg), timeout=150)
    data_dir = _data_dir(cfg)
    pid = _read_pid(data_dir) or _find_pz_pid_by_scan()
    if pid is None:
        log.warning("platform_compat: no PZ process found to stop")
        return None
    _stop_process_windows(pid, data_dir, rcon_quit_fn)
    return pid


def server_restart(cfg, rcon_quit_fn=None):
    plat = get_platform()
    if plat == "linux":
        return _systemctl("restart", _unit(cfg), timeout=150)
    mode = _windows_mode(cfg)
    if mode == "sc":
        _sc_action("stop", _unit(cfg), timeout=150)
        time.sleep(2)
        return _sc_action("start", _unit(cfg), timeout=20)
    server_stop(cfg, rcon_quit_fn)
    time.sleep(2)
    return server_start(cfg)


def server_active_since(cfg):
    """Returns unix epoch when server started, or None. Linux only."""
    if _IS_LINUX:
        return _systemctl_active_since(_unit(cfg))
    return None


def get_raw_server_state(cfg):
    """
    Returns a normalised state string:
      'active' | 'inactive' | 'failed' | 'starting' | 'unknown'
    """
    plat = get_platform()
    if plat == "linux":
        return _systemctl_is_active(_unit(cfg))
    mode = _windows_mode(cfg)
    if mode == "sc":
        s = _sc_query(_unit(cfg))
        return {
            "RUNNING": "active",
            "STOPPED": "inactive",
            "START_PENDING": "starting",
            "STOP_PENDING": "active",
        }.get(s, "unknown")
    data_dir = _data_dir(cfg)
    pid = _read_pid(data_dir)
    if pid is None:
        pid = _find_pz_pid_by_scan()
        if pid:
            _write_pid(pid, data_dir)
    if pid is None:
        return "inactive"
    return "active" if _pid_alive_windows(pid) else "inactive"


# ---------------------------------------------------------------------------
# File search -- OS-aware default roots
# ---------------------------------------------------------------------------

def default_search_roots():
    roots = []
    if _IS_LINUX:
        home = Path.home()
        roots.append(home / "Zomboid")
        roots.append(home)
        try:
            for entry in Path("/home").iterdir():
                if entry.is_dir():
                    roots.append(entry / "Zomboid")
        except (OSError, PermissionError):
            pass
    elif _IS_WINDOWS:
        candidates = []
        for drive in ["C", "D", "E"]:
            candidates += [
                Path(f"{drive}:\\steamcmd\\steamapps\\common\\Project Zomboid Dedicated Server"),
                Path(f"{drive}:\\SteamCMD\\steamapps\\common\\Project Zomboid Dedicated Server"),
                Path(f"{drive}:\\Steam\\steamapps\\common\\Project Zomboid Dedicated Server"),
                Path(f"{drive}:\\Games\\Project Zomboid Dedicated Server"),
                Path(f"{drive}:\\pzserver"),
            ]
        user_profile = os.environ.get("USERPROFILE", "C:\\Users\\Public")
        candidates.append(Path(user_profile) / "Zomboid")
        for pf in ["C:\\Program Files", "C:\\Program Files (x86)"]:
            candidates.append(Path(pf) / "Project Zomboid Dedicated Server")
        roots.extend(candidates)

    seen = set()
    out = []
    for r in roots:
        sr = str(r)
        if sr not in seen:
            seen.add(sr)
            out.append(r)
    return out


def default_acf_search_roots():
    roots = []
    if _IS_LINUX:
        home = Path.home()
        for candidate in [
            home / "steamcmd" / "steamapps" / "workshop",
            home / "pzserver" / "steamapps" / "workshop",
            home / "Steam" / "steamapps" / "workshop",
        ]:
            roots.append(candidate)
        roots.append(home)
    elif _IS_WINDOWS:
        for drive in ["C", "D", "E"]:
            roots += [
                Path(f"{drive}:\\steamcmd\\steamapps\\workshop"),
                Path(f"{drive}:\\SteamCMD\\steamapps\\workshop"),
                Path(f"{drive}:\\Steam\\steamapps\\workshop"),
            ]
        user_profile = os.environ.get("USERPROFILE", "C:\\Users\\Public")
        roots.append(Path(user_profile) / "steamcmd" / "steamapps" / "workshop")

    seen = set()
    out = []
    for r in roots:
        sr = str(r)
        if sr not in seen:
            seen.add(sr)
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Cross-platform file identity for tail/rotation detection
# ---------------------------------------------------------------------------

def file_identity(path):
    """
    Returns a hashable identity for detecting file replacement.
    Linux: (st_dev, st_ino) -- inode-based.
    Windows: same when available; falls back to (st_size, st_mtime)
             on filesystems without inode support (FAT, some network shares).
    """
    try:
        st = Path(path).stat()
        if st.st_ino != 0:
            return (st.st_dev, st.st_ino)
        return (st.st_size, st.st_mtime)
    except OSError:
        return None
