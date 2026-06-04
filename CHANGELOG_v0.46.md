# Changelog — v0.46

**Theme:** make naval combat actually fire (raid trigger), make the
supply chain visible (goods-flow walker), unify ships as carriers, add
placeholder ship art, and cap the trireme at 10 troop slots.

## Shipped

### Naval raid trigger (closes the v0.45 gap)

v0.45 built enemy-ship *combat* but nothing spawned them. Now:

- `WalkerManager.spawn_naval_raid(game_map, count, ...)` dispatches a
  wave of `EnemyShip`s from map-edge water tiles toward a coastal
  objective. `spawn_enemy_ship` spawns one.
- New `force_naval_raid` trigger effect (fields: `count`, `hp`,
  `damage`) wired through a `naval_raid_callback` on `TriggerManager` to
  `CaesarGameWindow._dispatch_naval_raid`. Headless contexts (no
  callback) log and no-op.

### CargoCarrier — visible port→store goods flow

v0.44 routed cargo by class but nothing moved on screen. `CargoCarrier`
is a walker that marches a straight tile path from a port to its routed
store carrying `(good, qty)`; `WalkerManager.dispatch_cargo_carrier`
picks the store (granary for nutrients, warehouse otherwise via the
shared classifier) and sends one. Uses the new `cargo_carrier` sprite.

### Ships as carriers (generic manifest)

Every `Ship` gained a `manifest` `{good: qty}` cargo hold with
`load_cargo` / `unload_cargo` / `cargo_total` / `cargo_space` and a
`CARGO_CAPACITY` (100 units) — the unified model behind
`TradeShip.cargo_qty` and `TransportShip.cargo`.

### Trireme — 10 troop slots

`TransportShip` (the trireme) now caps embarked troops at
`TROOP_SLOTS = 10`. Construction past the cap routes the extras to
`overflow`; `embark(soldiers)` boards only what fits and returns the
count; `free_slots` / `is_full` expose state. `data/units.json` records
`troop_slots: 10`.

### Placeholder ship sprites

16×16 RGBA PNGs added under `assets/textures/walkers/`: `trade_ship`,
`warship`, `transport_ship`, `barbarian_ship`, `cargo_carrier`. Crude on
purpose — they replace the coloured-rect fallback so ships are
distinguishable; real art still wanted.

### Docs

`DEV_AUDIT.md` updated: headline refreshed to v0.46, new **Chapter 11**
documenting the v0.40→v0.46 naval evolution, the updated audit status
(7.2 and 9.1 still open), GL-verification gaps, and the structural gap
list (notably: no military-unit command interface).

## Tests

- `tests/test_naval_raid_trigger.py` — 7 (spawn wave, edge spawn,
  objective, no-edge-water safety; trigger effect invokes callback,
  defaults, no-callback safety).
- `tests/test_cargo_carrier.py` — 9 (path walk, cargo, born-done edges,
  straight-path contiguity, dispatch routing granary/warehouse/none,
  reaches store).
- `tests/test_naval.py` — +5 (ship manifest load/unload/cap/reject;
  trireme 10-slot cap, embark respects free slots, empty transport).

Suite: **1120 passed, 0 failed** (was 1102 at v0.45 entry; +18).

## Files touched

- `walkers.py` — `CargoCarrier`; `Ship.manifest` + helpers;
  `TransportShip.TROOP_SLOTS`/`embark`/`free_slots`/`overflow`;
  `spawn_enemy_ship`/`spawn_naval_raid`; `dispatch_cargo_carrier` +
  `_straight_tile_path`.
- `triggers.py` — `naval_raid_callback`, `force_naval_raid` effect.
- `game_window.py` — `_dispatch_naval_raid`, callback wiring.
- `data/units.json` — `troop_slots: 10` on transport_ship.
- `assets/textures/walkers/` — 5 placeholder sprites.
- `DEV_AUDIT.md`, `version.py`.

## In-game checks (need a GL context)

- A `force_naval_raid` trigger (or manual spawn) should send enemy ships
  in; a warship intercepts, an unescorted trader is threatened.
- A dispatched cargo carrier should walk port→store carrying its good.
- Ship sprites should now render as little boats, not coloured rects.

## Note: military-unit command interface

A design review for a unit-management command vocabulary (group, split,
embark, disembark, join, navpoint, attack, defend, patrol, retreat,
siege, rest, scout) is tracked separately — it's a large UI/interaction
thread, unscoped here. The trireme's `embark`/slot model is the data
groundwork for the embark/disembark commands.
