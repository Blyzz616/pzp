"""
discord_module.py — Player-event watcher + Discord embed poster for pzpanel.

Tails two log files:
  1. server-console.txt      — server up/down, deaths
  2. Logs/*_connections.txt  — join, disconnect, login-queue
     (always the lexicographically highest *_connections.txt in the dir)

All kill arithmetic lives in kill_tracker.py.
This module owns:
  - Connection event watching (join / disconnect / death)
  - Session tracking (session_kills = n - m across all lives this login)
  - Discord embed construction and posting
  - Steam profile cache
"""

__version__ = "4.6.4"

import configparser
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

import platform_compat as pc
import player_db as pdb
import kill_tracker as kt
from discord_webhook import Discord as DiscordWebhook
from server_config import resolve_logs_dir

log = logging.getLogger("pzpanel.discord")

# ---------------------------------------------------------------------------
# Logs directory
# ---------------------------------------------------------------------------
# Was hardcoded to /home/pzserver/Zomboid/Logs -- broken on Windows, and
# silently ignored the [player_events] logs_dir override already exposed
# in Settings. Now resolved the same way the console-log viewer resolves
# it (server_config.resolve_logs_dir): the override if set, else the
# sibling "Logs" directory next to console_log. Re-resolved on every
# call rather than cached, since settings can change after a restart.


def _find_connections_log():
    logs_dir = resolve_logs_dir()
    if not logs_dir:
        return None
    try:
        candidates = sorted(logs_dir.glob("*_connections.txt"))
        return candidates[-1] if candidates else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Log-line patterns
# ---------------------------------------------------------------------------
_CONSOLE_PATTERNS_RAW = {
    "server_up":   r"SERVER STARTED",
    "server_down": r"Quit Java",
    "death":       r"\buser (?P<name>\S+) died at \(",
}

_CONNECTIONS_PATTERNS_RAW = {
    "login_queue": r'message="login-queue-request".*?steam-id="(?P<steamid>\d+)"',
    "join":        r'event="fully-connected".*?steam-id="(?P<steamid>\d+)".*?username="(?P<username>[^"]+)"',
    "disconnect":  r'event="disconnect".*?steam-id="(?P<steamid>\d+)".*?username="(?P<username>[^"]+)"',
}

COLOURS = {
    "LIME":       0x00FF00,
    "GREEN":      0x2ECC71,
    "RED":        0xE74C3C,
    "ORANGE":     0xE67E22,
    "GOLD":       0xFFD700,
    "DARKVIOLET": 0x9B59B6,
}

RESPAWN_MESSAGES = [
    "**{name}** is back — death is just a speed bump.",
    "**{name}** spawned again. The zombies aren't done yet.",
    "The undead couldn't keep **{name}** down.",
    "**{name}** respawned. The apocalypse will have to try harder.",
]
RAGE_MESSAGES = [
    "Looks like **{name}**'s exit was more dramatic than their survival skills.",
    "**{name}** rage-quit. The zombies win this round.",
    "**{name}** disconnected right after dying. Bold strategy.",
]

_MILESTONE_TEMPLATES = {
    "life": [
        "**{name}** just hit **{n:,}** kills this life. The zombies are running out of volunteers.",
        "**{n:,}** kills this life for **{name}**. Truly a survivor.",
        "**{name}** — **{n:,}** this run. The horde remembers.",
    ],
    "lifetime": [
        "**{name}** crosses **{n:,}** lifetime kills. A monument to the fallen undead.",
        "**{n:,}** lifetime kills for **{name}**. Legend.",
        "**{name}** — **{n:,}** all-time. The apocalypse bows.",
    ],
}


def _milestone_message(threshold, name, tier):
    if threshold == 1:
        return random.choice([
            f"{name} draws first blood. The apocalypse just got personal.",
            f"Kill #1 for {name}. The horde has been warned.",
            f"{name} opens their account. Only several thousand to go.",
        ])
    return random.choice(_MILESTONE_TEMPLATES[tier]).format(name=name, n=threshold)


def human_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    parts = []
    for unit, div in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
        v, seconds = divmod(seconds, div)
        if v:
            parts.append(f"{v}{unit}")
    return " ".join(parts)


