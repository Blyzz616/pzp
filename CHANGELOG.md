# Changelog

All notable changes to pzpanel are tracked here. Versioning follows the
same convention as diZcord: a single X.Y.Z applied uniformly across every
file. X = breaking/major, Y = new feature, Z = patch/bugfix.

Dates below are approximate reconstructions from session timestamps
(journalctl output, screenshots) since versioning wasn't tracked from
the project's start -- treat day-level precision as best-effort, not
exact.

## [2.20.0] - 2026-09-03

### Changed

- **Settings page follow-ups, all from real feedback after first use:**
  - **Cog icon redesigned.** The original 8-tooth version (ring +
    thin rays offset outward with a gap) read as a sun/light-mode
    toggle, not a gear -- confirmed by actually rendering the SVG to a
    PNG and looking at it, not just reasoning about the geometry.
    New version: a thicker ring with 6 wider, shorter teeth placed
    flush against the ring's outer edge (no gap), which renders as a
    clearly recognizable gear at nav-bar size.
  - **"realm.ini Path" label renamed** to "World Config (.ini) Path" --
    the path was always configurable, never hardcoded to that literal
    filename; the old label implied otherwise.
  - **"Find" buttons** for the World Config and Workshop ACF paths:
    searches conventional PZ install locations (the panel's own home
    directory's `Zomboid/` folder, then more broadly, plus other users'
    `Zomboid/` folders for multi-user setups) for `.ini`/`.acf` files,
    verifying each candidate by checking its contents for expected
    markers (`WorkshopItems=`/`PVP=`/`DefaultPort=` for the ini,
    `"appid" "108600"` for the ACF) rather than matching on extension
    alone. Bounded to a few thousand files scanned so an unrelated home
    directory can't turn it into a slow, unbounded walk. New
    `/settings/find-file?type=ini|acf|console_log` route backs this;
    tested against a real fake filesystem with both a genuine and a
    decoy `.ini` file to confirm the content check actually excludes
    false positives, not just matches by extension.
  - **"Derive from .ini" buttons** for Console Log Path and
    Multiplayer Save Dir -- computed client-side from whatever's
    currently typed into World Config Path, using the exact relative
    derivation confirmed against the project's own known-correct
    example (`.../Zomboid/Server/realm.ini` -> `.../Zomboid/server-console.txt`
    and `.../Zomboid/Saves/Multiplayer/realm`). Verified by actually
    running the JS in Node against that ground truth, not just
    reasoning about the path math -- console log also gets a "Find"
    fallback (exact-filename search for `server-console.txt`) in case
    the derived guess isn't right for a given install.
  - **Masked-field placeholder simplified** to "Click to change" (was
    "Configured -- leave blank to keep") -- same underlying behavior,
    shorter and clearer.
  - **Save Settings button** moved to the right (`justify-content:
    flex-end`) and disabled by default, only enabling once any field in
    the form actually changes (a single delegated `input`/`change`
    listener on the form, not per-field comparison against original
    values -- once dirty it stays enabled for the rest of that page
    view, which is simpler and sufficient).
- **Status page**:
  - **LED pulse redesigned as an irregular flicker** instead of a
    smooth sine-wave pulse (new multi-stop `@keyframes ledpulse`), with
    a randomized negative `animation-delay` computed fresh on every
    page render (new `_led_style()` helper) so multiple LEDs -- or the
    same LED across different browser tabs -- don't visibly flicker in
    lockstep.
  - Wipe World & Reset button gets `margin-left:auto` to push it to the
    right within its `.actions` row.
- **Console page**: scrollbar restyled to match the amber/dark theme
  (`scrollbar-color`/`scrollbar-width` for Firefox,
  `::-webkit-scrollbar*` for Chromium/Safari) instead of the browser's
  default gray scrollbar.

## [2.19.0] - 2026-09-03

### Added

- **Settings page** (`/settings`), a cog SVG icon in the nav rather than
  a text tab (hand-drawn from a stroked ring + rotated rect "teeth", not
  a copied icon-library glyph -- uses `currentColor` so it automatically
  follows the same hover/active color rules the text tabs already have).
  Covers most of `pzpanel.ini`: RCON host/port/password, all of
  `[paths]` (including `multiplayer_save_dir`), the Discord webhook,
  server display name/unit, all of `[player_events]`, and all of
  `[steam]`.
  - **Sensitive fields (RCON password, Discord webhook, Steam API key)
    are masked once set** -- the real value is never re-embedded in the
    rendered page at all (confirmed this holds even with real-looking
    secret values in a test config, not just checking the visual mask),
    just an empty box with a "Configured -- leave blank to keep"
    placeholder. Leaving one blank on save keeps the existing value
    untouched; typing a new one replaces it.
  - Saves edit `pzpanel.ini` by rewriting only the specific changed
    `key = value` lines in place (new `_update_ini_settings()`),
    appending a new key/section if one doesn't exist yet -- same
    reasoning as `modcheck.reorder_workshop_items()`: a configparser
    read+write round-trip would silently strip every comment in the
    file, and `pzpanel.ini.example`'s inline documentation is the whole
    point of this format. New keys are appended at the end of their
    section, not right after the header.
  - Notes directly on the page which sections apply immediately
    (RCON/paths/server, re-read fresh on every request) vs. need a
    panel restart (Discord/player_events/Steam, only read once into the
    long-running watcher at startup).

