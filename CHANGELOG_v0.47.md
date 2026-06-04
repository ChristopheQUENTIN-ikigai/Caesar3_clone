# Changelog — v0.47

**Theme:** foundation of the military-unit command campaign. The pure
model everything else rides on — selection, control groups, the order /
command-state abstraction, and the full 12-verb vocabulary — plus the
first two verbs (MOVE/STOP) actually resolving into movement.

This is **drop 1 of a multi-drop campaign**:
- **v0.47 (this)** — selection + command core + MOVE/STOP.
- v0.48 — movement orders (navpoint, embark/disembark, group/split/join).
- v0.49 — combat stances (attack, defend, patrol, retreat).
- v0.50 — special stances (siege, scout, rest).

## Shipped

### Command & selection model (`commands.py`, new pure-logic module)

No arcade imports — the testable core under the UI.

- **`Verb`** — the full vocabulary as a str-enum: STOP, MOVE, GROUP,
  SPLIT, JOIN, EMBARK, DISEMBARK, ATTACK, DEFEND, PATROL, RETREAT, SIEGE,
  REST, SCOUT. Declared in full now so later drops add only *resolution*,
  not model plumbing.
- **Land/naval legality** — `verb_applies_to(verb, naval=…)`. Unified
  system: EMBARK/SIEGE/SCOUT/REST/PATROL are land-only, DISEMBARK is
  naval-only, the rest universal. A mixed selection is fine; an order
  applies only to the units it's legal for.
- **`Order`** — verb + optional target (tile or unit id); coerces a bare
  string verb so UI/save callers can pass either.
- **`CommandState`** (on `unit.command`, attached lazily via
  `ensure_command`) — current order + persistent fallback stance +
  control-group number. Issuing a persistent stance updates the fallback.
- **`Selection`** — selected units by identity, drops dead units, with
  add/toggle (shift-click) and **0-9 control groups** (assign/recall,
  recall skips dead members).
- **`CommandManager`** — owns the selection; `issue(order)` stamps it
  onto every selected unit it's legal for and returns the accepted list
  (partial obedience for the UI to report); control-group passthrough.

### MOVE/STOP resolution

`Soldier.pick_target_with_world` now checks for a player command first
(`_apply_command_move`): a MOVE order steps the soldier toward its
target tile and clears on arrival (resuming autonomy); STOP cancels.
Other verbs are stored but not yet acted on (later drops). This makes
player move-commands actually drive soldiers while leaving auto-combat
intact when no order is active.

### Drag-box selection backend

`WalkerManager.player_units_in_rect(r0,c0,r1,c1)` returns the
player-controllable units (Soldiers + friendly Ships; excludes enemies,
citizens, delivery walkers) in a tile rect — the headless backend the UI
drag-box calls. Handles reversed corners.

### Ships are all naval now

`category = "naval"` moved to the `Ship` base (was only on
Warship/EnemyShip), so trade/transport ships classify correctly for verb
legality. No combat behaviour changed (verified by the full suite).

## Tests — `tests/test_commands.py` (23)

Verb legality (universal/land/naval), Order coercion, CommandState
stance rules, lazy attach, Selection dedup/toggle/dead-drop, control
group assign/recall/skip-dead, CommandManager partial-obedience and
group stamping, is_naval, MOVE drives a soldier to its tile, STOP clears,
and the drag-box rect backend (incl. reversed corners, enemy exclusion).

Suite: **1146 passed, 0 failed** (was 1123 at v0.46 entry; +23).

## Files touched

- `commands.py` (new) — the whole model.
- `walkers.py` — `Soldier._apply_command_move` + hook in
  `pick_target_with_world`; `Ship` base `category="naval"`;
  `WalkerManager.player_units_in_rect`.
- `version.py` — `v0.46` → `v0.47`.

## Deferred to v0.48 (the GL-dependent UI)

The click-to-select / drag-box-draw / right-click-move / control-group
*key handling* in `game_window.py` is **not** wired in this drop. The
backend it needs (`player_units_in_rect`, the command model, MOVE
resolution) is all here and tested; the actual on-screen selection
rectangle, selection highlight, and order feedback are GL-only and best
iterated with eyes on the screen, so they lead the v0.48 movement drop.
What this means: in-game you can't yet click-select units — that wiring
is next. The model underneath is proven.
