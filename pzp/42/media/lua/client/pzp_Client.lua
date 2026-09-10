require "pzp_Shared"

pzp.Client = pzp.Client or {}

local lastKillCount = -1
local lastUpdateTime = 0

-- How often, in real seconds, to send the kill count to the server.
local UPDATE_INTERVAL = 60

local function sendKillUpdate(player, isDeath)
    if not player or not player:isLocalPlayer() then
        return
    end

    local kills = player:getZombieKills()

    local args = {
        kills = kills,
        isDeath = isDeath or false
    }

    if isDeath then
        sendClientCommand(
            player,
            pzp.Module,
            pzp.Commands.PlayerDied,
            args
        )
    else
        sendClientCommand(
            player,
            pzp.Module,
            pzp.Commands.UpdateKills,
            args
        )
    end

    lastKillCount = kills
    lastUpdateTime = getTimestamp()
end

local function onPlayerUpdate(player)
    if not player or not player:isLocalPlayer() then
        return
    end

    local currentTime = getTimestamp()
    local kills = player:getZombieKills()

    -- Don't send continuously.
    if currentTime - lastUpdateTime < UPDATE_INTERVAL then
        return
    end

    -- Only send periodic updates if the kill count changed.
    if kills ~= lastKillCount then
        sendKillUpdate(player, false)
    else
        lastUpdateTime = currentTime    
    end
end

Events.OnPlayerUpdate.Add(onPlayerUpdate)

print("[pzp] Client tracker loaded")