### Fixed

- **Two real bugs found by actually saving the form and then trying to
  boot the watcher afterward, not just by testing the save step in
  isolation:** leaving an optional numeric field (e.g.
  `respawn_window`) blank wrote an empty string into the ini, which
  broke `int(cfg.get(key, fallback="600"))`-style code downstream since
  configparser's fallback only applies when a key is entirely *absent*,
  not *present-but-empty* -- and the same applied to optional path
  fields like `state_file`, which crashed the watcher thread outright
  (`Path("").expanduser()` has no valid parent to write into). Fixed by
  treating blank as "don't write this key at all" uniformly across
  every non-checkbox field, not just numbers. **Trade-off, noted
  directly**: this means the settings page can no longer be used to
  explicitly clear an optional field back to blank (e.g. disabling the
  Danger Zone by blanking `multiplayer_save_dir`) -- that still needs
  direct file editing. Chosen deliberately: a missing "clear" button is
  a much smaller problem than a crash.

## [2.18.4] - 2026-09-03

### Changed

- `.panel`'s bottom padding set to 0 (was `1.75rem` all around, now
  `1.75rem 1.75rem 0`), and the Danger Zone's `.hazard-frame` bottom
  margin changed from `1.5rem` to `0` to match -- removes the double
  gap that stacked up between the frame's own spacing and the panel's
  trailing padding when Danger Zone was the last thing on the page.

## [2.18.3] - 2026-09-03

### Changed

- **Danger Zone's hazard stripe now frames the whole section, not just
  a bar above it.** New `.hazard-frame`/`.hazard-frame-inner` CSS: an
  outer div carries the repeating-chevron background with a thin
  padding, an inner div sits on the normal panel background inside
  that -- so the red chevron border wraps all four sides, matching the
  page's existing top hazard-bar's visual language but as a full frame
  around the danger content instead of a single top edge.

## [2.18.2] - 2026-09-03

### Added

- **"Danger Zone" section on the Status page** -- a full world/character
  wipe-and-reset action, gated behind a new, deliberately-explicit
  `[paths] multiplayer_save_dir` config key (no computed fallback from
  `[server] name` or `server_ini`'s path -- confirmed directly that the
  real Saves/Multiplayer folder name is case-sensitive and comes from
  the realm `.ini` file's own basename, which isn't safe to assume from
  other configured paths). Section is completely absent from the page
  if this key isn't set -- safe by default, opt-in only.
  - Visually marked with a red hazard-bar variant (`.hazard-bar.danger`,
    same repeating-chevron style as the page's existing amber one).
  - New `/danger/wipe-saves` route: stops the server (a no-op if
    already offline), verifies it actually went offline, then
    `shutil.rmtree()`s the configured directory. Leaves the server
    stopped afterward rather than auto-restarting -- this is a
    "wipe and inspect before starting fresh" action, not a
    stop-apply-start cycle like the mod-management routes.
  - Type-to-confirm modal: the exact folder name (case-sensitive) must
    be typed to enable the destructive button, checked both
    client-side (disables the submit button) and server-side (a
    client-side-only check can be bypassed with a direct POST).
  - A basic guard-rail rejects the configured path outright if it
    doesn't actually look like a `Saves/Multiplayer/<name>` path
    (checked before touching systemctl at all), in case the config key
    is ever set wrong.
  - Tested against a real temp directory, not just the route logic in
    isolation: wrong confirmation text, wrong case, unconfigured,
    guard-rail rejection, and "stop didn't actually go offline" all
    confirmed to leave the directory untouched; only an exact,
    case-correct confirmation with a clean stop actually deletes it.

### Changed

- "Previously Removed Mod" heading on the Mod Manifest page was
  wrapping onto two lines on narrow/mobile widths (0.18em letter-spacing
  at that length didn't fit). Reduced to 0.06em (matching the nav tabs'
  spacing) and made it correctly pluralize ("Previously Removed Mod" vs
  "Previously Removed Mods") based on count, rather than a fixed string.
- **"Add" → "Re-add" throughout the Previously Removed flow**, to
  distinguish it from genuinely adding a new mod: the row button, the
  confirmation modal's title/button ("Confirm Re-add", "Restart &
  Re-add"), the online-server note text, and both success banner
  messages ("Re-added to WorkshopItems...", "Re-added and restarting").
- **Log page (`/log`) now shows local server time instead of UTC.**
  `actionlog.py` still stores everything as UTC ISO 8601 internally
  (unchanged) -- only the display converts it, via a new
  `_fmt_log_time()` helper (same `.astimezone()` approach
  `_fmt_removed_at()` already used). Header changed from "Time (UTC)"
  to plain "Time". Verified against a forced non-UTC timezone (not just
  checking the format changed) to confirm an actual conversion happens,
  not just cosmetic relabeling.

## [2.17.3] - 2026-09-03

### Added

- **Drag-and-drop mod reordering on the Mod Manifest page.** Each row
  now has a drag handle; dragging reorders the table client-side, and
  a right-aligned "Apply New Order & Restart Server" button (narrower
  than the table, appears only once the order actually changes from
  what's live) submits the new order. New `reorder_workshop_items()` in
  `modcheck.py` rewrites `WorkshopItems=` with the new order after
  validating it's the same set of mod IDs (raises `ModCheckError`
  otherwise -- this function only reorders, Add/Remove still own
  membership changes). New `/mods/reorder` route mirrors
  `remove_mod_with_restart`'s stop-apply-start pattern: always stops
  first (a no-op if already offline, same realm.ini-must-not-be-open
  reasoning as Add/Remove), applies, restarts -- and leaves the server
  stopped (not silently restarted) if the reorder itself is rejected,
  rather than restarting with a half-applied or unchanged manifest.
- Confirmed (no code change needed): the manifest already displayed
  mods in `WorkshopItems=`'s exact file order -- `get_configured_mod_ids()`
  preserves it and nothing downstream re-sorts. Used as the baseline
  for the new drag-and-drop feature above.

## [2.17.2] - 2026-09-03

### Fixed

- **Steam persona/hours/"also played" enrichment could get permanently
  stuck showing only persona+avatar, even with a valid `api_key`
  configured.** Root cause: `Steam.profile()`'s cache (`cache_hours`,
  default 24h) had no way to know a cached entry was written *before*
  an `api_key` existed -- so adding a key after the fact would silently
  keep serving the old no-key (XML-fallback-only) result for up to 24h,
  which looks identical to "the key isn't working." Now tags each cache
  entry with whether it was fetched via the API (`via_api`), and treats
  a no-key-sourced hit as stale the moment a key becomes available --
  self-heals on the next join, no manual cache-clearing needed. Also
  added diagnostic logging (`journalctl -u pzpanel`) for every Steam
  lookup: cache hit vs fresh fetch, which method was used, and what
  came back, to make this kind of issue visible next time instead of a
  silent "missing detail" symptom.
