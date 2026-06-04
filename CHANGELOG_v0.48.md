# Changelog — v0.48

**Theme:** the command interface becomes *drivable*. This drop wires the
GL selection UI deferred from v0.47 and adds the movement orders — so for
the first time you can click-select units and order them around in-game.

Drop 2 of the command campaign (v0.47 foundation → **v0.48 selection UI +
movement** → v0.49 combat stances → v0.50 special stances).

## Shipped

### Command mode + selection UI (key **U**)

- **'U' toggles command mode.** While on, left-click/drag selects player
  units and right-click issues orders; build/demolish are suspended so
  the two interaction models don't collide. Toggling shows a notification.
- **Selection:** left-click selects the single player unit under the
  cursor; left-**drag** box-selects every player unit in the rectangle;
  **Shift** adds/toggles into the existing selection. Empty-click clears.
- **Control groups:** **Ctrl+0-9** assigns the current selection to a
  group; plain **0-9** recalls it (skipping dead members).
- **Overlay:** selected units get a green ring; the live drag-box is
  drawn; a unit under a move/attack order gets a line to its destination
  tile (green for move, red for attack).

### Movement orders

- **Right-click = MOVE** to the tile (or **ATTACK** if an enemy is on
  it). Applied to every selected unit the verb is legal for; partial
  obedience is reported ("6/8 units can move here").
- **Ships obey MOVE/STOP too** — a player MOVE sets the ship's goal so
  the existing A* sea-pathing sails it there; STOP resumes scripted
  behaviour. (Soldiers already obeyed MOVE/STOP from v0.47.)
- **Embark / disembark** (the naval movement pair):
  `WalkerManager.command_embark(soldiers, transport)` boards soldiers up
  to the trireme's 10-slot cap (boarded troops leave the live map into
  `transport.cargo`); `command_disembark(transport, game_map)` lands them
  on an adjacent shore (no-op in open water). These are the backend the
  embark/disembark verbs drive.

## Tests — `tests/test_commands.py` (now 28, +5)

Ship obeys a MOVE command (sails to the tile); embark boards soldiers and
respects the 10-slot cap; disembark lands troops on a shore and is
blocked in open water.

Suite: **1151 passed, 0 failed** (was 1146 at v0.47 entry; +5).

## Files touched

- `game_window.py` — `command_mode` + `CommandManager`; command-mode
  branches in `on_mouse_press`/`on_mouse_drag`/`on_mouse_release`;
  `_cmd_commit_selection`, `_cmd_issue_order_at`, `_draw_command_overlay`;
  'U' toggle and 0-9 control-group keys.
- `walkers.py` — `Ship.pick_target` honours MOVE/STOP;
  `WalkerManager.command_embark` / `command_disembark`.
- `version.py` — `v0.47` → `v0.48`.

## In-game checks (this is the drivable drop — please try these)

1. Press **U** → "Command mode ON" notification.
2. **Click** a soldier → green ring appears. **Drag** a box over several
   units → all ringed. **Shift-click** another → added.
3. **Right-click** an empty tile → selected units move there (watch the
   destination ring + line). Right-click an enemy → they attack it.
4. **Ctrl+1** to save a group, reselect something else, **1** to recall.
5. Select a warship, right-click distant water → it sails there via the
   A* route (toggle **P** to see the path).

Feedback most useful on: does click-vs-drag feel right (the 12-world-px
threshold)? Is the selection ring readable? Should command mode auto-
engage when you click a unit (vs the explicit 'U' toggle)?

## Known scope lines

- **group/split/join** verbs: the control-group *selection* mechanic is
  in, but explicit GROUP/SPLIT/JOIN *orders* (named formations that move
  as one) are not yet — folded into the model, resolution deferred.
- The remaining stance verbs (attack beyond the one-shot right-click,
  defend, patrol, retreat, siege, scout, rest) are v0.49–v0.50.
