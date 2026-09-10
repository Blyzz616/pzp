require "pzp_Shared"

pzp.Server = pzp.Server or {}

local DATA_FILE = "pzp_player_kills.txt"

-- In-memory player database.
local playerData = {}

-- ------------------------------------------------------------
-- LOAD DATA
-- ------------------------------------------------------------

local function loadData()

    local file = getFileReader(DATA_FILE, false)

    if not file then
        print("[pzp] No existing kill data file found")
        return
    end

    local line = file:readLine()

    while line do

        local username, kills = string.match(
            line,
            "^([^|]+)|([^|]+)$"
        )

        if username and kills then

            playerData[username] = {
                username = username,
                kills = tonumber(kills) or 0
            }

        end

        line = file:readLine()
    end

    file:close()

    print("[pzp] Loaded kill data for players")
end


-- ------------------------------------------------------------
-- SAVE DATA
-- ------------------------------------------------------------

local function saveData()

    local file = getFileWriter(DATA_FILE, true, false)

    if not file then
        print("[pzp] ERROR: Could not open kill data file")
        return
    end

    for username, data in pairs(playerData) do

        local line =
            tostring(data.username) ..
            "|" ..
            tostring(data.kills)

        file:write(line)
        file:write("\n")

    end

    file:close()

    print("[pzp] Kill data saved")
end


-- ------------------------------------------------------------
-- UPDATE PLAYER
-- ------------------------------------------------------------

local function updatePlayerKills(player, kills)

    local username = tostring(player:getUsername())

    if not playerData[username] then

        playerData[username] = {
            username = username,
            kills = 0
        }

        print("[pzp] Created new player record: " .. username)

    end

    playerData[username].kills = tonumber(kills) or 0

    print(
        "[pzp] Updated " ..
        username ..
        " -> " ..
        tostring(playerData[username].kills) ..
        " kills"
    )

    saveData()
end


-- ------------------------------------------------------------
-- CLIENT COMMAND HANDLER
-- ------------------------------------------------------------

local function onClientCommand(module, command, player, args)

    if module ~= pzp.Module then
        return
    end

    if command == pzp.Commands.UpdateKills then

        print("[pzp] ================================")
        print("[pzp] Kill update received")
        print("[pzp] Username: " .. tostring(player:getUsername()))
        print("[pzp] Kills received: " .. tostring(args.kills))

        updatePlayerKills(player, args.kills)

        print("[pzp] ================================")

    elseif command == pzp.Commands.PlayerDied then

        print("[pzp] ================================")
        print("[pzp] Death update received")
        print("[pzp] Username: " .. tostring(player:getUsername()))
        print("[pzp] Kills received: " .. tostring(args.kills))

        updatePlayerKills(player, args.kills)

        print("[pzp] ================================")

    end
end


-- ------------------------------------------------------------
-- INITIALIZATION
-- ------------------------------------------------------------

loadData()

Events.OnClientCommand.Add(onClientCommand)

print("[pzp] Server tracker loaded")
