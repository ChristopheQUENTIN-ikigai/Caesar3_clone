# Caesar III Clone — v0.35

## Five-feature drop

### F1 — 'N' opens the nutrients informational panel

Players asked for a panel showing actual nutrient coverage rather than
the legacy `food` mental model the v0.34 cleanup retired. Press `N` to
open a non-modal panel listing each of the 10 nutrients with:

  * Nutrient name (human-readable)
  * Eaten this tick / population (so the player can see whether the
    nutrient is being delivered fast enough)
  * Production rate per tick (from `economy.gross_production`)
  * End-of-tick stock (the buffer)
  * Coverage percentage (eaten ÷ population, 0..100%)

Rows are coloured green (≥10% coverage), red (0% — chain broken), or
white (1..9% — chain working, scaling needed). A totals row shows
overall diversity, total fed/tick, and total produced/tick.

Closes on `N` again or `Esc`. Same non-modal pattern as the
happiness-debug overlay; opening it closes any other floating panel
(single-panel-at-a-time invariant from v0.23).

### F2 — Satiety bug fix (was: `Bread: 0%` despite a working bakery)

The bakery screenshot in the v0.34 bug report showed `Bread: 0% (0)`
even though a healthy bakery was producing 10 bread/tick. Root cause:
the v0.17 satiety formula in `economy.get_status_lines` read end-of-
tick *stock*, which is zero whenever bread is consumed as fast as it's
produced — pop 980 + bakery output 10/t means stock drops to 0 every
tick, so the formula reported 0% despite delivery.

The fix tracks per-nutrient eaten amount in the economy's food-loop
(`food_eaten_by_good` dict, populated in `update()`) and reports
satiety as `100 × eaten / population` (clamped 0..100). The residual
stock is still shown in parens as a buffer indicator.

The new formula correctly reports `Bread: 10% (0)` for a pop-100 city
with a single bakery — 10 bread/tick covers 10% of the per-tick
demand. The pop-980 / output-10 city now reads `Bread: 1% (0)`, an
accurate diagnosis the player can act on.

### F3 — Building editor ±1 buttons

Editor rows now show `−50 −10 −1 [val] +1 +10 +50` (was: just
±10/±50). Per-axis size still uses ±1 (unchanged). The Workers field
on the bakery (default 3) can now be stepped to 2 or 4 without
overshooting; the previous ±10 jumped to 0 or 13.

Production / consumption / build-materials row buttons match: same
7-button strip plus the existing remove (×) button. A modder editing
a low-magnitude entry (1-flour-per-loaf vs 2-flour-per-loaf) gets the
right granularity.

The new ±1 step is exposed as `EDITOR_STEP_UNIT = 1` on the window
class (companion to the existing `EDITOR_STEP_MINOR = 10` /
`EDITOR_STEP_MAJOR = 50`). Action names are `minus_unit:{field}` /
`plus_unit:{field}` for scalars and `{prefix}_minus_unit:{rid}` /
`{prefix}_plus_unit:{rid}` for resource rows.

### F4 — Happiness debug readability

The Z-key debug overlay was 360 px wide and used cryptic abbreviations
(`tax_pen`, `food_bon`, `house_bon`, `emp_bon`, `water_pen`,
`diversity_bon`, `h_prod`). The "housing/pop = 365 / 735" line
overflowed the right edge in the v0.34 screenshot.

Fix: panel widened to 460 px, term names spelled out in plain English
("Tax penalty", "Food bonus", "Housing bonus", "Employment bonus",
"Water penalty", "Nutrient diversity", "Service bonus" for `h_prod`).
Inputs section uses sentence-case labels ("Tax rate", "Food eaten /
needed", "Housing / population", "Employment efficiency",
"Dry-house fraction", "Nutrient diversity"). No data-layer change —
the breakdown dict keys are still the v0.21 short names; only the
display strings move. (Verified by
`test_happiness_breakdown_keys_unchanged_in_v035`.)

### F5 — 'C' opens international goods↔gold trade

Sibling of the existing bartering menu. Where bartering swaps good A
for good B at a fixed ratio, the new gold-trade menu swaps a single
good for gold (BUY: pay treasury for goods; SELL: give up goods for
treasury) at the same `bartering.STOCK_PRICES` table. 100 dn flat fee
per transaction on both sides.

Implementation:
  * New pure-logic module `gold_trade.py` (mirrors `bartering.py`).
  * New modal `_draw_gold_trade` in `game_window.py` (mirrors
    `_draw_barter`).
  * New state flag `show_gold_trade` and supporting scratch.
  * New hotkey binding `arcade.key.C`.
  * Entries in the modal-mutex map so opening it closes any other
    floating panel; Esc and the Cancel button both close it.

The single price table means a modder rebalancing one rebalances both
— a future tuning pass could introduce a buy/sell spread by adding a
per-resource margin to `gold_trade.STOCK_PRICES`. The v0.35 ship is
symmetric (the spread is just the flat fee, paid on both sides).

## Files touched

```
economy.py                       (F2)
gold_trade.py                    NEW (F5)
game_window.py                   (F1, F3, F4, F5)
constants.py                     (F5 — HELP_TEXT lines)
tests/test_v035_features.py      NEW
CHANGELOG_v0.35.md               NEW (this file)
MODDING.md                       NEW (v0.35 modder reference)
```

## Test status

  * 17 new tests in `tests/test_v035_features.py`, all green.
  * 921 + 17 = 938 passing on a clean run.
  * Same 16 pre-existing failures as v0.34 (stale tests asserting
    the legacy `food`-producing farm chain that the v0.34 cleanup
    retired — engine is correct, tests are out of date). Will be
    addressed in a follow-up drop dedicated to test hygiene; flagged
    in MODDING.md so new contributors don't mis-diagnose them as
    breakage they caused.

## Known follow-ups

  * The 16 stale tests above (`test_water_and_starvation.py`,
    `test_v013_features.py::TestFertileSoilBonus`,
    `test_road_network::test_connecting_road_resumes_production`,
    `test_stats::test_production_aggregates_correctly`, the
    debug-logging click tests). Most still expect a farm that
    produces 8 `food` per tick; they need to be rewritten against
    wheat + bread.
  * `game_window.py` is 10.9k lines. A maintainability refactor
    extracting each modal (`_draw_barter`, `_draw_gold_trade`,
    `_draw_happiness_debug`, `_draw_nutrients_panel`, etc.) into its
    own module would improve mod-author navigability without
    touching gameplay.
  * Per-building production charts could read from the new
    `food_eaten_by_good` dict if the player wants a "where does my
    bread go?" answer (currently the answer is "everyone eats it
    until none is left").
