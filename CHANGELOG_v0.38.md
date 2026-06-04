# v0.38 — RPG request runtime (closes audit finding 7.1)

## What was broken
The RPG-request editor, loader, `fire_request` trigger effect, and the
`game_window._pending_rpg_requests` queue all shipped (v0.31–v0.36), but
the **consumer** was never built: the queue was written in one place and
read in zero. A queued request displayed nothing; a decision's
`effects`, `delay_ticks`, `set_flag`, and `fire_event` never executed.
CI stayed green because every layer was tested in isolation — the
integration endpoint had no test because it had no code.

## What this drop adds
- **`rpg_player.py`** — the runtime presenter, a deliberate sibling of
  `cutscene_player.py`: `RpgPlayerState`, `start`, `choose`, `close`,
  `decisions`, `draw`, `handle_click`. Lazy-arcade so the logic is
  headless-testable. Renders scene + portrait + request text + one
  button per decision, with a terse outcome hint on each button.
- **`game_window.py`** wiring (6 points):
  1. `__init__`: `rpg_player_state`, `_rpg_delayed_effects`, `_rpg_was_paused`.
  2. `on_update`: `_drain_pending_rpg_requests()` before the pause gate
     (a request fires even while idle, then pauses the sim).
  3. `_game_tick`: `_process_rpg_delayed_effects()` (the "pay next
     month" deferral).
  4. `on_draw`: panel drawn at the cutscene precedence tier.
  5. `on_mouse_press`: panel eats clicks; a decision click resolves +
     applies + restores paused state.
  6. `on_key_press`: number keys 1–N pick a decision; Esc picks the
     first (a request must be answered — no free dismiss / soft-lock).
- **`apply_rpg_decision`** — immediate effects → `apply_event_effects`;
  delayed effects → the queue; `diplomacy` → `DiplomacyTracker.add`;
  `set_flag` → `fire_cutscene_for_flag` (also notifies the trigger
  graph); `fire_event` → `event_manager._trigger`. Reuses the exact
  paths the cutscene/trigger systems already use.

## Tests
`tests/test_rpg_player.py` — 12 tests covering present, choose,
out-of-range safety, double-choose guard, immediate apply, delayed
defer, fire-at-tick, and the refuse→event path. The endpoint test that
would have caught 7.1.

## Verification
- `python -m pytest tests/test_rpg_player.py -q` → 12 passed.
- Full suite: **982 passed, 16 failed** — the 16 are the identical
  pre-existing stale-farm / debug-logging failures from the v0.37
  baseline. **Zero regressions.**

## NOT yet verified (important)
The build + click paths were validated by headless tests and code
review, not by running the Arcade game (no GL context in the build
environment). **Load a scenario in-game and fire a `fire_request`
trigger once** to confirm the panel renders and a click resolves it
before relying on this in a shipped scenario.

## Follow-on (still TODO, not in this drop)
- `delay_ticks` / `_rpg_delayed_effects` should be added to
  `saveload.py` so a deferred payment survives save/load.
- Naval transport ships and the buy/sell trading spread (separate
  engine tasks).

---

## Addendum — completed in the same v0.38 cycle

Beyond the RPG runtime, the following also shipped and are covered by tests:

- **Naval system** — `Ship` base + `TradeShip` / `TransportShip` / `Warship`
  in `walkers.py`; manager ship API + `_process_ships`; three naval units
  in `units.json`; scenario `naval`-block spawning in `mapfile.py`;
  warships wired into `_resolve_combat`. Tests: `tests/test_naval.py` (12).
- **Buy/sell trade spread** — `bartering.TRADE_SPREAD` (0.15) applied in
  `gold_trade.compute_buy/sell`. Tests: `tests/test_trade_spread.py` (4),
  plus the four v0.35 gold-trade tests updated for the spread.
- **Green test bar** — all 16 standing failures fixed (stale farm-output
  tests rewritten to assert on `wheat`/`gross_production`; debug-logging
  mock replaced with a defaulting namespace). Version test made
  bump-robust. **Final suite: 1018 passed, 0 failed** (v0.37 baseline was
  970 passed / 16 failed).

Scenario `data/scenarios/mare_nostrum/` carries a `naval` roster (2
transports with 5+3 embarked legionaries, 2 warships, 1 trade ship) that
spawns at load, plus the landing cutscene/trigger.
