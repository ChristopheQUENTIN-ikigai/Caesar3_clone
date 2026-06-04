# v0.37 refactor — mixin extraction in `game_window.py`

## What changed

`game_window.py` was 11,470 lines: one `CaesarGameWindow` class
with 183 methods doing everything from terrain painting to JSON
editor save-to-disk. It was hard to navigate, hard to review, and
hard to reason about which parts of the file touched which game
state.

This drop splits it into a slimmer orchestrator plus eight
cohesive mixin modules under `mixins/`. **No behaviour change** —
all 953 passing tests stay green and the 16 pre-existing failures
(present on v0.36 baseline, documented in earlier changelogs)
are unchanged.

| Mixin | Lines | What it owns |
| --- | --- | --- |
| `ScenarioLayoutsMixin`     | 366 | Starter-city seeds (default / Gallic / skirmish) + skirmish wave dispatcher |
| `BuildingsEditorMixin`     | 1378 | The buildings.json editor modal — fields, picker panes, save-to-disk |
| `UnitEditorMixin`          | 517 | The units.json editor — scalar fields, flags, skill chips, save-to-disk |
| `MapEditorMixin`           | 944 | Terrain/feature painter, grid resize, save/load/rename map files |
| `ScheduledEventsPanelMixin` | 496 | The "per-map scheduled events" panel layered over the map editor |
| `TradePanelsMixin`         | 513 | The Bartering modal and the international Gold Trade modal |
| `DiagnosticPanelsMixin`    | 517 | Read-only panels: Nutrients ('N'), Happiness debug ('Z'), Production diagnostics ('D') |
| `SplashMenuMixin`          | 839 | Splash buttons, splash sub-screens, `_start_new_game`, `_reset_to_empty_world` |

`game_window.py` is now **6,153 lines** (down 46%), and contains
only the parts that genuinely cross-cut: `__init__` / `setup`,
the game loop (`on_update`, `_game_tick`), the main `on_draw` and
all HUD rendering, input dispatch (`on_mouse_*`, `on_key_press`),
the cutscene/RPG runtime hooks (`fire_cutscene_for_flag`,
`fire_request_for_id`, `invalidate_cutscenes_cache`), the
trigger editor, and the `CaesarGameWindow` class plumbing that
ties the mixins to `arcade.Window`.

## How the mixin pattern works here

Every mixin is a plain Python class whose methods reference state
through `self.<attr>` — exactly as they did inside
`CaesarGameWindow`. The mixins hold **no state of their own**
(no `__init__`, no class-level mutable data, just methods and
the layout-constant class attributes that already belonged with
those methods). `CaesarGameWindow` inherits from them all plus
`arcade.Window`:

```python
class CaesarGameWindow(
    ScenarioLayoutsMixin,
    BuildingsEditorMixin,
    UnitEditorMixin,
    MapEditorMixin,
    ScheduledEventsPanelMixin,
    TradePanelsMixin,
    DiagnosticPanelsMixin,
    SplashMenuMixin,
    arcade.Window,
):
    ...
```

Method resolution order follows the linearization above: a method
defined on the concrete class wins, otherwise the leftmost mixin
that defines it wins, ending at `arcade.Window`. Since no mixin
defines a method that's also on another mixin (each cluster is
disjoint), MRO ordering is incidental — but the order above
matches the conceptual layering (gameplay → editors → panels →
splash) which makes the inheritance line easier to read.

**Crucially:** because the methods still resolve as
`game_window_instance._editor_save_to_disk(...)` etc., every
existing call site (other modules, tests that reach into the
window) keeps working unchanged. **Zero test edits were
required.**

## Two contracts that needed care during extraction

### 1. `monkeypatch.setattr(game_window, "BUILDINGS_PATH", ...)`

