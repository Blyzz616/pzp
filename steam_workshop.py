"""
Steam Workshop update checker (stdlib only -- urllib, no requests dependency).

Uses the public ISteamRemoteStorage/GetPublishedFileDetails endpoint, which
needs no API key for public Workshop items. Returns title + time_updated
(unix epoch) per mod ID so callers can diff against a last-known state.
"""

__version__ = "4.0.1"

import json
import urllib.parse
import urllib.request

STEAM_API_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"


class SteamWorkshopError(Exception):
    pass


def get_mod_details(mod_ids, timeout=10.0):
    """
    mod_ids: iterable of Workshop item ID strings/ints.
    Returns: dict of {mod_id_str: {"title": str, "time_updated": int}}
    Raises SteamWorkshopError on network/parse failure -- callers should
    decide how to handle a failed check (e.g. skip this cycle, don't treat
    a fetch failure as "no updates").
    """
    mod_ids = [str(m) for m in mod_ids]
    form_data = {"itemcount": str(len(mod_ids))}
    for i, mod_id in enumerate(mod_ids):
        form_data[f"publishedfileids[{i}]"] = mod_id

    body = urllib.parse.urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(STEAM_API_URL, data=body, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise SteamWorkshopError(f"Steam API request failed: {e}") from e

    try:
        details = payload["response"]["publishedfiledetails"]
    except (KeyError, TypeError) as e:
        raise SteamWorkshopError(f"Unexpected Steam API response shape: {e}") from e

    results = {}
    for item in details:
        mod_id = str(item.get("publishedfileid", ""))
        if not mod_id:
            continue
        # result == 1 means success; anything else means the item is
        # missing/private/deleted -- still record it so the caller can
        # flag it rather than silently dropping it.
        results[mod_id] = {
            "title": item.get("title", "(unknown -- fetch failed)"),
            "time_updated": item.get("time_updated"),
            "result": item.get("result"),
            "preview_url": item.get("preview_url"),
            "description": item.get("description", ""),
        }
    return results


if __name__ == "__main__":
    import sys
    from datetime import datetime, timezone

    if len(sys.argv) < 2:
        print("Usage: python3 steam_workshop.py <mod_id> [mod_id ...]")
        print("   or: python3 steam_workshop.py 3436537035 3476880649 ...")
        sys.exit(1)

    ids = sys.argv[1:]
    print(f"Checking {len(ids)} mod(s)...")
    try:
        details = get_mod_details(ids)
    except SteamWorkshopError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    for mod_id in ids:
        info = details.get(mod_id)
        if not info:
            print(f"{mod_id}: NOT RETURNED by Steam API")
            continue
        if info["result"] != 1:
            print(f"{mod_id}: result={info['result']} (not success) -- {info['title']}")
            continue
        ts = info["time_updated"]
        when = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "unknown"
        print(f"{mod_id}: {info['title']!r} -- last updated {when} (epoch {ts})")
