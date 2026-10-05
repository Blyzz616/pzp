-- pzp kill tracker v5.2.0 (companion mod for PZ Panel)
--
-- Reports the local character's zombie kills and in-game hours survived
-- to the server:
--   Snapshot    once per character, SNAPSHOT_DELAY_MS after it is in the
--               game (sent from OnPlayerUpdate: a command sent while the
--               character is still loading never reached the server)
--   UpdateKills at most every SEND_INTERVAL_MS while the kill count is
--               changing, and every HEARTBEAT_MS regardless
--   PlayerDied  once, when the character dies
require "pzp_Shared"

pzp.Client = pzp.Client or {}

local SNAPSHOT_DELAY_MS = 5000
local SEND_INTERVAL_MS  = 15000
local HEARTBEAT_MS      = 300000

local lastKills   = -1    -- -1: no snapshot sent yet for this character
local lastSendMs  = 0
local firstSeenMs = nil   -- first update seen for this character
local deathSent   = false

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

local function resetCharacter()
    lastKills   = -1
    firstSeenMs = nil
    deathSent   = false
end

local function onCreatePlayer(playerIndex, player)
    if player and player:isLocalPlayer() then
        resetCharacter()
    end
end

local function onPlayerUpdate(player)
    if not player or not player:isLocalPlayer() or player:isDead() then
        return
    end
    -- A live character after a death is a new character.
    if deathSent then
        resetCharacter()
    end

    local now = nowMs()
    if lastKills < 0 then
        firstSeenMs = firstSeenMs or now
        if now - firstSeenMs >= SNAPSHOT_DELAY_MS then
            send(player, pzp.Commands.Snapshot)
        end
        return
    end

    local elapsed = now - lastSendMs
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
Events.OnPlayerUpdate.Add(onPlayerUpdate)
Events.OnPlayerDeath.Add(onPlayerDeath)

print("[pzp] Client tracker loaded")
