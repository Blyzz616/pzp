"""
server_config.py — Config loading, server control, and INI helpers for pzpanel.

Extracted from main.py (v4.2.2) to keep the FastAPI app file focused on routes.
All functions here are pure utility — no FastAPI imports, no app object.
"""

import configparser
import os
import random
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from rcon import RCONClient
import countdown_control as cc
import platform_compat as pc

CONFIG_PATH = str(os.environ.get("PZPANEL_CONFIG") or pc.get_default_config_path())

ALLOWED_ACTIONS = ("start", "stop", "restart")
ACTION_TIMEOUTS = {"start": 20, "stop": 150, "restart": 150}

import logging
log = logging.getLogger("pzpanel")


# ---------------------------------------------------------------------------
# Config readers
# ---------------------------------------------------------------------------

def _load_full_cfg():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


def load_server_config():
    """Returns (display_name, unit).

    Display name priority:
      1. PublicName in the server .ini file
      2. The server .ini file's own stem
      3. [server] name in pzpanel.ini
    """
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    unit = cfg.get("server", "unit", fallback="pzserver")
    pzpanel_name = cfg.get("server", "name", fallback="PZ Server")
    server_ini = cfg.get("paths", "server_ini", fallback="").strip()
    if server_ini:
        ini_path = Path(server_ini)
        server_ini_vals = _read_realm_ini(server_ini)
        public_name = server_ini_vals.get("PublicName", "").strip()
        if public_name:
            return public_name, unit
        if ini_path.stem:
            return ini_path.stem, unit
    return pzpanel_name, unit


def load_console_log_path():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg.get("paths", "console_log", fallback="")


def load_server_ini_path():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg.get("paths", "server_ini", fallback="").strip() or None


def get_multiplayer_save_dir():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    raw = cfg.get("paths", "multiplayer_save_dir", fallback="").strip()
    return Path(raw) if raw else None


def load_rcon_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return (
        cfg.get("rcon", "host", fallback="127.0.0.1"),
        cfg.getint("rcon", "port", fallback=27015),
        cfg.get("rcon", "password", fallback=""),
    )


def _read_all_settings():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return {section: dict(cfg[section]) for section in cfg.sections()}


# ---------------------------------------------------------------------------
# Server state / control
# ---------------------------------------------------------------------------

def rcon_reachable():
    host, port, password = load_rcon_config()
    try:
        with RCONClient(host, port, password, timeout=2.0):
            return True
    except Exception:
        return False


def get_server_state():
    cfg = _load_full_cfg()
    raw = pc.get_raw_server_state(cfg)
    if raw == "inactive":
        return "offline"
    if raw == "failed":
        return "failed"
    if raw in ("active", "starting"):
        return "online" if rcon_reachable() else "starting"
    return raw


def _rcon_quit():
    host, port, password = load_rcon_config()
    try:
        with RCONClient(host, port, password, timeout=5.0) as client:
            client.command("quit")
    except Exception as e:
        log.warning("_rcon_quit: %s", e)


def run_server_action(action, cfg=None):
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"disallowed action: {action!r}")
    if cfg is None:
        cfg = _load_full_cfg()
    if action == "start":
        return pc.server_start(cfg)
    if action == "stop":
        return pc.server_stop(cfg, rcon_quit_fn=_rcon_quit)
    if action == "restart":
        return pc.server_restart(cfg, rcon_quit_fn=_rcon_quit)


def _countdown_summary():
    countdown_state = cc.get_countdown_state()
    countdown = None
    if countdown_state and countdown_state.get("active"):
        if countdown_state.get("paused"):
            remaining_display = cc.format_remaining(countdown_state.get("paused_remaining") or 0)
        else:
            target_epoch = countdown_state.get("target_epoch", time.time())
            remaining_display = cc.format_remaining(max(0, target_epoch - time.time()))
        countdown = {
            "paused": bool(countdown_state.get("paused")),
            "remaining_display": remaining_display,
            "mods": ", ".join(countdown_state.get("mods", [])),
        }
    postponed_state = cc.get_postponed_state()
    postponed = None
    if postponed_state and postponed_state.get("scheduled") and postponed_state.get("target_epoch"):
        target_epoch = postponed_state["target_epoch"]
        local_dt = datetime.fromtimestamp(target_epoch).astimezone()
        today = datetime.now().astimezone().date()
        suffix = " (tomorrow)" if local_dt.date() != today else ""
        postponed = {
            "label": f"{local_dt.strftime('%H:%M')}{suffix}",
            "mods": ", ".join(postponed_state.get("mods", [])),
        }
    return countdown, postponed


