-- pzp kill tracker v5.2.1 (companion mod for PZ Panel)
pzp = pzp or {}

pzp.Module = "pzp"

pzp.Commands = {
    Snapshot    = "Snapshot",     -- character loaded or created
    UpdateKills = "UpdateKills",  -- periodic update
    PlayerDied  = "PlayerDied"    -- character died (exact final count)
}
