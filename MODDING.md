# Modding Caesar III Clone

A guide for community modders. Most game content lives in JSON files
that can be edited without touching Python — buildings, units, events,
scenarios, RPG-style dialogues, and cutscenes are all data-driven.
This document is the single reference for every extension point the
engine reads at startup.

## Quick start

1. Run the game once: `python main.py`.
2. From the splash menu, the in-game **editors** let you author most
   of the content listed below without leaving the game:
   - **Buildings editor** — tweak any building's cost, workers,
     production, consumption, build materials, size, sprite.
   - **Unit editor** — tweak any military unit's HP, damage, speed,
     sight, patrol, training time, cost, upkeep, flags.
   - **Trigger editor** — schedule events that fire at a specific
     year/month or in response to a city milestone.
   - **RPG request editor** — author NPC dialogues with multi-choice
     decisions.
   - **Cut-scene cinematic editor** — author scripted slide decks.
   - **Map editor** — author maps up to 64×64 tiles with terrain
     features (fertile soil, forest, stone, iron, gold, copper,
     water, …).
3. The editors all write back to `./data/*.json` via an atomic
   `tmp + rename`, so you can also hand-edit those JSON files
   directly. Keep a backup; a malformed file fails the next load
   loudly.
4. Sprite assets go under `./assets/textures/<category>/`
   (`buildings/`, `terrain/`, `walkers/`, `units/`, `portraits/`,
   `scenes/`). Names map through `data/textures.json`.

## Extension points at a glance

| File | Loaded by | Purpose |
|------|-----------|---------|
| `data/buildings.json` | `building.BuildingRegistry` | every placeable structure |
| `data/units.json` | `units.UnitRegistry` | every military walker type |
| `data/events.json` | `events.EconomicEventManager` | timed scenario events |
| `data/rpg_requests.json` | `rpg_requests.RpgRequestLibrary` | NPC dialogue prompts |
| `data/cutscenes.json` | `cutscene_player` | scripted slide decks |
| `data/textures.json` | `textures.TextureRegistry` | id → image filename map |
| `data/maps/*.json` | `mapfile` | scenario maps |
| `bartering.py` `STOCK_PRICES` | `bartering`, `gold_trade` | barter + gold-trade prices |
| `balance.py` `Balance` dataclass | `balance` | hundreds of tunable numbers |

## buildings.json — the headline schema

Each entry maps a building id to a dict. Bakery is a representative
example:

```json
"bakery": {
  "name": "Bakery",
  "color": [210, 170, 110],
  "size": [1, 1],
  "cost": 70,
  "production": {"bread": 10},
  "consumption": {"flour": 2, "wood": 1},
  "workers": 3,
  "worker_role": "worker",
  "description": "Bakes bread (nutrient) from flour. Needs road.",
  "category": "commerce",
  "requires_road": true,
  "construction_ticks": 18
}
```

### Required fields

- `name` — display name (also accepted as a textures.json key)
- `size` — `[width_cols, height_rows]`, each 1..10
- `cost` — gold to place
- `category` — one of `infra`, `housing`, `water`, `agriculture`,
  `mining`, `industry`, `commerce`, `bank`, `entertainment`,
  `religion`, `politic`, `education`, `health`, `security`,
  `military`

### Common optional fields

- `color` — `[r,g,b]` fallback colour if no sprite is found
- `description` — appears in the inspector tooltip
- `production` — `{resource_id: qty_per_tick}`; output when staffed
  and serviced; written each tick if storage allows
- `consumption` — `{resource_id: qty_per_tick}`; production gated by
  availability (a bakery with no flour stops producing bread)
- `workers` — how many citizens this building employs
- `worker_role` — `worker` (default), `priest`, `actor`, `trader`,
  `soldier`, etc.; modders can invent new roles, they're free-form
- `requires_road` — production halts unless the footprint touches a
  road
- `housing` — citizens this building shelters (houses; non-zero only
  for `housing` category)
- `storage` — non-nutrient resource cap (warehouses)
- `construction_ticks` — placement-to-operational time
- `needs_terrain` — `"any"` or `"water"` (port-like buildings)
- `needs_feature` — list of terrain features required at footprint
  (`["forest"]` for lumber mill, `["iron"]` for mine)
- `needs_water_adjacent` — must touch a water tile (reservoirs)
- `needs_water_source` — must be connected through an aqueduct chain
- `material_cost` — `{resource_id: qty}` deducted from stockpile on
  placement (planks, stone, horses for a cavalry fort)
- `feature_yield_bonus` — `{feature: multiplier}` for soft bonuses
  (e.g. farm on fertile soil: `{"fertile_soil": 1.5}`)
- `provides_service`, `service_radius`, `service_intensity` — for
  service buildings (fountain provides water, prefecture provides
  fire prevention)
- `needs_services` — `{service: minimum_intensity}` requirements
- `tier_max` — only houses use this (4); how many evolution tiers
- `spawn_unit` — military buildings reference a units.json id

