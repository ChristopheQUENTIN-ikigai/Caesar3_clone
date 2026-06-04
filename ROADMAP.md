# Caesar III Clone — Roadmap

This document tracks the project's release history (what shipped) and
the planned future direction (what's next, what's tentative, what's
been deliberately deferred).

---

## Release history

### v0.53 — Inter-city commerce is per-trip only: per-tick route layer retired *(shipped)*

**Theme:** finish the commerce rework begun in v0.50–v0.52. The arc:

- **v0.50** added the *Commercial roads* window (`R`): link foreign cities, then attach **persistent per-tick auto-trade routes** that sold a stockpile good every tick for gold.
- **v0.51** added the **per-trip voyage** layer (`voyages.py`): a free commercial ship loads cargo, sails to a linked city, and gold lands once on its return — gated on free ships, port stock, destination war state, and sea piracy, with a distance-scaled cooldown.
- **v0.52** stopped the per-tick routes from *paying gold* (income moved entirely to voyages) and made them export-only, plus made voyages visible as real `TradeShip` walkers.
- **v0.53** removes the per-tick route layer **entirely**. It had become a silent value leak: the route still moved stock out of the city every tick for *no gold*, while the UI mislabelled it "gold by trip". Inter-city goods now move **only per trip** — a city exports only when a ship sails a voyage and sells on return.

**Shipped:**

- **`CommercialRoadManager` is now link-bookkeeping only.** `add_route` / `remove_route` / `routes_for` / `total_routes` survive as deprecated no-op stubs (create nothing, register nothing with the tick loop, return `None` / `False` / `[]` / `0`) so no caller crashes. Since nothing builds a `pays_treasury=False` route anymore, the tick loop moves no inter-city stock and credits no inter-city gold. The generic `TradeRoute` / `TradeRouteManager` primitive (ambient routes, `pays_treasury=True` default) is untouched and stays available for reuse.
- **Save/load: links persist, routes dropped.** `to_dict` emits `routes: {}` for shape stability; `from_dict` reads the linked-city set and **ignores** any legacy `routes` block — a pre-v0.53 save stops leaking stock the instant it loads, while a city that existed only under the old routes block is still re-linked (the player keeps the road they paid for).
- **Window reworked to per-trip.** Removed the "Stock-moving routes" list, the "Add a route" staging area, the rate stepper, and the "Add route" button (and the `addroute:` / `removeroute:` / `rate:` click actions). Kept the good picker and "Ship by trip"; the detail view shows a per-trip earnings preview (capacity × unit price × interval), and the city list shows voyage state instead of a route count.
- **Tests:** the six per-tick route tests in `test_v050_features.py` rewritten to the retired-no-op contract, plus a new legacy-save recovery test; window `addroute`/`rate` tests replaced by `ship_by_trip` / `good_pick`; `test_saveload.py` round-trip updated. Suite: **1264 passed, 0 failed.**

See `CHANGELOG_v0.53.md` (and `CHANGELOG_v0.50.md` … `_v0.52.md`) for the full notes.

### v0.39 — Naval infrastructure: shipyards, harbors, port split + combat-event crash fix *(shipped)*

**Theme:** give the naval system real buildings — places that *build* ships and *berth* them — and split the generic port into commercial and military flavours. Also fixes a crash introduced by the v0.38 naval events.

**Shipped:**

- **Combat-event crash fix (regression from v0.38).** `WalkerManager._process_ships` appended 4-tuples (`("trade_ship_sale", row, col, price)`) to `_combat_events`, but `game_window._game_tick` unpacks every event as a 5-tuple `(attacker, defender, amount, row, col)` — so the first naval event crashed the tick with `ValueError: not enough values to unpack (expected 5, got 4)`. Both naval events now conform to the 5-tuple contract, and `tests/test_naval.py` gained a regression test asserting every ship-emitted event has exactly 5 fields so this can't silently return.
- **Six new buildings** in `data/buildings.json`, all `needs_water_adjacent` (must touch a water tile):
  - `commercial_shipyard` / `military_shipyard` — build and launch ships (`ship_kind` = `trade_ship` / `warship`) onto an adjacent water tile, consuming wood/planks(/iron) per hull.
  - `commercial_harbor` / `military_harbor` — large buildings with `ship_slots: 4` berths for idle ships, intended as the buffer that transfers goods/troops between docked ships and the city's warehouses/granaries.
  - `commercial_port` / `military_port` — the generic `port` is split into two explicit `port_role` flavours; the legacy `port` id is kept for old-map back-compat.
- **`Building` gained three naval fields** — `ship_slots` (harbor berths), `ship_kind` (what a shipyard builds), `port_role` (`commercial`/`military`) — parsed in `from_dict`.
- **Shipyard runtime** — `WalkerManager._process_shipyards` finds each shipyard's adjacent water tile and periodically launches its ship (capped per yard, guarded on construction state / water access / affordability). Covered by `tests/test_shipyards.py` (8 tests).
- Texture-manifest entries added for the six buildings (placeholder art; the renderer falls back to a coloured rect, same as the ship sprites).

Suite: **1027 passed, 0 failed**.