# ---------------------------------------------------------------------------
# INI file read/write
# ---------------------------------------------------------------------------

def _read_realm_ini(path):
    result = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    except OSError:
        pass
    return result


def _bool_val(raw, inverted=False):
    b = raw.strip().lower() in ("true", "1", "yes")
    return (not b) if inverted else b


def _ini_bool(display_on, inverted=False):
    actual_on = (not display_on) if inverted else display_on
    return "true" if actual_on else "false"


def _update_ini_settings(updates):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    for (section, key), value in updates.items():
        sec_start = None
        sec_end = len(lines)
        for i, line in enumerate(lines):
            m = section_re.match(line)
            if m:
                if sec_start is not None:
                    sec_end = i
                    break
                if m.group(1).strip().lower() == section.lower():
                    sec_start = i
        if sec_start is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"\n[{section}]\n")
            lines.append(f"{key} = {value}\n")
            continue
        key_re = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.IGNORECASE)
        found = False
        for i in range(sec_start + 1, sec_end):
            if key_re.match(lines[i]):
                lines[i] = f"{key} = {value}\n"
                found = True
                break
        if not found:
            insert_at = sec_end
            while insert_at > sec_start + 1 and lines[insert_at - 1].strip() == "":
                insert_at -= 1
            lines.insert(insert_at, f"{key} = {value}\n")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _update_realm_ini(ini_path, updates):
    ini_path = Path(ini_path)
    backup_dir = ini_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    backup_path = backup_dir / f"{ini_path.name}.{ts}"
    shutil.copy2(str(ini_path), str(backup_path))
    existing = sorted(backup_dir.glob(f"{ini_path.name}.*"))
    for old in existing[:-20]:
        try:
            old.unlink()
        except OSError:
            pass
    with open(ini_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    key_re_cache = {}
    for key, value in updates.items():
        rx = key_re_cache.setdefault(key, re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.IGNORECASE))
        found = False
        for i, line in enumerate(lines):
            if rx.match(line):
                lines[i] = f"{key}={value}\n"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}\n")
    with open(ini_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# File search helpers
# ---------------------------------------------------------------------------

def _find_candidate_files(extension, content_markers, search_roots=None, max_results=8, max_scanned=3000):
    if search_roots is None:
        search_roots = pc.default_search_roots()
    candidates = []
    seen = set()
    scanned = 0
    for root in search_roots:
        try:
            if not root.exists():
                continue
            for p in root.rglob(f"*.{extension}"):
                scanned += 1
                if scanned > max_scanned:
                    return candidates
                sp = str(p)
                if sp in seen:
                    continue
                seen.add(sp)
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        head = f.read(4096)
                except OSError:
                    continue
                if any(marker.lower() in head.lower() for marker in content_markers):
                    candidates.append(sp)
                if len(candidates) >= max_results:
                    return candidates
        except (OSError, PermissionError):
            continue
    return candidates


def _find_exact_named_files(filename, content_markers=None, search_roots=None, max_results=8, max_scanned=3000):
    if search_roots is None:
        search_roots = pc.default_search_roots()
    candidates = []
    seen = set()
    scanned = 0
    for root in search_roots:
        try:
            if not root.exists():
                continue
            for p in root.rglob(filename):
                scanned += 1
                if scanned > max_scanned:
                    return candidates
                sp = str(p)
                if sp in seen:
                    continue
                seen.add(sp)
                if content_markers:
                    try:
                        with open(p, "r", encoding="utf-8", errors="ignore") as f:
                            head = f.read(4096)
                    except OSError:
                        continue
                    if not any(marker.lower() in head.lower() for marker in content_markers):
                        continue
                candidates.append(sp)
                if len(candidates) >= max_results:
                    return candidates
        except (OSError, PermissionError):
            continue
    return candidates


# ---------------------------------------------------------------------------
# Settings schema (pzpanel.ini fields)
# ---------------------------------------------------------------------------

SETTINGS_GROUPS = [
    {
        "group": "Connection",
        "help": "Required — the panel cannot control your server without these.",
        "collapsed": False,
        "fields": [
            {"section": "rcon", "key": "host", "label": "RCON Host", "type": "text"},
            {"section": "rcon", "key": "port", "label": "RCON Port", "type": "number"},
            {"section": "rcon", "key": "password", "label": "RCON Password", "type": "password", "sensitive": True},
        ],
    },
    {
        "group": "Paths",
        "help": "Required — point these at your server files so the panel can read mods, status, and logs.",
        "collapsed": False,
        "fields": [
            {"section": "paths", "key": "server_ini", "label": "World Config (.ini) Path", "type": "text", "assist": "find-ini",
             "help": "Your world's server .ini file. Can be named anything (e.g. servertest.ini)."},
            {"section": "paths", "key": "workshop_acf", "label": "Workshop ACF Path", "type": "text", "assist": "find-acf"},
            {"section": "paths", "key": "console_log", "label": "Console Log Path", "type": "text", "assist": "derive-console-log"},
            {"section": "paths", "key": "multiplayer_save_dir", "label": "Multiplayer Save Dir", "type": "text", "assist": "derive-save-dir",
             "help": "Enables the Danger Zone (world wipe) on the Status page. Leave blank to hide it."},
        ],
    },
    {
        "group": "Server Control",
        "help": "How the panel starts and stops your server. Linux uses systemd; Windows uses a service or a batch file.",
        "collapsed": True,
        "os_toggle": True,
        "fields": [
            {"section": "server", "key": "name", "label": "Display Name (fallback)", "type": "text",
             "help": "Only used if no World Config .ini is set above. The panel normally reads the name from PublicName in the server .ini."},
            {"section": "server", "key": "unit", "label": "Systemd Unit Name", "type": "text", "os": "linux",
             "help": "The systemd unit that runs your PZ server (e.g. pzserver)."},
            {"section": "server", "key": "unit", "label": "Windows Service Name", "type": "text", "os": "windows",
             "help": "The Windows service name as registered with sc.exe or NSSM (sc mode only)."},
            {"section": "server", "key": "windows_mode", "label": "Windows Mode", "type": "text", "os": "windows",
             "help": "'sc' — Windows Service (recommended). 'process' — pzpanel launches the server itself."},
            {"section": "server", "key": "windows_start_script", "label": "Windows Start Script", "type": "text", "os": "windows",
             "help": "Process mode only: path to your server's .bat or .exe start file."},
        ],
    },
    {
        "group": "Discord Integration",
        "help": "Optional — post join/leave/death embeds and mod-update announcements to a Discord channel. Needs a panel restart to take effect.",
        "collapsed": True,
        "preview_html": """<div style="margin-bottom:1rem;padding:.75rem;background:#1e1f22;border-radius:4px;border-left:4px solid #9b59b6;font-family:'Inter',sans-serif;">
            <div style="font-size:.68rem;color:#9b59b6;font-weight:700;letter-spacing:.05em;margin-bottom:.4rem;">YOUR SERVER NAME</div>
            <div style="font-size:.85rem;color:#dcddde;margin-bottom:.25rem;">&#128279; New connection:</div>
            <div style="font-size:.8rem;color:#b9bbbe;">Steam Profile: SurvivorJoe</div>
            <div style="display:flex;gap:1.5rem;margin-top:.5rem;">
                <div style="font-size:.72rem;color:#72767d;"><span style="color:#b9bbbe;font-weight:600;">Logging in as</span><br>SurvivorJoe</div>
            </div>
            <div style="font-size:.65rem;color:#72767d;margin-top:.5rem;">&#128308; SurvivorJoe has disconnected &nbsp;&middot;&nbsp; &#128128; SurvivorJoe has now completed their playthrough.</div>
        </div>
        <p style="font-family:'JetBrains Mono',monospace;font-size:.68rem;color:var(--ink-dim);margin:0;">Posts join, disconnect, death, and server up/down embeds. Add Steam Enrichment below for names and avatars.</p>""",
        "fields": [
            {"section": "discord", "key": "webhook_url", "label": "Webhook URL", "type": "password", "sensitive": True,
             "help": "Create a webhook in Discord: Server Settings \u2192 Integrations \u2192 Webhooks."},
        ],
    },
    {
        "group": "Steam Enrichment",
        "help": "Optional — adds Steam persona names, avatars, and PZ hours to join embeds. Needs a panel restart to take effect.",
        "preview_html": """<div style="margin-bottom:1rem;padding:.75rem;background:#1e1f22;border-radius:4px;border-left:4px solid #9b59b6;font-family:'Inter',sans-serif;">
            <div style="font-size:.68rem;color:#9b59b6;font-weight:700;letter-spacing:.05em;margin-bottom:.4rem;">YOUR SERVER NAME</div>
            <div style="font-size:.85rem;color:#dcddde;margin-bottom:.4rem;">&#128279; New connection:</div>
            <div style="display:flex;gap:.6rem;align-items:center;margin-bottom:.5rem;">
                <div style="width:32px;height:32px;border-radius:4px;background:linear-gradient(135deg,#4a5568,#2d3748);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.9rem;">&#129503;</div>
                <div style="font-size:.8rem;color:#b9bbbe;">Steam Profile: <span style="color:#00b0f4;">SurvivorJoe</span></div>
            </div>
            <div style="display:flex;gap:1.5rem;">
                <div style="font-size:.72rem;"><span style="color:#72767d;">Kills:</span><br><span style="color:#b9bbbe;font-weight:600;">47</span></div>
                <div style="font-size:.72rem;"><span style="color:#72767d;">Hours on Record:</span><br><span style="color:#b9bbbe;font-weight:600;">312</span></div>
            </div>
            <div style="font-size:.72rem;margin-top:.4rem;color:#72767d;border-top:1px solid #2c2f33;padding-top:.4rem;">
                <span style="color:#b9bbbe;font-weight:600;">SurvivorJoe has also played:</span>
                &nbsp; DayZ &nbsp;&middot;&nbsp; 88 hrs &nbsp;&middot;&nbsp; Last played: Yesterday
            </div>
        </div>
        <p style="font-family:'JetBrains Mono',monospace;font-size:.68rem;color:var(--ink-dim);margin:0 0 .4rem;">Without an API key: fetches Steam persona name and avatar only.<br>With an API key: also adds PZ hours, recently played games, and last-seen dates.</p>""",
        "collapsed": True,
        "fields": [
            {"section": "steam", "key": "enabled", "label": "Enabled", "type": "checkbox"},
            {"section": "steam", "key": "api_key", "label": "Steam Web API Key", "type": "password", "sensitive": True,
             "help": "Free at steamcommunity.com/dev/apikey \u2014 unlocks hours on record and recently played games in join embeds."},
            {"section": "steam", "key": "cache_hours", "label": "Cache Hours", "type": "number",
             "help": "How long to cache Steam profile lookups. 24 is a sensible default."},
        ],
    },
    {
        "group": "Kill Tracking (PZP Mod)",
        "help": "Optional — enables the Killboard page. Requires the PZP Lua mod installed on your server. Needs a panel restart to take effect.",
        "collapsed": True,
        "preview_html": """<div style="display:flex;gap:.9rem;align-items:flex-start;margin-bottom:.9rem;padding:.75rem;background:#1a2016;border:1px solid #2b3324;border-radius:3px;">
            <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=3793020885" target="_blank" rel="noopener noreferrer" style="flex-shrink:0;">
                <img src="https://steamuserimages-a.akamaihd.net/ugc/placeholder/3793020885/"
                     width="64" height="64" style="border-radius:2px;border:1px solid #2b3324;display:block;background:#12160f;"
                     onerror="this.src='https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/108600/header.jpg';this.style.objectFit='cover';this.style.height='36px';" alt="PZP mod">
            </a>
            <div style="min-width:0;">
                <div style="font-family:'Oswald',sans-serif;font-size:.95rem;letter-spacing:.03em;margin:0 0 .2rem;color:#e8e3d2;">PZP &#8212; Kill Tracker</div>
                <div style="font-family:'JetBrains Mono',monospace;font-size:.68rem;color:#8b9179;margin-bottom:.4rem;">Workshop ID: 3793020885</div>
                <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=3793020885" target="_blank" rel="noopener noreferrer"
                   style="font-family:'JetBrains Mono',monospace;font-size:.68rem;color:#ffb020;text-decoration:none;">View on Steam Workshop &#8599;</a>
            </div>
            <form method="post" action="/mods/add/confirm" style="margin-left:auto;flex-shrink:0;">
                <input type="hidden" name="mod_id" value="3793020885">
                <button type="submit" class="btn primary" style="font-size:.72rem;padding:.45rem .9rem;">+ Add to Mods</button>
            </form>
        </div>
        <p style="font-family:'JetBrains Mono',monospace;font-size:.68rem;color:var(--ink-dim);margin:0 0 .75rem;">The mod writes a kill-count file the panel reads every poll interval. Install it on your server, set <code>kills_file</code> below, restart the panel, and the Killboard tab goes live.</p>""",
        "fields": [
            {"section": "player_events", "key": "kills_file", "label": "Kills File Path", "type": "text",
             "help": "Path to pzp_player_kills.txt written by the PZP mod."},
            {"section": "player_events", "key": "player_db", "label": "Player DB Path", "type": "text",
             "help": "SQLite database for persistent player/kill tracking."},
            {"section": "player_events", "key": "state_file", "label": "Player-Event State File", "type": "text"},
            {"section": "player_events", "key": "poll_interval", "label": "Poll Interval (sec)", "type": "number"},
            {"section": "player_events", "key": "respawn_window", "label": "Respawn Window (sec)", "type": "number",
             "help": "A death followed by a rejoin within this many seconds counts as a respawn, not a new connection."},
            {"section": "player_events", "key": "logs_dir", "label": "Logs Dir Override", "type": "text",
             "help": "Override the Logs/ directory location if it's not alongside console_log."},
        ],
    },
]

# Flat list for the settings save handler
SETTINGS_SCHEMA = [f for g in SETTINGS_GROUPS for f in g["fields"]]


# ---------------------------------------------------------------------------
# Config page: realm .ini groups
# ---------------------------------------------------------------------------

_REALM_GROUPS = [
    {"group": "Players & Access", "fields": [
        {"key": "Open", "label": "Open to Public", "type": "toggle"},
        {"key": "Password", "label": "Server Password", "type": "password"},
        {"key": "MaxPlayers", "label": "Max Players", "type": "range", "min": 1, "max": 100, "default": 32},
        {"key": "MaxAccountsPerUser", "label": "Max Characters per Steam Account", "type": "range", "min": 1, "max": 32, "default": 1},
        {"key": "AllowCoop", "label": "Allow Split-Screen Co-op", "type": "toggle"},
        {"key": "PingLimit", "label": "Ping Limit (ms, 0=off)", "type": "range", "min": 0, "max": 1000, "default": 400},
        {"key": "LoginQueueEnabled", "label": "Login Queue", "type": "toggle", "master_for": "LoginQueue"},
        {"key": "LoginQueueConnectTimeout", "label": "Queue Timeout (sec)", "type": "range", "min": 20, "max": 1200, "default": 60, "group_key": "LoginQueue"},
        {"key": "AllowNonAsciiUsername", "label": "Allow Non-ASCII Usernames", "type": "toggle"},
    ]},
    {"group": "PVP", "fields": [
        {"key": "PVP", "label": "PVP Enabled", "type": "toggle", "master_for": "PVP"},
        {"key": "SafetySystem", "label": "Safety System", "type": "toggle", "group_key": "PVP", "master_for": "Safety"},
        {"key": "ShowSafety", "label": "Show Safety Status", "type": "toggle", "group_key": "Safety"},
        {"key": "SafetyToggleTimer", "label": "Safety Toggle Timer (min)", "type": "range", "min": 0, "max": 1440, "default": 2, "group_key": "Safety"},
        {"key": "SafetyCooldownTimer", "label": "Safety Cooldown Timer (min)", "type": "range", "min": 0, "max": 1440, "default": 3, "group_key": "Safety"},
        {"key": "PVPMeleeDamageModifier", "label": "PVP Melee Damage Modifier (%)", "type": "range", "min": 0, "max": 500, "default": 30, "group_key": "PVP"},
        {"key": "PVPFirearmDamageModifier", "label": "PVP Firearm Damage Modifier (%)", "type": "range", "min": 0, "max": 500, "default": 50, "group_key": "PVP"},
        {"key": "PVPMeleeWhileHitReaction", "label": "PVP Melee While Staggered", "type": "toggle", "group_key": "PVP"},
    ]},
    {"group": "World & Gameplay", "fields": [
        {"key": "PauseEmpty", "label": "Pause When Empty", "type": "toggle"},
        {"key": "NoFire", "label": "Disable Fire Spread", "type": "toggle"},
        {"key": "SaveWorldEveryMinutes", "label": "Save World Every (minutes)", "type": "range", "min": 0, "max": 1440, "default": 0},
        {"key": "BloodSplatLifespanDays", "label": "Blood Splat Lifespan (days, 0=forever)", "type": "range", "min": 0, "max": 365, "default": 0},
        {"key": "AllowDestructionBySledgehammer", "label": "Allow Sledgehammer Destruction", "type": "toggle"},
        {"key": "SledgehammerOnlyInSafehouse", "label": "Sledgehammer Only in Safehouse", "type": "toggle"},
        {"key": "SpeedLimit", "label": "Vehicle Speed Limit (kph)", "type": "range", "min": 10, "max": 150, "default": 70},
        {"key": "CarEngineAttractionModifier", "label": "Vehicle Engine Noise Modifier", "type": "range", "min": 0, "max": 10, "default": 1},
        {"key": "TrashDeleteAll", "label": "Bin Deletes All Items", "type": "toggle"},
        {"key": "UsePhysicsHitReaction", "label": "Physics Hit Reactions", "type": "toggle"},
    ]},
    {"group": "Vehicles & Towing", "fields": [
        {"key": "DisableVehicleTowing", "label": "Allow Vehicle Towing", "type": "toggle", "inverted": True},
        {"key": "DisableTrailerTowing", "label": "Allow Trailer Towing", "type": "toggle", "inverted": True},
        {"key": "DisableBurntTowing", "label": "Allow Burnt Vehicle Towing", "type": "toggle", "inverted": True},
    ]},
    {"group": "Players on Map", "fields": [
        {"key": "MapRemotePlayerVisibility", "label": "Remote Player Map Visibility", "type": "range", "min": 1, "max": 3, "default": 1, "help": "1=Friends, 2=Friends of friends, 3=Everyone"},
        {"key": "SteamScoreboard", "label": "Steam Scoreboard", "type": "range", "min": 0, "max": 2, "default": 1, "help": "0=Off, 1=Friends, 2=Everyone"},
        {"key": "ShowCoordinates", "label": "Show Coordinates", "type": "toggle"},
        {"key": "DisplayUserName", "label": "Display Username Above Head", "type": "toggle"},
        {"key": "ShowFirstAndLastName", "label": "Show First and Last Name", "type": "toggle"},
        {"key": "MouseOverToSeeDisplayName", "label": "Mouse-Over to See Name", "type": "toggle"},
        {"key": "HidePlayersBehindYou", "label": "Hide Players Behind You", "type": "toggle"},
        {"key": "UsernameDisguises", "label": "Username Disguises", "type": "toggle"},
        {"key": "HideDisguisedUserName", "label": "Hide Disguised Usernames", "type": "toggle"},
        {"key": "SneakModeHideFromOtherPlayers", "label": "Sneaking Hides from Other Players", "type": "toggle"},
    ]},
    {"group": "Chat", "fields": [
        {"key": "GlobalChat", "label": "Global Chat", "type": "toggle", "master_for": "Chat"},
        {"key": "ChatStreams", "label": "Chat Channels", "type": "checkboxes", "group_key": "Chat",
         "options": [("s", "Say"), ("r", "Radio"), ("a", "Admin"), ("w", "Whisper"), ("y", "Yell"), ("sh", "Shout"), ("f", "Faction"), ("all", "All")]},
        {"key": "AnnounceDeath", "label": "Announce Player Deaths", "type": "toggle"},
        {"key": "AnnounceAnimalDeath", "label": "Announce Animal Deaths", "type": "toggle"},
        {"key": "BanKickGlobalSound", "label": "Global Sound on Ban/Kick", "type": "toggle"},
        {"key": "ChatMessageCharacterLimit", "label": "Chat Message Character Limit", "type": "range", "min": 64, "max": 2000, "default": 200, "group_key": "Chat"},
        {"key": "ChatMessageSlowModeTime", "label": "Chat Slow Mode (sec, 0=off)", "type": "range", "min": 0, "max": 60, "default": 0, "group_key": "Chat"},
    ]},
    {"group": "Safehouses", "fields": [
        {"key": "PlayerSafehouse", "label": "Player Safehouses", "type": "toggle", "master_for": "Safehouse"},
        {"key": "AdminSafehouse", "label": "Admin Safehouses", "type": "toggle"},
        {"key": "SafehouseAllowTrespass", "label": "Allow Safehouse Trespass", "type": "toggle", "group_key": "Safehouse"},
        {"key": "SafehouseAllowFire", "label": "Allow Fire in Safehouse", "type": "toggle", "group_key": "Safehouse"},
        {"key": "SafehouseAllowLoot", "label": "Allow Safehouse Looting", "type": "toggle", "group_key": "Safehouse"},
        {"key": "SafehouseAllowRespawn", "label": "Allow Respawn in Safehouse", "type": "toggle", "group_key": "Safehouse"},
        {"key": "SafehouseDaySurvivedToClaim", "label": "Days Survived to Claim Safehouse", "type": "range", "min": 0, "max": 365, "default": 0, "group_key": "Safehouse"},
        {"key": "SafeHouseRemovalTime", "label": "Safehouse Removal Time (hours)", "type": "range", "min": 0, "max": 8760, "default": 144, "group_key": "Safehouse"},
        {"key": "SafehouseAllowNonResidential", "label": "Allow Non-Residential Safehouses", "type": "toggle", "group_key": "Safehouse"},
        {"key": "DisableSafehouseWhenOwnerConnected", "label": "Disable Safehouse When Owner Online", "type": "toggle", "group_key": "Safehouse"},
        {"key": "MaxSafezoneSize", "label": "Max Safezone Size", "type": "range", "min": 1, "max": 10, "default": 3, "group_key": "Safehouse"},
        {"key": "SafehouseDisableDisguises", "label": "Disguises Disabled in Safehouse", "type": "toggle", "group_key": "Safehouse"},
        {"key": "SafehousePreventsLootRespawn", "label": "Safehouse Prevents Loot Respawn", "type": "toggle", "group_key": "Safehouse"},
    ]},
    {"group": "Factions", "fields": [
        {"key": "Faction", "label": "Factions Enabled", "type": "toggle", "master_for": "Faction"},
        {"key": "FactionDaySurvivedToCreate", "label": "Days Survived to Create Faction", "type": "range", "min": 0, "max": 365, "default": 1, "group_key": "Faction"},
        {"key": "FactionPlayersRequiredForTag", "label": "Players Required for Faction Tag", "type": "range", "min": 1, "max": 64, "default": 1, "group_key": "Faction"},
    ]},
    {"group": "War", "fields": [
        {"key": "War", "label": "War Mode Enabled", "type": "toggle", "master_for": "War"},
        {"key": "WarStartDelay", "label": "War Start Delay (hours)", "type": "range", "min": 0, "max": 720, "default": 24, "group_key": "War"},
        {"key": "WarDuration", "label": "War Duration (hours)", "type": "range", "min": 1, "max": 720, "default": 24, "group_key": "War"},
        {"key": "WarSafehouseHitPoints", "label": "Safehouse Hit Points During War", "type": "range", "min": 100, "max": 10000, "default": 2000, "group_key": "War"},
    ]},
    {"group": "Respawn", "fields": [
        {"key": "PlayerRespawnWithSelf", "label": "Respawn With Self", "type": "toggle"},
        {"key": "PlayerRespawnWithOther", "label": "Respawn With Other Players", "type": "toggle"},
        {"key": "DropOffWhiteListAfterDeath", "label": "Remove Whitelist on Death", "type": "toggle"},
        {"key": "SpawnPoint", "label": "Spawn Point (x,y,z or blank=random)", "type": "text"},
    ]},
    {"group": "Sleep & Time", "fields": [
        {"key": "SleepAllowed", "label": "Sleep Allowed", "type": "toggle", "master_for": "Sleep"},
        {"key": "SleepNeeded", "label": "Sleep Required", "type": "toggle", "group_key": "Sleep"},
        {"key": "FastForwardMultiplier", "label": "Sleep Fast-Forward Speed", "type": "range", "min": 1, "max": 100, "default": 40, "group_key": "Sleep"},
    ]},
    {"group": "Voice (VOIP)", "fields": [
        {"key": "VoiceEnable", "label": "VOIP Enabled", "type": "toggle", "master_for": "VOIP"},
        {"key": "VoiceMinDistance", "label": "Min Voice Distance", "type": "range", "min": 0, "max": 100, "default": 10, "group_key": "VOIP"},
        {"key": "VoiceMaxDistance", "label": "Max Voice Distance", "type": "range", "min": 0, "max": 100, "default": 100, "group_key": "VOIP"},
        {"key": "Voice3D", "label": "3D Voice", "type": "toggle", "group_key": "VOIP"},
    ]},
    {"group": "Networking", "fields": [
        {"key": "DefaultPort", "label": "Game Port", "type": "range", "min": 1024, "max": 65535, "default": 16261},
        {"key": "UDPPort", "label": "UDP Port", "type": "range", "min": 1024, "max": 65535, "default": 16262},
        {"key": "UPnP", "label": "UPnP", "type": "toggle"},
        {"key": "DenyLoginOnOverloadedServer", "label": "Deny Login When Overloaded", "type": "toggle"},
        {"key": "SteamVAC", "label": "Steam VAC", "type": "toggle"},
        {"key": "MaxPacketsPerSecond", "label": "Max Packets per Second", "type": "range", "min": 1, "max": 100, "default": 60},
        {"key": "MultiplayerStatisticsPeriod", "label": "Statistics Period", "type": "range", "min": 5, "max": 3600, "default": 30},
    ]},
    {"group": "Radio Restrictions", "fields": [
        {"key": "DisableRadioStaff", "label": "Staff Can Use Radio", "type": "toggle", "inverted": True},
        {"key": "DisableRadioAdmin", "label": "Admins Can Use Radio", "type": "toggle", "inverted": True},
        {"key": "DisableRadioGM", "label": "GMs Can Use Radio", "type": "toggle", "inverted": True},
        {"key": "DisableRadioOverseer", "label": "Overseers Can Use Radio", "type": "toggle", "inverted": True},
        {"key": "DisableRadioModerator", "label": "Moderators Can Use Radio", "type": "toggle", "inverted": True},
        {"key": "DisableRadioInvisible", "label": "Invisible Admins Can Use Radio", "type": "toggle", "inverted": True},
    ]},
    {"group": "Public Info", "fields": [
        {"key": "Public", "label": "Listed on Public Browser", "type": "toggle"},
        {"key": "PublicName", "label": "Public Server Name", "type": "text"},
        {"key": "PublicDescription", "label": "Public Description", "type": "text"},
        {"key": "HideAdminsInPlayerList", "label": "Hide Admins in Player List", "type": "toggle"},
        {"key": "DisableScoreboard", "label": "Disable Scoreboard", "type": "toggle"},
        {"key": "PerkLogs", "label": "Perk Logs", "type": "toggle"},
        {"key": "ItemNumbersLimitPerContainer", "label": "Item Limit per Container", "type": "range", "min": 0, "max": 9000, "default": 0},
        {"key": "DoLuaChecksum", "label": "Lua Checksum Verification", "type": "toggle", "help": "Disable if using mods that modify client-side Lua."},
    ]},
    {"group": "Anti-Cheat Severity", "fields": [
        {"key": "AntiCheatProtectionType1", "label": "Protection Type 1", "type": "range", "min": 1, "max": 4, "default": 2},
        {"key": "AntiCheatProtectionType2", "label": "Protection Type 2", "type": "range", "min": 1, "max": 4, "default": 4},
        {"key": "AntiCheatProtectionType3", "label": "Protection Type 3", "type": "range", "min": 1, "max": 4, "default": 2},
        {"key": "AntiCheatProtectionType4", "label": "Protection Type 4", "type": "range", "min": 1, "max": 4, "default": 4},
        {"key": "AntiCheatProtectionType5", "label": "Protection Type 5", "type": "range", "min": 1, "max": 4, "default": 4},
        {"key": "AntiCheatProtectionType6", "label": "Protection Type 6", "type": "range", "min": 1, "max": 4, "default": 4},
        {"key": "AntiCheatProtectionType7", "label": "Protection Type 7", "type": "range", "min": 1, "max": 4, "default": 4},
        {"key": "AntiCheatProtectionType8", "label": "Protection Type 8", "type": "range", "min": 1, "max": 4, "default": 4},
    ]},
    {"group": "Server Backups", "fields": [
        {"key": "BackupsOnStart", "label": "Backup on Server Start", "type": "toggle"},
        {"key": "BackupsOnVersionChange", "label": "Backup on Version Change", "type": "toggle"},
        {"key": "BackupsCount", "label": "Max Backups to Keep", "type": "range", "min": 1, "max": 300, "default": 5},
        {"key": "BackupsPeriod", "label": "Backup Period (minutes, 0=off)", "type": "range", "min": 0, "max": 10080, "default": 0},
    ]},
]
