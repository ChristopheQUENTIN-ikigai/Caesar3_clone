# Changelog — v0.52

**Theme:** inter-city commerce becomes *visible and honest*, plus a
keyboard/file-location reference and a commercial-fleet panel. This
release acts on a focused brief:

1. **Income only by trip.** The v0.50/v0.51 commercial-roads window
   still ran a *per-tick* gold drip alongside the per-trip voyages.
   v0.52 removes the per-tick gold: an inter-city route now moves stock
   but earns **no gold per tick** — all inter-city income comes from
   completed **voyages** (trips).
2. **Voyages you can see.** A launched voyage now spawns a real
   `TradeShip` walker that sails out and back, so foreign trade is
   visible on the water instead of being a hidden ledger entry.
3. **Commerce-ships panel (`!`).** A read-only summary of busy
   (at-sea) commercial ships and free berths ready to dispatch.
4. **"Mapping keys keyboards" splash button.** A pre-game overlay
   listing every key binding *and* the on-disk folders the game saves
   to / loads from (saves, maps, data root) — shown as absolute paths.

## Shipped

### Inter-city commerce earns gold only per trip — per-tick drip removed (`trade.py`, `commercial_roads.py`)

`TradeRoute` gains a `pays_treasury` flag (defaults `True`, preserving
the generic ambient-route primitive and every existing test). The
`commercial_roads` window now builds its inter-city routes with
`pays_treasury=False`: the route still **moves stock** out of the city
each tick (the export is real and `total_traded` still accrues), but it
credits **no gold**. The gold is earned by the per-trip voyage layer
(`voyages.py`) when a ship returns home.

The flag round-trips through `to_dict`/`from_dict`; pre-v0.52 saves
(no key) load as paying, so older saves are unaffected. The
commercial-roads window's labels were updated to be honest — "sell per
tick → gold" / "earned X dn" became "export per tick · gold by trip".

### Voyages are mirrored by a visible `TradeShip` (`voyages.py`, `game_window.py`, `walkers.py`)

`VoyageManager.update` now calls two **optional** hooks on the
duck-typed `VoyageWorld` it's handed:

- `on_voyage_launched(voyage)` — fired once a trip departs (after the
  cargo is debited and the voyage is in flight).
- `on_voyage_completed(record, voyage)` — fired once the round trip
  elapses (after the gold is credited).

Both are resolved via `getattr` and wrapped in a guard, so the manager
stays **pure logic / headless-testable** — a world without the hooks
(every existing fake world) behaves exactly as before, and a hook that
raises can never roll back the launch or lose the gold credit.

The window's `_voyage_world` adapter implements them: on launch it
spawns a `TradeShip` from a commercial-harbour water tile toward the
nearest map-edge water tile (the "foreign city is off-map"
abstraction), tags it `_voyage_ship`, and tracks it by `id(voyage)`; on
completion it turns that ship home / retires it. Crucially,
`_process_ships` now **skips export-income crediting and reloading for
voyage ships** — the voyage layer already pays on the trip, so the
visible ship is purely cosmetic and never double-credits.

### Commerce-ships panel (`!`) (`mixins/diagnostic_panels.py`, `game_window.py`)

The **`!`** key (Shift+1; both `EXCLAMATION` and `KEY_1`+Shift
accepted, mirroring the `$` finance panel) opens a read-only
**Commerce ships** panel:

- **Busy (at sea)** — one row per in-flight voyage: cargo (qty + good),
  destination city, locked-in payout, and ETA in ticks, sorted
  soonest-to-return first.
- **Free** — the commercial-harbour berths not currently out on a
  voyage, i.e. ships ready to dispatch (the exact count the voyage
  layer gates departures on).
- A lifetime footnote (trips completed, gold earned).

Same non-modal overlay tier and single-panel mutex as the finance
panel; closes on `!` again or Esc.

### "Mapping keys keyboards" splash button (`game_window.py`, `mixins/splash_menu.py`)

A new splash-menu button opens a read-only overlay with two columns:

- **Key bindings** — driven by the shared `HELP_TEXT` constant, so the
  splash overlay and the in-game `H` help never drift.
- **File locations** — the resolved **absolute** folders the game
  saves to / loads from: `SAVES_DIR` (saved games), `MAPS_DIR` (custom
  maps), and `DATA_DIR` (data/config root). This answers the brief's
  "the folder path where the config file is saved or loaded should be
  displayed."

Toggled by the button; closes on Esc or any click (same pattern as the
Credits overlay).

### Help text + version

Help text lists the new `!` panel; `version.py` → `v0.52`.

## Tests

- `tests/test_trade.py` — `pays_treasury` default, non-paying route
  moves stock without gold, flag round-trips, pre-v0.52 save loads as
  paying (+4).
- `tests/test_v050_features.py` — the old "route ships goods for gold
  each tick" test rewritten to the new "moves stock, pays no per-tick
  gold" behaviour.
- `tests/test_v051_voyages.py` — visible-ship hooks (launch/complete
  fire, hooks optional, a bad hook doesn't undo the voyage);
  commerce-ships panel (method exists, flag default, mutex both ways,
  adapter exposes hooks); visible-ship window integration (launch
  spawns a tagged `TradeShip`; voyage ship doesn't double-credit) (+11).
- `tests/test_v052_keymap.py` (new) — keymap action exists/toggles,
  button label in source, overlay uses `HELP_TEXT`, overlay shows
  `SAVES_DIR`/`MAPS_DIR`/`DATA_DIR`, paths are absolute, Esc + click
  close paths present (+7).

Suite: **1263 passed, 0 failed** (was 1241 at v0.51; +22).

## Files touched

- `trade.py` — `TradeRoute.pays_treasury` flag + gated `execute`,
  persistence.
- `commercial_roads.py` — inter-city routes created non-paying.
- `voyages.py` — optional `on_voyage_launched` / `on_voyage_completed`
  hooks on `update`/`_complete_arrivals`/`_consider_departures`.
- `walkers.py` — `_process_ships` skips income/reload for
  `_voyage_ship`-tagged trade ships.
- `game_window.py` — visible-ship hooks in `_voyage_world`,
  `_voyage_ships` tracking dict, `!` commerce-ships panel state / key /
  draw dispatch / Esc / mutex, `show_keymap` state + splash button +
  Esc/click close.
- `mixins/diagnostic_panels.py` — `_draw_commerce_ships_panel`.
- `mixins/splash_menu.py` — `_splash_keymap` action + keymap overlay
  draw (bindings + file locations).
- `constants.py` — help text gains the `!` line.
- `version.py` — `v0.51` → `v0.52`.
- `DEV_AUDIT.md` — new Chapter 11 (mixin design-pattern rationale;
  SpriteList-batching and numpy optimisation analysis).

## In-game checks (need a GL context)

- **Income only by trip:** open a city (`R`), add a per-tick route, and
  confirm stock drops each tick but the treasury does **not** rise from
  it; then set "Ship by trip" and confirm gold lands only when a ship
  returns.
- **Visible voyages:** with a commercial harbour on the coast and a
  linked city configured, watch a `TradeShip` sail out toward the map
  edge on launch and turn home on completion.
- **Commerce-ships panel:** press **!** — verify the busy rows (cargo,
  destination, payout, ETA) and the free-berth count update as ships
  sail and return.
- **Keymap overlay:** from the splash, click **Mapping keys keyboards**
  — verify the bindings list and the three absolute folder paths, and
  that Esc / a click closes it.
