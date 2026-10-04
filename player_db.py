"""
player_db.py -- SQLite store for PZP kill tracking.

Model (one Zomboid server per host):
  personas    one per Steam ID. Cached Steam name/avatar, real-time spent
              on the server, highest lifetime-kill milestone announced.
  accounts    one per server account name (unique on a PZ server). steamid
              is the persona that last logged into it.
  characters  one per in-game character (a life). An account has at most
              one alive character; a death closes it and the next report
              from that account starts a new one.
  sessions    open log-ons only, keyed by steamid (a persona can only be
              on one account at a time). Deleted at disconnect.
  meta        key/value (event-log read position).

Kill totals are sums over characters:
  account kills = SUM(characters.kills) for that account
  persona kills = SUM over every account linked to that steamid

characters.pre_kills is the count already on a character when tracking
first saw it (a character older than this database). Session kills only
count kills above it, so the first report after an install doesn't show
up as one huge session.

Thread safety: a single lock guards every method; the connection is
shared across threads.
"""

__version__ = "5.0.0"

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("pzpanel.player_db")

# A report whose in-game hours are this far below the alive character's
# means a new character (the death report was lost).
_NEW_CHARACTER_HOURS_DROP = 1.0
# A character first seen with fewer in-game hours than this is treated
# as brand new (all its kills were made while tracked).
_FRESH_CHARACTER_HOURS = 1.0
# A death report arriving this soon after the account's last death, with
# no alive character, is a duplicate.
_DUPLICATE_DEATH_SECS = 120

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS personas (
    steamid            TEXT PRIMARY KEY,
    name               TEXT,
    avatar_url         TEXT,
    profile_url        TEXT,
    pz_hours           REAL,
    total_seconds      REAL NOT NULL DEFAULT 0,
    lifetime_milestone INTEGER NOT NULL DEFAULT 0,
    last_seen          REAL
);

CREATE TABLE IF NOT EXISTS accounts (
    username   TEXT PRIMARY KEY,
    steamid    TEXT,
    first_seen REAL NOT NULL,
    last_seen  REAL
);

CREATE TABLE IF NOT EXISTS characters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL REFERENCES accounts(username),
    name           TEXT,
    kills          INTEGER NOT NULL DEFAULT 0,
    pre_kills      INTEGER NOT NULL DEFAULT 0,
    hours          REAL NOT NULL DEFAULT 0,
    alive          INTEGER NOT NULL DEFAULT 1,
    death_reported INTEGER NOT NULL DEFAULT 0,
    life_milestone INTEGER NOT NULL DEFAULT 0,
    first_seen     REAL NOT NULL,
    last_seen      REAL NOT NULL,
    died_at        REAL
);
CREATE INDEX IF NOT EXISTS characters_username ON characters(username);

