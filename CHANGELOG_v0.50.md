# Changelog — v0.50

**Theme:** authoring + commerce. Two player-facing features: a map-editor
form to set a scenario's *starting budget*, and an in-game
*Commercial roads* window to link foreign cities and run persistent
auto-trade routes to them.

## Shipped

### Map editor — "Init values" form

A new **Init values** button on the editor's top toolbar opens a modal
that edits the scenario's starting budget:

- **Gold** (treasury) and **population** steppers at the top.
- A scrollable list of every **good** the economy tracks — the ten
  nutrients (granary-side) followed by the warehouse-side raw /
  intermediate stocks — each with `+/-` steppers and a click-to-type
  value box.
- **Reset** restores the template defaults; **Confirm** writes the
  values onto the live economy; **Cancel** discards.

The form edits exactly the fields `mapfile.collect_map_state` already
serialises (`economy.treasury` / `economy.population` /
`economy.resources`), so **Save map** persists them with no new
plumbing. A fresh editor world is seeded from the new template so a new
map starts with sensible numbers; a *loaded* map keeps its own saved
values.

#### `default_initial_values.py` (new)

The prefill template, in the same mod-friendly spirit as
`bartering.STOCK_PRICES`: a plainly named table of starting gold,
population, and per-good opening stocks (mirroring `balance.start_*`).
`build_default_initial_values()` returns the ordered
`{gold, population, goods}` dict the form consumes.

### Commercial roads — link cities, run persistent trade routes (`R`)

The **R** key now opens the **Commercial roads** window. It lists the
seven foreign cities — **Massilia, Roma, Carthage, Athina, Istanbul,
Tunis, Alexandria** — and lets the player:

- **Link a city** (one-off treasury cost) to open a commercial road.
- Attach **persistent auto-trade routes** to a linked city: pick a
  stockpile good and a per-tick rate; the route sells that good every
  tick for gold, driven by the existing `TradeRouteManager` — the same
  engine as the ambient trade routes.
- **Remove** individual routes or **Unlink** a city (tearing down all
  its routes).

Route profit is derived from the good's market price
(`bartering.STOCK_PRICES`) scaled by a per-city distance premium, so
expensive goods and far cities (Alexandria, Istanbul) pay more.

#### `commercial_roads.py` (new)

`CommercialRoadManager` owns the linked-city set and per-city route
lists, registering each route with the shared `TradeRouteManager` so a
single `trade_manager.update()` per tick drives ambient and city routes
alike. `to_dict` / `from_dict` round-trip the state.

### `R` rebind

`R` was the tick recorder; it's now **Commercial roads**, and the
recorder moved to **Ctrl+R** (a diagnostic tool used far less often).
Help text updated.

### Save / load

Save files gain a `commercial_roads` block (links + city routes). City
routes are excluded from the `trade_routes` block (`_ambient_trade_routes`
filters them out) so a load restores each route exactly once — no
double-registration, no loss. Backward compatible: older saves with no
`commercial_roads` key load to an empty manager.

## Tests — `tests/test_v050_features.py` (new, 36) + 1 in `test_saveload.py`

Template shape/order; commercial-road link cost / affordability /
double-link guard, route registration with the shared manager, per-tick
shipping, removal + unlink teardown, persistence round-trip (including
no-double-register); init-values bootstrap seed, open/commit/cancel,
clamp-at-zero, `collect_map_state` round-trip, template reset, keyboard
digit/backspace/Esc/Enter; commercial-roads window link/add/close via
the click handler; and a save/load round-trip proving ambient and city
routes restore exactly once each.

Suite: **1194 passed, 0 failed** (was 1157 at v0.49; +37). All real
draw paths (city-list, city-detail, init-values form, updated toolbar)
verified to render in a headless GL context.

## Files touched

- `default_initial_values.py` — new prefill template.
- `commercial_roads.py` — new city-link + route manager.
- `constants.py` — `TRADE_CITIES`, `TRADE_CITY_*` tuning, help text.
- `mixins/map_editor.py` — Init values button + modal (draw / click /
  key / scroll), template seed at bootstrap.
- `mixins/trade_panels.py` — Commercial roads modal (draw / click /
  scroll).
- `mixins/splash_menu.py` — `_ensure_commercial_roads` after each
  trade-manager rebuild.
- `game_window.py` — modal state, `_ensure_commercial_roads`, `R`
  rebind + Ctrl+R recorder, draw / mouse / scroll / key wiring, panel
  mutex entry.
- `saveload.py` — `commercial_roads` persistence + ambient-route split.
- `version.py` — `v0.49` → `v0.50`.

## In-game checks (need a GL context)

- **Editor → Init values:** open the map editor, click **Init values**,
  bump gold/population, type into a good's box, **Confirm**, then
  **Save map** — reload and confirm the starting budget stuck.
- **Commercial roads:** in play, press **R**, **Link** a city, **Open**
  it, add a route (e.g. sell wheat to Massilia), watch the treasury tick
  up as stock ships. **Ctrl+R** still toggles the recorder.
