"""
PZ Panel - minimal web UI for managing the Project Zomboid server.

Start/stop/restart just call platform_compat -- graceful RCON shutdown
lives in pzserver.service's ExecStop= directive on Linux (or is handled
by platform_compat's RCON quit + wait + kill on Windows), so it applies
whether this panel, the CLI, or a reboot triggers the stop.

Cross-platform: Linux (systemd) and Windows (sc or process mode).
See platform_compat.py for all OS-specific operations.
CONFIG_PATH is defined in server_config.py and defaults to
/opt/pzp/pzpanel.ini on Linux or alongside main.py on Windows.
Override with PZPANEL_CONFIG environment variable.
"""

__version__ = "4.2.5"

import asyncio
import html
import json
import logging
import os
import shutil
import subprocess
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

from rcon import RCONClient
from modcheck import (check_for_updates, add_workshop_item, remove_workshop_item,
                       reorder_workshop_items, ModCheckError)
from steam_workshop import get_mod_details, SteamWorkshopError
from actionlog import log_action, read_recent
from automation import is_paused, set_paused
import countdown_control as cc
import removed_mods
import discord_module
from discord_module import _fmt_ingame_time
import player_db as pdb
import platform_compat as pc

from server_config import (
    CONFIG_PATH, ALLOWED_ACTIONS,
    load_server_config, load_console_log_path, load_server_ini_path,
    get_multiplayer_save_dir, get_server_state, run_server_action,
    load_rcon_config, rcon_reachable, _countdown_summary,
    _read_all_settings, _update_ini_settings, _update_realm_ini,
    _find_candidate_files, _find_exact_named_files,
    _read_realm_ini, _bool_val, _ini_bool,
    SETTINGS_GROUPS, SETTINGS_SCHEMA, _REALM_GROUPS,
)
from ui_helpers import (
    PAGE_STYLE, page_shell, _led_style,
    _fmt_ts, _fmt_log_time, _fmt_removed_at,
    _steam_id_link, _mod_identity_cells, _thumb_html, _mod_status_tag,
)

log = logging.getLogger("pzpanel")

app = FastAPI(title="PZ Panel")

_SCRIPT_DIR = Path(__file__).parent
_FAVICON_DIR = (_SCRIPT_DIR / "favicon" if (_SCRIPT_DIR / "favicon").exists()
                else pc.get_default_data_dir() / "favicon")

app.mount("/favicon", StaticFiles(directory=str(_FAVICON_DIR)), name="favicon")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    ico = _FAVICON_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico))
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