CREATE TABLE IF NOT EXISTS sessions (
    steamid       TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    joined_at     REAL NOT NULL,
    start_char_id INTEGER,
    start_kills   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class PlayerDB:

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        log.info("PlayerDB opened at %s", self.path)

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers (caller holds the lock)
    # ------------------------------------------------------------------

    def _tx(self):
        return _Transaction(self._conn)

    def _ensure_account(self, username, now, steamid=None):
        self._conn.execute(
            "INSERT INTO accounts(username, steamid, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
            "steamid = COALESCE(excluded.steamid, steamid), last_seen = excluded.last_seen",
            (username, steamid, now, now))

    def _ensure_persona(self, steamid, now):
        self._conn.execute(
            "INSERT INTO personas(steamid, last_seen) VALUES (?, ?) "
            "ON CONFLICT(steamid) DO UPDATE SET last_seen = excluded.last_seen",
            (steamid, now))

    def _alive_character(self, username):
        return self._conn.execute(
            "SELECT * FROM characters WHERE username=? AND alive=1 "
            "ORDER BY id DESC LIMIT 1", (username,)).fetchone()

    def _latest_character(self, username):
        return self._conn.execute(
            "SELECT * FROM characters WHERE username=? ORDER BY id DESC LIMIT 1",
            (username,)).fetchone()

    def _steamid_for(self, username):
        row = self._conn.execute(
            "SELECT steamid FROM accounts WHERE username=?", (username,)).fetchone()
        return row["steamid"] if row else None

    def _account_kills(self, username):
        row = self._conn.execute(
            "SELECT COALESCE(SUM(kills), 0) AS k FROM characters WHERE username=?",
            (username,)).fetchone()
        return row["k"]

    def _persona_kills(self, steamid):
        row = self._conn.execute(
            "SELECT COALESCE(SUM(c.kills), 0) AS k FROM characters c "
            "JOIN accounts a ON a.username = c.username WHERE a.steamid=?",
            (steamid,)).fetchone()
        return row["k"]

    # ------------------------------------------------------------------
    # Mod reports
    # ------------------------------------------------------------------

    def apply_report(self, kind, username, kills, hours, name, now=None):
        """
        Apply one report from the mod. kind is "J" (snapshot), "U" (update)
        or "D" (death). Returns None for an ignored duplicate death, else:
          char_id, username, steamid, name, kills, hours, died,
          old_kills    -- the character's previous count, or None when the
                          character was first seen with history we don't have
          persona_kills -- persona total after this report (None if the
                          account isn't linked to a Steam ID yet)
        """
        now = now or time.time()
        name = (name or "").strip() or None
        with self._lock, self._tx():
            self._ensure_account(username, now)
            alive = self._alive_character(username)

            if alive is not None:
                hours_dropped = hours < alive["hours"] - _NEW_CHARACTER_HOURS_DROP
                renamed = name and alive["name"] and name != alive["name"]
                if hours_dropped or renamed:
                    # The previous character's death report never arrived.
                    log.info("New character on %s without a death report; "
                             "closing character %d", username, alive["id"])
                    self._conn.execute(
                        "UPDATE characters SET alive=0, died_at=? WHERE id=?",
                        (now, alive["id"]))
                    alive = None

            died = kind == "D"
            if alive is None:
                if died:
                    last = self._latest_character(username)
                    if (last is not None and not last["alive"] and last["died_at"]
                            and now - last["died_at"] < _DUPLICATE_DEATH_SECS):
                        return None
                fresh = hours < _FRESH_CHARACTER_HOURS
                pre_kills = 0 if fresh else kills
                cur = self._conn.execute(
                    "INSERT INTO characters(username, name, kills, pre_kills, hours, "
                    "alive, death_reported, first_seen, last_seen, died_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (username, name, kills, pre_kills, hours,
                     0 if died else 1, 1 if died else 0, now, now,
                     now if died else None))
                char_id = cur.lastrowid
                old_kills = 0 if fresh else None
            else:
                char_id = alive["id"]
                old_kills = alive["kills"]
                self._conn.execute(
                    "UPDATE characters SET kills=?, hours=?, name=COALESCE(?, name), "
                    "last_seen=?, alive=?, death_reported=?, died_at=? WHERE id=?",
                    (kills, hours, name, now,
                     0 if died else 1, 1 if died else 0,
                     now if died else None, char_id))
                name = name or alive["name"]

            steamid = self._steamid_for(username)
            return {
                "char_id": char_id,
                "username": username,
                "steamid": steamid,
                "name": name,
                "kills": kills,
                "hours": hours,
                "died": died,
                "old_kills": old_kills,
                "persona_kills": self._persona_kills(steamid) if steamid else None,
            }

    def claim_life_milestone(self, char_id, threshold):
        """Record a per-character milestone. True if it wasn't already recorded."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE characters SET life_milestone=? WHERE id=? AND life_milestone < ?",
                (threshold, char_id, threshold))
            return cur.rowcount == 1

    def claim_lifetime_milestone(self, steamid, threshold):
        """Record a persona lifetime milestone. True if it wasn't already recorded."""
        with self._lock:
            self._ensure_persona(steamid, time.time())
            cur = self._conn.execute(
                "UPDATE personas SET lifetime_milestone=? "
                "WHERE steamid=? AND lifetime_milestone < ?",
                (threshold, steamid, threshold))
            return cur.rowcount == 1

    # ------------------------------------------------------------------
    # Steam data
    # ------------------------------------------------------------------

    def update_persona(self, steamid, name=None, avatar_url=None,
                       profile_url=None, pz_hours=None):
        with self._lock:
            now = time.time()
            self._ensure_persona(steamid, now)
            self._conn.execute(
                "UPDATE personas SET name=COALESCE(?, name), "
                "avatar_url=COALESCE(?, avatar_url), profile_url=COALESCE(?, profile_url), "
                "pz_hours=COALESCE(?, pz_hours) WHERE steamid=?",
                (name, avatar_url, profile_url, pz_hours, steamid))

    def persona_name(self, steamid):
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM personas WHERE steamid=?", (steamid,)).fetchone()
            return row["name"] if row else None

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def open_session(self, steamid, username, now=None):
        """
        Start a session at join. Returns the join-embed numbers:
          persona_kills, account_kills, run_kills (alive character, else 0)
        """
        now = now or time.time()
        with self._lock, self._tx():
            self._ensure_persona(steamid, now)
            self._ensure_account(username, now, steamid=steamid)
            self._conn.execute(
                "UPDATE accounts SET steamid=? WHERE username=?", (steamid, username))
            alive = self._alive_character(username)
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions(steamid, username, joined_at, "
                "start_char_id, start_kills) VALUES (?, ?, ?, ?, ?)",
                (steamid, username, now,
                 alive["id"] if alive else None,
                 alive["kills"] if alive else 0))
            return {
                "persona_kills": self._persona_kills(steamid),
                "account_kills": self._account_kills(username),
                "run_kills": alive["kills"] if alive else 0,
            }

    def close_session(self, steamid, now=None):
        """
        End a session at disconnect. Returns None if no session was open, else:
          username, duration, total_seconds, session_kills,
          rage_quit -- a character died this session and no new one was made
        """
        now = now or time.time()
        with self._lock, self._tx():
            sess = self._conn.execute(
                "SELECT * FROM sessions WHERE steamid=?", (steamid,)).fetchone()
            if sess is None:
                return None
            username = sess["username"]
            joined_at = sess["joined_at"]

            session_kills = 0
            for c in self._conn.execute(
                    "SELECT * FROM characters WHERE username=? AND (id=? OR first_seen>=?)",
                    (username, sess["start_char_id"], joined_at)):
                if c["id"] == sess["start_char_id"]:
                    session_kills += max(0, c["kills"] - sess["start_kills"])
                else:
                    session_kills += max(0, c["kills"] - c["pre_kills"])

            alive = self._alive_character(username)
            died_this_session = self._conn.execute(
                "SELECT 1 FROM characters WHERE username=? AND alive=0 AND died_at>=?",
                (username, joined_at)).fetchone() is not None

            duration = max(0.0, now - joined_at)
            self._ensure_persona(steamid, now)
            self._conn.execute(
                "UPDATE personas SET total_seconds = total_seconds + ? WHERE steamid=?",
                (duration, steamid))
            total = self._conn.execute(
                "SELECT total_seconds FROM personas WHERE steamid=?",
                (steamid,)).fetchone()["total_seconds"]
            self._conn.execute("DELETE FROM sessions WHERE steamid=?", (steamid,))
            return {
                "username": username,
                "duration": duration,
                "total_seconds": total,
                "session_kills": session_kills,
                "rage_quit": alive is None and died_this_session,
            }

    def close_all_sessions(self, now=None, count_time=True):
        """
        Drop every open session (server stopped or crashed). With count_time
        the time up to now is added to each persona's total.
        """
        now = now or time.time()
        with self._lock, self._tx():
            if count_time:
                for s in self._conn.execute("SELECT * FROM sessions").fetchall():
                    self._ensure_persona(s["steamid"], now)
                    self._conn.execute(
                        "UPDATE personas SET total_seconds = total_seconds + ? "
                        "WHERE steamid=?", (max(0.0, now - s["joined_at"]), s["steamid"]))
            self._conn.execute("DELETE FROM sessions")

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def get_meta(self, key, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, None if value is None else str(value)))

    # ------------------------------------------------------------------
    # Killboard
    # ------------------------------------------------------------------

    def get_killboard(self):
        """
        Returns (players, characters):
          players    -- one dict per persona, most kills first:
                        steamid, name, avatar_url, profile_url, pz_hours,
                        kills, total_seconds, accounts (list of names)
          characters -- one dict per character, most kills first:
                        name, username, persona, kills, hours, alive, died_at
        """
        with self._lock:
            players = []
            for p in self._conn.execute("SELECT * FROM personas").fetchall():
                accounts = [r["username"] for r in self._conn.execute(
                    "SELECT username FROM accounts WHERE steamid=? ORDER BY username",
                    (p["steamid"],))]
                players.append({
                    "steamid": p["steamid"],
                    "name": p["name"] or p["steamid"],
                    "avatar_url": p["avatar_url"],
                    "profile_url": p["profile_url"],
                    "pz_hours": p["pz_hours"],
                    "kills": self._persona_kills(p["steamid"]),
                    "total_seconds": p["total_seconds"],
                    "accounts": accounts,
                })
            players.sort(key=lambda p: p["kills"], reverse=True)

            characters = [dict(r) for r in self._conn.execute(
                "SELECT c.name, c.username, c.kills, c.hours, c.alive, c.died_at, "
                "p.name AS persona FROM characters c "
                "LEFT JOIN accounts a ON a.username = c.username "
                "LEFT JOIN personas p ON p.steamid = a.steamid "
                "ORDER BY c.kills DESC, c.id DESC")]
            return players, characters


class _Transaction:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the block raises."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, *_):
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