def _fmt_ingame_time(hours_survived):
    if hours_survived is None:
        return None
    total_hours = int(hours_survived)
    days = total_hours // 24
    hrs  = total_hours % 24
    if days and hrs:
        return f"{days} day{'s' if days != 1 else ''}, {hrs} hour{'s' if hrs != 1 else ''}"
    if days:
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hrs} hour{'s' if hrs != 1 else ''}"


# ---------------------------------------------------------------------------
# Steam profile cache
# ---------------------------------------------------------------------------
class SteamCache:
    def __init__(self, api_key, cache_hours=24):
        self.api_key      = api_key
        self.cache_secs   = cache_hours * 3600
        self._cache       = {}

    def profile(self, steamid):
        if not steamid:
            return {}
        now    = time.time()
        cached = self._cache.get(steamid)
        if cached and now - cached["_ts"] < self.cache_secs:
            return cached
        result = {"_ts": now}
        try:
            r = requests.get(
                "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
                params={"key": self.api_key, "steamids": steamid}, timeout=8)
            r.raise_for_status()
            players = r.json().get("response", {}).get("players", [])
            if players:
                p = players[0]
                result["persona"]     = p.get("personaname")
                result["avatar"]      = p.get("avatarfull") or p.get("avatar")
                result["profile_url"] = p.get("profileurl")
        except Exception as e:
            log.debug("Steam profile lookup failed for %s: %s", steamid, e)
        if self.api_key:
            try:
                r2 = requests.get(
                    "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
                    params={"key": self.api_key, "steamid": steamid,
                            "include_appinfo": 1, "appids_filter[0]": 108600},
                    timeout=8)
                r2.raise_for_status()
                games = r2.json().get("response", {}).get("games", [])
                if games:
                    result["pz_hours"] = round(games[0].get("playtime_forever", 0) / 60, 1)
            except Exception as e:
                log.debug("Steam PZ hours lookup failed for %s: %s", steamid, e)
            try:
                r3 = requests.get(
                    "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
                    params={"key": self.api_key, "steamid": steamid,
                            "include_appinfo": 1},
                    timeout=8)
                r3.raise_for_status()
                all_games = r3.json().get("response", {}).get("games", [])
                recent = sorted(
                    [g for g in all_games
                     if g.get("appid") != 108600 and g.get("rtime_last_played", 0) > 0],
                    key=lambda g: g.get("rtime_last_played", 0), reverse=True
                )[:2]
                result["recent_games"] = [
                    {"name": g["name"],
                     "total_hours": round(g.get("playtime_forever", 0) / 60, 1),
                     "last_played": g.get("rtime_last_played", 0)}
                    for g in recent
                ]
            except Exception as e:
                log.debug("Steam recent games lookup failed for %s: %s", steamid, e)
        self._cache[steamid] = result
        return result


class _NoSteam:
    def profile(self, _):
        return {}


