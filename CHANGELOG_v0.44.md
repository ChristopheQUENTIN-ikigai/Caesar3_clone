# Changelog — v0.44

**Theme:** two threads in one drop — expand ship pathfinding (A* + route
overlay), and build port↔granary/warehouse supply-chain routing on top.

## Part 1 — Pathfinding expansion

### BFS → A*

`find_sea_path` now uses an A* priority queue ordered by `g + Manhattan`.
On the 4-connected unit-cost grid the Manhattan heuristic is admissible
and consistent, so A* still returns a **shortest** path — but it explores
a directed wedge toward the goal instead of BFS's omnidirectional flood.

- All v0.43 path tests still pass unchanged (same signature, same return
  contract).
- **Fixes the large-map limit:** v0.43 BFS tripped its fixed 4000 cap
  corner-to-corner on a 100×100 open sea and returned None; A* now finds
  it. Benchmarks (open-sea worst case): 40×30 ≈ 2.7 ms, 60×60 ≈ 8 ms,
  100×100 ≈ 24 ms. Real maps with coastlines prune far more than open
  sea (A*'s pathological case).

### Adaptive expansion cap

`max_expansions` now defaults to `None` → a cap scaled to map area
(`_sea_path_cap`, at least the historical 4000, at least rows×cols), so a
reachable far goal on a big map isn't a false negative. An explicit int
still wins (tests use a tiny cap to exercise the bail-out).

### Ship route debug overlay (key **P**)

`_draw_ship_paths` draws each ship's cached sea path as a world-space
polyline, with a goal ring and a next-step dot. Non-modal toggle on 'P'
(mirrors the 'Z' happiness overlay). Lets you *see* routes pan/zoom with
the map and confirm ships route around coastlines.

## Part 2 — Port ↔ granary/warehouse supply chain

### Shared routing classifier

New single source of truth in `storage.py`:

- `store_class_for_good(good)` → `"granary"` for nutrients (bread,
  vegetables, fish, wine, …) or `"warehouse"` for everything else.
- `is_nutrient_good(good)` convenience predicate.

These mirror the type-based accept rule `distribute()` already applies
(granary accepts `NUTRIENTS_SET`; warehouse rejects nutrients), centralised
so the per-tick distribute path and the new port-routing path can't drift.

### Spatial routing + ship cargo class

- `WalkerManager.nearest_store_for_good(game_map, frm, good)` returns the
  nearest store of the correct class (granary for nutrients, warehouse
  otherwise) to a reference tile — the spatial half of "a docked ship's
  cargo flows to/from the right store."
- `_load_trade_ship` now classifies the cargo and records it on
  `TradeShip.cargo_store_class` (`"granary"`/`"warehouse"`), so the
  supply chain and overlay/inspector reflect where cargo flows. Always a
  string (`""` before load) — safe to read anywhere.

## Tests

- `tests/test_sea_pathfinding.py` — +3 A* tests (large open map solved,
  path still shortest around a barrier, adaptive cap scales). 11 total.
- `tests/test_supply_chain_routing.py` — 9 new: classifier (nutrient →
  granary, good → warehouse, agrees with distribute), nearest-store
  lookup (granary vs warehouse, None when absent), and the ship recording
  its cargo store class.

Suite: **1086 passed, 0 failed** (was 1074 at v0.43 entry; +12).

## Files touched

- `walkers.py` — `find_sea_path` BFS→A*, `_sea_path_cap`;
  `nearest_store_for_good`; `_load_trade_ship` records cargo class;
  `TradeShip.cargo_store_class`.
- `storage.py` — `store_class_for_good`, `is_nutrient_good`.
- `game_window.py` — `show_ship_paths` flag, `_draw_ship_paths`, P toggle.
- `version.py` — `v0.43` → `v0.44`.

## In-game checks (need a GL context)

- Press **P** with ships sailing: route polylines + goal rings should
  appear, tracing around coastlines.
- A nutrient-carrying trade route should associate with a granary; a
  goods route with a warehouse (cargo class is recorded; visible
  flow/inspector wiring can come next).

## Still outstanding (naval expansion)

Harbor berth occupancy (slots exist, not yet filled by idle ships) is the
remaining structural thread; naval combat depth (enemy ships at sea) is
the remaining gameplay thread. Both now rest on working pathfinding.
