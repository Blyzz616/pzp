"""
ui_helpers.py — HTML rendering helpers for pzpanel.

Extracted from main.py (v4.2.2). All functions here are pure HTML/string
utilities with no FastAPI imports. They depend on server_config for
load_server_config() and PAGE_STYLE.
"""

__version__ = "5.3.0"

import html
import random
import re
from datetime import datetime, timedelta, timezone

from server_config import load_server_config

# Re-exported so main.py can do: from ui_helpers import PAGE_STYLE
PAGE_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --void: #0a0d09; --panel: #12160f; --line: #2b3324;
  --ink: #e8e3d2; --ink-dim: #8b9179;
  --amber: #ffb020; --amber-dim: #7a5a1a;
  --rust: #c2432b; --rust-dim: #5c2418;
  --signal: #6fbf5a; --signal-dim: #34401f;
  --info: #3a7fb8;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--void);
  background-image: radial-gradient(ellipse at top left,rgba(255,176,32,.05),transparent 50%),radial-gradient(ellipse at bottom right,rgba(111,191,90,.04),transparent 50%);
  color:var(--ink); font-family:'Inter',system-ui,sans-serif; min-height:100vh; }
.hazard-bar { height:7px; background:repeating-linear-gradient(135deg,var(--amber),var(--amber) 14px,#171203 14px,#171203 28px); }
.hazard-bar.danger { background:repeating-linear-gradient(135deg,var(--rust),var(--rust) 14px,#171203 14px,#171203 28px); }
.hazard-frame { background:repeating-linear-gradient(135deg,var(--amber),var(--amber) 14px,#171203 14px,#171203 28px); padding:7px; border-radius:3px; }
.hazard-frame.danger { background:repeating-linear-gradient(135deg,var(--rust),var(--rust) 14px,#171203 14px,#171203 28px); }
.hazard-frame-inner { background:var(--panel); border-radius:2px; padding:1.25rem 1.5rem; }
.wrap { max-width:760px; margin:0 auto; padding:2.5rem 1.5rem 3rem; }
.eyebrow { font-family:'JetBrains Mono',monospace; font-size:.72rem; letter-spacing:.18em; color:var(--ink-dim); text-transform:uppercase; margin:0 0 .35rem; }
h1 { font-family:'Oswald',sans-serif; font-weight:700; font-size:2rem; letter-spacing:.02em; text-transform:uppercase; margin:0 0 1.5rem; }
nav.tabs { display:flex; align-items:center; gap:1.5rem; margin-bottom:1.75rem; font-family:'JetBrains Mono',monospace; font-size:.78rem; letter-spacing:.06em; text-transform:uppercase; }
nav.tabs a { color:var(--ink-dim); text-decoration:none; padding-bottom:.3rem; border-bottom:2px solid transparent; transition:color .15s,border-color .15s; }
nav.tabs a:hover,nav.tabs a.active { color:var(--amber); border-color:var(--amber); }
nav.tabs a:focus-visible { outline:2px solid var(--amber); outline-offset:3px; }
nav.tabs a.icon-tab { display:inline-flex; align-items:center; margin-left:auto; }
nav.tabs a.icon-tab svg { display:block; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:3px; padding:1.75rem; margin-bottom:25px; }
.readout { display:flex; align-items:center; gap:.85rem; margin-bottom:1.75rem; flex-wrap:wrap; }
.led { width:13px; height:13px; border-radius:50%; flex-shrink:0; background:var(--led-colour,var(--ink-dim)); box-shadow:0 0 10px 2px var(--led-colour,transparent); }
.led.pulse { animation:ledpulse 1.7s ease-in-out infinite; }
@keyframes ledpulse { 0%{opacity:1}8%{opacity:.4}10%{opacity:1}30%{opacity:.85}33%{opacity:1}55%{opacity:.3}60%{opacity:1}80%{opacity:.92}100%{opacity:1} }
@media(prefers-reduced-motion:reduce){.led.pulse{animation:none}}
.readout-label { font-family:'JetBrains Mono',monospace; font-size:1rem; letter-spacing:.1em; text-transform:uppercase; }
.readout-sub { font-family:'JetBrains Mono',monospace; font-size:.72rem; color:var(--ink-dim); margin-left:auto; }
.actions { display:flex; gap:.6rem; flex-wrap:wrap; }
.actions form { margin:0; }
.btn { font-family:'JetBrains Mono',monospace; font-weight:600; font-size:.8rem; letter-spacing:.08em; text-transform:uppercase; padding:.65rem 1.3rem; border:1px solid var(--line); border-radius:2px; background:#1a2016; color:var(--ink); cursor:pointer; transition:filter .12s,transform .05s; }
.btn:hover{filter:brightness(1.25)}.btn:active{transform:translateY(1px)}.btn:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.btn.primary{background:var(--amber-dim);border-color:var(--amber);color:#fff3dc}
.btn.info{background:#16324a;border-color:var(--info);color:#dcefff}
.btn.danger{background:var(--rust-dim);border-color:var(--rust);color:#ffe3dc}
a.link{color:var(--amber);text-decoration:none}a.link:hover{text-decoration:underline}
.note { font-family:'JetBrains Mono',monospace; font-size:.72rem; color:var(--ink-dim); line-height:1.6; margin-top:1.5rem; padding-top:1rem; border-top:1px solid var(--line); }
.banner{font-size:.85rem;margin:0 0 1.25rem;padding:.6rem .9rem;border-radius:2px;border:1px solid currentColor}
.banner.success{color:var(--signal);background:rgba(111,191,90,.08)}
.banner.error{color:var(--rust);background:rgba(194,67,43,.08)}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-family:'JetBrains Mono',monospace;font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-dim);padding:0 .7rem .6rem;border-bottom:1px solid var(--line)}
td{padding:.7rem;border-bottom:1px solid var(--line);font-size:.9rem;vertical-align:middle}
td.mono{font-family:'JetBrains Mono',monospace;font-size:.8rem;color:var(--ink-dim)}
.tag{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:.68rem;letter-spacing:.06em;text-transform:uppercase;padding:.25rem .55rem;border-radius:2px;border:1px solid currentColor;background:transparent;white-space:nowrap;cursor:help}
.tag.ok{color:var(--signal);background:rgba(111,191,90,.08)}.tag.warn{color:var(--amber);background:rgba(255,176,32,.08)}
.tag.info{color:#8fc4ea;background:rgba(58,127,184,.1)}.tag.muted{color:var(--ink-dim);background:rgba(139,145,121,.08)}
.thumb{width:40px;height:40px;object-fit:cover;border-radius:2px;border:1px solid var(--line);display:block}
.thumb-empty{background:#1a2016}.thumb-lg{width:96px;height:96px;object-fit:cover;border-radius:2px;border:1px solid var(--line);flex-shrink:0}
.thumb-btn{background:none;border:none;padding:0;cursor:pointer;display:block}.thumb-btn:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.link-id{color:var(--amber);font-family:'JetBrains Mono',monospace;font-size:.8rem;text-decoration:none}
.link-id:hover{text-decoration:underline}
.lookup-form{display:flex;gap:.6rem;margin-bottom:1.25rem}
.input{font-family:'JetBrains Mono',monospace;background:#0d110a;border:1px solid var(--line);color:var(--ink);padding:.6rem .8rem;border-radius:2px;flex:1}
.input:focus-visible{outline:2px solid var(--amber);outline-offset:1px}
.preview{display:flex;gap:1rem;align-items:center;margin:.5rem 0 1rem}
.preview-title{font-family:'Oswald',sans-serif;font-size:1.15rem;margin:0 0 .25rem;text-transform:none}
code{font-family:'JetBrains Mono',monospace;background:#0d110a;padding:.1rem .35rem;border-radius:2px;font-size:.85em}
.link-remove{color:var(--rust);background:none;border:none;font-family:'JetBrains Mono',monospace;font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;cursor:pointer;padding:0}
.link-remove:hover{text-decoration:underline}
.link-add{color:var(--signal);background:none;border:none;font-family:'JetBrains Mono',monospace;font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;cursor:pointer;padding:0}
.link-add:hover{text-decoration:underline}
.link-title{color:var(--ink);background:none;border:none;padding:0;font:inherit;text-align:left;cursor:pointer}
.link-title:hover{color:var(--amber);text-decoration:underline}
.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(10,13,9,.78);backdrop-filter:blur(4px);align-items:center;justify-content:center;padding:1.25rem;z-index:100}
.modal-backdrop.open{display:flex}
.modal-box{background:var(--panel);border:1px solid var(--modal-accent,var(--rust));border-radius:3px;max-width:440px;width:100%;max-height:85vh;overflow-y:auto;padding:1.75rem}
.modal-thumb{width:100%;max-width:280px;aspect-ratio:1/1;object-fit:cover;border-radius:2px;border:1px solid var(--line);display:block;margin:0 auto 1.1rem}
.modal-eyebrow{font-family:'JetBrains Mono',monospace;font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--modal-accent,var(--rust));margin:0 0 .4rem}
.modal-title{font-family:'Oswald',sans-serif;font-size:1.25rem;margin:0 0 .3rem;text-transform:none}
.modal-meta{font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--ink-dim);margin:0 0 1rem}
.modal-desc{font-size:.85rem;line-height:1.6;color:var(--ink);margin:0 0 1.4rem}
.modal-actions{display:flex;gap:.6rem;justify-content:flex-end}.modal-actions form{margin:0}
.footer-version{text-align:center;font-family:'JetBrains Mono',monospace;font-size:.68rem;color:var(--ink-dim);opacity:.6;margin:1.5rem 0 0}
.console-toolbar{display:flex;gap:.6rem;align-items:center;margin-bottom:.75rem;flex-wrap:wrap}
.console-toolbar .input{flex:1;min-width:140px}
.console-status{font-family:'JetBrains Mono',monospace;font-size:.72rem;margin-left:auto;white-space:nowrap}
.console-path{font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--ink-dim);margin:0 0 .75rem;word-break:break-all}
.console-box{background:#050704;border:1px solid var(--line);border-radius:2px;padding:1rem;height:60vh;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:.76rem;line-height:1.55;color:var(--ink);white-space:pre-wrap;word-break:break-word;margin:0;scrollbar-width:thin;scrollbar-color:var(--amber-dim) var(--void)}
.console-box::-webkit-scrollbar{width:10px}.console-box::-webkit-scrollbar-track{background:var(--void)}
.console-box::-webkit-scrollbar-thumb{background:var(--amber-dim);border-radius:2px;border:2px solid var(--void)}
.console-box::-webkit-scrollbar-thumb:hover{background:var(--amber)}
* {scrollbar-width:thin;scrollbar-color:var(--amber-dim) var(--void)}
*::-webkit-scrollbar{width:10px;height:10px}*::-webkit-scrollbar-track{background:var(--void)}
*::-webkit-scrollbar-thumb{background:var(--amber-dim);border-radius:2px;border:2px solid var(--void)}
*::-webkit-scrollbar-thumb:hover{background:var(--amber)}
*::-webkit-scrollbar-corner{background:var(--void)}
.lvl-log{color:var(--ink);font-weight:600}.lvl-warn{color:var(--amber);font-weight:700}.lvl-error{color:var(--rust);font-weight:700}.lvl-server-started{color:var(--signal);font-weight:700;background:rgba(111,191,90,.08);display:block}.lvl-steam-connect{color:var(--info);font-weight:600;display:block}
.drag-handle{cursor:grab;color:var(--ink-dim);text-align:center;font-size:1.1rem;user-select:none;width:1.6rem}
.drag-handle:active{cursor:grabbing}tr.dragging{opacity:.35}
.cfg-section{margin-bottom:0;padding-bottom:0;border-bottom:1px solid var(--line)}
.cfg-section:last-child{border-bottom:none}
.cfg-section-title{font-family:'Oswald',sans-serif;font-size:1.1rem;letter-spacing:.04em;text-transform:uppercase;margin:0;color:var(--amber);padding:.9rem 0;cursor:pointer;display:flex;align-items:center;justify-content:space-between;user-select:none}
.cfg-section-title:hover{color:#ffc840}
.cfg-section-title .cfg-chevron{font-size:.75rem;transition:transform .2s;display:inline-block}
.cfg-section-title.collapsed .cfg-chevron{transform:rotate(-90deg)}
.cfg-section-body{padding-bottom:1.25rem}
.cfg-group{margin-bottom:1rem;display:flex;flex-direction:column;gap:.35rem}
.cfg-label{font-family:'JetBrains Mono',monospace;font-size:.75rem;color:var(--ink-dim);text-transform:uppercase;letter-spacing:.05em}
.cfg-help{font-family:'JetBrains Mono',monospace;font-size:.68rem;color:var(--ink-dim);opacity:.7;line-height:1.5}
.cfg-disabled{opacity:.35;pointer-events:none}
.toggle-wrap{display:flex;align-items:center;gap:.75rem}
.toggle{position:relative;display:inline-block;width:44px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:#1a2016;border:1px solid var(--line);border-radius:24px;cursor:pointer;transition:background .2s,border-color .2s}
.toggle-slider:before{content:'';position:absolute;left:3px;top:3px;width:16px;height:16px;border-radius:50%;background:var(--ink-dim);transition:transform .2s,background .2s}
.toggle input:checked+.toggle-slider{background:var(--signal-dim);border-color:var(--signal)}
.toggle input:checked+.toggle-slider:before{transform:translateX(20px);background:var(--signal)}
.range-wrap{display:flex;align-items:center;gap:.75rem}
.range-wrap input[type=range]{flex:1;accent-color:var(--amber);background:#1a2016;border-radius:2px}
.range-value{font-family:'JetBrains Mono',monospace;font-size:.85rem;min-width:4rem;text-align:right;background:#0d110a;border:1px solid var(--line);border-radius:2px;padding:.3rem .5rem;color:var(--ink)}
.checkbox-group{display:flex;flex-wrap:wrap;gap:.5rem 1rem}
.checkbox-item{display:flex;align-items:center;gap:.4rem;font-size:.85rem}
.checkbox-item input{accent-color:var(--amber)}
.pw-wrap{position:relative;display:flex}
.pw-wrap .input{flex:1;padding-right:2.5rem}
.pw-eye{position:absolute;right:.6rem;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;color:var(--ink-dim);padding:.2rem;display:flex;align-items:center}
.cfg-save-bar{position:fixed;bottom:0;left:0;right:0;background:var(--panel);border-top:1px solid var(--amber);padding:.75rem 1.5rem;display:flex;align-items:center;justify-content:flex-end;gap:1rem;z-index:50;transform:translateY(100%);transition:transform .2s}
.cfg-save-bar.visible{transform:translateY(0)}
</style>
"""


def _led_style(colour):
    import random as _r
    delay = _r.uniform(-1.6, 0)
    return f"--led-colour:{colour}; animation-delay:{delay:.2f}s;"


def page_shell(title, active_tab, body_html, extra_head="", version=""):
    server_name, _ = load_server_config()

    def tab(href, label, key):
        cls = "active" if key == active_tab else ""
        return f'<a href="{href}" class="{cls}">{label}</a>'

    def icon_tab(href, key, title_attr, svg):
        cls = "active icon-tab" if key == active_tab else "icon-tab"
        return (f'<a href="{href}" class="{cls}" title="{html.escape(title_attr)}" '
                f'aria-label="{html.escape(title_attr)}">{svg}</a>')

    cog_svg = (
        '<svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        '<circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="3"/>'
        '<rect id="pzp-cog-tooth" x="10.4" y="3.6" width="3.2" height="3.4" rx="0.6"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(60 12 12)"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(120 12 12)"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(180 12 12)"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(240 12 12)"/>'
        '<use href="#pzp-cog-tooth" transform="rotate(300 12 12)"/>'
        '</svg>'
    )

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>{title}</title>
    {extra_head}
    <link rel="icon" type="image/x-icon" href="/favicon.ico">
    <link rel="icon" type="image/png" sizes="16x16" href="/favicon/favicon-16x16.png">
    <link rel="icon" type="image/png" sizes="32x32" href="/favicon/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="48x48" href="/favicon/favicon-48x48.png">
    <link rel="icon" type="image/png" sizes="64x64" href="/favicon/favicon-64x64.png">
    <link rel="apple-touch-icon" sizes="128x128" href="/favicon/favicon-128x128.png">
    <link rel="apple-touch-icon" sizes="256x256" href="/favicon/favicon-256x256.png">
    {PAGE_STYLE}
</head>
<body>
    <div class="hazard-bar"></div>
    <div class="wrap">
        <p class="eyebrow">{html.escape(server_name)} &middot; Survivor Ops Terminal</p>
        <h1>PZ Panel</h1>
        <nav class="tabs">
            {tab("/", "Status", "status")}
            {tab("/config", "Config", "config")}
            {tab("/mods", "Mod Manifest", "mods")}
            {tab("/killboard", "Killboard", "killboard")}
            {tab("/console", "Console", "console")}
            {tab("/log", "Log", "log")}
            {icon_tab("/settings", "settings", "Settings", cog_svg)}
        </nav>
        <div class="panel">
            {body_html}
        </div>
        <p class="footer-version">pzpanel v{version}</p>
    </div>
</body>
</html>"""


def _fmt_ts(ts):
    if ts is None:
        return "\u2014"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_log_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str or "\u2014"


def _fmt_removed_at(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone()
        age = datetime.now().astimezone() - local_dt
        if age <= timedelta(days=7):
            return local_dt.strftime("%a %d %b %Y %H:%M")
        return local_dt.strftime("%d %b %Y")
    except Exception:
        return iso_str or "\u2014"


def _steam_id_link(mod_id):
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod_id}"
    return (f'<a class="link-id" href="{html.escape(url, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">{html.escape(mod_id)}</a>')


_BBCODE_SIMPLE_SUBS = [
    (re.compile(r'\[h1\](.*?)\[/h1\]', re.IGNORECASE | re.DOTALL),
     r'<strong style="display:block;font-size:1.15em;margin:.6em 0 .3em;">\1</strong>'),
    (re.compile(r'\[h2\](.*?)\[/h2\]', re.IGNORECASE | re.DOTALL),
     r'<strong style="display:block;font-size:1.08em;margin:.6em 0 .3em;">\1</strong>'),
    (re.compile(r'\[h3\](.*?)\[/h3\]', re.IGNORECASE | re.DOTALL),
     r'<strong style="display:block;margin:.5em 0 .2em;">\1</strong>'),
    (re.compile(r'\[b\](.*?)\[/b\]', re.IGNORECASE | re.DOTALL), r'<strong>\1</strong>'),
    (re.compile(r'\[i\](.*?)\[/i\]', re.IGNORECASE | re.DOTALL), r'<em>\1</em>'),
    (re.compile(r'\[u\](.*?)\[/u\]', re.IGNORECASE | re.DOTALL),
     r'<span style="text-decoration:underline;">\1</span>'),
    (re.compile(r'\[(?:s|strike)\](.*?)\[/(?:s|strike)\]', re.IGNORECASE | re.DOTALL),
     r'<span style="text-decoration:line-through;">\1</span>'),
    (re.compile(r'\[hr\](?:\s*\[/hr\])?', re.IGNORECASE), r'<hr style="border-color:var(--line);margin:.6em 0;">'),
    (re.compile(r'\[url=(https?://[^\]\s]*)\](.*?)\[/url\]', re.IGNORECASE | re.DOTALL),
     r'<a href="\1" target="_blank" rel="noopener noreferrer" class="link">\2</a>'),
    (re.compile(r'\[url\](https?://[^\[\s]*?)\[/url\]', re.IGNORECASE | re.DOTALL),
     r'<a href="\1" target="_blank" rel="noopener noreferrer" class="link">\1</a>'),
    (re.compile(r'\[quote(?:=[^\]]*)?\](.*?)\[/quote\]', re.IGNORECASE | re.DOTALL),
     r'<blockquote style="margin:.5em 0;padding:.3em .8em;border-left:3px solid var(--line);color:var(--ink-dim);">\1</blockquote>'),
    (re.compile(r'\[code\](.*?)\[/code\]', re.IGNORECASE | re.DOTALL),
     r'<code style="display:block;white-space:pre-wrap;margin:.4em 0;padding:.4em .6em;background:var(--void);">\1</code>'),
    (re.compile(r'\[spoiler\](.*?)\[/spoiler\]', re.IGNORECASE | re.DOTALL),
     r'<span style="border:1px dashed var(--line);padding:0 .3em;">\1</span>'),
    (re.compile(r'\[img\](https?://[^\s\[\]]+?)\[/img\]', re.IGNORECASE),
     r'<img src="\1" alt="" style="max-width:100%;height:auto;display:block;margin:.4em 0;">'),
]

# Order matters: inline tags are closed before block tags so a cut-off
# "[list][*][b]x" becomes "...[/b][/list]", never an overlapping pair.
_BBCODE_PAIRED_TAGS = ("b", "i", "u", "s", "strike", "h1", "h2", "h3", "code",
                       "spoiler", "quote", "list", "olist")
_BBCODE_STRAY_CLOSER_RE = re.compile(
    r'\[/(?:hr|list|olist|h1|h2|h3|b|i|u|s|strike|quote|code|spoiler|url)\]', re.IGNORECASE)

_BBCODE_LIST_RE = re.compile(r'\[list\](.*?)\[/list\]', re.IGNORECASE | re.DOTALL)
_BBCODE_OLIST_RE = re.compile(r'\[olist\](.*?)\[/olist\]', re.IGNORECASE | re.DOTALL)
_BBCODE_ITEM_SPLIT_RE = re.compile(r'\[\*\]', re.IGNORECASE)


def _render_bbcode(raw_text):
    """
    Converts a common subset of Steam Workshop's BBCode-like description
    markup into safe HTML: [h1]-[h3], [b]/[i]/[u]/[s] (or [strike]),
    [list]/[*]/[/list], [olist], [url]/[url=...], [hr]. This is NOT a
    claim of covering Steam's complete/official BBCode spec -- just the
    tags that actually show up in real Workshop descriptions. An
    unrecognized or malformed tag (e.g. one cut off by this project's
    own description-length truncation) is left as literal escaped text
    rather than breaking the page.

    Escapes the raw text FIRST (so any literal HTML/script a mod author
    put in a description can't execute), then layers the recognized
    tags on top of the already-escaped text -- safe because escaping
    only touches &/</>/"/', never the square brackets these patterns
    match on.
    """
    raw_text = raw_text or ""
    # Description truncation can cut markup in half, leaving an opener with
    # no closer ([list] with no [/list], a dangling [b]) -- close any
    # unbalanced paired tags so the content still renders, then drop stray
    # closers (e.g. "[/hr]", or a [/b] whose opener was cut) rather than
    # showing them as literal text.
    for _tag in _BBCODE_PAIRED_TAGS:
        _open = len(re.findall(rf"\[{_tag}(?:=[^\]]*)?\]", raw_text, re.IGNORECASE))
        _close = len(re.findall(rf"\[/{_tag}\]", raw_text, re.IGNORECASE))
        if _open > _close:
            raw_text += f"[/{_tag}]" * (_open - _close)
    text = html.escape(raw_text)

    def _list_repl(tag):
        def repl(m):
            items = [i.strip() for i in _BBCODE_ITEM_SPLIT_RE.split(m.group(1)) if i.strip()]
            if not items:
                return ""
            lis = "".join(f"<li>{i}</li>" for i in items)
            return f'<{tag} style="margin:.3em 0 .6em;padding-left:1.3em;">{lis}</{tag}>'
        return repl

    text = _BBCODE_LIST_RE.sub(_list_repl("ul"), text)
    text = _BBCODE_OLIST_RE.sub(_list_repl("ol"), text)

    for pattern, repl in _BBCODE_SIMPLE_SUBS:
        text = pattern.sub(repl, text)

    text = _BBCODE_STRAY_CLOSER_RE.sub("", text)

    # Preserve literal line breaks -- Workshop descriptions commonly use
    # them for paragraph breaks outside of any BBCode tag.
    text = text.replace("\n", "<br>")
    return text


def _mod_identity_cells(mod_id, title, preview_url, description, desc_limit=800):
    mod_id = mod_id or ""
    title = title or mod_id
    desc_snippet = (description or "")[:desc_limit]
    rendered_desc = _render_bbcode(desc_snippet)
    data_attrs = (
        f'data-mod-id="{html.escape(mod_id, quote=True)}" '
        f'data-title="{html.escape(title, quote=True)}" '
        f'data-preview="{html.escape(preview_url or "", quote=True)}" '
        f'data-description="{html.escape(rendered_desc, quote=True)}"'
    )
    thumb_html = (
        f'<button type="button" class="thumb-btn" {data_attrs} onclick="openDetailModal(this)">'
        f'{_thumb_html(preview_url)}</button>'
    )
    title_html = (
        f'<button type="button" class="link-title" {data_attrs} onclick="openDetailModal(this)">'
        f'{html.escape(title)}</button>'
    )
    return thumb_html, title_html


def _thumb_html(url, size_class="thumb"):
    if url:
        return f'<img src="{html.escape(url)}" class="{size_class}" alt="">'
    return f'<div class="{size_class} thumb-empty"></div>'


def _mod_status_tag(r):
    note_attr = f' title="{html.escape(r["note"])}"' if r["note"] else ""
    if r["update_available"]:
        return '<span class="tag warn">Update available</span>'
    if r.get("needs_enable"):
        return ('<span class="tag info" title="Downloaded but not loaded -- '
                'add its internal ID to Mods= to actually enable it">Needs Enable</span>')
    if r["live_ts"] is None:
        return f'<span class="tag muted"{note_attr}>Lookup failed</span>'
    if r["installed_ts"] is None:
        return f'<span class="tag info"{note_attr}>Not installed</span>'
    if r["note"]:
        return f'<span class="tag muted"{note_attr}>Flagged</span>'
    return '<span class="tag ok">Current</span>'
