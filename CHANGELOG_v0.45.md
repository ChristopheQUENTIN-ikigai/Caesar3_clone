# Changelog — v0.45

**Theme:** the last two naval-expansion threads — harbor berth occupancy
(structural) and naval combat depth (enemy ships at sea). Both rest on
the v0.43/44 pathfinder.

## Part 1 — Harbor berth occupancy

Harbors carried `ship_slots: 10` (since v0.42) but nothing filled them.
Berthing is now a **computed view** — no new persistent state, so no
save-schema change and nothing to desync:

- `WalkerManager.harbor_berth_occupancy(game_map)` returns, per harbor,
  `{"capacity", "occupied", "ships"}`. A ship is berthed when it is idle
  (no goal, or already at its goal), role-matches the harbor, and sits in
  the harbor's berth zone (the water tiles adjacent to its footprint).
- **Role matching:** commercial harbors berth trade ships; military
  harbors berth warships and transports; an unroled harbor berths any.
- Occupancy is capped at `ship_slots`; each ship is assigned to at most
  one harbor, so harbors sharing a water tile don't double-count.
- Helpers: `_berth_zone`, `_ship_is_idle`, `_berth_role_matches`.
- **UI:** the harbor info panel now shows a `Berths: N/10` line (green
  until full, gold when full).

## Part 2 — Naval combat depth (enemy ships at sea)

### `EnemyShip` — hostile naval raider

A new `Enemy` subclass that sails the **sea** domain (A* over water, same
cache idiom as `Ship`) toward an objective. Because it subclasses
`Enemy`, the combat resolver's enemy set picks it up for free — a
friendly **Warship intercepts and sinks it at sea** with no special-casing
(warships are ranged; the existing pass shoots any enemy in range). A
greedy water step is the fallback when A* finds no route.

### Sea raids on trade ships

`WalkerManager._resolve_sea_raids` (called at the end of `_resolve_combat`,
so it runs live every tick): an `EnemyShip` within Chebyshev 1 of a
`TradeShip` rams it for its damage. Trade ships are non-combatants the
main pass ignores, so without this raiders would sail through merchant
traffic untouched. Deliberately one-directional — traders don't fight
back — which is exactly what makes a **warship escort meaningful**: keep a
raider out of the lane and the trader survives; leave it unescorted and a
persistent raider sinks it.

## Tests

- `tests/test_harbor_berths.py` — 9: empty harbor, idle ship occupies a
  berth, sailing ship does not, cap at slots, role matching
  (commercial/military, both directions), out-of-zone, no-harbors.
- `tests/test_naval_combat.py` — 7: enemy ship sails to objective, warship
  sinks an enemy ship at sea, EnemyShip counts as enemy, raider damages /
  can sink an adjacent trader, distant raider can't reach, no-raider no-op.

Suite: **1102 passed, 0 failed** (was 1086 at v0.44 entry; +16).

## Files touched

- `walkers.py` — `EnemyShip` class; `harbor_berth_occupancy` + berth
  helpers; `_resolve_sea_raids` (+ call from `_resolve_combat`).
- `game_window.py` — `Berths: N/cap` line in the harbor info panel.
- `version.py` — `v0.44` → `v0.45`.

## In-game checks (need a GL context)

- Click a harbor with idle ships nearby: the info panel should read
  `Berths: N/10`.
- Spawn/encounter an enemy ship: a warship should intercept and sink it;
  an unescorted trade ship caught by a raider should take damage and can
  be sunk. (Enemy-ship *spawning* — a raid trigger that sends them — is
  the natural follow-up; the combat behaviour they drive is in place.)

## Naval expansion — status

All four originally-scoped threads are now in: shipyard queue (v0.41),
port/harbor split + upkeep (v0.42), ship pathfinding (v0.43/44 BFS→A* +
overlay), port↔store routing (v0.44), harbor berths (v0.45), naval combat
(v0.45). Natural follow-ups: a scripted enemy-ship raid trigger
(`force_raid` with a naval side), a visible goods-flow walker port→store,
and ship/building sprite art to replace the coloured-rect placeholders.
