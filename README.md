# Caesar III Clone

A city-builder economy simulation in the spirit of *Caesar III*,
built with **Python 3.11+** and **Arcade 3.x**.

## Run

```bash
pip install arcade
python main.py
```

## Quick tour

- **Splash menu:** *New game* / *New game (Gallic War)* / **Play
  custom map** / *Load game* / **Map editor** / **Buildings editor** /
  **Unit editor** / **Trigger editor** / **RPG request editor** /
  **Mapping keys keyboards** / *Credits* / *Exit*.
- **Gameplay:** left-click a palette button, then left-click on the
  map to place; right-click to demolish (50 % refund). The bottom
  bar tabs you through the building categories. The right panel
  shows resources, happiness, treasury, nutrient diversity, and
  trade-route status.
- **Hotkeys:** see the in-game help (`H`).
- **Quick-save / load:** `F5` / `F9`.
- **Tick recorder:** `Ctrl+R` — toggles two JSONL logs in
  `./data/recordings/`: per-tick world state (diagnostics) **and**
  per-action player decisions. Use to debug starvation or staffing
  issues, or as imitation-learning data; analyse with
  `pandas.read_json(path, lines=True)`. (Plain `R` opens commercial
  roads.)
- **Bartering:** `B` — international resource exchange. Prices in
  `bartering.py`; 100 gold flat fee per trade.

## What's new in v0.52

Inter-city commerce becomes *visible and honest*:

- **Income only per trip.** Commercial-road routes still move stock out
  of the city each tick, but no longer drip gold — all inter-city
  income now comes from completed **voyages** (round trips).
- **Visible voyages.** A launched voyage spawns a real `TradeShip` that
  sails from a commercial harbour out to the map edge and back, so
  foreign trade is something you watch on the water, not a hidden
  ledger entry.
- **Commerce-ships panel — press `!`.** A read-only summary of *busy*
  ships at sea (cargo, destination, payout, ETA) versus *free* berths
  ready to dispatch, with a lifetime trips/gold footnote.
- **"Mapping keys keyboards" splash button.** A pre-game overlay
  listing every key binding *and* the absolute on-disk folders the game
  saves to / loads from (saved games, custom maps, data/config root).

See `CHANGELOG_v0.52.md` and **Chapter 13** of `DEV_AUDIT.md` (the
latter also answers two design questions: why mixins over the
alternatives, and whether to batch SpriteLists / use numpy arrays).

## What's new in v0.31

Narrative-authoring tooling: **RPG request editor** on the splash
menu, the first piece of an upcoming storytelling system that lets
NPCs interrupt gameplay with a dialogue + multi-choice decision panel
(Caesar demanding a tribute, the high priest asking for an Oracle,
etc.).

The editor edits `data/rpg_requests.json` with the same `.tmp →
rename` atomic write pattern as the buildings + trigger editors.
Each request defines an NPC name, portrait + scene asset keys, the
request text shown to the player, a resources preview, and a list
of decisions — each with a label, a delay (so "Yes for next month"
deducts the cost later, not now), an effects bundle, an optional
flag the simulation can key on, an optional event to fire, and a
happiness delta.

The runtime dispatcher that actually displays these in-game (full-
screen scene + portrait + decision buttons, matching the brief's
reference screenshots) is queued for v0.32, along with a
`fire_request` effect in the trigger editor so a scheduled event
can spawn an RPG request. See `CHANGELOG_v0.31.md`.

## What's new in v0.29

Combat-system review. Five player-flagged items closed in one drop:

- **Cavalry shock** — a cavalry soldier's first strike on a given
  enemy deals 1.75× damage (modelling a charge at speed). Subsequent
  strikes on the same target hit at base damage. Disengaging and
  re-engaging the same target doesn't refresh the bonus; a new
  target is a new charge.
- **Bowman buff + fire arrows** — bowman damage 8 → 11. Ranged
  soldiers (bowmen, ballistae) can now damage adjacent enemy-occupied
  buildings via the new fire-arrow pass (0.5× base damage to the
  structure, sets it on fire).
- **Persistent fire** — once a building takes combat damage, an
  `on_fire` flag is set and the fire-propagation pass damages it
  every tick *regardless of enemy presence*. An ignored fire takes
  ~30 ticks to destroy an undefended house.
- **Fireman walker** — new civic walker spawned from prefectures.
  Patrols within Chebyshev 4 of the home prefecture; extinguishes
  burning buildings at Chebyshev 1 (4 burn-points per tick). Engineer
  posts no longer repair on-fire buildings — the fireman must arrive
  first.
- **Editor map ceiling 128 → 64** — playtester feedback flagged
  pathfinding stutter on integrated GPUs at the v0.27 128×128 cap.
  The supporting refactor (services / walkers / pathfinding off
  module constants) stays in place; only the editor's enforcement
  constant moved.

See `CHANGELOG_v0.29.md` for the full release notes and the
queued-for-v0.30 list (experience, enemy diversification, tower
rebalance, fire spread).

