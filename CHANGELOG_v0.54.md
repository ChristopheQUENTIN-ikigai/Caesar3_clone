# Changelog — v0.54

**Theme:** the `G` **City Metrics** graph gains four leading-indicator
plots and moves to a three-column grid. v0.26 added the money-flow
series (income / expenses / net / tax); v0.54 adds the four "things are
about to go wrong" signals the panel was still missing, each read from a
value the simulation already maintains.

## Shipped

### Four new metric plots (`game_window.py`)

`_record_plot_sample` now samples four more series each tick, and
`_draw_graphs` plots them. None require new simulation bookkeeping —
they read existing live values:

- **Housing room** (`housing_headroom`) — `economy.housing_capacity −
  population`. Positive means migration has somewhere to land; ≤0 means
  growth has stalled. The side panel already showed `Population /
  capacity` as a number; now it's a trend. (May dip negative briefly if
  housing is razed under a full population.)
- **Unrest %** (`rebel_pressure`) — `RebellionTracker.pressure` as a
  percentage of `balance.rebel_pressure_threshold`, so **100 = rebels
  about to spawn**. The single best early-warning line in the panel.
- **Wages %** (`wage_ratio`) — `economy.wage_payment_ratio × 100`. Below
  100 means the city is underpaying wages, which bleeds happiness a tick
  or two later.
- **Jobless** (`jobless`) — `population − employed` (floored at 0). The
  `J` jobs panel showed this as a number; now it's a trend line.

### Graph grid reworked 2×7 → 3×6 (`game_window.py`)

With 14 → 18 plots, adding an 8th row to the already-tall panel was the
wrong move (it bottom-clips on 800px windows). Instead the grid is now
**3 columns × 6 rows** — a perfect 18-cell fit:

- Each plot keeps roughly its v0.26 row height (the axis that matters
  for reading a trend) and only loses horizontal width, which the line
  plots tolerate well.
- `panel_w` 620 → 900 to give three columns usable box widths;
  `panel_h` 720 → 660 since six rows need less height than seven.
- Column/row count is now driven by `N_COLS` / `N_ROWS` constants rather
  than the hard-coded `// 2` / `% 2` the old layout used, so a future
  plot count change is a one-line edit.
- The existing screen-clamp (slide-up when the panel would bottom-clip)
  is unchanged and still keeps the panel inside small viewports.

New-plot colours are chosen to read at a glance: warm "watch me" tones
for Unrest % (red-orange) and Jobless (amber); calm tones for Housing
room (teal) and Wages % (blue).

## Tests

- `tests/test_graphs_history.py` — `_HISTORY_KEYS` extended with the
  four new keys (the file's own comment asks it be kept in sync with the
  `_plot_history` initialiser). `_StubEconomy` gains `housing_capacity`
  and `wage_payment_ratio`; the end-to-end
  `test_record_plot_sample_in_window_class` now stubs `w.rebellion` /
  `w.balance` and asserts all four new series record the expected values
  (headroom 10, unrest 50%, wages 100%, jobless 45).
- `tests/test_v026_features.py::test_economic_stats_plotted_under_g_key`
  — its hand-built history pool and stub economy updated with the four
  new keys/fields plus rebellion/balance stubs, so the v0.26 assertions
  still pass under the new sampler.
- Full suite green: **1264 passed**.

## Migration notes

- **Saves are unaffected.** The new series are derived per-tick from
  existing economy / rebellion state and are not persisted; old saves
  load and immediately begin populating the new plots.
- **No key changes.** `G` still opens and closes the panel; the layout
  is wider but still centred and screen-clamped.
