"""
discord_module.py — Player-event watcher + Discord embed poster for pzpanel.

Tails two log files:
  1. server-console.txt      — server up/down
  2. Logs/*_connections.txt  — join, disconnect, login-queue
     (always the lexicographically highest *_connections.txt in the dir)

Kill reports (including deaths) come from the pzp mod's event log via
kill_tracker.py; all kill and session data lives in player_db.py.
This module owns:
  - Connection event watching (join / disconnect)
  - Discord embeds: server up/down, join, disconnect / rage-quit, death,
    kill milestones
  - Steam profile cache
"""

__version__ = "5.2.0"

import configparser
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

DEATH_MESSAGES = [
    "The horde claims another.",
    "Knox County adds one more to the count.",
    "Should have checked that last corner.",
    "Bitten, scratched, or just unlucky. Either way, it's over.",
    "Another survivor joins the shambling masses.",
    "The zombies send their regards.",
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


def fmt_total_time(seconds):
    """Total time on server: "1d 1h" from a day up, else human_duration."""
    seconds = int(seconds)
    if seconds < 86400:
        return human_duration(seconds)
    days, hours = divmod(seconds // 3600, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _fmt_hours(hours):
    """74.0 -> "74", 74.25 -> "74.3"."""
    return f"{hours:,.1f}".rstrip("0").rstrip(".")


def _md(text):
    """Escape Discord markdown in user-supplied text."""
    return re.sub(r"([\\*_~`|>\[\]])", r"\\\1", str(text))


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
    def __init__(self, log_path, discord_url, steam, db,
                 tracker=None,
                 poll_interval=2.0,
                 server_name="PZ Server"):
        self.log_path      = log_path
        self.discord       = DiscordWebhook(discord_url) if discord_url else None
        self.steam         = steam
        self.db            = db        # player_db.PlayerDB
        self.tracker       = tracker   # kill_tracker.KillTracker
        self.poll_interval = poll_interval
        self.server_name   = server_name

        self.console_patterns = {
            n: re.compile(rx) for n, rx in _CONSOLE_PATTERNS_RAW.items()
        }
        self.conn_patterns = {
            n: re.compile(rx) for n, rx in _CONNECTIONS_PATTERNS_RAW.items()
        }
        self._stop = threading.Event()
        # steamid -> login-queue timestamp (in the queue, not yet joined)
        self._pending = {}

    def stop(self):
        self._stop.set()
        if self.tracker is not None:
            self.tracker.stop()

    # ------------------------------------------------------------------
    # Main loops
    # ------------------------------------------------------------------
    def run(self):
        if self.tracker is not None:
            self.tracker.start()

        # Start connections log watcher thread
        conn_thread = threading.Thread(
            target=self._run_connections,
            name="pzp-connections-watcher", daemon=True)
        conn_thread.start()

        # Console log loop (server up/down)
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
                try:
                    getattr(self, f"on_{name}")(m, line)
                except Exception:
                    log.exception("Handler on_%s failed for line %r", name, line)
                break

    def _handle_conn(self, line):
        for name, rx in self.conn_patterns.items():
            m = rx.search(line)
            if m:
                try:
                    getattr(self, f"on_{name}")(m, line)
                except Exception:
                    log.exception("Handler on_%s failed for line %r", name, line)
                break

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _drain(self):
        """Apply pending mod reports so join/disconnect see current kills."""
        if self.tracker is None:
            return
        try:
            self.tracker.drain()
        except Exception:
            log.exception("Kill tracker drain failed")

    def _profile(self, steamid):
        """Steam profile (cached), copied into the DB for the killboard."""
        if not steamid:
            return {}
        sp = self.steam.profile(steamid)
        if sp.get("persona") or sp.get("avatar"):
            self.db.update_persona(steamid, name=sp.get("persona"),
                                   avatar_url=sp.get("avatar"),
                                   profile_url=sp.get("profile_url"),
                                   pz_hours=sp.get("pz_hours"))
        return sp

    def _persona(self, steamid, sp, fallback):
        return (sp.get("persona")
                or (self.db.persona_name(steamid) if steamid else None)
                or fallback)

    # ------------------------------------------------------------------
    # Kill tracker callbacks
    # ------------------------------------------------------------------
    def on_mod_death(self, report):
        if self.discord is None:
            return
        name = report["name"] or report["username"]
        sp   = self._profile(report["steamid"])
        fields = [{"name": "Kills this run:",
                   "value": f"{report['kills']:,}", "inline": True}]
        survived = _fmt_ingame_time(report["hours"])
        if survived:
            fields.append({"name": "Survived:", "value": survived, "inline": True})
        self.discord.embed(COLOURS["ORANGE"], title=f"{name} died",
                           description=random.choice(DEATH_MESSAGES),
                           thumbnail=sp.get("avatar"), fields=fields)

    def on_milestone(self, report, threshold, tier):
        if self.discord is None:
            return
        sp = self._profile(report["steamid"])
        if tier == "life":
            name   = report["name"] or report["username"]
            title  = f"\U0001f3c6 Milestone: {threshold:,} kill{'s' if threshold != 1 else ''} this life!"
            fields = [{"name": "Kills this life",
                       "value": f"{report['kills']:,}", "inline": True}]
        else:
            name   = self._persona(report["steamid"], sp, report["username"])
            title  = f"\U0001f31f Lifetime milestone: {threshold:,} kill{'s' if threshold != 1 else ''}!"
            fields = [{"name": "Lifetime kills",
                       "value": f"{report['persona_kills']:,}", "inline": True}]
        self.discord.embed(COLOURS["GOLD"], title=title,
                           description=_milestone_message(threshold, _md(name), tier),
                           thumbnail=sp.get("avatar"), fields=fields)

    # ------------------------------------------------------------------
    # Console event handlers
    # ------------------------------------------------------------------
    def on_server_up(self, m, line):
        now = time.time()
        # Sessions still open here were cut off by a crash; their end
        # time is unknown, so they don't count toward time on server.
        self.db.close_all_sessions(count_time=False)
        self._pending.clear()
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
        self._drain()
        self.db.close_all_sessions(count_time=True)
        if self.discord is None:
            return
        self.discord.embed(COLOURS["RED"],
                           title=f"{self.server_name} has gone **OFFLINE**")

    # ------------------------------------------------------------------
    # Connection event handlers
    # ------------------------------------------------------------------
    def on_login_queue(self, m, line):
        steamid = m.groupdict().get("steamid", "")
        if steamid:
            self._pending[steamid] = time.time()

    def on_join(self, m, line):
        d        = m.groupdict()
        steamid  = d.get("steamid", "")
        username = d.get("username", "")
        if not steamid or not username:
            return
        self._pending.pop(steamid, None)

        self._drain()
        snap = self.db.open_session(steamid, username)
        sp   = self._profile(steamid)
        if self.discord is None:
            return

        persona = self._persona(steamid, sp, username)
        profile = sp.get("profile_url") or f"https://steamcommunity.com/profiles/{steamid}"

        fields = [{"name": "Kills:", "value": f"{snap['persona_kills']:,}", "inline": False}]
        if sp.get("pz_hours") is not None:
            fields.append({"name": "Hours on Record:",
                           "value": f"{sp['pz_hours']:,.0f}", "inline": False})
        fields.append({"name": "​",
                       "value": f"Logging in as **{_md(username)}**", "inline": False})
        fields.append({"name": "Kills:",
                       "value": f"{snap['account_kills']:,}", "inline": True})
        fields.append({"name": "Kills this run so far:",
                       "value": f"{snap['run_kills']:,}", "inline": True})

        recent = sp.get("recent_games") or []
        if recent:
            fields.append({"name": f"{persona} has also played:",
                           "value": "​", "inline": False})
            for g in recent[:2]:
                val = f"{_fmt_hours(g.get('total_hours', 0))} hrs on record"
                if g.get("last_played"):
                    val += "\nLast played: " + datetime.fromtimestamp(
                        g["last_played"]).strftime("%d %b %Y")
                fields.append({"name": g.get("name") or "?",
                               "value": val, "inline": True})

        self.discord.embed(
            COLOURS["DARKVIOLET"],
            title="New connection:",
            description=f"Steam Profile: [{_md(persona)}]({profile})",
            thumbnail=sp.get("avatar"),
            fields=fields,
        )

    def on_disconnect(self, m, line):
        d       = m.groupdict()
        steamid = d.get("steamid", "")
        if not steamid:
            return
        was_pending = bool(self._pending.pop(steamid, None))

        self._drain()
        res = self.db.close_session(steamid)
        if res is None:
            # Left from the login queue, or joined before the panel started.
            if was_pending or self.discord is None:
                return
            sp = self._profile(steamid)
            persona = self._persona(steamid, sp, d.get("username") or steamid)
            self.discord.embed(COLOURS["RED"],
                               title=f"{persona} has disconnected",
                               thumbnail=sp.get("avatar"))
            return
        if self.discord is None:
            return

        sp      = self._profile(steamid)
        persona = self._persona(steamid, sp, res["username"])
        total   = res["total_seconds"]
        lines   = [f"{_md(persona)} was online for {human_duration(res['duration'])}",
                   "Total time on server:",
                   fmt_total_time(total)]
        if total >= 86400:
            lines.append(f"({int(total // 3600):,} hours)")
        fields = [{"name": "Kills this session:",
                   "value": f"{res['session_kills']:,}", "inline": False}]

        if res["rage_quit"]:
            msg = random.choice(RAGE_MESSAGES).format(name=_md(persona))
            self.discord.embed(COLOURS["RED"], title=f"{persona} Rage-quit",
                               description="\n".join([msg, ""] + lines),
                               thumbnail=sp.get("avatar"), fields=fields)
        else:
            self.discord.embed(COLOURS["RED"],
                               title=f"{persona} has disconnected",
                               description="\n".join(lines),
                               thumbnail=sp.get("avatar"), fields=fields)

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

    kills_db      = ""
    event_log     = ""
    poll_interval = 2.0
    for section in ("player_events", "dizcord"):
        if cfg.has_section(section):
            kills_db  = kills_db  or cfg.get(section, "kills_db",  fallback="").strip()
            event_log = event_log or cfg.get(section, "event_log", fallback="").strip()
            try:
                poll_interval = float(cfg.get(section, "poll_interval", fallback="2").strip() or "2")
            except ValueError:
                pass

    if not kills_db:
        env_dir  = os.environ.get("PZPANEL_DATA_DIR", "").strip()
        data_dir = Path(env_dir) if env_dir else pc.get_default_data_dir()
        kills_db = str(data_dir / "pzp_kills.sqlite")
    if not event_log:
        # The mod writes to <Zomboid>/Lua/, next to server-console.txt.
        event_log = str(Path(log_path).parent / "Lua" / "pzp_events.log")

    db = pdb.PlayerDB(kills_db)

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

    watcher = PlayerEventWatcher(
        log_path=log_path,
        discord_url=webhook_url,
        steam=steam,
        db=db,
        poll_interval=poll_interval,
        server_name=server_name,
    )
    watcher._config_path = config_path
    watcher.tracker = kt.KillTracker(
        db, event_log,
        on_death=watcher.on_mod_death,
        on_milestone=watcher.on_milestone,
        poll_interval=poll_interval,
    )
    log.info("Kill tracking: event log %s, database %s", event_log, kills_db)
    return watcher
