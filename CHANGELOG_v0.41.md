# Changelog — v0.41

**Theme:** make shipyards player-driven. A shipyard's info panel now has
a build queue — click to enqueue cargo ships (commercial yard) or
warships (military yard), watch them build, and remove queued hulls.
Replaces the v0.39 auto-launch-on-timer behaviour.

## Shipped

### Shipyard build queue (player-driven launches)

Shipyards no longer auto-spit a ship every `SHIPYARD_INTERVAL` ticks.
Instead the player queues hulls, and each yard builds them one at a time.

- **Queue model (`walkers.py`).** Each shipyard carries a `ship_queue`
  (a list of ship-kind strings) stored in
  `building_state[(r,c)]["ship_queue"]`, so it round-trips through
  save/load with **no schema bump** (it's just another JSON value in the
  per-building state dict). New accessors:
    - `WalkerManager.shipyard_queue(game_map, r, c)` — the live list.
    - `shipyard_enqueue(game_map, r, c, kind)` — append, capped at
      `SHIPYARD_QUEUE_MAX = 8`; returns False when full.
    - `shipyard_dequeue(game_map, r, c, index)` — remove by index;
      out-of-range is a safe no-op.
- **Queue-draining build loop.** `_process_shipyards` now advances a
  per-yard build timer (`ship_build_t`, also in `building_state`) only
  when that yard has something queued; on completion it launches the
  head hull onto an adjacent water tile and pops it. Empty queue → the
  yard does nothing. Launches stay guarded on construction state, water
  access, the per-yard live-ship cap (`SHIPYARD_MAX_PER_YARD`), and
  affordability — a blocked launch holds the hull and the timer full and
  retries next tick (no lost hull, no crash). Ship-construction logic
  was factored into `_launch_ship(game_map, economy, kind, water)`.
- **Build-queue panel (`game_window.py`).** Clicking a shipyard grows
  the info panel and draws a build-queue footer: a `+ Build <Cargo
  ship/Warship>` button (labelled by the yard's `ship_kind`), a header
  showing `queue (n/8)`, and one row per queued hull. The head-of-queue
  row shows a live build-progress bar + percentage; every row has a ✕
  remove button. Mirrors the existing warehouse-toggle pattern
  (`_shipyard_btn_rects` cache + hit-test in `on_mouse_press`).
- **Click handling.** Left-clicking the add button enqueues the yard's
  own ship kind; clicking a ✕ removes that hull. Action strings
  (`__add__` / `__rm__<index>`) are parsed defensively (a malformed
  index is a no-op).

## Tests

- `tests/test_shipyards.py` — the 8 v0.39 auto-launch tests rewritten as
  13 queue-driven tests (enqueue/dequeue round-trip, queue cap, build
  cycle gating one launch per interval, per-yard cap with a backed-up
  queue, no-water / constructing guards, empty-queue-launches-nothing,
  building_state persistence).
- `tests/test_shipyard_queue_ui.py` — 7 new tests driving the real
  `_draw_shipyard_queue` against a stubbed arcade (rect generation for
  empty / partial / full queues, well-formed rects) and the
  `__add__` / `__rm__<i>` click-action parsing the handler uses.

Suite: **1060 passed, 0 failed** (was 1048 at v0.40 entry; the shipyard
file grew +5 and the new UI file added +7).

## Files touched

- `walkers.py` — queue accessors, queue-draining `_process_shipyards`,
  `_launch_ship` helper.
- `game_window.py` — `_shipyard_btn_rects` cache, panel-height branch,
  `_draw_shipyard_queue`, click dispatch.
- `version.py` — `v0.40` → `v0.41`.

## Not done (needs a GL context)

The panel rendering and clicks are verified headlessly (rect shapes,
action parsing) but not on screen. In-game check: click a commercial or
military shipyard, confirm the `+ Build` button and queue rows render,
enqueue a couple of hulls, watch the head row's progress bar fill, and
confirm ships launch onto adjacent water as the queue drains.

## Roadmap note

This is the **shipyard build-queue UI** thread from the four-part naval
expansion (shipyard queue / port↔granary-warehouse supply chain /
harbors-as-berths / ship pathfinding). Still outstanding from that set:
the supply-chain routing, berth occupancy, and — the foundational gap —
a real ship pathfinder (ships still use a greedy one-step heuristic with
no backtracking, which deadlocks on concave coastlines).