## What's new in v0.28

Trigger editor on the splash menu (edits `data/events.json` in place
with the same `.tmp → rename` pattern as the buildings editor) and a
scheduled-events panel in the map editor toolbar that wires events
into specific scenarios via the six v0.27 trigger kinds (`at_year`,
`at_month`, `at_tick`, `random_after_tick`, `on_population_above`,
`on_treasury_below`). Blizzard and Flood event definitions ship in
`data/events.json`. See `CHANGELOG_v0.28.md`.

## What's new in v0.27

Infrastructure expansion: road brushes (stamp 5 or 10 roads in one
click, horizontal or vertical), full bridge implementation (wooden
and stone, 5-tile and 10-tile, both orientations — walker-passable
and road-network-connected once built), 128×128 map ceiling (lowered
to 64×64 in v0.29 — see above), and a polished buildings-editor
scalar row (uniform ±10 / ±50 with the v0.26 layout-overlap bug fixed).

See `CHANGELOG_v0.27.md` for the full release notes and the
deferred-to-v0.28 list (Trigger editor + per-map scheduled events).

## What's new in v0.23.x

Four playtester-flagged gaps closed in one drop. Three visibility wins
plus a substantial military addition.

### Natural-resource textures actually render

`game_map._draw_terrain` now draws the PNGs shipped under
`assets/textures/natural/` (forest, fertile soil, groundwater, iron /
gold / copper / stone veins) for every feature-bearing tile. The
legacy glyph rendering (triangle for forest, dots for fertile soil,
circle for ore) remains the fallback when no PNG is on disk.

### Five JSON-driven military units

`data/units.json` declares five canonical units, loaded via the new
`units.py` registry:

- **Scout Squadron** — garrisoned by the watchtower; fast, wide patrol.
- **Light Infantry** — the legacy v0.22 soldier; barracks default.
- **Heavy Infantry Legionary** — armoured (takes ½ damage); fort default.
- **Heavy Cavalry** — armoured + fast; new `fort_cavalry` building.
- **Bowman** — ranged (engages at Chebyshev 2); new `archery_range`.

Each unit has per-type hp / damage / speed / sight / patrol / sprite
fields. Buildings declare which unit they spawn via the new
`spawn_unit` field in `data/buildings.json`.

### Per-building cumulative produced / consumed totals

The info panel now shows `Lifetime made: 540 wheat, 12 food` and
`Lifetime used: 28 money` alongside the per-tick rates. Totals
accumulate per building, drop on demolish, and round-trip through
saveload.

### Middle-click on raw natural-resource tiles

Middle-click a forest or iron vein with no building yet → bubble shows
the same "200 of 1000 left" readout used for built extractors. Useful
for scouting a site before committing.

See `CHANGELOG_v0.23.x.md` for the full release notes and
`ROADMAP.md` for the v0.24 plan (construction time + full Unit editor).

## What's new in v0.19

Diagnostics, bartering, menu parity. Highlights:

- **Tick recorder** — press `Ctrl+R` to start two JSONL logs: a
  diagnostic world-state log (every building, resource, walker, every
  tick) and a player-action log (placements, demolitions, tax changes,
  barters). They join on `tick`.
- **Bartering menu** — press `B` to swap resources at fixed prices
  defined in `bartering.py`. 100 gold fee per trade.
- **Play custom map** — splash button → file picker → load any
  map from `./data/maps/`.
- **Splash ↔ ESC menu parity** — Map editor, Buildings editor, and
  Unit editor entries on both menus.
- **Expanded Buildings editor** — sprite filename, full
  production/consumption summary, primary-output and
  primary-input qty editing.
- **Resource sprite generator** — `python tools/generate_resource_textures.py`
  emits 25 placeholder icons.

See `CHANGELOG_v0.19.md` for the full release notes.

## What's new in v0.17

A UX-focused polish pass on top of the v0.16 nutrient/map work:

### HUD: per-nutrient satiety

The single `Food: N` line is replaced by a `── Satiety ──` block
listing all 10 nutrients with per-nutrient percentage of population
demand covered, colour-toned (green/warn/red).

### Currency: gold

`gold` everywhere instead of the abbreviation `dn`.

### Job stats panel — `J`

New modal: pool / demand / filled / jobless / utilisation, per-role
breakdown (worker / trader / soldier / citizen), and a
short-staffed-first list of every worker-using building.

### Jobless heatmap — `O` cycle

11th overlay entry. Red/amber shading on understaffed building
footprints. Read from the same data as the J panel.

### Hover tooltip while paused

The tooltip is re-drawn over the pause dim, so spacebar-pause-and-
look-around now works.

### Texture manifest

`./assets/textures/` folder structure with `data/textures.json`
mapping building ids *and* names to filenames. Both keys work
simultaneously. Supports `.png .jpg .jpeg .webp`.

### Load game button fix

The in-game ESC menu's *Load game* button now visibly notifies on
success (with year/pop summary) and on failure (showing the
expected save path).

