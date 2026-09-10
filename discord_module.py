"""
Player-event tracking for PZP -- watches the PZ server's connection/
user logs (join/leave/death/server up-down) and the PZP kill-count mod's
output file, and surfaces both as: (1) live state main.py's /killboard
page reads directly, and (2) optional Discord embeds.

This started as diZcord 2.2.0 (github.com/Blyzz616/diZcord-b42), ported
into PZP as an optional module. The Discord/Steam/Tail/State classes and
DEFAULT_PATTERNS below are kept byte-for-byte identical to diZcord
2.2.0's logic where noted -- they're VERIFIED against real B42.20 logs
and shouldn't be touched casually.

As of this revision, tracking (sessions, kills, deaths) runs
unconditionally -- it no longer requires a Discord webhook to be
configured, since the killboard needs this data with or without
Discord. Discord posting is now a separate, optional consumer: each
on_* handler always updates self.state, and only calls self.discord.*
if a webhook was actually configured (self.discord is None otherwise).
This is the one real architectural change from the initial merge --
prior to this, the whole Watcher (and therefore all tracking) simply
didn't run at all without a configured webhook.

v3.0.0: added SQLite-backed persistent player tracking via player_db.py.
v4.0.0: cross-platform -- CONFIG_PATH and state/DB paths now OS-aware
         via platform_compat.py. No Linux-specific paths hardcoded.

IMPORTANT -- parser status (carried over from diZcord.py):
    Every pattern below except "denied" is VERIFIED against real B42.20
    logs. "denied" still carries B41-era wording and is UNVERIFIED-B42.
    Patterns can be overridden via an optional [patterns] section in
    pzpanel.ini -- no code change needed.
"""

__version__ = "4.1.0"

import configparser
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import player_db as pdb
import platform_compat as pc

log = logging.getLogger("pzpanel.discord_module")

# --------------------------------------------------------------------------
# Discord embed colours (decimal), ported from diZcord's colours.dec
# --------------------------------------------------------------------------
COLOURS = {
    "RED": 16711680,
    "ORANGE": 16753920,
    "LIME": 65280,
    "PURPLE": 8388736,
    "DARKVIOLET": 9699539,
    "DISCORDBLUE": 45015,
    "LAVENDER": 15132410,
    "CHARTREUSE": 8388352,
}

# --------------------------------------------------------------------------
# Default log patterns -- verified against real 42.20 logs (see diZcord
# 2.2.0's CHANGELOG for verification history). Override any of these in
# pzpanel.ini's [patterns] section.
# --------------------------------------------------------------------------
DEFAULT_PATTERNS = {
    "connecting": r"message=\"client-connect\".*?steam-id=\"(?P<steamid>\d+)\".*?username=\"(?P<username>[^\"]*)\"",
    "join": r"event=\"fully-connected\".*?ip=\"(?P<ip>[^\"]*)\".*?steam-id=\"(?P<steamid>\d+)\".*?username=\"(?P<username>[^\"]*)\"",
    "disconnect": r"event=\"disconnect\".*?ip=\"(?P<ip>[^\"]*)\".*?steam-id=\"(?P<steamid>\d+)\".*?username=\"(?P<username>[^\"]*)\"",
    "death": r"user (?P<name>.+?) died at \((?P<x>\d+),(?P<y>\d+),(?P<z>\d+)\)",
    "server_up": r"SERVER STARTED",
    "server_down": r"Server exited",
    "denied": r"Client sent invalid server password",
}

