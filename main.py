"""
PZ Panel - minimal web UI for managing the Project Zomboid server.

Start/stop/restart just call platform_compat -- graceful RCON shutdown
lives in pzserver.service's ExecStop= directive on Linux (or is handled
by platform_compat's RCON quit + wait + kill on Windows), so it applies
whether this panel, the CLI, or a reboot triggers the stop.

Cross-platform: Linux (systemd) and Windows (sc or process mode).
See platform_compat.py for all OS-specific operations.
CONFIG_PATH defaults to /opt/pzp/pzpanel.ini on Linux and to the
directory alongside main.py on Windows. Override with PZPANEL_CONFIG
environment variable.
"""

__version__ = "4.1.2"

import asyncio
import configparser
import html
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from rcon import RCONClient
from modcheck import (check_for_updates, add_workshop_item, remove_workshop_item,
                       reorder_workshop_items, ModCheckError)
from steam_workshop import get_mod_details, SteamWorkshopError
from actionlog import log_action, read_recent
from automation import is_paused, set_paused
import countdown_control as cc
import removed_mods
import discord_module
import player_db as pdb
import platform_compat as pc

log = logging.getLogger("pzpanel")

app = FastAPI(title="PZ Panel")

CONFIG_PATH = str(os.environ.get("PZPANEL_CONFIG") or pc.get_default_config_path())

ALLOWED_ACTIONS = ("start", "stop", "restart")
ACTION_TIMEOUTS = {"start": 20, "stop": 150, "restart": 150}

_SCRIPT_DIR = Path(__file__).parent
_FAVICON_DIR = (_SCRIPT_DIR / "favicon" if (_SCRIPT_DIR / "favicon").exists()
                else pc.get_default_data_dir() / "favicon")

app.mount("/favicon", StaticFiles(directory=str(_FAVICON_DIR)), name="favicon")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    ico = _FAVICON_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico))
    from fastapi.responses import Response
    return Response(status_code=204)


_shutting_down = asyncio.Event()
_player_event_watcher = None
_player_event_thread = None


@app.on_event("startup")
def _on_startup():
    global _player_event_watcher, _player_event_thread
    try:
        _player_event_watcher = discord_module.build_watcher(CONFIG_PATH)
    except Exception:
        log.exception("Failed to build player-event watcher, continuing without it")
        _player_event_watcher = None
    if _player_event_watcher is not None:
        _player_event_thread = threading.Thread(
            target=_player_event_watcher.run, name="player-event-watcher", daemon=True)
        _player_event_thread.start()
        log.info("Player-event watcher started")


@app.on_event("shutdown")
async def _on_shutdown():
    _shutting_down.set()
    if _player_event_watcher is not None:
        _player_event_watcher.stop()
        if _player_event_thread is not None:
            _player_event_thread.join(timeout=5)


PAGE_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --void: #0a0d09; --panel: #12160f; --line: #2b3324;
  --ink: #e8e3d2; --ink-dim: #8b9179;
  --amber: #ffb020; --amber-dim: #7a5a1a;
  --rust: #c2432b; --rust-dim: #5c2418;
  --signal: #6fbf5a; --signal-dim: #34401f;
  --info: #3a7fb8;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--void);
  background-image: radial-gradient(ellipse at top left,rgba(255,176,32,.05),transparent 50%),radial-gradient(ellipse at bottom right,rgba(111,191,90,.04),transparent 50%);
  color:var(--ink); font-family:'Inter',system-ui,sans-serif; min-height:100vh; }
