"""
kill_tracker.py — Kill data processor for pzpanel.

Owns the YAML kills file and all kill arithmetic. Tails the Lua event log
(pzp_events.log) written by pzp_Server.lua and keeps the YAML up to date.

YAML format:
  'steamid':
    lifetimeKills: N          # sum of (previous + current) for all accounts
    'AccountName':
      previous: N             # kills from all dead characters on this account
      current:  N             # kills from the currently alive character
      alive:    true/false
      survived: N             # in-game hours survived (current character only)

Kill flow:
  - UpdateKills event  → update current, update survived, check milestones
  - PlayerDied event   → previous += current, current = 0, alive = false,
                         update survived, recompute lifetimeKills
  - On join            → ensure persona/account exists; return snapshot for
                         discord embed
  - On disconnect      → return current snapshot; no YAML change (run not over)

Session kills (computed by discord_module, not here):
  session_start = snapshot at join
  session_kills = kills_at_disconnect - session_start
                + sum of completed runs (deaths) this session
"""

__version__ = "4.6.4"

import logging
import os
import re
import threading
import time
from pathlib import Path

import platform_compat as pc

log = logging.getLogger("pzpanel.kill_tracker")

# ---------------------------------------------------------------------------
# Milestone thresholds
# ---------------------------------------------------------------------------
_LIFE_MILESTONES     = [1, 50, 100, 500, 1000, 5000, 10000]
_LIFETIME_BASE       = [1, 50, 100, 500, 1000, 5000, 10000]
_LIFETIME_STEP       = 10000


def _lifetime_milestones_up_to(n):
    result = list(_LIFETIME_BASE)
    nxt = _LIFETIME_BASE[-1] + _LIFETIME_STEP
    while nxt <= n:
        result.append(nxt)
        nxt += _LIFETIME_STEP
    return result


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def _yaml_str(s):
    """Single-quoted YAML string, escaping internal single quotes."""
    return "'" + str(s).replace("'", "''") + "'"


def _write_yaml(data, path):
    """
    Write the kills dict to path atomically.
    data: {steamid: {lifetimeKills, accounts: {name: {previous, current, alive, survived}}}}
    """
    lines = []
    for steamid in sorted(data):
        entry = data[steamid]
        lines.append(f"{_yaml_str(steamid)}:")
        lines.append(f"  lifetimeKills: {entry['lifetimeKills']}")
        for uname in sorted(entry["accounts"]):
            acc = entry["accounts"][uname]
            lines.append(f"  {_yaml_str(uname)}:")
            lines.append(f"    previous: {acc['previous']}")
            lines.append(f"    current:  {acc['current']}")
            lines.append(f"    alive:    {'true' if acc['alive'] else 'false'}")
            lines.append(f"    survived: {acc['survived']}")
    content = "\n".join(lines) + "\n"
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, str(path))


def _read_yaml(path):
    """
    Parse the kills YAML.
    Returns {steamid: {lifetimeKills, accounts: {name: {previous, current, alive, survived}}}}
    """
    result = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    current_steamid = None
    current_account = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue

        # Top-level steamid: 'steamid':
        m = re.match(r"^'(.*)':\s*$", line)
        if m and not line.startswith("  "):
            current_steamid = m.group(1)
            current_account = None
            if current_steamid not in result:
                result[current_steamid] = {"lifetimeKills": 0, "accounts": {}}
            continue

        if current_steamid is None:
            continue

        # lifetimeKills
        m = re.match(r"^  lifetimeKills:\s*(\S+)", line)
        if m:
            try:
                result[current_steamid]["lifetimeKills"] = int(float(m.group(1)))
            except ValueError:
                pass
            continue

        # Account name: '  AccountName':
        m = re.match(r"^  '(.*)':\s*$", line)
        if m:
            current_account = m.group(1)
            if current_account not in result[current_steamid]["accounts"]:
                result[current_steamid]["accounts"][current_account] = {
                    "previous": 0, "current": 0, "alive": False, "survived": 0.0
                }
            continue

        if current_account is None:
            continue

        acc = result[current_steamid]["accounts"][current_account]

        m = re.match(r"^    previous:\s*(\S+)", line)
        if m:
            try: acc["previous"] = int(float(m.group(1)))
            except ValueError: pass
            continue

        m = re.match(r"^    current:\s*(\S+)", line)
        if m:
            try: acc["current"] = int(float(m.group(1)))
            except ValueError: pass
            continue

        m = re.match(r"^    alive:\s*(\S+)", line)
        if m:
            acc["alive"] = m.group(1).strip() == "true"
            continue

        m = re.match(r"^    survived:\s*(\S+)", line)
        if m:
            try: acc["survived"] = float(m.group(1))
            except ValueError: pass

    return result