# Wrap page_shell to inject version automatically
def _page(title, active_tab, body_html, extra_head=""):
    return page_shell(title, active_tab, body_html, extra_head=extra_head, version=__version__)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
            <div class="hazard-frame danger" style="margin:2rem -1.75rem -1.75rem;width:calc(100% + 3.5rem);">
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
        </script>"""
    return _page("PZ Panel", "status", body)


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
    return _page("PZ Panel &mdash; Mods", "mods", body)


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
    return _page("PZ Panel &mdash; Add Mod", "mods", body)


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
    return _page("PZ Panel &mdash; Log", "log", body_inner)


@app.get("/killboard", response_class=HTMLResponse)
def killboard_page():
    if _player_event_watcher is None:
        body = '<p class="note">Player-event tracking isn\'t running -- check that pzpanel.ini was readable at startup.</p>'
        return _page("PZ Panel &mdash; Killboard", "killboard", body)
    db = _player_event_watcher.db
    if db is not None:
        accounts = db.get_killboard()
        if not accounts:
            table_html = '<p class="note">No player data yet -- waiting for the first connection to be recorded.</p>'
        else:
            # Players table
            player_rows = []
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
                    hours_note = f'<span class="readout-sub" style="margin-left:.5rem;font-size:.72rem;">{acc["pz_hours"]:,} hrs in PZ</span>'
                player_rows.append(f"""<tr>
                    <td>{avatar_html}<a href="{html.escape(profile_url, quote=True)}"
                        target="_blank" rel="noopener noreferrer"
                        class="link-id" style="font-size:1rem;">{steam_name}</a>{hours_note}</td>
                    <td class="mono">{acc['account_kills']}</td>
                </tr>""")
            players_table = f"""<p class="eyebrow" style="margin:1.5rem 0 .6rem;letter-spacing:.06em;">Players</p>
                <table>
                    <tr><th>Player</th><th>Total kills</th></tr>
                    {"".join(player_rows)}
                </table>"""
            # Characters table
            char_rows = []
            for acc in accounts:
                for c in acc["characters"]:
                    run_str = str(c["current_run_kills"]) if c["current_run_kills"] else "\u2014"
                    char_rows.append(f"""<tr>
                        <td>{html.escape(c['username'])}</td>
                        <td class="mono">{run_str}</td>
                        <td class="mono">\u2014</td>
                    </tr>""")
            chars_table = f"""<p class="eyebrow" style="margin:1.5rem 0 .6rem;letter-spacing:.06em;">Characters</p>
                <table>
                    <tr><th>Character</th><th>Kills (current run)</th><th>Time survived (in-game)</th></tr>
                    {"".join(char_rows)}
                </table>"""
            table_html = players_table + chars_table
        body = f"""
            {table_html}
            <p class="note">Lifetime kills accumulate across all characters and runs tracked by PZP.
            The mod file is rewritten roughly every 60 seconds so live kill counts can lag by up to that long.</p>
        """
        return _page("PZ Panel &mdash; Killboard", "killboard", body)
    if _player_event_watcher.kills_file is None:
        body = '<p class="note">No kills_file configured under <code>[player_events]</code> in pzpanel.ini.</p>'
        return _page("PZ Panel &mdash; Killboard", "killboard", body)
    st = _player_event_watcher.state.data
    current = st.get("kills_current_run", {})
    lifetime = st.get("kills_lifetime", {})
    hours = st.get("hours_current_run", {})
    logins = st.get("logins", {})  # username -> steamid
    last_updated = st.get("kills_last_updated")
    names = sorted(set(current) | set(lifetime), key=lambda n: current.get(n, 0), reverse=True)
    if not names:
        table_html = '<p class="note">No kill data yet.</p>'
    else:
        # Players section: group lifetime kills by steamid where known, else by username
        seen_steamids = {}
        player_totals = {}  # display_key -> {name, total}
        for n in names:
            sid = logins.get(n)
            key = sid or n
            if key not in player_totals:
                player_totals[key] = {"name": n, "total": 0}
            player_totals[key]["total"] += lifetime.get(n, 0)
        player_rows = []
        for key, p in sorted(player_totals.items(), key=lambda x: x[1]["total"], reverse=True):
            player_rows.append(f"""<tr><td>{html.escape(p['name'])}</td><td class="mono">{p['total']}</td></tr>""")
        players_table = f"""<p class="eyebrow" style="margin:0 0 .6rem;letter-spacing:.06em;">Players</p>
            <table>
                <tr><th>Player</th><th>Total kills</th></tr>
                {"".join(player_rows)}
            </table>"""
        # Characters section
        char_rows = []
        for n in names:
            ingame = _fmt_ingame_time(hours.get(n))
            time_cell = html.escape(ingame) if ingame else "\u2014"
            char_rows.append(f"""<tr><td>{html.escape(n)}</td><td class="mono">{current.get(n, '\u2014')}</td><td class="mono">{time_cell}</td></tr>""")
        chars_table = f"""<p class="eyebrow" style="margin:1.5rem 0 .6rem;letter-spacing:.06em;">Characters</p>
            <table>
                <tr><th>Character</th><th>Kills (current run)</th><th>Time survived (in-game)</th></tr>
                {"".join(char_rows)}
            </table>"""
        table_html = players_table + chars_table
    updated_note = f'Last updated {_fmt_ts(last_updated)}.' if last_updated else 'Not yet updated.'
    body = f"{table_html}<p class=\"note\">(In-memory fallback.) {updated_note}</p>"
    return _page("PZ Panel &mdash; Killboard", "killboard", body)


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
            function colorizeLine(line){{var escaped=escapeHtml(line);if(escaped.indexOf('*** SERVER STARTED')!==-1){{return '<span class="lvl-server-started">'+escaped+'</span>';}}if(/Steam client \\d+ is initiating a connection/.test(line)){{return '<span class="lvl-steam-connect">'+escaped+'</span>';}}var m=line.match(/^(ERROR|WARN|LOG)/);if(!m)return escaped;var level=m[1];var cls=level==='ERROR'?'lvl-error':level==='WARN'?'lvl-warn':'lvl-log';var idx=escaped.indexOf(level);if(idx===-1)return escaped;return escaped.slice(0,idx)+'<span class="'+cls+'">'+level+'</span>'+escaped.slice(idx+level.length);}}
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
    return _page("PZ Panel &mdash; Console", "console", body)


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

    def _field_html(field):
        sec = field["section"]
        key = field["key"]
        current_val = current.get(sec, {}).get(key, "")
        field_name = f"{sec}__{key}"
        os_attr = f' data-os="{field["os"]}"' if field.get("os") else ""
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
                          f'value="{html.escape(current_val)}">')
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
        return f"""
            <div style="margin-bottom:1.1rem;"{os_attr}>
                <label style="display:block;font-family:'JetBrains Mono',monospace;font-size:.75rem;
                    color:var(--ink-dim);margin-bottom:.3rem;text-transform:uppercase;letter-spacing:.05em;">
                    {html.escape(field['label'])}
                </label>
                {input_html}
                {assist_html}
                {help_html}
            </div>
        """

    sections_html = ""
    for grp in SETTINGS_GROUPS:
        collapsed_cls = " collapsed" if grp.get("collapsed") else ""
        collapsed_style = "display:none;" if grp.get("collapsed") else ""
        grp_help = (f'<p class="cfg-help" style="margin-bottom:.75rem;">{html.escape(grp["help"])}</p>'
                    if grp.get("help") else "")
        preview_block = grp.get("preview_html", "")
        os_toggle_html = ""
        if grp.get("os_toggle"):
            os_toggle_html = """
                <div style="display:flex;align-items:center;gap:.75rem;margin-bottom:1rem;">
                    <span style="font-family:'JetBrains Mono',monospace;font-size:.75rem;color:var(--ink-dim);text-transform:uppercase;letter-spacing:.05em;">Platform:</span>
                    <button type="button" class="btn" id="os-toggle-btn" onclick="settingsToggleOS(this)">Linux</button>
                </div>
            """
        fields_html = "".join(_field_html(f) for f in grp["fields"])
        sections_html += f"""
            <div class="cfg-section">
                <p class="cfg-section-title{collapsed_cls}" onclick="cfgToggleSection(this)">{html.escape(grp['group'])}<span class="cfg-chevron">&#9660;</span></p>
                <div class="cfg-section-body" style="{collapsed_style}">
                    {grp_help}
                    {preview_block}
                    {os_toggle_html}
                    {fields_html}
                </div>
            </div>
        """

    body = f"""
        {banner_html}
        <form method="post" action="/settings" id="settings-form">
            {sections_html}
        </form>
        <div class="cfg-save-bar" id="settings-save-bar">
            <button type="button" class="btn" onclick="location.reload()">Discard</button>
            <button type="button" class="btn primary" onclick="document.getElementById('settings-form').submit()">Save Settings</button>
        </div>
        <script>
        (function(){{
            var form=document.getElementById('settings-form');
            var bar=document.getElementById('settings-save-bar');
            function markDirty(){{bar.classList.add('visible');}}
            form.addEventListener('input',markDirty);form.addEventListener('change',markDirty);
        }})();
        function cfgToggleSection(titleEl){{var body=titleEl.nextElementSibling;var collapsed=titleEl.classList.toggle('collapsed');body.style.display=collapsed?'none':'';}}
        var pzpCurrentOS='linux';
        function settingsToggleOS(btn){{
            pzpCurrentOS=(pzpCurrentOS==='linux')?'windows':'linux';
            btn.textContent=pzpCurrentOS==='linux'?'Linux':'Windows';
            document.querySelectorAll('[data-os]').forEach(function(el){{
                el.style.display=(el.dataset.os===pzpCurrentOS||!el.dataset.os)?'':'none';
            }});
        }}
        document.querySelectorAll('[data-os]').forEach(function(el){{
            el.style.display=el.dataset.os===pzpCurrentOS?'':'none';
        }});
        function pzpFindFile(fieldName,searchType){{fetch('/settings/find-file?type='+encodeURIComponent(searchType)).then(function(r){{return r.json();}}).then(function(data){{var box=document.getElementById(fieldName+'-find-results');box.innerHTML='';if(!data.candidates||data.candidates.length===0){{box.innerHTML='<p class="note" style="border-top:none;margin-top:0;padding-top:0;">No matches found nearby.</p>';box.style.display='block';return;}}data.candidates.forEach(function(path){{var btn=document.createElement('button');btn.type='button';btn.className='link-add';btn.style.display='block';btn.style.marginTop='.3rem';btn.textContent=path;btn.onclick=function(){{var input=document.querySelector('input[name="'+fieldName+'"]');input.value=path;input.dispatchEvent(new Event('input',{{bubbles:true}}));box.style.display='none';}};box.appendChild(btn);}});box.style.display='block';}}).catch(function(err){{console.error('find-file failed',err);}});}}
        function pzpIniParts(iniPath){{var parts=iniPath.split('/').filter(function(p){{return p.length>0;}});if(parts.length===0)return null;var filename=parts.pop();var stem=filename.replace(/[.]ini$/i,'');return {{dirParts:parts,stem:stem}};}}
        function pzpDeriveConsoleLog(fieldName){{var iniInput=document.querySelector('input[name="paths__server_ini"]');var parsed=pzpIniParts(iniInput.value||'');if(!parsed||parsed.dirParts.length<1){{alert('Fill in the World Config (.ini) Path above first.');return;}}var upTwo=parsed.dirParts.slice(0,-1);var target='/'+upTwo.concat(['server-console.txt']).join('/');var input=document.querySelector('input[name="'+fieldName+'"]');input.value=target;input.dispatchEvent(new Event('input',{{bubbles:true}}));}}
        function pzpDeriveSaveDir(fieldName){{var iniInput=document.querySelector('input[name="paths__server_ini"]');var parsed=pzpIniParts(iniInput.value||'');if(!parsed||parsed.dirParts.length<1){{alert('Fill in the World Config (.ini) Path above first.');return;}}var upTwo=parsed.dirParts.slice(0,-1);var target='/'+upTwo.concat(['Saves','Multiplayer',parsed.stem]).join('/');var input=document.querySelector('input[name="'+fieldName+'"]');input.value=target;input.dispatchEvent(new Event('input',{{bubbles:true}}));}}
        </script>
    """
    return _page("PZ Panel &mdash; Settings", "settings", body)


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
        return _page("PZ Panel &mdash; Config", "config", body)
    if not Path(ini_path).exists():
        body = f"""
            {banner_html}
            <p class="note" style="color:var(--rust);">Configured path <code>{html.escape(ini_path)}</code>
            does not exist. Check the path in the <a href="/settings" class="link">Settings page</a>.</p>
        """
        return _page("PZ Panel &mdash; Config", "config", body)
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
    return _page("PZ Panel &mdash; Config", "config", body)


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
