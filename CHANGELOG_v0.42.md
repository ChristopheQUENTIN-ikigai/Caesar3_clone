# Changelog — v0.42

**Theme:** correct the port/harbor model and add building upkeep. Goods
are buffered at **ports**; **harbors** are ship shelter only. The naval
buildings now cost gold to run, and ships build fast for dev feedback.

## Shipped

### Port / harbor role fix (storage moved off harbors)

The v0.39 data had it backwards — harbors carried the goods `storage`
while the ports were near-empty defs. Corrected to match the intended
(and already-documented) model:

- **Commercial port** now holds the goods buffer (`storage: 400`, the
  value harbors used to have). It's automatically picked up by the
  `Storage` allocator (which discovers any building with `storage > 0`),
  so it functions as a warehouse-class buffer with the accept/reject
  panel — no wiring change needed.
- **Military port** stores **no goods** (`storage: 0`) — it handles troop
  embark/disembark, a later thread.
- **Commercial & military harbors** lose `storage` entirely — they are
  ship shelter only. Descriptions updated to say goods/troop transfer
  happens at the port, not the harbor.

### 10 ship-berth slots per harbor

`ship_slots` raised 4 → 10 on both harbors (the "container" of berths the
player expects). Ports keep `ship_slots: 0` — they're transfer points,
not shelters.

### Building gold upkeep (new mechanic)

A flat per-building gold cost, deducted every tick regardless of staffing
or connectivity — you pay to keep a building standing.

- New `upkeep` field on `BuildingDef` (parsed from `data/buildings.json`,
  default 0).
- The six naval buildings (2 ports, 2 harbors, 2 shipyards) have
  `upkeep: 1`.
- `EconomyManager.update` sums upkeep over the tick's building list,
  deducts it from the treasury, and folds it into `expenses_per_tick`
  (so it shows in the HUD "Cost" line). Also exposed as
  `upkeep_per_tick` for the inspector/stats.

### Faster ship builds (dev feedback)

`WalkerManager.SHIPYARD_INTERVAL` (the per-hull build time the v0.41
queue drains against) cut 600 → 60 ticks (~30s at 2 tps) so naval systems
can be exercised quickly while still in active development. Bump back up
for a real difficulty pass. (Note: this is the *ship* build time; the
*building's own* `construction_ticks` are unchanged.)

## Tests

- `tests/test_shipyards.py` — building-def tests updated to the new model
  (harbors: 10 slots, 0 storage); two new tests:
  `test_v042_port_harbor_storage_split` and
  `test_v042_naval_buildings_have_upkeep`.
- `tests/test_economy.py` — new `TestBuildingUpkeep` (4 tests): per-tick
  deduction (A/B isolated since input-consumption also costs gold),
  scaling with building count, zero upkeep for non-naval buildings, and
  inclusion in the expenses line.

Suite: **1066 passed, 0 failed** (was 1060 at v0.41 entry; +6).

## Files touched

- `data/buildings.json` — storage→ports, harbors shelter-only (10 slots,
  no storage), `upkeep: 1` on the six naval buildings, harbor
  descriptions.
- `building.py` — `upkeep` field on `BuildingDef` + parser.
- `economy.py` — upkeep deduction in `update`, `upkeep_per_tick`.
- `walkers.py` — `SHIPYARD_INTERVAL` 600 → 60.
- `version.py` — `v0.41` → `v0.42`.

## In-game checks (need a GL context — can't verify here)

- Click a **commercial port**: should now show the storage/stock panel +
  accept-reject toggles (it's a buffer now).
- Click a **harbor**: should show 10 berths and **no** goods-storage
  panel.
- Watch the HUD "Cost" line: each naval building adds 1 gold/tick.
- Queue a ship: it should now finish in ~30s, not ~5 min.

## Still outstanding (naval expansion)

Port↔granary/warehouse routing by item nature (nutrients→granary,
goods→warehouse — the storage buffer now lives in the right place for
this), berth occupancy wiring (slots exist but aren't yet filled by idle
ships), and the foundational ship pathfinder (still greedy one-step).