- **Kill counts could be double-counted into lifetime/session totals**
  when two rollover events (death, disconnect, or a server-down mass
  disconnect) happened close together, before the kill-count mod's next
  ~60s file rewrite. Each rollover site used to independently read
  whatever `kills_current_run` currently held and add it in -- so e.g. a
  death immediately followed by a disconnect would see the same
  unchanged on-disk snapshot twice and add it twice. Consolidated all
  three rollover sites into one `_roll_kills()` helper that tracks, per
  player, the kills file's mtime at the moment its value was last
  actually rolled in (`kills_accounted_mtime`) -- a second rollover
  against the same unchanged snapshot is now a no-op for the totals
  (still reports the correct "kills this run" number for the message,
  just doesn't add it again). Found and fixed by actually testing the
  death-then-quick-disconnect sequence, not by inspection alone.

### Added

- **Session kills**, tracked separately from both "kills this run"
  (resets on death) and lifetime kills (never resets): accumulates
  across every run/respawn within one continuous connection, and only
  resets when the player genuinely disconnects and later starts a new
  session (a respawn-triggered rejoin is not treated as a new session,
  matching how `sessions`/playtime tracking already worked). New
  `session_kills` state, keyed by steamid.
- **Disconnect (and server-down mass-disconnect) embeds now show three
  kill fields**: Kills this run, Session kills, Lifetime kills. A
  disconnect that wasn't preceded by a death rolls its still-in-progress
  run into both totals first, so the numbers are accurate even when a
  player just logs off alive rather than dying.

### Changed

- **Mod Manifest page (`/mods`)**:
  - Moved "+ Add Mod" from above the mod table to below the
    "N mods checked..." summary line, above "Previously Removed".
  - Changed it from a plain text link to a button (`.btn.primary`),
    right-aligned.
  - Fixed pluralization: "N mod(s) checked" / "N update(s) available"
    literal text is gone -- now correctly "1 mod checked" / "2 mods
    checked" / "0 mods checked", same for "update"/"updates".

## [2.17.1] - 2026-09-01

### Changed

- Removed the permanent "Server must be stopped before adding a mod..."
  warning banner from the Mod Manifest page (`/mods`). The underlying
  behavior it described is unchanged -- Add still requires the server
  offline, Remove still offers an automatic stop/apply/restart when
  online -- this only removed the standing banner text; `offline`
  itself is still computed and still drives that existing modal logic.

## [2.17.0] - 2026-08-30

### Added

- **diZcord (2.2.0) merged into PZP as an optional module**
  (`discord_module.py`), the first phase of restructuring around PZP
  as the primary project (see project hand-off notes). Player
  join/leave/death/server-up-down events now post Discord embeds
  directly from pzpanel's own process, instead of requiring diZcord's
  separate `dizcord.py` service to be run and kept in config-sync with
  the panel. Fully optional -- if `[discord] webhook_url` in
  `pzpanel.ini` is empty/`CHANGEME`, pzpanel runs exactly as it did
  before this module existed.
  - Runs as a daemon background thread (`threading.Thread`, started on
    FastAPI's `startup` event, stopped on `shutdown`), not on the
    async event loop -- diZcord's Discord/Steam/log-tailing calls are
    blocking by design (same as when it ran as its own process), and a
    background OS thread avoided having to rewrite any of that as
    async.
  - Patterns, flavour-text lists, and the `Discord`/`Steam`/`Tail`/
    `State`/`Watcher` classes are ported byte-for-byte from diZcord
    2.2.0's VERIFIED-against-B42.20 logic. Only `Watcher.run()`'s outer
    loop changed: a `threading.Event.wait()` instead of a bare
    `time.sleep()` loop with its own `SIGINT`/`SIGTERM` handlers
    (signal handlers only work in the main thread in Python, and this
    now runs as a subordinate thread inside pzpanel's process).
  - New message formats (kill counts, survival time, Steam owned-games
    "also played" list) are **not** part of this phase -- diZcord's
    existing join/leave/death embed formats are unchanged for now.
    That reformat is planned for a later phase, once the in-progress
    kill-count/survival-time Workshop mod exists to supply the data.
- New optional `[player_events]` config section (`state_file`,
  `poll_interval`, `respawn_window`, `logs_dir` override, `kills_file`)
  and `[steam]` section (`enabled`, `api_key`, `cache_hours`) in
  `pzpanel.ini` for the merged watcher's settings.
- **Kill-count tracking + new `/killboard` page**, reading
  `pzp_player_kills.txt` written by the separate PZP Zomboid Workshop
  mod (Workshop ID `3793020885`, in-game mod ID `pzp`). The mod
  rewrites the file wholesale roughly every 60 seconds with one
  `username|kills` line per player (current-run kills only -- resets
  per character/death, not lifetime). New `KillsFile` class in
  `discord_module.py` polls it by mtime (full re-parse on change, not
  a line-by-line follow, since the file is a snapshot rather than an
  append log). A forced immediate re-read happens right before building
  a join/disconnect/death message, so PZP never adds its own extra
  staleness on top of the mod's ~60s write interval (which remains an
  unavoidable floor either way).
  - PZP separately accumulates a **lifetime kill total per player**,
    since the mod's file has no concept of one -- rolled up at the
    moment a death is detected (`on_death`), adding that run's final
    count into `state.kills_lifetime` and clearing the current-run
    entry. **Unverified assumption**, flagged in code comments: this
    assumes the mod's file always reflects the currently-alive
    character's run and that a fresh character truly starts at 0 in
    the mod's own Lua state -- inferred from the file's behaviour, not
    from reading the mod's Lua source. Worth confirming if lifetime
    totals ever look off after a string of deaths.
  - `/killboard`: new page listing every player's current-run and
    PZP-tracked lifetime kills, sorted by current-run count. Shows a
    last-updated timestamp and an explanatory note about the mod's
    ~60s write cadence so a lagging number isn't mistaken for a bug.
- **Kill counts now appear in Discord join/disconnect/death embeds**
  (a `Kills this run` field; death embeds also add `Total kills on
  server`), when a `kills_file` is configured.
- **Paused-countdown controls: Cancel Timer and Check Mods.** When a
  restart countdown is paused, the status page now shows two more
  buttons alongside Resume/Postpone (only visible while paused --
  Resume/Postpone remain available either way):
  - **Cancel Timer** (new `/countdown/cancel` route) -- stands the
    restart down entirely from the paused state, distinct from the
    existing `/countdown/cancel-postponed` (same underlying
    `cc.send_command("cancel")`, kept as a separate route so the
    action log reads clearly about which state was cancelled).
    `mod_restart.py`'s command loop already handled `"cancel"` from
    the paused sub-loop before this change -- this only needed a route
    and a button, no orchestration changes.
  - **Check Mods** (new `/countdown/check-mods` route) -- runs a fresh
    `check_for_updates()` comparison on demand while still paused,
    without sending any command itself. If nothing's pending anymore
    (e.g. someone already restarted manually), auto-cancels with no
    confirmation needed. If updates are still pending, returns them to
    the frontend, which shows a popup listing them with three choices:
    **Resume Timer**, **Maintain Pause State** (just closes, no
    action), or **Cancel Timer**. Always a fresh check against
    whatever's pending right now, which can differ from the mods that
    originally triggered the countdown if more landed during the pause
    -- intentional, confirmed with the person before building.

### Fixed

- **`ONLINE` message's "time to start" duration was silently broken
  after the merge, found from real Discord output** (the message was
  showing with no duration at all). Root cause: `on_server_up` computed
  the duration against `state.watcher_started` -- which, pre-merge, was
  a reasonable proxy (diZcord and the game server were started
  together), but post-merge means "how long has *pzpanel itself* been
  running", since pzpanel is a persistent web panel, not something
  restarted alongside the game server. That's almost always well over
  the 1-hour cutoff, so the duration silently stopped appearing. Now
  queries systemd directly (`systemctl show -p ActiveEnterTimestamp`)
  for when the unit's current run actually became active -- correct
  regardless of what triggered the start (panel button, `mod_restart.py`'s
  automated restart in its own separate process, or an external/boot-time
  start pzpanel never directly saw). New `_systemd_active_since()` helper
  in `discord_module.py`; degrades to no duration shown (not a crash or
  wrong number) if systemd can't be queried or the timestamp can't be
  parsed.
- Duplicate join/leave/death Discord posts were traced to the old
  standalone `dizcord.service` still running alongside the merged
  watcher -- both processes tailing the same logs and posting to the
  same webhook. Not a code bug; `dizcord.service` needs to be stopped
  and disabled now that PZP covers the same functionality.
- **diZcord 2.2.0's own `on_death` had a latent bug, found by actually
  running the ported code against a sample line rather than trusting
  its "VERIFIED" status**: the `death` pattern's capture group was
  named `n`, but the handler read `groupdict()["name"]` -- a key that
  never matched, so every death embed's flavour-text silently rendered
  the player's name as `?` instead of the real name. Renamed the group
  to `name` in `discord_module.py`'s copy. Worth checking whether the
  still-standalone `dizcord.py` has the same bug, if it's deployed
  anywhere.
- `Discord.embed()`, `.content()`, and `.raw()` weren't returning
  `_post()`'s result (a bug introduced during the initial port, also
  caught by actually exercising the code) -- callers had no way to
  tell whether a post actually succeeded.

### Known limitation (new)

- **Join/disconnect embeds lost Steam persona/hours/"also played"
  enrichment after the merge** -- these require `[steam] api_key` in
  `pzpanel.ini`, which wasn't migrated from the old standalone
  `dizcord.ini`'s `[steam] api_key` during the merge. Without a key,
  `Steam.profile()` falls back to the public-profile-XML path (persona
  + avatar only), which is why join embeds still showed a Steam Profile
  link but nothing else. Needs a manual one-time copy of the key value
  from `dizcord.ini` into `pzpanel.ini`'s `[steam]` section -- not
  something code can fix, since the key itself lives only in the old
  config file.
- **Death messages still have no "time survived" field.** This needs a
  data source PZP doesn't have yet: in-game elapsed time (not
  wall-clock time, since PZ's day length is scaled by a server-configured
  multiplier), which only the Lua mod itself has visibility into.
  Blocked on the mod either already tracking this or being extended to.
  Not attempted this session pending that answer.

