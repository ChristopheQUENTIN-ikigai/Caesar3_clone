# Changelog — v0.43

**Theme:** dedicated ship pathfinding (proof of concept). Ships now find
a real route over water with BFS instead of a greedy one-step heuristic
that deadlocked on concave coastlines. This is the foundation the
port-supply-chain, berth-occupancy, and naval-combat threads sit on.

## Shipped

### BFS sea pathfinding (`find_sea_path`)

A new module-level function in `walkers.py`:

```
find_sea_path(game_map, start, goal, *, max_expansions=4000)
    -> list[(row,col)] | None
```

- Breadth-first shortest path over the **sea domain** (`_impassable_sea`
  — water passable, land/out-of-bounds not). Returns the full tile path
  including both endpoints, or `None` when the goal is unreachable by
  water (or the goal is a land tile).
- **BFS, not A***, deliberately: on an unweighted 4-connected grid BFS is
  already shortest-path; A* would only shrink the explored set, which the
  `max_expansions` cap bounds anyway. Keeping it BFS makes the PoC simple
  to reason about and test. Swapping in an A* frontier later is a
  localised change if profiling demands it.
- `SEA_PATH_MAX_EXPANSIONS = 4000` safety cap so a huge open sea can't
  stall a tick (returns `None` → caller falls back to greedy).

### Ships follow the path (with greedy fallback)

`Ship` now caches a BFS path and walks it step by step:

- `_recompute_sea_path` / `_next_path_step` rebuild the cache lazily —
  only when the goal changes, the cache is empty, the ship has drifted
  off the path, or the next step became blocked (terrain edits/bridges).
  No per-tick recompute in steady state.
- `pick_target` tries the path step first; if there's no path
  (unreachable goal, or no goal at all) it falls back to the **exact
  pre-v0.43 greedy heuristic**. This guarantees a ship is never *worse*
  off than before — when BFS can't help, behaviour is identical to old.

### Why it matters

The greedy heuristic had no backtracking: reaching a goal that required
*first* moving away from it (a U-shaped bay, a dead-end inlet, the
vertical channel in the `easy5` screenshot) made the ship oscillate
against the barrier forever. BFS routes around it. A regression test
proves a ship now escapes a U-trap that demonstrably defeats greedy.

## Tests — `tests/test_sea_pathfinding.py` (8)

- `find_sea_path`: straight open water, routing around a barrier through
  a gap, unreachable pocket → None, land goal → None, start==goal, and
  the expansion-cap bail-out.
- Ship integration: escaping a U-shaped greedy trap (with a faithful
  memoryless-greedy simulator asserting the map *is* a real trap, so the
  BFS test isn't vacuous), and graceful greedy fallback when the goal is
  on an isolated water pocket (no crash, never arrives — acceptable).

Suite: **1074 passed, 0 failed** (was 1066 at v0.42 entry; +8).

## Performance

Worst-case (fully open sea, corner to corner): 40×30 ≈ 1.2 ms, 60×60 ≈
3.4 ms. Ships recompute only on goal-change/exhaustion, not per tick, so
steady-state cost is negligible. Known limit: on a *very* large open map
(~100×100) the 4000-expansion cap can trip for a far goal and return
`None` → greedy fallback; large open seas are exactly where greedy works
fine, so this is acceptable for the PoC. Raise the cap or switch to A* if
a real map ever needs it.

## Files touched

- `walkers.py` — `find_sea_path`, `SEA_PATH_MAX_EXPANSIONS`; `Ship` path
  cache (`_sea_path`/`_sea_path_goal`), `_recompute_sea_path`,
  `_next_path_step`, path-aware `pick_target` with greedy fallback.
- `version.py` — `v0.42` → `v0.43`.

## PoC status / next

This is the proof of concept you asked for. It's self-contained and
fully headless-verified. If it's the right direction, the natural
expansions are: (1) A* upgrade + a higher/þadaptive cap if large maps
need it; (2) hook the path into a *visible* debug overlay so you can see
ship routes in-game; (3) build the **port↔granary/warehouse routing** on
top, now that ships can reliably reach a port. Nothing here needs a GL
context, but seeing a ship navigate the `easy5` channel in the running
game is the satisfying confirmation.
