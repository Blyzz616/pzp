-- pzp kill tracker v5.0.0 (companion mod for PZ Panel)
--
-- Reports the local character's zombie kills and in-game hours survived
-- to the server:
--   Snapshot    when the character is loaded or created
--   UpdateKills at most every SEND_INTERVAL_MS while the kill count is
--               changing, and every HEARTBEAT_MS regardless
--   PlayerDied  once, when the character dies
require "pzp_Shared"

pzp.Client = pzp.Client or {}

local SEND_INTERVAL_MS = 15000
local HEARTBEAT_MS     = 300000

local lastKills  = -1
local lastSendMs = 0
local deathSent  = false

local function nowMs()
    if getTimestampMs then
        return getTimestampMs()
    end
    return getTimestamp() * 1000
end

local function characterName(player)
    local ok, name = pcall(function()
        local desc = player:getDescriptor()
        return desc:getForename() .. " " .. desc:getSurname()
    end)
    if ok and name then
        return name
    end
    return ""
end

local function send(player, command)
    local kills = player:getZombieKills()
    sendClientCommand(player, pzp.Module, command, {
        kills = kills,
        hours = player:getHoursSurvived(),
        name  = characterName(player)
    })
    lastKills  = kills
    lastSendMs = nowMs()
end

local function onCreatePlayer(playerIndex, player)
    if not player or not player:isLocalPlayer() then
        return
    end
    deathSent = false
    send(player, pzp.Commands.Snapshot)
end

local function onGameStart()
    local player = getSpecificPlayer(0)
    if player and not player:isDead() then
        send(player, pzp.Commands.Snapshot)
    end
end

local function onPlayerUpdate(player)
    if not player or not player:isLocalPlayer() or player:isDead() then
        return
    end
    -- A live character after a death means a new character.
    deathSent = false

    local elapsed = nowMs() - lastSendMs
    if elapsed < SEND_INTERVAL_MS then
        return
    end
    if player:getZombieKills() ~= lastKills or elapsed >= HEARTBEAT_MS then
        send(player, pzp.Commands.UpdateKills)
    end
end

local function onPlayerDeath(player)
    if not player or not player:isLocalPlayer() or deathSent then
        return
    end
    deathSent = true
    send(player, pzp.Commands.PlayerDied)
end

Events.OnCreatePlayer.Add(onCreatePlayer)
Events.OnGameStart.Add(onGameStart)
Events.OnPlayerUpdate.Add(onPlayerUpdate)
Events.OnPlayerDeath.Add(onPlayerDeath)

print("[pzp] Client tracker loaded")
