-- pzp kill tracker v5.7.0 (companion mod for PZ Panel)
--
-- Appends one line per client report to Zomboid/Lua/pzp_events.log,
-- which the panel's kill_tracker.py tails:
--   v2|<J|U|D>|<account username>|<kills>|<in-game hours survived>|<character name>
--   v3|S|<account>|<x>|<y>|<z>|<health %>|<infected 0/1>|<asleep 0/1>|<vehicle>|
--       <weapon>|<profession>|<traits ; separated>|<skills Name:level:percent ; separated>|<total xp>
--   (S = live status; health -1 = unknown, empty text = unknown)
-- The account name comes from the server's own player object, not from
-- the client. "|" and newlines are stripped from free-text fields.
require "pzp_Shared"

pzp.Server = pzp.Server or {}

local EVENT_FILE = "pzp_events.log"

local KINDS = {
    [pzp.Commands.Snapshot]    = "J",
    [pzp.Commands.UpdateKills] = "U",
    [pzp.Commands.PlayerDied]  = "D"
}

local function clean(value)
    local s = string.gsub(tostring(value or ""), "[|\r\n]", " ")
    return s
end

local function appendLine(line)
    -- createIfNull = true, append = true
    local file = getFileWriter(EVENT_FILE, true, true)
    if not file then
        print("[pzp] ERROR: could not open " .. EVENT_FILE)
        return false
    end
    file:write(line .. "\n")
    file:close()
    return true
end

local function num(value)
    return tostring(math.floor(tonumber(value) or 0))
end

local function text(value, max)
    local s = clean(value)
    if #s > (max or 120) then
        s = string.sub(s, 1, max or 120)
    end
    return s
end

local function onStatus(player, args)
    appendLine("v3|S|" .. clean(player:getUsername()) .. "|" ..
               num(args.x) .. "|" .. num(args.y) .. "|" .. num(args.z) .. "|" ..
               num(args.health) .. "|" ..
               ((tonumber(args.infected) or 0) ~= 0 and "1" or "0") .. "|" ..
               ((tonumber(args.asleep) or 0) ~= 0 and "1" or "0") .. "|" ..
               text(args.vehicle, 60) .. "|" .. text(args.weapon, 80) .. "|" ..
               text(args.profession, 60) .. "|" .. text(args.traits, 400) .. "|" ..
               text(args.skills, 1200) .. "|" .. num(args.xp))
end

local function onClientCommand(module, command, player, args)
    if module ~= pzp.Module then
        return
    end
    if command == pzp.Commands.Status then
        if player and args then
            onStatus(player, args)
        end
        return
    end
    local kind = KINDS[command]
    if not kind or not player or not args then
        return
    end

    local kills = math.max(0, math.floor(tonumber(args.kills) or 0))
    -- "hoursSurvived" is what pre-5.0.0 clients send.
    local hours = tonumber(args.hours or args.hoursSurvived) or 0
    hours = math.floor(hours * 100) / 100

    local line = "v2|" .. kind .. "|" ..
                 clean(player:getUsername()) .. "|" ..
                 tostring(kills) .. "|" ..
                 tostring(hours) .. "|" ..
                 clean(args.name)

    if not appendLine(line) then
        return
    end

    if kind == "D" then
        print("[pzp] Death: " .. line)
    end
end

Events.OnClientCommand.Add(onClientCommand)

print("[pzp] Server tracker loaded")
