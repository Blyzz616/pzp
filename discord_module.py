"""
discord_module.py — Player-event watcher + Discord embed poster for pzpanel.

Tails two log files:
  1. server-console.txt  — server up/down, deaths (unchanged in B42)
  2. Logs/*_connections.txt — join, disconnect, login-queue (B42 moved
     these out of the console log into a structured connections file;
     always the lexicographically highest *_connections.txt in the dir)

Also polls the PZP kill-count mod's output file and surfaces everything as:
  (1) live state for main.py's /killboard page and the SQLite player_db
  (2) Discord embeds (optional)

Kill/survival tracking runs unconditionally when the config block is present
(even with no webhook configured), since the killboard needs this data with
or without Discord.

Milestone shout-outs:
  Per-life:    1, 50, 100, 500, 1000, 5000, 10000
               Fires when the kills file crosses the threshold on any poll.
               Resets when the character dies (file drops back to 0).
  Lifetime:    Same thresholds, then every 10k after (20k, 30k, ...)
               Fires at most once per threshold per steamid, ever.
               Tracked in persistent state.

Kills file format (v4.2.3): JSON keyed by steamid.
  {steamid: {username: {kills, alive, survived}, lifetimeKills: N}}
"""

import configparser
import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path

import requests

import platform_compat as pc
import player_db as pdb

log = logging.getLogger("pzpanel.discord")

# ---------------------------------------------------------------------------
# Logs directory (B42 connections file lives here)
# ---------------------------------------------------------------------------
_LOGS_DIR = Path("/home/pzserver/Zomboid/Logs")


def _find_connections_log():
    """Return the lexicographically highest *_connections.txt in _LOGS_DIR."""
    try:
        candidates = sorted(_LOGS_DIR.glob("*_connections.txt"))
        return candidates[-1] if candidates else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Log-line patterns
# ---------------------------------------------------------------------------

# Console log: server lifecycle + deaths only (B42 removed join/disconnect)
_CONSOLE_PATTERNS_RAW = {
    "server_up":   r"SERVER STARTED",
    "server_down": r"Quit Java",
    "death":       r"\buser (?P<name>\S+) died at \(",
}

# Connections log: structured key="value" lines
_CONNECTIONS_PATTERNS_RAW = {
    "login_queue": (
        r'message="login-queue-request"'
        r'.*?steam-id="(?P<steamid>\d+)"'
    ),
    "join": (
        r'event="fully-connected"'
        r'.*?steam-id="(?P<steamid>\d+)"'
        r'.*?username="(?P<username>[^"]+)"'
    ),
    "disconnect": (
        r'event="disconnect"'
        r'.*?steam-id="(?P<steamid>\d+)"'
        r'.*?username="(?P<username>[^"]+)"'
    ),
}

COLOURS = {
    "LIME":       0x00FF00,
    "GREEN":      0x2ECC71,
    "RED":        0xE74C3C,
    "ORANGE":     0xE67E22,
    "YELLOW":     0xF1C40F,
    "BLUE":       0x3498DB,
    "DARKVIOLET": 0x9B59B6,
    "GREY":       0x95A5A6,
    "GOLD":       0xFFD700,
}

# ---------------------------------------------------------------------------
# Milestone thresholds
# ---------------------------------------------------------------------------
_LIFE_MILESTONES = [1, 50, 100, 500, 1000, 5000, 10000]
_LIFETIME_BASE   = [1, 50, 100, 500, 1000, 5000, 10000]
_LIFETIME_STEP   = 10000


def _lifetime_milestones_up_to(n):
    result = list(_LIFETIME_BASE)
    nxt = _LIFETIME_BASE[-1] + _LIFETIME_STEP
    while nxt <= n:
        result.append(nxt)
        nxt += _LIFETIME_STEP
    return result