### Changed

- **Consolidated the project's two independent Discord HTTP clients
  into one.** `mod_restart.py` previously had its own
  `_post_discord_payload()` for restart/postpone announcements, with
  no retry logic at all -- separate from diZcord's `Discord` class,
  which retries on HTTP 429 (respecting Discord's `retry_after`) and
  on 5xx. `mod_restart.py`'s `post_discord()` /
  `post_discord_mod_restart()` (and the `--test-discord-only` CLI path)
  now route through `discord_module.Discord`, so every Discord post in
  the project -- restart announcements and the new player-event embeds
  alike -- shares one webhook config value and one retry-aware HTTP
  implementation instead of two that could silently drift apart.
- `console_log` and the server display name in `[server]`/`[paths]`
  are now the single source of truth shared by the rest of the panel
  AND the player-event watcher (which previously, as standalone
  diZcord, had its own separate `zomboid_dir`/`console_log`/`name`
  keys in `dizcord.ini`). Avoids two independently-configured paths to
  the same underlying log file that could point at different places.
- **Tracking (sessions/kills/deaths) now runs unconditionally**,
  independent of whether `[discord] webhook_url` is configured --
  needed once the `/killboard` page required this data with or without
  Discord. `Watcher.discord` is `None` when no webhook is configured;
  every `on_*` handler always updates `self.state` and only calls
  `self.discord.*` when it isn't `None`. Previously the entire Watcher
  (and therefore all tracking) simply didn't run without a webhook.

### Known limitation

- The `denied` (wrong server password) pattern in `discord_module.py`
  still carries B41-era wording, unverified against real B42.20 logs --
  carried over unchanged from diZcord 2.2.0's own unresolved item.
- The standalone `dizcord.py` service/repo (`diZcord-b42`) is not yet
  deprecated or removed -- that decision (retire it entirely vs. keep
  as a reference/fallback) is deferred to a later session.

## [2.16.0] - 2026-08-16

### Changed

- Mod icons in both the main manifest and Previously Removed tables are
  now clickable, opening the same detail modal as the title link
  (shared `_mod_identity_cells()` helper so both stay in lock-step).
- Workshop ID in both tables now links directly to the Steam Workshop
  page (`target="_blank"`), not just plain text.
- Previously Removed's note text replaced with a count:
  `N Mod(s) available in "previously used list"` (pluralizes correctly).
- "Removed" timestamp now shows the server's local time (was forced
  UTC), and uses a friendlier format depending on age: full
  `Sun 16 Aug 2026 09:46` within the last 7 days, a shorter `16 Aug 2026`
  beyond that -- exact time matters less for older entries.

