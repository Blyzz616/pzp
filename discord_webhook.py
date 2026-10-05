"""
discord_webhook.py — Minimal Discord webhook client shared by
discord_module.py and mod_restart.py.
"""

__version__ = "5.0.1"

import logging
import requests

log = logging.getLogger("pzpanel.discord_webhook")


class Discord:
    """Simple Discord webhook poster used by mod_restart.py."""

    def __init__(self, url):
        self.url = url

    def content(self, text):
        """Post a plain text message. Returns True on success."""
        try:
            r = requests.post(self.url, json={"content": text}, timeout=8)
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning("Discord webhook content post failed: %s", e)
            return False

    def raw(self, payload):
        """Post an arbitrary payload dict. Returns True on success."""
        try:
            r = requests.post(self.url, json=payload, timeout=8)
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning("Discord webhook raw post failed: %s", e)
            return False

    def embed(self, colour, *, title=None, description=None,
              thumbnail=None, fields=None):
        """Post an embed. Returns True on success."""
        payload = {"color": colour}
        if title:       payload["title"]       = title
        if description: payload["description"] = description
        if thumbnail:   payload["thumbnail"]   = {"url": thumbnail}
        if fields:      payload["fields"]      = fields
        return self.raw({"embeds": [payload]})
