require "pzp_Shared"

pzp.Client = pzp.Client or {}

local lastKillCount = -1
local lastUpdateTime = 0
local lastHourSent = -1

-- Minimum real-world seconds between sends (backstop only).
-- In-game hour boundaries trigger their own send regardless of this.
local UPDATE_INTERVAL = 60

local function sendKillUpdate(player, isDeath)
    if not player or not player:isLocalPlayer() then
        return
    end

    local kills = player:getZombieKills()
    local hoursSurvived = player:getHoursSurvived()
    local steamID = tostring(player:getSteamID())

    local args = {
        kills = kills,
        hoursSurvived = hoursSurvived,
        steamID = steamID,
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
    lastHourSent = math.floor(hoursSurvived)
end

local function onPlayerUpdate(player)
    if not player or not player:isLocalPlayer() then
        return
    end

    local currentTime = getTimestamp()
    local kills = player:getZombieKills()
    local hoursSurvived = player:getHoursSurvived()
    local currentHour = math.floor(hoursSurvived)

    -- Fire immediately on a new in-game hour, regardless of real time elapsed.
    if currentHour > lastHourSent then
        sendKillUpdate(player, false)
        return
    end

    -- Otherwise respect the real-time interval.
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

local function onGameStart()
    local player = getSpecificPlayer(0)
    if player then
        sendKillUpdate(player, false)
    end
end

Events.OnGameStart.Add(onGameStart)
Events.OnPlayerUpdate.Add(onPlayerUpdate)

print("[pzp] Client tracker loaded")