### Known limitation

- `removed_mods.py` doesn't currently store a mod's description at
  removal time (only title/preview_url), so the detail modal opened
  from the Previously Removed table will show "(no description)" even
  though the main manifest's detail view has one. Not fixed here since
  it wasn't asked for -- flagging in case it's wanted later.

## [2.15.0] - 2026-08-13

### Added

- "Previously Removed" section on the Mod Manifest page (new
  `removed_mods.py`, plain JSON tracking file) -- every mod removed via
  Remove now stays listed with its thumbnail/title/removal time and an
  Add link, so a suspected misbehaving mod can be pulled for testing
  and put back easily if it turns out not to be the problem. Add
  mirrors Remove's exact UX: same blurred-backdrop confirmation modal,
  same online-aware behaviour (stop -> apply -> restart automatically
  if the server's up, via new `/mods/readd` and `/mods/readd-with-restart`
  routes).
- Seeded the tracking list with Spongie's Character Customisation
  (3414634809), already manually removed from `WorkshopItems=` before
  this feature existed.

### Changed

- Removed the Installed/Live timestamp columns from the Mod Manifest
  table and the mod detail modal -- hard to interpret, no real use.
  The underlying comparison logic in modcheck.py (which still needs
  both timestamps to detect updates) is unaffected, only the display
  changed.