.hazard-bar { height:7px; background:repeating-linear-gradient(135deg,var(--amber),var(--amber) 14px,#171203 14px,#171203 28px); }
.hazard-bar.danger { background:repeating-linear-gradient(135deg,var(--rust),var(--rust) 14px,#171203 14px,#171203 28px); }
.hazard-frame { background:repeating-linear-gradient(135deg,var(--amber),var(--amber) 14px,#171203 14px,#171203 28px); padding:7px; border-radius:3px; }
.hazard-frame.danger { background:repeating-linear-gradient(135deg,var(--rust),var(--rust) 14px,#171203 14px,#171203 28px); }
.hazard-frame-inner { background:var(--panel); border-radius:2px; padding:1.25rem 1.5rem; }
.wrap { max-width:760px; margin:0 auto; padding:2.5rem 1.5rem 3rem; }
.eyebrow { font-family:'JetBrains Mono',monospace; font-size:.72rem; letter-spacing:.18em; color:var(--ink-dim); text-transform:uppercase; margin:0 0 .35rem; }
h1 { font-family:'Oswald',sans-serif; font-weight:700; font-size:2rem; letter-spacing:.02em; text-transform:uppercase; margin:0 0 1.5rem; }
nav.tabs { display:flex; align-items:center; gap:1.5rem; margin-bottom:1.75rem; font-family:'JetBrains Mono',monospace; font-size:.78rem; letter-spacing:.06em; text-transform:uppercase; }
nav.tabs a { color:var(--ink-dim); text-decoration:none; padding-bottom:.3rem; border-bottom:2px solid transparent; transition:color .15s,border-color .15s; }
nav.tabs a:hover,nav.tabs a.active { color:var(--amber); border-color:var(--amber); }
nav.tabs a:focus-visible { outline:2px solid var(--amber); outline-offset:3px; }
nav.tabs a.icon-tab { display:inline-flex; align-items:center; margin-left:auto; }
nav.tabs a.icon-tab svg { display:block; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:3px; padding:1.75rem 1.75rem 0; }
.readout { display:flex; align-items:center; gap:.85rem; margin-bottom:1.75rem; flex-wrap:wrap; }
.led { width:13px; height:13px; border-radius:50%; flex-shrink:0; background:var(--led-colour,var(--ink-dim)); box-shadow:0 0 10px 2px var(--led-colour,transparent); }
.led.pulse { animation:ledpulse 1.7s ease-in-out infinite; }
@keyframes ledpulse { 0%{opacity:1}8%{opacity:.4}10%{opacity:1}30%{opacity:.85}33%{opacity:1}55%{opacity:.3}60%{opacity:1}80%{opacity:.92}100%{opacity:1} }
@keyframes led-dark-flash { 0%,100%{opacity:inherit}1%{opacity:0}66%{opacity:0}67%{opacity:inherit} }
.led.dark-flash { animation:ledpulse 1.7s ease-in-out infinite,led-dark-flash 33ms linear 1; }
@media(prefers-reduced-motion:reduce){.led.pulse{animation:none}}
.readout-label { font-family:'JetBrains Mono',monospace; font-size:1rem; letter-spacing:.1em; text-transform:uppercase; }
.readout-sub { font-family:'JetBrains Mono',monospace; font-size:.72rem; color:var(--ink-dim); margin-left:auto; }
.actions { display:flex; gap:.6rem; flex-wrap:wrap; }
.actions form { margin:0; }
.btn { font-family:'JetBrains Mono',monospace; font-weight:600; font-size:.8rem; letter-spacing:.08em; text-transform:uppercase; padding:.65rem 1.3rem; border:1px solid var(--line); border-radius:2px; background:#1a2016; color:var(--ink); cursor:pointer; transition:filter .12s,transform .05s; }
.btn:hover{filter:brightness(1.25)}.btn:active{transform:translateY(1px)}.btn:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.btn.primary{background:var(--amber-dim);border-color:var(--amber);color:#fff3dc}
.btn.info{background:#16324a;border-color:var(--info);color:#dcefff}
.btn.danger{background:var(--rust-dim);border-color:var(--rust);color:#ffe3dc}
a.link{color:var(--amber);text-decoration:none}a.link:hover{text-decoration:underline}
.note { font-family:'JetBrains Mono',monospace; font-size:.72rem; color:var(--ink-dim); line-height:1.6; margin-top:1.5rem; padding-top:1rem; border-top:1px solid var(--line); }
.banner{font-size:.85rem;margin:0 0 1.25rem;padding:.6rem .9rem;border-radius:2px;border:1px solid currentColor}
.banner.success{color:var(--signal);background:rgba(111,191,90,.08)}
.banner.error{color:var(--rust);background:rgba(194,67,43,.08)}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-family:'JetBrains Mono',monospace;font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-dim);padding:0 .7rem .6rem;border-bottom:1px solid var(--line)}
td{padding:.7rem;border-bottom:1px solid var(--line);font-size:.9rem;vertical-align:middle}
td.mono{font-family:'JetBrains Mono',monospace;font-size:.8rem;color:var(--ink-dim)}
.tag{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:.68rem;letter-spacing:.06em;text-transform:uppercase;padding:.25rem .55rem;border-radius:2px;border:1px solid currentColor;background:transparent;white-space:nowrap;cursor:help}
.tag.ok{color:var(--signal);background:rgba(111,191,90,.08)}.tag.warn{color:var(--amber);background:rgba(255,176,32,.08)}
.tag.info{color:#8fc4ea;background:rgba(58,127,184,.1)}.tag.muted{color:var(--ink-dim);background:rgba(139,145,121,.08)}
.thumb{width:40px;height:40px;object-fit:cover;border-radius:2px;border:1px solid var(--line);display:block}
.thumb-empty{background:#1a2016}.thumb-lg{width:96px;height:96px;object-fit:cover;border-radius:2px;border:1px solid var(--line);flex-shrink:0}
.thumb-btn{background:none;border:none;padding:0;cursor:pointer;display:block}.thumb-btn:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.link-id{color:var(--amber);font-family:'JetBrains Mono',monospace;font-size:.8rem;text-decoration:none}
.link-id:hover{text-decoration:underline}
.lookup-form{display:flex;gap:.6rem;margin-bottom:1.25rem}
.input{font-family:'JetBrains Mono',monospace;background:#0d110a;border:1px solid var(--line);color:var(--ink);padding:.6rem .8rem;border-radius:2px;flex:1}
.input:focus-visible{outline:2px solid var(--amber);outline-offset:1px}
.preview{display:flex;gap:1rem;align-items:center;margin:.5rem 0 1rem}
.preview-title{font-family:'Oswald',sans-serif;font-size:1.15rem;margin:0 0 .25rem;text-transform:none}
code{font-family:'JetBrains Mono',monospace;background:#0d110a;padding:.1rem .35rem;border-radius:2px;font-size:.85em}
.link-remove{color:var(--rust);background:none;border:none;font-family:'JetBrains Mono',monospace;font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;cursor:pointer;padding:0}
.link-remove:hover{text-decoration:underline}
.link-add{color:var(--signal);background:none;border:none;font-family:'JetBrains Mono',monospace;font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;cursor:pointer;padding:0}
.link-add:hover{text-decoration:underline}
.link-title{color:var(--ink);background:none;border:none;padding:0;font:inherit;text-align:left;cursor:pointer}
.link-title:hover{color:var(--amber);text-decoration:underline}
.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(10,13,9,.78);backdrop-filter:blur(4px);align-items:center;justify-content:center;padding:1.25rem;z-index:100}
.modal-backdrop.open{display:flex}
.modal-box{background:var(--panel);border:1px solid var(--modal-accent,var(--rust));border-radius:3px;max-width:440px;width:100%;max-height:85vh;overflow-y:auto;padding:1.75rem}
.modal-thumb{width:100%;max-width:280px;aspect-ratio:1/1;object-fit:cover;border-radius:2px;border:1px solid var(--line);display:block;margin:0 auto 1.1rem}
.modal-eyebrow{font-family:'JetBrains Mono',monospace;font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--modal-accent,var(--rust));margin:0 0 .4rem}
.modal-title{font-family:'Oswald',sans-serif;font-size:1.25rem;margin:0 0 .3rem;text-transform:none}
.modal-meta{font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--ink-dim);margin:0 0 1rem}
.modal-desc{font-size:.85rem;line-height:1.6;color:var(--ink);margin:0 0 1.4rem}
.modal-actions{display:flex;gap:.6rem;justify-content:flex-end}.modal-actions form{margin:0}
.footer-version{text-align:center;font-family:'JetBrains Mono',monospace;font-size:.68rem;color:var(--ink-dim);opacity:.6;margin:1.5rem 0 0}
.console-toolbar{display:flex;gap:.6rem;align-items:center;margin-bottom:.75rem;flex-wrap:wrap}
.console-toolbar .input{flex:1;min-width:140px}
.console-status{font-family:'JetBrains Mono',monospace;font-size:.72rem;margin-left:auto;white-space:nowrap}
.console-path{font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--ink-dim);margin:0 0 .75rem;word-break:break-all}
.console-box{background:#050704;border:1px solid var(--line);border-radius:2px;padding:1rem;height:60vh;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:.76rem;line-height:1.55;color:var(--ink);white-space:pre-wrap;word-break:break-word;margin:0;scrollbar-width:thin;scrollbar-color:var(--amber-dim) var(--void)}
.console-box::-webkit-scrollbar{width:10px}.console-box::-webkit-scrollbar-track{background:var(--void)}
.console-box::-webkit-scrollbar-thumb{background:var(--amber-dim);border-radius:2px;border:2px solid var(--void)}
.console-box::-webkit-scrollbar-thumb:hover{background:var(--amber)}
.lvl-log{color:var(--ink);font-weight:600}.lvl-warn{color:var(--amber);font-weight:700}.lvl-error{color:var(--rust);font-weight:700}
.drag-handle{cursor:grab;color:var(--ink-dim);text-align:center;font-size:1.1rem;user-select:none;width:1.6rem}
.drag-handle:active{cursor:grabbing}tr.dragging{opacity:.35}
.cfg-section{margin-bottom:0;padding-bottom:0;border-bottom:1px solid var(--line)}
.cfg-section:last-child{border-bottom:1px solid var(--line)}
.cfg-section-title{font-family:'Oswald',sans-serif;font-size:1.1rem;letter-spacing:.04em;text-transform:uppercase;margin:0;color:var(--amber);padding:.9rem 0;cursor:pointer;display:flex;align-items:center;justify-content:space-between;user-select:none}
.cfg-section-title:hover{color:#ffc840}
.cfg-section-title .cfg-chevron{font-size:.75rem;transition:transform .2s;display:inline-block}
.cfg-section-title.collapsed .cfg-chevron{transform:rotate(-90deg)}
.cfg-section-body{padding-bottom:1.25rem}
.cfg-group{margin-bottom:1rem;display:flex;flex-direction:column;gap:.35rem}
.cfg-label{font-family:'JetBrains Mono',monospace;font-size:.75rem;color:var(--ink-dim);text-transform:uppercase;letter-spacing:.05em}
.cfg-help{font-family:'JetBrains Mono',monospace;font-size:.68rem;color:var(--ink-dim);opacity:.7;line-height:1.5}
.cfg-disabled{opacity:.35;pointer-events:none}
.toggle-wrap{display:flex;align-items:center;gap:.75rem}
.toggle{position:relative;display:inline-block;width:44px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:#1a2016;border:1px solid var(--line);border-radius:24px;cursor:pointer;transition:background .2s,border-color .2s}
.toggle-slider:before{content:'';position:absolute;left:3px;top:3px;width:16px;height:16px;border-radius:50%;background:var(--ink-dim);transition:transform .2s,background .2s}
.toggle input:checked+.toggle-slider{background:var(--signal-dim);border-color:var(--signal)}
.toggle input:checked+.toggle-slider:before{transform:translateX(20px);background:var(--signal)}
.range-wrap{display:flex;align-items:center;gap:.75rem}
.range-wrap input[type=range]{flex:1;accent-color:var(--amber);background:#1a2016;border-radius:2px}
.range-value{font-family:'JetBrains Mono',monospace;font-size:.85rem;min-width:4rem;text-align:right;background:#0d110a;border:1px solid var(--line);border-radius:2px;padding:.3rem .5rem;color:var(--ink)}
.checkbox-group{display:flex;flex-wrap:wrap;gap:.5rem 1rem}
.checkbox-item{display:flex;align-items:center;gap:.4rem;font-size:.85rem}
.checkbox-item input{accent-color:var(--amber)}
.pw-wrap{position:relative;display:flex}
.pw-wrap .input{flex:1;padding-right:2.5rem}
.pw-eye{position:absolute;right:.6rem;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;color:var(--ink-dim);padding:.2rem;display:flex;align-items:center}
.cfg-save-bar{position:fixed;bottom:0;left:0;right:0;background:var(--panel);border-top:1px solid var(--amber);padding:.75rem 1.5rem;display:flex;align-items:center;justify-content:flex-end;gap:1rem;z-index:50;transform:translateY(100%);transition:transform .2s}
.cfg-save-bar.visible{transform:translateY(0)}
</style>
"""


def load_server_config():
    """Returns (display_name, unit).

    Display name priority:
      1. PublicName key in the server .ini file (the PZ world config)
      2. The server .ini file's own stem (e.g. 'servertest' from servertest.ini)
      3. [server] name in pzpanel.ini (fallback if no server_ini configured)
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


def _load_full_cfg():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


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


def _rcon_quit():
    host, port, password = load_rcon_config()
    from rcon import RCONClient
    try:
        with RCONClient(host, port, password, timeout=5.0) as client:
            client.command("quit")
    except Exception as e:
        log.warning("_rcon_quit: %s", e)


def load_rcon_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return (
        cfg.get("rcon", "host", fallback="127.0.0.1"),
        cfg.getint("rcon", "port", fallback=27015),
        cfg.get("rcon", "password", fallback=""),
    )


def rcon_reachable():
    host, port, password = load_rcon_config()
    try:
        with RCONClient(host, port, password, timeout=2.0):
            return True
    except Exception:
        return False


def _led_style(colour):
    delay = random.uniform(-1.6, 0)
    return f"--led-colour:{colour}; animation-delay:{delay:.2f}s;"


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


SETTINGS_SCHEMA = [
    {"section": "rcon", "key": "host", "label": "RCON Host", "type": "text"},
    {"section": "rcon", "key": "port", "label": "RCON Port", "type": "number"},
    {"section": "rcon", "key": "password", "label": "RCON Password", "type": "password", "sensitive": True},
    {"section": "paths", "key": "server_ini", "label": "World Config (.ini) Path", "type": "text", "assist": "find-ini",
     "help": "The current world's server .ini file. Can be named anything (e.g. servertest.ini, myworld.ini)."},
    {"section": "paths", "key": "workshop_acf", "label": "Workshop ACF Path", "type": "text", "assist": "find-acf"},
    {"section": "paths", "key": "console_log", "label": "Console Log Path", "type": "text", "assist": "derive-console-log"},
    {"section": "paths", "key": "multiplayer_save_dir", "label": "Multiplayer Save Dir", "type": "text", "assist": "derive-save-dir",
     "help": "Enables the Status page Danger Zone. Leave blank to disable."},
    {"section": "discord", "key": "webhook_url", "label": "Discord Webhook URL", "type": "password", "sensitive": True,
     "help": "Needs a panel restart to take effect."},
    {"section": "server", "key": "name", "label": "Server Display Name (fallback)", "type": "text",
     "help": "Used only if the World Config .ini path above is not set. Panel normally reads the name from PublicName in the server .ini."},
    {"section": "server", "key": "unit", "label": "Systemd Unit / Service Name", "type": "text",
     "help": "Linux: systemd unit (e.g. pzserver). Windows sc mode: Windows service name. Not used in process mode."},
    {"section": "server", "key": "windows_mode", "label": "Windows Mode", "type": "text",
     "help": "Windows only: 'sc' (Windows Service) or 'process' (bare process). Ignored on Linux."},
    {"section": "server", "key": "windows_start_script", "label": "Windows Start Script", "type": "text",
     "help": "Windows process mode only: path to the .bat or .exe used to launch the PZ server."},
    {"section": "player_events", "key": "state_file", "label": "Player-Event State File", "type": "text",
     "help": "Needs a panel restart to take effect."},
    {"section": "player_events", "key": "player_db", "label": "Player DB Path", "type": "text",
     "help": "SQLite DB for persistent player/kill tracking. Needs a panel restart to take effect."},
    {"section": "player_events", "key": "poll_interval", "label": "Poll Interval (sec)", "type": "number",
     "help": "Needs a panel restart to take effect."},
    {"section": "player_events", "key": "respawn_window", "label": "Respawn Window (sec)", "type": "number",
     "help": "Needs a panel restart to take effect."},
    {"section": "player_events", "key": "logs_dir", "label": "Logs Dir Override", "type": "text",
     "help": "Needs a panel restart to take effect."},
    {"section": "player_events", "key": "kills_file", "label": "Kills File Path", "type": "text",
     "help": "Needs a panel restart to take effect."},
    {"section": "steam", "key": "enabled", "label": "Steam Enrichment Enabled", "type": "checkbox",
     "help": "Needs a panel restart to take effect."},
    {"section": "steam", "key": "api_key", "label": "Steam Web API Key", "type": "password", "sensitive": True,
     "help": "Needs a panel restart to take effect."},
    {"section": "steam", "key": "cache_hours", "label": "Steam Cache Hours", "type": "number",
     "help": "Needs a panel restart to take effect."},
]


def _read_all_settings():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return {section: dict(cfg[section]) for section in cfg.sections()}


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
    import shutil as _shutil
    _shutil.copy2(str(ini_path), str(backup_path))
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


def page_shell(title, active_tab, body_html, extra_head=""):
    server_name, _ = load_server_config()

    def tab(href, label, key):
        cls = "active" if key == active_tab else ""
        return f'<a href="{href}" class="{cls}">{label}</a>'

    def icon_tab(href, key, title_attr, svg):
        cls = "active icon-tab" if key == active_tab else "icon-tab"
        return (f'<a href="{href}" class="{cls}" title="{html.escape(title_attr)}" '
                f'aria-label="{html.escape(title_attr)}">{svg}</a>')

    cog_svg = (
        '<svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        '<circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="3"/>'
        '<rect id="pzp-cog-tooth" x="10.4" y="3.6" width="3.2" height="3.4" rx="0.6"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(60 12 12)"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(120 12 12)"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(180 12 12)"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(240 12 12)"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(300 12 12)"/>'
        '</svg>'
    )

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>{title}</title>
    {extra_head}
    <link rel="icon" type="image/x-icon" href="/favicon.ico">
    <link rel="icon" type="image/png" sizes="16x16" href="/favicon/favicon-16x16.png">
    <link rel="icon" type="image/png" sizes="32x32" href="/favicon/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="48x48" href="/favicon/favicon-48x48.png">
    <link rel="icon" type="image/png" sizes="64x64" href="/favicon/favicon-64x64.png">
    <link rel="apple-touch-icon" sizes="128x128" href="/favicon/favicon-128x128.png">
    <link rel="apple-touch-icon" sizes="256x256" href="/favicon/favicon-256x256.png">
    {PAGE_STYLE}
</head>
<body>
    <div class="hazard-bar"></div>
    <div class="wrap">
        <p class="eyebrow">{html.escape(server_name)} &middot; Survivor Ops Terminal</p>
        <h1>PZ Panel</h1>
        <nav class="tabs">
            {tab("/", "Status", "status")}
            {tab("/config", "Config", "config")}
            {tab("/mods", "Mod Manifest", "mods")}
            {tab("/killboard", "Killboard", "killboard")}
            {tab("/console", "Console", "console")}
            {tab("/log", "Log", "log")}
            {icon_tab("/settings", "settings", "Settings", cog_svg)}
        </nav>
        <div class="panel">
            {body_html}
        </div>
        <p class="footer-version">pzpanel v{__version__}</p>
    </div>
</body>
</html>"""


def _fmt_ts(ts):
    if ts is None:
        return "\u2014"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_log_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str or "\u2014"


def _fmt_removed_at(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone()
        age = datetime.now().astimezone() - local_dt
        if age <= timedelta(days=7):
            return local_dt.strftime("%a %d %b %Y %H:%M")
        return local_dt.strftime("%d %b %Y")
    except Exception:
        return iso_str or "\u2014"


def _steam_id_link(mod_id):
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod_id}"
    return (f'<a class="link-id" href="{html.escape(url, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">{html.escape(mod_id)}</a>')


def _mod_identity_cells(mod_id, title, preview_url, description, desc_limit=800):
    mod_id = mod_id or ""
    title = title or mod_id
    desc_snippet = (description or "")[:desc_limit]
    data_attrs = (
        f'data-mod-id="{html.escape(mod_id, quote=True)}" '
        f'data-title="{html.escape(title, quote=True)}" '
        f'data-preview="{html.escape(preview_url or "", quote=True)}" '
        f'data-description="{html.escape(desc_snippet, quote=True)}"'
    )
    thumb_html = (
        f'<button type="button" class="thumb-btn" {data_attrs} onclick="openDetailModal(this)">'
        f'{_thumb_html(preview_url)}</button>'
    )
    title_html = (
        f'<button type="button" class="link-title" {data_attrs} onclick="openDetailModal(this)">'
        f'{html.escape(title)}</button>'
    )
    return thumb_html, title_html


def _thumb_html(url, size_class="thumb"):
    if url:
        return f'<img src="{html.escape(url)}" class="{size_class}" alt="">'
    return f'<div class="{size_class} thumb-empty"></div>'


def _mod_status_tag(r):
    note_attr = f' title="{html.escape(r["note"])}"' if r["note"] else ""
    if r["update_available"]:
        return '<span class="tag warn">Update available</span>'
    if r["live_ts"] is None:
        return f'<span class="tag muted"{note_attr}>Lookup failed</span>'
    if r["installed_ts"] is None:
        return f'<span class="tag info"{note_attr}>Not installed</span>'
    if r["note"]:
        return f'<span class="tag muted"{note_attr}>Flagged</span>'
    return '<span class="tag ok">Current</span>'


@app.get("/", response_class=HTMLResponse)
def status_page():
    _, unit = load_server_config()
    state = get_server_state()
    is_running = state in ("online", "starting")
    paused = is_paused()
    label = state.upper()
    led_colour, pulse = {
        "online": ("var(--signal)", True),
        "starting": ("var(--amber)", True),
        "failed": ("var(--rust)", True),
    }.get(state, ("var(--ink-dim)", False))
    pulse_class = "pulse" if pulse else ""
    start_class = "primary" if not is_running else ""
    stop_class = "danger" if is_running else ""
    restart_class = "info" if state == "online" else ""
    automation_tag = ('<span class="tag warn" id="watchdog-tag">Watchdog paused</span>' if paused
                       else '<span class="tag ok" id="watchdog-tag">Watchdog active</span>')
    automation_action = "resume" if paused else "pause"
    automation_btn_label = "Resume" if paused else "Pause"
    automation_btn_class = "primary" if paused else "danger"
    countdown, postponed = _countdown_summary()
    countdown_display_style = "" if countdown else "display:none;"
    countdown_label_text = ""
    countdown_led = "var(--rust)"
    countdown_pause_action, countdown_pause_label, countdown_pause_class = "/countdown/pause", "Pause", ""
    if countdown:
        if countdown["paused"]:
            countdown_label_text = f"Countdown paused at {countdown['remaining_display']}"
            countdown_led = "var(--amber)"
            countdown_pause_action, countdown_pause_label, countdown_pause_class = "/countdown/resume", "Resume", "primary"
        else:
            countdown_label_text = f"Restarting in {countdown['remaining_display']}"
    paused_actions_style = "" if (countdown and countdown["paused"]) else "display:none;"
    postponed_display_style = "" if postponed else "display:none;"
    save_dir = get_multiplayer_save_dir()
    danger_zone_html = ""
    if save_dir is not None:
        save_dir_name = save_dir.name
        danger_zone_html = f"""
            <div class="hazard-frame danger" style="margin:2rem -1.75rem 0;width:calc(100% + 3.5rem);">
                <div class="hazard-frame-inner">
                    <p class="eyebrow" style="color:var(--rust);letter-spacing:.1em;">Danger Zone</p>
                    <p class="note" style="border-top:none;margin-top:0;padding-top:.5rem;">
                        Stops the server and permanently deletes <code>{html.escape(str(save_dir))}</code> --
                        every character, every building, all of it. No undo. Server is left stopped afterward.
                    </p>
                    <div class="actions">
                        <button type="button" class="btn danger" style="margin-left:auto;" onclick="openWipeModal()">Wipe World &amp; Reset</button>
                    </div>
                </div>
            </div>
            <div id="wipe-modal-backdrop" class="modal-backdrop" onclick="if(event.target===this) closeWipeModal()">
                <div class="modal-box" style="--modal-accent:var(--rust);">
                    <p class="modal-eyebrow">Irreversible</p>
                    <p class="modal-title">Wipe world &amp; reset</p>
                    <p class="modal-desc">This stops the server and permanently deletes
                    <code>{html.escape(str(save_dir))}</code>. There is no undo.
                    Type <strong>{html.escape(save_dir_name)}</strong> below to confirm.</p>
                    <form method="post" action="/danger/wipe-saves" id="wipe-form">
                        <input type="text" id="wipe-confirm-input" name="confirm" class="input"
                               style="width:100%;margin-bottom:1.1rem;" autocomplete="off"
                               placeholder="Type '{html.escape(save_dir_name)}' to confirm">
                        <div class="modal-actions">
                            <button type="button" class="btn" onclick="closeWipeModal()">Cancel</button>
                            <button type="submit" class="btn danger" id="wipe-confirm-btn" disabled>Wipe &amp; Reset</button>
                        </div>
                    </form>
                </div>
            </div>
            <script>
            var WIPE_TARGET_NAME={json.dumps(save_dir_name)};
            function openWipeModal(){{document.getElementById('wipe-confirm-input').value='';document.getElementById('wipe-confirm-btn').disabled=true;document.getElementById('wipe-modal-backdrop').classList.add('open');}}
            function closeWipeModal(){{document.getElementById('wipe-modal-backdrop').classList.remove('open');}}
            document.getElementById('wipe-confirm-input').addEventListener('input',function(){{document.getElementById('wipe-confirm-btn').disabled=(this.value!==WIPE_TARGET_NAME);}});
            document.addEventListener('keydown',function(e){{if(e.key==='Escape')closeWipeModal();}});
            </script>"""

    body = f"""
        <div class="readout">
            <span class="led {pulse_class}" id="status-led" style="{_led_style(led_colour)}"></span>
            <span class="readout-label" id="status-label">{label}</span>
            <span class="readout-sub" id="status-unit">{html.escape(unit)}.service</span>
        </div>
        <div class="actions">
            <form method="post" action="/action/start"><button type="submit" class="btn {start_class}" id="btn-start">Start</button></form>
            <form method="post" action="/action/restart"><button type="submit" class="btn {restart_class}" id="btn-restart">Restart</button></form>
            <form method="post" action="/action/stop"><button type="submit" class="btn {stop_class}" id="btn-stop">Stop</button></form>
        </div>
        <p class="note">Restart/stop save gracefully (RCON quit before the process stops).</p>
        <div class="readout" style="margin-top:1.5rem;padding-top:1.25rem;border-top:1px solid var(--line);">
            {automation_tag}
            <form method="post" id="watchdog-form" action="/automation/{automation_action}" style="margin-left:auto;">
                <button type="submit" class="btn {automation_btn_class}" id="watchdog-btn">{automation_btn_label} Watchdog</button>
            </form>
        </div>
        <div id="countdown-block" style="{countdown_display_style}">
            <div class="readout" style="margin-top:1.5rem;padding-top:1.25rem;border-top:1px solid var(--line);">
                <span class="led pulse" id="countdown-led" style="{_led_style(countdown_led)}"></span>
                <span class="readout-label" id="countdown-label">{html.escape(countdown_label_text)}</span>
                <span class="readout-sub" id="countdown-mods">{html.escape(countdown['mods'] if countdown else '')}</span>
            </div>
            <div class="actions">
                <form method="post" id="countdown-pause-form" action="{countdown_pause_action}">
                    <button type="submit" class="btn {countdown_pause_class}" id="countdown-pause-btn">{countdown_pause_label}</button>
                </form>
                <button type="button" class="btn" onclick="openPostponeModal()">Postpone</button>
                <form method="post" id="countdown-cancel-form" action="/countdown/cancel" style="{paused_actions_style}">
                    <button type="submit" class="btn danger">Cancel Timer</button>
                </form>
                <button type="button" class="btn" id="countdown-checkmods-btn" style="{paused_actions_style}" onclick="checkModsWhilePaused()">Check Mods</button>
            </div>
        </div>
        <div id="postponed-block" style="{postponed_display_style}">
            <div class="readout" style="margin-top:1.5rem;padding-top:1.25rem;border-top:1px solid var(--line);">
                <span class="led pulse" style="{_led_style('var(--amber)')}"></span>
                <span class="readout-label" id="postponed-label">{html.escape('Restart scheduled for ' + postponed['label'] if postponed else '')}</span>
                <span class="readout-sub" id="postponed-mods">{html.escape(postponed['mods'] if postponed else '')}</span>
            </div>
            <div class="actions">
                <form method="post" action="/countdown/cancel-postponed">
                    <button type="submit" class="btn danger">Cancel Postponement</button>
                </form>
            </div>
        </div>
        {danger_zone_html}
        <div id="postpone-modal-backdrop" class="modal-backdrop" onclick="if(event.target===this) closePostponeModal()">
            <div class="modal-box" style="--modal-accent:var(--amber);">
                <p class="modal-eyebrow">Postpone restart</p>
                <p class="modal-title">Choose a restart time</p>
                <form method="post" action="/countdown/postpone">
                    <select name="target_epoch" id="postpone-select" class="input" style="width:100%;margin-bottom:1.1rem;"></select>
                    <div class="modal-actions">
                        <button type="button" class="btn" onclick="closePostponeModal()">Cancel</button>
                        <button type="submit" class="btn primary">Confirm Postpone</button>
                    </div>
                </form>
            </div>
        </div>
        <div id="checkmods-modal-backdrop" class="modal-backdrop" onclick="if(event.target===this) closeCheckModsModal()">
            <div class="modal-box" style="--modal-accent:var(--info);">
                <p class="modal-eyebrow">Still pending</p>
                <p class="modal-title">Update(s) still pending</p>
                <ul id="checkmods-list" style="font-size:.85rem;line-height:1.6;color:var(--ink);margin:0 0 1.4rem;padding-left:1.2rem;"></ul>
                <div class="modal-actions">
                    <button type="button" class="btn" onclick="closeCheckModsModal()">Maintain Pause State</button>
                    <button type="button" class="btn primary" onclick="checkModsResume()">Resume Timer</button>
                    <button type="button" class="btn danger" onclick="checkModsCancel()">Cancel Timer</button>
                </div>
            </div>
        </div>
        <script>
        function openPostponeModal(){{fetch('/api/postpone-options').then(function(r){{return r.json();}}).then(function(data){{var select=document.getElementById('postpone-select');select.innerHTML='';data.options.forEach(function(opt){{var el=document.createElement('option');el.value=opt.epoch;el.textContent=opt.label+(opt.tomorrow?' (tomorrow)':'');select.appendChild(el);}});document.getElementById('postpone-modal-backdrop').classList.add('open');}});}}
        function closePostponeModal(){{document.getElementById('postpone-modal-backdrop').classList.remove('open');}}
        function checkModsWhilePaused(){{fetch('/countdown/check-mods',{{method:'POST'}}).then(function(r){{return r.json();}}).then(function(data){{if(data.error){{console.error('check-mods failed',data.error);return;}}if(data.cancelled){{location.reload();return;}}var list=document.getElementById('checkmods-list');list.innerHTML='';data.updates.forEach(function(u){{var li=document.createElement('li');li.textContent=u.title;list.appendChild(li);}});document.getElementById('checkmods-modal-backdrop').classList.add('open');}}).catch(function(err){{console.error('check-mods request failed',err);}});}}
        function closeCheckModsModal(){{document.getElementById('checkmods-modal-backdrop').classList.remove('open');}}
        function checkModsResume(){{fetch('/countdown/resume',{{method:'POST'}}).then(function(){{location.reload();}});}}
        function checkModsCancel(){{fetch('/countdown/cancel',{{method:'POST'}}).then(function(){{location.reload();}});}}
        document.addEventListener('keydown',function(e){{if(e.key==='Escape'){{closePostponeModal();closeCheckModsModal();}}}});
        function refreshStatus(){{fetch('/api/status-full').then(function(r){{return r.json();}}).then(function(data){{var ledColours={{online:'var(--signal)',starting:'var(--amber)',failed:'var(--rust)'}};var led=document.getElementById('status-led');var colour=ledColours[data.state]||'var(--ink-dim)';led.style.setProperty('--led-colour',colour);led.classList.toggle('pulse',!!ledColours[data.state]);document.getElementById('status-label').textContent=data.state.toUpperCase();document.getElementById('status-unit').textContent=data.unit+'.service';var isRunning=(data.state==='online'||data.state==='starting');document.getElementById('btn-start').className='btn'+(!isRunning?' primary':'');document.getElementById('btn-stop').className='btn'+(isRunning?' danger':'');document.getElementById('btn-restart').className='btn'+(data.state==='online'?' info':'');var wdForm=document.getElementById('watchdog-form');var wdBtn=document.getElementById('watchdog-btn');var wdTagParent=document.getElementById('watchdog-tag').parentNode;if(data.watchdog_paused){{wdTagParent.querySelector('#watchdog-tag').outerHTML='<span class="tag warn" id="watchdog-tag">Watchdog paused</span>';wdForm.action='/automation/resume';wdBtn.textContent='Resume Watchdog';wdBtn.className='btn primary';}}else{{wdTagParent.querySelector('#watchdog-tag').outerHTML='<span class="tag ok" id="watchdog-tag">Watchdog active</span>';wdForm.action='/automation/pause';wdBtn.textContent='Pause Watchdog';wdBtn.className='btn danger';}}var cdBlock=document.getElementById('countdown-block');if(data.countdown){{cdBlock.style.display='';document.getElementById('countdown-label').textContent=data.countdown.paused?('Countdown paused at '+data.countdown.remaining_display):('Restarting in '+data.countdown.remaining_display);document.getElementById('countdown-mods').textContent=data.countdown.mods;document.getElementById('countdown-led').style.setProperty('--led-colour',data.countdown.paused?'var(--amber)':'var(--rust)');var pForm=document.getElementById('countdown-pause-form');var pBtn=document.getElementById('countdown-pause-btn');if(data.countdown.paused){{pForm.action='/countdown/resume';pBtn.textContent='Resume';pBtn.className='btn primary';}}else{{pForm.action='/countdown/pause';pBtn.textContent='Pause';pBtn.className='btn';}}var pausedDisplay=data.countdown.paused?'':'none';document.getElementById('countdown-cancel-form').style.display=pausedDisplay;document.getElementById('countdown-checkmods-btn').style.display=pausedDisplay;}}else{{cdBlock.style.display='none';}}var pdBlock=document.getElementById('postponed-block');if(data.postponed){{pdBlock.style.display='';document.getElementById('postponed-label').textContent='Restart scheduled for '+data.postponed.label;document.getElementById('postponed-mods').textContent=data.postponed.mods;}}else{{pdBlock.style.display='none';}}}}).catch(function(err){{console.error('status refresh failed',err);}});}}
        setInterval(refreshStatus,3000);
        (function scheduleLedFlicker(){{var led=document.getElementById('status-led');if(!led)return;function doFlash(cb){{led.classList.remove('dark-flash');void led.offsetWidth;led.classList.add('dark-flash');led.addEventListener('animationend',function onEnd(e){{if(e.animationName!=='led-dark-flash')return;led.removeEventListener('animationend',onEnd);led.classList.remove('dark-flash');if(cb)cb();}},{{once:false}});}}function scheduleNext(){{var gap=5000+Math.random()*9000;setTimeout(function(){{if(!led.classList.contains('pulse')){{scheduleNext();return;}};var isDouble=Math.random()<0.6;if(isDouble){{doFlash(function(){{setTimeout(function(){{if(led.classList.contains('pulse'))doFlash(null);scheduleNext();}},300+Math.random()*300);}});}}else{{doFlash(null);scheduleNext();}}}},gap);}}scheduleNext();}})();
        </script>"""
    return page_shell("PZ Panel", "status", body)


@app.get("/api/status-full")
def api_status_full():
    _, unit = load_server_config()
    countdown, postponed = _countdown_summary()
    return {"state": get_server_state(), "unit": unit, "watchdog_paused": is_paused(),
            "countdown": countdown, "postponed": postponed}


@app.get("/api/postpone-options")
def api_postpone_options():
    return {"options": cc.compute_postpone_options()}


@app.post("/action/{action}")
def do_action(action: str):
    if action not in ALLOWED_ACTIONS:
        return RedirectResponse("/", status_code=303)
    try:
        run_server_action(action)
        log_action("panel", action, "manual (panel button)")
    except subprocess.TimeoutExpired:
        log_action("panel", action, "manual (panel button) -- panel's own wait timed out")
    except Exception as e:
        log.error("server action %s failed: %s", action, e)
    return RedirectResponse("/", status_code=303)


@app.post("/automation/pause")
def automation_pause():
    set_paused(True)
    log_action("panel", "pause-automation", "manual (panel toggle)")
    return RedirectResponse("/", status_code=303)


@app.post("/automation/resume")
def automation_resume():
    postponed = cc.get_postponed_state()
    if postponed and postponed.get("scheduled"):
        cc.send_command("cancel")
        log_action("panel", "resume-automation", "manual (panel toggle) -- also cancelled postponed restart")
    else:
        log_action("panel", "resume-automation", "manual (panel toggle)")
    set_paused(False)
    return RedirectResponse("/", status_code=303)


@app.post("/countdown/pause")
def countdown_pause():
    cc.send_command("pause")
    log_action("panel", "countdown-pause", "manual (panel button)")
    return RedirectResponse("/", status_code=303)


@app.post("/countdown/resume")
def countdown_resume():
    cc.send_command("resume")
    log_action("panel", "countdown-resume", "manual (panel button)")
    return RedirectResponse("/", status_code=303)


@app.post("/countdown/postpone")
def countdown_postpone(target_epoch: str = Form(...)):
    try:
        epoch = int(target_epoch)
    except ValueError:
        return RedirectResponse("/", status_code=303)
    cc.send_command("postpone", target_epoch=epoch)
    local_dt = datetime.fromtimestamp(epoch).astimezone()
    log_action("panel", "countdown-postpone", f"Postponed to {local_dt.strftime('%H:%M')} (manual, panel)")
    return RedirectResponse("/", status_code=303)


@app.post("/countdown/cancel-postponed")
def countdown_cancel_postponed():
    cc.send_command("cancel")
    log_action("panel", "countdown-cancel", "manual (panel) -- cancelled pending postponed restart")
    return RedirectResponse("/", status_code=303)


@app.post("/countdown/cancel")
def countdown_cancel():
    cc.send_command("cancel")
    log_action("panel", "countdown-cancel", "manual (panel button) -- cancelled active countdown")
    return RedirectResponse("/", status_code=303)


@app.post("/countdown/check-mods")
def countdown_check_mods():
    state = cc.get_countdown_state()
    if not state or not state.get("active") or not state.get("paused"):
        return {"error": "no paused countdown to check"}
    try:
        results = check_for_updates()
    except (ModCheckError, SteamWorkshopError) as e:
        return {"error": str(e)}
    updated = [r for r in results if r["update_available"]]
    if not updated:
        cc.send_command("cancel")
        log_action("panel", "countdown-cancel", "manual (panel, check-mods) -- no updates still pending, auto-cancelled")
        return {"cancelled": True}
    log_action("panel", "countdown-check-mods", f"manual (panel) -- {len(updated)} update(s) still pending")
    return {"cancelled": False, "updates": [{"title": m["title"], "mod_id": m["mod_id"]} for m in updated]}


@app.post("/danger/wipe-saves")
def wipe_saves(confirm: str = Form(...)):
    save_dir = get_multiplayer_save_dir()
    if save_dir is None:
        qs = urllib.parse.urlencode({"msg": "No multiplayer_save_dir configured", "msg_type": "error"})
        return RedirectResponse(f"/?{qs}", status_code=303)
    if confirm != save_dir.name:
        qs = urllib.parse.urlencode({"msg": "Confirmation text didn't match -- nothing deleted", "msg_type": "error"})
        return RedirectResponse(f"/?{qs}", status_code=303)
    parts = save_dir.parts
    if "Saves" not in parts or "Multiplayer" not in parts or len(parts) < 4:
        qs = urllib.parse.urlencode({"msg": f"Refusing to delete {save_dir} -- not a Saves/Multiplayer path", "msg_type": "error"})
        return RedirectResponse(f"/?{qs}", status_code=303)
    try:
        run_server_action("stop")
    except subprocess.TimeoutExpired:
        qs = urllib.parse.urlencode({"msg": "Stop timed out -- world NOT wiped", "msg_type": "error"})
        return RedirectResponse(f"/?{qs}", status_code=303)
    log_action("panel", "stop", f"manual (panel) -- stopping to wipe {save_dir}")
    if get_server_state() != "offline":
        qs = urllib.parse.urlencode({"msg": "Server did not stop cleanly -- world NOT wiped", "msg_type": "error"})
        return RedirectResponse(f"/?{qs}", status_code=303)
    if not save_dir.exists():
        qs = urllib.parse.urlencode({"msg": f"{save_dir} doesn't exist (server left stopped)"})
        return RedirectResponse(f"/?{qs}", status_code=303)
    try:
        shutil.rmtree(save_dir)
    except Exception as e:
        qs = urllib.parse.urlencode({"msg": f"Wipe failed: {e}", "msg_type": "error"})
        return RedirectResponse(f"/?{qs}", status_code=303)
    log_action("panel", "wipe-saves", f"manual (panel) -- DELETED {save_dir}")
    qs = urllib.parse.urlencode({"msg": "World wiped. Server left stopped -- start it when ready."})
    return RedirectResponse(f"/?{qs}", status_code=303)


@app.get("/api/status")
def api_status():
    _, unit = load_server_config()
    return {"unit": unit, "state": get_server_state(), "automation_paused": is_paused()}


@app.get("/mods", response_class=HTMLResponse)
def mods_page(request: Request):
    msg = request.query_params.get("msg")
    msg_type = request.query_params.get("msg_type", "success")
    banner_html = f'<p class="banner {msg_type}">{html.escape(msg)}</p>' if msg else ""
    offline = get_server_state() == "offline"
    try:
        results = check_for_updates()
        error = None
    except (ModCheckError, SteamWorkshopError) as e:
        results = []
        error = str(e)
    updates_count = sum(1 for r in results if r["update_available"])
    if error:
        list_html = f'<p style="color:var(--rust);">Could not check mods: {html.escape(error)}</p>'
        reorder_script = ""
    else:
        rows = []
        original_order = []
        for r in results:
            original_order.append(r["mod_id"])
            description = (r.get("description") or "").strip()
            desc_snippet = description[:400]
            thumb_html, title_html = _mod_identity_cells(r['mod_id'], r['title'], r.get('preview_url'), description)
            rows.append(f"""<tr data-mod-id="{html.escape(r['mod_id'], quote=True)}">
                <td class="drag-handle" draggable="true">&#9776;</td>
                <td>{thumb_html}</td><td>{title_html}</td>
                <td>{_steam_id_link(r['mod_id'])}</td>
                <td>{_mod_status_tag(r)}</td>
                <td><button type="button" class="link-remove"
                    data-mod-id="{html.escape(r['mod_id'], quote=True)}"
                    data-title="{html.escape(r['title'], quote=True)}"
                    data-preview="{html.escape(r.get('preview_url') or '', quote=True)}"
                    data-description="{html.escape(desc_snippet, quote=True)}"
                    onclick="openRemoveModal(this)">Remove</button></td>
            </tr>""")
        mod_word = "mod" if len(results) == 1 else "mods"
        update_word = "update" if updates_count == 1 else "updates"
        list_html = f"""<p class="eyebrow" style="margin-bottom:.75rem;letter-spacing:.06em;">Current Mods &amp; Load Order</p>
        <table id="mods-table">
            <tr><th></th><th></th><th>Mod</th><th>ID</th><th>Status</th><th></th></tr>
            <tbody id="mods-tbody">{"".join(rows)}</tbody>
        </table>
        <div id="reorder-apply-wrap" style="display:none;width:92%;margin:1rem auto 0;">
            <form method="post" action="/mods/reorder" id="reorder-form">
                <input type="hidden" name="order" id="reorder-order-input" value="">
                <button type="submit" class="btn primary" style="width:100%;">Apply New Order &amp; Restart Server</button>
            </form>
        </div>
        <p class="note">{len(results)} {mod_word} checked &middot; {updates_count} {update_word} available.</p>"""
        reorder_script = f"""<script>(function(){{var tbody=document.getElementById('mods-tbody');if(!tbody)return;var originalOrder={json.dumps(original_order)};var dragSrc=null;function getCurrentOrder(){{return Array.prototype.map.call(tbody.querySelectorAll('tr[data-mod-id]'),function(tr){{return tr.dataset.modId;}});}}function updateReorderVisibility(){{var current=getCurrentOrder();var changed=current.length!==originalOrder.length||current.some(function(id,i){{return id!==originalOrder[i];}});var wrap=document.getElementById('reorder-apply-wrap');if(wrap)wrap.style.display=changed?'':'none';var input=document.getElementById('reorder-order-input');if(input)input.value=JSON.stringify(current);}}Array.prototype.forEach.call(tbody.querySelectorAll('.drag-handle'),function(handle){{handle.addEventListener('dragstart',function(e){{dragSrc=handle.closest('tr');e.dataTransfer.effectAllowed='move';dragSrc.classList.add('dragging');}});handle.addEventListener('dragend',function(){{if(dragSrc)dragSrc.classList.remove('dragging');dragSrc=null;updateReorderVisibility();}});}});Array.prototype.forEach.call(tbody.querySelectorAll('tr[data-mod-id]'),function(row){{row.addEventListener('dragover',function(e){{if(!dragSrc||dragSrc===row)return;e.preventDefault();var rect=row.getBoundingClientRect();var after=(e.clientY-rect.top)/(rect.bottom-rect.top)>0.5;tbody.insertBefore(dragSrc,after?row.nextSibling:row);}});}});}})();</script>"""

    add_mod_html = '<p style="text-align:right;margin:1rem 0 0;"><a href="/mods/add" class="btn primary">+ Add Mod</a></p>'
    removed_entries = removed_mods.get_removed_mods()
    if removed_entries:
        removed_rows = []
        for e in removed_entries:
            title = e.get("title") or e.get("mod_id", "")
            mod_id = e.get("mod_id", "")
            thumb_html, title_html = _mod_identity_cells(mod_id, title, e.get('preview_url'), "")
            removed_rows.append(f"""<tr>
                <td>{thumb_html}</td><td>{title_html}</td>
                <td>{_steam_id_link(mod_id)}</td>
                <td class="mono">{_fmt_removed_at(e.get('removed_at'))}</td>
                <td style="white-space:nowrap;">
                    <button type="button" class="link-add"
                        data-mod-id="{html.escape(mod_id, quote=True)}"
                        data-title="{html.escape(title, quote=True)}"
                        data-preview="{html.escape(e.get('preview_url') or '', quote=True)}"
                        onclick="openReaddModal(this)">Re-add</button>
                    &nbsp;<button type="button" class="link-remove"
                        onclick="deletePreviouslyRemoved('{html.escape(mod_id, quote=True)}',this)">Remove from list</button>
                </td>
            </tr>""")
        removed_count = len(removed_entries)
        removed_mod_word = "Mod" if removed_count == 1 else "Mods"
        removed_html = f"""<div style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--line);">
            <p class="eyebrow" style="margin-bottom:.75rem;letter-spacing:.06em;text-align:center;">Previously Removed {removed_mod_word}</p>
            <table><tr><th></th><th>Mod</th><th>ID</th><th>Removed</th><th></th></tr>{"".join(removed_rows)}</table>
            <p class="note">{removed_count} {removed_mod_word} available in "previously used list"</p>
        </div>"""
    else:
        removed_html = ""

    modal_html = f"""
        <div id="modal-backdrop" class="modal-backdrop" onclick="if(event.target===this) closeRemoveModal()">
            <div class="modal-box" style="--modal-accent:var(--rust);"><p class="modal-eyebrow">Confirm removal</p>
                <img id="modal-thumb" class="modal-thumb" src="" alt="" style="display:none;">
                <p class="modal-title" id="modal-title"></p><p class="modal-meta" id="modal-id"></p>
                <p class="modal-desc" id="modal-desc"></p>
                <p class="modal-desc" id="modal-online-note" style="display:none;color:var(--amber);"></p>
                <div class="modal-actions"><button type="button" class="btn" onclick="closeRemoveModal()">Cancel</button>
                    <form method="post" id="modal-confirm-form" action="/mods/remove">
                        <input type="hidden" id="modal-mod-id-input" name="mod_id" value="">
                        <input type="hidden" id="modal-title-input" name="title" value="">
                        <input type="hidden" id="modal-preview-input" name="preview_url" value="">
                        <button type="submit" class="btn danger" id="modal-confirm-btn">Confirm Remove</button>
                    </form></div></div></div>
        <div id="detail-modal-backdrop" class="modal-backdrop" onclick="if(event.target===this) closeDetailModal()">
            <div class="modal-box" style="--modal-accent:var(--amber);"><p class="modal-eyebrow">Mod details</p>
                <img id="detail-thumb" class="modal-thumb" src="" alt="" style="display:none;">
                <p class="modal-title" id="detail-title"></p><p class="modal-meta" id="detail-meta"></p>
                <p class="modal-desc" id="detail-desc"></p>
                <div class="modal-actions"><button type="button" class="btn" onclick="closeDetailModal()">Close</button></div></div></div>
        <div id="readd-modal-backdrop" class="modal-backdrop" onclick="if(event.target===this) closeReaddModal()">
            <div class="modal-box" style="--modal-accent:var(--signal);"><p class="modal-eyebrow">Confirm re-add</p>
                <img id="readd-thumb" class="modal-thumb" src="" alt="" style="display:none;">
                <p class="modal-title" id="readd-title"></p><p class="modal-meta" id="readd-id"></p>
                <p class="modal-desc" id="readd-online-note" style="display:none;color:var(--amber);"></p>
                <div class="modal-actions"><button type="button" class="btn" onclick="closeReaddModal()">Cancel</button>
                    <form method="post" id="readd-confirm-form" action="/mods/readd">
                        <input type="hidden" id="readd-mod-id-input" name="mod_id" value="">
                        <button type="submit" class="btn primary" id="readd-confirm-btn">Confirm Re-add</button>
                    </form></div></div></div>
        <script>
        var SERVER_ONLINE={str(not offline).lower()};
        function openRemoveModal(btn){{var d=btn.dataset;var thumb=document.getElementById('modal-thumb');if(d.preview){{thumb.src=d.preview;thumb.style.display='block';}}else{{thumb.style.display='none';}}document.getElementById('modal-title').textContent=d.title;document.getElementById('modal-id').textContent='Workshop ID '+d.modId;document.getElementById('modal-desc').textContent=d.description||'(no description)';document.getElementById('modal-mod-id-input').value=d.modId;document.getElementById('modal-title-input').value=d.title;document.getElementById('modal-preview-input').value=d.preview||'';var noteEl=document.getElementById('modal-online-note');var form=document.getElementById('modal-confirm-form');var confirmBtn=document.getElementById('modal-confirm-btn');if(SERVER_ONLINE){{noteEl.textContent='Server is online. Removing will stop, apply, and restart.';noteEl.style.display='block';form.action='/mods/remove-with-restart';confirmBtn.textContent='Restart & Remove';}}else{{noteEl.style.display='none';form.action='/mods/remove';confirmBtn.textContent='Confirm Remove';}}document.getElementById('modal-backdrop').classList.add('open');}}
        function closeRemoveModal(){{document.getElementById('modal-backdrop').classList.remove('open');}}
        function openDetailModal(btn){{var d=btn.dataset;var thumb=document.getElementById('detail-thumb');if(d.preview){{thumb.src=d.preview;thumb.style.display='block';}}else{{thumb.style.display='none';}}document.getElementById('detail-title').textContent=d.title;document.getElementById('detail-meta').textContent='Workshop ID '+d.modId;document.getElementById('detail-desc').textContent=d.description||'(no description)';document.getElementById('detail-modal-backdrop').classList.add('open');}}
        function closeDetailModal(){{document.getElementById('detail-modal-backdrop').classList.remove('open');}}
        function openReaddModal(btn){{var d=btn.dataset;var thumb=document.getElementById('readd-thumb');if(d.preview){{thumb.src=d.preview;thumb.style.display='block';}}else{{thumb.style.display='none';}}document.getElementById('readd-title').textContent=d.title;document.getElementById('readd-id').textContent='Workshop ID '+d.modId;document.getElementById('readd-mod-id-input').value=d.modId;var noteEl=document.getElementById('readd-online-note');var form=document.getElementById('readd-confirm-form');var confirmBtn=document.getElementById('readd-confirm-btn');if(SERVER_ONLINE){{noteEl.textContent='Server is online. Re-adding will stop, apply, and restart.';noteEl.style.display='block';form.action='/mods/readd-with-restart';confirmBtn.textContent='Restart & Re-add';}}else{{noteEl.style.display='none';form.action='/mods/readd';confirmBtn.textContent='Confirm Re-add';}}document.getElementById('readd-modal-backdrop').classList.add('open');}}
        function closeReaddModal(){{document.getElementById('readd-modal-backdrop').classList.remove('open');}}
        document.addEventListener('keydown',function(e){{if(e.key==='Escape'){{closeRemoveModal();closeDetailModal();closeReaddModal();}}}});
        function deletePreviouslyRemoved(modId,btn){{if(!confirm('Remove "'+modId+'" from the previously-removed list?'))return;btn.disabled=true;btn.textContent='Removing\u2026';fetch('/mods/removed/delete',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'mod_id='+encodeURIComponent(modId)}}).then(function(r){{return r.json();}}).then(function(data){{if(data.ok){{var row=btn.closest('tr');if(row)row.remove();}}else{{btn.disabled=false;btn.textContent='Remove from list';alert('Failed: '+(data.error||'unknown error'));}}}}).catch(function(err){{btn.disabled=false;btn.textContent='Remove from list';alert('Request failed: '+err);}});}}
        </script>"""

    body = f"{banner_html}{list_html}{add_mod_html}{removed_html}{modal_html}{reorder_script}"
    return page_shell("PZ Panel &mdash; Mods", "mods", body)


@app.get("/api/mods")
def api_mods():
    try:
        return {"ok": True, "mods": check_for_updates()}
    except (ModCheckError, SteamWorkshopError) as e:
        return {"ok": False, "error": str(e)}


@app.get("/mods/add", response_class=HTMLResponse)
def add_mod_page(request: Request, lookup: str = ""):
    lookup = lookup.strip()
    preview_html = ""
    msg = request.query_params.get("msg")
    msg_type = request.query_params.get("msg_type", "error")
    banner_html = f'<p class="banner {msg_type}">{html.escape(msg)}</p>' if msg else ""
    if lookup:
        if not lookup.isdigit():
            preview_html = '<p style="color:var(--rust);">Workshop ID must be numeric.</p>'
        else:
            try:
                details = get_mod_details([lookup])
            except SteamWorkshopError as e:
                preview_html = f'<p style="color:var(--rust);">Lookup failed: {html.escape(str(e))}</p>'
            else:
                info = details.get(lookup)
                if not info or info.get("result") != 1:
                    preview_html = '<p style="color:var(--rust);">No mod found for that Workshop ID.</p>'
                else:
                    description = (info.get("description") or "").strip()
                    desc_snippet = description[:400]
                    ellipsis = "\u2026" if len(description) > 400 else ""
                    preview_html = f"""
                        <div class="preview">
                            {_thumb_html(info.get('preview_url'), 'thumb-lg')}
                            <div>
                                <p class="preview-title">{html.escape(info['title'])}</p>
                                <p class="mono" style="color:var(--ink-dim);margin:0;">ID {lookup}</p>
                            </div>
                        </div>
                        <p class="note" style="border-top:none;margin-top:0;padding-top:0;">
                            {html.escape(desc_snippet)}{ellipsis}
                        </p>
                        <form method="post" action="/mods/add/confirm">
                            <input type="hidden" name="mod_id" value="{lookup}">
                            <button type="submit" class="btn primary">Confirm Add</button>
                        </form>
                    """
    offline = get_server_state() == "offline"
    warn_html = "" if offline else '<p class="banner error">Server must be stopped before adding a mod.</p>'
    body = f"""
        {banner_html}
        <form method="get" action="/mods/add" class="lookup-form">
            <input type="text" name="lookup" placeholder="Workshop ID" value="{html.escape(lookup)}" class="input" required>
            <button type="submit" class="btn">Look Up</button>
        </form>
        {warn_html}
        {preview_html}
        <p class="note"><a class="link" href="/mods">&larr; Back to manifest</a></p>
    """
    return page_shell("PZ Panel &mdash; Add Mod", "mods", body)


@app.post("/mods/add/confirm")
def add_mod_confirm(mod_id: str = Form(...)):
    if get_server_state() != "offline":
        qs = urllib.parse.urlencode({"lookup": mod_id, "msg": "Server must be stopped first", "msg_type": "error"})
        return RedirectResponse(f"/mods/add?{qs}", status_code=303)
    try:
        added = add_workshop_item(mod_id)
    except ModCheckError as e:
        qs = urllib.parse.urlencode({"msg": str(e), "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    removed_mods.clear_removed(mod_id)
    msg = "Added to WorkshopItems" if added else "Already in WorkshopItems"
    qs = urllib.parse.urlencode({"msg": msg})
    return RedirectResponse(f"/mods?{qs}", status_code=303)


@app.post("/mods/remove")
def remove_mod_confirm(mod_id: str = Form(...), title: str = Form(""), preview_url: str = Form("")):
    if get_server_state() != "offline":
        qs = urllib.parse.urlencode({"msg": "Server must be stopped first", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    try:
        removed = remove_workshop_item(mod_id)
    except ModCheckError as e:
        qs = urllib.parse.urlencode({"msg": str(e), "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    if removed:
        removed_mods.record_removed(mod_id, title or mod_id, preview_url or None)
    msg = "Removed from WorkshopItems" if removed else "That mod ID wasn't in WorkshopItems"
    qs = urllib.parse.urlencode({"msg": msg})
    return RedirectResponse(f"/mods?{qs}", status_code=303)


@app.post("/mods/remove-with-restart")
def remove_mod_with_restart(mod_id: str = Form(...), title: str = Form(""), preview_url: str = Form("")):
    try:
        run_server_action("stop")
    except subprocess.TimeoutExpired:
        qs = urllib.parse.urlencode({"msg": "Stop timed out -- mod NOT removed", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    log_action("panel", "stop", f"manual (panel) -- stopping to remove mod {mod_id}")
    if get_server_state() != "offline":
        qs = urllib.parse.urlencode({"msg": "Server did not stop cleanly -- mod NOT removed", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    try:
        removed = remove_workshop_item(mod_id)
    except ModCheckError as e:
        qs = urllib.parse.urlencode({"msg": f"Remove failed: {e} -- server left stopped", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    if removed:
        removed_mods.record_removed(mod_id, title or mod_id, preview_url or None)
    try:
        run_server_action("start")
        log_action("panel", "start", f"manual (panel) -- restarting after removing mod {mod_id}")
    except subprocess.TimeoutExpired:
        pass
    msg = "Removed and restarting" if removed else "That mod ID wasn't in WorkshopItems -- restarting anyway"
    qs = urllib.parse.urlencode({"msg": msg})
    return RedirectResponse(f"/mods?{qs}", status_code=303)


@app.post("/mods/readd")
def readd_mod_confirm(mod_id: str = Form(...)):
    if get_server_state() != "offline":
        qs = urllib.parse.urlencode({"msg": "Server must be stopped first", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    try:
        added = add_workshop_item(mod_id)
    except ModCheckError as e:
        qs = urllib.parse.urlencode({"msg": str(e), "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    removed_mods.clear_removed(mod_id)
    msg = "Re-added to WorkshopItems" if added else "Already in WorkshopItems"
    qs = urllib.parse.urlencode({"msg": msg})
    return RedirectResponse(f"/mods?{qs}", status_code=303)


@app.post("/mods/readd-with-restart")
def readd_mod_with_restart(mod_id: str = Form(...)):
    try:
        run_server_action("stop")
    except subprocess.TimeoutExpired:
        qs = urllib.parse.urlencode({"msg": "Stop timed out -- mod NOT added back", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    log_action("panel", "stop", f"manual (panel) -- stopping to add back mod {mod_id}")
    if get_server_state() != "offline":
        qs = urllib.parse.urlencode({"msg": "Server did not stop cleanly -- mod NOT added back", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    try:
        added = add_workshop_item(mod_id)
    except ModCheckError as e:
        qs = urllib.parse.urlencode({"msg": f"Add failed: {e} -- server left stopped", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    removed_mods.clear_removed(mod_id)
    try:
        run_server_action("start")
        log_action("panel", "start", f"manual (panel) -- restarting after adding back mod {mod_id}")
    except subprocess.TimeoutExpired:
        pass
    msg = "Re-added and restarting" if added else "Already in WorkshopItems -- restarting anyway"
    qs = urllib.parse.urlencode({"msg": msg})
    return RedirectResponse(f"/mods?{qs}", status_code=303)


@app.post("/mods/removed/delete")
def delete_from_removed(mod_id: str = Form(...)):
    try:
        removed_mods.clear_removed(mod_id)
        log_action("panel", "delete-from-removed",
                   f"manual (panel) -- removed {mod_id} from previously-removed list")
        return {"ok": True}
    except Exception as e:
        log.exception("Failed to remove %s from previously-removed list", mod_id)
        return {"ok": False, "error": str(e)}


@app.post("/mods/reorder")
def reorder_mods(order: str = Form(...)):
    try:
        new_order = json.loads(order)
        if not isinstance(new_order, list) or not all(isinstance(x, str) for x in new_order):
            raise ValueError("bad shape")
    except (ValueError, TypeError):
        qs = urllib.parse.urlencode({"msg": "Invalid reorder payload", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    try:
        run_server_action("stop")
    except subprocess.TimeoutExpired:
        qs = urllib.parse.urlencode({"msg": "Stop timed out -- order NOT applied", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    log_action("panel", "stop", "manual (panel) -- stopping to apply new mod order")
    if get_server_state() != "offline":
        qs = urllib.parse.urlencode({"msg": "Server did not stop cleanly -- order NOT applied", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    try:
        reorder_workshop_items(new_order)
    except ModCheckError as e:
        qs = urllib.parse.urlencode({"msg": f"Reorder failed: {e} -- server left stopped", "msg_type": "error"})
        return RedirectResponse(f"/mods?{qs}", status_code=303)
    log_action("panel", "reorder-mods", "manual (panel) -- applied new WorkshopItems order")
    try:
        run_server_action("start")
        log_action("panel", "start", "manual (panel) -- restarting after reordering mods")
    except subprocess.TimeoutExpired:
        pass
    qs = urllib.parse.urlencode({"msg": "New mod order applied -- restarting"})
    return RedirectResponse(f"/mods?{qs}", status_code=303)


@app.get("/log", response_class=HTMLResponse)
def log_page():
    entries = read_recent(200)
    if not entries:
        body_inner = '<p class="note">No actions logged yet.</p>'
    else:
        rows = []
        for e in entries:
            actor = e.get("actor", "?")
            actor_tag = ('<span class="tag info">Panel</span>' if actor == "panel"
                         else '<span class="tag warn">Modcheck</span>' if actor == "modcheck"
                         else f'<span class="tag muted">{html.escape(actor)}</span>')
            rows.append(f"""<tr>
                <td class="mono">{html.escape(_fmt_log_time(e.get('time', '')))}</td>
                <td>{actor_tag}</td>
                <td class="mono">{html.escape(e.get('action', ''))}</td>
                <td>{html.escape(e.get('reason', ''))}</td>
            </tr>""")
        action_word = "action" if len(entries) == 1 else "actions"
        body_inner = f"""<table>
            <tr><th>Time</th><th>Source</th><th>Action</th><th>Reason</th></tr>
            {"".join(rows)}
        </table>
        <p class="note">Showing the last {len(entries)} {action_word}.</p>"""
    return page_shell("PZ Panel &mdash; Log", "log", body_inner)


@app.get("/killboard", response_class=HTMLResponse)
def killboard_page():
    if _player_event_watcher is None:
        body = '<p class="note">Player-event tracking isn\'t running -- check that pzpanel.ini was readable at startup.</p>'
        return page_shell("PZ Panel &mdash; Killboard", "killboard", body)
    db = _player_event_watcher.db
    if db is not None:
        accounts = db.get_killboard()
        if not accounts:
            table_html = '<p class="note">No player data yet -- waiting for the first connection to be recorded.</p>'
        else:
            account_blocks = []
            for acc in accounts:
                steamid = acc["steamid"]
                steam_name = html.escape(acc["steam_name"] or steamid)
                profile_url = f"https://steamcommunity.com/profiles/{steamid}"
                avatar_html = ""
                if acc.get("avatar_url"):
                    avatar_html = (f'<img src="{html.escape(acc["avatar_url"])}" '
                                   f'class="thumb" style="vertical-align:middle;margin-right:.5rem;" alt="">')
                hours_note = ""
                if acc.get("pz_hours") is not None:
                    hours_note = f'<span class="readout-sub" style="margin-left:.5rem;">{acc["pz_hours"]:,} hrs in PZ</span>'
                char_rows = []
                for c in acc["characters"]:
                    run_str = str(c["current_run_kills"]) if c["current_run_kills"] else "\u2014"
                    char_rows.append(f"""<tr>
                        <td style="padding-left:2rem;">{html.escape(c['username'])}</td>
                        <td class="mono">{run_str}</td>
                        <td class="mono">{c['char_kills']}</td>
                    </tr>""")
                account_blocks.append(f"""
                    <tr style="background:rgba(255,176,32,.04);">
                        <td colspan="3" style="padding:.7rem;">
                            {avatar_html}<a href="{html.escape(profile_url, quote=True)}"
                            target="_blank" rel="noopener noreferrer"
                            class="link-id" style="font-size:1rem;">{steam_name}</a>
                            {hours_note}
                            <span class="tag ok" style="margin-left:.75rem;">{acc['account_kills']} lifetime</span>
                        </td>
                    </tr>
                    {"".join(char_rows)}
                """)
            table_html = f"""<table>
                <tr><th>Player / Character</th><th>Kills (current run)</th><th>Kills (lifetime)</th></tr>
                {"".join(account_blocks)}
            </table>"""
        body = f"""
            {table_html}
            <p class="note">Grouped by Steam account. Lifetime kills accumulate across all characters
            and runs tracked by PZP. The mod file is rewritten roughly every 60 seconds so live
            kill counts can lag by up to that long.</p>
        """
        return page_shell("PZ Panel &mdash; Killboard", "killboard", body)
    if _player_event_watcher.kills_file is None:
        body = '<p class="note">No kills_file configured under <code>[player_events]</code> in pzpanel.ini.</p>'
        return page_shell("PZ Panel &mdash; Killboard", "killboard", body)
    st = _player_event_watcher.state.data
    current = st.get("kills_current_run", {})
    lifetime = st.get("kills_lifetime", {})
    last_updated = st.get("kills_last_updated")
    names = sorted(set(current) | set(lifetime), key=lambda n: current.get(n, 0), reverse=True)
    if not names:
        table_html = '<p class="note">No kill data yet.</p>'
    else:
        rows = []
        for n in names:
            rows.append(f"""<tr><td>{html.escape(n)}</td><td class="mono">{current.get(n, '\u2014')}</td><td class="mono">{lifetime.get(n, 0)}</td></tr>""")
        table_html = f"""<table><tr><th>Player</th><th>Kills (current run)</th><th>Kills (lifetime)</th></tr>{"".join(rows)}</table>"""
    updated_note = f'Last updated {_fmt_ts(last_updated)}.' if last_updated else 'Not yet updated.'
    body = f"{table_html}<p class=\"note\">(In-memory fallback.) {updated_note}</p>"
    return page_shell("PZ Panel &mdash; Killboard", "killboard", body)


async def _tail_file_events(path, request=None, poll_interval=0.5, initial_lines=200):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[-initial_lines:]:
                yield line
    except FileNotFoundError:
        yield f"[pzpanel] {path} does not exist yet -- waiting for it to appear\n"
    last_inode = None
    last_pos = 0
    try:
        last_inode = pc.file_identity(path)
        last_pos = os.stat(path).st_size
    except FileNotFoundError:
        pass
    while True:
        if _shutting_down.is_set():
            return
        if request is not None and await request.is_disconnected():
            return
        await asyncio.sleep(poll_interval)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        current_identity = pc.file_identity(path)
        if last_inode is not None and current_identity != last_inode:
            last_pos = 0
            yield "[pzpanel] --- log file replaced (server restarted) ---\n"
        last_inode = current_identity
        if st.st_size < last_pos:
            last_pos = 0
        if st.st_size > last_pos:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(last_pos)
                new_data = f.read()
                last_pos = f.tell()
            for line in new_data.splitlines(keepends=True):
                yield line


@app.get("/console", response_class=HTMLResponse)
def console_page():
    path = load_console_log_path()
    body = f"""
        <p class="console-path">{html.escape(path or '(no console_log configured)')}</p>
        <div class="console-toolbar">
            <input type="text" id="filter-input" class="input" placeholder="Filter (client-side)" oninput="renderConsole()">
            <button type="button" class="btn" id="pause-btn" onclick="toggleConsolePause()">Pause</button>
            <button type="button" class="btn" onclick="clearConsole()">Clear</button>
            <span class="console-status" id="stream-status" style="color:var(--signal);">&#9679; STREAMING</span>
        </div>
        <div id="console-output" class="console-box"></div>
        <script>
        (function(){{
            var paused=false,buffer=[],MAX_LINES=3000;
            var output=document.getElementById('console-output'),statusEl=document.getElementById('stream-status'),filterEl=document.getElementById('filter-input'),pauseBtn=document.getElementById('pause-btn'),atBottom=true;
            output.addEventListener('scroll',function(){{atBottom=(output.scrollHeight-output.scrollTop-output.clientHeight)<40;}});
            function escapeHtml(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
            function colorizeLine(line){{var escaped=escapeHtml(line);var m=line.match(/^(ERROR|WARN|LOG)/);if(!m)return escaped;var level=m[1];var cls=level==='ERROR'?'lvl-error':level==='WARN'?'lvl-warn':'lvl-log';var idx=escaped.indexOf(level);if(idx===-1)return escaped;return escaped.slice(0,idx)+'<span class="'+cls+'">'+level+'</span>'+escaped.slice(idx+level.length);}}
            function appendLine(line){{var div=document.createElement('div');div.innerHTML=colorizeLine(line);output.appendChild(div);while(output.childElementCount>MAX_LINES){{output.removeChild(output.firstChild);}}if(atBottom)output.scrollTop=output.scrollHeight;}}
            var source=new EventSource('/console/stream');
            source.onmessage=function(e){{buffer.push(e.data);if(buffer.length>MAX_LINES)buffer.shift();if(paused)return;var filter=filterEl.value.toLowerCase();if(!filter||e.data.toLowerCase().indexOf(filter)!==-1){{appendLine(e.data);}}}}
            source.onerror=function(){{statusEl.textContent='\u25CF DISCONNECTED';statusEl.style.color='var(--rust)';}}
            window.renderConsole=function(){{var filter=filterEl.value.toLowerCase();var lines=filter?buffer.filter(function(l){{return l.toLowerCase().indexOf(filter)!==-1;}}):buffer;output.innerHTML='';var frag=document.createDocumentFragment();lines.forEach(function(l){{var div=document.createElement('div');div.innerHTML=colorizeLine(l);frag.appendChild(div);}});output.appendChild(frag);if(atBottom)output.scrollTop=output.scrollHeight;}}
            window.toggleConsolePause=function(){{paused=!paused;pauseBtn.textContent=paused?'Resume':'Pause';if(!paused){{statusEl.textContent='\u25CF STREAMING';statusEl.style.color='var(--signal)';renderConsole();}}else{{statusEl.textContent='\u25CF PAUSED';statusEl.style.color='var(--amber)';}}}}
            window.clearConsole=function(){{buffer=[];renderConsole();}}
        }})();
        </script>
    """
    return page_shell("PZ Panel &mdash; Console", "console", body)


@app.get("/console/stream")
async def console_stream(request: Request):
    path = load_console_log_path()
    if not path:
        async def empty():
            yield "data: [pzpanel] console_log not configured\n\n"
        return StreamingResponse(empty(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    async def event_generator():
        async for line in _tail_file_events(path, request=request):
            safe = line.rstrip("\n").replace("\r", "")
            yield f"data: {safe}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/settings/find-file")
def settings_find_file(type: str = "ini"):
    if type == "ini":
        candidates = _find_candidate_files("ini", ["WorkshopItems", "PVP=", "DefaultPort"])
    elif type == "acf":
        candidates = _find_candidate_files("acf", ['"appid"', '"108600"'],
                                           search_roots=pc.default_acf_search_roots())
    elif type == "console_log":
        candidates = _find_exact_named_files("server-console.txt")
    else:
        return {"candidates": [], "error": "unknown search type"}
    return {"candidates": candidates}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    msg = request.query_params.get("msg")
    msg_type = request.query_params.get("msg_type", "success")
    banner_html = f'<p class="banner {msg_type}">{html.escape(msg)}</p>' if msg else ""
    current = _read_all_settings()
    sections_seen = []
    rows_by_section = {}
    for field in SETTINGS_SCHEMA:
        sec = field["section"]
        if sec not in rows_by_section:
            rows_by_section[sec] = []
            sections_seen.append(sec)
        current_val = current.get(sec, {}).get(field["key"], "")
        field_name = f"{sec}__{field['key']}"
        if field.get("sensitive"):
            is_set = bool(current_val) and current_val != "CHANGEME"
            placeholder = "Click to change" if is_set else "Not set"
            input_html = (f'<input type="password" name="{html.escape(field_name)}" class="input" '
                          f'placeholder="{html.escape(placeholder)}" autocomplete="new-password">')
        elif field["type"] == "checkbox":
            checked = "checked" if current_val.strip().lower() != "false" else ""
            input_html = f'<input type="checkbox" name="{html.escape(field_name)}" value="true" {checked}>'
        else:
            input_html = (f'<input type="text" name="{html.escape(field_name)}" class="input" '
                          f'value="{html.escape(current_val)}">'
                          )
        assist = field.get("assist")
        assist_html = ""
        if assist in ("find-ini", "find-acf"):
            search_type = "ini" if assist == "find-ini" else "acf"
            assist_html = (f'<button type="button" class="btn" style="margin-top:.4rem;" '
                           f'onclick="pzpFindFile(\'{field_name}\', \'{search_type}\')">Find</button>'
                           f'<div id="{field_name}-find-results" style="display:none;margin-top:.4rem;"></div>')
        elif assist == "derive-console-log":
            assist_html = (f'<button type="button" class="btn" style="margin-top:.4rem;" '
                           f'onclick="pzpDeriveConsoleLog(\'{field_name}\')">Derive from .ini</button> '
                           f'<button type="button" class="btn" style="margin-top:.4rem;" '
                           f'onclick="pzpFindFile(\'{field_name}\', \'console_log\')">Find</button>'
                           f'<div id="{field_name}-find-results" style="display:none;margin-top:.4rem;"></div>')
        elif assist == "derive-save-dir":
            assist_html = (f'<button type="button" class="btn" style="margin-top:.4rem;" '
                           f'onclick="pzpDeriveSaveDir(\'{field_name}\')">Derive from .ini</button>')
        help_html = (f'<p class="note" style="margin-top:.3rem;padding-top:0;border-top:none;">'
                    f'{html.escape(field["help"])}</p>' if field.get("help") else "")
        rows_by_section[sec].append(f"""
            <div style="margin-bottom:1.1rem;">
                <label style="display:block;font-family:'JetBrains Mono',monospace;font-size:.75rem;
                    color:var(--ink-dim);margin-bottom:.3rem;text-transform:uppercase;letter-spacing:.05em;">
                    {html.escape(field['label'])}
                </label>
                {input_html}
                {assist_html}
                {help_html}
            </div>
        """)
    sections_html = ""
    for sec in sections_seen:
        sections_html += f"""
            <p class="eyebrow" style="margin-top:1.75rem;letter-spacing:.1em;">[{html.escape(sec)}]</p>
            {"".join(rows_by_section[sec])}
        """
    body = f"""
        {banner_html}
        <form method="post" action="/settings" id="settings-form">
            {sections_html}
            <div class="actions" style="margin-top:.5rem;padding-top:1.25rem;border-top:1px solid var(--line);justify-content:flex-end;">
                <button type="submit" class="btn primary" id="settings-save-btn" disabled>Save Settings</button>
            </div>
        </form>
        <script>
        (function(){{
            var form=document.getElementById('settings-form'),saveBtn=document.getElementById('settings-save-btn');
            function markDirty(){{saveBtn.disabled=false;}}
            form.addEventListener('input',markDirty);form.addEventListener('change',markDirty);
        }})();
        function pzpFindFile(fieldName,searchType){{fetch('/settings/find-file?type='+encodeURIComponent(searchType)).then(function(r){{return r.json();}}).then(function(data){{var box=document.getElementById(fieldName+'-find-results');box.innerHTML='';if(!data.candidates||data.candidates.length===0){{box.innerHTML='<p class="note" style="border-top:none;margin-top:0;padding-top:0;">No matches found nearby.</p>';box.style.display='block';return;}}data.candidates.forEach(function(path){{var btn=document.createElement('button');btn.type='button';btn.className='link-add';btn.style.display='block';btn.style.marginTop='.3rem';btn.textContent=path;btn.onclick=function(){{var input=document.querySelector('input[name="'+fieldName+'"]');input.value=path;input.dispatchEvent(new Event('input',{{bubbles:true}}));box.style.display='none';}};box.appendChild(btn);}});box.style.display='block';}}).catch(function(err){{console.error('find-file failed',err);}});}}
        function pzpIniParts(iniPath){{var parts=iniPath.split('/').filter(function(p){{return p.length>0;}});if(parts.length===0)return null;var filename=parts.pop();var stem=filename.replace(/[.]ini$/i,'');return {{dirParts:parts,stem:stem}};}}
        function pzpDeriveConsoleLog(fieldName){{var iniInput=document.querySelector('input[name="paths__server_ini"]');var parsed=pzpIniParts(iniInput.value||'');if(!parsed||parsed.dirParts.length<1){{alert('Fill in the World Config (.ini) Path above first.');return;}}var upTwo=parsed.dirParts.slice(0,-1);var target='/'+upTwo.concat(['server-console.txt']).join('/');var input=document.querySelector('input[name="'+fieldName+'"]');input.value=target;input.dispatchEvent(new Event('input',{{bubbles:true}}));}}
        function pzpDeriveSaveDir(fieldName){{var iniInput=document.querySelector('input[name="paths__server_ini"]');var parsed=pzpIniParts(iniInput.value||'');if(!parsed||parsed.dirParts.length<1){{alert('Fill in the World Config (.ini) Path above first.');return;}}var upTwo=parsed.dirParts.slice(0,-1);var target='/'+upTwo.concat(['Saves','Multiplayer',parsed.stem]).join('/');var input=document.querySelector('input[name="'+fieldName+'"]');input.value=target;input.dispatchEvent(new Event('input',{{bubbles:true}}));}}
        </script>
    """
    return page_shell("PZ Panel &mdash; Settings", "settings", body)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    updates = {}
    errors = []
    for field in SETTINGS_SCHEMA:
        sec, key = field["section"], field["key"]
        field_name = f"{sec}__{key}"
        if field.get("sensitive"):
            new_val = str(form.get(field_name, "")).strip()
            if not new_val:
                continue
            if "\n" in new_val or "\r" in new_val:
                errors.append(f"{field['label']} can't contain a newline")
                continue
            updates[(sec, key)] = new_val
            continue
        if field["type"] == "checkbox":
            updates[(sec, key)] = "true" if form.get(field_name) else "false"
            continue
        new_val = str(form.get(field_name, "")).strip()
        if "\n" in new_val or "\r" in new_val:
            errors.append(f"{field['label']} can't contain a newline")
            continue
        if not new_val:
            continue
        if field["type"] == "number":
            try:
                float(new_val)
            except ValueError:
                errors.append(f"{field['label']} must be a number")
                continue
        updates[(sec, key)] = new_val
    if errors:
        qs = urllib.parse.urlencode({"msg": "; ".join(errors), "msg_type": "error"})
        return RedirectResponse(f"/settings?{qs}", status_code=303)
    _update_ini_settings(updates)
    log_action("panel", "settings-save", f"manual (panel) -- updated {len(updates)} setting(s)")
    qs = urllib.parse.urlencode({"msg": "Settings saved. Discord/Steam/player-tracking changes need a panel restart to take effect."})
    return RedirectResponse(f"/settings?{qs}", status_code=303)


# =============================================================================
# Config page -- edits server .ini via the UI
# =============================================================================

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


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    msg = request.query_params.get("msg")
    msg_type = request.query_params.get("msg_type", "success")
    banner_html = f'<p class="banner {msg_type}">{html.escape(msg)}</p>' if msg else ""
    ini_path = load_server_ini_path()
    if not ini_path:
        body = f"""
            {banner_html}
            <p class="note">No world config (.ini) path configured. Set the path in the
            <a href="/settings" class="link">Settings page</a> under
            <code>World Config (.ini) Path</code> to enable the Config page.</p>
        """
        return page_shell("PZ Panel &mdash; Config", "config", body)
    if not Path(ini_path).exists():
        body = f"""
            {banner_html}
            <p class="note" style="color:var(--rust);">Configured path <code>{html.escape(ini_path)}</code>
            does not exist. Check the path in the <a href="/settings" class="link">Settings page</a>.</p>
        """
        return page_shell("PZ Panel &mdash; Config", "config", body)
    ini = _read_realm_ini(ini_path)
    masters = {}
    for grp in _REALM_GROUPS:
        for f in grp["fields"]:
            if "master_for" in f:
                masters[f["master_for"]] = f["key"]
    sections_html = ""
    first_section = True
    for grp in _REALM_GROUPS:
        fields_html = ""
        for f in grp["fields"]:
            key = f["key"]
            label = html.escape(f["label"])
            help_text = html.escape(f.get("help", ""))
            raw = ini.get(key, "")
            inverted = f.get("inverted", False)
            group_key = f.get("group_key")
            is_master = "master_for" in f
            field_id = f"cfg_{key}"
            if group_key:
                master_key = masters.get(group_key, "")
                master_raw = ini.get(master_key, "false")
                disabled_cls = "" if _bool_val(master_raw) else " cfg-disabled"
                wrapper_extra = (f' class="cfg-group{disabled_cls}"'
                                 f' data-group-key="{html.escape(group_key)}"')
            else:
                wrapper_extra = ' class="cfg-group"'
            if f["type"] == "toggle":
                checked = _bool_val(raw, inverted)
                control = f"""
                    <div class="toggle-wrap">
                        <label class="toggle">
                            <input type="checkbox" id="{field_id}" name="{field_id}"
                                {'checked' if checked else ''}
                                {'data-master-for="' + html.escape(f['master_for']) + '"' if is_master else ''}
                                onchange="cfgDirty(this)">
                            <span class="toggle-slider"></span>
                        </label>
                        <span id="{field_id}_label" style="font-family:'JetBrains Mono',monospace;font-size:.8rem;">
                            {'ON' if checked else 'OFF'}
                        </span>
                    </div>"""
            elif f["type"] == "range":
                mn, mx, dflt = f.get("min", 0), f.get("max", 100), f.get("default", 0)
                try:
                    val = int(float(raw)) if raw else dflt
                except ValueError:
                    val = dflt
                val = max(mn, min(mx, val))
                control = f"""
                    <div class="range-wrap">
                        <input type="range" id="{field_id}" name="{field_id}"
                            min="{mn}" max="{mx}" value="{val}"
                            oninput="cfgRangeUpdate(this);cfgDirty(this)">
                        <input type="number" id="{field_id}_num" class="range-value"
                            min="{mn}" max="{mx}" value="{val}"
                            oninput="cfgNumUpdate(this);cfgDirty(this)">
                    </div>"""
            elif f["type"] == "checkboxes":
                current_vals = set(v.strip() for v in raw.split(";")) if raw else set()
                opts_html = ""
                for opt_val, opt_label in f.get("options", []):
                    chk = "checked" if opt_val in current_vals else ""
                    opts_html += (f'<label class="checkbox-item">'
                                  f'<input type="checkbox" name="{field_id}" value="{html.escape(opt_val)}" '
                                  f'{chk} onchange="cfgDirty(this)"> {html.escape(opt_label)}</label>')
                control = f'<div class="checkbox-group">{opts_html}</div>'
            elif f["type"] == "password":
                is_set = bool(raw)
                placeholder = "Click to change" if is_set else "Not set"
                control = f"""
                    <div class="pw-wrap">
                        <input type="password" id="{field_id}" name="{field_id}" class="input"
                            placeholder="{html.escape(placeholder)}"
                            oninput="cfgDirty(this)" autocomplete="new-password">
                        <button type="button" class="pw-eye"
                            onmousedown="document.getElementById('{field_id}').type='text'"
                            onmouseup="document.getElementById('{field_id}').type='password'"
                            onmouseleave="document.getElementById('{field_id}').type='password'">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
                            </svg>
                        </button>
                    </div>"""
            else:
                control = f"""<input type="text" id="{field_id}" name="{field_id}" class="input"
                    value="{html.escape(raw)}" oninput="cfgDirty(this)">"""
            fields_html += f"""
                <div{wrapper_extra} id="{field_id}_wrap">
                    <div class="cfg-label">{label}</div>
                    {control}
                    {('<div class="cfg-help">' + help_text + '</div>') if help_text else ''}
                </div>
            """
        collapsed_cls = "" if first_section else " collapsed"
        collapsed_style = "" if first_section else "display:none;"
        sections_html += f"""
            <div class="cfg-section">
                <p class="cfg-section-title{collapsed_cls}" onclick="cfgToggleSection(this)">{html.escape(grp['group'])}<span class="cfg-chevron">&#9660;</span></p>
                <div class="cfg-section-body" style="{collapsed_style}">{fields_html}</div>
            </div>
        """
        first_section = False
    body = f"""
        {banner_html}
        <form method="post" action="/config" id="cfg-form">
            {sections_html}
        </form>
        <div class="cfg-save-bar" id="cfg-save-bar">
            <button type="button" class="btn" onclick="location.reload()">Discard</button>
            <button type="button" class="btn primary" onclick="cfgSaveClick()">Save</button>
        </div>
        <div id="cfg-restart-modal" class="modal-backdrop" onclick="if(event.target===this) cfgCloseModal()">
            <div class="modal-box" style="--modal-accent:var(--amber);">
                <p class="modal-eyebrow">Server restart required</p>
                <p class="modal-title">Apply changes?</p>
                <p class="modal-desc">Most PZ server settings require a restart to take effect.</p>
                <div class="modal-actions">
                    <button type="button" class="btn" onclick="cfgCloseModal()">Cancel</button>
                    <button type="button" class="btn primary" onclick="cfgSubmit()">Save Only</button>
                    <button type="button" class="btn info" onclick="cfgSaveAndRestart()">Apply &amp; Restart</button>
                </div>
            </div>
        </div>
        <script>
        var cfgDirtyFlag=false;
        function cfgApplyMaster(masterFor,isOn){{
            document.querySelectorAll('[data-group-key="'+masterFor+'"]').forEach(function(wrap){{
                wrap.classList.toggle('cfg-disabled',!isOn);
                if(!isOn){{
                    var childCb=wrap.querySelector('input[type="checkbox"][data-master-for]');
                    if(childCb){{cfgApplyMaster(childCb.dataset.masterFor,false);}}
                }}else{{
                    var childCb=wrap.querySelector('input[type="checkbox"][data-master-for]');
                    if(childCb){{cfgApplyMaster(childCb.dataset.masterFor,childCb.checked);}}
                }}
            }});
        }}
        function cfgDirty(el){{
            cfgDirtyFlag=true;
            document.getElementById('cfg-save-bar').classList.add('visible');
            if(el.type==='checkbox'&&el.closest('.toggle-wrap')){{
                var lbl=document.getElementById(el.id+'_label');
                if(lbl)lbl.textContent=el.checked?'ON':'OFF';
            }}
            var masterFor=el.dataset.masterFor;
            if(masterFor){{cfgApplyMaster(masterFor,el.checked);}}
        }}
        function cfgRangeUpdate(el){{var num=document.getElementById(el.id+'_num');if(num)num.value=el.value;}}
        function cfgNumUpdate(el){{var rangeId=el.id.replace('_num','');var range=document.getElementById(rangeId);if(range){{var v=Math.max(parseInt(range.min),Math.min(parseInt(range.max),parseInt(el.value)||0));range.value=v;el.value=v;}}}}
        function cfgToggleSection(titleEl){{var body=titleEl.nextElementSibling;var collapsed=titleEl.classList.toggle('collapsed');body.style.display=collapsed?'none':'';}}
        function cfgSaveClick(){{document.getElementById('cfg-restart-modal').classList.add('open');}}
        function cfgCloseModal(){{document.getElementById('cfg-restart-modal').classList.remove('open');}}
        function cfgSubmit(){{cfgCloseModal();document.getElementById('cfg-form').submit();}}
        function cfgSaveAndRestart(){{cfgCloseModal();var form=document.getElementById('cfg-form');var input=document.createElement('input');input.type='hidden';input.name='_restart';input.value='1';form.appendChild(input);form.submit();}}
        document.addEventListener('keydown',function(e){{if(e.key==='Escape')cfgCloseModal();}});
        </script>
    """
    return page_shell("PZ Panel &mdash; Config", "config", body)


@app.post("/config")
async def config_save(request: Request):
    ini_path = load_server_ini_path()
    if not ini_path or not Path(ini_path).exists():
        qs = urllib.parse.urlencode({"msg": "No valid server_ini path configured", "msg_type": "error"})
        return RedirectResponse(f"/config?{qs}", status_code=303)
    form = await request.form()
    do_restart = bool(form.get("_restart"))
    current_ini = _read_realm_ini(ini_path)
    updates = {}
    for grp in _REALM_GROUPS:
        for f in grp["fields"]:
            key = f["key"]
            field_id = f"cfg_{key}"
            inverted = f.get("inverted", False)
            if f["type"] == "toggle":
                display_on = field_id in form
                ini_val = _ini_bool(display_on, inverted)
                if current_ini.get(key, "").lower() != ini_val:
                    updates[key] = ini_val
            elif f["type"] == "range":
                raw = form.get(field_id, "")
                try:
                    val = str(int(float(str(raw))))
                except (ValueError, TypeError):
                    continue
                mn, mx = f.get("min", 0), f.get("max", 100)
                val = str(max(mn, min(mx, int(val))))
                if current_ini.get(key, "") != val:
                    updates[key] = val
            elif f["type"] == "checkboxes":
                vals = form.getlist(field_id)
                new_val = ";".join(vals)
                if current_ini.get(key, "") != new_val:
                    updates[key] = new_val
            elif f["type"] == "password":
                raw = str(form.get(field_id, "")).strip()
                if raw and current_ini.get(key, "") != raw:
                    updates[key] = raw
            else:
                raw = str(form.get(field_id, "")).strip()
                if current_ini.get(key, "") != raw:
                    updates[key] = raw
    if updates:
        try:
            _update_realm_ini(ini_path, updates)
            log_action("panel", "config-save",
                       f"manual (panel) -- updated {len(updates)} server .ini setting(s): "
                       f"{', '.join(updates.keys())}")
        except Exception as e:
            qs = urllib.parse.urlencode({"msg": f"Save failed: {e}", "msg_type": "error"})
            return RedirectResponse(f"/config?{qs}", status_code=303)
    if do_restart:
        is_running = get_server_state() not in ("offline", "failed")
        if is_running:
            try:
                run_server_action("restart")
                log_action("panel", "restart", "manual (panel) -- config save with restart")
            except subprocess.TimeoutExpired:
                pass
            msg = f"Config saved ({len(updates)} change(s)) and restarting."
        else:
            msg = f"Config saved ({len(updates)} change(s)). Server was not running -- no restart triggered."
    else:
        msg = f"Config saved ({len(updates)} change(s))." if updates else "No changes detected."
    qs = urllib.parse.urlencode({"msg": msg})
    return RedirectResponse(f"/config?{qs}", status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)