**Not yet wired (next step, see What's next):** the *harbor transfer* itself. `ship_slots` is declared and parsed, but there is no runtime that berths idle ships in a harbor or moves goods/troops between a harbor and the warehouses/granaries. Trade ships still credit income directly on reaching the map-edge exit (v0.38 behaviour) rather than loading from a harbor buffer; transports still disembark on any shore rather than docking at a military harbor. Wiring the harbor as a real buffer is the v0.40 task.

### v0.38 — RPG runtime, naval system, trade spread & a green test bar *(shipped)*

**Theme:** make the island-conquest scenario actually playable and close the largest standing gaps from the code audit. Four threads shipped together.

**Shipped:**

- **RPG-request runtime (closes audit finding 7.1).** The RPG editor, loader, `fire_request` trigger effect, and queue had all shipped across v0.31–v0.36, but the *consumer* — the on-screen decision panel — was never built, so a queued request displayed nothing and a decision's `effects` / `delay_ticks` / `set_flag` / `fire_event` never ran. New `rpg_player.py` (a sibling of `cutscene_player.py`) plus six wiring points in `game_window.py` and an `apply_rpg_decision` method now present a scene + portrait + decision buttons, apply the chosen outcome, and defer effects with a non-zero `delay_ticks` onto a queue processed each tick ("pay next month"). 12 tests in `tests/test_rpg_player.py` — the integration endpoint test that was missing.
- **Naval system.** New `Ship` walker base (moves on an inverted `_impassable_sea` domain) and `TradeShip` / `TransportShip` / `Warship` subclasses, plus a `WalkerManager` ship API and `_process_ships` (trade-route income + reversal, troop disembark onto the shore). Three `naval`-category unit defs in `units.json`. A scenario's `naval` block is spawned at load by `mapfile._spawn_scenario_navy`, so transports actually carry legions to the island shore. Warships are wired into `_resolve_combat` so they screen the coast. 12 tests in `tests/test_naval.py`.
- **Buy/sell trade spread.** `bartering.TRADE_SPREAD` (0.15) makes the market buy below and sell above the listed price — no risk-free arbitrage; you trade to cover a shortage. The economic depth behind the trade ships. 4 tests; set to 0.0 to restore symmetric pricing.
- **Save/load persistence for the RPG delayed-effect queue.** Save version 13 → 14: `_rpg_delayed_effects` and `_pending_rpg_requests` now persist, with a v13→v14 migration defaulting them empty for old saves, so a deferred payment survives a save/load. 4 tests.
- **Green test bar.** All 16 standing failures (the stale pre-v0.34 farm-output tests and the rotted debug-logging mock) were fixed, and the version test was made bump-robust. Suite: **1018 passed, 0 failed** (was 970 passed / 16 failed at v0.37).

**Known caveat:** the pure logic of all the above is unit-tested headless, but rendering and input (the RPG panel draw/click, ship sprites on water, ship placement UI) could not be verified in the build environment (no GL context). Load `data/scenarios/mare_nostrum/` in the running game to confirm the transports land their troops and a `fire_request` trigger shows a decision panel before relying on it in a shipped scenario.

### v0.29 — Combat-system review (cavalry shock, ranged-vs-building, persistent fire, Fireman walker, editor cap 128 → 64) *(shipped)*

**Theme:** the player asked for a combat-system review, citing five
specific items: a check on ranged units (especially bowmen) and the
rate at which buildings get destroyed; a cavalry shock-on-contact
mechanic; a separation between engineer repair and active
firefighting (engineers aren't enough when a building catches fire —
"need a fireman"); and lowering the editor map ceiling from the
v0.27 128×128 maximum to a more practical 64×64. All five items
shipped together.

**Shipped:**

- **Cavalry shock — first-strike charge bonus.** New
  `COMBAT_CAVALRY_SHOCK_MULT = 1.75` constant. Each `Soldier`
  carries a `category` field (sourced from the unit registry's
  `UnitDef.category`) and a `_struck_targets: set[int]` ledger
  of Enemy `id()`s it has hit. In `WalkerManager._resolve_combat`,
  cavalry soldiers striking a never-hit enemy fold the shock
  multiplier into their damage product; subsequent strikes on the
  same target use 1.0. Disengaging and re-engaging the same target
  does NOT refresh the bonus (a charge happens once per pairing); a
  new enemy is a new shock event. Stacks multiplicatively with the
  existing armed / retreat / defence modifiers. Emits a new
  `("shock", "enemy", bonus_dmg, row, col)` event so the HUD can
  surface "Cavalry charge!" distinctly from a routine hit.
- **Bowman damage 8 → 11.** Both `data/units.json` and the
  `units.py` builtin defaults updated. A damage-11 bowman now
  meaningfully out-trades a generic raider at Chebyshev 2 (the
  bowman's stand-off advantage).
- **Ranged-vs-building (fire arrows).** New
  `COMBAT_RANGED_BUILDING_DAMAGE_MULT = 0.5` constant gates a new
  pass in `_resolve_combat`: every armed ranged soldier within
  `RANGED_ATTACK_RANGE` of an enemy-occupied building tile deals
  `int(round(s.damage * MULT))` to the structure (minimum 1) and
  flips the building's `on_fire` flag. Melee soldiers and unarmed
  bowmen skip this pass; friendly (non-enemy-occupied) buildings
  are never targeted. Use case: sieging a captured granary without
  rushing melee infantry into the death zone.
- **Persistent fire.** Pre-v0.29, the v0.28 enemy-burn damage
  stopped the moment the enemy walked away or died. v0.29 sets
  `building_state[(orow, ocol)]['on_fire'] = True` on the first hit
  (enemy melee or ranged-soldier fire-arrow), and a new
  fire-propagation pass in `_resolve_combat` iterates every on-fire
  building each tick and applies `COMBAT_FIRE_PROPAGATION_DAMAGE = 1`
  (independent of enemy presence). An unattended fire on a
  default-HP (30) building destroys it in ~30 ticks; an actively-
  attacked one in ~10 ticks. The flag persists through save/load
  via the existing `building_state` snapshot — no schema change.
  The pass iterates `building_state` directly (O(burning), not
  O(map)) so the common case of zero on-fire buildings has zero
  overhead.
- **Fireman walker.** New `Fireman` class in `walkers.py`. Spawned
  from prefectures, one per prefecture
  (`MAX_FIREMEN_PER_PREFECTURE = 1`, `FIREMAN_SPAWN_INTERVAL = 30`).
  Patrols within `PATROL_RADIUS = 4` tiles of its home prefecture
  (matches the prefecture's `service_radius`). Each tick after
  combat resolution, runs `extinguish_adjacent` which scans the
  Chebyshev-1 box for on-fire buildings and reduces `burn` by
  `COMBAT_FIREMAN_EXTINGUISH_RATE = 4`. When `burn` hits 0, the
  `on_fire` flag is cleared (and the `burn` key removed from state
  to keep the dict tidy). Emits a `("fireman", "fire", amt,
  orow, ocol)` event per extinguish. Spared from the
  end-of-tick citizen cull (same as Soldier/Enemy — has a job).
- **Engineer-post repair gated on `on_fire`.** New hard-coded gate
  in `decay.py::DecayManager.tick`: while `state["on_fire"] is
  True`, the repair component drops to 0. Decay still applies and
  the fire-propagation pass keeps eating HP. Only after the fireman
  has cleared the flag does the engineer's repair tick resume.
  Models the real-world ordering: extinguish, then rebuild.
- **`EDITOR_GRID_MAX` 128 → 64.** Lowered in `constants.py` per
  playtester feedback (128×128 maps were technically supported but
  pathological for A* on integrated GPUs; 64×64 covers every
  authored scenario). The v0.27 supporting refactor (services /
  walkers / pathfinding allocating against live `game_map.rows /
  cols`) stays — only the enforcement constant moved. Tests that
  instantiate `GameMap(reg, rows=128, cols=128)` directly still
  pass.
- **Prefecture description / worker_role updated.**
  `data/buildings.json` now declares `worker_role: "prefect"` and
  notes the fireman dispatch in the description.
- **24 new tests** in `tests/test_v029_features.py`. Suite total:
  **789 passing** (+24 from v0.28's 765), zero regressions over the
  19 pre-existing wheat-farm legacy failures.

**Open for review — next drop:**

The military review flagged five items; all five shipped. Four
candidates queued for the next drop, prioritised by playtester
feedback:

- **Per-soldier "experience" stat** — a soldier that survives N
  strikes gains a small damage bonus. Default off.
- **Enemy type diversification** — raid chief (armoured + higher HP)
  and enemy archer (ranged retaliation).
- **Tower garrison rebalance** — scouts (damage 6) are too thin for
  meaningful wall defence; either bump scout damage or change the
  tower's spawn_unit.
- **Fire spread to adjacent buildings** — currently fire is
  localised to the hit footprint. A simple Chebyshev-1 propagation
  chance per tick would create catastrophic raid dynamics.

Direction welcome before v0.30 commits to a scope.

See `CHANGELOG_v0.29.md` for the full release notes.

---

### v0.28 — Trigger editor + per-map scheduled events (Drop 2 of the v0.27 brief) *(shipped)*

**Theme:** v0.27 shipped infrastructure (road brushes, bridges,
128×128 maps); v0.28 shipped the *authoring* half — a Trigger editor
on the splash menu for editing `data/events.json`, a scheduled-events
panel in the map editor for wiring events into specific scenarios,
plus the Blizzard and Flood event definitions the player asked for.

The engine plumbing landed in v0.27 (`EconomicEventManager.scheduled`
with six trigger kinds, map JSON schema with `scheduled_events` and
`disable_random_events`, end-to-end Blizzard implementation
including the `pop_kill_when_resource_zero` modifier path). v0.28
filled in the UI and tests.

**Shipped:**

- Trigger editor modal on the splash menu — scrollable event list,
  per-event scalar editor (effects dict, pop, happy, msg,
  duration_ticks, modifiers dict), RGB colour picker, atomic save
  back to `data/events.json` via `.tmp → os.replace`.
- Scheduled-events panel in the map editor toolbar — per-row
  event-name dropdown, trigger-kind dropdown (the six kinds:
  `at_year`, `at_month`, `at_tick`, `random_after_tick`,
  `on_population_above`, `on_treasury_below`), trigger-value field,
  add/edit/remove rows. Top-bar `disable_random_events` toggle.
- Blizzard end-to-end (doubled wood consumption + freeze-death
  pop-kill when wood is empty), Flood event definition, and the six
  scheduled-trigger kinds all round-tripping through map JSON.

See `CHANGELOG_v0.28.md` for the full release notes.

---

### v0.26 — Building taxonomy expansion, construction-time rollout, bubble layout fix *(shipped)*

**Theme:** the player asked for the palette to better reflect a real
city's organisation — a dedicated Mining tab with split iron / copper /
gold mines (each with its own smelter), a Politic tab pulling Senate
out of religion and adding Forum / Consulate / Embassy / Caesar Hostel,
a Bank tab for money producers, and rounded-out Edu / Health / Fun
tabs. Plus the v0.25-shipped construction-time mechanic finally
populated across the building set, and the middle-click reserves
bubble had two glitches the player flagged from screenshots (bar
overlapping the value text, and a useless "1 tile" line for
inexhaustible single-tile footprints).

**Shipped:**

- **Mining tab.** Generic `mine` split into three vein-specific ids:
  `mine` (Iron Mine, needs `iron_vein` only), `copper_mine` (needs
  `copper_vein`), `gold_mine` (needs `gold_vein`). Generic `smelter`
  renamed to Iron Smelter and joined by `copper_smelter` and
  `gold_smelter` — each refines its own ore. `clay_pit` moved to
  Mining and now requires the new `clay_deposit` feature on its
  footprint (consistent with the other extractive industries). Four
  new resource ids — `copper_ore`, `gold_ore`, `copper`, `gold` —
  priced in `bartering.STOCK_PRICES` and given placeholder icons in
  `assets/textures/resources/`. The starter map seeds at least one
  `iron_vein` patch (south-west) and one `clay_deposit` patch
  (central) so the new chains are buildable from frame zero.
- **Politic tab.** Senate moved out of Religion. Four new monumental
  civic buildings: Forum (2×2, public-square entertainment +
  revenue), Consulate (2×2, ongoing money sink, prestige), Embassy
  (2×2, diplomacy / trade-route relations), Caesar Hostel (3×3,
  reputation centerpiece, consumes wine).
- **Edu tab.** Three new specialised schools: Chess School,
  Painting School, Music School — all 2×2, providing education at
  0.75 intensity over a 4-tile radius. Variety lets the player
  cover a neighbourhood with a mix of disciplines.
- **Health tab.** Three new providers: Public Bath (water-themed,
  health + slight happiness), Dentist (small, low-cost specialist),
  Sanitarium (3×3, wide-radius high-intensity health for late-game
  cities).
- **Fun tab.** Coliseum (3×3, 8-tile entertainment radius,
  monumental) and Zoo (3×3, consumes meat, family-scale fun).
- **Bank tab (new).** Local Bank (18 money/tick, low-overhead),
  Business Bank (35 money/tick with 5 overhead), Private Bank (55
  money/tick with 10 overhead) — staffed by `trader` walkers so they
  compete with markets for the trader pool.
- **Construction-time rollout.** v0.25's `construction_ticks` field
  was set on a single building (military_manufacture); v0.26 set
  meaningful values across ~50 buildings (small: 12-20 ticks,
  medium: 25-40, large: 50-80, monumental: 100-120). The progress
  bar and dark scaffolding tint v0.25 already implemented now
  actually show up in normal play. Tiny civic infrastructure
  (well, fountain, aqueduct, prefecture, engineer post, tower,
  house) stays instant so water lines and patrol posts feel like
  roads. New `bypass_construction` kwarg on `GameMap.place_building`
  lets tests place buildings as live without spinning ticks
  (autouse `conftest` fixture sets it as the test-time default so
  the 600+ existing tests don't need touching).
- **Bubble layout fix** (`_draw_extract_bubble`). The v0.23 bubble
  was 52 px tall with the progress bar at a fixed offset; on
  certain footprints the value-text glyphs collided with the bar
  visually (the bar.png screenshot). v0.26 computes the bubble
  height from what's actually drawn — label + optional value text +
  optional progress bar, with explicit gap constants. The
  "1 tile" line that rendered when a single inexhaustible feature
  tile sat under the footprint (bar2.png — useless because the
  player already sees the footprint) is now dropped: when
  `reserves is None and tiles == 1`, the bubble shows just the
  label. Multi-tile inexhaustibles still render "N tiles" (that
  number IS informative).
- **20 new tests** in `tests/test_v026_features.py`. Suite total:
  **684 passing** (+20 from v0.25), same 11 pre-existing
  failures (the wheat-farm `food` legacy issues that the v0.18.x
  drop is slated to fix).

**Combat / military note — open for review:**

The military stack as it stands today (post-v0.23.x) gives the
player five JSON-driven unit types with hp / damage / speed / sight
/ range / armour, a working morale-and-retreat behaviour, weapons
consumption on spawn, and a diagnostics chain that surfaces the
root cause when soldiers fail to materialise. What's still queued
from the original v0.23 brief:

  * **Editable unit editor** (per-unit stat tuning that saves back
    to `data/units.json`) — currently still read-only.
  * **Combat events log** in the unit editor.

What v0.26 *did not* attempt, pending a review pass:

  * **Siege mechanics** — towers, walls, ballista-vs-fortification.
    Today ballistae are units; they don't interact with structures
    differently than infantry do.
  * **Formation / tactics** — units fight individually; no
    line-versus-flank logic, no shield-wall buffs.
  * **Enemy AI behaviour** — raids spawn and pathfind to the city
    centre. They don't react to defensive layouts, don't focus-fire
    weak spots, don't retreat under pressure (player units do, via
    morale; enemies don't).
  * **Fortification damage** — fort / wall HP, gradual destruction
    of defensive buildings under sustained attack.

Direction welcome before v0.27 commits to a scope.

**Honestly not shipped — queued for v0.27:**

- **Sprite art for new buildings.** Forum, Consulate, Embassy,
  Caesar Hostel, the three schools, Bath, Dentist, Sanitarium,
  Coliseum, Zoo, the three banks, and the new mine/smelter
  variants all currently use texture fallbacks (Senate art for
  politic buildings, school art for the new schools, market art
  for banks, etc.). They render and play correctly but visually
  alias to their parent — the placeholder generator should fan
  these out.
- **Editable unit editor** (still on the v0.24+ list).
- **Combat events log.**
- **Whatever direction comes out of the military review.**

See `CHANGELOG_v0.26.md` for the full release notes.

---

### v0.27 — Infrastructure expansion (road brushes, bridges, 128×128 maps, editor polish) *(shipped, Drop 1 of 2)*

**Theme:** four player-flagged infrastructure gaps closed in one
drop. The Infra tab finally has something besides a single road
tile, water tiles stopped being movement-impassable forever (you
can build bridges), the map editor can size scenarios up to a
proper city-scale 128×128, and the buildings editor's scalar /
production / consumption rows got the uniform ±10 / ±50 strip
the player asked for after the v0.26 screenshot review.

This is the first half of a two-part release; the v0.28 drop
ships the Trigger editor and per-map scheduled events (engine
plumbing already landed in v0.27 — see "What's next").

**Shipped:**

- **Road brushes** in the Infra palette. Four new building ids
  (`road_h5` / `road_h10` / `road_v5` / `road_v10`) backed by a
  new `placement_brush: {stamp_id, axis, length}` field on the
  `Building` dataclass. A brush is *not* a grid building — the
  click handler resolves it and walks N tiles in the requested
  axis, calling `place_building("road", …)` per tile. Stops at
  the first collision (already-placed tiles stay placed,
  Caesar-3-style forgiving drag). Cost is the brush's `cost`
  field (5× / 10× road cost) charged up-front; partial placement
  doesn't refund.
- **Full bridge implementation.** Eight new building ids: wooden
  / stone × horizontal / vertical × 5-tile / 10-tile. Backed by a
  new `bridges_water: bool` flag on the `Building` dataclass.
  Wooden bridges are cheap with no material cost; stone bridges
  need planks + stone blocks and take 30-45 ticks to build. The
  placement rule is the *opposite* of `needs_terrain="water"`:
  every cell of the footprint must be water (not just one). Two
  new `GameMap` helpers — `is_bridge_at()` and
  `is_passable_for_walker()` — wire bridges into the walker
  passability oracle (water tiles under a finished bridge are
  crossable; under-construction bridges are not). `RoadNetwork`
  treats every cell of a finished bridge's footprint as a road
  tile, so a road butted up against either end flows through to
  the other shore — no special case in the pathfinder.
- **128×128 map ceiling.** Pre-v0.27 the editor's resize ceiling
  was the module-level `GRID_COLS` (40) / `GRID_ROWS` (30); going
  larger would have stepped outside arrays that `services.py`,
  `walkers.py`, and `pathfinding.py` allocated against those
  module constants at import time. v0.27 refactors all three off
  the module constants and onto the **live** `game_map.rows /
  cols`. `ServiceMap` allocates each coverage grid at the real
  map size every `rebuild()`. `Pathfinder._access_tiles` and the
  A* inner loop read bounds via two new helpers
  (`_rows()` / `_cols()`). Eight bounds-check sites in `walkers.py`
  (`Walker.pick_target`, `CombatWalker._step_toward` / `_patrol`,
  `Enemy.pick_target`, raid edge-spawn, `force_raid`,
  `force_raid_punitive`, `_access_tile_for`,
  `_record_delivery_radius`) moved to live game_map dims. The new
  `EDITOR_GRID_MAX = 128` constant is the single source of truth;
  default new-game maps stay 40×30 so the larger allocation only
  applies to resized editor maps.
- **Buildings editor 5-button row.** Each scalar field (cost,
  workers, housing, storage, construction_ticks) renders as
  `-50 / -10 / [value] / +10 / +50` with strict uniform steps
  across every field (was: per-field steps `cost: 10, workers: 1,
  housing: 1, storage: 50, construction: 5`). New action strings
  on the editor button rects: `minus_major:{field}`,
  `minus_minor:{field}`, `plus_minor:{field}`,
  `plus_major:{field}`, all routing through `_editor_dispatch`
  to add/subtract `EDITOR_STEP_MAJOR` (50) or `EDITOR_STEP_MINOR`
  (10), clamped at 0.
- **Production / Consumption row 5-button strip.** Same uniform
  ±10 / ±50 strip with a trailing `[×]` remove button, replacing
  the v0.26 single-step `-` / `+` / `×` layout. Pane width capped
  at 290 px (was ~420), pane height capped at 120 px (was
  flooding to the footer) — the player flagged the v0.26 pane as
  "too big" after the bakery screenshot. Panel height trimmed
  720 (was 760).
- **Two screenshot bugs fixed:** the size row's W / H labels no
  longer share a y-coordinate with the "Size (tiles, 1..10):"
  header (they sit inline with their button strip now), and the
  Production / Consumption pane no longer clips the
  `Construction (ticks)` row (the v0.26 `rows_top` math hardcoded
  `4 * row_stride` while the list had five fields; now uses
  `len(editable_fields)`).
- **Edge-case rules covered:** placement brushes are rejected by
  `GameMap.can_place` so a stale caller can't drop a phantom
  brush-tile on the grid; `_diagnose_placement_failure` returns
  `"Bridge must sit fully on water"` and uses live game_map dims
  (the pre-v0.27 hardcoded `30` / `40` bounds were wrong on
  resized maps); the mapfile load-path grid clamp uses
  `EDITOR_GRID_MAX` so a malformed map claiming `width: 10000`
  is clamped to 128 rather than allocating 100M cells.
- **35 new tests** in `tests/test_v027_features.py`. Suite total:
  **725 passing** (+31 from v0.26), same 11 pre-existing failures
  the v0.18.x economy fix will address. The new tests cover road
  brush registration / placement / partial-collision / treasury /
  editor-mode behavior, bridge placement / passability / road
  network / under-construction blocking, the 128×128 ceiling
  (GameMap supports it, ServiceMap coverage works past col 40,
  pathfinder A*s past col 40, mapfile clamps malformed grids),
  and the editor 5-button row (major/minor steps, clamp at 0,
  uniform across all five scalar fields, prod/cons rows step
  by ±10 / ±50 like the scalars).

**Cut / deferred to v0.28:**

- Trigger editor on the splash menu and per-map scheduled events
  in the map editor (engine plumbing landed in v0.27 already —
  see the v0.28 plan in "What's next" below).
- Bridge sprite art — the eight bridge ids ship with manifest
  entries pointing at `wooden_bridge.jpg` / `stone_bridge.jpg`
  which don't exist on disk; the colored-rectangle fallback
  renders them as a brown-or-grey strip across the water until
  the placeholder generator catches up.

See `CHANGELOG_v0.27.md` for the full release notes.

---

### v0.23.x — Natural textures, military units, per-building lifetime, raw-feature middle-click *(shipped)*

**Theme:** four playtester-flagged gaps closed in one drop. Three
visibility wins (textures actually render, per-building totals, raw
feature inspection) and one big mechanical addition (five JSON-driven
military unit types).

**Shipped:**

- **Natural-resource textures render.** `GameMap._draw_terrain` now
  calls `TextureRegistry.feature(feat)` for every feature-bearing tile
  and draws the PNG (forest, ore veins, fertile soil, groundwater all
  already shipped under `assets/textures/natural/`). The legacy glyph
  rendering (triangle for forest, dots for fertile soil, circle for
  ore) is preserved as the fallback when no art is on disk — so
  modders shipping a minimal sprite pack still get a readable map.
- **Five JSON-driven military unit types** declared in
  `data/units.json` and exposed via the new `units.py` module
  (`UnitRegistry`, `UnitDef`). The five canonical units:
    - **Scout Squadron** (`scout`) — fast, light, wide patrol radius;
      garrisoned in the watchtower.
    - **Light Infantry** (`light_infantry`) — the legacy v0.22
      soldier; default barracks output.
    - **Heavy Infantry Legionary** (`heavy_infantry`) — armoured
      (takes ½ incoming damage), slow, high HP; garrisoned in the
      fort.
    - **Heavy Cavalry** (`heavy_cavalry`) — armoured + fast + highest
      melee damage; garrisoned in the new `fort_cavalry` building.
      Consumes 1 horse per spawn (alongside the v0.22 weapon).
    - **Bowman** (`bowman`) — ranged: engages enemies at Chebyshev 2
      instead of 1, and the enemy can't melee-retaliate from 2 tiles
      away. Garrisoned in the new `archery_range` building.
  Each unit has hp / damage / speed / sight / patrol_radius /
  ranged / armoured / sprite fields in JSON. The `Building` schema
  gained a `spawn_unit` field; `WalkerManager._dispatch_soldiers`
  looks up the declared unit and instantiates a `Soldier` with the
  matching stats. Legacy callers that build a `WalkerManager`
  without a `unit_registry` still spawn the v0.22 generic soldier —
  back-compat invariant preserved.
- **Per-building cumulative produced / consumed totals** tracked on
  `EconomyManager.produced_lifetime[(row, col)]` and
  `consumed_lifetime[(row, col)]`. The info panel now reads these and
  shows two new rows:
    - `Lifetime made: 540 wheat, 12 food` (top 3 by total)
    - `Lifetime used: 28 money` (top 3 by total)
  Totals accumulate every tick the building runs (post-throttle so
  starved / unstaffed buildings don't fake credit). Demolishing a
  building drops the entry via the `building_removed` signal — re-
  placing on the same tile starts fresh. Round-trips through
  saveload; pre-v0.23.x saves load cleanly with empty dicts.
- **Middle-click on raw natural-resource tiles** (no building) shows
  the same extractable bubble used for built extractors. Useful for
  scouting: middle-click an iron vein BEFORE deciding to plant a mine
  and see "200 of 1000 left". Implemented as
  `GameMap.raw_feature_summary(row, col)` returning the same dict
  shape `extractable_summary` already returns. Water tiles report
  an inexhaustible fish summary so the player can preview a fishery
  site too. Empty grass / hills / mountains correctly return None
  (silent no-op, matching the legacy behaviour).
- **24 new tests** in `tests/test_v023x_features.py`. The one
  pre-existing failing test (`test_texture_registry_feature_falls_back_to_features_dir`)
  was updated to assert `reg.feature("forest") is not None` — the
  natural-resource lookup now succeeds, and that's the new lock.

**Honestly not shipped — queued for v0.24:**

- **Construction time** — buildings still appear instantly. v0.24
  picks up the v0.20 brief: `build_ticks` per-building field, an
  `under_construction` state with progress bar, a mason
  specialisation on the stonemason. The buildings editor will gain
  a `Build ticks` row so the player can tune it.
- **Full Unit editor** — the menu entry still opens the read-only
  army roster from v0.22. v0.24 makes the panel editable per the
  v0.23 brief (per-unit HP / damage / speed / sight / sprite,
  saves to `data/units.json`).

See `CHANGELOG_v0.23.x.md` for the full release notes.

### v0.23 — Modal panel mutex, engineer-post staffing gate, discovery-time reserves *(partial drop)*

**Theme:** close five gameplay/observability gaps the player flagged
during v0.22 playtesting. The original v0.23 scope (trade-route
management UI) is queued for a follow-up; the items below took its
release slot.

**Shipped:**

- **Middle-click extractable bubble** now reads
  `"9000 of 85000 left (3 tiles)"` with a coloured progress bar
  beneath. `terrain_feature_state` entries grew from
  `{"reserves": v}` to `{"reserves": v, "initial": v}`; the initial
  value is stamped at seed time and persisted through saveload.
  Legacy 3-tuple save entries fall back to `initial = current
  reserves` so old saves still load.
- **`J` jobs panel** and **`S` city statistics** scroll on
  PgUp/PgDn instead of truncating with `(+N more)`. The diagnostics
  panel's existing PgUp/PgDn from v0.22 keeps its behaviour; the
  dispatcher routes to whichever panel is open. Each panel resets
  its scroll offset to 0 on open.
- **Single-panel-at-a-time** — pressing `J` while the city stats
  panel is up now closes the stats panel before opening jobs. Same
  for every other floating info panel (`G`, `D`, `Z`, `B`, Unit
  editor entry points). Implemented as one helper,
  `_close_other_modal_panels(keep=...)`, called on every panel
  opens — Esc-unwind paths keep the v0.22 ordering intact.
- **Engineer post staffing gate** — `ServiceMap.rebuild()` now
  skips a service-providing building whose worker slot has zero
  citizens filled. So a freshly-built engineer post with no
  citizens *no longer* maintains the surrounding city; decay
  proceeds at full rate until the post is staffed. Buildings with
  `workers_needed == 0` (well, fountain, aqueduct, reservoir) are
  exempt. Existing tests in `test_decay.py` and
  `test_civic_services.py` pass unmodified — they don't populate
  `building_status` so they fall through to the legacy
  always-active path.
- **13 new tests** in `tests/test_v023_features.py`. Suite total:
  **634 passing** (+13 from v0.22's 621), same 10 pre-existing
  failures (the wheat-farm `food` legacy issues that the v0.18.x
  drop is slated to fix).

**Honestly not shipped — queued for the next drop:**

- **New menu Unit Editor** — a balance-tuning panel mirroring the
  buildings editor (per-unit HP / damage / speed / sprite
  filename), with the v0.22 army-roster info folded in as the
  read-only display block. The menu entry currently still opens
  the v0.22 read-only roster.
- **Combat events log** in the unit editor.
- **Trade route management UI** — the original v0.23 scope. *(Eventually shipped as the Commercial roads window in v0.50, reworked through v0.52, and the per-tick route half retired in v0.53 in favour of per-trip voyages — see the v0.53 release-history entry above.)*

See `CHANGELOG_v0.23.md` for the full release notes.

### v0.22 — Production diagnostics + military rework *(shipped)*

**Theme:** make production failures *legible* and rebuild the
military stack so the iron→smelter→weapon chain is a real gate on
soldier production. Two drops:

**Drop 1 (engine):**

- **`diagnostics.py`** — pure-logic root-cause engine. Walks the
  declared chain for any tracked resource (wheat, bread, wood,
  planks, iron, weapons, stone_blocks, tools, wine, oil, pottery,
  *plus* a synthetic `soldiers` chain) stage-by-stage, surfacing the
  *first* broken stage rather than the last visible symptom. A
  weapons failure caused by an unstaffed mine reports the **mine**
  as the root cause, not the weapon smith. 13 distinct failure
  causes are tracked.
- **`DecayManager.recent_collapses`** — rolling 12-entry buffer of
  buildings collapsed from neglect. Diagnostics reads this so a
  `decay_collapsed` cause appears with the building id and tile
  coordinates of the casualty.

**Drop 2 (HUD wiring + military rework):**

- **`D` key — diagnostic modal panel**. Lists every broken chain
  root-cause-first; severity icons (`i`/`!`/`X`) and per-stage
  detail + fix. PgUp/PgDn scroll. Esc/click closes.
- **Smelter chain**. Mine now produces `iron_ore`; new `smelter`
  building refines it into `iron`; weapon_smith and factory
  consume the refined iron. Old saves still load (v12 → v13
  migration is declarative; per-building production dicts are
  read live from the registry).
- **Weapon-consumption-on-soldier-spawn**. `_dispatch_soldiers`
  consumes one `weapons` per spawn; empty stockpile → unarmed
  soldier (still spawns so the player sees the consequence).
- **Soldier morale & retreat**. `armed: bool` and `morale: float`
  attributes. Morale decays on damage and adjacent-ally death
  (Chebyshev≤2 propagation), recovers slowly when peaceful at
  full HP. Below threshold → soldier walks home and swings at
  reduced damage (fighting retreat, not stand-and-die).
- **Unit editor — live roster**. Replaces the v0.21 stub with
  total/armed/unarmed/routing counts, average morale, weapons /
  iron / iron_ore stockpile readout, and one row per soldier
  with HP / morale / position / [ROUT] tag.
- **Recorder schema bump**. JSONL records gain `military`,
  `diagnostics`, `rebellion`, and `caesar` blocks per tick. A
  replay analyst can now ask "did the iron chain break before the
  raid spawned?" with `pandas.json_normalize`.
- **8 new balance tunables** for combat (`COMBAT_UNARMED_DAMAGE_MULT`,
  `COMBAT_MORALE_*`, `COMBAT_RETREAT_*`).
- **39 new tests** across `tests/test_military_chain.py`,
  `tests/test_morale.py`, `tests/test_v022_features.py`. Suite
  total: 627 passing (+39 from v0.22 first drop), same 11
  pre-existing failures.

See `CHANGELOG_v0.22.md` for the full release notes.

### v0.22a — Production diagnostics core (first drop) *(shipped)*

The first drop of v0.22 landed the pure-logic diagnostic engine
without HUD wiring or military rework — those followed in v0.22
proper above. Everything in this sub-release is included in v0.22
and described more concisely there.

- **`diagnostics.py`** — pure-logic root-cause engine. 13 failure
  causes; chain-walker reports root causes first.
- **`DecayManager.recent_collapses`** — bounded 12-entry buffer of
  buildings reaped by decay; diagnostics reads it for the
  `decay_collapsed` cause.
- **15 new tests** in `tests/test_diagnostics.py`. Suite total at
  drop time: 588 passing (+15 from v0.21).

### v0.21 — Tab fixes, debug overlays, extractable bubbles

Slaughterhouse + cheese shop moved to commerce so they fit on the
first page of the tab. Press `Z` opens a happiness equation debug
overlay surfacing every additive term. Middle-click on a mine /
farm / lumber mill / fishery shows an extractable-reserves bubble.
Tick counter added next to the date in the top bar. Terrain
texture loading consolidated. Unit editor became an honest stub
(was a misleading "read-only viewer").

### v0.19.x — Visible resource flow

**Theme:** wire the v0.19 resource-icon set into the world. The
recorder gave the *data* answer; this gives the *visual* one.

**Shipped in this drop:**

- **Cargo overlay on delivery walkers.** Every active
  `DeliveryWalker` paints a small icon of its carried good
  above-and-right of the walker body — a wheat-laden trader looks
  visibly different from a wine-laden trader at any zoom. Sized at
  ~⅔ the walker sprite (11 px). Hash-coloured fallback square if the
  resource PNG is missing.
- **Resource icons on warehouses & granaries.** Up to four resource
  icons drawn along the bottom strip of every storage building's
  footprint, sorted by quantity (alphabetic tiebreak so the layout
  doesn't flicker). Reads from `Storage.contents()` so the world
  view and the inspector always agree.
- **"Goods flow" overlay** — twelfth entry in the `O` cycle. Every
  delivery walker deposits onto its current tile each tick;
  geometric decay (0.85) keeps a recent-traffic trace visible for
  ~3-4 seconds. Bright cyan-green = busy, faint blue = low traffic.
  The palette is *flipped* vs need-overlays — activity is the thing
  the player wants to see, not avoid.
- **`TextureRegistry.resource(id)`** — new accessor mirroring
  `building()` / `walker()` / `feature()`. Reads from
  `assets/textures/resources/<id>.png` (the set the v0.19 generator
  produces). Pre-loaded at startup so the first delivery walker
  doesn't trigger a one-frame stutter.

**New tests (13 added, 0 regressions):**
- `tests/test_v019x_features.py` — cargo-attribute plumbing, flow
  heatmap accumulator and decay, OVERLAY_NAMES contract, storage
  icon constants, resource-PNG-on-disk regression guard.

See `CHANGELOG_v0.19.x.md` for the full release notes.

### v0.19 — Diagnostics, bartering, menu parity

**Theme:** answer the player's "I can't see what's happening" questions
with a real diagnostic recorder, expose international barter as a
first-class menu, finish menu-parity between splash and ESC, and grow
the buildings editor.

**Shipped in this drop:**

- **`R` — tick recorder** — toggle on/off. Each press starts (or stops
  and rotates) a session file at `./data/recordings/session_<ts>.jsonl`.
  Every simulation tick appends one JSON line capturing population,
  treasury, happiness, fed-fraction, nutrient diversity, the full
  resources/production/consumption snapshot, the jobs aggregate,
  per-building rows (with state + reason + workers filled/needed),
  walker counts by role, and any barters/events accumulated since the
  last record. Rationale for JSONL over CSV in `recorder.py`'s module
  docstring; analysis with `pandas.read_json(path, lines=True)`. No
  tick-rate slowdown — disk I/O is sub-millisecond at the v0.19
  schema's ~3 KB/tick.
- **`B` — international bartering menu** — modal overlay, opens with
  the `B` key or from the ESC menu. Players pick a *give* resource and
  a *receive* resource from two columns, set a quantity (-50/-10/-5/
  -1/+1/+5/+10/+50 step buttons + live total), and confirm. The
  exchange ratio is computed from `bartering.STOCK_PRICES` (a 25-entry
  table the player or modder can edit directly in `bartering.py`),
  with a flat 100-gold transaction fee per the spec. Full preview of
  the trade outcome before commit; failure modes (insufficient stock,
  insufficient treasury, qty rounds to zero) surface as red
  notifications.
- **"Play custom map" splash button** — opens a file picker listing
  every `.json` in `./data/maps/`. Clicking a row tears down to a
  clean game state, loads the map via `mapfile.load_map`, and
  switches to playing mode. Esc dismisses. Sample map shipped at
  `data/maps/sample_riverside_hub.json` round-trips through the
  picker.
- **Splash ↔ ESC menu parity** — both now expose Map editor,
  Buildings editor, and Unit editor entries. The ESC menu also gets
  a Bartering shortcut (mirror of the `B` key). Splash menu grew
  from 6 to 9 entries; ESC menu grew from 6 to 9 too.
- **Expanded Buildings editor** — beyond the original four scalars
  (cost / workers / housing / storage), the editor now shows the
  building's sprite filename (read-only — the supported workflow is
  to drop a PNG at the printed path), its full production /
  consumption summary, and editable +/- buttons for the *primary
  output qty* and *primary input qty* (the largest entry in each
  dict, deterministic alphabetic tiebreak). Buildings without
  production or consumption hide the corresponding row instead of
  showing a meaningless "0 +/-" control. Save still writes the
  whole `data/buildings.json` atomically and hot-reloads the
  registry.
- **`tools/generate_resource_textures.py`** — auto-generates one PNG
  per priced resource at `./assets/textures/resources/<id>.png` (32×32,
  flat colour + dark rim + 2-letter ID). Driven by the
  `bartering.STOCK_PRICES` table so the icon set always matches the
  trade table. 25 icons, ~100 KB total. Run once after install; the
  game's missing-texture fallback covers the unrun case.
- **Read-only Unit editor** — surfaces the four walker roles
  (worker / trader / soldier / citizen) plus the implicit subclasses
  (delivery / enemy) in a single panel, lists the building
  specializations the engine actually distinguishes (mason, baker,
  miller, miner, smith, engineer, prefect, doctor, teacher, priest,
  trader, senator), and labels itself "read-only" honestly — walker
  defs still live in `walkers.py`, JSON-driven units are deferred to
  v0.20.
- **R-key remapping** — the previous `R` ("buy next trade route")
  was retired; trade routes still seed at game start, and v0.22's
  trade panel will replace the keyboard fast-path. This freed `R`
  for the recorder, which the player needs vastly more often.

**New tests (23 added, 0 regressions):**
- `tests/test_bartering.py` (10 tests) — table integrity, ratio
  math, all failure modes.
- `tests/test_recorder.py` (5 tests) — JSONL roundtrip, barter
  drain, idempotent stop, redirected `RECORDINGS_DIR`.
- `tests/test_v019_features.py` (8 tests) — splash/ESC menu parity
  via source scan, primary-output extraction in the editor, R-key
  toggle, generator importability.

**Honestly not shipped — a checklist for v0.19.x or later:**

- **Walkers seeking jobs.** The brief asks for citizens to "look
  for job, working, transporting stuff" with per-walker IDs. This
  is a multi-day refactor of `walkers.py` (1,018 lines) — every
  walker subclass would need a state machine and saveload would
  need the new fields. Out of scope for one drop. The recorder
  *does* let the player *see* that walkers are random-walking, so
  the diagnosis the brief asks for is now possible even before
  the fix.
- **Building construction time.** The brief asks for a build-time
  bar with mason specialization. Construction is currently
  instant; making it a multi-tick process touches `_try_place`,
  saveload, decay, *and* needs a new "under_construction" status
  in `building_status.py`. Tracked for v0.20.
- **Resources visibly extracted/transported.** Resource sprites are
  now generated; wiring them to walker overlays + warehouse icons
  is one PR's worth of `walkers.py.draw()` and storage rendering
  work. Tracked for v0.19.x.
- **Removing legacy `food`.** The brief asks for `food` to go away.
  The v0.18 work removed it from production but the eating-order
  fallback in `economy.py` (lines 297–334) still reads it second
  in the eat-order, and tests pin the old shape. Properly closing
  this is the v0.18.x economy chunk that was already on the
  roadmap. Listed in known test failures (11 pre-existing tests
  that hard-code `food` consumption).

**Pre-existing test failures (unchanged from v0.18):** 11 tests in
`test_balance_audit`, `test_nutrients_v16`, `test_road_network`,
`test_stats`, `test_supply_chain_v11`, `test_v013_features`, and
`test_water_and_starvation` still pin pre-nutrient-refactor
assumptions where the wheat farm produced `food` directly. Fixing
these is the v0.18.x economy chunk; they're called out in
CHANGELOG_v0.18 already.

### v0.18 — Terrain layer & food-chain spine *(in progress)*

**Theme:** finish the supply chain by collapsing legacy `food`, and
extend the terrain palette so the editor can paint everything the
economy actually models.

**Shipped in this drop:**

- **`desert` terrain** — a fifth terrain id (`TERRAIN_DESERT = 5`).
  Unbuildable for every building footprint (treated like
  hills/mountains in `can_place`) but **passable** for walkers — they
  cross it freely the same way they cross grass. Renders as a sandy
  yellow-brown tile (`COLOR_DESERT = (200, 175, 110)`) with a texture
  fallback for asset-less environments. Round-trips through
  `mapfile.py` automatically (terrain serialization is generic over
  ids).
- **`iron_vein` feature** — a first-class feature alongside
  `gold_vein` and `copper_vein`, with its own colour
  (`(110, 110, 130)` — iron grey-blue) and label ("Iron vein").
  Reserves default to `400.0` (same depletion budget as gold/copper —
  iron is consumed at a comparable cadence in the factory and
  weapon-smith chains). The mine now accepts any of
  `iron_vein` / `gold_vein` / `copper_vein` in its
  `needs_feature` set.
- **Wheat farm produces wheat only** — the legacy `food: 8`
  subsistence output was removed. Farms now grow wheat, which goes to
  a windmill (flour) and then a bakery (bread). The +50% fertile-soil
  yield bonus is preserved.
- **Market and tavern consume `bread`** — the market now sells
  `bread: 3` (was `food: 3`) for denarii and the tavern consumes
  `bread: 2 + wine: 1` (was `food: 2 + wine: 1`). This makes the
  bread chain (wheat → flour → bread) the canonical grain pipeline
  rather than a parallel track to a generic `food` resource.

**Deferred to a later v0.18.x drop:**

- Splash-menu reorganisation: a "Buildings editor" entry, an in-game
  "Save game" button with name input, and a save-file picker for the
  splash "Load game" button.
- Map-editor left-side palette listing all five terrains
  (grass / water / hills / mountains / desert) and seven features
  (forest / fertile / gold / copper / stone / groundwater / iron).
- HUD population panel showing `Pop X / Y (free Z)` and a storage
  breakdown panel listing granary slots vs. warehouse slots.
- Removing the legacy `food` HUD line and finishing the
  `food → bread` migration in `economy.py` (the eating-order code at
  lines 288–349 still keeps `food` in the legacy fallback list).
- Version-label bump on the splash screen from "v0.17" → "v0.18".
- Test updates: existing tests that hard-code `food` consumption for
  market/tavern need updating; new tests for desert placement,
  iron-vein recognition, and bread substitution.

These deferrals are tracked in `tests/test_v17_features.py` (the v0.17
suite still passes) and listed in `CHANGELOG_v0.18.md` so the next
session can pick up the same checklist.

### v0.17 — Polish & Visibility

**Theme:** make v0.16 readable.

- **HUD: per-nutrient satiety** — replaced the single `Food: N` line
  with a `── Satiety ──` section listing all 10 nutrients with
  per-nutrient percentage of population demand covered, colour-toned
  (green ≥ 100%, warn 25–99%, bad < 25%).
- **Currency: `gold` everywhere** — every player-facing string now
  says "gold" instead of the abbreviation "dn".
- **Texture manifest** at `data/textures.json` mapping building ids
  *and* names to filenames. Both keys work simultaneously. Supports
  `.png .jpg .jpeg .webp`. Fallback to legacy `<id>.<ext>` lookup
  when no manifest entry.
- **Assets folder structure** at `./assets/textures/` with subfolders
  for buildings, terrain, features, walkers, houses, ui, plus a
  README explaining the conventions.
- **Job statistics panel** — press `J` for a modal showing population
  pool, demand, filled, jobless, per-role distribution, and a
  short-staffed-first list of every worker-using building.
- **Jobless heatmap overlay** — 11th entry in the `O` key overlay
  cycle. Red/amber/faint shading of building footprints by shortfall
  ratio.
- **Hover tooltip while paused** — re-drawn on top of the pause dim
  so spacebar-pause-and-look-around works.
- **Load game button fix** — notifications now survive the
  `load_game` call's `game_time` overwrite. Default notification
  duration bumped 25 → 50 ticks. Better feedback wording with
  loaded year/pop and explicit save path on miss.

See `CHANGELOG_v0.17.md` for the full release notes.

### v0.16 — Nutrients & Maps

**Theme:** richer food chain + scenario authoring tools.

- 10-nutrient food system replacing single-good `food`.
- 11 new buildings (the eight missing nutrient producers + butcher
  shop + fishmonger + stable).
- Nutrient diversity bonus on happiness and pop growth.
- Granary/warehouse type-based default storage.
- Map editor: free-placement scenario authoring.
- `./data/maps/*.json` schema with versioned forward migration.
- Sample map shipped at `data/maps/sample_riverside_hub.json`.

See `CHANGELOG_v0.16.md` for the full release notes.

### v0.15

- Buildings editor (in-game cost/workers/housing/storage tweaking).
- Hills + mountains terrain.
- Gallic War scenario (alternate starter layout).
- Gross production telemetry.

### v0.14

- Terrain feature depletion (forests + ore veins exhaust).
- World hover tooltip.
- Per-tile stats panel.

### v0.13

- Construction materials gate (planks/stone_blocks for civic
  buildings).
- Worker wages.
- Terrain features (forest, fertile soil, ore veins, groundwater).
- Feature yield bonus on farms.

### v0.12

- Building activity classification (active / partial / starved /
  unstaffed / disconnected).
- Per-building inspector with status + reason.
- Flow graph visualisation.

### v0.11

- Supply chain rework: workshop → lumber_mill, sawmill (planks),
  windmill (flour), wheat → flour → bread chain.
- Per-warehouse `accepts` filter.

### v0.10

- v0.10 was an internal cleanup pass — no headline feature.

### v0.9

- Per-warehouse storage allocation (visible in inspector).
- Building decay & engineer post maintenance.

### v0.8

- Caesar requests (tribute deadlines).
- Diplomacy points.
- Rebellion pressure.
- Water gating on farms.

### v0.7

- Splash screen.
- Stats window.
- Save format versioning.

### v0.6

- Texture system with PNG + colour fallback.
- Education + health services.
- Combat (soldiers, raids).
- Mini-map.
- Long-form graphs window.

### v0.5

- Delivery walkers (oil/wine/pottery distribution).
- Tier-3 + tier-4 house evolution gates.

### v0.4

- House evolution (shack → insula → domus → villa).
- Service map (water/food/entertainment/religion radii).
- Tabbed building palette.
- Aqueducts + reservoirs.

### v0.3 and earlier

- Basic city builder loop (build → tax → pop → happiness).
- JSON-driven building registry.
- Walker-based road network.

---

## What's next

### v0.40+ — after the naval-infrastructure drop (in progress)

With ships, shipyards, and the building set in place, the threads in priority order:

- **Harbor transfer (the missing half of v0.39).** *(PoC shipped in v0.40 — see `CHANGELOG_v0.40.md`.)* The first slice is in: trade ships now load real cargo from the goods ledger (the harbor buffer in miniature), debiting it, and export income is proportional to what they carried — no stock, no phantom income. **Still to do for the full thread:** a per-warehouse buffer (the PoC pulls from the global `economy.resources` ledger, which Storage is only a derived view of, rather than from the nearest harbor's own stock); idle ships berthing in a harbor's `ship_slots`; import unload on the return leg; and a military harbor embarking soldiers from a barracks/fort onto a transport.
- **In-game verification + ship/building art.** *(Headless verification shipped in v0.40.)* Warship-vs-coastal-raider combat and transport troop-disembark are now covered by tests that drive the real `WalkerManager` over coastal geometry (`tests/test_naval_battle_verification.py`). The mare_nostrum intro cinematics, which previously never fired, now resolve and render end to end (cutscene library merge + slide-schema normalization; see changelog). **Still requires a GL context:** confirming the same in the running game (a shipyard visibly launching a ship, the RPG panel resolving). Ship sprites and the six new building textures remain placeholders (renderer falls back to coloured rects).
- **Shipyard build queue (player-driven launches).** *(Shipped in v0.41 — see `CHANGELOG_v0.41.md`.)* Shipyards no longer auto-launch on a timer; the player queues hulls from the building info panel (a `+ Build Cargo ship` / `+ Build Warship` button, a queue list with per-hull progress + remove), and `_process_shipyards` drains the queue one hull at a time. The queue persists in `building_state` (no save-schema bump). **Still needs a GL context** to confirm the panel renders and clicks register in the running game. Part of the four-piece naval-expansion ask (queue / port-supply-chain / harbor-berths / ship-pathfinding); the remaining three are below.
- **Port ↔ granary/warehouse supply chain.** *(Shipped in v0.44 — see `CHANGELOG_v0.44.md`.)* Cargo now routes by item nature via a shared classifier (`storage.store_class_for_good`): nutrients ↔ granary, other goods ↔ warehouse. `WalkerManager.nearest_store_for_good` finds the right store spatially, and a loaded trade ship records its `cargo_store_class`. The classifier is shared with the per-tick `distribute()` routing so they can't drift. **Possible next:** a visible goods-flow walker between port and store, and inspector wiring.
- **Harbors as berths.** *(Shipped in v0.45 — see `CHANGELOG_v0.45.md`.)* `harbor_berth_occupancy` computes per-harbor berth use each tick (idle, role-matched ships in the footprint-adjacent water zone, capped at `ship_slots`); the harbor info panel shows `Berths: N/10`. A computed view — no save-schema change.
- **Naval combat depth.** *(Shipped in v0.45.)* `EnemyShip` (a sea-sailing `Enemy` subclass) is intercepted and sunk by friendly warships via the existing combat pass, and `_resolve_sea_raids` lets raiders ram trade ships — making warship escort of merchant lanes meaningful. **Follow-up:** a scripted enemy-ship raid trigger (`force_raid` with a naval side) to actually dispatch them in a scenario.
- **Ship pathfinding (foundational gap).** *(BFS PoC v0.43; A* + adaptive cap + route overlay v0.44.)* Ships follow a real A* shortest path over the sea domain (`find_sea_path`), with a lazy path cache, graceful greedy fallback when a goal is unreachable, and a 'P'-key debug overlay drawing routes in world space. Proven to escape concave-coastline traps and to solve large open maps the BFS cap rejected.

**Misc shipped in v0.42:** building gold upkeep (1/tick on the six naval buildings; new `upkeep` field + deduction in `economy.update`), and ship build time cut to ~30s for dev feedback.
- **Directional / scripted raids.** Expose `force_raid` as a trigger effect with a `side` parameter so the Mare Nostrum southern-raid pillar fires real enemy waves rather than the economy-event stand-in.
- **The inert unit-editor fields (audit 7.2)** and the **`Storage.distribute` optimisation (audit 9.1)** remain unblocked.

### Inter-city commerce, next steps (post-v0.53)

The per-trip voyage layer is now the *only* inter-city export path. Threads that build on it:

- **Terrestrial caravans (per-trip land trade).** The land counterpart to sea voyages: a linked inland (or any) city reachable by road sends a caravan that loads goods, travels for a distance-scaled interval, sells on arrival, and returns — gold per completed trip, never per tick, mirroring `voyages.py`. Design intent is a sibling pure-logic module (a `CaravanManager` consuming a `CaravanWorld` adapter for road reachability / pack-animal availability / banditry on the route, paralleling `VoyageWorld`'s ship/stock/war/piracy checks), so the two share the trip-economy shape and a future unified "commerce" panel. Gating ideas: a land trade-post building as the caravan's origin berth; route safety tied to road condition and enemy land walkers.
- **Per-trip commerce polish.** A unified commerce summary (sea voyages + land caravans in one panel); import legs (a returning trip can carry a needed good *in*, not just sell out); and visible-walker parity for caravans (as v0.52 did for `TradeShip`).
- **Cleanup:** the deprecated `CommercialRoadManager` route stubs (`add_route` etc.) can be deleted outright once no external script/mod is expected to call them; until then they remain harmless no-ops.

### Military-unit command interface (multi-drop campaign, started v0.47)

An RTS-style command layer for land + naval units: single-click + drag-box + 0-9 control-group selection, and the 12-verb vocabulary (group, split, join, embark, disembark, navpoint/move, attack, defend, patrol, retreat, siege, rest, scout). Sequencing:

- **v0.47 (shipped) — foundation.** `commands.py`: the selection model, control groups, `Order`/`CommandState`, the full `Verb` enum with land/naval legality, and `CommandManager`. MOVE/STOP resolve into soldier movement; the drag-box backend (`player_units_in_rect`) is in. See `CHANGELOG_v0.47.md`.
- **v0.48 (shipped) — movement orders + selection UI.** Command mode (key 'U'): click/drag/shift selection, Ctrl+0-9 control groups, right-click move/attack, selection overlay. Ships obey MOVE/STOP; embark/disembark backend (`command_embark`/`command_disembark`) on the trireme's 10 slots. See `CHANGELOG_v0.48.md`. *Still folded-not-resolved: explicit GROUP/SPLIT/JOIN formation orders.*
- **v0.49 (shipped) — cheap combat stances.** DEFEND (hold), REST, PATROL, RETREAT as persistent per-soldier stances reusing existing morale/home/patrol machinery; hotkeys H/Y/J/K in command mode. See `CHANGELOG_v0.49.md`. Stances regrouped by cost (the re-plan): the cheap four ship here.
- **Formations drop (planned) — group/split/join.** Cohesive-movement formations as their own drop (deferred from v0.48; the control-group *selection* already works, formation *movement* doesn't).
- **Expensive stances (planned) — siege + scout.** SIEGE needs an enemy-building/target model that isn't built; SCOUT needs fog-of-war/vision that doesn't exist. Likely preceded by building those systems. A persistent ATTACK-pursuit stance can join here.

The detailed older planning notes below predate v0.38–v0.39 and are kept for historical context; treat the block above as the current direction.

The order below reflects current intent — *not* a hard schedule. We
prioritise mechanics that close existing loops over net-new systems.

### v0.30 — Combat-system review, Round 2 (military review queue)

**Theme:** the v0.29 military review closed all five player-flagged
items (cavalry shock, ranged buffs, persistent fire, fireman walker,
editor cap). The review also surfaced four follow-ups that didn't fit
the v0.29 scope but were noted as next-drop candidates. v0.30 picks
them up.

**Planned:**

- **Per-soldier experience.** A `Soldier` gains a small damage bonus
  per N strikes survived. New `experience: int = 0` field on the
  `Soldier` class, incremented in `take_damage` (every successful
  parry) and in `_resolve_combat` (every dealt strike). Bonus
  schedule: +1 damage per 10 XP, capped at +5 (so a 50-XP veteran
  caps out — meaningful but not game-breaking). Default OFF: a new
  `COMBAT_EXPERIENCE_ENABLED: bool = False` constant in `balance.py`
  gates the entire mechanic so existing scenarios don't shift
  silently. Save format adds the field to walker snapshots; loading
  an old save fills it with 0.
- **Enemy type diversification.** Two new entries in
  `data/units.json` for the *enemy* side:
  - `raider_chief` — armoured, HP 80, damage 14. A raid waves can
    spawn one chief per N regulars (configurable via
    `RAID_CHIEF_RATIO` in `balance.py`). Visually distinct sprite.
  - `enemy_archer` — ranged=True, HP 25, damage 9. Engages player
    soldiers at Chebyshev 2 like a friendly bowman, creating a real
    reason to advance with mixed melee + ranged formations.
  Both reuse the existing `Enemy` class — the new variation is
  purely stat-driven via the unit registry (the dispatch in
  `_maybe_spawn_raid` learns to consult the registry for raid
  unit ids).
- **Tower garrison rebalance.** Watchtower's `spawn_unit` is
  currently `"scout"` (damage 6) — too thin for wall defence
  against the v0.28 raid waves. Three options, decision pending
  playtester feedback:
  1. Bump scout damage 6 → 9 (keeps watchtower light, brings it
     in line with bowman now-buffed-to-11).
  2. Change watchtower's `spawn_unit` to `"bowman"` (towers become
     archer platforms — a stronger but more equipment-hungry unit
     since bowmen consume weapons).
  3. Add a new `"tower_archer"` unit type (in-between stats).
- **Fire spread to adjacent buildings.** Currently fire is
  localised to the originally-hit footprint. v0.30 adds a
  Chebyshev-1 spread rule: each tick, every on-fire building has a
  `COMBAT_FIRE_SPREAD_CHANCE` chance per adjacent flammable
  building of igniting it. Creates catastrophic-raid dynamics where
  one ignored fire takes out a city block. Default chance low
  enough that an attentive player with adequate prefecture coverage
  isn't punished. Stone vs. wooden materials may eventually gate
  flammability (currently every building is "flammable"); deferred
  until material tags exist as a first-class concept.

**Tests** in `tests/test_v030_features.py`:

- Experience gain on successful strikes; cap at +5.
- Experience feature gated off by default (`COMBAT_EXPERIENCE_ENABLED
  = False` produces zero bonus regardless of XP).
- Raid spawn includes a chief at the right ratio.
- Enemy archer engages at Chebyshev 2.
- Tower spawn unit matches the rebalance decision.
- Fire spreads to an adjacent Chebyshev-1 building when the spread
  chance is forced to 1.0; doesn't spread at 0.0.
- Spread respects buildings that are already destroyed (no
  re-igniting empty tiles).

**Cut from v0.30 (deferred further):**

- Material-tag system for flammability. The "everything is
  flammable" placeholder is fine for one drop; a material taxonomy
  (wood / stone / mixed) is a larger refactor across
  `data/buildings.json` that ships when art assets get a parallel
  pass.

### v0.24 — Construction time + full Unit editor

Both items appear in this section because they were both promised in
earlier drops (the v0.20 brief for construction, the v0.23.x brief
for the editable Unit editor) and both were deferred. The v0.23.x
release closed four other gaps the player flagged; these two are next.

**Construction time** turns building placement from "instant
spawn" into a multi-tick build:

- New `build_ticks` field per building in `data/buildings.json`
  (default: `0` = instant, matching today's behaviour so old data
  doesn't change behaviour silently).
- `place_building` stamps an `under_construction: True, build_progress: 0`
  state into `building_state[(row, col)]`. Each tick increments
  `build_progress`; on reaching `build_ticks` the flag clears and
  the building activates normally.
- Renderer draws a progress bar over the footprint while the
  building is under construction (similar to the v0.23 discovery-
  reserves bar), uses a desaturated tint of the building's base
  colour, and skips the texture (so the player visually
  distinguishes WIP from finished).
- Economy / status / decay / services all treat under-construction
  buildings as inactive — no production, no consumption, no
  service emission, no decay. The classifier in
  `building_status.py` gains a new `under_construction` state.
- Builder walker (the v0.20 brief's "mason"): a citizen spawned
  from each WIP footprint, walks to the site, and accelerates
  construction within its service radius. Out of scope for the
  first cut; ships as a stretch goal — the base mechanic works
  without it (ticks fill at a flat rate).
- Buildings editor gains a `Build ticks` row alongside cost / workers
  so authors can tune per-building build times without leaving the
  game.

**Full Unit editor** picks up what the v0.23.x JSON-driven unit
registry left half-done — the registry exists, but there's no UI
to edit it:

- Mirror the buildings editor: scrollable list of units on the left,
  scalar editor (HP / damage / speed / sight / patrol_radius) in the
  middle, boolean toggles for `ranged` / `armoured`, sprite filename
  input on the right.
- Save writes back to `data/units.json` atomically (same `.tmp` →
  `rename` pattern the buildings editor uses).
- Reload the running WalkerManager's `unit_registry` on save so the
  next spawned soldier picks up the new stats — no restart required.
- Fold the v0.22 read-only army roster (live counts / morale /
  per-soldier rows) into the bottom of the panel so the editor
  page is also the diagnostics page.
- **Combat events log** — a scrollable log of recent combat hits
  (attacker, defender, damage, location) read from
  `WalkerManager.combat_events()`. Pinned to the bottom of the unit
  editor panel; persists across panel open/close.

### v0.25 — Walker job assignment

The deepest deferred item from v0.19. Today's walkers random-walk
even when they're labelled "worker". v0.21 makes the labels real:

- Per-walker stable id (UUID4, persisted through saveload).
- Each `worker_role`-bearing walker has a *home* (the building that
  spawned it) and a *workplace* (the building it's currently
  assigned to). At spawn it picks the nearest understaffed building
  whose `worker_role` matches.
- Workers commute home → workplace daily; while at the workplace
  they contribute to that building's workers_filled counter; while
  away they don't.
- This collapses the v0.13 wage-payment gating into something the
  player can actually watch happen on screen.

This is *the* gameplay-feel improvement on the roadmap. It's last
because every preceding release built infrastructure for it (jobs
panel, building status, recorder, walker subclass split).

### v0.22 — Production diagnostics + military *(shipped)*

See *Release history* above. The diagnostics engine, the `D` panel,
the smelter chain, weapon-consumption gating, soldier morale and
retreat, the live army roster, and the recorder schema bump all
landed. 39 new tests, 627 passing total.

### v0.23 — Modal panel mutex, engineer-post staffing, discovery reserves *(partial drop, shipped)*

See *Release history* above. Five gameplay/observability gaps from
v0.22 playtesting closed; trade route management UI was punted to
the next drop. 13 new tests, 634 passing total.

### v0.23.x — Unit editor (full) + trade route UI

The follow-up release picks up what v0.23 deferred:

- **New menu Unit editor** — a balance-tuning panel mirroring the
  buildings editor. Per-unit (worker / trader / citizen / soldier
  / enemy) HP, damage, speed, sight, sprite filename, and morale
  defaults. Saves to a new `data/units.json`; walkers.py reads
  from it at construction. The v0.22 army-roster info (live counts
  / morale / stockpile / per-soldier rows) becomes a read-only
  display block on the soldier tab so the editor and the roster
  share one menu entry.
- **Trade route management UI** — the original v0.23 scope. A
  trade panel listing every route with current rate / profit,
  route up/down toggling, per-route quantity sliders, integration
  with the bartering menu so they share the price table.
  *(Status: superseded. This landed as the Commercial roads window
  in v0.50 — but the per-tick rate/quantity model described here was
  retired in v0.53. Inter-city commerce is now per-trip only; the
  panel sets a per-city voyage good rather than per-tick routes. See
  the v0.53 release-history entry.)*

### v0.24 — Save-file picker (full) *(was v0.23)*

The in-game ESC menu's *Load game* (and the splash *Load game*)
currently load a single hard-coded slot — `./saves/quicksave.json`.
The "Play custom map" picker added in v0.19 is the template for
the full save picker: rename, delete, duplicate, sort by date, and
a "create from current map" shortcut.

### v0.25 — Map editor budget panel *(was v0.24)*

The map editor saves a budget snapshot of whatever the current
in-memory economy holds, but it doesn't currently let the author
*edit* it. Adds a side panel with per-nutrient sliders, per-stock
sliders, and population/treasury/calendar editors. Schema in
`mapfile.py` already supports all of these.

### v0.26 — Terrain painting in the editor *(was v0.25)*

Currently the editor lets you place buildings on the procedural
terrain layout. Adds terrain-painting tools to the toolbar — brushes
for water / hills / mountains / desert / grass / forest / ore vein /
fertile soil / groundwater, variable size, region flatten.

### v0.27 — Resources / textures editor

Currently missing from both the splash menu and the ESC menu, despite
the parity work in v0.19 between the two. Adds an editor that lets
the player (or a modder, or a scenario author) edit the *terrain
texture set* and the *resource definitions* without dropping into
JSON.

The motivation is concrete: the v0.19 buildings editor lets the
player rebind a building's sprite filename, but there's no equivalent
flow for terrain (grass / water / hills / mountains / desert) or for
the 25-resource set the bartering menu and the cargo-icon overlay
read from. A modder swapping in a new sprite pack today has to
hand-edit `data/textures.json` and `data/buildings.json` and risk
a typo bricking the load. The editor closes that gap.

Concrete deliverables:

- **Resources tab** — one row per known resource (the `STOCK_PRICES`
  table in `bartering.py`, plus any non-priced intermediates like
  `iron_ore`). Each row exposes:
    - sprite filename (text input + file-picker into
      `assets/textures/resources/`),
    - barter price (the `STOCK_PRICES` value),
    - whether it's a nutrient (granary-side) or a non-nutrient
      (warehouse-side),
    - house-tier gating (which nutrient sets it satisfies, mirror
      of the existing tier requirements in `balance.py`).
- **Terrain tab** — one row per terrain type (grass, water, hills,
  mountains, desert) plus the feature overlays (forest, fertile
  soil, ore veins, groundwater). Each row exposes:
    - sprite filename + file-picker,
    - movement effect (passable / impassable / variable cost),
    - feature reserves (default depletion amount for the v0.11
      reserve system).
- **Effects tab** — a dial for each terrain effect on top of the
  base movement / building-eligibility rules. Examples: hills
  reduce farm yield by N%, fertile soil increases farm yield by
  N%, water adjacency boosts fishery output, etc. Sources the
  existing balance constants (`FERTILE_SOIL_BONUS`,
  `HILLS_FARM_PENALTY`, etc.) so the editor is a UI for what's
  already a balance.py constant rather than a new system.
- **Save / cancel / reload-defaults** buttons in the toolbar; a
  preview tile that re-renders as the player edits sprite paths so
  they can see the texture before committing.

Splash + ESC menu both gain a "Resources editor" entry alongside
the existing Map / Buildings / Unit editors — keeping the v0.19
parity invariant intact.

The work is mostly UI assembly on top of existing data: the
resource list lives in `bartering.STOCK_PRICES`; the terrain texture
filenames live in `data/textures.json`; the effects already live in
`balance.py`. The editor is a write-back layer over those three
data sources.

---

## Deliberately deferred

These are reasonable asks that aren't currently on the roadmap, with
notes on why:

### Multiple difficulty levels

Caesar III shipped with five difficulties. We could do this via
`Balance` overrides keyed off a difficulty enum. We've deferred it
because the v0.8 economy is still being tuned — locking in
"hard mode" before "normal mode" balance is settled would mean
re-tuning twice.

### Isometric rendering

The grid renders as a top-down 2D layout. An isometric view would be
nicer to look at but is purely cosmetic; it doesn't unlock any new
gameplay. The geometry layer (`game_map.grid_to_world` /
`world_to_grid`) is already abstracted to make this swap cheap when
we want it.

### Multi-tile aqueducts as bridges

Aqueducts are 1×1 cells today; they can't span water without sitting
in the water tile. Real aqueducts are bridges. The v0.4 water layer
intentionally simplified this; revisiting needs a small pathfinding
extension.

### Scenario campaigns

A list of maps the player plays in sequence with shared progression.
We have everything for this *except* the inter-scenario state
transfer (treasury carryover, Caesar reputation, retired veterans).
Holding off until the budget panel (v0.20) lands so authors have a
proper tool first.

### MOD framework

Modders today can edit `data/buildings.json` and `data/events.json`
directly. A proper mod system with discoverable mod packs, dependency
declarations, and load order is a significant engineering project
that we'd want to scope deliberately rather than bolt on.

### Multiplayer

Out of scope. The architecture (single-process, single-clock,
direct memory mutation) would need a substantial rewrite.

---

## Project hygiene

Things we'd like to clean up but that aren't user-visible:

- `game_window.py` is now **5,144 lines** (was 4,998 at v0.19 entry).
  v0.19.x added ~150 lines for the storage-icon overlay, the
  goods-flow overlay branch in `_overlay_metric` / `_draw_overlay`,
  and resource-texture preloading. The split into rendering / input
  / state-machine modules is now a hard requirement — the next
  feature chunk (v0.20) should start with that refactor before
  adding more UI panels.
- The `walkers.py` module (1,141 lines) similarly wants splitting
  by walker type — and v0.20's JSON-driven units refactor is the
  natural moment to do it.
- `BALANCE_AUDIT.txt` is hand-regenerated from `tools/balance_audit.py`.
  A CI step that auto-regenerates on every PR would catch
  drift.
- Test files have grown organically — 36 test files at v0.19.x, with
  `test_v019x_features.py` joining the `test_v01N_features.py`
  pattern. A `tests/v0xx/` subdirectory pattern would make it
  clearer which tests belong to which release's behaviour.
- ~~11 pre-existing test failures~~ **(resolved in v0.38).** The stale farm-output tests that hard-coded the pre-v0.34 `food: 8` farm chain — across `test_water_and_starvation`, `test_v013_features`, `test_stats`, and `test_road_network` — were rewritten to assert on `wheat` / `gross_production`, and the rotted `test_debug_logging` mock was replaced with a defaulting namespace. The suite is now fully green (1018 passed, 0 failed). The lesson logged for next time: don't freeze a red baseline — a permanently-failing suite hides new regressions, which is exactly why the audit (Chapter 7.5 / 10.2) flagged it.

These are technical debt items, not features — we'll tackle them
between feature releases when one starts to actively block work.
