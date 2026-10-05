-- pzp kill tracker v5.2.0 (companion mod for PZ Panel)
--
-- Appends one line per client report to Zomboid/Lua/pzp_events.log,
-- which the panel's kill_tracker.py tails:
--   v2|<J|U|D>|<account username>|<kills>|<in-game hours survived>|<character name>
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

local function onClientCommand(module, command, player, args)
    if module ~= pzp.Module then
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

    -- createIfNull = true, append = true
    local file = getFileWriter(EVENT_FILE, true, true)
    if not file then
        print("[pzp] ERROR: could not open " .. EVENT_FILE)
        return
    end
    file:write(line .. "\n")
    file:close()

    if kind == "D" then
        print("[pzp] Death: " .. line)
    end
end

Events.OnClientCommand.Add(onClientCommand)

print("[pzp] Server tracker loaded")
