"""
ini_safe.py -- Safe writes for the live server .ini (realm.ini).

Every panel edit of the server ini goes through safe_write_lines():
  1. Timestamped backup into <ini dir>/backups/ (newest 20 kept) --
     the same location the Config page already used.
  2. Atomic replace: write a temp file next to the ini, fsync, then
     os.replace(), so a crash or full disk mid-write can never leave a
     truncated/empty realm.ini. File mode is preserved.
  3. If the temp file can't be created (directory not writable but the
     file itself is), falls back to an in-place write -- never worse
     than the old behaviour.

A failed backup is logged and does not block the edit.
"""

__version__ = "5.2.1"

import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_BACKUPS = 20


def backup_ini(ini_path):
    """Copy ini_path into <dir>/backups/. Returns backup Path or None."""
    ini_path = Path(ini_path)
    try:
        backup_dir = ini_path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")
        backup_path = backup_dir / f"{ini_path.name}.{ts}"
        shutil.copy2(str(ini_path), str(backup_path))
        for old in sorted(backup_dir.glob(f"{ini_path.name}.*"))[:-KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
        return backup_path
    except OSError as e:
        log.warning("Could not back up %s: %s", ini_path, e)
        return None


def safe_write_lines(ini_path, lines):
    """Back up, then atomically replace ini_path with the given lines."""
    ini_path = Path(ini_path)
    backup_ini(ini_path)
    try:
        fd, tmp = tempfile.mkstemp(prefix=ini_path.name + ".", suffix=".tmp",
                                   dir=str(ini_path.parent))
    except OSError:
        with open(ini_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copymode(str(ini_path), tmp)
        except OSError:
            pass
        os.replace(tmp, str(ini_path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