## [2.14.2] - 2026-08-10

### Fixed

- Console page (`/console`) could freeze the whole browser tab
  ("Page Unresponsive") after a while of live streaming. Root cause:
  `renderConsole()` re-rendered the *entire* buffer (up to 3000 lines)
  from scratch on every single incoming SSE message, and the old
  `escapeHtml()` did a DOM round-trip (create a div, set textContent,
  read innerHTML) for every one of those lines, every time -- O(n)
  work per message, compounding toward O(n²) over a chatty log.
  Switched to appending only the new line per message (O(1)), a plain
  string-replace escape instead of the DOM round-trip, and a full
  rebuild only when actually needed (filter text changed, resuming
  from pause, or Clear). Unrelated to the earlier SSE shutdown-hang fix
  in 2.14.0 -- that was a server-side stall on restart, this was a
  client-side rendering cost during normal use.

## [2.14.1] - 2026-08-10

### Fixed

- `[B42] Simple Flashlight on Belt!` (ID `3394588830`) was showing
  twice on the Mod Manifest -- a real duplicate entry in `realm.ini`'s
  `WorkshopItems=` line (flagged back when this repo was first
  reviewed, before the panel existed). `get_configured_mod_ids()` now
  deduplicates while preserving order, so a literal duplicate in the
  file only shows once regardless of whether the underlying data gets
  cleaned up.

## [2.14.0] - 2026-08-09

### Fixed

- **The real cause of "restarts don't finish":** the `/console` SSE
  stream's infinite loop never checked for client disconnect or app
  shutdown, so any open browser tab on `/console` held the connection
  open and stalled every `systemctl restart pzpanel` by ~90s while
  uvicorn waited for it to close, until systemd force-killed it.
  Confirmed directly from journalctl showing the exact
  "Waiting for connections to close" -> "State 'stop-sigterm' timed
  out. Killing." sequence on three separate restarts. Fixed by checking
  `request.is_disconnected()` and a new shutdown-event flag on every
  poll; `pzpanel.service` also gets `TimeoutStopSec=10` as a backstop.
- Postponed restarts previously only got a short 30s->1 warning right
  before firing, even if the original 10-minute heads-up was hours
  earlier. Now they get the exact same full COUNTDOWN_SCHEDULE
  (10m/5m/1m/30s/10s/5-4-3-2-1) as a normal countdown, via a shared
  `_run_ticking_countdown()` helper used by both paths.
- Resuming the Watchdog while a restart was postponed didn't actually
  cancel the postponement (they shared a pause flag, but nothing told
  the already-sleeping process to stop) -- this is almost certainly
  what caused the reported "countdown continued after restart": Resume
  let a *second*, independent check cycle start while the *first*
  postponed restart was still quietly sleeping toward its own scheduled
  time, and the second one's countdown looked like a continuation of
  the first. `/automation/resume` now also sends an explicit cancel
  command when a postponement is pending. Added a `cancel` command
  end-to-end (`countdown_control.py` -> `mod_restart.py`) and an
  explicit "Cancel Postponement" button on the status page, rather than
  relying only on the implicit Resume behaviour.

### Changed

- Replaced the status page's `<meta http-equiv="refresh">` (full page
  reload every 5s) with `fetch()`-based polling against a new
  `/api/status-full` endpoint every 3s, patching specific DOM elements
  in place. Fixes both reported symptoms: the postpone modal no longer
  gets destroyed mid-selection, and the page no longer visibly
  "jumps"/flickers on a full reload.
- The postpone time picker's options are now fetched fresh from
  `/api/postpone-options` when the modal opens, instead of being baked
  into the page at initial load -- correct even if the page has been
  open for hours without a reload.

## [2.13.0] - 2026-08-08

### Added