### Adding a new building (worked example: cheese shop)

1. Pick an unused id (snake_case): `cheese_shop`.
2. Add the entry to `data/buildings.json`:

   ```json
   "cheese_shop": {
     "name": "Cheese Shop",
     "size": [1, 1],
     "cost": 80,
     "production": {"cheese": 6},
     "consumption": {"livestock": 1},
     "workers": 4,
     "category": "industry",
     "requires_road": true,
     "construction_ticks": 24,
     "description": "Turns livestock into cheese (nutrient)."
   }
   ```

3. Drop a sprite at `assets/textures/buildings/cheese_shop.jpg`
   (any size — the engine scales to the footprint). Optional: add
   an entry in `data/textures.json` if your filename differs from
   the id.
4. If `cheese` is a new resource, add it to `bartering.STOCK_PRICES`
   so it can be traded; tier evolution and house demand follow the
   `NUTRIENTS` tuple in `constants.py` if you want it to be a
   subsistence good.
5. Restart the game (or, in-editor, click Save in the buildings
   editor — the registry hot-reloads).

## units.json — military walkers

```json
"heavy_cavalry": {
  "name": "Heavy Cavalry",
  "category": "cavalry",
  "hp": 70,
  "damage": 18,
  "speed": 0.04,
  "sight": 7,
  "patrol_radius": 10,
  "ranged": false,
  "armoured": true,
  "description": "..."
}
```

Fields the engine reads:

- `hp` — health pool at spawn
- `damage` — base hit
- `speed` — fraction of a tile moved per tick (0.05 = default)
- `sight` — Chebyshev tile radius for spotting enemies
- `patrol_radius` — how far a soldier wanders from the home garrison
- `ranged` — true uses extended sight to attack at distance (bowmen)
- `armoured` — true halves incoming damage
- `category` — `light`, `infantry`, `cavalry`, `siege` (display only)
- `sprite` — optional; defaults to the unit id

Cavalry units automatically get a 1.75× first-strike charge bonus
(see `walkers.py`).

## events.json — timed scenario events

A list of `{name, effects, pop, happy, msg, color}` dicts. Each
event mutates the economy when fired by the trigger system:

```json
{
  "name": "Famine",
  "effects": {"food": -150},
  "pop": -8,
  "happy": -15,
  "msg": "A terrible famine strikes!",
  "color": [200, 60, 60]
}
```

`effects` is a `{resource_id: delta}` dict — negative numbers drain,
positive numbers credit. Pair events with triggers via the
**Trigger editor**; that's where you set the year/month/condition
that fires the event.

## rpg_requests.json — NPC dialogues

NPC arrives with a portrait, says something, presents 2–4 decision
buttons each with delayed effects. Example:

```json
{
  "id": "caesar_tribute_1k",
  "npc_name": "MESSENGER",
  "npc_portrait": "messenger",
  "background_scene": "throne_room",
  "request_text": "Caesar demands a tribute of one thousand golden coins.",
  "resources": {"money": -1000},
  "trigger_flag": "caesar_tribute_paid",
  "trigger_event": "caesar_displeased",
  "decisions": [
    {"label": "Yes immediately",   "delay_ticks": 0,    "effects": {"money": -1000}, "set_flag": "caesar_tribute_paid", "happy": 0},
    {"label": "Yes for next month","delay_ticks": 600,  "effects": {"money": -1000}, "set_flag": "caesar_tribute_paid", "happy": -2},
    {"label": "Refuse",            "delay_ticks": 0,    "effects": {},                                                  "happy": -10, "fire_event": "caesar_displeased"}
  ]
}
```

- `delay_ticks` — 0 fires now; non-zero schedules effects for later
  (each tick is ~0.5 s at default speed; 600 ticks ≈ 5 minutes real
  time)
- `set_flag` — gameplay flag the trigger system can key on
- `fire_event` — name of an event from `events.json` to fire

Portraits live in `assets/textures/portraits/`. Scenes live in
`assets/textures/scenes/`.

## cutscenes.json — scripted slide decks

```json
{
  "id": "intro_welcome",
  "title": "Welcome to Caesar III",
  "trigger_flag": "game_started",
  "slides": [
    {"image": "throne_room", "caption": "Year 58 BC", "body": "..."},
    {"image": "forum",       "caption": "A Governor's Duty", "body": "..."}
  ]
}
```

Each slide has an `image` (resolved through `textures.json` /
`assets/textures/scenes/`), a caption, and a body paragraph. The
player advances with **Next**; the last slide swaps to **OK** which
closes.

## bartering.py / gold_trade.py — trade prices

`bartering.STOCK_PRICES` is a plain dict: `{resource_id: dn/unit}`.
This single table backs:

- **B-key barter** (good ↔ good at fixed ratios, 100 dn flat fee)
- **C-key gold trade** (good ↔ gold at the same per-unit price,
  100 dn flat fee on either side) — added in v0.35

