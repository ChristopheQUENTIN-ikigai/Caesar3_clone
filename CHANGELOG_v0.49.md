# Changelog — v0.49

**Theme:** combat stances — the cheap ones first. Per the campaign
re-plan (regroup stances by cost), this drop does the four stances that
reuse existing soldier machinery (morale/home/patrol); the expensive
ones (siege, scout) wait for a later drop because they need systems that
don't exist yet (enemy-building targets; fog-of-war).

Drop 3 of the command campaign (v0.47 model → v0.48 selection+movement →
**v0.49 cheap stances** → formations drop → expensive stances).

## Shipped

### Four persistent stances (`Soldier._apply_command_stance`)

Resolved each tick before default autonomy, re-asserting until the player
changes the order:

- **DEFEND (hold)** — anchors on the current tile; engages an enemy that
  comes within sight *of the anchor* but never chases beyond it; drifts
  back to the anchor when clear. The "hold this ground" stance.
- **REST** — holds position, recovers morale at 2× and never engages.
  For pulling a battered unit off the line.
- **PATROL** — loops between the anchor tile and the order's waypoint,
  turning around at each end.
- **RETREAT** — walks home ignoring enemies; clears itself on arrival
  (a commanded fall-back, distinct from the morale-driven auto-retreat).

These layer cleanly: a stance overrides wandering, but DEFEND still
fights intruders. All reuse existing fields (`morale`, `home_row/col`,
`sight_radius`, `_step_toward`) — no new subsystems.

### Stance hotkeys (command mode + selection)

Active only in command mode with a non-empty selection (they return
before the global key bindings see them): **H** = defend/hold, **Y** =
rest, **J** = patrol, **K** = retreat. Issuing re-anchors DEFEND/PATROL
at each unit's current tile; PATROL defaults its waypoint a few tiles
east when none is given. Partial obedience reported via notification.

## Cost-based re-plan (why these four)

A codebase audit set the split:
- **Cheap (this drop):** defend/rest/patrol/retreat — morale, home, and
  patrol machinery already exist.
- **Expensive (deferred):** SIEGE needs a building-target + enemy-
  building concept that isn't modelled; SCOUT needs fog-of-war/vision
  reveal that doesn't exist. Building these means building the systems
  under them first — a separate, larger drop.

## Tests — `tests/test_commands.py` (now 34, +6)

Retreat walks home and clears on arrival; rest recovers morale without
moving; defend holds its anchor with no enemy and steps toward one in
sight; patrol oscillates between anchor and waypoint; stance persistence
in CommandState.

Suite: **1157 passed, 0 failed** (was 1151 at v0.48 entry; +6).

## Files touched

- `walkers.py` — `Soldier._apply_command_stance` + hook in
  `pick_target_with_world`.
- `game_window.py` — stance hotkeys (H/Y/J/K) in command mode.
- `version.py` — `v0.48` → `v0.49`.

## In-game checks (need a GL context)

In command mode, select soldiers and: **H** to hold a line (they stop
wandering, fight what comes), **Y** to rest (watch morale recover), **J**
to patrol, **K** to retreat home. The selection overlay (v0.48) shows
who's selected.

## Remaining campaign

- **Formations drop** — explicit group/split/join (cohesive movement),
  its own drop per the re-plan.
- **Expensive stances** — siege (after an enemy-building/target model)
  and scout (after fog-of-war). May be preceded by building those
  systems.
- **ATTACK as a persistent pursuit** — currently the one-shot right-click
  attack from v0.48; a persistent "hunt this target" stance could join
  the expensive drop.