def _milestone_message(threshold, name, tier):
    if threshold == 1:
        templates = [
            "{name} draws first blood. The apocalypse just got personal.",
            "Kill #1 for {name}. The horde has been warned.",
            "{name} opens their account. Only several thousand to go.",
        ]
    elif tier == "life":
        templates = [
            "**{name}** just hit **{n:,}** kills this life. The zombies are running out of volunteers.",
            "**{n:,}** kills this life for **{name}**. Truly a survivor.",
            "**{name}** — **{n:,}** this run. The horde remembers.",
        ]
    else:
        templates = [
            "**{name}** crosses **{n:,}** lifetime kills. A monument to the fallen undead.",
            "**{n:,}** lifetime kills for **{name}**. Legend.",
            "**{name}** — **{n:,}** all-time. The apocalypse bows.",
        ]
    return random.choice(templates).format(name=name, n=threshold)


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
# Discord webhook helper
# ---------------------------------------------------------------------------
class DiscordWebhook:
    def __init__(self, url):
        self.url = url

    def embed(self, colour, *, title=None, description=None, thumbnail=None,
              fields=None, footer=None):
        embed = {"color": colour}
        if title:
            embed["title"] = title
        if description:
            embed["description"] = description
        if thumbnail:
            embed["thumbnail"] = {"url": thumbnail}
        if fields:
            embed["fields"] = fields
        if footer:
            embed["footer"] = {"text": footer}
        try:
            r = requests.post(self.url, json={"embeds": [embed]}, timeout=8)
            r.raise_for_status()
        except Exception as e:
            log.warning("Discord webhook failed: %s", e)


# ---------------------------------------------------------------------------
# Kills file reader
# Format (v4.2.3): JSON keyed by steamid.
# {steamid: {username: {kills, alive, survived}, lifetimeKills: N}}
# ---------------------------------------------------------------------------
class KillsFile:
    def __init__(self, path):
        self.path = path
        self._mtime = None

    def poll(self, force=False):
        try:
            st = self.path.stat()
        except OSError:
            return None
        if not force and self._mtime is not None and st.st_mtime == self._mtime:
            return None
        self._mtime = st.st_mtime
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not text:
            return None
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            log.warning("Kills file is not valid JSON, skipping")
            return None
        # Returns {steamid: {characters: {username: {kills, alive, survived}}, lifetimeKills: N}}
        result = {}
        for steamid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            lifetime = entry.get("lifetimeKills", 0)
            characters = {}
            for key, val in entry.items():
                if key == "lifetimeKills" or not isinstance(val, dict):
                    continue
                characters[key] = {
                    "kills": int(val.get("kills", 0)),
                    "alive": bool(val.get("alive", False)),
                    "survived": float(val.get("survived", 0.0)),
                }
            result[steamid] = {
                "characters": characters,
                "lifetimeKills": int(lifetime),
            }
        return result


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------
class State:
    def __init__(self, path):
        self.path = path
        self.data = {
            "sessions": {},
            "totals": {},
            "players": {},
            "logins": {},
            "dead": {},
            "pending": {},
            "server_up_since": None,
            "kills_current_run": {},
            "hours_current_run": {},
            "kills_lifetime_by_steamid": {},
            "kills_last_updated": None,
            "session_kills": {},
            "milestones_life_fired": {},
            "milestones_lifetime_fired": {},
        }
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self.path and Path(self.path).exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in loaded.get("milestones_life_fired", {}).items():
                    loaded["milestones_life_fired"][k] = set(v)
                for k, v in loaded.get("milestones_lifetime_fired", {}).items():
                    loaded["milestones_lifetime_fired"][k] = set(v)
                self.data.update(loaded)
            except Exception as e:
                log.warning("Could not load state from %s: %s", self.path, e)

    def save(self):
        if not self.path:
            return
        try:
            with self._lock:
                data_copy = dict(self.data)
                data_copy["milestones_life_fired"] = {
                    k: list(v) for k, v in self.data["milestones_life_fired"].items()
                }
                data_copy["milestones_lifetime_fired"] = {
                    k: list(v) for k, v in self.data["milestones_lifetime_fired"].items()
                }
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data_copy, f, indent=2)
                os.replace(tmp, self.path)
        except Exception as e:
            log.warning("Could not save state to %s: %s", self.path, e)


