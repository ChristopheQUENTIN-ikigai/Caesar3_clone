# Changelog — v0.51

**Theme:** inter-city commerce gets *real ships and real trips*, plus a
finance budget panel. Two player-facing features: a **per-trip voyage
layer** that makes foreign trade depend on free commercial ships,
stock, safe seas, and distance; and a **`$` finance panel** that
summarises every income source, every upkeep/expense, and the net
balance.

## Shipped

### Per-trip voyages — ships carry goods, gold lands on the return (`voyages.py`)

The v0.50 *Commercial roads* window shipped a *per-tick* drip: a route
sold a few units of a good every single tick for gold. v0.51 adds the
stricter, more faithful model the brief asked for — **inter-city goods
trading now requires free commercial ships to carry the goods, and the
goods↔gold exchange happens per TRIP, not per tick.**

A *voyage* is one round trip of one free commercial ship. It loads up
to **1000 units** (`VOYAGE_SHIP_CAPACITY`) of a good at the commercial
port, sails to a linked foreign city, sells the cargo for gold, and
sails home. The gold is credited **once, on the ship's return** — never
per tick.

A voyage only *departs* when **all four** of the brief's conditions
hold, checked each tick against a `VoyageWorld` adapter the window
supplies:

1. **A free commercial ship is available** — counted from the berths of
   the city's *commercial* harbours (`ship_slots`, `port_role
   == "commercial"`) minus the voyages already at sea.
2. **The good is in stock at the commercial port** before departure
   (`port_stock(good) >= 1`); the ship loads `min(1000, stock)`, so a
   partial hold is fine but an empty one never sails.
3. **The destination is not at war** (`no_war_at_destination`) — modelled
   as "no hostile enemy walker on the board".
4. **The sea road is clear of pirates** (`no_piracy_on_route`) — closed
   while any hostile `EnemyShip` is afloat.

Even with a free ship and clear seas, a city can't dispatch again until
its previous voyage's round-trip time has elapsed. That cooldown is

    VOYAGE_INTERVAL_BASE + VOYAGE_INTERVAL_PER_DISTANCE * city.distance

ticks, so **the interval depends on distance**: Roma (distance 2) turns
a ship in ~18 ticks; Alexandria (distance 9) in ~46. Per-unit payout
reuses `bartering.STOCK_PRICES` scaled by `VOYAGE_PROFIT_FRACTION` and
the city's `profit_mult`, so an expensive good shipped to a far city
earns the most.

#### `voyages.py` (new, pure logic — no arcade)

`VoyageManager` owns per-city configs (which good ships, on/off), the
in-flight `ActiveVoyage` list, the per-city cooldown clocks, and the
lifetime tallies. `update(world)` completes arrivals first (freeing
ships) then considers departures. `to_dict`/`from_dict` round-trip the
whole layer. Same mod-friendly split as `bartering` / `gold_trade` /
`commercial_roads`: all the economics live here, the window only adapts
world state.

#### Commercial-roads window — voyage controls

The city-detail view gains a **Per-trip voyages** block: the configured
good, the distance-scaled interval, the per-unit price, the free-ship
count, and **Pause/Resume** + **Stop** buttons. A **Ship by trip**
button (beside the existing per-tick **Add route**) sets the picked good
as the city's voyage good. Unlinking a city clears its voyage config;
ships already at sea still return and pay out.

### Finance budget panel (`$`)

The **`$`** key (Shift+4; both `DOLLAR` and `KEY_4`+Shift accepted)
opens a read-only **Finance** panel summarising the city budget in
gold/tick:

- **Income** — taxes, money-producing buildings, per-tick trade-route
  profit, and per-trip voyage gold (the spike on a returning ship).
- **Upkeep & expenses** — worker wages, flat per-building upkeep, and
  buildings that consume money.
- **Balance** — net per-tick swing (green surplus / red deficit) and the
  current treasury, with a voyage lifetime footnote (trips, gold,
  ships at sea).

#### `economy.finance_breakdown` (new)

The economy now stamps a granular `finance_breakdown` dict each tick
(`tax`, `production`, `wages`, `upkeep`, `consumption`). The window
injects the two out-of-economy income lines (`trade_routes`,
`voyages`) after the tick. `TradeRouteManager.update` now tracks
`last_tick_profit` for the trade-route line.

### Save / load

Save files gain a `voyages` block (configs, cooldowns, in-flight
voyages, lifetime tallies, tick clock). Backward compatible: older saves
with no `voyages` key load to an empty voyage layer.

### Help text + version

Help text lists the new `$` panel; `version.py` → `v0.51`.

## Tests — `tests/test_v051_voyages.py` (new, 45) + 2 in `test_saveload.py`

Interval distance-scaling and unit-price city-scaling; each of the four
departure conditions in isolation (no ship / no stock / war / piracy)
plus unlinked / disabled / no-good guards; 1000-unit capacity cap and
partial holds; cargo debited from the port; gold credited only on
completion; cooldown blocks re-dispatch; two cities dispatch
independently; lifetime tallies accumulate; config create/update,
`clear_city` (in-flight still completes); persistence round-trip
(config + in-flight + tick + lifetime), restored voyage still pays,
empty-dict loads clean; economy finance-breakdown keys present and
income-consistent, upkeep recorded for a naval building; trade-route
`last_tick_profit` tracking; window integration (voyage manager created,
`_voyage_world` ship-count from commercial-harbour berths, port
stock/debit, Ship-by-trip / toggle / clear / unlink click handlers);
finance-panel flag + mutex + breakdown feed; plus two save/load
round-trips (voyage state survives; pre-v0.51 save loads clean).

Suite: **1241 passed, 0 failed** (was 1194 at v0.50; +47).

## Files touched

- `voyages.py` — new per-trip voyage layer (pure logic).
- `constants.py` — `distance` per trade city; `VOYAGE_*` tuning; help text.
- `economy.py` — `finance_breakdown` dict, per-tick population of the
  income/expense lines.
- `trade.py` — `TradeRouteManager.last_tick_profit`.
- `game_window.py` — voyage manager creation, `_voyage_world` adapter,
  voyage tick + income injection, `$` finance panel state / key / draw
  dispatch / Esc / mutex.
- `mixins/trade_panels.py` — per-trip voyage block + Ship-by-trip button
  + click actions; unlink clears voyage config.
- `mixins/diagnostic_panels.py` — `_draw_finance_panel`.
- `saveload.py` — `voyages` persistence block.
- `version.py` — `v0.50` → `v0.51`.

## In-game checks (need a GL context)

- **Voyages:** build a **commercial harbour** (berths) + a **commercial
  port** on the coast, link a city (R), open it, pick a good with stock,
  hit **Ship by trip**. Watch a ship leave, the cooldown count down, and
  gold land on the return (notification). Confirm a far city (Alexandria)
  cycles slower than a near one (Roma), and that a naval raid (`EnemyShip`)
  halts departures until cleared.
- **Finance panel:** press **$** — verify income lines (taxes, production,
  trade routes, voyages), upkeep/expense lines (wages, upkeep,
  consumption), and the net balance + treasury read correctly and update
  per tick.