def _recompute_lifetime(entry):
    """Recompute lifetimeKills as sum of (previous + current) for all accounts."""
    total = 0
    for acc in entry["accounts"].values():
        total += acc["previous"] + acc["current"]
    entry["lifetimeKills"] = total


# ---------------------------------------------------------------------------
# KillTracker
# ---------------------------------------------------------------------------

class KillTracker:
    """
    Owns the YAML kills file and all kill arithmetic.
    Thread-safe. Call start() to begin tailing the event log.
    """

    def __init__(self, yaml_path, event_log_path,
                 milestone_callback=None, poll_interval=2.0):
        """
        yaml_path         — path to pzp_player_kills.yaml
        event_log_path    — path to pzp_events.log written by pzp_Server.lua
        milestone_callback— callable(steamid, username, threshold, tier,
                                     current_kills, lifetime_kills, survived)
                            called when a milestone is crossed
        poll_interval     — seconds between event log polls
        """
        self.yaml_path      = Path(yaml_path)
        self.event_log_path = Path(event_log_path)
        self.milestone_cb   = milestone_callback
        self.poll_interval  = poll_interval

        self._lock  = threading.Lock()
        self._data  = _read_yaml(self.yaml_path)   # in-memory kills store
        self._stop  = threading.Event()

        # Milestone tracking (in-memory; reset on panel restart is safe
        # because first-poll logic pre-populates them)
        self._life_fired     = {}   # {username: set(thresholds)}
        self._lifetime_fired = {}   # {steamid:  set(thresholds)}
        self._prepopulate_milestones()

        log.info("KillTracker: loaded %d steamid(s) from %s",
                 len(self._data), self.yaml_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start the event log tail thread. Returns the thread."""
        t = threading.Thread(target=self._tail_events,
                             name="kill-tracker", daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()

    def get_snapshot(self, steamid, username):
        """
        Return current kill count for this account's alive character.
        Returns 0 if not found.
        """
        with self._lock:
            return self._get_current(steamid, username)

    def get_lifetime(self, steamid):
        """Return total lifetime kills for this steamid across all accounts."""
        with self._lock:
            entry = self._data.get(steamid)
            return entry["lifetimeKills"] if entry else 0

    def get_account(self, steamid, username):
        """
        Return a copy of the account dict:
        {previous, current, alive, survived}
        Returns None if not found.
        """
        with self._lock:
            entry = self._data.get(steamid)
            if not entry:
                return None
            acc = entry["accounts"].get(username)
            return dict(acc) if acc else None

    def get_all(self):
        """Return a deep copy of the full data dict for killboard rendering."""
        with self._lock:
            import copy
            return copy.deepcopy(self._data)

    def on_join(self, steamid, username):
        """
        Ensure persona/account exists in YAML.
        Returns (lifetime_kills, current_kills, survived) for the join embed.
        """
        with self._lock:
            self._ensure_account(steamid, username)
            self._save()
            entry = self._data[steamid]
            acc   = entry["accounts"][username]
            return (entry["lifetimeKills"], acc["current"], acc["survived"])

    def on_disconnect(self, steamid, username):
        """
        Return current snapshot for session kill calculation.
        Does NOT modify the YAML — run is not over.
        Returns current kill count.
        """
        with self._lock:
            return self._get_current(steamid, username)

    # ------------------------------------------------------------------
    # Event log tail
    # ------------------------------------------------------------------

    def _tail_events(self):
        last_inode = None
        last_pos   = 0
        try:
            last_inode = pc.file_identity(str(self.event_log_path))
            last_pos   = self.event_log_path.stat().st_size
        except OSError:
            pass

        while not self._stop.is_set():
            self._stop.wait(self.poll_interval)
            try:
                st = self.event_log_path.stat()
            except FileNotFoundError:
                continue

            current_inode = pc.file_identity(str(self.event_log_path))
            if last_inode is not None and current_inode != last_inode:
                last_pos = 0
            last_inode = current_inode

            if st.st_size < last_pos:
                last_pos = 0
            if st.st_size <= last_pos:
                continue

            try:
                with open(self.event_log_path, "r",
                          encoding="utf-8", errors="replace") as f:
                    f.seek(last_pos)
                    chunk = f.read()
                    last_pos = f.tell()
            except OSError as e:
                log.warning("KillTracker: error reading event log: %s", e)
                continue

            for line in chunk.splitlines():
                line = line.strip()
                if line:
                    self._process_event_line(line)

    def _process_event_line(self, line):
        """Parse and apply one event line: steamid|username|kills|survived|isDeath"""
        parts = line.split("|")
        if len(parts) != 5:
            log.warning("KillTracker: malformed event line: %r", line)
            return
        steamid, username, kills_s, survived_s, is_death_s = parts
        try:
            kills    = int(float(kills_s))
            survived = float(survived_s)
            is_death = is_death_s.strip() == "1"
        except ValueError:
            log.warning("KillTracker: unparseable event line: %r", line)
            return

        with self._lock:
            self._ensure_account(steamid, username)
            entry = self._data[steamid]
            acc   = entry["accounts"][username]

            old_current  = acc["current"]
            old_lifetime = entry["lifetimeKills"]

            if is_death:
                # Accumulate into previous, reset current, mark dead
                acc["previous"] += kills
                acc["current"]   = 0
                acc["alive"]     = False
                acc["survived"]  = survived
            else:
                acc["current"]  = kills
                acc["survived"] = survived
                acc["alive"]    = True

            _recompute_lifetime(entry)
            self._save()

            new_current  = acc["current"] if not is_death else kills
            new_lifetime = entry["lifetimeKills"]

        # Milestone checks (outside lock to avoid deadlock with callback)
        if self.milestone_cb is not None:
            self._check_milestones(
                steamid, username,
                old_current, new_current,
                old_lifetime, new_lifetime,
                survived, is_death
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_account(self, steamid, username):
        """Create persona/account in _data if not present. Must hold _lock."""
        if steamid not in self._data:
            self._data[steamid] = {"lifetimeKills": 0, "accounts": {}}
            log.info("KillTracker: new persona %s", steamid)
        if username not in self._data[steamid]["accounts"]:
            self._data[steamid]["accounts"][username] = {
                "previous": 0, "current": 0, "alive": False, "survived": 0.0
            }
            log.info("KillTracker: new account %s / %s", steamid, username)

    def _get_current(self, steamid, username):
        """Return current kill count. Must hold _lock (or be called from locked context)."""
        entry = self._data.get(steamid)
        if not entry:
            return 0
        acc = entry["accounts"].get(username)
        return acc["current"] if acc else 0

    def _save(self):
        """Write YAML. Must hold _lock."""
        try:
            _write_yaml(self._data, self.yaml_path)
        except OSError as e:
            log.error("KillTracker: failed to write YAML: %s", e)

    def _prepopulate_milestones(self):
        """Pre-populate fired sets from current YAML so we don't spam on restart."""
        for steamid, entry in self._data.items():
            lt = entry["lifetimeKills"]
            lf = self._lifetime_fired.setdefault(steamid, set())
            for t in _lifetime_milestones_up_to(lt):
                if lt >= t:
                    lf.add(t)
            for uname, acc in entry["accounts"].items():
                current = acc["current"]
                mf = self._life_fired.setdefault(uname, set())
                for t in _LIFE_MILESTONES:
                    if current >= t:
                        mf.add(t)

    def _check_milestones(self, steamid, username,
                          old_current, new_current,
                          old_lifetime, new_lifetime,
                          survived, is_death):
        """Fire milestone callback for any thresholds crossed."""
        if self.milestone_cb is None:
            return

        # Per-life milestones (reset on death — clear fired set)
        if is_death:
            self._life_fired.pop(username, None)
        else:
            life_fired = self._life_fired.setdefault(username, set())
            for t in _LIFE_MILESTONES:
                if t in life_fired:
                    continue
                if old_current < t <= new_current:
                    life_fired.add(t)
                    self.milestone_cb(
                        steamid, username, t, "life",
                        new_current, new_lifetime, survived
                    )

        # Lifetime milestones
        lt_fired = self._lifetime_fired.setdefault(steamid, set())
        for t in _lifetime_milestones_up_to(new_lifetime):
            if t in lt_fired:
                continue
            if old_lifetime < t <= new_lifetime:
                lt_fired.add(t)
                self.milestone_cb(
                    steamid, username, t, "lifetime",
                    new_current, new_lifetime, survived
                )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_tracker(yaml_path, event_log_path,
                  milestone_callback=None, poll_interval=2.0):
    """Build and return a KillTracker. Call .start() to begin tailing."""
    return KillTracker(
        yaml_path=yaml_path,
        event_log_path=event_log_path,
        milestone_callback=milestone_callback,
        poll_interval=poll_interval,
    )
