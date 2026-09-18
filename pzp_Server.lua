require "pzp_Shared"

pzp.Server = pzp.Server or {}

local DATA_FILE = "pzp_player_kills.yaml"

-- In-memory database.
-- Structure: playerData[steamID] = {
--     characters = {
--         [username] = { kills=N, alive=bool, survived=N }
--     },
--     lifetimeKills = N   (sum of all characters' kills, recomputed on every save)
-- }
local playerData = {}


-- ------------------------------------------------------------
-- YAML HELPERS
-- ------------------------------------------------------------

-- Escape a string for YAML (single-quoted, escaping single quotes by doubling).
local function yamlStr(s)
    s = tostring(s)
    return "'" .. s:gsub("'", "''") .. "'"
end

-- Write playerData as YAML.
-- Format:
--   "steamid":
--     lifetimeKills: N
--     "username":
--       kills: N
--       alive: true/false
--       survived: N
local function yamlEncode(data)
    local lines = {}
    -- Sort steamIDs for deterministic output.
    local steamIDs = {}
    for sid in pairs(data) do steamIDs[#steamIDs + 1] = sid end
    table.sort(steamIDs)

    for _, steamID in ipairs(steamIDs) do
        local entry = data[steamID]
        lines[#lines + 1] = yamlStr(steamID) .. ":"
        lines[#lines + 1] = "  lifetimeKills: " .. tostring(entry.lifetimeKills or 0)

        -- Sort usernames for deterministic output.
        local usernames = {}
        for uname in pairs(entry.characters) do usernames[#usernames + 1] = uname end
        table.sort(usernames)

        for _, uname in ipairs(usernames) do
            local ch = entry.characters[uname]
            lines[#lines + 1] = "  " .. yamlStr(uname) .. ":"
            lines[#lines + 1] = "    kills: " .. tostring(ch.kills or 0)
            lines[#lines + 1] = "    alive: " .. (ch.alive and "true" or "false")
            lines[#lines + 1] = "    survived: " .. tostring(ch.survived or 0)
        end
    end

    return table.concat(lines, "\n") .. "\n"
end

-- Minimal YAML parser for the subset we write.
-- Returns: { [steamid] = { lifetimeKills=N, characters={ [uname]={kills,alive,survived} } } }
local function yamlDecode(s)
    local result = {}
    local currentSteamID = nil
    local currentUsername = nil

    for line in (s .. "\n"):gmatch("([^\n]*)\n") do
        -- Strip trailing whitespace
        local trimmed = line:match("^(.-)%s*$")
        if trimmed == "" then
            -- skip blank lines
        elseif trimmed:match("^'(.+)':%s*$") then
            -- top-level steamid key: 'steamid':
            local sid = trimmed:match("^'(.*)':%s*$")
            if sid then
                currentSteamID = sid
                currentUsername = nil
                result[currentSteamID] = { lifetimeKills = 0, characters = {} }
            end
        elseif trimmed:match("^  lifetimeKills:%s*(.+)$") then
            local val = trimmed:match("^  lifetimeKills:%s*(.+)$")
            if currentSteamID then
                result[currentSteamID].lifetimeKills = tonumber(val) or 0
            end
        elseif trimmed:match("^  '(.+)':%s*$") then
            -- username key under steamid: '  username':
            local uname = trimmed:match("^  '(.*)':%s*$")
            if uname and currentSteamID then
                currentUsername = uname
                result[currentSteamID].characters[currentUsername] = {
                    kills = 0, alive = false, survived = 0
                }
            end
        elseif trimmed:match("^    kills:%s*(.+)$") then
            local val = trimmed:match("^    kills:%s*(.+)$")
            if currentSteamID and currentUsername then
                result[currentSteamID].characters[currentUsername].kills = tonumber(val) or 0
            end
        elseif trimmed:match("^    alive:%s*(.+)$") then
            local val = trimmed:match("^    alive:%s*(.+)$")
            if currentSteamID and currentUsername then
                result[currentSteamID].characters[currentUsername].alive = (val == "true")
            end
        elseif trimmed:match("^    survived:%s*(.+)$") then
            local val = trimmed:match("^    survived:%s*(.+)$")
            if currentSteamID and currentUsername then
                result[currentSteamID].characters[currentUsername].survived = tonumber(val) or 0
            end
        end
    end

    return result
end


-- ------------------------------------------------------------
-- LOAD DATA
-- ------------------------------------------------------------

local function loadData()
    local file = getFileReader(DATA_FILE, false)
    if not file then
        print("[pzp] No existing kill data file found")
        return
    end

    local lines = {}
    local line = file:readLine()
    while line do
        lines[#lines + 1] = line
        line = file:readLine()
    end
    file:close()

    local raw = table.concat(lines, "\n")
    if raw == "" then return end

    local ok, decoded = pcall(yamlDecode, raw)
    if not ok or type(decoded) ~= "table" then
        print("[pzp] WARNING: Could not parse kill data YAML, starting fresh")
        return
    end

    for steamID, entry in pairs(decoded) do
        playerData[steamID] = {
            characters    = entry.characters or {},
            lifetimeKills = entry.lifetimeKills or 0,
        }
    end

    print("[pzp] Loaded kill data for players")
end


-- ------------------------------------------------------------
-- SAVE DATA
-- ------------------------------------------------------------

local function saveData()
    -- Recompute lifetimeKills as sum of all characters' kills.
    for steamID, entry in pairs(playerData) do
        local total = 0
        for _, ch in pairs(entry.characters) do
            total = total + (ch.kills or 0)
        end
        entry.lifetimeKills = total
    end

    local file = getFileWriter(DATA_FILE, true, false)
    if not file then
        print("[pzp] ERROR: Could not open kill data file for writing")
        return
    end

    file:write(yamlEncode(playerData))
    file:close()

    print("[pzp] Kill data saved")
end


-- ------------------------------------------------------------
-- UPDATE PLAYER
-- ------------------------------------------------------------

local function updatePlayerKills(player, kills, hoursSurvived, steamID, isDeath)
    local username = tostring(player:getUsername())

    -- Fallback: server-side steamID lookup if client didn't send one.
    if not steamID or steamID == "" or steamID == "0" then
        steamID = tostring(player:getSteamID and player:getSteamID() or "unknown")
    end

    if not playerData[steamID] then
        playerData[steamID] = { characters = {}, lifetimeKills = 0 }
        print("[pzp] Created new steam account record: " .. steamID)
    end

    local entry = playerData[steamID]

    -- Only one character alive at a time per account.
    for uname, ch in pairs(entry.characters) do
        if uname ~= username then
            ch.alive = false
        end
    end

    if not entry.characters[username] then
        entry.characters[username] = { kills = 0, alive = true, survived = 0 }
        print("[pzp] Created new character record: " .. username .. " (" .. steamID .. ")")
    end

    local ch = entry.characters[username]
    ch.kills    = tonumber(kills) or 0
    ch.survived = tonumber(hoursSurvived) or 0
    ch.alive    = not isDeath

    print(
        "[pzp] Updated " .. username ..
        " (" .. steamID .. ")" ..
        " -> " .. tostring(ch.kills) .. " kills" ..
        ", " .. tostring(ch.survived) .. " hours survived" ..
        ", alive=" .. tostring(ch.alive)
    )

    saveData()
end


-- ------------------------------------------------------------
-- CLIENT COMMAND HANDLER
-- ------------------------------------------------------------

local function onClientCommand(module, command, player, args)
    if module ~= pzp.Module then return end

    if command == pzp.Commands.UpdateKills then
        print("[pzp] ================================")
        print("[pzp] Kill update received")
        print("[pzp] Username: " .. tostring(player:getUsername()))
        print("[pzp] SteamID: "  .. tostring(args.steamID or "?"))
        print("[pzp] Kills: "    .. tostring(args.kills))
        print("[pzp] Hours: "    .. tostring(args.hoursSurvived or 0))
        updatePlayerKills(player, args.kills, args.hoursSurvived, args.steamID, false)
        print("[pzp] ================================")

    elseif command == pzp.Commands.PlayerDied then
        print("[pzp] ================================")
        print("[pzp] Death update received")
        print("[pzp] Username: " .. tostring(player:getUsername()))
        print("[pzp] SteamID: "  .. tostring(args.steamID or "?"))
        print("[pzp] Kills: "    .. tostring(args.kills))
        print("[pzp] Hours: "    .. tostring(args.hoursSurvived or 0))
        updatePlayerKills(player, args.kills, args.hoursSurvived, args.steamID, true)
        print("[pzp] ================================")
    end
end


-- ------------------------------------------------------------
-- INITIALIZATION
-- ------------------------------------------------------------

loadData()

Events.OnClientCommand.Add(onClientCommand)

print("[pzp] Server tracker loaded")