Several test fixtures (`test_v033_features.py`,
`test_buildings_editor.py`, `test_v028_features.py`) redirect a
constant on the `game_window` module so the editor save-to-disk
writes to a tmp file instead of the repo's `data/`. Pre-mixin,
those constants were imported directly into `game_window.py` so
the patch reached the code that read them. Post-extraction, the
buildings-editor save code lives in `mixins.buildings_editor`,
which has its own `from constants import BUILDINGS_PATH` — and
the test patches `game_window`, not `mixins.buildings_editor`.

Fix: inside `_editor_save_to_disk`, read the constant through
the `game_window` module reference so the redirect transparently
takes effect:

```python
import game_window as _gw
path = _gw.BUILDINGS_PATH
```

The contract for tests is preserved; only the implementation
shifts one indirection.

### 2. `Path(__file__).resolve().parent / "data" / ...`

`_unit_editor_save_to_disk` originally located the data file
relative to its own module. When the method moves into
`mixins/unit_editor.py`, `__file__` resolves to the mixin path
and the data lookup fails. Replaced with the shared
`DATA_DIR` constant from `constants.py` (which resolves relative
to the project root regardless of which module the method ends
up in).

### 3. Tests that grep `game_window.py` as source text

A handful of tests do `Path("game_window.py").read_text()` and
assert that certain strings appear in it:

- `test_v019_features.py` — looks for menu binding strings like
  `'"Buildings editor", self._menu_open_editor'`. These strings
  live in `_create_gui_texts`, which **stays in
  `game_window.py`**, so the bindings are still in the file even
  though the target methods now live in mixins.
- `test_v032_features.py` — looks for the literal
  `"def fire_cutscene_for_flag"` and `"def invalidate_cutscenes_cache"`.
  The cutscene runtime hooks **stay in `game_window.py`**
  (lines ~4177–4334 in the new file) for this reason. They're
  runtime engine glue, not splash UI, so this was a natural
  boundary anyway.

If you add another mixin in a future drop, scan the test tree
for `read_text()` / `open(...).read()` patterns against
`game_window.py` and check what strings they assert.

## Verification

```
$ python3 -m pytest -q --tb=no
953 passed, 16 failed in 2.7s
```

The 16 failures are identical (test names + assertion values) to
the v0.36 baseline — verified by running the same suite against
the unmodified v0.36 tree. They are pre-existing, unrelated to
this refactor, and documented in `CHANGELOG_v0.34.md` and
earlier as known-broken (mostly `test_debug_logging.py` which
depends on attributes that were removed several versions ago).

## What didn't change

- Every public attribute on `CaesarGameWindow` (the editor
  state dicts, the splash flags, the panel scroll positions).
- Every method signature on `CaesarGameWindow`.
- The data files (`data/buildings.json`, `data/units.json`,
  scenario JSON, etc.) — none touched.
- The on-disk save format and the scenario format.
- The four files from the v0.36 work (`pathfinding.py`,
  `road_network.py`, `triggers_editor.py`, `game_window.py`'s
  wiring of the new triggers editor) integrate unchanged — the
  numpy A* speedups and the triggers-editor UI are still
  exactly as v0.36 shipped them.

## What's next, if you want to keep going

The remaining ~6,150 lines in `game_window.py` cluster roughly as:

- **Rendering** (HUD, minimap, overlays, info panel, tooltips,
  graphs): ~2,400 lines. The biggest remaining cluster, but
  the trickiest to extract because it cross-references
  `_create_gui_texts`-managed text objects throughout. Worth a
  separate refactor if anyone touches `on_draw` again.
- **Input** (`on_mouse_*`, `on_key_press`): ~2,000 lines. The
  giant `on_key_press` is essentially a dispatch table that
  could be split as soon as someone wants to.
- **Game-loop / placement** (`_game_tick`, `_try_place`,
  `_diagnose_placement_failure`): ~1,000 lines. Tightly
  coupled to engine state; cleanest left where it is.
- **Trigger editor**: ~900 lines. Already self-contained;
  would extract cleanly as a ninth mixin if needed. It was
  left in place this drop only to keep the diff smaller —
  it's a candidate for v0.38.