# ---------------------------------------------------------------------------
# Player event watcher
# ---------------------------------------------------------------------------
class PlayerEventWatcher:
    def __init__(self, log_path, discord_url, steam,
                 tracker,
                 db=None,
                 poll_interval=2.0,
                 respawn_window=300,
                 server_name="PZ Server"):
        self.log_path      = log_path
        self.discord       = DiscordWebhook(discord_url) if discord_url else None
        self.steam         = steam
        self.tracker       = tracker   # KillTracker instance
        self.db            = db
        self.poll_interval = poll_interval
        self.respawn_window = respawn_window
        self.server_name   = server_name

        self.console_patterns = {
            n: re.compile(rx) for n, rx in _CONSOLE_PATTERNS_RAW.items()
        }
        self.conn_patterns = {
            n: re.compile(rx) for n, rx in _CONNECTIONS_PATTERNS_RAW.items()
        }
        self._stop = threading.Event()

        # Session state (owned here, not in kill_tracker)
        # sessions[steamid]            = join timestamp
        # totals[steamid]              = cumulative real-world seconds on server
        # session_kills_start[steamid] = kill snapshot at session start
        # session_kills_deaths[steamid]= kills accumulated from deaths this session
        # dead[steamid]                = timestamp of most recent death
        # pending[steamid]             = timestamp of login-queue entry
        # logins[username]             = steamid (reverse map)
        # players[steamid]             = {"login": username}
        self._st = {
            "sessions":             {},
            "totals":               {},
            "session_kills_start":  {},
            "session_kills_deaths": {},
            "dead":                 {},
            "pending":              {},
            "logins":               {},
            "players":              {},
        }

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    # Main loops
    # ------------------------------------------------------------------
    def run(self):
        # Start kill tracker tail thread
        self.tracker.start()

        # Start connections log watcher thread
        conn_thread = threading.Thread(
            target=self._run_connections,
            name="pzp-connections-watcher", daemon=True)
        conn_thread.start()

        # Console log loop (server up/down, deaths)
        path = Path(self.log_path)
        last_inode, last_pos = None, 0
        try:
            last_inode = pc.file_identity(str(path))
            last_pos   = path.stat().st_size
        except OSError:
            pass

        while not self._stop.is_set():
            try:
                st = path.stat()
            except FileNotFoundError:
                self._stop.wait(self.poll_interval)
                continue
            cur_inode = pc.file_identity(str(path))
            if last_inode is not None and cur_inode != last_inode:
                last_pos = 0
            last_inode = cur_inode
            if st.st_size < last_pos:
                last_pos = 0
            if st.st_size > last_pos:
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_pos)
                        chunk = f.read()
                        last_pos = f.tell()
                    for line in chunk.splitlines():
                        self._handle_console(line)
                except OSError as e:
                    log.warning("Console log read error: %s", e)
            self._stop.wait(self.poll_interval)

    def _run_connections(self):
        current_path = None
        last_pos, last_inode = 0, None

        while not self._stop.is_set():
            latest = _find_connections_log()
            if latest != current_path:
                current_path = latest
                last_pos, last_inode = 0, None
                if current_path:
                    try:
                        last_pos = current_path.stat().st_size
                    except OSError:
                        pass
                    log.info("Connections watcher: tailing %s", current_path)

            if current_path is None:
                self._stop.wait(self.poll_interval)
                continue

            try:
                st = current_path.stat()
            except FileNotFoundError:
                current_path = None
                self._stop.wait(self.poll_interval)
                continue

            cur_inode = pc.file_identity(str(current_path))
            if last_inode is not None and cur_inode != last_inode:
                last_pos = 0
            last_inode = cur_inode
            if st.st_size < last_pos:
                last_pos = 0
            if st.st_size > last_pos:
                try:
                    with open(current_path, "r",
                              encoding="utf-8", errors="replace") as f:
                        f.seek(last_pos)
                        chunk = f.read()
                        last_pos = f.tell()
                    for line in chunk.splitlines():
                        self._handle_conn(line)
                except OSError as e:
                    log.warning("Connections log read error: %s", e)
            self._stop.wait(self.poll_interval)

    def _handle_console(self, line):
        for name, rx in self.console_patterns.items():
            m = rx.search(line)
            if m:
                getattr(self, f"on_{name}")(m, line)
                break

    def _handle_conn(self, line):
        for name, rx in self.conn_patterns.items():
            m = rx.search(line)
            if m:
                getattr(self, f"on_{name}")(m, line)
                break

    # ------------------------------------------------------------------
    # Milestone callback (called by KillTracker)
    # ------------------------------------------------------------------
    def on_milestone(self, steamid, username, threshold, tier,
                     current_kills, lifetime_kills, survived):
        if self.discord is None:
            return
        sp     = self.steam.profile(steamid)
        avatar = sp.get("avatar")
        msg    = _milestone_message(threshold, username, tier)
        if tier == "life":
            title  = f"\U0001f3c6 Milestone: {threshold:,} kills this life!"
            fields = [{"name": "Kills this life",
                       "value": f"{current_kills:,}", "inline": True}]
        else:
            title  = f"\U0001f31f Lifetime milestone: {threshold:,} kills!"
            fields = [{"name": "Lifetime kills",
                       "value": f"{lifetime_kills:,}", "inline": True}]
        self.discord.embed(COLOURS["GOLD"], title=title,
                           description=msg, thumbnail=avatar, fields=fields)

    # ------------------------------------------------------------------
    # Console event handlers
    # ------------------------------------------------------------------
    def on_server_up(self, m, line):
        now = time.time()
        # Clear session state on server restart
        self._st["sessions"].clear()
        self._st["session_kills_start"].clear()
        self._st["session_kills_deaths"].clear()
        if self.discord is None:
            return
        desc = None
        active_since = pc.server_active_since(self._make_cfg())
        if active_since and now - active_since < 3600:
            desc = f"Took {human_duration(now - active_since)} to start up."
        self.discord.embed(COLOURS["LIME"],
                           title=f"{self.server_name} is now **ONLINE**",
                           description=desc)

    def on_server_down(self, m, line):
        st = self._st
        now = time.time()
        # Compute uptime from sessions if available
        up_since = None  # server_up_since tracked separately if needed
        if self.discord is None:
            return
        self.discord.embed(COLOURS["RED"],
                           title=f"{self.server_name} has gone **OFFLINE**")

    def on_death(self, m, line):
        """Console log death line: user <name> died at ("""
        name    = m.groupdict().get("name", "?")
        st      = self._st
        steamid = st["logins"].get(name)
        if not steamid:
            log.debug("on_death: %r not in active logins, skipping", name)
            return

        # Get account data from tracker before it processes the death event
        # (the Lua death event will arrive via event log shortly; we use
        # the console death line as a signal to prepare the embed).
        # We wait briefly for the event log to catch up, then read.
        time.sleep(1.5)
        acc      = self.tracker.get_account(steamid, name)
        lifetime = self.tracker.get_lifetime(steamid)

        if steamid:
            st["dead"][steamid] = time.time()
            # Accumulate this run's kills into session deaths total
            current = acc["current"] if acc else 0
            st["session_kills_deaths"][steamid] = (
                st["session_kills_deaths"].get(steamid, 0) + current
            )

        if self.discord is None:
            return

        fields = []
        if acc:
            kills_this_run = acc["current"]
            fields.append({"name": "Kills this life",
                           "value": f"{kills_this_run:,}", "inline": True})
            fields.append({"name": "Lifetime kills",
                           "value": f"{lifetime:,}", "inline": True})
            ingame = _fmt_ingame_time(acc.get("survived"))
            if ingame:
                fields.append({"name": "Survived (in-game)",
                               "value": ingame, "inline": True})

        sp     = self.steam.profile(steamid)
        avatar = sp.get("avatar")
        self.discord.embed(
            COLOURS["ORANGE"],
            title=f"{name} has now completed their playthrough.",
            thumbnail=avatar,
            fields=fields or None,
        )

    # ------------------------------------------------------------------
    # Connection event handlers
    # ------------------------------------------------------------------
    def on_login_queue(self, m, line):
        steamid = m.groupdict().get("steamid", "")
        if steamid:
            self._st["pending"][steamid] = time.time()

    def on_join(self, m, line):
        d        = m.groupdict()
        steamid  = d.get("steamid", "")
        username = d.get("username", "") or steamid
        now      = time.time()
        st       = self._st

        st["pending"].pop(steamid, None)
        is_new_session = steamid not in st["sessions"]
        st["sessions"].setdefault(steamid, now)
        st["players"].setdefault(steamid, {})["login"] = username
        st["logins"][username] = steamid

        # Ensure account exists and get current data
        lifetime, current_kills, survived = self.tracker.on_join(steamid, username)

        if is_new_session:
            st["session_kills_start"][steamid]  = current_kills
            st["session_kills_deaths"][steamid] = 0

        # Respawn check
        death_at = st["dead"].get(steamid)
        if death_at and now - death_at <= self.respawn_window:
            del st["dead"][steamid]
            if self.db is not None:
                sp = self.steam.profile(steamid)
                self.db.on_join(steamid=steamid, username=username,
                                steam_name=sp.get("persona"),
                                avatar_url=sp.get("avatar"),
                                pz_hours=sp.get("pz_hours"),
                                kills_snapshot=0, hours_survived=0)
            if self.discord is not None:
                sp     = self.steam.profile(steamid)
                fields = [{"name": "Kills this run",
                           "value": str(current_kills), "inline": True}]
                self.discord.embed(
                    COLOURS["DARKVIOLET"], title="Respawn notice:",
                    description=random.choice(RESPAWN_MESSAGES).format(name=username),
                    thumbnail=sp.get("avatar"), fields=fields)
            return

        st["dead"].pop(steamid, None)

        sp      = self.steam.profile(steamid)
        persona = sp.get("persona") or username
        avatar  = sp.get("avatar")

        if self.db is not None:
            self.db.on_join(steamid=steamid, username=username,
                            steam_name=persona, avatar_url=avatar,
                            pz_hours=sp.get("pz_hours"),
                            kills_snapshot=current_kills,
                            hours_survived=survived)

        if self.discord is None:
            return

        profile = f"https://steamcommunity.com/profiles/{steamid}"
        fields  = []

        # Hours on Record
        if sp.get("pz_hours") is not None:
            fields.append({"name": "Hours on Record",
                           "value": f"{sp['pz_hours']:,}", "inline": False})
        # Lifetime kills
        fields.append({"name": "Lifetime kills",
                       "value": f"{lifetime:,}", "inline": False})
        # Logging in as
        fields.append({"name": "Logging in as",
                       "value": f"**{username}**", "inline": False})
        # Survived for | Current run
        ingame = _fmt_ingame_time(survived)
        if ingame:
            fields.append({"name": "Survived for",
                           "value": ingame, "inline": True})
        run_str = str(current_kills) if current_kills > 0 else "0"
        fields.append({"name": "Current run",
                       "value": f"{run_str} kills", "inline": True})

        # Recently played
        recent = sp.get("recent_games", [])
        if recent:
            for i, g in enumerate(recent[:2]):
                hrs         = g.get("total_hours", 0)
                name_g      = g.get("name", "?")
                last_played = g.get("last_played", 0)
                lp_str      = (datetime.fromtimestamp(last_played).strftime("%d %b %Y")
                               if last_played else "")
                val         = f"**{name_g}**\n{hrs:,} hrs on record"
                if lp_str:
                    val += f"\nLast played: {lp_str}"
                field_name = f"{persona} has also played:" if i == 0 else "\u200b"
                fields.append({"name": field_name, "value": val, "inline": True})

        self.discord.embed(
            COLOURS["GREEN"],
            title="New connection:",
            description=f"Steam Profile: [{persona}]({profile})",
            thumbnail=avatar,
            fields=fields or None,
        )

    def on_disconnect(self, m, line):
        d        = m.groupdict()
        steamid  = d.get("steamid", "")
        st       = self._st

        was_pending = bool(st["pending"].pop(steamid, None))
        if steamid not in st["sessions"]:
            if was_pending:
                return
            if self.discord is None:
                return
            name   = (d.get("username") or
                      st["players"].get(steamid, {}).get("login") or steamid)
            avatar = self.steam.profile(steamid).get("avatar")
            self.discord.embed(COLOURS["RED"],
                               title=f"{name} has disconnected",
                               thumbnail=avatar)
            return

        name    = (d.get("username") or
                   st["players"].get(steamid, {}).get("login") or steamid)
        now     = time.time()
        session = now - st["sessions"].pop(steamid)
        st["totals"][steamid] = st["totals"].get(steamid, 0) + session

        # Session kills = (current kills at disconnect - snapshot at join)
        #                 + kills accumulated from deaths this session
        current_now   = self.tracker.on_disconnect(steamid, name)
        start_snap    = st["session_kills_start"].pop(steamid, 0)
        deaths_kills  = st["session_kills_deaths"].pop(steamid, 0)
        partial       = max(0, current_now - start_snap)
        session_kills = deaths_kills + partial

        if self.db is not None:
            acc = self.tracker.get_account(steamid, name)
            self.db.on_disconnect(steamid=steamid, username=name,
                                  kills_snapshot=current_now,
                                  hours_survived=acc.get("survived") if acc else None)

        if self.discord is None:
            return

        total  = st["totals"][steamid]
        avatar = self.steam.profile(steamid).get("avatar")
        lines  = [f"{name} was online for {human_duration(session)}",
                  f"Total time on server:\n{human_duration(total)}"]
        if total >= 3600:
            lines.append(f"({int(total // 3600)} Hours)")

        fields = [
            {"name": "Kills this session",
             "value": str(session_kills), "inline": True},
            {"name": "Kills this run",
             "value": str(current_now), "inline": True},
        ]

        if steamid in st["dead"]:
            del st["dead"][steamid]
            msg = random.choice(RAGE_MESSAGES).format(name=name)
            self.discord.embed(COLOURS["RED"], title=f"{name} rage-quit",
                               description="\n".join([msg, ""] + lines),
                               thumbnail=avatar, fields=fields)
        else:
            self.discord.embed(COLOURS["RED"],
                               title=f"{name} has disconnected",
                               description="\n".join(lines),
                               thumbnail=avatar, fields=fields)

    def _make_cfg(self):
        cfg = configparser.ConfigParser()
        cfg.read(self._config_path)
        return cfg


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_watcher(config_path):
    cfg = configparser.ConfigParser()
    cfg.read(config_path)

    discord_section = None
    for s in ("discord", "dizcord"):
        if cfg.has_section(s):
            discord_section = s
            break

    log_path = cfg.get("paths", "console_log", fallback="").strip()
    if not log_path:
        log.warning("No console_log configured; player-event watcher disabled")
        return None

    webhook_url = ""
    if discord_section:
        webhook_url = cfg.get(discord_section, "webhook_url", fallback="").strip()

    steam_enabled = cfg.get("steam", "enabled", fallback="false").strip().lower() \
                    not in ("false", "0", "no", "")
    steam_api_key = cfg.get("steam", "api_key", fallback="").strip()
    cache_hours   = int(cfg.get("steam", "cache_hours", fallback="24").strip() or "24")
    steam         = SteamCache(steam_api_key, cache_hours) if steam_enabled else _NoSteam()

    kills_path    = ""
    event_log     = ""
    state_path    = ""
    player_db_path = ""
    poll_interval  = 2.0
    respawn_window = 300

    for section in ("player_events", "dizcord"):
        if cfg.has_section(section):
            kills_path     = kills_path     or cfg.get(section, "kills_file",    fallback="").strip()
            event_log      = event_log      or cfg.get(section, "event_log",     fallback="").strip()
            state_path     = state_path     or cfg.get(section, "state_file",    fallback="").strip()
            player_db_path = player_db_path or cfg.get(section, "player_db",     fallback="").strip()
            try:
                poll_interval = float(cfg.get(section, "poll_interval", fallback="2").strip() or "2")
            except ValueError:
                pass
            try:
                respawn_window = int(cfg.get(section, "respawn_window", fallback="300").strip() or "300")
            except ValueError:
                pass

    db = pdb.PlayerDB(player_db_path) if player_db_path else None

    server_name = cfg.get("server", "name", fallback="PZ Server").strip()
    ini_path    = cfg.get("paths", "server_ini", fallback="").strip()
    if ini_path and Path(ini_path).exists():
        try:
            for raw in Path(ini_path).read_text(encoding="utf-8", errors="replace").splitlines():
                if raw.strip().lower().startswith("publicname="):
                    v = raw.partition("=")[2].strip()
                    if v:
                        server_name = v
                    break
        except OSError:
            pass

    # Build KillTracker — watcher passes itself as milestone callback
    watcher = PlayerEventWatcher(
        log_path=log_path,
        discord_url=webhook_url,
        steam=steam,
        tracker=None,   # set below after watcher exists
        db=db,
        poll_interval=poll_interval,
        respawn_window=respawn_window,
        server_name=server_name,
    )
    watcher._config_path = config_path

    if kills_path and event_log:
        tracker = kt.build_tracker(
            yaml_path=kills_path,
            event_log_path=event_log,
            milestone_callback=watcher.on_milestone,
            poll_interval=poll_interval,
        )
        watcher.tracker = tracker
    else:
        log.warning("kills_file or event_log not configured; kill tracking disabled")
        watcher.tracker = None

    return watcher