# ---------------------------------------------------------------------------
# Steam profile cache
# ---------------------------------------------------------------------------
class SteamCache:
    def __init__(self, api_key, cache_hours=24):
        self.api_key = api_key
        self.cache_seconds = cache_hours * 3600
        self._cache = {}

    def profile(self, steamid):
        if not steamid:
            return {}
        now = time.time()
        cached = self._cache.get(steamid)
        if cached and now - cached["_ts"] < self.cache_seconds:
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
                result["persona"] = p.get("personaname")
                result["avatar"] = p.get("avatarfull") or p.get("avatar")
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
                log.debug("Steam hours lookup failed for %s: %s", steamid, e)
            try:
                r3 = requests.get(
                    "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
                    params={"key": self.api_key, "steamid": steamid, "include_appinfo": 1},
                    timeout=8)
                r3.raise_for_status()
                all_games = r3.json().get("response", {}).get("games", [])
                recent = sorted(
                    [g for g in all_games if g.get("appid") != 108600 and g.get("rtime_last_played", 0) > 0],
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
    def profile(self, steamid):
        return {}


# ---------------------------------------------------------------------------
# Main watcher
# ---------------------------------------------------------------------------
class PlayerEventWatcher:
    def __init__(self, log_path, state_path, discord_url, steam,
                 kills_file_path=None, db=None,
                 poll_interval=2.0, respawn_window=300,
                 server_name="PZ Server"):
        self.log_path = log_path
        self.state = State(state_path)
        self.discord = DiscordWebhook(discord_url) if discord_url else None
        self.steam = steam
        self.kills_file = KillsFile(Path(kills_file_path).expanduser()) if kills_file_path else None
        self.db = db
        self.poll_interval = poll_interval
        self.respawn_window = respawn_window
        self.server_name = server_name
        self.console_patterns = {
            name: re.compile(rx) for name, rx in _CONSOLE_PATTERNS_RAW.items()
        }
        self.conn_patterns = {
            name: re.compile(rx) for name, rx in _CONNECTIONS_PATTERNS_RAW.items()
        }
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    # Console log loop (server up/down, deaths)
    # ------------------------------------------------------------------
    def run(self):
        conn_thread = threading.Thread(
            target=self._run_connections, name="pzp-connections-watcher", daemon=True)
        conn_thread.start()

        path = Path(self.log_path)
        last_inode = None
        try:
            last_pos = path.stat().st_size
        except OSError:
            last_pos = 0
        kills_poll_interval = 30
        last_kills_poll = 0

        while not self._stop.is_set():
            try:
                st = path.stat()
            except FileNotFoundError:
                self._stop.wait(self.poll_interval)
                continue
            current_inode = pc.file_identity(str(path))
            if last_inode is not None and current_inode != last_inode:
                last_pos = 0
            last_inode = current_inode
            if st.st_size < last_pos:
                last_pos = 0
            if st.st_size > last_pos:
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_pos)
                        chunk = f.read()
                        last_pos = f.tell()
                    for line in chunk.splitlines():
                        self._handle_console_line(line)
                except OSError as e:
                    log.warning("Error reading console log %s: %s", path, e)
            now = time.time()
            if now - last_kills_poll >= kills_poll_interval:
                self._refresh_kills()
                last_kills_poll = now
            self._stop.wait(self.poll_interval)

    # ------------------------------------------------------------------
    # Connections log loop (join, disconnect, login-queue)
    # ------------------------------------------------------------------
    def _run_connections(self):
        current_path = None
        last_pos = 0
        last_inode = None

        while not self._stop.is_set():
            latest = _find_connections_log()

            if latest != current_path:
                current_path = latest
                last_pos = 0
                last_inode = None
                if current_path:
                    try:
                        last_pos = current_path.stat().st_size
                    except OSError:
                        last_pos = 0
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

            current_inode = pc.file_identity(str(current_path))
            if last_inode is not None and current_inode != last_inode:
                last_pos = 0
            last_inode = current_inode
            if st.st_size < last_pos:
                last_pos = 0
            if st.st_size > last_pos:
                try:
                    with open(current_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_pos)
                        chunk = f.read()
                        last_pos = f.tell()
                    for line in chunk.splitlines():
                        self._handle_conn_line(line)
                except OSError as e:
                    log.warning("Error reading connections log %s: %s", current_path, e)

            self._stop.wait(self.poll_interval)

    def _handle_console_line(self, line):
        for name, rx in self.console_patterns.items():
            m = rx.search(line)
            if m:
                getattr(self, f"on_{name}")(m, line)
                break

    def _handle_conn_line(self, line):
        for name, rx in self.conn_patterns.items():
            m = rx.search(line)
            if m:
                getattr(self, f"on_{name}")(m, line)
                break

    # ------------------------------------------------------------------
    # Kill / milestone helpers
    # ------------------------------------------------------------------

    def _refresh_kills(self, force=False):
        if self.kills_file is None:
            return
        updated = self.kills_file.poll(force=force)
        if updated is None:
            return
        # updated: {steamid: {characters: {username: {kills,alive,survived}}, lifetimeKills: N}}
        st = self.state.data
        current = st.setdefault("kills_current_run", {})
        hours_current = st.setdefault("hours_current_run", {})
        is_first_poll = not current

        for steamid, entry in updated.items():
            lifetime = entry.get("lifetimeKills", 0)
            st.setdefault("kills_lifetime_by_steamid", {})[steamid] = lifetime
            for username, ch in entry.get("characters", {}).items():
                st.setdefault("logins", {})[username] = steamid
                new_kills = ch["kills"]
                old_kills = current.get(username, 0)

                if not ch["alive"] and old_kills > 0 and new_kills == 0:
                    current.pop(username, None)
                    hours_current.pop(username, None)
                    st.setdefault("milestones_life_fired", {}).pop(username, None)
                    continue

                current[username] = new_kills
                if ch["survived"] is not None:
                    hours_current[username] = ch["survived"]

                if is_first_poll and new_kills > 0:
                    life_fired = st.setdefault("milestones_life_fired", {}).setdefault(username, set())
                    for t in _LIFE_MILESTONES:
                        if new_kills >= t:
                            life_fired.add(t)
                    lifetime_fired = st.setdefault("milestones_lifetime_fired", {}).setdefault(steamid, set())
                    for t in _lifetime_milestones_up_to(lifetime):
                        if lifetime >= t:
                            lifetime_fired.add(t)
                elif new_kills > old_kills and self.discord is not None:
                    self._check_milestones(username, steamid, old_kills, new_kills, lifetime)

        st["kills_last_updated"] = time.time()
        self.state.save()

    def _check_milestones(self, username, steamid, old_kills, new_kills, lifetime=None):
        st = self.state.data
        life_fired = st.setdefault("milestones_life_fired", {}).setdefault(username, set())
        lifetime_key = steamid or username
        lifetime_fired = st.setdefault("milestones_lifetime_fired", {}).setdefault(
            lifetime_key, set())
        if lifetime is None:
            lifetime = st.get("kills_lifetime_by_steamid", {}).get(steamid, new_kills)
        avatar = (st.get("steam_cache", {}).get(steamid, {}).get("avatar")
                  if steamid else None)

        for threshold in _LIFE_MILESTONES:
            if threshold in life_fired:
                continue
            if old_kills < threshold <= new_kills:
                life_fired.add(threshold)
                msg = _milestone_message(threshold, username, "life")
                fields = [{"name": "Kills this life", "value": f"{new_kills:,}", "inline": True}]
                self.discord.embed(
                    COLOURS["GOLD"],
                    title=f"\U0001f3c6 Milestone: {threshold:,} kills this life!",
                    description=msg,
                    thumbnail=avatar,
                    fields=fields,
                )

        old_lifetime = lifetime - new_kills + old_kills
        for threshold in _lifetime_milestones_up_to(lifetime):
            if threshold in lifetime_fired:
                continue
            if old_lifetime < threshold <= lifetime:
                lifetime_fired.add(threshold)
                msg = _milestone_message(threshold, username, "lifetime")
                fields = [{"name": "Lifetime kills",
                           "value": f"{lifetime:,}", "inline": True}]
                self.discord.embed(
                    COLOURS["GOLD"],
                    title=f"\U0001f31f Lifetime milestone: {threshold:,} kills!",
                    description=msg,
                    thumbnail=avatar,
                    fields=fields,
                )

    def _kills_field(self, username):
        kills = self.state.data.get("kills_current_run", {}).get(username)
        if kills is None:
            return None
        return {"name": "Kills this run", "value": str(kills), "inline": True}

    def _roll_kills(self, name, steamid, is_death=False):
        st = self.state.data
        raw = st.get("kills_current_run", {}).pop(name, None)
        if is_death:
            st.get("hours_current_run", {}).pop(name, None)
            st.setdefault("milestones_life_fired", {}).pop(name, None)
            if steamid and raw is not None:
                sess = st.setdefault("session_kills", {})
                sess[steamid] = sess.get(steamid, 0) + max(0, raw)
        return raw

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_server_up(self, m, line):
        now = time.time()
        st = self.state.data
        st["server_up_since"] = now
        st["kills_current_run"] = {}
        st["hours_current_run"] = {}
        st["milestones_life_fired"] = {}
        st.pop("session_kills_start", None)
        st.pop("kills_accounted_mtime", None)
        self.state.save()
        if self.discord is None:
            return
        desc = None
        active_since = pc.server_active_since(self._make_cfg())
        if active_since and now - active_since < 3600:
            desc = f"Took {human_duration(now - active_since)} to start up."
        self.discord.embed(COLOURS["LIME"],
                           title=f"{self.server_name} is now **ONLINE**",
                           description=desc)

    def _make_cfg(self):
        cfg = configparser.ConfigParser()
        cfg.read(self._config_path)
        return cfg

    def on_server_down(self, m, line):
        st = self.state.data
        up_since = st.get("server_up_since")
        now = time.time()
        uptime = human_duration(now - up_since) if up_since else None
        st["server_up_since"] = None
        self.state.save()
        if self.discord is None:
            return
        self.discord.embed(COLOURS["RED"],
                           title=f"{self.server_name} has gone **OFFLINE**",
                           description=f"Was up for {uptime}." if uptime else None)

    def on_login_queue(self, m, line):
        steamid = m.groupdict().get("steamid", "")
        if not steamid:
            return
        self.state.data.setdefault("pending", {})[steamid] = time.time()
        self.state.save()

    def on_join(self, m, line):
        d = m.groupdict()
        steamid = d.get("steamid", "")
        username = (d.get("username")
                    or self.state.data["players"].get(steamid, {}).get("login")
                    or steamid)
        now = time.time()
        st = self.state.data
        st.setdefault("pending", {}).pop(steamid, None)
        is_new_session = steamid not in st["sessions"]
        st["sessions"].setdefault(steamid, now)
        st["players"].setdefault(steamid, {})["login"] = username
        st["logins"][username] = steamid
        self._refresh_kills(force=True)
        if is_new_session and self.kills_file is not None:
            st.setdefault("session_kills", {})[steamid] = 0
            start_snap = st.get("kills_current_run", {}).get(username, 0)
            st.setdefault("session_kills_start", {})[steamid] = start_snap

        death_at = st["dead"].get(steamid)
        if death_at and now - death_at <= self.respawn_window:
            del st["dead"][steamid]
            self.state.save()
            if self.db is not None:
                sp_respawn = self.steam.profile(steamid)
                self.db.on_join(
                    steamid=steamid, username=username,
                    steam_name=sp_respawn.get("persona"),
                    avatar_url=sp_respawn.get("avatar"),
                    pz_hours=sp_respawn.get("pz_hours"),
                    kills_snapshot=0,
                    hours_survived=0,
                )
            if self.discord is not None:
                sp = self.steam.profile(steamid)
                avatar = sp.get("avatar")
                fields = []
                kf = self._kills_field(username)
                if kf:
                    fields.append(kf)
                self.discord.embed(
                    COLOURS["DARKVIOLET"], title="Respawn notice:",
                    description=random.choice(RESPAWN_MESSAGES).format(name=username),
                    thumbnail=avatar, fields=fields or None)
            return
        st["dead"].pop(steamid, None)
        self.state.save()

        sp = self.steam.profile(steamid)
        persona = sp.get("persona") or username
        avatar = sp.get("avatar")

        if self.db is not None:
            self.db.on_join(
                steamid=steamid, username=username,
                steam_name=persona,
                avatar_url=avatar,
                pz_hours=sp.get("pz_hours"),
                kills_snapshot=st.get("kills_current_run", {}).get(username, 0),
                hours_survived=st.get("hours_current_run", {}).get(username, 0),
            )

        if self.discord is None:
            return

        profile = f"https://steamcommunity.com/profiles/{steamid}"
        fields = []

        kills_now = st.get("kills_current_run", {}).get(username, 0)
        session_start = st.get("session_kills_start", {}).get(steamid, kills_now)
        run_kills_so_far = kills_now - session_start
        lifetime_kills = st.get("kills_lifetime_by_steamid", {}).get(steamid)

        # Row 1: Hours on Record (full width)
        if sp.get("pz_hours") is not None:
            fields.append({"name": "Hours on Record",
                           "value": f"{sp['pz_hours']:,}", "inline": False})
        # Row 2: Lifetime kills (full width)
        if lifetime_kills is not None:
            fields.append({"name": "Lifetime kills", "value": f"{lifetime_kills:,}", "inline": False})
        # Logging in as (full width) — no spacer needed, inline:False handles the break
        fields.append({"name": "Logging in as", "value": f"**{username}**", "inline": False})
        # Survived for | Current run (inline)
        ingame_survived = _fmt_ingame_time(st.get("hours_current_run", {}).get(username))
        if ingame_survived:
            fields.append({"name": "Survived for", "value": ingame_survived, "inline": True})
        run_kills_str = str(kills_now) if kills_now > 0 else "No kills yet"
        fields.append({"name": "Current run", "value": f"{run_kills_str} kills", "inline": True})
        # Spacer before recently played
        fields.append({"name": "\u200b", "value": "\u200b", "inline": False})

        # Recently played: header + two games side by side
        recent = sp.get("recent_games", [])
        if recent:
            from datetime import datetime
            fields.append({"name": f"{persona or username} has also played:",
                           "value": "\u200b", "inline": False})
            for g in recent[:2]:
                hrs = g.get("total_hours", 0)
                name_g = g.get("name", "?")
                last_played = g.get("last_played", 0)
                lp_str = datetime.fromtimestamp(last_played).strftime("%d %b %Y") if last_played else ""
                val = f"{hrs:,} hrs on record"
                if lp_str:
                    val += f"\nLast played: {lp_str}"
                fields.append({"name": name_g, "value": val, "inline": True})

        self.discord.embed(
            COLOURS["GREEN"],
            title="New connection:",
            description=f"Steam Profile: [{persona or username}]({profile})",
            thumbnail=avatar,
            fields=fields or None,
        )

    def on_disconnect(self, m, line):
        d = m.groupdict()
        steamid = d.get("steamid", "")
        st = self.state.data
        was_pending = bool(st.get("pending", {}).pop(steamid, None))
        self.state.save()
        self._refresh_kills(force=True)

        if steamid not in st["sessions"]:
            self.state.save()
            if was_pending:
                return
            if self.discord is None:
                return
            name = (d.get("username") or st["players"].get(steamid, {}).get("login")
                    or steamid or "Unknown")
            avatar = st.get("steam_cache", {}).get(steamid, {}).get("avatar")
            self.discord.embed(COLOURS["RED"], title=f"{name} has disconnected", thumbnail=avatar)
            return

        name = (d.get("username") or st["players"].get(steamid, {}).get("login")
                or steamid or "Unknown")
        now = time.time()
        session = now - st["sessions"].pop(steamid)
        st["totals"][steamid] = st["totals"].get(steamid, 0) + session
        # Do NOT call _roll_kills on disconnect — run is not over.
        current_run_kills = st.get("kills_current_run", {}).get(name)
        hours_survived = st.get("hours_current_run", {}).get(name)
        start_snap = st.get("session_kills_start", {}).pop(steamid, 0)
        partial = (current_run_kills - start_snap) if current_run_kills is not None else 0
        session_kills_total = st.get("session_kills", {}).pop(steamid, 0) + max(0, partial)
        lifetime_kills_total = st.get("kills_lifetime_by_steamid", {}).get(steamid)
        self.state.save()

        if self.db is not None:
            self.db.on_disconnect(
                steamid=steamid, username=name,
                kills_snapshot=current_run_kills if current_run_kills is not None else 0,
                hours_survived=hours_survived,
            )

        if self.discord is None:
            return

        avatar = st.get("steam_cache", {}).get(steamid, {}).get("avatar")
        total = st["totals"][steamid]
        lines = [f"{name} was online for {human_duration(session)}",
                 f"Total time on server:\n{human_duration(total)}"]
        if total >= 3600:
            lines.append(f"({int(total // 3600)} Hours)")
        fields = []
        fields.append({"name": "Kills this session",
                       "value": str(session_kills_total), "inline": True})
        if current_run_kills is not None:
            fields.append({"name": "Kills this run",
                           "value": str(current_run_kills), "inline": True})

        if steamid in st["dead"]:
            del st["dead"][steamid]
            self.state.save()
            msg = random.choice(RAGE_MESSAGES).format(name=name)
            self.discord.embed(COLOURS["RED"], title=f"{name} rage-quit",
                               description="\n".join([msg, ""] + lines),
                               thumbnail=avatar, fields=fields or None)
        else:
            self.discord.embed(COLOURS["RED"], title=f"{name} has disconnected",
                               description="\n".join(lines) or None,
                               thumbnail=avatar, fields=fields or None)

    def on_death(self, m, line):
        name = m.groupdict().get("name", "?")
        st = self.state.data
        self._refresh_kills(force=True)
        steamid = st["logins"].get(name)
        if not st["logins"].get(name):
            log.debug("on_death: %r not in active logins, skipping (likely NPC)", name)
            return

        hours_survived = st.get("hours_current_run", {}).get(name)
        kills_this_run = self._roll_kills(name, steamid, is_death=True)

        if self.db is not None and steamid:
            self.db.on_death(
                steamid=steamid, username=name,
                kills_snapshot=kills_this_run if kills_this_run is not None else 0,
                hours_survived=hours_survived,
            )

        if steamid:
            st["dead"][steamid] = time.time()
        self.state.save()

        if self.discord is None:
            return

        fields = []
        if kills_this_run is not None:
            fields.append({"name": "Kills this life",
                           "value": f"{kills_this_run:,}", "inline": True})
            lifetime_after = st.get("kills_lifetime_by_steamid", {}).get(steamid)
            if lifetime_after is not None:
                fields.append({"name": "Lifetime kills",
                               "value": f"{lifetime_after:,}", "inline": True})
        if hours_survived is not None:
            ingame = _fmt_ingame_time(hours_survived)
            if ingame:
                fields.append({"name": "Survived (in-game)",
                               "value": ingame, "inline": True})

        avatar = (st.get("steam_cache", {}).get(steamid, {}).get("avatar")
                  if steamid else None)
        self.discord.embed(
            COLOURS["ORANGE"],
            title=f"{name} has now completed their playthrough.",
            thumbnail=avatar,
            fields=fields or None,
        )


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
        log.warning("No console_log path configured; player-event watcher disabled")
        return None

    webhook_url = ""
    if discord_section:
        webhook_url = cfg.get(discord_section, "webhook_url", fallback="").strip()

    steam_enabled = cfg.get("steam", "enabled", fallback="false").strip().lower() not in ("false", "0", "no", "")
    steam_api_key = cfg.get("steam", "api_key", fallback="").strip()
    cache_hours = int(cfg.get("steam", "cache_hours", fallback="24").strip() or "24")

    steam = SteamCache(steam_api_key, cache_hours) if steam_enabled else _NoSteam()

    kills_path = ""
    state_path = ""
    player_db_path = ""
    poll_interval = 2.0
    respawn_window = 300

    for section in ("player_events", "dizcord"):
        if cfg.has_section(section):
            kills_path = kills_path or cfg.get(section, "kills_file", fallback="").strip()
            state_path = state_path or cfg.get(section, "state_file", fallback="").strip()
            player_db_path = player_db_path or cfg.get(section, "player_db", fallback="").strip()
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
    ini_path = cfg.get("paths", "server_ini", fallback="").strip()
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

    watcher = PlayerEventWatcher(
        log_path=log_path,
        state_path=state_path or None,
        discord_url=webhook_url,
        steam=steam,
        kills_file_path=kills_path or None,
        db=db,
        poll_interval=poll_interval,
        respawn_window=respawn_window,
        server_name=server_name,
    )
    watcher._config_path = config_path
    return watcher
