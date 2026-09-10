"""
Minimal parser for Valve's KeyValues (VDF) text format, as used in Steam's
appworkshop_*.acf files (stdlib only, no external vdf dependency).

Handles the plain nested "key" { ... } / "key" "value" structure seen in
real .acf files. Does NOT handle VDF conditionals (e.g. [$WIN32]) or
comments -- neither appears in appworkshop_*.acf output, so this is
intentionally not a general-purpose VDF parser.
"""

__version__ = "4.1.0"

import re

_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\{)|(\})')


class VDFParseError(Exception):
    pass


def parse_vdf(text):
    """Returns a nested dict, e.g. {"AppWorkshop": {"appid": "108600", ...}}"""
    tokens = []
    for m in _TOKEN_RE.finditer(text):
        if m.group(1) is not None:
            tokens.append(("str", m.group(1)))
        elif m.group(2):
            tokens.append(("open", "{"))
        elif m.group(3):
            tokens.append(("close", "}"))

    pos = 0

    def parse_object():
        nonlocal pos
        obj = {}
        while pos < len(tokens):
            ttype, tval = tokens[pos]
            if ttype == "close":
                pos += 1
                return obj
            if ttype != "str":
                raise VDFParseError(f"Unexpected token {tokens[pos]} at index {pos}")
            key = tval
            pos += 1
            if pos >= len(tokens):
                raise VDFParseError(f"Unexpected end of input after key {key!r}")
            ntype, nval = tokens[pos]
            if ntype == "str":
                obj[key] = nval
                pos += 1
            elif ntype == "open":
                pos += 1
                obj[key] = parse_object()
            else:
                raise VDFParseError(f"Unexpected token after key {key!r}: {tokens[pos]}")
        return obj

    if not tokens or tokens[0][0] != "str":
        raise VDFParseError("Expected a root key as the first token")
    root_key = tokens[0][1]
    pos = 1
    if pos >= len(tokens) or tokens[pos][0] != "open":
        raise VDFParseError(f"Expected '{{' after root key {root_key!r}")
    pos += 1
    root_obj = parse_object()
    return {root_key: root_obj}


def get_installed_workshop_items(acf_path):
    """
    Reads an appworkshop_<appid>.acf file and returns
    {mod_id_str: timeupdated_int} for everything actually installed on
    disk, taken from WorkshopItemsInstalled (not WorkshopItemDetails --
    the latter includes latest_timeupdated, which we deliberately don't
    trust as an update signal since it's unclear when SteamCMD refreshes
    it without an explicit query).
    """
    with open(acf_path, "r", encoding="utf-8") as f:
        text = f.read()
    data = parse_vdf(text)
    app_workshop = data.get("AppWorkshop", {})
    installed = app_workshop.get("WorkshopItemsInstalled", {})
    result = {}
    for mod_id, fields in installed.items():
        ts = fields.get("timeupdated")
        result[mod_id] = int(ts) if ts is not None else None
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python3 acf.py <path-to-appworkshop_108600.acf>")
        sys.exit(1)

    items = get_installed_workshop_items(sys.argv[1])
    print(f"Found {len(items)} installed workshop item(s):")
    for mod_id, ts in sorted(items.items()):
        print(f"  {mod_id}: timeupdated={ts}")
