"""
kill_tracker.py -- Feeds the pzp mod's event log into player_db.

pzp_Server.lua appends one line per client report to
Zomboid/Lua/pzp_events.log:

  v2|<J|U|D>|<account username>|<kills>|<in-game hours>|<character name>

  J  character loaded or created (snapshot)
  U  periodic update
  D  character died; kills/hours are the final values

The read position is stored in the database, so reports written while
the panel is down are processed when it comes back. Once everything has
been read and the file is over _ROTATE_BYTES it is truncated.

Callbacks (run outside the read lock):
  on_death(report)                      -- report dict from apply_report
  on_milestone(report, threshold, tier) -- tier "life" (character kills)
                                           or "lifetime" (persona total)
A milestone is announced once, for the highest threshold crossed between
two consecutive reports (98 -> 103 announces 100).
"""

__version__ = "5.0.1"

import logging
import threading
from pathlib import Path

import platform_compat as pc

log = logging.getLogger("pzpanel.kill_tracker")

LIFE_MILESTONES = [1, 50, 100, 500, 1000, 5000, 10000]
_LIFETIME_BASE = [1, 50, 100, 500, 1000, 5000, 10000]
_LIFETIME_STEP = 10000

_ROTATE_BYTES = 512 * 1024


def lifetime_milestones_up_to(n):
    result = [t for t in _LIFETIME_BASE if t <= n]
    nxt = _LIFETIME_BASE[-1] + _LIFETIME_STEP
    while nxt <= n:
        result.append(nxt)
        nxt += _LIFETIME_STEP
    return result


def _highest_crossed(thresholds, old, new):
    crossed = [t for t in thresholds if old < t <= new]
    return max(crossed) if crossed else None


def parse_event(line):
    """Parse one event-log line. Returns (kind, username, kills, hours, name) or None."""
    parts = line.split("|", 5)
    if len(parts) != 6 or parts[0] != "v2" or parts[1] not in ("J", "U", "D"):
        return None
    _, kind, username, kills_s, hours_s, name = parts
    username = username.strip()
    if not username:
        return None
    try:
        kills = max(0, int(float(kills_s)))
        hours = max(0.0, float(hours_s))
    except ValueError:
        return None
    return kind, username, kills, hours, name.strip()


class KillTracker:

    def __init__(self, db, event_log_path, on_death=None, on_milestone=None,
                 poll_interval=2.0):
        self.db = db
        self.event_log_path = Path(event_log_path)
        self.on_death = on_death
        self.on_milestone = on_milestone
        self.poll_interval = poll_interval
        self._read_lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        t = threading.Thread(target=self._run, name="kill-tracker", daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()

    def _run(self):
        log.info("KillTracker: tailing %s", self.event_log_path)
        while not self._stop.is_set():
            try:
                self.drain()
            except Exception:
                log.exception("KillTracker: drain failed")
            self._stop.wait(self.poll_interval)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def drain(self):
        """
        Process every complete line not yet read. Safe to call from any
        thread; the connection watcher calls it before join/disconnect so
        those see the latest kill counts.
        """
        notices = []
        with self._read_lock:
            path = self.event_log_path
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return
            identity = str(pc.file_identity(str(path)))
            pos = int(self.db.get_meta("event_log_pos", "0") or 0)
            if self.db.get_meta("event_log_id") != identity or size < pos:
                pos = 0

            if size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read(size - pos)
                # Leave a partial last line for the next pass.
                end = chunk.rfind(b"\n") + 1
                for raw in chunk[:end].splitlines():
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        notices.extend(self._process(line))
                pos += end

            if pos == size and size > _ROTATE_BYTES:
                pos = self._rotate(path, size, pos)

            self.db.set_meta("event_log_pos", pos)
            self.db.set_meta("event_log_id", identity)

        for fn, args in notices:
            try:
                fn(*args)
            except Exception:
                log.exception("KillTracker: callback failed")

    def _rotate(self, path, size, pos):
        """Truncate a fully-read log. A line the mod appends in the instant
        between the size check and the truncate is lost."""
        try:
            if path.stat().st_size != size:
                return pos
            with open(path, "r+b") as f:
                f.truncate(0)
            log.info("KillTracker: rotated %s at %d bytes", path, size)
            return 0
        except OSError as e:
            log.warning("KillTracker: could not rotate %s: %s", path, e)
            return pos

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def _process(self, line):
        """Apply one line. Returns a list of (callback, args) to run."""
        parsed = parse_event(line)
        if parsed is None:
            log.debug("KillTracker: skipping line %r", line)
            return []
        kind, username, kills, hours, name = parsed
        report = self.db.apply_report(kind, username, kills, hours, name)
        if report is None:
            return []

        notices = []
        if report["died"] and self.on_death:
            notices.append((self.on_death, (report,)))

        old = report["old_kills"]
        new = report["kills"]
        steamid = report["steamid"]

        if old is None:
            # First sighting of a character with history: record the
            # milestones it is already past so they aren't announced late.
            reached = _highest_crossed(LIFE_MILESTONES, -1, new)
            if reached:
                self.db.claim_life_milestone(report["char_id"], reached)
            if steamid and report["persona_kills"] is not None:
                reached = _highest_crossed(
                    lifetime_milestones_up_to(report["persona_kills"]), -1,
                    report["persona_kills"])
                if reached:
                    self.db.claim_lifetime_milestone(steamid, reached)
            return notices

        if not report["died"]:
            t = _highest_crossed(LIFE_MILESTONES, old, new)
            if t and self.db.claim_life_milestone(report["char_id"], t):
                if self.on_milestone:
                    notices.append((self.on_milestone, (report, t, "life")))

        if steamid and report["persona_kills"] is not None and new > old:
            after = report["persona_kills"]
            before = after - (new - old)
            t = _highest_crossed(lifetime_milestones_up_to(after), before, after)
            if t and self.db.claim_lifetime_milestone(steamid, t):
                if self.on_milestone:
                    notices.append((self.on_milestone, (report, t, "lifetime")))
        return notices
