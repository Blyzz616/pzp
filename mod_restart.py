"""
Orchestrates a mod-sync restart: checks for pending Workshop updates and,
if any are found, announces on Discord, counts down to connected players
via RCON, then restarts pzserver.service. The actual mod content fetch
is NOT done here -- PZ's own built-in Workshop auto-download on startup
handles that (see modcheck.py's module docstring for why we're not
shelling out to steamcmd).

The countdown itself can be paused, resumed, or postponed to a specific
future hour from the panel while it's running (see countdown_control.py)
-- distinct from the Watchdog on/off toggle in automation.py, which
controls whether a check cycle starts at all, not an already-running
countdown.
"""

__version__ = "4.0.1"

import configparser
import os
import re
import subprocess
import time
from datetime import datetime, timedelta

from modcheck import check_for_updates, ModCheckError
from steam_workshop import SteamWorkshopError
from rcon import RCONClient, RCONError
from actionlog import log_action
from automation import is_paused, set_paused
from discord_module import Discord
import countdown_control as cc
import platform_compat as pc

CONFIG_PATH = os.environ.get("PZPANEL_CONFIG") or str(pc.get_default_config_path())
TICK_SECONDS = 1.0

COUNTDOWN_SCHEDULE = [
    (600, "Server restarting in 10 minutes to update: {mods}. Get to safety."),
    (300, "Server restarting in 5 minutes to update: {mods}. Get to safety."),
    (60,  "Server restarting in 1 minute to update: {mods}. Get to safety."),
    (30,  "Server restarting in 30 seconds."),
    (10,  "Server restarting in 10 seconds."),
    (5,   "5"),
    (4,   "4"),
    (3,   "3"),
    (2,   "2"),
    (1,   "1"),
]

WARNING_WINDOW_SECONDS = COUNTDOWN_SCHEDULE[0][0]


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


def get_server_unit(cfg):
    return cfg.get("server", "unit", fallback="pzserver")


def rcon_client(cfg):
    return RCONClient(
        cfg.get("rcon", "host", fallback="127.0.0.1"),
        cfg.getint("rcon", "port", fallback=27015),
        cfg.get("rcon", "password", fallback=""),
        timeout=5.0,
    )


def get_player_count(cfg):
    try:
        with rcon_client(cfg) as client:
            response = client.command("players")
    except (RCONError, OSError) as e:
        print(f"mod_restart: could not reach RCON to check players: {e}")
        return None
    m = re.search(r"Players connected \((\d+)\)", response)
    if m:
        return int(m.group(1))
    print(f"mod_restart: unexpected 'players' response, treating as unknown: {response!r}")
    return None


def send_servermsg(cfg, message):
    try:
        with rcon_client(cfg) as client:
            client.command(f'servermsg "{message}"')
        print(f"mod_restart: servermsg sent: {message!r}")
    except (RCONError, OSError) as e:
        print(f"mod_restart: servermsg failed ({e}), continuing anyway")


def _discord_client(cfg):
    webhook_url = cfg.get("discord", "webhook_url", fallback="")
    if not webhook_url:
        print("mod_restart: no discord webhook_url configured, skipping announcement")
        return None
    return Discord(webhook_url)


def post_discord(cfg, content):
    client = _discord_client(cfg)
    if client is None:
        return
    if client.content(content):
        print("mod_restart: discord announcement posted")


def post_discord_mod_restart(cfg, updated_mods):
    client = _discord_client(cfg)
    if client is None:
        return

    amber = int("FFB020", 16)
    embeds = []
    for mod in updated_mods:
        embed = {
            "title": mod["title"],
            "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod['mod_id']}",
            "description": f"Workshop ID: {mod['mod_id']}",
            "color": amber,
        }
        if mod.get("preview_url"):
            embed["thumbnail"] = {"url": mod["preview_url"]}
        embeds.append(embed)

    server_name = cfg.get("server", "name", fallback="Realm")
    mod_word = "mod" if len(updated_mods) == 1 else "mods"
    payload = {
        "content": f"\U0001F504 **{server_name} restarting to update {len(updated_mods)} {mod_word}**",
        "embeds": embeds[:10],
    }
    if client.raw(payload):
        print("mod_restart: discord announcement posted")


def restart_server(cfg, reason):
    unit = cfg.get("server", "unit", fallback="pzserver")
    print(f"mod_restart: restarting server ({unit})")
    log_action("modcheck", "restart", reason)
    try:
        pc.server_restart(cfg)
        print("mod_restart: restart command completed")
    except Exception as e:
        print(f"mod_restart: restart FAILED: {e}")


