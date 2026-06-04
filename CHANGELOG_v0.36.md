# Caesar III Clone — v0.36

## Headline

The fifth authoring tool — the **Event editor** — now has a runtime.
v0.35 had four library editors (Map, Trigger/Events, RPG Request,
Cutscene) that each owned one file and could not see each other; an
author wiring a cutscene to fire after an RPG decision had to cross
their fingers that the trigger_flag chain worked. v0.36 introduces
the `triggers` block on `map.json`, a `TriggerManager` to dispatch
it, and a first scripted scenario (`tribute_crisis`) that exercises
the whole pipeline end-to-end.

This drop is the **runtime + content** half of the Event editor
feature. The graph-view UI itself is a follow-up; the schema and
dispatcher land first so authors editing map JSON by hand can already
ship scripted scenarios.

## F1 — `TriggerManager` (new `triggers.py`)

A per-map dispatcher, same shape as `EconomicEventManager`. Each
trigger is one edge in the scenario flag graph:

```json
{
  "id":   "branch_refused",
  "when": {"kind": "on_flag", "flag": "tribute_refused"},
  "do": [
    {"kind": "play_cutscene", "id": "cutscene_caesar_wrath"},
    {"kind": "fire_event",    "name": "Caesar Displeased"},
    {"kind": "schedule_event","event": "Barbarian Raid",
     "trigger": {"kind": "at_tick", "value": 3600}}
  ],
  "once": true
}
```

`when` kinds (the first six mirror v0.28 `scheduled_events` verbatim
so authors can move wiring between fields without semantic surprise;
the last two are net-new and are the bridge between the flag graph
and the timeline):

  * `at_tick`, `at_year`, `at_month`
  * `on_population_above`, `on_treasury_below`
  * `random_after_tick`
  * `on_flag` (with `state: "set"` or `"unset"`)
  * `on_event_fired`

`do` kinds — `play_cutscene`, `fire_request`, `fire_event`,
`set_flag`, `schedule_event`. Effects compose: a single trigger can
fan out to a cinematic, an event, and a delayed follow-up in one
dispatch.

The manager is a thin dispatcher. It does **not** own the cutscene
library, the request library, or the event registry — those live
behind their own editors. Callbacks injected at construction
(`cutscene_callback`, `request_callback`, `flag_setter`,
`event_manager`) keep the separation-of-concerns boundary clean. The
same reason `cutscenes.py` is a sibling of `events.py` rather than a
subclass.

Auto-connects to the `signals` bus at construction time so
`on_event_fired` triggers work without the caller having to forward
firings. Round-trips through `to_dict` / `load_dict` with `fired`-
state and `flags`-set preservation, so a save mid-scenario doesn't
replay one-shot triggers on reload.

## F2 — `mapfile.py` reads + persists the new fields

Two new top-level keys on `map.json`:

  * `triggers` — the new wiring array.
  * `scenario_libraries` — pointers to scenario-local cutscenes /
    requests / events JSON files. Default `{}` means "use the
    global `data/*.json` libraries"; setting any of them overrides
    the corresponding library for the duration of the scenario.

Loaded by `_apply_world_to_game`; persisted by `serialize_for_save`.
The live `TriggerManager.triggers` is the source of truth on save
(so any `schedule_event` effects the manager has dispatched at
runtime get captured), with `game.scenario_triggers` as the
fallback for the cold-load case.

Pre-v0.36 maps continue to load unchanged: absent `triggers` =
empty list, absent `scenario_libraries` = global libraries.

## F3 — `game_window.py` wires the manager into the main loop

Four hunks:

  * `__init__` — constructs `self.trigger_manager` right after
    `self.event_manager`, with all four callbacks wired
    (`fire_cutscene_for_flag_id`, `fire_request_for_id`,
    `fire_cutscene_for_flag`, `event_manager`).
  * Per-tick `update` — ticks the trigger manager right after the
    event manager so `on_event_fired` triggers see this-tick
    firings via the signal bus.
  * `fire_cutscene_for_flag` — forwards to
    `trigger_manager.on_flag_set` so `on_flag` triggers fire next
    tick. The flag forwarding happens **before** the cutscene
    lookup, so a flag with no matching cutscene still wires the
    trigger graph.
  * Two new by-id helpers:
      * `fire_cutscene_for_flag_id(cid)` — plays a cutscene by its
        `id` (distinct from `fire_cutscene_for_flag` which looks
        up by `trigger_flag`).
      * `fire_request_for_id(rid)` — pushes an RPG request onto
        `self._pending_rpg_requests`. The dispatcher that pops
        from that queue and shows the modal is a follow-up.

`_rpg_requests_library` and `_pending_rpg_requests` are now seeded
in `__init__` alongside `_cutscenes_library` (used to be hasattr-
guarded on first use; the seed is a small maintainability cleanup
that came with this drop).

## F4 — Scenario: `The Tribute Crisis`

