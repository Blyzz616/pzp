require "pzp_Shared"

pzp.Server = pzp.Server or {}

local DATA_FILE = "pzp_player_kills.json"

-- In-memory database.
-- Structure: playerData[steamID] = {
--     characters = {
--         [username] = { kills=N, alive=bool, survived=N }
--     },
--     lifetimeKills = N
-- }
local playerData = {}


-- ------------------------------------------------------------
-- JSON HELPERS
-- ------------------------------------------------------------

-- Minimal JSON serialiser (numbers, strings, booleans, tables).
local function jsonEncode(val, indent, level)
    indent = indent or "  "
    level = level or 0
    local t = type(val)
    if t == "boolean" then
        return val and "true" or "false"
    elseif t == "number" then
        return tostring(val)
    elseif t == "string" then
        return '"' .. val:gsub('\\', '\\\\'):gsub('"', '\\"') .. '"'
    elseif t == "table" then
        local pad = string.rep(indent, level)
        local inner = string.rep(indent, level + 1)
        local keys = {}
        for k in pairs(val) do keys[#keys + 1] = k end
        table.sort(keys, function(a, b)
            return tostring(a) < tostring(b)
        end)
        local parts = {}
        for _, k in ipairs(keys) do
            local v = val[k]
            parts[#parts + 1] = inner .. '"' .. tostring(k) .. '": ' ..
                jsonEncode(v, indent, level + 1)
        end
        if #parts == 0 then return "{}" end
        return "{\n" .. table.concat(parts, ",\n") .. "\n" .. pad .. "}"
    end
    return "null"
end

-- Minimal JSON parser (handles the subset we write ourselves).
local function jsonDecode(s)
    local pos = 1

    local function skipWS()
        while pos <= #s and s:sub(pos, pos):match("%s") do
            pos = pos + 1
        end
    end

    local parseValue

    local function parseString()
        pos = pos + 1 -- skip opening "
        local result = {}
        while pos <= #s do
            local c = s:sub(pos, pos)
            if c == '"' then
                pos = pos + 1
                return table.concat(result)
            elseif c == '\\' then
                pos = pos + 1
                local e = s:sub(pos, pos)
                if e == '"' then result[#result+1] = '"'
                elseif e == '\\' then result[#result+1] = '\\'
                else result[#result+1] = e end
            else
                result[#result+1] = c
            end
            pos = pos + 1
        end
        return table.concat(result)
    end

    local function parseObject()
        pos = pos + 1 -- skip {
        local obj = {}
        skipWS()
        if s:sub(pos, pos) == "}" then pos = pos + 1; return obj end
        while pos <= #s do
            skipWS()
            local key = parseString()
            skipWS()
            pos = pos + 1 -- skip :
            skipWS()
            obj[key] = parseValue()
            skipWS()
            local c = s:sub(pos, pos)
            if c == "}" then pos = pos + 1; break end
            if c == "," then pos = pos + 1 end
        end
        return obj
    end

    parseValue = function()
        skipWS()
        local c = s:sub(pos, pos)
        if c == '"' then
            return parseString()
        elseif c == '{' then
            return parseObject()
        elseif c == 't' then
            pos = pos + 4; return true
        elseif c == 'f' then
            pos = pos + 5; return false
        elseif c == 'n' then
            pos = pos + 4; return nil
        else
            -- number
            local num = s:match("^-?%d+%.?%d*[eE]?[+-]?%d*", pos)
            if num then pos = pos + #num; return tonumber(num) end
        end
    end

    skipWS()
    return parseValue()
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

    local ok, decoded = pcall(jsonDecode, raw)
    if not ok or type(decoded) ~= "table" then
        print("[pzp] WARNING: Could not parse kill data JSON, starting fresh")
        return
    end

    for steamID, entry in pairs(decoded) do
        if type(entry) == "table" then
            local chars = {}
            for k, v in pairs(entry) do
                if k ~= "lifetimeKills" and type(v) == "table" then
                    chars[k] = {
                        kills     = tonumber(v.kills)    or 0,
                        alive     = v.alive == true,
                        survived  = tonumber(v.survived) or 0,
                    }
                end
            end
            playerData[steamID] = {
                characters   = chars,
                lifetimeKills = tonumber(entry.lifetimeKills) or 0,
            }
        end
    end

    print("[pzp] Loaded kill data for players")
end


-- ------------------------------------------------------------
-- SAVE DATA
-- ------------------------------------------------------------

local function saveData()
    -- Rebuild lifetime kills from all characters before saving.
    for steamID, entry in pairs(playerData) do
        local total = 0
        for _, ch in pairs(entry.characters) do
            total = total + (ch.kills or 0)
        end
        entry.lifetimeKills = total
    end

    -- Build the table to serialise.
    local out = {}
    for steamID, entry in pairs(playerData) do
        local row = { lifetimeKills = entry.lifetimeKills }
        for username, ch in pairs(entry.characters) do
            row[username] = {
                kills    = ch.kills,
                alive    = ch.alive,
                survived = ch.survived,
            }
        end
        out[steamID] = row
    end

    local file = getFileWriter(DATA_FILE, true, false)
    if not file then
        print("[pzp] ERROR: Could not open kill data file for writing")
        return
    end

    file:write(jsonEncode(out))
    file:write("\n")
    file:close()

    print("[pzp] Kill data saved")
end


-- ------------------------------------------------------------
-- UPDATE PLAYER
-- ------------------------------------------------------------

local function updatePlayerKills(player, kills, hoursSurvived, steamID, isDeath)
    local username = tostring(player:getUsername())

    -- Fallback: if client didn't send steamID use server-side lookup.
    if not steamID or steamID == "" or steamID == "0" then
        steamID = tostring(player:getSteamID and player:getSteamID() or "unknown")
    end

    if not playerData[steamID] then
        playerData[steamID] = { characters = {}, lifetimeKills = 0 }
        print("[pzp] Created new steam account record: " .. steamID)
    end

    local entry = playerData[steamID]

    -- Mark all other characters for this steam account as not alive.
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
        print("[pzp] SteamID: " .. tostring(args.steamID or "?"))
        print("[pzp] Kills received: " .. tostring(args.kills))
        print("[pzp] Hours survived: " .. tostring(args.hoursSurvived or 0))

        updatePlayerKills(player, args.kills, args.hoursSurvived, args.steamID, false)

        print("[pzp] ================================")

    elseif command == pzp.Commands.PlayerDied then
        print("[pzp] ================================")
        print("[pzp] Death update received")
        print("[pzp] Username: " .. tostring(player:getUsername()))
        print("[pzp] SteamID: " .. tostring(args.steamID or "?"))
        print("[pzp] Kills received: " .. tostring(args.kills))
        print("[pzp] Hours survived: " .. tostring(args.hoursSurvived or 0))

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