Add a new tradeable resource by appending an entry. Both modules
pick it up immediately. No engine code changes needed.

## balance.py — gameplay tuning numbers

A `Balance` dataclass with ~150 fields covering pop growth rates,
tax revenue per capita, employment threshold, happiness equation
constants, walker speeds, fire spread rate, rebellion thresholds,
etc. The values are deliberately one-source-of-truth; every engine
module reads them off the singleton `BALANCE`.

For a community mod, copy the file, change values, ship as
`balance_mymod.py`, and either patch the import in `main.py` or load
through a config flag (TODO — there's no built-in selector yet).

## maps — scenario design

Scenario maps are JSON files under `data/maps/`. The map editor
writes them; you can also hand-edit. Each file holds:

- `name`, `width`, `height` (cols × rows, up to 64 × 64)
- `tiles` — flat list of tile types (grass, water, hill, desert,
  forest, fertile_soil, stone, iron, gold, copper, clay, fish)
- `pre_placed_buildings` — list of `{id, row, col}` for buildings
  that exist at scenario start
- `triggers` — references into `events.json`

The splash menu's **Play custom map** loads any file in this
directory; **Map editor** creates and renames them.

## Hotkeys (v0.35)

| Key | Action |
|-----|--------|
| `1-9, 0` | Select building (current tab) |
| `Tab` | Cycle palette category |
| `Arrows / WASD` | Pan camera |
| `Scroll wheel` | Zoom |
| `+ / -` | Speed up / slow down |
| `Space` | Pause |
| `T` | Cycle tax rate |
| `R` | Toggle tick recorder (JSONL diagnostics) |
| `B` | International bartering menu (good ↔ good) |
| `C` | **NEW v0.35** International trade menu (good ↔ gold) |
| `N` | **NEW v0.35** Nutrients coverage panel |
| `S` | Statistics panel |
| `J` | Jobs / employment panel |
| `Z` | Happiness equation debug overlay |
| `D` | Production diagnostics |
| `G` | Long-form graphs |
| `M` | Mini-map |
| `O` / `[` / `]` | Cycle overlay |
| `Y` | Yield to Caesar's demand |
| `H` | Help |
| `F5 / F9` | Quick-save / quick-load |
| `Esc` | Menu / close modal |

## Test suite

Run with `pytest tests/`. 938 tests pass on a clean v0.35 checkout.
**16 tests fail as inherited baseline from v0.34** — they assert the
legacy `food`-producing farm chain that was retired in the v0.34
cleanup. They are stale tests, not engine bugs. A follow-up drop
will rewrite them against the current wheat → flour → bread chain.
If you're contributing a feature, do not be alarmed by these red
lines; check `diff` against the baseline-failures list to see only
your impact.

When adding a new feature, follow the pattern of
`tests/test_v035_features.py`: a single file per release, every test
self-describing, no shared mutable state. The `conftest.py` fixture
suite gives you a fully-loaded `registry`, a `tiny_registry`, and a
`fast_balance` to compose from.

## Where the engine lives (file map)

- `main.py` — entry point
- `game_window.py` — the big one: window, draw, input, all the modals
- `economy.py` — per-tick production, consumption, taxes, happiness
- `walkers.py` — every moving entity (citizens, traders, soldiers, …)
- `pathfinding.py` — A* over the road network
- `services.py` — coverage propagation for water / religion / etc.
- `house_evolution.py` — when a tier-1 house levels up
- `storage.py` — granary / warehouse distribution
- `decay.py` — building wear-and-tear, fire
- `diagnostics.py` — broken-chain detection (D key)
- `recorder.py` — JSONL tick log (R key)
- `bartering.py` / `gold_trade.py` — international exchange
- `trade.py` — auto-execute trade routes
- `caesar.py` — tribute demands
- `diplomacy.py` — diplomacy score
- `rebellion.py` — unrest from sustained unhappiness/starvation
- `events.py` — timed events
- `cutscenes.py`, `cutscene_player.py`, `cutscene_editor.py`
- `rpg_requests.py`, `rpg_request_editor.py`
- `saveload.py` — game state persistence (JSON)
- `mapfile.py` — scenario map persistence
- `signals.py` — pub/sub bus
- `constants.py` — paths, grid size, hotkey map, palette categories,
  NUTRIENTS list
- `balance.py` — every tunable gameplay number
- `building.py` — Building dataclass + registry
- `units.py` — Unit dataclass + registry

## Reporting issues / sharing mods

The CHANGELOG files (`CHANGELOG_v0.27.md` through `CHANGELOG_v0.35.md`)
are the running history of player-flagged issues and how they were
fixed. When opening an issue or submitting a mod, reference the
relevant changelog so we know the version context.

---

*v0.35 • Last updated alongside the 5-feature drop (nutrients panel,
satiety fix, editor ±1, happiness readability, gold trade).*
