require "pzp_Shared"

pzp.Server = pzp.Server or {}

-- Append-only event log read by kill_tracker.py on the panel side.
-- One line per event: steamid|username|kills|survived|isDeath(0/1)
local EVENT_FILE = "pzp_events.log"

local function appendEvent(steamID, username, kills, hoursSurvived, isDeath)
    local file = getFileWriter(EVENT_FILE, false, false)
    if not file then
        print("[pzp] ERROR: Could not open event log for writing")
        return
    end
    local line = tostring(steamID) .. "|" ..
                 tostring(username) .. "|" ..
                 tostring(tonumber(kills) or 0) .. "|" ..
                 tostring(tonumber(hoursSurvived) or 0) .. "|" ..
                 (isDeath and "1" or "0")
    file:write(line .. "\n")
    file:close()
    print("[pzp] Event logged: " .. line)
end

local function onClientCommand(module, command, player, args)
    if module ~= pzp.Module then return end

    local username      = tostring(player:getUsername())
    local steamID       = tostring(args.steamID or "")
    local kills         = args.kills or 0
    local hoursSurvived = args.hoursSurvived or 0

    -- Fallback: server-side steamID lookup if client didn't send one.
    if steamID == "" or steamID == "0" then
        steamID = tostring(player:getSteamID and player:getSteamID() or "unknown")
    end

    if command == pzp.Commands.UpdateKills then
        print("[pzp] ================================")
        print("[pzp] Kill update | " .. username .. " (" .. steamID .. ")")
        print("[pzp] Kills: " .. tostring(kills) .. " | Hours: " .. tostring(hoursSurvived))
        appendEvent(steamID, username, kills, hoursSurvived, false)
        print("[pzp] ================================")

    elseif command == pzp.Commands.PlayerDied then
        print("[pzp] ================================")
        print("[pzp] Death event | " .. username .. " (" .. steamID .. ")")
        print("[pzp] Kills: " .. tostring(kills) .. " | Hours: " .. tostring(hoursSurvived))
        appendEvent(steamID, username, kills, hoursSurvived, true)
        print("[pzp] ================================")
    end
end

Events.OnClientCommand.Add(onClientCommand)

print("[pzp] Server tracker loaded")