First scripted scenario, lives at `data/scenarios/tribute_crisis/`.
A small but complete arc: opening narration → grow the town → a
four-decision tribute request from Caesar → branching outcomes →
barbarian raid → recovery decision → victory / defeat. Exercises
every `when` kind except `at_year` and every `do` kind.

Files (each authored in its own editor; the five-tool clean split
is documented in the scenario README):

  * `map.json` — world + `triggers` block + `scenario_libraries`
    pointers
  * `cutscenes.json` — 7 cutscenes
  * `rpg_requests.json` — 2 requests, 7 decisions between them
  * `events.json` — 3 scenario-local events
  * `assets/` — 8 placeholder scenes + 3 portraits, runtime
    copies of the same images sit in `assets/textures/scene/` and
    `assets/textures/portraits/` so the renderer finds them by
    basename

The placeholder generator moved to
`tools/generate_tribute_crisis_placeholders.py` (was a one-shot
script with a hardcoded output path; now takes `--project-root`
and writes to both the scenario tree and the runtime trees).

### Scenario quality improvements over the v1 drop

The v1 (separately-delivered) drop of the scenario had three
authoring loose ends — flags whose set-decisions had no
corresponding trigger to fire a payoff cutscene. v0.36 closes them:

  * **`tribute_partial` branch.** Decision 3 of the tribute request
    ("Pay gold, withhold iron") now lights up `branch_partial` →
    `cutscene_caesar_uneasy` ("A Half-Answer to Rome") + the
    `Caesar Displeased` event. Previously the player picked it and
    got only the displeased event with no narrative beat.
  * **`barbarian_raid_abandoned` branch.** Decision 3 of the raid-
    aftermath request ("Send them away") now lights up
    `branch_raid_abandoned` → `cutscene_raid_abandoned` ("The
    Village That Was Sent Away"). Previously the player got the
    happiness hit and silence.
  * **`branch_pleased` uses scenario-local event.** Now schedules
    `Frontier Trade Boom` (from the scenario's `events.json`)
    rather than the global `Trade Boom`. Keeps the scenario self-
    contained — a future tuning pass touches one file, not the
    global library.
  * **Messenger pacing.** Population threshold for Caesar's
    tribune to arrive bumped 80 → 100. From a start of 60, hitting
    80 is often a single housing-tier-upgrade away; 100 gives the
    player a real chance to settle in.
  * **`Iron Vein Exhausted` flavor event.** The scenario-local
    event had no caller in v1 — added as a `random_after_tick`
    scheduled event around year-2-ish for ambient texture.
  * **Two new placeholder scenes** to back the new cutscenes:
    `caesar_uneasy` (amber palette, between `caesar_pleased` and
    `caesar_wrath`) and `abandoned_village` (cooler grey-brown,
    distinct from `burning_village`).

The graph after these changes (every node a library entry, every
edge a `triggers` array entry):

```
       intro_on_start  (at_tick=0)
           │
           ▼
   [intro_tribute_crisis]
           │  (grow to pop > 100)
           ▼
   messenger_arrives_when_town_grows
           │
           ▼
   [caesar_tribute_500]   ← 4 decisions
       │       │       │       │
       │   set:tribute_paid    │   set:tribute_refused
       │       │       │       │
       │   set:tribute_partial │
       ▼       ▼       ▼       ▼
   branch_  branch_  branch_  branch_
   pleased  partial  refused  refused
   (FTB)    (CD)     (CD+raid@3600)
       │       │       │
       ▼       ▼       ▼
   [pleased][uneasy][wrath] cutscenes
                   │
                   ▼ (raid lands, scheduled or refused-branch)
              raid_survivor_arrives
                   │
                   ▼
              [raid_aftermath]    ← 3 decisions
              ┌────┴────┬─────────┐
              │         │         │
       set:survived  set:survived  set:abandoned
              │         │         │
              ▼         ▼         ▼
        victory_path        branch_raid_abandoned
              │                   │
              ▼                   ▼
        [cutscene_victory]   [cutscene_raid_abandoned]

  (parallel hard-fail path, any time:)
    defeat_on_collapse  (on_treasury_below=-200)
       │
       ▼
   [cutscene_defeat]
```

## F5 — End-to-end tests

`tests/test_trigger_manager_e2e.py` — 8 tests driving the actual
scenario JSON through the manager with stub callbacks. Asserts the
`at_tick=0` opener, the population-threshold tribune, the full
refusal-branch `do` list (cutscene + event + scheduled event), the
signal-bus bridge from a fired event to an `on_event_fired`
trigger, the treasury-collapse defeat path, the `once=True`
guarantee, the save/load round-trip preserving `fired`/`flags`
state, and graceful handling of unknown `when.kind`/`do.kind`.

The population-threshold test reads the threshold off the scenario
JSON itself rather than hardcoding 80 or 100, so future scenario
tuning passes don't silently break the assertion.

## F6 — Splash menu: **Load scenario**

The pre-v0.36 splash had **Play custom map**, a picker rooted at
`data/maps/`. That doesn't surface the new `data/scenarios/<id>/`
bundles — and copying a scenario map into `data/maps/` would
either duplicate or symlink, both worse than just adding the right
picker.

  * New splash button **Load scenario** sits next to **Play custom
    map** in the button stack (same "load a world from disk" cluster).
  * New picker modal `_draw_splash_load_scenario` mirrors the
    existing load-map picker — same backdrop, gold border, click
    rows / Esc / click-outside dismissal. Each row shows the
    friendly name (from the map JSON's `name` field) above the
    scenario id, taller rows than the map picker because each
    entry carries two text lines.
  * New `mapfile.list_scenarios()` enumerates subdirectories of
    `data/scenarios/` that contain a `map.json`. Returns
    `[{"id", "name", "path"}]`; the path is data-dir-relative so
    the picker hands it straight to the loader. Folders without
    a `map.json` are skipped silently — in-progress authoring is
    not an error.
  * `mapfile.load_map` refactored: the pre-existing entry point
    (which joins with `MAPS_DIR`) now delegates to a new
    `load_map_path(game, path)` that takes any path. The legacy
    signature is preserved bit-for-bit — `load_map(game, filename)`
    still does what it did. The scenario picker calls
    `load_map_path` directly with the data-dir-relative path.
  * New `SCENARIOS_DIR = DATA_DIR / "scenarios"` in `constants.py`.

7 new tests in `tests/test_v036_scenario_picker.py` cover the new
helpers: picker discovers the shipped bundle, returns empty when
the directory is missing, skips work-in-progress folders, falls
back to folder name on unreadable JSON, alphabetical order,
`load_map_path` and `load_map` return False (don't raise) on
missing paths.

## F7 — Splash menu: **Event editor** (informational stub)

The graph-view UI for the Event editor is a v0.37 follow-up. Until
then, a splash button surfaces an info modal documenting the
schema and — explicitly — the responsibility split that prompted
this whole drop:

  * **Trigger editor** (existing splash button) owns *what events
    do* — the effects, banner text, timed modifiers each event
    applies when fired. Library = `data/events.json`.
  * **Event editor** (new) owns *when those events fire and what
    cascades from them* — the wiring graph. Library = the
    `triggers` block on `map.json`.

The modal is a quick reference: header + two columns listing
every `when.kind` and `do.kind` with their required fields,
plus a footer that tells the author exactly where to edit
(`data/scenarios/<id>/map.json`'s top-level `triggers` array)
and points at `data/scenarios/tribute_crisis/` as a worked
example. Dismisses on Esc / click anywhere. Toggles on a second
press of the splash button (same convention as Credits).

When the graph-view UI ships, this handler swaps from "open info
modal" to "open editor" — the splash button itself stays.

## Files touched

```
triggers.py                                          NEW (F1)
mapfile.py                                           (F2, F6 — 5 hunks:
                                                     scenarios picker
                                                     + load_map_path
                                                     refactor)
game_window.py                                       (F3, F6, F7 —
                                                     splash modal flags,
                                                     two splash buttons,
                                                     two draw methods,
                                                     click + Esc wiring,
                                                     __init__ cleanup)
constants.py                                         (F6 — SCENARIOS_DIR)
tests/test_trigger_manager_e2e.py                    NEW (F5 — 8 tests)
tests/test_v036_scenario_picker.py                   NEW (F6 — 7 tests)
data/scenarios/tribute_crisis/                       NEW (F4)
  README.md, map.json, cutscenes.json,
  events.json, rpg_requests.json,
  assets/scene/*.png, assets/portraits/*.png
assets/textures/scene/*.png                          + 8 placeholder scenes
assets/textures/portraits/*.png                      + 3 placeholder portraits
tools/generate_tribute_crisis_placeholders.py        NEW (F4 — was a
                                                     scenario-local
                                                     one-shot)
CHANGELOG_v0.36.md                                   NEW (this file)
```

## Test status

  * 15 new tests across `tests/test_trigger_manager_e2e.py` (8)
    and `tests/test_v036_scenario_picker.py` (7), all green.
  * 953 passing on a clean run (938 v0.35 + 15 new).
  * Same 16 pre-existing failures as v0.35 (the stale farm-`food`-
    chain tests called out in `CHANGELOG_v0.35.md` — engine is
    correct, tests are out of date). **Zero new regressions.**

## Known follow-ups

  * **Event editor UI.** The graph view over the four library
    editors. Sits on top of the runtime that this drop ships; no
    further engine changes needed to build it.
  * **RPG request dispatcher.** `fire_request_for_id` queues
    requests onto `_pending_rpg_requests`; a consumer that pops
    from the queue and shows the modal is still TBD. The contract
    is in place.
  * **End-of-game on defeat.** `defeat_on_collapse` plays the
    defeat cutscene but the engine has no game-over mechanic, so
    the player can keep playing in a broken state. The cutscene's
    "Scenario Lost" wording promises something the engine doesn't
    deliver. Game-over support is its own drop.
  * **Migration for existing maps.** None of `data/maps/*.json`
    carry a `triggers` block today; the patched loader treats
    absent = empty list, so they continue to load unchanged. No
    migration step needed.
