# Scenario: The Tribute Crisis

A small, scripted scenario that exercises the full v0.36 authoring
pipeline — the four library editors (Map, Trigger/Events, RPG
Request, Cutscene) plus the new Event editor that wires them
together via the `triggers` block on `map.json`.

> **History.** The first drop of this scenario (alongside the
> v0.36 patch set) lived at `scenarios/tribute_crisis/` and assumed
> the trigger runtime hadn't shipped yet. v0.36 ships the runtime,
> integrates the scenario at `data/scenarios/tribute_crisis/`, and
> closes the three authoring loose ends from the v1 drop (see
> `CHANGELOG_v0.36.md` § F4 for the diff). This README documents the
> integrated state, not the proposal.

## Files in this folder

| File | Authored by | Schema source |
|---|---|---|
| `map.json` | Map editor + Event editor (the `triggers` block) | `mapfile.py` |
| `cutscenes.json` | Cutscene cinematic editor | `cutscenes.py` |
| `rpg_requests.json` | RPG request editor | `rpg_requests.py` |
| `events.json` | Trigger editor (owns the event library) | `events.py` |
| `assets/scene/*.png` | placeholder generator | — |
| `assets/portraits/*.png` | placeholder generator | — |

The library files follow exactly the same schemas as the project-
level `data/cutscenes.json` / `data/rpg_requests.json` /
`data/events.json` — they round-trip through `cutscenes.load_cutscenes`,
`rpg_requests.load_requests`, and `events.load_events` without
modification. The `map.json`'s `scenario_libraries` block points the
runtime at these scenario-scoped copies instead of the globals.

## The five editors — clean responsibility split

| Editor | Role | On-disk shape |
|---|---|---|
| **Map editor** | World — terrain, buildings, starting state | `map.json` (the bulk of the file) |
| **Trigger editor** | Event library — what Famine, Plague, etc. *do* | `events.json` |
| **RPG request editor** | NPC dialogue library | `rpg_requests.json` |
| **Cutscene editor** | Cinematic library | `cutscenes.json` |
| **Event editor** *(new in v0.36)* | Wiring — when things fire, in what order | `map.json` → `triggers` block |

The Event editor is the only place that sees all four libraries at
once, which is exactly what an author needs when they ask "what
happens after the tribute request resolves?". The graph-view UI is a
follow-up; v0.36 ships the file format + runtime that the UI will
sit on. Authors editing JSON by hand can already ship scripted
scenarios today.

## The `triggers` block — quick reference