DEATH_MESSAGES = [
    "**{name}** just died.",
    "**{name}** has now made their contribution to the horde.",
    "**{name}** swapped sides.",
    "**{name}** has now completed their playthrough.",
    "**{name}** used the wrong hole.",
    "**{name}** kicked the bucket.",
    "**{name}** decided to try something else (it did not work).",
    "**{name}** forgot to pay their tribute to the R-N-Geezus.",
    "**{name}** bought the farm.",
    "**{name}** is still walking... breathing... not so much.",
    "**{name}**'s survival story just hit a dead end.",
    "**{name}**'s journey through the apocalypse has come to an abrupt halt.",
    "RIP **{name}** — may your next respawn be more successful.",
    "The zombies threw a party, and **{name}** was the main course.",
    "**{name}** was measured. **{name}** was weighed. **{name}** was found wanting.",
    "Looks like **{name}** just rolled a nat **1**.",
    "Rest in pieces, **{name}**.",
]

RAGE_MESSAGES = [
    "Looks like **{name}**'s exit was more dramatic than their survival skills.",
    "Quitting is easy, surviving is hard. **{name}**, the zombies miss you.",
    "**{name}** decided to take a break from survival.",
    "Rage-quitting won't make the zombies go away, **{name}**. Come back and show them who's boss!",
    "Surviving the apocalypse takes grit, **{name}**. Quitting only delays the inevitable. Ready for redemption?",
    "Even the best stumble. **{name}**, the server needs your resilience. Rise from the ashes!",
    "Zombies: 1, **{name}**: 0. Are you going to let them have the last laugh?",
    "Nobody said surviving the apocalypse was easy. **{name}**, dust off those setbacks and rejoin the fight!",
    "Rage-quitting won't erase the past, **{name}**. Redemption is just a login away.",
    "The zombies might have won this round, but **{name}** isn't out for the count.",
    "Apocalypse got you down, **{name}**? Rise from the ashes and show the zombies what you're made of!",
    "Survival isn't for the faint-hearted. **{name}**, the server misses your resilience.",
    "Shame! :bell: Shame! :bell: Shame! :bell:",
]

RESPAWN_MESSAGES = [
    "Well, well, well, if it isn't **{name}**. _Back_ from the dead.",
    "Player **{name}** has rejoined the fight.",
    "**{name}** decided to play for _Team Living_ once more.",
    "If life knocks **{name}** down, they just get right back up.",
    "There's no keeping **{name}** down for long.",
    "When life hands **{name}** lemons, they do tequila shots.",
    "**{name}** returns like a phoenix from the ashes of the apocalypse.",
    "Undead beware, **{name}** is on a respawn rampage!",
    "Zombies, meet your worst nightmare: **{name}**, resurrected and ready for more.",
    "Death is just a pit-stop for **{name}** on the road of survival.",
    "Did someone say zombie buffet? **{name}** is back for seconds.",
    "New day, new character, same old **{name}** kicking zombie ass.",
    "They tried to bury **{name}**. Little did they know, it's just a respawn point.",
    "Back in the land of the living: **{name}**, the unstoppable survivor.",
]


def last_played_str(epoch):
    if not epoch:
        return None
    d = date.fromtimestamp(epoch)
    today = date.today()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return d.strftime("%d %b %Y")


def human_duration(secs):
    secs = max(0, int(secs))
    parts = []
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            parts.append(f"{secs // size}{unit}")
            secs %= size
    parts.append(f"{secs}s")
    return " ".join(parts)


def _systemd_active_since(unit):
    """Linux only -- returns unix epoch or None."""
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", unit],
            capture_output=True, text=True, timeout=5)
        value = result.stdout.strip()
    except Exception as err:
        log.warning("Could not query systemd for %s start time: %s", unit, err)
        return None
    if not value or value == "n/a":
        return None
    naive = value.rsplit(" ", 1)[0] if " " in value else value
    try:
        dt = datetime.strptime(naive, "%a %Y-%m-%d %H:%M:%S")
        return time.mktime(dt.timetuple())
    except ValueError:
        return None


