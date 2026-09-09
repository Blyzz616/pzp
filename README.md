# PZ Panel

A web-based control panel for a **Project Zomboid B42 dedicated server**. Manage your server, mods, and players from a browser — on Linux or Windows.

## Features

- **Status page** — start, stop, restart with live status indicator
- **Config page** — edit your server's `.ini` settings from the browser (no more SSH for tweaks)
- **Mod Manifest** — view installed mods, check for Workshop updates, add/remove/reorder mods
- **Killboard** — per-player kill tracking, grouped by Steam account, backed by SQLite
- **Console** — live tail of `server-console.txt` with filtering
- **Discord integration** — join/leave/death embeds and mod-update restart announcements
- **Mod-update auto-restart** — scheduled countdown with pause/postpone/cancel controls
- **Action log** — audit trail of everything the panel has done

---

## Requirements

| | Linux | Windows |
|---|---|---|
| Python | 3.10+ | 3.10+ |
| PZ server | Running under systemd | Running as a service or bare process |
| SteamCMD | Installed alongside PZ server | Installed alongside PZ server |

---

## Linux Installation

These instructions assume your PZ server runs as user `pzserver` under systemd, with server files under `/home/pzserver/`. Adjust paths to match your setup.

### 1. Download pzpanel

```bash
cd /opt
sudo git clone https://github.com/Blyzz616/pzpanel.git pzp
sudo chown -R pzserver:pzserver /opt/pzp
```

### 2. Install Python dependencies

```bash
sudo -u pzserver bash
cd /opt/pzp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure pzpanel

Copy the example config and edit it:

```bash
cp /opt/pzp/pzpanel.ini.example /opt/pzp/pzpanel.ini
nano /opt/pzp/pzpanel.ini
```

At minimum, set these values:

```ini
[rcon]
password = your_rcon_password_here

[paths]
server_ini    = /home/pzserver/Zomboid/Server/realm.ini
workshop_acf  = /home/pzserver/pzserver/steamapps/workshop/appworkshop_108600.acf
console_log   = /home/pzserver/Zomboid/server-console.txt

[server]
name = Realm
unit = pzserver
```

Use the **Settings page** (cog icon) in the browser to configure optional features (Discord, Steam enrichment, kill tracking) after the panel is running. The **Find** and **Derive** buttons on the Settings page can locate your paths automatically if you're unsure.

### 4. Set up the systemd service

Copy the included service file and enable it:

```bash
sudo cp /opt/pzp/pzpanel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pzpanel
sudo systemctl start pzpanel
```

The panel will be available at **http://your-server-ip:8000**.

### 5. (Optional) Set up graceful server stop

The included `pzserver.service` uses `graceful_stop.py` as its `ExecStop` handler, which sends an RCON `quit` command (saving the world) before systemd kills the process. To use it:

```bash
sudo cp /opt/pzp/pzserver.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Edit `/etc/systemd/system/pzserver.service` to match your PZ server's actual install path and user before enabling it.

### 6. (Optional) Set up mod-update auto-checker

The mod checker runs on a timer and restarts the server when Workshop updates are available, with a countdown warning to online players.

```bash
sudo cp /opt/pzp/pzmodcheck.service /etc/systemd/system/
sudo cp /opt/pzp/pzmodcheck.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pzmodcheck.timer
```

### Updating pzpanel (Linux)

```bash
cd /opt/pzp
sudo -u pzserver git pull
sudo -u pzserver bash -c "source venv/bin/activate && pip install -r requirements.txt"
sudo chown -R pzserver:pzserver /opt/pzp/*
sudo systemctl restart pzpanel
```

---

## Windows Installation

### 1. Install Python

