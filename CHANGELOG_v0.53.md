# Changelog — v0.53

**Theme:** inter-city commerce becomes *per-trip only*. v0.52 stopped
per-tick routes from paying gold but left them moving stock for free
every tick — a silent value leak the UI mislabelled as "gold by trip".
v0.53 finishes the job: the per-tick route layer is **retired
entirely**. A linked foreign city now exports goods *only* when a free
commercial ship sails a voyage and sells the cargo on its return —
never per tick. The model is now a clean one-liner: **inter-city goods
move per trip, not per tick.**

This also clears the way for a planned terrestrial **caravan** layer:
when it lands it will be the land counterpart to sea voyages and, like
them, per-trip — not a per-tick drip.

## Shipped

### Per-tick commercial routes retired (`commercial_roads.py`, `trade.py`)

`CommercialRoadManager` is now a pure **city-link bookkeeping** layer.
The route methods survive only as deprecated no-op stubs so nothing
crashes:

- `add_route(...)` creates nothing, registers nothing with the shared
  `TradeRouteManager`, logs a warning, and returns `None`.
- `remove_route(...)` returns `False`; `routes_for(...)` returns `[]`;
  `total_routes()` returns `0`.

Because no caller ever again builds a `pays_treasury=False` route, the
tick loop moves no inter-city stock and credits no inter-city gold. The
generic `TradeRoute` / `TradeRouteManager` primitive is untouched
(ambient routes still default to `pays_treasury=True`); only the
commercial-roads use of it is gone.

### Save/load: links persist, routes are dropped (`commercial_roads.py`)

`to_dict` still emits a `routes` key for on-disk shape stability, but it
is always `{}`. `from_dict` reads the linked-city set and **ignores**
any legacy `routes` block — those per-tick routes are *not* re-registered
into the tick loop, so a pre-v0.53 save stops leaking stock the instant
it loads. A city that appeared only under the old `routes` block (never
in `linked`) is still promoted to linked, so a player keeps the
commercial road they paid for and simply re-establishes commerce via a
voyage.

### Commercial-roads window reworked to per-trip (`mixins/trade_panels.py`)

- Removed the **"Stock-moving routes"** list, the **"Add a route"**
  staging area, the **rate stepper** (`-10 … +10`), and the **"Add
  route"** button. The `addroute:` / `removeroute:` / `rate:` click
  actions are gone (a stale `addroute:` action now safely no-ops).
- The detail view keeps the good picker and the **"Ship by trip"**
  button, and shows a per-trip earnings preview (capacity × unit price,
  interval) in place of the old per-tick rate preview.
- The city-list row now shows the **voyage** state (`voyages shipping:
  <good>` / `paused` / `no voyage set`) instead of a per-tick route
  count.
- Honest copy throughout: the detail header reads "Inter-city goods move
  by sea voyage — one trip per ship, gold paid on return. (No per-tick
  routes.)"

## Tests

- `tests/test_v050_features.py` — the per-tick route tests
  (`add_route_registers…`, `route_ships_goods…`, `remove_route…`,
  `unlink_removes_all…`, `persistence_round_trip`,
  `from_dict_does_not_double_register`) rewritten to assert the retired
  no-op contract, plus a new `test_legacy_save_routes_recover_link_not_routes`
  proving a pre-v0.53 save recovers the link but no routes and moves no
  stock on tick. Window tests `test_add_route_via_click_handler` →
  `test_ship_by_trip_via_click_handler` (+ a stale-action no-op test);
  `test_rate_step_clamps_min_one` → `test_good_pick_via_click_handler`.
- `tests/test_saveload.py::test_commercial_roads_round_trip` — now
  asserts links persist, the city route never registers, and the ambient
  route is unaffected.
- Full suite green: **1264 passed**.

## Migration notes

- **Old saves load fine.** Any per-tick commercial routes in a pre-v0.53
  save are dropped on load; the city links they implied are preserved.
  Re-enable commerce by opening the city (`R`) and pressing **Ship by
  trip**.
- **Modders / scripts** calling `CommercialRoadManager.add_route` will
  now get `None` and a warning. Configure a voyage instead via
  `VoyageManager.configure(city_id, good=..., enabled=True)`.