class Discord:
    def __init__(self, webhook_url, dry_run=False):
        self.url = webhook_url
        self.dry_run = dry_run

    def embed(self, colour, title=None, description=None, thumbnail=None, fields=None):
        e = {"color": colour}
        if title:
            e["title"] = title
        if description:
            e["description"] = description
        if thumbnail:
            e["thumbnail"] = {"url": thumbnail}
        if fields:
            e["fields"] = fields
        return self._post({"embeds": [e]})

    def content(self, text):
        return self._post({"content": text})

    def raw(self, payload):
        return self._post(payload)

    def _post(self, payload, attempt=0):
        if self.dry_run:
            log.info("[dry-run] would post: %s", json.dumps(payload))
            return True
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": f"pzpanel-discord/{__version__}"})
        try:
            urllib.request.urlopen(req, timeout=15)
            return True
        except urllib.error.HTTPError as err:
            if err.code == 429 and attempt < 3:
                try:
                    wait = float(json.loads(err.read()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                log.warning("Discord rate limit, retrying in %.1fs", wait)
                time.sleep(wait + 0.2)
                return self._post(payload, attempt + 1)
            elif err.code >= 500 and attempt < 3:
                time.sleep(2 * (attempt + 1))
                return self._post(payload, attempt + 1)
            else:
                try:
                    detail = err.read().decode("utf-8", errors="replace")
                except Exception:
                    detail = "(could not read response body)"
                log.error("Discord webhook failed (%s): %s -- %s", err.code, err.reason, detail)
                return False
        except Exception as err:
            log.error("Discord webhook error: %s", err)
            return False


class Steam:
    PZ_APPID = 108600
    API = "https://api.steampowered.com"

    def __init__(self, cfg, state):
        s = cfg.get("steam", {})
        self.enabled = s.get("enabled", "true").strip().lower() != "false"
        self.api_key = s.get("api_key", "").strip()
        self.cache_secs = float(s.get("cache_hours", "24") or 24) * 3600
        self.state = state

    def profile(self, steamid):
        if not self.enabled or not steamid:
            return {}
        cache = self.state.data.setdefault("steam_cache", {})
        hit = cache.get(steamid)
        have_key = bool(self.api_key)
        if hit and time.time() - hit.get("at", 0) < self.cache_secs:
            if not (have_key and not hit.get("via_api")):
                return hit
            log.info("Steam profile for %s: cached entry predates api_key, re-fetching", steamid)
        info = {"at": time.time(), "via_api": have_key}
        try:
            if have_key:
                self._fill_from_api(steamid, info)
            else:
                self._fill_from_xml(steamid, info)
        except Exception as err:
            log.warning("Steam lookup failed for %s: %s", steamid, err)
            if hit:
                return hit
        cache[steamid] = info
        self.state.save()
        return info

    def _fetch(self, url):
        req = urllib.request.Request(
            url, headers={"User-Agent": f"pzpanel-discord/{__version__}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()

    def _fill_from_api(self, steamid, info):
        q = urllib.parse.urlencode({"key": self.api_key, "steamids": steamid})
        data = json.loads(self._fetch(f"{self.API}/ISteamUser/GetPlayerSummaries/v2/?{q}"))
        players = data.get("response", {}).get("players", [])
        if players:
            info["persona"] = players[0].get("personaname")
            info["avatar"] = players[0].get("avatarfull")
        q = urllib.parse.urlencode({
            "key": self.api_key, "steamid": steamid,
            "include_appinfo": 1, "include_played_free_games": 1})
        data = json.loads(self._fetch(f"{self.API}/IPlayerService/GetOwnedGames/v1/?{q}"))
        others = []
        for g in data.get("response", {}).get("games", []):
            if g.get("appid") == self.PZ_APPID:
                info["pz_hours"] = g.get("playtime_forever", 0) // 60
            elif g.get("playtime_forever", 0) >= 60:
                others.append(g)
        others.sort(key=lambda g: g.get("rtime_last_played", 0), reverse=True)
        info["games"] = [{"name": g.get("name", "?"),
                          "hours": g.get("playtime_forever", 0) // 60,
                          "last": g.get("rtime_last_played")}
                         for g in others[:2]]

    def _fill_from_xml(self, steamid, info):
        xml = self._fetch(
            f"https://steamcommunity.com/profiles/{steamid}?xml=1"
        ).decode("utf-8", "replace")
        m = re.search(r"<steamID><!\[CDATA\[(.*?)\]\]></steamID>", xml, re.S)
        if m:
            info["persona"] = m.group(1)
        m = re.search(r"<avatarFull><!\[CDATA\[(.*?)\]\]></avatarFull>", xml, re.S)
        if m:
            info["avatar"] = m.group(1)


class Tail:
    def __init__(self, path_fn, from_start=False):
        self.path_fn = path_fn
        self.fh = None
        self.path = None
        self.sig = None
        self.from_start = from_start
        self.buf = ""

    def _signature(self, path):
        return pc.file_identity(path)

    def _open(self, path, seek_end):
        try:
            self.fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            self.fh = None
            return
        self.path = path
        self.sig = self._signature(path)
        self.buf = ""
        if seek_end:
            self.fh.seek(0, 2)

    def poll(self):
        target = self.path_fn()
        if target is None:
            return []
        target = Path(target)

        if self.fh is None:
            self._open(target, seek_end=not self.from_start)
            if self.fh is None:
                return []
        elif target != self.path or self._signature(target) != self.sig:
            self.fh.close()
            self._open(target, seek_end=False)
            if self.fh is None:
                return []
        else:
            try:
                if target.stat().st_size < self.fh.tell():
                    self.fh.seek(0)
            except OSError:
                pass

        chunk = self.fh.read()
        if not chunk:
            return []
        self.buf += chunk
        lines = self.buf.split("\n")
        self.buf = lines.pop()
        return [ln.rstrip("\r") for ln in lines if ln.strip()]

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


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
        result = {}
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or "|" not in line:
                continue
            username, _, kills_str = line.rpartition("|")
            try:
                result[username] = int(kills_str)
            except ValueError:
                log.warning("Unparseable line in kills file, skipping: %r", raw_line)
        return result


class State:
    def __init__(self, path):
        self.path = path
        self.data = {
            "server_up_since": None,
            "watcher_started": None,
            "sessions": {},
            "pending": {},
            "totals": {},
            "players": {},
            "logins": {},
            "dead": {},
            "kills_current_run": {},
            "kills_lifetime": {},
            "kills_last_updated": None,
            "session_kills": {},
            "kills_accounted_mtime": {},
            "session_kills_start": {},
        }
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as err:
                log.warning("Could not read player-event state file (%s), starting fresh", err)

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as err:
            log.error("Could not save player-event state: %s", err)


class Watcher:
    def __init__(self, cfg, discord, state, db=None):
        self.cfg = cfg
        self.discord = discord
        self.state = state
        self.db = db
        self.patterns = {name: re.compile(rx) for name, rx in cfg["patterns"].items()}
        self.steam = Steam(cfg, state)
        self.server_name = cfg["server"]["name"]
        self.unit = cfg["server"].get("unit", "pzserver")
        self.respawn_window = int(cfg["dizcord"].get("respawn_window", "600"))
        self._stop_event = threading.Event()

        zdir = Path(cfg["server"]["zomboid_dir"]).expanduser()
        console = cfg["server"].get("console_log") or str(zdir / "server-console.txt")
        self.console_tail = Tail(lambda: Path(console))
        logs_dir = Path(cfg["server"].get("logs_dir") or (zdir / "Logs"))
        self.user_tail = Tail(lambda: self._latest_log(logs_dir, "*_user.txt"))
        self.conn_tail = Tail(lambda: self._latest_log(logs_dir, "*_connections.txt"))

        kills_path = cfg["dizcord"].get("kills_file", "").strip()
        self.kills_file = KillsFile(Path(kills_path).expanduser()) if kills_path else None

    @staticmethod
    def _latest_log(logs_dir, pattern):
        try:
            candidates = sorted(logs_dir.glob(pattern),
                                key=lambda p: p.stat().st_mtime)
            return candidates[-1] if candidates else None
        except OSError:
            return None

    def _refresh_kills(self, force=False):
        if self.kills_file is None:
            return
        updated = self.kills_file.poll(force=force)
        if updated is None:
            return
        st = self.state.data
        st.setdefault("kills_current_run", {}).update(updated)
        st["kills_last_updated"] = time.time()
        self.state.save()

    def _kills_field(self, username):
        kills = self.state.data.get("kills_current_run", {}).get(username)
        if kills is None:
            return None
        return {"name": "Kills this run", "value": str(kills), "inline": True}

    def _roll_kills(self, name, steamid, is_death=False):
        st = self.state.data
        raw = st.get("kills_current_run", {}).pop(name, None)
        if raw is None:
            return None
        mtime = self.kills_file._mtime if self.kills_file else None
        accounted = st.setdefault("kills_accounted_mtime", {})
        if mtime is not None and accounted.get(name) == mtime:
            return raw
        start_snaps = st.setdefault("session_kills_start", {})
        start_snap = start_snaps.get(steamid, 0) if steamid else 0
        delta = max(0, raw - start_snap)
        lifetime = st.setdefault("kills_lifetime", {})
        lifetime[name] = lifetime.get(name, 0) + delta
        if steamid:
            sess = st.setdefault("session_kills", {})
            sess[steamid] = sess.get(steamid, 0) + delta
            if is_death:
                start_snaps[steamid] = 0
            else:
                start_snaps.pop(steamid, None)
        if mtime is not None:
            accounted[name] = mtime
        return raw

    def handle_line(self, line):
        for name, rx in self.patterns.items():
            m = rx.search(line)
            if m:
                getattr(self, f"on_{name}", self.on_unknown)(m, line)

    def on_unknown(self, m, line):
        pass

    def on_server_up(self, m, line):
        now = time.time()
        self.state.data["server_up_since"] = now
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
        """Reconstruct a minimal configparser for platform_compat calls."""
        import configparser as _cp
        cfg = _cp.ConfigParser()
        cfg.read_dict({"server": {
            "unit": self.unit,
            "name": self.server_name,
        }})
        return cfg

    def on_server_down(self, m, line):
        st = self.state.data
        up_since = st.get("server_up_since")
        desc = (f"The server was up for {human_duration(time.time() - up_since)}"
                if up_since else "Server shutting down.")
        st["server_up_since"] = None
        self.state.save()
        if self.discord is not None:
            self.discord.embed(COLOURS["ORANGE"],
                               title=f"{self.server_name} is going **DOWN**",
                               description=desc)

        now = time.time()
        db_players = []
        for steamid in list(st["sessions"].keys()):
            name = st["players"].get(steamid, {}).get("login", steamid)
            session = now - st["sessions"].pop(steamid)
            st["totals"][steamid] = st["totals"].get(steamid, 0) + session
            st["dead"].pop(steamid, None)
            total = st["totals"][steamid]
            current_run_kills = self._roll_kills(name, steamid)
            session_kills_total = st.get("session_kills", {}).pop(steamid, None)
            lifetime_kills_total = st.get("kills_lifetime", {}).get(name)
            db_players.append((steamid, name, current_run_kills if current_run_kills is not None else 0))
            if self.discord is None:
                continue
            avatar = st.get("steam_cache", {}).get(steamid, {}).get("avatar")
            lines = [f"{name} was online for {human_duration(session)}",
                     f"Total time on server:\n{human_duration(total)}",
                     "Reason: server stopped"]
            if total >= 3600:
                lines.append(f"({int(total // 3600)} Hours)")
            fields = []
            if current_run_kills is not None:
                fields.append({"name": "Kills this run", "value": str(current_run_kills), "inline": True})
            if session_kills_total is not None:
                fields.append({"name": "Session kills", "value": str(session_kills_total), "inline": True})
            if lifetime_kills_total is not None:
                fields.append({"name": "Lifetime kills", "value": str(lifetime_kills_total), "inline": True})
            self.discord.embed(COLOURS["RED"], title=f"{name} has disconnected",
                               description="\n".join(lines), thumbnail=avatar,
                               fields=fields or None)

        if self.db is not None and db_players:
            self.db.on_server_down(db_players)
        self.state.save()

    def on_connecting(self, m, line):
        d = m.groupdict()
        steamid = d.get("steamid", "")
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
            )

        if self.discord is None:
            return

        profile = f"https://steamcommunity.com/profiles/{steamid}"
        fields = []
        account_kills = None
        char_kills = None
        if self.db is not None:
            try:
                board = self.db.get_killboard()
                for acc in board:
                    if acc["steamid"] == steamid:
                        account_kills = acc["account_kills"]
                        for c in acc["characters"]:
                            if c["username"] == username:
                                char_kills = c["char_kills"]
                        break
            except Exception:
                log.warning("on_join: failed to read db for kill totals", exc_info=True)

        if account_kills is not None:
            fields.append({"name": "Kills:", "value": str(account_kills), "inline": False})
        if sp.get("pz_hours") is not None:
            fields.append({"name": "Hours on Record:",
                           "value": f"{sp['pz_hours']:,}", "inline": False})
        login_line = f"Logging in as **{username}**"
        if char_kills is not None:
            fields.append({"name": "\u200b", "value": login_line, "inline": False})
            fields.append({"name": "Kills:", "value": str(char_kills), "inline": True})
            run_kills_so_far = st.get("kills_current_run", {}).get(username, 0)
            fields.append({"name": "Kills this run so far:",
                           "value": str(run_kills_so_far), "inline": True})
        else:
            fields.append({"name": "\u200b", "value": login_line, "inline": False})
        if sp.get("games"):
            fields.append({"name": f"{persona} has also played:",
                           "value": "\u200b", "inline": False})
            for g in sp["games"]:
                value = f"{g['hours']:,} hrs on record"
                last = last_played_str(g.get("last"))
                if last:
                    value += f"\nLast played: {last}"
                fields.append({"name": g["name"], "value": value, "inline": True})

        self.discord.embed(
            COLOURS["PURPLE"], title="New connection:",
            description=f"Steam Profile: [{persona}]({profile})",
            thumbnail=avatar, fields=fields or None)

    def on_disconnect(self, m, line):
        d = m.groupdict()
        steamid = d.get("steamid", "")
        st = self.state.data
        was_pending = st.setdefault("pending", {}).pop(steamid, None) is not None
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
        current_run_kills = self._roll_kills(name, steamid)
        session_kills_total = st.get("session_kills", {}).pop(steamid, None)
        lifetime_kills_total = st.get("kills_lifetime", {}).get(name)
        self.state.save()

        if self.db is not None:
            self.db.on_disconnect(
                steamid=steamid, username=name,
                kills_snapshot=current_run_kills if current_run_kills is not None else 0,
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
        if current_run_kills is not None:
            fields.append({"name": "Kills this run", "value": str(current_run_kills), "inline": True})
        if session_kills_total is not None:
            fields.append({"name": "Session kills", "value": str(session_kills_total), "inline": True})
        if lifetime_kills_total is not None:
            fields.append({"name": "Lifetime kills", "value": str(lifetime_kills_total), "inline": True})

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
        kills_this_run = self._roll_kills(name, steamid, is_death=True)

        if self.db is not None and steamid:
            self.db.on_death(
                steamid=steamid, username=name,
                kills_snapshot=kills_this_run if kills_this_run is not None else 0,
            )

        if steamid:
            st["dead"][steamid] = time.time()
        self.state.save()

        if self.discord is None:
            return
        fields = []
        if kills_this_run is not None:
            fields.append({"name": "Kills this run", "value": str(kills_this_run), "inline": True})
            fields.append({"name": "Total kills on server",
                           "value": str(st["kills_lifetime"].get(name, 0)), "inline": True})
        self.discord.embed(COLOURS["RED"],
                           description=random.choice(DEATH_MESSAGES).format(name=name),
                           fields=fields or None)

    def on_denied(self, m, line):
        if self.discord is not None:
            self.discord.embed(COLOURS["RED"],
                               title="Access denied — check your credentials.")

    def stop(self):
        self._stop_event.set()

    def run(self):
        self.state.data["watcher_started"] = time.time()
        self.state.save()
        poll = float(self.cfg["dizcord"].get("poll_interval", "0.5"))
        log.info("Player-event watcher watching %s", self.server_name)

        while not self._stop_event.is_set():
            for tail in (self.console_tail, self.user_tail, self.conn_tail):
                for line in tail.poll():
                    self.handle_line(line)
            self._refresh_kills(force=False)
            self._stop_event.wait(poll)

        self.console_tail.close()
        self.user_tail.close()
        self.conn_tail.close()
        if self.db is not None:
            self.db.close()
        log.info("Player-event watcher stopped.")


def build_watcher(config_path=None):
    """
    Reads the shared pzpanel.ini and returns a ready-to-run Watcher, or
    None only if the config file itself can't be read at all.
    """
    if config_path is None:
        config_path = os.environ.get("PZPANEL_CONFIG") or str(pc.get_default_config_path())

    ini = configparser.ConfigParser()
    if not ini.read(config_path, encoding="utf-8"):
        log.warning("Config not found at %s, player-event tracking disabled", config_path)
        return None

    webhook_url = ini.get("discord", "webhook_url", fallback="").strip()
    if not webhook_url or webhook_url == "CHANGEME":
        log.info("No [discord] webhook_url configured -- tracking runs, Discord posting disabled")
        webhook_url = ""

    server_name = ini.get("server", "name", fallback="Realm")
    server_unit = ini.get("server", "unit", fallback="pzserver")
    console_log = ini.get("paths", "console_log", fallback="").strip()
    if not console_log:
        console_log = str(pc.get_default_data_dir() / "server-console.txt")
    zomboid_dir = Path(console_log).expanduser().parent
    logs_dir = ini.get("player_events", "logs_dir", fallback=str(zomboid_dir / "Logs")) \
        if ini.has_section("player_events") else str(zomboid_dir / "Logs")

    patterns = dict(DEFAULT_PATTERNS)
    if ini.has_section("patterns"):
        patterns.update(dict(ini["patterns"]))

    pe = dict(ini["player_events"]) if ini.has_section("player_events") else {}

    cfg = {
        "server": {
            "name": server_name,
            "unit": server_unit,
            "zomboid_dir": str(zomboid_dir),
            "console_log": console_log,
            "logs_dir": logs_dir,
        },
        "dizcord": {
            "poll_interval": pe.get("poll_interval", "0.5"),
            "respawn_window": pe.get("respawn_window", "600"),
            "kills_file": pe.get("kills_file", ""),
        },
        "steam": dict(ini["steam"]) if ini.has_section("steam") else {},
        "patterns": patterns,
    }

    data_dir = pc.get_default_data_dir()
    state_path = Path(pe.get("state_file", "")).expanduser() if pe.get("state_file") \
        else data_dir / "player_events_state.json"
    db_path = Path(pe.get("player_db", "")).expanduser() if pe.get("player_db") \
        else data_dir / "player_db.sqlite"

    discord = Discord(webhook_url) if webhook_url else None
    state = State(state_path)
    try:
        player_database = pdb.PlayerDB(db_path)
    except Exception:
        log.exception("Failed to open PlayerDB at %s, continuing without it", db_path)
        player_database = None
    return Watcher(cfg, discord, state, db=player_database)
