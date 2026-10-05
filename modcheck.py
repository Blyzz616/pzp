"""
Mod-update checker.

Ties together three sources of truth:
  1. realm.ini's WorkshopItems=  -- what SHOULD be installed (never
     hardcoded here, always read live from the server config)
  2. the local ACF file           -- what's ACTUALLY installed on disk
  3. Steam's live Workshop API    -- what's currently published

A mod has a pending update when its live time_updated is newer than
what's recorded as installed in the ACF.
"""

__version__ = "5.2.0"

import configparser
import os
import re

import platform_compat as pc
from acf import get_installed_workshop_items
from ini_safe import safe_write_lines
from steam_workshop import get_mod_details, SteamWorkshopError

CONFIG_PATH = os.environ.get("PZPANEL_CONFIG") or str(pc.get_default_config_path())


class ModCheckError(Exception):
    pass


def load_paths_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return (
        cfg.get("paths", "server_ini",
                fallback="/home/pzserver/Zomboid/Server/realm.ini"),
        cfg.get("paths", "workshop_acf",
                fallback="/home/pzserver/pzserver/steamapps/workshop/appworkshop_108600.acf"),
    )


def get_configured_mod_ids(server_ini_path):
    """
    Reads WorkshopItems=... straight from the live server ini. PZ's ini
    format isn't standard sectioned INI (no [section] headers visible in
    realm.ini), so this is a direct line scan rather than configparser.

    Deduplicates while preserving order -- WorkshopItems= can end up
    with a literal duplicate ID in the file itself (confirmed on this
    server), and without this every check/display would show that mod
    twice even though it's still just one real mod.
    """
    pattern = re.compile(r"^\s*WorkshopItems\s*=\s*(.*)$")
    with open(server_ini_path, "r", encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                raw = m.group(1).strip()
                seen = set()
                ordered_unique = []
                for item in raw.split(";"):
                    item = item.strip()
                    if item and item not in seen:
                        seen.add(item)
                        ordered_unique.append(item)
                return ordered_unique
    raise ModCheckError(f"No WorkshopItems= line found in {server_ini_path}")


def get_mods_line_ids(server_ini_path):
    """
    Reads Mods=...  straight from the live server ini -- the internal
    string mod ID(s) PZ actually loads, as distinct from WorkshopItems=
    (which only controls what gets *downloaded*). A Workshop item being
    in WorkshopItems= with nothing here is the single most common cause
    of "I added the mod but it's not working": PZ downloaded it but
    never loaded it.
    """
    pattern = re.compile(r"^\s*Mods\s*=\s*(.*)$")
    with open(server_ini_path, "r", encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                raw = m.group(1).strip()
                return [x.strip() for x in raw.split(";") if x.strip()]
    return []


def add_mods_line_items(internal_ids):
    """
    Appends any of the given internal mod IDs to Mods= that aren't
    already present, preserving existing order. Returns the list of IDs
    actually added (empty if all were already there -- a no-op, not an
    error). Companion to add_workshop_item(), but for the second,
    easy-to-forget step: WorkshopItems= only gets a mod *downloaded*,
    Mods= is what actually makes PZ load it.
    """
    server_ini, _ = load_paths_config()
    with open(server_ini, "r", encoding="utf-8") as f:
        lines = f.readlines()

    pattern = re.compile(r"^(\s*Mods\s*=\s*)(.*)$")
    found = False
    added = []
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        found = True
        prefix, raw = m.group(1), m.group(2).strip()
        existing = [x.strip() for x in raw.split(";") if x.strip()]
        for iid in internal_ids:
            if iid not in existing:
                existing.append(iid)
                added.append(iid)
        if added:
            lines[i] = f"{prefix}{';'.join(existing)}\n"
        break

    if not found:
        raise ModCheckError(f"No Mods= line found in {server_ini}")

    if added:
        safe_write_lines(server_ini, lines)
    return added


def check_for_updates():
    """
    Returns a list of dicts, one per configured mod:
      mod_id, title, installed_ts, live_ts, update_available,
      internal_ids, internal_details, needs_enable, note

    internal_ids/needs_enable close the WorkshopItems=/Mods= gap: a
    mod can be fully downloaded (installed_ts set) but never actually
    loaded because its internal string ID was never added to Mods=.
    needs_enable is True when the mod's internal ID(s) are known
    (found via downloaded mod.info -- get_local_mod_ids()) but at
    least one isn't yet in Mods=. internal_ids is [] whenever the mod
    isn't installed yet (nothing to read) or genuinely has no code
    mod.info (e.g. a map-only Workshop item).

    Raises ModCheckError / SteamWorkshopError if the check can't be
    performed at all. Callers must NOT treat a raised exception as "no
    updates" -- only as "couldn't check this cycle, try again later".
    """
    server_ini, acf_path = load_paths_config()
    mod_ids = get_configured_mod_ids(server_ini)
    installed = get_installed_workshop_items(acf_path)
    live = get_mod_details(mod_ids)
    mods_line_ids = get_mods_line_ids(server_ini)

    results = []
    for mod_id in mod_ids:
        installed_ts = installed.get(mod_id)
        live_info = live.get(mod_id)

        internal_details = get_local_mod_details(mod_id) if installed_ts is not None else []
        internal_ids = [d["id"] for d in internal_details]
        # Flag only when NONE of the bundled IDs are enabled. Some Workshop
        # items bundle placeholder/legacy mods the admin deliberately leaves
        # out (e.g. Functional Gutters' FunctionalGuttersRemoved); requiring
        # all of them would nag forever after a deliberate partial enable.
        needs_enable = bool(internal_ids) and not any(i in mods_line_ids for i in internal_ids)
        for d in internal_details:
            d["enabled"] = d["id"] in mods_line_ids

        if live_info is None or live_info.get("result") != 1:
            results.append({
                "mod_id": mod_id, "title": "(unknown)",
                "installed_ts": installed_ts, "live_ts": None,
                "preview_url": None, "description": "",
                "update_available": False,
                "internal_ids": internal_ids, "internal_details": internal_details,
                "needs_enable": needs_enable,
                "note": "Steam API did not return valid details for this mod ID",
            })
            continue

        title = live_info["title"]
        live_ts = live_info["time_updated"]
        preview_url = live_info.get("preview_url")
        description = live_info.get("description", "")

        if installed_ts is None:
            results.append({
                "mod_id": mod_id, "title": title,
                "installed_ts": None, "live_ts": live_ts,
                "preview_url": preview_url, "description": description,
                "update_available": False,
                "internal_ids": internal_ids, "internal_details": internal_details,
                "needs_enable": needs_enable,
                "note": "not installed yet -- listed in WorkshopItems but the server "
                        "hasn't downloaded it (needs a restart)",
            })
            continue

        update_available = live_ts is not None and live_ts > installed_ts
        results.append({
            "mod_id": mod_id, "title": title,
            "installed_ts": installed_ts, "live_ts": live_ts,
            "preview_url": preview_url, "description": description,
            "update_available": update_available,
            "internal_ids": internal_ids, "internal_details": internal_details,
            "needs_enable": needs_enable,
            "note": None,
        })

    return results


def add_workshop_item(mod_id):
    """
    Appends mod_id to WorkshopItems= in the live server ini if not
    already present. Returns True if added, False if it was already
    there (a no-op, not an error -- avoids creating duplicate IDs, one
    of which already exists in this server's real WorkshopItems= list).

    Only touches WorkshopItems=. Does NOT touch Mods= -- the internal
    string mod ID PZ needs there isn't available from the Workshop API,
    only from the mod's own mod.info after it's actually been
    downloaded, so auto-populating Mods= here would risk silently
    writing a wrong or incomplete value. Once the content IS downloaded,
    check_for_updates()'s needs_enable/internal_ids fields surface that
    it's ready, and add_mods_line_items() handles that second step.
    """
    server_ini, _ = load_paths_config()
    mod_id = str(mod_id).strip()
    if not mod_id.isdigit():
        raise ModCheckError(f"Not a valid Workshop ID: {mod_id!r}")

    with open(server_ini, "r", encoding="utf-8") as f:
        lines = f.readlines()

    pattern = re.compile(r"^(\s*WorkshopItems\s*=\s*)(.*)$")
    found = False
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        found = True
        prefix, raw = m.group(1), m.group(2).strip()
        existing = [x.strip() for x in raw.split(";") if x.strip()]
        if mod_id in existing:
            return False
        existing.append(mod_id)
        lines[i] = f"{prefix}{';'.join(existing)}\n"
        break

    if not found:
        raise ModCheckError(f"No WorkshopItems= line found in {server_ini}")

    safe_write_lines(server_ini, lines)
    return True


def _derive_content_dir(acf_path):
    """
    Given .../steamapps/workshop/appworkshop_<appid>.acf, derives
    .../steamapps/workshop/content/<appid> -- where SteamCMD actually
    unpacks each Workshop item's downloaded files. Derived rather than
    hardcoded so this keeps working if the install path ever changes.
    """
    m = re.search(r"appworkshop_(\d+)\.acf$", acf_path)
    if not m:
        raise ModCheckError(f"Could not determine appid from ACF path: {acf_path}")
    appid = m.group(1)
    workshop_dir = os.path.dirname(acf_path)
    return os.path.join(workshop_dir, "content", appid)


def get_local_mod_details(mod_id):
    """
    Like get_local_mod_ids() but returns [{"id": ..., "name": ...}, ...]
    so the Enable picker can show each bundled mod's display name
    (mod.info name=) next to its internal ID. name falls back to the id
    when mod.info has no name= line. Returns [] if nothing is downloaded.
    """
    _, acf_path = load_paths_config()
    content_dir = _derive_content_dir(acf_path)
    mod_root = os.path.join(content_dir, str(mod_id), "mods")
    if not os.path.isdir(mod_root):
        return []

    details = []
    for entry in sorted(os.listdir(mod_root)):
        info_path = os.path.join(mod_root, entry, "mod.info")
        if not os.path.isfile(info_path):
            continue
        mid, name = None, None
        try:
            with open(info_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r"^\s*id\s*=\s*(.+?)\s*$", line)
                    if m and mid is None:
                        mid = m.group(1)
                        continue
                    m = re.match(r"^\s*name\s*=\s*(.+?)\s*$", line)
                    if m and name is None:
                        name = m.group(1)
        except OSError:
            continue
        if mid:
            details.append({"id": mid, "name": name or mid})
    return details


def get_local_mod_ids(mod_id):
    """
    Reads mod.info files from a Workshop item's downloaded content to
    find its real internal mod ID(s) -- the string IDs PZ's Mods= line
    actually needs. Ground truth read from disk, not guessed. A single
    Workshop item can bundle multiple mods, hence a list.

    Returns [] if the content isn't downloaded (nothing to read) rather
    than raising -- that's an expected case, e.g. an item added but
    never restarted into yet.
    """
    _, acf_path = load_paths_config()
    content_dir = _derive_content_dir(acf_path)
    mod_root = os.path.join(content_dir, str(mod_id), "mods")
    if not os.path.isdir(mod_root):
        return []

    ids = []
    for entry in os.listdir(mod_root):
        info_path = os.path.join(mod_root, entry, "mod.info")
        if not os.path.isfile(info_path):
            continue
        try:
            with open(info_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r"^\s*id\s*=\s*(.+?)\s*$", line)
                    if m:
                        ids.append(m.group(1))
                        break
        except OSError:
            continue
    return ids


def reorder_workshop_items(new_order):
    """
    Rewrites WorkshopItems= with the given ordered list of mod IDs.
    new_order must contain exactly the same SET of mod IDs currently in
    WorkshopItems= (same membership, different order) -- raises
    ModCheckError otherwise. This function's only job is reordering;
    use add_workshop_item()/remove_workshop_item() to actually change
    which mods are installed.
    """
    server_ini, _ = load_paths_config()
    current = get_configured_mod_ids(server_ini)
    if len(new_order) != len(current) or set(new_order) != set(current):
        raise ModCheckError(
            "New order must contain exactly the same set of mod IDs as "
            "WorkshopItems= currently has -- use Add/Remove to change membership.")

    with open(server_ini, "r", encoding="utf-8") as f:
        lines = f.readlines()

    pattern = re.compile(r"^(\s*WorkshopItems\s*=\s*)(.*)$")
    found = False
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            found = True
            lines[i] = f"{m.group(1)}{';'.join(new_order)}\n"
            break

    if not found:
        raise ModCheckError(f"No WorkshopItems= line found in {server_ini}")

    safe_write_lines(server_ini, lines)


def remove_workshop_item(mod_id):
    """
    Removes mod_id from WorkshopItems=. Also removes any internal mod
    ID(s) it owns from Mods=, discovered via get_local_mod_ids() (ground
    truth from downloaded content) -- not guessed, and skipped entirely
    if the mod was never actually downloaded (nothing to find).

    Returns True if removed from WorkshopItems=, False if it wasn't
    there to begin with (a no-op, not an error).
    """
    server_ini, _ = load_paths_config()
    mod_id = str(mod_id).strip()
    internal_ids = get_local_mod_ids(mod_id)

    with open(server_ini, "r", encoding="utf-8") as f:
        lines = f.readlines()

    ws_pattern = re.compile(r"^(\s*WorkshopItems\s*=\s*)(.*)$")
    mods_pattern = re.compile(r"^(\s*Mods\s*=\s*)(.*)$")
    removed_from_workshop = False

    for i, line in enumerate(lines):
        m = ws_pattern.match(line)
        if m:
            prefix, raw = m.group(1), m.group(2).strip()
            existing = [x.strip() for x in raw.split(";") if x.strip()]
            if mod_id in existing:
                existing.remove(mod_id)
                lines[i] = f"{prefix}{';'.join(existing)}\n"
                removed_from_workshop = True
            continue
        if internal_ids:
            m2 = mods_pattern.match(line)
            if m2:
                prefix, raw = m2.group(1), m2.group(2).strip()
                existing = [x.strip() for x in raw.split(";") if x.strip()]
                new_existing = [x for x in existing if x not in internal_ids]
                if new_existing != existing:
                    lines[i] = f"{prefix}{';'.join(new_existing)}\n"

    if not removed_from_workshop:
        return False

    safe_write_lines(server_ini, lines)
    return True


if __name__ == "__main__":
    from datetime import datetime, timezone

    def fmt(ts):
        if ts is None:
            return "unknown"
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    try:
        results = check_for_updates()
    except (ModCheckError, SteamWorkshopError) as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)

    updates = [r for r in results if r["update_available"]]

    print(f"Checked {len(results)} configured mod(s), {len(updates)} update(s) available.\n")
    for r in results:
        flag = "UPDATE AVAILABLE" if r["update_available"] else "up to date"
        line = f"[{flag:17}] {r['mod_id']}: {r['title']}"
        line += f"  (installed {fmt(r['installed_ts'])}, live {fmt(r['live_ts'])})"
        if r["note"]:
            line += f"  -- {r['note']}"
        print(line)