See **CHANGELOG_v0.17.md** for the full release notes.

## What's new in v0.16

Two big additions on top of v0.15:

### Nutrients

Population food is now ten distinct goods (**bread, vegetables,
fruits, meat, fish, cheese, olive oil, honey, spice, wine**). The
HUD's *Nutrients: N/10* line tracks how many you have stocked; with
all ten you get a happiness bonus and a 1.5× population growth
multiplier.

Eleven new buildings ship to produce the missing nutrients (vegetable
grower, orchard, pasturage, slaughterhouse, butcher, fishery,
fishmonger, cheese shop, spice field, beekeeper) plus a stable for
horses.

Granaries default-store nutrients only. Warehouses default-store
non-nutrient raw goods only. Both can be overridden per-instance via
the inspector toggle pattern from v0.11.

### Map editor

Click *Map editor* from the splash to enter a free-placement scenario
authoring mode. Save your layout with the *Save map* button — files
go to `./data/maps/<slug>.json`. Load with *Load map*.

A sample map ships at `data/maps/sample_riverside_hub.json`.

See **CHANGELOG_v0.16.md** for v0.16 release notes and
**ROADMAP.md** for what's next.

## Layout

```
.
├── main.py                  # entry point
├── game_window.py           # the arcade.Window class — UI + state machine
├── game_map.py              # 2D grid: terrain, features, buildings
├── economy.py               # resources, taxes, production, population
├── balance.py               # all gameplay tuning numbers (frozen dataclass)
├── building.py              # registry loaded from data/buildings.json
├── building_status.py       # per-tick activity classification
├── walkers.py               # market traders, delivery walkers, soldiers
├── pathfinding.py           # walker routing
├── road_network.py          # road connectivity for production gating
├── services.py              # water/food/entertainment/etc. coverage radii
├── house_evolution.py       # tier-up rules + supply memory
├── storage.py               # per-warehouse stock allocation
├── decay.py                 # building condition + maintenance
├── events.py                # scripted economic events
├── caesar.py                # Caesar request manager
├── diplomacy.py             # diplomatic-points tracker
├── rebellion.py             # starvation pressure → rebel walkers
├── trade.py                 # trade-route manager (per-tick routes)
├── commercial_roads.py      # v0.50: foreign-city links + routes (v0.52: stock-only, gold by trip)
├── voyages.py               # v0.51: per-trip inter-city ship commerce
├── stats.py                 # statistics panel
├── jobs.py                  # employment statistics + jobless heatmap data
├── textures.py              # manifest-aware texture loader
├── signals.py               # tiny pub-sub bus
├── credits.py               # splash credits text
├── saveload.py              # JSON save/load + version migration
├── mapfile.py               # JSON map editor save/load
├── triggers.py              # v0.36: TriggerManager — map-level
│                              wiring (Event editor runtime)
├── constants.py             # paths, grid, colours, nutrients
│
├── data/
│   ├── buildings.json       # building definitions (modder-editable)
│   ├── events.json          # event definitions
│   ├── cutscenes.json       # cutscene library
│   ├── rpg_requests.json    # RPG request library
│   ├── textures.json        # texture manifest: id/name → filename
│   ├── maps/                # scenario maps written by the editor
│   │   └── sample_riverside_hub.json
│   └── scenarios/           # v0.36: full scripted scenarios bundling
│       │                      map + cutscenes + requests + events
│       └── tribute_crisis/  # exercises the v0.36 trigger pipeline
│
├── assets/
│   └── textures/            # drop your PNGs/JPEGs/WEBPs in here
│       ├── README.md
│       ├── buildings/       # one image per building
│       ├── terrain/         # grass, water, hills, mountains
│       ├── features/        # forest, ore vein, fertile soil, …
│       ├── walkers/         # worker, trader, citizen, soldier, …
│       ├── houses/          # tier0..tier4 (shack → villa)
│       ├── scene/           # cutscene backgrounds
│       ├── portraits/       # RPG-request character portraits
│       └── ui/              # splash background, panel art
│
├── tests/                   # pytest suite (946 passing; see notes)
└── tools/
    ├── balance_audit.py     # producer/consumer ratios, bottleneck analysis
    ├── generate_flows_pdf.py
    ├── generate_placeholder_textures.py
    └── generate_tribute_crisis_placeholders.py  # v0.36: per-scenario art
```

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

Should print `664 passed` plus 11 pre-existing failures documented in
`ROADMAP.md` (the `food → bread` legacy issues queued for v0.18.x).

## Contributing

The project is heavily test-driven (every new feature ships with
locking tests). When adding a building, edit `data/buildings.json` —
no engine code needed unless the building needs a new rule (e.g. a
new feature gate). When adding a balance knob, add a field to
`Balance` in `balance.py` rather than hard-coding.

`game_window.py` is large; please keep new UI features close to the
existing patterns (lazy text pools, hit-test rect lists rebuilt each
draw, single-state-machine app_state field).
