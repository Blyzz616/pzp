"""
player_db.py -- SQLite-backed persistent player tracking for PZP.

Hierarchy:
  steam_accounts  -- one row per Steam ID (immutable key)
      characters  -- one per in-game username under that Steam account
          runs    -- one per character life (reset on death)
            sessions -- one per log-on/log-off within a run

Kills aggregate upward: session -> run -> character -> account.
Stored as (kills_start, kills_end) per session -- the delta is the
session's contribution, which avoids the in-memory double-count bug
that _roll_kills() also guards against (two rollover events before the
mod file's next ~60s write would see the same raw snapshot twice).

Thread safety: a single threading.Lock guards all write paths. Reads
are lock-free; SQLite's own WAL mode handles concurrent readers.

DB lives at /opt/pzp/player_db.sqlite by default (same directory as
the rest of pzpanel's runtime state). Path is configurable via
[player_events] player_db in pzpanel.ini.
"""

__version__ = "4.0.1"

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("pzpanel.player_db")

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS steam_accounts (
    steamid     TEXT PRIMARY KEY,
    steam_name  TEXT,
    avatar_url  TEXT,
    pz_hours    INTEGER,
    last_seen   REAL
);

CREATE TABLE IF NOT EXISTS characters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    steamid     TEXT NOT NULL REFERENCES steam_accounts(steamid),
    username    TEXT NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL,
    UNIQUE(steamid, username)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id),
    started_at   REAL NOT NULL,
    ended_at     REAL,
    ended_by     TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    character_id INTEGER NOT NULL REFERENCES characters(id),
    started_at   REAL NOT NULL,
    ended_at     REAL,
    ended_by     TEXT,
    kills_start  INTEGER NOT NULL DEFAULT 0,
    kills_end    INTEGER
);
"""


class PlayerDB:
    """
    All public methods are thread-safe. Write paths acquire self._lock;
    reads run directly on the same connection (WAL mode allows concurrent
    readers even while a write is in progress).

    Caller is responsible for calling close() on shutdown (or using
    it as a context manager: `with PlayerDB(path) as db:`).
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False,
            isolation_level=None,   # autocommit; we manage transactions manually
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        log.info("PlayerDB opened at %s", self.path)

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_account(self, cur, steamid, steam_name=None,
                        avatar_url=None, pz_hours=None):
        """Insert or update a steam_accounts row. Only non-None fields
        are written -- avoids clobbering a previously-fetched steam_name
        with None just because this particular call didn't have one."""
        cur.execute("""
            INSERT INTO steam_accounts(steamid, steam_name, avatar_url, pz_hours, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(steamid) DO UPDATE SET
                steam_name = COALESCE(excluded.steam_name, steam_name),
                avatar_url = COALESCE(excluded.avatar_url, avatar_url),
                pz_hours   = COALESCE(excluded.pz_hours,   pz_hours),
                last_seen  = excluded.last_seen
        """, (steamid, steam_name, avatar_url, pz_hours, time.time()))

    def _get_or_create_character(self, cur, steamid, username):
        """Returns the character row id for (steamid, username), inserting
        a new character row if this is the first time we've seen this
        username under this Steam account. The same steamid with a
        different username = a genuinely new character with its own
        independent history."""
        row = cur.execute(
            "SELECT id FROM characters WHERE steamid=? AND username=?",
            (steamid, username),
        ).fetchone()
        if row:
            return row["id"]
        now = time.time()
        cur.execute(
            "INSERT INTO characters(steamid, username, first_seen, last_seen) VALUES (?,?,?,?)",
            (steamid, username, now, now),
        )
        return cur.lastrowid

    def _open_run(self, cur, character_id):
        """Opens a new run row for this character. Called on join when no
        open run exists for this character yet (first join ever, or
        after a death that closed the previous run)."""
        cur.execute(
            "INSERT INTO runs(character_id, started_at) VALUES (?, ?)",
            (character_id, time.time()),
        )
        return cur.lastrowid

    def _current_run(self, cur, character_id):
        """Returns the open (ended_at IS NULL) run row for this character,
        or None. A character may have at most one open run at a time."""
        return cur.execute(
            "SELECT * FROM runs WHERE character_id=? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (character_id,),
        ).fetchone()

    def _current_session(self, cur, character_id):
        """Returns the open (ended_at IS NULL) session for this character,
        or None."""
        return cur.execute(
            "SELECT * FROM sessions WHERE character_id=? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (character_id,),
        ).fetchone()

    # ------------------------------------------------------------------
    # Public write API -- called by discord_module.Watcher's on_* handlers
    # ------------------------------------------------------------------

    def on_join(self, steamid, username,
                steam_name=None, avatar_url=None,
                pz_hours=None, kills_snapshot=0):
        """
        Called when a player fully connects. Creates or updates the
        steam_accounts row, ensures a character row exists for this
        (steamid, username) pair, opens a run if none is open, and
        opens a new session within that run.

        kills_snapshot: the value read from the kills file at join time
        (i.e. kills already on record for this player from a previous
        session in the same run). Stored as kills_start on the new session
        row so we can compute the true per-session delta at close time
        rather than attributing the entire run total to this session.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._upsert_account(cur, steamid, steam_name, avatar_url, pz_hours)
                char_id = self._get_or_create_character(cur, steamid, username)
                cur.execute(
                    "UPDATE characters SET last_seen=? WHERE id=?",
                    (time.time(), char_id),
                )

                run = self._current_run(cur, char_id)
                if run is None:
                    run_id = self._open_run(cur, char_id)
                else:
                    run_id = run["id"]

                # Guard: close any stale open session (shouldn't happen
                # in normal flow, but a crash/restart could leave one).
                stale = self._current_session(cur, char_id)
                if stale is not None:
                    log.warning(
                        "on_join: closing stale open session %d for char %d",
                        stale["id"], char_id,
                    )
                    cur.execute(
                        "UPDATE sessions SET ended_at=?, ended_by='server_down' WHERE id=?",
                        (time.time(), stale["id"]),
                    )

                cur.execute(
                    "INSERT INTO sessions "
                    "(run_id, character_id, started_at, kills_start) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, char_id, time.time(), kills_snapshot),
                )
                self._conn.execute("COMMIT")
                log.debug("on_join: steamid=%s username=%s char_id=%d run_id=%d",
                          steamid, username, char_id, run_id)
            except Exception:
                self._conn.execute("ROLLBACK")
                log.exception("on_join failed for steamid=%s username=%s", steamid, username)

    def on_disconnect(self, steamid, username, kills_snapshot):
        """
        Closes the open session for this character. The run is left open
        (player may rejoin on the same character without dying).
        kills_snapshot: the mod-file value at disconnect time (= kills_end).
        """
        self._close_session(steamid, username, kills_snapshot, "disconnect")

    def on_death(self, steamid, username, kills_snapshot):
        """
        Closes the open session AND the open run for this character.
        kills_snapshot: the mod-file value at death time.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                char_row = cur.execute(
                    "SELECT id FROM characters WHERE steamid=? AND username=?",
                    (steamid, username),
                ).fetchone()
                if char_row is None:
                    log.warning("on_death: no character row for steamid=%s username=%s",
                                steamid, username)
                    self._conn.execute("ROLLBACK")
                    return
                char_id = char_row["id"]
                now = time.time()

                sess = self._current_session(cur, char_id)
                if sess:
                    cur.execute(
                        "UPDATE sessions SET ended_at=?, ended_by='death', kills_end=? "
                        "WHERE id=?",
                        (now, kills_snapshot, sess["id"]),
                    )

                run = self._current_run(cur, char_id)
                if run:
                    cur.execute(
                        "UPDATE runs SET ended_at=?, ended_by='death' WHERE id=?",
                        (now, run["id"]),
                    )

                self._conn.execute("COMMIT")
                log.debug("on_death: steamid=%s username=%s char_id=%d", steamid, username, char_id)
            except Exception:
                self._conn.execute("ROLLBACK")
                log.exception("on_death failed for steamid=%s username=%s", steamid, username)

    def on_server_down(self, open_players):
        """
        Mass-close all open sessions for every player still connected
        when the server goes down. Runs are left open -- server_down
        doesn't mean the character died; they'll resume in the same run
        on the next server start.
        open_players: list of (steamid, username, kills_snapshot) tuples.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                now = time.time()
                for steamid, username, kills_snapshot in open_players:
                    char_row = cur.execute(
                        "SELECT id FROM characters WHERE steamid=? AND username=?",
                        (steamid, username),
                    ).fetchone()
                    if char_row is None:
                        continue
                    char_id = char_row["id"]
                    sess = self._current_session(cur, char_id)
                    if sess:
                        cur.execute(
                            "UPDATE sessions SET ended_at=?, ended_by='server_down', "
                            "kills_end=? WHERE id=?",
                            (now, kills_snapshot, sess["id"]),
                        )
                self._conn.execute("COMMIT")
                log.debug("on_server_down: closed sessions for %d players", len(open_players))
            except Exception:
                self._conn.execute("ROLLBACK")
                log.exception("on_server_down failed")

    def update_steam_info(self, steamid, steam_name=None,
                          avatar_url=None, pz_hours=None):
        """Updates steam enrichment data for an account. Called after a
        successful Steam API/XML lookup so the killboard can display
        the Steam persona name without re-fetching on every page load."""
        with self._lock:
            cur = self._conn.cursor()
            self._upsert_account(cur, steamid, steam_name, avatar_url, pz_hours)

    def _close_session(self, steamid, username, kills_snapshot, reason):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                char_row = cur.execute(
                    "SELECT id FROM characters WHERE steamid=? AND username=?",
                    (steamid, username),
                ).fetchone()
                if char_row is None:
                    log.warning("_close_session: no char for steamid=%s username=%s",
                                steamid, username)
                    self._conn.execute("ROLLBACK")
                    return
                char_id = char_row["id"]
                sess = self._current_session(cur, char_id)
                if sess is None:
                    log.warning("_close_session: no open session for char_id=%d", char_id)
                    self._conn.execute("ROLLBACK")
                    return
                cur.execute(
                    "UPDATE sessions SET ended_at=?, ended_by=?, kills_end=? WHERE id=?",
                    (time.time(), reason, kills_snapshot, sess["id"]),
                )
                self._conn.execute("COMMIT")
                log.debug("_close_session: char_id=%d reason=%s kills_end=%d",
                          char_id, reason, kills_snapshot)
            except Exception:
                self._conn.execute("ROLLBACK")
                log.exception("_close_session failed for steamid=%s username=%s",
                              steamid, username)

    # ------------------------------------------------------------------
    # Public read API -- called by the /killboard page
    # ------------------------------------------------------------------

    def get_killboard(self):
        """
        Returns one dict per Steam account, sorted by account lifetime
        kills descending. Each dict has:
            steamid, steam_name, avatar_url, pz_hours,
            account_kills (lifetime total across all characters),
            characters: list of dicts with:
                username, char_kills, current_run_kills
        """
        cur = self._conn.cursor()

        # Per-character lifetime kills (sum of closed session deltas)
        char_kills = {}
        for row in cur.execute("""
            SELECT c.id, c.steamid, c.username,
                   COALESCE(SUM(s.kills_end - s.kills_start), 0) AS char_kills
            FROM characters c
            LEFT JOIN sessions s ON s.character_id = c.id AND s.kills_end IS NOT NULL
            GROUP BY c.id
        """):
            char_kills[row["id"]] = {
                "steamid": row["steamid"],
                "username": row["username"],
                "char_kills": row["char_kills"],
                "current_run_kills": 0,
            }

        # Current run kills (open session -- kills_end is NULL while live)
        for row in cur.execute("""
            SELECT s.character_id,
                   COALESCE(s.kills_end, s.kills_start) AS run_kills
            FROM sessions s
            WHERE s.ended_at IS NULL
        """):
            if row["character_id"] in char_kills:
                char_kills[row["character_id"]]["current_run_kills"] = row["run_kills"]

        # Group by Steam account
        accounts = {}
        for row in cur.execute("SELECT * FROM steam_accounts ORDER BY last_seen DESC"):
            accounts[row["steamid"]] = {
                "steamid": row["steamid"],
                "steam_name": row["steam_name"] or row["steamid"],
                "avatar_url": row["avatar_url"],
                "pz_hours": row["pz_hours"],
                "account_kills": 0,
                "characters": [],
            }

        for char_id, cd in char_kills.items():
            sid = cd["steamid"]
            if sid not in accounts:
                # Character exists but account row is missing -- handle gracefully.
                accounts[sid] = {
                    "steamid": sid,
                    "steam_name": sid,
                    "avatar_url": None,
                    "pz_hours": None,
                    "account_kills": 0,
                    "characters": [],
                }
            accounts[sid]["account_kills"] += cd["char_kills"]
            accounts[sid]["characters"].append({
                "username": cd["username"],
                "char_kills": cd["char_kills"],
                "current_run_kills": cd["current_run_kills"],
            })

        # Sort characters within each account by char_kills desc
        for acc in accounts.values():
            acc["characters"].sort(key=lambda c: c["char_kills"], reverse=True)

        # Sort accounts by account_kills desc
        return sorted(accounts.values(), key=lambda a: a["account_kills"], reverse=True)