def _run_ticking_countdown(cfg, mod_names, target_epoch, time_scale=1.0, check_watchdog_pause=True):
    cc.write_countdown_state(target_epoch, mod_names)
    marks_remaining = list(COUNTDOWN_SCHEDULE)
    mods_str = ", ".join(mod_names)

    while True:
        if check_watchdog_pause and is_paused():
            cc.clear_countdown_state()
            send_servermsg(cfg, "Restart cancelled by admin (watchdog disabled) -- standing down.")
            print("mod_restart: countdown aborted, watchdog was disabled mid-countdown")
            return "cancelled"

        cmd = cc.read_and_clear_command()
        if cmd:
            action = cmd.get("command")

            if action == "pause":
                remaining = max(0.0, target_epoch - time.time())
                cc.write_countdown_state(target_epoch, mod_names, paused=True,
                                          paused_remaining=remaining)
                send_servermsg(cfg, f"Countdown paused at {cc.format_remaining(remaining)}.")
                log_action("modcheck", "countdown-paused", f"At {cc.format_remaining(remaining)}")
                set_paused(True)
                paused_remaining = remaining
                mods_str_reminder = ", ".join(mod_names)

                now_dt = datetime.now().astimezone()
                next_reminder_epoch = (now_dt.replace(minute=0, second=0, microsecond=0)
                                        + timedelta(hours=1)).timestamp()

                while True:
                    time.sleep(TICK_SECONDS * time_scale)

                    if time.time() >= next_reminder_epoch:
                        send_servermsg(cfg, f"Mod-checker paused. Update(s) pending: "
                                             f"{mods_str_reminder}. Restart is on hold.")
                        next_reminder_epoch += 3600

                    cmd2 = cc.read_and_clear_command()
                    if not cmd2:
                        continue
                    if cmd2.get("command") == "resume":
                        set_paused(False)
                        target_epoch = time.time() + paused_remaining
                        cc.write_countdown_state(target_epoch, mod_names, paused=False)
                        send_servermsg(cfg, f"Countdown resumed -- restarting in "
                                             f"{cc.format_remaining(paused_remaining)}.")
                        log_action("modcheck", "countdown-resumed",
                                   f"{cc.format_remaining(paused_remaining)} remaining")
                        break
                    if cmd2.get("command") == "postpone":
                        result = _handle_postpone(cfg, cmd2.get("target_epoch"), mod_names, time_scale)
                        if result == "postponed_invalid":
                            break
                        return result
                    if cmd2.get("command") == "cancel":
                        cc.clear_countdown_state()
                        set_paused(False)
                        send_servermsg(cfg, "Restart cancelled by admin -- standing down.")
                        log_action("modcheck", "restart-cancelled",
                                   f"Cancelled while paused: {mods_str_reminder}")
                        return "cancelled"
                continue

            elif action == "postpone":
                result = _handle_postpone(cfg, cmd.get("target_epoch"), mod_names, time_scale)
                if result != "postponed_invalid":
                    return result

            elif action == "cancel":
                cc.clear_countdown_state()
                send_servermsg(cfg, "Restart cancelled by admin -- standing down.")
                log_action("modcheck", "restart-cancelled", f"Cancelled: {mods_str}")
                return "cancelled"

        remaining = target_epoch - time.time()
        if remaining <= 0:
            break

        while marks_remaining and remaining <= marks_remaining[0][0] * time_scale:
            seconds_before, template = marks_remaining.pop(0)
            message = template.format(mods=mods_str) if "{mods}" in template else template
            send_servermsg(cfg, message)

        time.sleep(min(TICK_SECONDS * time_scale, max(0.01, remaining)))

    cc.clear_countdown_state()
    return "completed"


def run_countdown(cfg, mod_names, time_scale=1.0):
    target_epoch = time.time() + WARNING_WINDOW_SECONDS * time_scale
    return _run_ticking_countdown(cfg, mod_names, target_epoch, time_scale, check_watchdog_pause=True)


