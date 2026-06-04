# Caesar III Clone — v0.37

## Headline

**Maintainability drop + four bug fixes surfaced by it.** No new
gameplay features. `game_window.py` went from one 11,470-line class
to a 6,153-line orchestrator plus eight focused mixins under
`mixins/` — a 46% reduction in the god-object. Every public
attribute, method signature, and data file is unchanged; all 953
tests still pass; the 16 pre-existing documented failures are
unchanged.

This drop also fixes four bugs:

1. **Splash → Load game crashed** with `NameError: load_game is
   not defined`. The splash mixin was missing
   `from saveload import load_game`.
2. **Buildings editor Production / Consumption panes were empty.**
   The per-pane height `pane_h_max = 80` was *less than* the
   minimum 88 px that the first-row-render guard requires
   (row geometry: `row_y = top_y - 38`, `row_h = 22`, floor at
   `bot_y + 28` → need ≥ 88 px for one row). Tightening from 120
   to 80 in v0.33 silently broke every pane; every building
   rendered as "Add resource…" only. Bumped to 110 so two rows
   render reliably; the third still truncates with the existing
   guard, which is the intended overflow behaviour. *This is
   the bug screenshots `bakery.png` / `buildings_editor.png`
   exhibit.*
3. **Latent skirmish `COLOR_GOLD` crash.** A comprehensive AST
   scan across all eight mixins found `scenario_layouts.py`
   calling `self._notify(..., COLOR_GOLD)` inside
   `_skirmish_init` without importing `COLOR_GOLD`. Would crash
   any time the skirmish scenario launched. Pre-existing in
   v0.36; surfaced only because the mixin extraction made the
   import boundary explicit.
4. **Module-load-time circular import.** Two mixins
   (`diagnostic_panels`, `map_editor`) imported `game_window` at
   module top to read `TOP_BAR_H`. This worked when the game
   started via `main.py` (game_window imports the mixins first,
   the back-edge resolves) but failed if any tool ever imported
   a mixin standalone. Moved to **lazy import inside the two
   methods that need `TOP_BAR_H`**. Now every mixin in
   `mixins/` imports cleanly on its own with no entry-order
   dependency.

This drop also integrates the v0.36 work that was developed in a
separate branch: the numpy A* pathfinder (`pathfinding.py`
rewrite, 2.3× faster on long paths, 50× smaller road mask in
RAM), the numpy road mask in `road_network.py`, and the full
`triggers_editor.py` (970-line graph-view UI that edits the
`triggers` block of `data/scenarios/<name>/map.json` atomically).

See `REFACTORING.md` for the mixin-pattern walk-through and the
specific test contracts that needed care during extraction.

## The eight mixins

```
mixins/
├── __init__.py
├── scenario_layouts.py       (367 LOC)  starter cities + skirmish waves
├── buildings_editor.py      (1389 LOC)  data/buildings.json editor
├── unit_editor.py            (517 LOC)  data/units.json editor
├── map_editor.py             (944 LOC)  terrain painter, save/load maps
├── scheduled_events_panel.py (496 LOC)  per-map scheduled events panel
├── trade_panels.py           (513 LOC)  Bartering + Gold Trade modals
├── diagnostic_panels.py      (524 LOC)  Nutrients / Happiness / Diagnostics
└── splash_menu.py            (839 LOC)  splash buttons, sub-screens, _start_new_game
```

Each mixin is a plain Python class. `CaesarGameWindow` inherits
from all eight plus `arcade.Window`. No mixin holds state — methods
still read and write through `self.<attr>` exactly as before.

## What's *not* in this drop

- The render layer (HUD, minimap, info panel, ~2,400 lines) stays
  inline. Extracting it cleanly would require touching the shared
  `_create_gui_texts` text-object cache, which is out of scope
  for a zero-regression maintainability pass.
- The trigger editor (~900 lines, the legacy info-modal variant
  in `game_window.py`) stays inline too. It's a clean candidate
  for a ninth mixin in v0.38.
- Input dispatch (`on_mouse_press`, `on_key_press`, ~2,000 lines
  total) stays inline.

## Three implementation notes worth keeping

1. **`BUILDINGS_PATH` / `EVENTS_PATH` monkeypatch contract.**
   Tests patch these constants on the `game_window` module. The
   buildings-editor save path now reads them through
   `import game_window as _gw` indirection so the patch keeps
   working after the move into `mixins/buildings_editor.py`.

2. **`Path(__file__).parent / "data"` is module-fragile.** The
   unit-editor save path used this pattern; it broke as soon as
   the method moved out of `game_window.py`. Replaced with the
   shared `DATA_DIR` constant from `constants.py`.

3. **Source-text tests still pass.** A few tests read
   `game_window.py` as a string and assert on substrings (menu
   binding lines, method-defn signatures). The relevant strings
   either still live in `game_window.py` (menu bindings,
   cutscene runtime hooks) or appear by reference in
   `_create_gui_texts` (splash button labels). No test needed
   to change.

## Verification

- `python3 -m pytest -q --tb=no` → **953 passed, 16 failed**.
- Same 16 failures as v0.36 baseline, byte-identical test names
  and assertion values.
- Verified after each of the eight mixin extractions, after
  each of the four bug fixes, and from a fresh extraction of
  the packaged zip.
- A standalone-import smoke test exercises every mixin module
  in isolation to catch the circular-import class of regression.

## Files touched

- `game_window.py` — methods moved out, mixin imports + class
  declaration updated. No method body edits other than the two
  small contract fixes (which happen inside the mixin copies,
  not the original).
- `mixins/` — new package, eight mixin modules.
- `REFACTORING.md` — design notes.
- `CHANGELOG_v0.37.md` — this file.

## Files unchanged

Every other `.py` in the tree (`economy.py`, `walkers.py`,
`game_map.py`, all of `tests/`, all of `data/`, etc.) is
byte-identical to v0.36.
