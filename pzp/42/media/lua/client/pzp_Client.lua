-- pzp kill tracker v5.8.0 (companion mod for PZ Panel)
--
-- Reports the local character's zombie kills and in-game hours survived
-- to the server:
--   Snapshot    once per character, SNAPSHOT_DELAY_MS after it is in the
--               game (sent from OnPlayerUpdate: a command sent while the
--               character is still loading never reached the server)
--   UpdateKills at most every SEND_INTERVAL_MS while the kill count is
--               changing, and every HEARTBEAT_MS regardless
--   PlayerDied  once, when the character dies
--   Status      every STATUS_INTERVAL_MS once the snapshot is sent: world
--               position, health, infection, sleep, vehicle, weapon,
--               profession, traits, skill levels and total XP. Every field
--               is read inside pcall -- a Lua/Java API that differs in a
--               game build costs that one field (sent empty), never the
--               kill reports.
require "pzp_Shared"

pzp.Client = pzp.Client or {}

local SNAPSHOT_DELAY_MS = 5000
local SEND_INTERVAL_MS  = 15000
local HEARTBEAT_MS      = 300000
local STATUS_INTERVAL_MS = 15000

-- Perk IDs, in the order the game's skill screen shows them. The panel maps
-- each ID to the in-game name and group. Some B42 IDs are alternates
-- (ALT_PERKS): the first one the game knows is used.
local SKILLS = {
    "Aiming", "Reloading",
    "Axe", "LongBlade", "Blunt", "Maintenance", "SmallBlade", "SmallBlunt", "Spear",
    "Blacksmith", "Woodwork", "Carving", "Cooking", "Electricity", "Glassmaking",
    "Knapping", "Masonry", "Mechanics", "Pottery", "Tailoring", "MetalWelding",
    "Farming", "Husbandry", "Butchering",
    "Fitness", "Lightfoot", "Nimble", "Sprinting", "Sneak", "Strength",
    "Doctor", "Fishing", "PlantScavenging", "Tracking", "Trapping"
}
local ALT_PERKS = {
    Husbandry = { "Husbandry", "AnimalCare" },
    Knapping  = { "Knapping", "FlintKnapping" },
    Blacksmith = { "Blacksmith", "Blacksmithing" },
}

local lastKills   = -1    -- -1: no snapshot sent yet for this character
local lastSendMs  = 0
local firstSeenMs = nil   -- first update seen for this character
local deathSent   = false
local lastStatusMs = 0

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

-- Run fn; return its result, or default if it errors or returns nil.
local function safe(fn, default)
    local ok, v = pcall(fn)
    if ok and v ~= nil then
        return v
    end
    return default
end

-- Java list (ArrayList etc.) -> "a;b;c"
local function listToString(list)
    local out = {}
    for i = 0, list:size() - 1 do
        out[#out + 1] = tostring(list:get(i))
    end
    return table.concat(out, ";")
end

-- True if obj has a callable member `name`. PZ logs every error even when
-- it is caught by pcall, so probe first instead of calling and catching.
local function has(obj, name)
    return obj ~= nil and obj[name] ~= nil
end

local function traitsString(player)
    if has(player, "getCharacterTraits") then
        local ct = player:getCharacterTraits()
        if has(ct, "getKnownTraits") then
            return listToString(ct:getKnownTraits())
        end
    end
    if has(player, "getTraits") then
        return listToString(player:getTraits())
    end
    return ""
end

local function professionString(player)
    local desc = player:getDescriptor()
    if desc == nil then
        return ""
    end
    local p = nil
    if has(desc, "getCharacterProfession") then
        p = desc:getCharacterProfession()
    elseif has(desc, "getProfession") then
        p = desc:getProfession()
    end
    return p ~= nil and tostring(p) or ""
end

local function findPerk(name)
    for _, n in ipairs(ALT_PERKS[name] or { name }) do
        local perk = Perks[n]
        if perk ~= nil then
            return perk
        end
    end
    return nil
end

-- Percent (0-99) through the player's current level of a perk, or 0 when
-- the game doesn't expose the numbers.
local function perkProgress(player, perk, level)
    if level >= 10 then
        return 0
    end
    local xpObj = player:getXp()
    if not has(xpObj, "getXP") or PerkFactory == nil or not has(PerkFactory, "getPerk") then
        return 0
    end
    local def = PerkFactory.getPerk(perk)
    if def == nil or not has(def, "getTotalXpForLevel") then
        return 0
    end
    local base = 0
    if level > 0 then
        base = def:getTotalXpForLevel(level)
    end
    local nextXp = def:getTotalXpForLevel(level + 1)
    local cur = xpObj:getXP(perk)
    if base == nil or nextXp == nil or cur == nil or nextXp <= base then
        return 0
    end
    local pct = math.floor((cur - base) * 100 / (nextXp - base))
    if pct < 0 then
        return 0
    end
    if pct > 99 then
        return 99
    end
    return pct
end

-- "Name:level:percent;..." for every perk that has a level or progress.
local function skillsString(player)
    local out = {}
    for _, name in ipairs(SKILLS) do
        local perk = findPerk(name)
        if perk ~= nil then
            local level = safe(function() return player:getPerkLevel(perk) end, 0)
            local pct = safe(function() return perkProgress(player, perk, level) end, 0)
            if level > 0 or pct > 0 then
                out[#out + 1] = name .. ":" .. tostring(level) .. ":" .. tostring(pct)
            end
        end
    end
    return table.concat(out, ";")
end

local function healthPercent(player)
    return safe(function()
        local bd = player:getBodyDamage()
        if has(bd, "getHealth") then
            local h = bd:getHealth()
            if h ~= nil then
                return math.floor(h)
            end
        end
        return math.floor(100 - bd:getOverallBodyDamage())
    end, -1)
end

local function vehicleName(player)
    return safe(function()
        local v = player:getVehicle()
        if not v then
            return ""
        end
        if has(v, "getScriptName") then
            local n = v:getScriptName()
            if n ~= nil then
                return tostring(n)
            end
        end
        if has(v, "getScript") then
            local sc = v:getScript()
            if sc ~= nil then
                return tostring(sc:getName())
            end
        end
        return ""
    end, "")
end

local function sendStatus(player)
    sendClientCommand(player, pzp.Module, pzp.Commands.Status, {
        x          = safe(function() return math.floor(player:getX()) end, 0),
        y          = safe(function() return math.floor(player:getY()) end, 0),
        z          = safe(function() return math.floor(player:getZ()) end, 0),
        health     = healthPercent(player),
        infected   = safe(function() return player:getBodyDamage():IsInfected() end, false) and 1 or 0,
        asleep     = safe(function() return player:isAsleep() end, false) and 1 or 0,
        vehicle    = vehicleName(player),
        weapon     = safe(function()
            local item = player:getPrimaryHandItem()
            if item == nil then
                return ""
            end
            return item:getDisplayName()
        end, ""),
        profession = safe(function() return professionString(player) end, ""),
        traits     = traitsString(player),
        skills     = skillsString(player),
        xp         = safe(function() return math.floor(player:getXp():getTotalXp()) end, 0)
    })
    lastStatusMs = nowMs()
end

local function resetCharacter()
    lastKills   = -1
    firstSeenMs = nil
    deathSent   = false
    lastStatusMs = 0
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

    if now - lastStatusMs >= STATUS_INTERVAL_MS then
        sendStatus(player)
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