def _handle_postpone(cfg, target_epoch, mod_names, time_scale=1.0):
    cc.clear_countdown_state()

    if target_epoch is None:
        print("mod_restart: postpone command missing target_epoch, restarting now instead")
        return "postponed_invalid"

    set_paused(True)
    cc.write_postponed_state(target_epoch, mod_names)

    local_dt = datetime.fromtimestamp(target_epoch).astimezone()
    today = datetime.now().astimezone().date()
    suffix = " (tomorrow)" if local_dt.date() != today else ""
    label = f"{local_dt.strftime('%H:%M')}{suffix}"

    send_servermsg(cfg, f"Restart postponed until {label}.")
    post_discord(cfg, f"\u23F8\ufe0f **Restart postponed until {label}** -- {', '.join(mod_names)}")
    log_action("modcheck", "restart-postponed", f"Postponed to {label}: {', '.join(mod_names)}")
    print(f"mod_restart: postponed, sleeping until warning window before {local_dt.isoformat()}")

    warning_start = target_epoch - WARNING_WINDOW_SECONDS

    while True:
        remaining = warning_start - time.time()
        if remaining <= 0:
            break
        cmd = cc.read_and_clear_command()
        if cmd:
            action = cmd.get("command")
            if action == "postpone":
                return _handle_postpone(cfg, cmd.get("target_epoch"), mod_names, time_scale)
            if action == "cancel":
                cc.clear_postponed_state()
                set_paused(False)
                send_servermsg(cfg, "Postponed restart cancelled by admin.")
                log_action("modcheck", "restart-cancelled",
                           f"Postponement cancelled: {', '.join(mod_names)}")
                return "cancelled"
        time.sleep(min(TICK_SECONDS * time_scale, max(0.01, remaining)))

    cc.clear_postponed_state()

    player_count = get_player_count(cfg)
    if player_count is not None and player_count == 0:
        print("mod_restart: no players online at the postponed warning window, restarting immediately")
        set_paused(False)
        restart_server(cfg, f"Postponed mod update: {', '.join(mod_names)}")
        return "postponed"

    result = _run_ticking_countdown(cfg, mod_names, target_epoch, time_scale, check_watchdog_pause=False)
    if result == "cancelled":
        set_paused(False)
        return "cancelled"
    if result == "postponed":
        return "postponed"

    set_paused(False)
    restart_server(cfg, f"Postponed mod update: {', '.join(mod_names)}")
    return "postponed"


def run_mod_sync_cycle(time_scale=1.0, dry_run=False, force_countdown=False, simulate_update=False):
    if is_paused():
        print("mod_restart: automation is paused, skipping this cycle entirely")
        return

    cfg = load_config()

    if simulate_update:
        print("mod_restart: --simulate-update set, using a fake mod instead of a real check")
        updated = [{"mod_id": "0", "title": "[TEST] Simulated Mod Update", "preview_url": None}]
    else:
        try:
            results = check_for_updates()
        except (ModCheckError, SteamWorkshopError) as e:
            print(f"mod_restart: could not check for updates this cycle: {e}")
            return

        updated = [r for r in results if r["update_available"]]
        if not updated:
            print("mod_restart: no updates found")
            return

        print(f"mod_restart: {len(updated)} update(s) found: {[m['title'] for m in updated]}")

    mod_names = [m["title"] for m in updated]

    if dry_run:
        print("mod_restart: DRY RUN -- stopping here, no Discord post, no restart")
        return

    post_discord_mod_restart(cfg, updated)

    player_count = get_player_count(cfg)
    needs_countdown = force_countdown or player_count is None or player_count > 0

    if needs_countdown:
        reason = ("--force-countdown set" if force_countdown
                   else "could not confirm player count -- running countdown to be safe" if player_count is None
                   else f"{player_count} player(s) online")
        print(f"mod_restart: {reason}, running countdown")
        result = run_countdown(cfg, mod_names, time_scale=time_scale)
        if result in ("postponed", "cancelled"):
            return
    else:
        print("mod_restart: no players online, restarting immediately")

    restart_server(cfg, f"Mod update: {', '.join(mod_names)}")


if __name__ == "__main__":
    import sys

    if "--test-discord-only" in sys.argv:
        cfg = load_config()
        client = _discord_client(cfg)
        if client is None:
            print("No discord webhook_url configured in pzpanel.ini")
            sys.exit(1)
        success = client.content("\U0001F9EA Test post from mod_restart.py --test-discord-only")
        sys.exit(0 if success else 1)

    dry = "--dry-run" in sys.argv
    force = "--force-countdown" in sys.argv
    simulate = "--simulate-update" in sys.argv
    scale = 1.0
    for arg in sys.argv:
        if arg.startswith("--scale="):
            scale = float(arg.split("=", 1)[1])
    run_mod_sync_cycle(time_scale=scale, dry_run=dry, force_countdown=force, simulate_update=simulate)
