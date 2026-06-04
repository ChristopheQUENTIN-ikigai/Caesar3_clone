# Changelog — v0.56

**Theme:** two performance/correctness fixes in the hot loop — the
simulation no longer changes behaviour with the player's frame rate, and
the map renders in a handful of GPU draw calls instead of thousands.

## Fixed

### Frame-rate-coupled simulation (correctness + CPU)

`on_update` called `WalkerManager.update()` once **per rendered frame**
for visual smoothness, but that method also ran all the per-tick *logic*
— citizen spawning, delivery/soldier/fireman dispatch, raid rolls,
combat resolution, the fire sweep, ship processing, and walker reaping —
gated by integer `timer += 1` counters. Those counters advanced per
frame, not per game tick, and ignored `speed_multiplier`. Consequences:

- Spawn / dispatch / raid cadence silently tracked the display's refresh
  rate; a 144 Hz machine populated a city faster than a 60 Hz one.
- The game-speed control didn't affect any of that logic — only the
  economy honoured it.
- Full combat / fire / ship sweeps ran 60+×/second instead of at tick
  cadence — wasted CPU.

**Fix.** Movement is split out of the tick logic:

- `WalkerManager.update_movement(game_map, dt_scale)` — **per-frame**,
  positions only. `dt_scale` (frame seconds × game speed ÷ tick period)
  decouples distance-per-tick from frame rate: a walker crosses a tile
  in the same number of *ticks* at 30, 60, or 144 fps, and game speed
  scales motion proportionally.
- `WalkerManager.update(game_map, economy, move=False)` — the heavy
  per-**tick** logic, now called from inside the fixed-timestep tick
  loop in `game_window.on_update`. `move` defaults to `True` so headless
  harnesses and the test suite keep treating one `update()` as one
  complete tick step (movement included).
- `Walker.update(game_map, dt_scale=1.0)` and subclasses scale their
  progress increment by `dt_scale`.

### Per-tile immediate-mode map rendering (CPU/GPU)

`GameMap._draw_terrain` re-issued one immediate-mode draw call **per tile
per frame** — ~2,400 on the default 40×30 map, ~8,000 on a 64×64 editor
map — plus one `draw_line` per grid edge. None of it was batched or
culled.

**Fix.** The static terrain and feature layers are baked once into
`arcade.SpriteList`s (one batched GPU draw call each) and the grid lines
into a `ShapeElementList`, rebuilt lazily only when terrain or features
change:

- `GameMap.invalidate_render_cache()` marks the layers stale; the next
  `draw` rebuilds them. Wired at every mutation site: feature depletion
  (`_clear_depleted_feature`), save load (`from_dict`), and the three
  map-editor paint paths (terrain / feature / clear-feature).
- Tiles whose texture is missing (no PNG shipped — common in tests and
  partial asset sets) fall back to the original colour/glyph path, so
  textureless environments render identically. The fallback is culled to
  the camera's visible-tile window via the new
  `GameWindow._visible_tile_bounds()` → `GameMap.draw(visible_bounds=…)`.

No gameplay or save-format change; both fixes are internal to the
update/draw paths.

## Tests

- `tests/test_v056_frame_rate_and_render_cache.py` (new, 12 tests):
  - movement: `dt_scale` scales progress linearly; two half-steps equal
    one full step (the frame-rate-independence guarantee); `move=False`
    leaves positions untouched while `update_movement` advances them; the
    default `move=True` still moves.
  - render cache: dirty on construction, cleared on build, re-set by
    `invalidate`, feature depletion, and `from_dict`; textureless tiles
    populate the fallback lists; the viewport-bounds predicate filters
    correctly.
- `tests/test_v023x_features.py::test_game_map_draw_terrain_method_uses_feature_textures`
  updated: the texture lookup moved from per-frame `_draw_terrain` into
  the cached `_build_render_cache`, so the test now stubs the
  SpriteList / Sprite / shape-list constructors (no GL) and asserts
  `textures.feature()` is consulted at cache-build time. Contract
  unchanged — the registry is still consulted for every feature tile.
- Full suite green: **1281 passed** (1269 prior + 12 new).

## Migration notes

- **Saves unaffected.** No change to the save format.
- **No gameplay change** beyond the intended one: simulation cadence and
  walker speed are now identical across frame rates and respond to the
  game-speed control uniformly.
- **Version bumped to v0.56**, which also corrects a lingering drift
  (`version.py` still read `v0.54` after the v0.55 release).