Download and install Python 3.10 or newer from [python.org](https://www.python.org/downloads/).

> **Important:** On the installer's first screen, tick **"Add Python to PATH"** before clicking Install.

### 2. Download pzpanel

Either clone with Git:

```
git clone https://github.com/Blyzz616/pzpanel.git pzpanel
```

Or download and extract the ZIP from GitHub (green **Code** button → **Download ZIP**).

### 3. Run the launcher

Double-click **`pzpanel_windows.bat`** inside the pzpanel folder.

On first run it will:
- Check your Python version
- Create a virtual environment
- Install all dependencies
- Copy `pzpanel.ini.example` to `pzpanel.ini` and pause so you can edit it

### 4. Configure pzpanel

Open `pzpanel.ini` in any text editor (Notepad works). At minimum, set:

```ini
[rcon]
password = your_rcon_password_here

[paths]
server_ini   = C:\Users\YourName\Zomboid\Server\realm.ini
workshop_acf = C:\steamcmd\steamapps\workshop\appworkshop_108600.acf
console_log  = C:\Users\YourName\Zomboid\server-console.txt

[server]
name = Realm
; Choose one:
windows_mode = sc       ; if your PZ server runs as a Windows service
; windows_mode = process  ; if you launch PZ server from a batch file

; process mode only -- path to your server's start script:
; windows_start_script = C:\pzserver\StartServer64.bat
```

**Finding your paths:**
- `server_ini` — look in `%USERPROFILE%\Zomboid\Server\` for a `.ini` file named after your world
- `workshop_acf` — look in your SteamCMD folder under `steamapps\workshop\appworkshop_108600.acf`
- `console_log` — look in `%USERPROFILE%\Zomboid\server-console.txt`

After the panel is running, the **Settings page** (cog icon) has **Find** buttons that can locate these automatically.

### 5. Choose a server control mode

**`windows_mode = sc` (Windows Service — recommended for always-on servers)**

Your PZ server must be registered as a Windows Service. The most common tool for this is [NSSM](https://nssm.cc/) (Non-Sucking Service Manager):

```
nssm install pzserver "C:\pzserver\StartServer64.bat"
nssm start pzserver
```

Set `unit = pzserver` in `pzpanel.ini` to match whatever name you gave the service.

**`windows_mode = process` (bare process — simpler, no service setup)**

pzpanel will launch and track the server process itself. Set `windows_start_script` to the path of your server's `.bat` or `.exe` start file. No other setup needed.

### 6. Start the panel

Run `pzpanel_windows.bat` again (or keep the window from step 3 open). The panel will be available at **http://localhost:8000**.

To access the panel from other devices on your network, use your PC's local IP address instead of `localhost`.

### Running pzpanel at startup (Windows)

To have pzpanel start automatically when Windows boots:

1. Press `Win + R`, type `taskschd.msc`, press Enter
2. Click **Create Basic Task**
3. Name it `pzpanel`, click Next
4. Trigger: **When the computer starts**, click Next
5. Action: **Start a program**
6. Program: browse to `pzpanel_windows.bat` in your pzpanel folder
7. **Start in:** the pzpanel folder path (e.g. `C:\pzpanel`)
8. Finish, then right-click the task → **Properties** → **Run whether user is logged on or not**

### Updating pzpanel (Windows)

If you cloned with Git:
```
cd pzpanel
git pull
pzpanel_windows.bat
```

If you downloaded the ZIP, re-download and extract, then copy your `pzpanel.ini` back in.

---

## Kill Tracking (optional)

The Killboard page shows per-player kill counts grouped by Steam account. This requires the **PZP Lua mod** installed on your server.

**Workshop ID: [3793020885](https://steamcommunity.com/sharedfiles/filedetails/?id=3793020885)**

Add it to your server via the Mod Manifest page, then set the kills file path in Settings:

```ini
[player_events]
; Linux:
kills_file = /home/pzserver/Zomboid/Lua/pzp_player_kills.txt
; Windows:
; kills_file = C:\Users\YourName\Zomboid\Lua\pzp_player_kills.txt
```

Restart the panel after adding this setting.

---

## Discord Integration (optional)

Create a webhook in your Discord server (**Server Settings → Integrations → Webhooks**) and paste the URL into Settings → Discord Webhook URL, or directly into `pzpanel.ini`:

```ini
[discord]
webhook_url = https://discord.com/api/webhooks/...
```

Restart the panel for this to take effect. The panel will then post join/leave/death embeds and mod-update restart announcements to your Discord channel.

For richer join embeds (Steam persona, avatar, hours played), also add a Steam Web API key — free at [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey):

```ini
[steam]
api_key = your_steam_api_key_here
```

---

## Accessing the panel remotely

The panel binds to port **8000** by default. To access it from outside your local network:

- Open port 8000 on your firewall/router (TCP)
- Navigate to `http://your-server-ip:8000`

There is currently no built-in authentication — it is recommended to restrict access via firewall rules or a reverse proxy (nginx/Caddy) if your server is publicly accessible.

---

## Troubleshooting

**Panel won't start**
- Check that `pzpanel.ini` exists and has valid RCON credentials
- On Linux: `sudo journalctl -u pzpanel -n 50`
- On Windows: run `pzpanel_windows.bat` from a Command Prompt to see error output

**"Could not check mods" error**
- Verify `workshop_acf` points to the correct `appworkshop_108600.acf` file
- Verify `server_ini` points to your world's `.ini` file
- Use the **Find** buttons on the Settings page to locate them automatically

**Server status shows STARTING but never goes ONLINE**
- Check that your RCON port and password in `pzpanel.ini` match what's in your server's `.ini`
- The server can take 2–5 minutes to fully load on first world generation

**Kill tracking not updating**
- Confirm the PZP Lua mod (Workshop ID `3793020885`) is installed and loaded
- Confirm `kills_file` in `pzpanel.ini` points to the correct path
- The mod updates its file roughly every 60 seconds — counts may lag by up to that long

**Windows: server won't start in `sc` mode**
- Open Services (`services.msc`) and confirm the service name matches `unit =` in `pzpanel.ini`
- Check that the account running pzpanel has permission to control the service

**Windows: server won't start in `process` mode**
- Confirm `windows_start_script` points to a valid `.bat` or `.exe` file
- Try running that script manually first to confirm it works on its own

---

## File overview

| File | Purpose |
|---|---|
| `main.py` | FastAPI web panel — all pages and API routes |
| `platform_compat.py` | Cross-platform OS abstraction (start/stop/status) |
| `discord_module.py` | Player event watcher + Discord embeds |
| `player_db.py` | SQLite player/kill tracking database |
| `mod_restart.py` | Mod-update auto-restart with countdown |
| `modcheck.py` | Workshop update checker |
| `graceful_stop.py` | Linux ExecStop wrapper (RCON quit before kill) |
| `pzpanel.service` | systemd unit for pzpanel (Linux) |
| `pzserver.service` | systemd unit for PZ server with graceful stop (Linux) |
| `pzmodcheck.service/.timer` | systemd timer for mod-update checks (Linux) |
| `pzpanel_windows.bat` | Windows launcher (setup + start) |
| `pzpanel.ini.example` | Annotated config template |

---

## Version

**v4.0.0** — Cross-platform release (Linux + Windows)

See `CHANGELOG.md` for full history.