- Pause now also disables the Watchdog for the duration (nothing should
  start a new check cycle while a restart is on hold), and sends an
  in-game reminder at the top of every hour while paused ("Mod-checker
  paused. Update(s) pending: X. Restart is on hold.") until Resumed.
  Removed a now-redundant is_paused() check inside the pause-wait loop
  that would have immediately self-cancelled once pause started setting
  that same flag.
- Removing a mod while the server is online no longer just errors --
  the modal now offers to stop, apply the removal, and restart
  automatically (`/mods/remove-with-restart`), or cancel (Cancel button
  or clicking the blurred backdrop, same as before).
- Mod names in the manifest are now clickable, opening a read-only
  detail modal (thumbnail, full description, installed/live dates) --
  same blurred-backdrop pattern as the remove confirmation, but no
  destructive action, just Close.

## [2.12.0] - 2026-08-08

### Added

- Countdown control, separate from the Watchdog on/off toggle -- lets
  the panel steer an *already-running* 10-minute restart countdown
  happening inside `mod_restart.py` (a different process), via new
  `countdown_control.py` and shared state/command files on disk (no
  other IPC exists between the two processes):
  - **Pause**: freezes the countdown at its current position, sends an
    in-game "Countdown paused at mm:ss" (or just "Ns" under a minute),
    remembers the exact remaining time.
  - **Resume**: continues the same countdown from where it was frozen.
  - **Postpone**: opens a time picker (hour-only, max 25 entries,
    starts at the next hour with >=10min lead time, wraps past
    midnight with a "(tomorrow)" suffix -- exact algorithm confirmed
    against three worked examples before building). Stands the
    current countdown down, sets the Watchdog pause flag for the
    duration, sleeps within the same process until the chosen time
    (rather than spinning up a new systemd timer -- see the
    `pzmodcheck.service` change below), fires a short final warning
    (30s->1) right before the restart actually happens, then restarts
    and re-enables the Watchdog automatically.
  - Status page shows live state for both an active countdown and a
    pending postponed restart, plus the new Pause/Postpone controls
    and the postpone time-picker modal.

### Changed

- `pzmodcheck.service` now sets `TimeoutStartSec=27h` so a postponed
  restart's long in-process sleep doesn't get killed by systemd's
  default oneshot start timeout. **Unverified** until a real
  postponement is tested end to end.

### Known limitations

- Manually clicking "Resume Watchdog" while a restart is postponed
  does NOT cancel the postponement -- they share the same pause flag,
  but the sleep loop doesn't watch for early cancellation once a
  postpone command has been accepted. The postponed restart will still
  fire at its scheduled time regardless. No cancel-postponement UI yet.
- The status page's 5s auto-refresh will close the postpone modal if
  left open that long without submitting -- picking a time is normally
  quick enough not to matter, but worth knowing.

## [2.11.0] - 2026-08-08

### Changed

- Action log "Showing the last N action(s)" now correctly pluralizes
  ("1 action" vs "9 actions").
- Console tail now colors the log level per line, matching ZCP's style:
  LOG stays default text color, WARN amber, ERROR rust-red. Switched
  rendering from `textContent` to escaped `innerHTML` to allow the
  per-token coloring while still safely escaping all log content first.

## [2.10.0] - 2026-08-08

### Added

- Live console tail page (`/console`) -- streams `server-console.txt` to
  the browser via Server-Sent Events, matching ZCP's live log viewer.
  Handles the file getting replaced wholesale on every server restart
  (same behaviour diZcord's own Tail class was built around) by
  watching the inode, not just appending forever. Client-side pause,
  clear, and text filter; scroll position is preserved unless already
  at the bottom.
- New `console_log` path in `pzpanel.ini`'s `[paths]` section.

### Changed

- In-game countdown messages: removed the "check for mod updates and
  restart your game too" line, replaced with "Get to safety" -- fits
  the game's theme better and is more actionable mid-session.
- Discord mod-restart announcement now correctly pluralizes
  ("1 mod" vs "3 mods" instead of always "N mod(s)").
- Renamed "Auto-Restart" to "Watchdog" throughout the status page UI
  (tag, button, note text) for clarity.

### Fixed

- `get_local_mod_ids()` (used by mod removal to clean up `Mods=`) wrapped
  its `open()` call in try/except but not the preceding `os.listdir()`,
  so a filesystem permission issue there crashed the whole request with
  an uncaught 500 instead of degrading gracefully. Both calls now share
  one try/except.
- Diagnosed (not a code fix -- an environment one): a real `/mods/remove`
  500 turned out to be `PermissionError` writing `realm.ini`, caused by
  incorrect file ownership on the server -- confirmed the same
  permission issue was also blocking the actual PZ server's own config
  writes (`ConfigFile.write> Exception thrown` in the game's own log).

## [2.9.0] - 2026-08-08

### Added

- Pause/Resume Auto-Restart toggle on the status page. Backed by
  `automation.py`, a plain marker file shared between the panel and
  `mod_restart.py` (separate processes). Pausing skips the entire next
  check cycle, and if paused mid-countdown, aborts within one step
  (checked between every countdown message) -- sends a stand-down
  message in-game and a "postponed" note to Discord instead of
  restarting, and logs the cancellation.
- `[server]` config section (`name`, `unit`) in `pzpanel.ini`. The
  server display name (header/footer/Discord messages) and the
  systemd unit actually being controlled were both hardcoded to
  "Realm"/`pzserver` in several places -- now read from config
  everywhere, so pointing this panel at a different server instance
  needs one config change, not a code change.
- Version number moved from the page header to a footer line
  (`pzpanel vX.Y.Z`), out of the way of the actual server name/status.

## [2.8.0] - 2026-08-08

### Added

- Remove-mod flow on the Mod Manifest page: a "Remove" trigger per row
  opens a blurred-backdrop confirmation modal with the mod's large
  thumbnail, title, ID, and description. Clicking the backdrop (outside
  the modal box) cancels, same as the Cancel button.
- Confirming strips the ID from `WorkshopItems=`. If the mod was
  actually downloaded, its real internal string ID(s) are also stripped
  from `Mods=` -- discovered by reading `mod.info` files from the
  mod's own downloaded content on disk (ground truth, not guessed),
  via new `modcheck.get_local_mod_ids()`.
- Requires the server to be offline first, same safety rule as adding a
  mod. Does NOT delete the downloaded Workshop content from disk --
  config-only removal; disk cleanup would be a separate, more
  destructive action.

## [2.7.1] - 2026-08-08

### Fixed

- Discord webhook posts from `mod_restart.py` were failing with
  `HTTP Error 403: Forbidden` on every real mod-sync restart overnight
  (confirmed via `journalctl -u pzmodcheck`). Root cause: no `User-Agent`
  header on the request -- Discord's edge servers reject requests with
  none. Added a `User-Agent` and centralized both Discord posting
  functions through one `_post_discord_payload()` helper that also logs
  the actual response body on failure instead of just the bare status
  code.

## [2.7.0] - 2026-08-08

### Added

- `actionlog.py`: append-only JSON-lines log of every start/stop/restart
  triggered through the panel or the mod-checker, with timestamp, actor
  (panel/modcheck), and a human-readable reason.
- `/log` page on the panel showing recent action history.
- `mod_restart.py` now posts one rich Discord embed per updated mod
  (title, Steam Workshop link, thumbnail, amber colour bar) instead of a
  single flat text line, and logs every automated restart with its
  triggering mod(s) as the reason.

## [2.6.1] - 2026-08-08

### Fixed

- `pzpanel.service` crashed on startup with
  `RuntimeError: Form data requires "python-multipart" to be installed`
  after the Add Mod flow introduced FastAPI's `Form(...)`.
  `python-multipart` added to `requirements.txt`.

## [2.6.0] - 2026-08-08

### Added

- Mod thumbnails on the Mod Manifest page, pulled from Steam's
  `preview_url` (already returned by the existing API call, just not
  used before now).
- Add Mod flow: `/mods/add` looks up a Workshop ID via Steam's API,
  shows a title/thumbnail/description preview, and on confirm appends
  the ID to `WorkshopItems=` in `realm.ini` (`modcheck.add_workshop_item`).
  Refuses to run while the server is online, to avoid editing `realm.ini`
  while PZ has it open. Does NOT touch `Mods=` -- the internal string ID
  PZ needs there isn't available from the Workshop API until the mod is
  actually downloaded.

### Fixed

- Mods not yet downloaded (listed in `WorkshopItems=` but absent from
  the ACF) were rendering a full sentence of explanation directly inside
  the status tag, breaking the table layout. Replaced with a short
  "Not installed" tag; full detail moved to a hover tooltip.

## [2.5.0] - 2026-08-08

### Changed

- Full visual redesign: "field-ops terminal" design system (Oswald +
  Inter + JetBrains Mono, warm near-black palette, pulsing LED status
  indicator, hazard-stripe accent, stamped status tags) replacing the
  initial generic dark-mode styling.

## [2.4.0] - 2026-08-07

### Added

- `mod_restart.py`: orchestrates a full mod-sync restart -- Discord
  announcement, RCON `servermsg` countdown (10m/5m/1m/30s/10s/5-4-3-2-1)
  to connected players, then a graceful restart. Content fetch is
  deliberately NOT done here; confirmed via real-world testing (a manual
  restart auto-updated a pending mod) that PZ's own built-in Workshop
  auto-download on startup handles it, no `steamcmd` call needed.
- `pzmodcheck.service` + `pzmodcheck.timer`: runs the check automatically
  every 15 minutes.

## [2.3.0] - 2026-08-06

### Added

- `steam_workshop.py`: Steam Workshop API client (stdlib `urllib`, no
  dependency) for live mod title/`time_updated`/thumbnail lookups.
- `acf.py`: minimal VDF/KeyValues parser for reading SteamCMD's
  `appworkshop_108600.acf`, the ground truth for what's actually
  installed on disk.
- `modcheck.py`: diffs `realm.ini`'s `WorkshopItems=` (what should be
  installed) against the ACF (what is) against the live Steam API (what
  currently exists), never hardcoding the mod ID list.
- `/mods` page rendering the check results.

## [2.2.0] - 2026-08-06

### Changed

- Server status now distinguishes Online from Starting by actually
  attempting an RCON auth, not just checking `systemctl is-active` --
  systemd reports "active" the instant the Java process launches, long
  before the world/network stack is ready.
- Status label renamed "inactive" -> "Offline"; button highlighting
  (Start/Restart/Stop) now reflects real state.

## [2.1.1] - 2026-08-05

### Fixed

- FastAPI's own subprocess timeout on Stop/Restart was 15s, but a real
  graceful shutdown (RCON quit + world save) can take a minute or more --
  caused a 500 error even though the underlying `systemctl` command was
  still working correctly. Timeouts now scaled per action (20s start,
  150s stop/restart, comfortably above `pzserver.service`'s
  `TimeoutStopSec=120`).

## [2.1.0] - 2026-08-05

### Added

- Start/Stop/Restart buttons on the panel, backed by a scoped
  `sudoers.d` rule allowing the `pzserver` user password-less
  `systemctl start/stop/restart/status pzserver`.

## [2.0.0] - 2026-08-05

### Added

- `rcon.py`: minimal Source RCON protocol client (stdlib `socket` +
  `struct`, no dependency), verified against the real server (`players`,
  `servermsg`, `quit`).
- `graceful_stop.py`: `pzserver.service`'s `ExecStop=` wrapper -- sends
  RCON `quit` and waits for the process to actually exit before
  returning, so systemd's follow-up kill signal never lands mid-save.
- `pzserver.service` (`Restart=on-success`) and `pzpanel.service` systemd
  units.
- `main.py` v1: read-only FastAPI status page.