Each entry is one edge in the scenario flag graph:

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
  "once": true,
  "notes": "free-text author comment, ignored at runtime"
}
```

### `when` kinds

| kind | extra fields | fires when |
|---|---|---|
| `at_tick` | `value: int` | `game_time >= value` |
| `at_year` | `value: int` | `game.year >= value` |
| `at_month` | `value: int` | `game.month == value` (any year) |
| `on_population_above` | `value: int` | `economy.population > value` |
| `on_treasury_below` | `value: int` | `economy.treasury < value` |
| `random_after_tick` | `value: int` | past `value`, 3 %/tick chance |
| `on_flag` | `flag: str`, `state: "set"\|"unset"` | the flag was set (default) or cleared this tick |
| `on_event_fired` | `event: str` | the named event just fired |

The first six mirror v0.28's `scheduled_events` so the same author
intent is expressible in either field. The last two are the bridge
between the flag graph and the timeline.

### `do` kinds

| kind | extra fields | effect |
|---|---|---|
| `play_cutscene` | `id: str` | resolve cutscene by id, start playback |
| `fire_request` | `id: str` | resolve request by id, push onto queue |
| `fire_event` | `name: str` | fire event through the event manager |
| `set_flag` | `flag: str` | set the named flag |
| `schedule_event` | `event: str`, `trigger: {kind, value}` | append to `scheduled_events` at runtime |

`once: true` (default) means the trigger fires at most once per
game. `once: false` re-arms after each firing — useful for recurring
beats (yearly tax-collection request, monthly market festival, …).

### `scenario_libraries`

Tells the runtime which library files belong to this scenario.
Absent (default) means "use the global `data/*.json`". Setting any
of the three keys overrides the corresponding global library for
the duration of the scenario:

```json
"scenario_libraries": {
  "cutscenes":    "data/scenarios/tribute_crisis/cutscenes.json",
  "rpg_requests": "data/scenarios/tribute_crisis/rpg_requests.json",
  "events":       "data/scenarios/tribute_crisis/events.json"
}
```

Paths are relative to the project root.

## The scenario itself — the flag graph

```
       intro_on_start  (at_tick=0)
           │
           ▼
   [intro_tribute_crisis]    ← opening cutscene
           │
           │   player builds, town grows past 100 pop
           ▼
   messenger_arrives_when_town_grows
           │
           ▼
   [caesar_tribute_500_scenario]    ← RPG request, 4 decisions
       │       │       │       │
       │       │       │       │
   set:    set:    set:    set:
   tribute tribute tribute tribute
   _paid   _paid   _partial _refused
       │       │       │       │
       ▼       ▼       ▼       ▼
   branch_pleased  branch_partial  branch_refused
       │               │               │
       │               │               ├ play cutscene
       │               │               │  (caesar_wrath)
       │               │               │
       ├ play cutscene ├ play cutscene │
       │  (pleased)    │  (uneasy)     ├ fire event:
       │               │               │   Caesar Displeased
       ├ schedule:     ├ fire event:   │
       │   Frontier    │   Caesar      └ schedule:
       │   Trade Boom  │   Displeased       Barbarian Raid
       │   (at 7200)                        (at_tick=3600)
       │                                            │
       │                                            │
       │   (parallel: random Barbarian Raid         │
       │    from scheduled_events @ tick > 3600)   │
       │                  │                         │
       │                  └────────────┬────────────┘
       │                               ▼
       │              raid_survivor_arrives
       │              (on_event_fired=Barbarian Raid)
       │                               │
       │                               ▼
       │              [raid_aftermath_scenario]   ← RPG request
       │                               │
       │              ┌────────────────┼────────────────┐
       │              │                │                │
       │       set: survived    set: survived    set: abandoned
       │           (rebuild)     (palisade)
       │              │                │                │
       │              ▼                ▼                ▼
       │              victory_path           branch_raid_abandoned
       │                  │                          │
       │                  ▼                          ▼
       │        [cutscene_victory]    [cutscene_raid_abandoned]
       │
       │   (Iron Vein Exhausted as ambient flavor,
       │    random_after_tick=5400, scheduled_events)


  (parallel hard-fail path, any time:)
    defeat_on_collapse   (on_treasury_below=-200)
       │
       ├ set_flag: treasury_collapsed
       ▼
   [cutscene_defeat]
```

Every node above is one entry in one of the four library files.
Every edge is one entry in `map.json`'s `triggers` block. That is
the clean split — the Event editor lets you author the edges
without leaving the libraries' authoring scopes.

## Installation

Already integrated in v0.36 — the scenario lives at
`data/scenarios/tribute_crisis/`, the placeholder assets at
`assets/textures/{scene,portraits}/`. From the splash menu, click
**Load scenario**; the picker enumerates every subdirectory of
`data/scenarios/` that ships a `map.json`. The scenario's
`scenario_libraries` block points the runtime at the local
`cutscenes.json` / `rpg_requests.json` / `events.json` for the
duration of the scenario.

There's also a splash **Event editor** button — informational only
in v0.36 (the graph-view UI is a v0.37 follow-up). It surfaces the
`triggers` schema reference so authors editing JSON by hand have
the kind list at the splash menu, not buried in the changelog.

To re-generate the placeholder PNGs (e.g. after editing the colour
palette in the generator):

```sh
python3 tools/generate_tribute_crisis_placeholders.py
```

The generator writes into both the scenario's own `assets/` tree
*and* the runtime `assets/textures/{scene,portraits}/` trees. Pass
`--scenario-only` to skip the runtime copy (useful when porting the
scenario into another project).

## Running the tests

```sh
python3 tests/test_trigger_manager_e2e.py
# or, under pytest:
python3 -m pytest tests/test_trigger_manager_e2e.py -v
```

Expected: `All 8 tests passed.` The suite drives the actual
scenario JSON through the manager with stub callbacks; if you edit
`map.json`, `cutscenes.json`, or `rpg_requests.json` and break a
flag chain, the relevant test will fail with a specific assertion.

## Known limitations

  * The cutscene `cutscene_defeat` says "Scenario Lost" but the
    engine has no end-of-game mechanic yet — the player can keep
    playing in a broken state. Game-over support is a separate
    drop (called out in `CHANGELOG_v0.36.md`).
  * `fire_request_for_id` queues onto `_pending_rpg_requests` but
    the modal-display consumer is also TBD. Until it ships, the
    queued request lands silently. The full chain works in the
    e2e tests (which inspect the queue directly).
