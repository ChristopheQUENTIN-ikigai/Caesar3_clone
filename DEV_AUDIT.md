# Caesar III Clone — Developer Documentation & Code Audit

*Target version: **v0.53** · Python 3.11+ / Arcade 3.x · ~52 source modules. The v0.38 baseline added the RPG-request runtime and the initial naval system; v0.40–v0.46 built the naval subsystem out fully — see **Chapter 11**. v0.50–v0.51 added inter-city commerce in two tiers (per-tick commercial-road routes, then per-trip voyages with free-ship/stock/war/piracy gating and a distance-scaled interval) and a `$` finance budget panel — see **Chapter 12**. v0.52 made inter-city commerce earn gold **only per trip** (no per-tick drip), mirrored each voyage with a **visible `TradeShip`**, added the `!` commerce-ships panel and a splash "Mapping keys keyboards" overlay, and answers two long-standing design questions (mixins vs alternatives; SpriteList batching & numpy) — see **Chapter 13**. **v0.53 retired the per-tick commercial-road route layer entirely** — inter-city goods now move *only per trip* via voyages; the route layer (which still moved stock for free every tick) is gone, `CommercialRoadManager` is now link-bookkeeping only, and the window is reworked to per-trip — see **Chapter 14**. Test bar green (1264 passed, 0 failed) — see Chapters 10, 12, 13 & 14.*

This document does four jobs:

1. **Chapter 1** summarises the economy system quickly (the original request's headline ask).
2. **Chapters 2–4** document the project structure: important classes, the key values they carry, and the central loops / conditional gates that drive a tick.
3. **Chapters 5–6** are deep dives into the two subsystems most worth understanding after the economy: the combat/walker engine and the editor/authoring suite.
4. **Chapters 7–9** are the audit proper — bugs, missing features, optimisation room, and an opinion on where the effort is best spent.

A reader who only wants the economy can stop after Chapter 1. A reader who wants to *change* the economy safely should also read Chapter 3 (the tick) and the "Bugs" chapter. The two highest-impact findings of the second pass live in Chapter 7: the RPG-request feature that's authored but never displayed (7.1), and the unit-editor fields the engine ignores (7.2).

---

## Chapter 1 — The economy system at a glance

The economy is a **batch-update tick simulation** with no real-time interpolation: every ~0.5 s the game calls `EconomyManager.update(building_ids)` once, and that single call advances the entire city by one step. Everything else (rendering, walkers, decay) reads the result.

### The production chain

The city runs a multi-stage supply chain. Raw extractors feed refiners, which feed the staple/luxury producers, which feed houses and the treasury. The headline food path is:

```
farm → wheat(12)   windmill: wheat(4) → flour(7)   bakery: flour(2)+wood(1) → bread(10)
                                                          │
                                              bread is the staple citizens eat
```

Parallel chains exist for construction (`quarry → stone → stonemason → stone_blocks`; `lumber_mill → wood → sawmill → planks`), metal (`mine → iron_ore → smelter → iron → weapon_smith/factory`), three luxury food chains (oil, wine, cheese/meat), and pure-money buildings (market, senate, port, forum, the three-tier bank chain `local→business→private` at 18/35/55 money). 64 of the 86 buildings produce or consume something.

### What one tick computes, in order

`EconomyManager.update()` runs this pipeline (see `economy.py:276`):

1. **`_calc_production`** — two passes over buildings.
   - *Pass 1* sums housing capacity, storage capacity, and total worker demand, and computes the road-connectivity gate.
   - *Pass 2* classifies each building (`building_status.classify`) and accumulates **throttled** production/consumption. Throttle = `min(worker_ratio, input_ratio)`; a building with no inputs or no workers contributes nothing.
2. **Apply production** — added to `resources`, clamped against storage headroom.
3. **Apply consumption** — drained from `resources`, scaled the same way so an unstaffed factory burns no inputs.
4. **Feed the population** — citizens eat from a *priority pool*: `bread → other nutrients → legacy food`. `fed_fraction` = fraction of citizens who ate.
5. **Taxes** — `population × fed_fraction × tax_rate × per_capita × tier_multiplier`. Starving cities collect less tax (clamped to a 0.2 floor).
6. **Wages** — each employed worker draws `wage_per_worker`; if the treasury can't cover the bill, it's paid pro-rata and *next* tick's workforce shrinks (`wage_payment_ratio`).
7. **Nutrient diversity** — count distinct nutrients in stock (0–10); drives a happiness bonus and a growth multiplier.
8. **Happiness target** — an additive equation (base − tax penalty + food/housing/employment/water/diversity terms + building happiness), then smoothed toward the current value.
9. **Population growth/shrink** — every 5 ticks, gated on happiness + food + housing headroom.

### The key levers (all in `balance.py`, one frozen dataclass)

| Concept | Default | Note |
|---|---|---|
| `start_treasury` / `start_population` | 1000 / 100 | runway is ~100 ticks at lowest tax |
| `tax_revenue_per_capita` | 5.0 | × rate × tier mult |
| `wage_per_worker` | 0.2 | the deficit feedback loop |
| `happiness_base` / `_smoothing` | 50.0 / 0.1 | exponential approach |
| `farm_water_min_factor` | 0.4 | dry farm still yields 40 % |
| `nutrient_diversity_*` | 10 / +15 happiness / 1.5× growth | end-game "feed well" reward |
| `pop_growth_rate` / `_shrink_rate` | 0.03 / 0.02 | checked every 5 ticks |

**The mental model that matters:** the economy is *deterministic and pure* — no arcade imports, no randomness, no wall-clock. Same inputs → same outputs. That is the project's single biggest quality asset: the whole simulation is unit-testable headless, and ~970 tests exercise it.

---

## Chapter 2 — Project structure & important classes

The codebase splits cleanly into **pure-simulation** modules (no rendering, fully testable) and the **UI layer** (`game_window.py` + `mixins/` + the editors).

### Pure simulation (the trustworthy core)

| Module | Class(es) | Owns |
|---|---|---|
| `economy.py` | `EconomyManager` | resources, treasury, tax, production, population, happiness — the heart |
| `building.py` | `Building`, `BuildingRegistry` | static building defs loaded from `data/buildings.json` |
| `building_status.py` | `BuildingStatus` + `classify()` | per-building active/partial/starved/unstaffed/idle/disconnected state |
| `storage.py` | `Storage` | per-warehouse allocation view over the global pool |
| `house_evolution.py` | `HouseEvolution` | tier up/down from service coverage + goods deliveries |
| `services.py` | `ServiceMap` | water/food/religion/entertainment coverage rasterisation |
| `road_network.py` | `RoadNetwork` | flood-fill connectivity for the production gate |
| `jobs.py` | `JobsSnapshot` | read-only labour view for the 'J' panel |
| `decay.py` | `DecayManager` | building condition wear-down |
| `caesar.py` | `CaesarRequestManager` | tribute demands + deadlines |
| `rebellion.py` | `RebellionTracker` | starvation-pressure rebel spawns |
| `diplomacy.py`, `trade.py`, `bartering.py`, `gold_trade.py` | mixed | four of the six commerce surfaces |
| `commercial_roads.py` (v0.50, reworked v0.53) | `CommercialRoadManager`, city-link helpers | links to foreign cities (one-off gold cost). **v0.53:** link-bookkeeping only — the per-tick auto-trade route layer is retired; the `add_route`/`remove_route` methods are deprecated no-op stubs (**Chapter 14**) |
| `voyages.py` (v0.51) | `VoyageManager`, `ActiveVoyage`, `CityVoyageConfig` | *per-trip* inter-city commerce — free ships carry ≤1000 units, gold lands on the return, gated on ship/stock/war/piracy, interval scales with distance (**Chapter 12**) |
| `walkers.py` | `Walker`/`DeliveryWalker`/`Soldier`/`Enemy`/`Fireman`/`WalkerManager`/`Projectile` | mobile agents + the entire combat resolver (**Chapter 5**) |
| `units.py` | `UnitDef`/`UnitRegistry` | data-driven military unit stats (JSON + built-in fallback) |
| `triggers.py`, `cutscene_player.py`, `rpg_requests.py`, `events.py` | `TriggerManager`, `CutscenePlayerState`, loaders | the runtime that consumes authored content (**Chapter 6**) |
| `game_map.py` | `GameMap` | the tile grid, building placement, `building_state` |
| `balance.py` | `Balance` (frozen) + `COMBAT_*` constants | every tuning number |
| `constants.py` | — | `NUTRIENTS`, `TAX_RATES`, `EDITOR_GRID_MAX`, labels, paths |

### UI layer

`CaesarGameWindow` (in `game_window.py`, ~6,150 lines) is the orchestrator. It inherits from eight stateless mixins (`mixins/`) — `ScenarioLayoutsMixin`, `BuildingsEditorMixin`, `UnitEditorMixin`, `MapEditorMixin`, `ScheduledEventsPanelMixin`, `TradePanelsMixin`, `DiagnosticPanelsMixin`, `SplashMenuMixin` — plus `arcade.Window` (that order is the actual MRO). The mixins hold *no state*; they reach into `self.<attr>` on the concrete window. This was an explicit v0.37 refactor (`REFACTORING.md`) that took the file from 11,470 → 6,153 lines with zero behaviour change. The three large standalone editors (`triggers_editor.py`, `rpg_request_editor.py`, `cutscene_editor.py`) live outside the mixin set; the editor suite as a whole is analysed in **Chapter 6**.

### Data flow (who owns what)

```
data/buildings.json ─► BuildingRegistry ─┐
                                          ├─► EconomyManager.update() ─► resources/treasury/pop/happiness
GameMap.building_state (tiers, accepts) ─┤              │
RoadNetwork.is_connected ────────────────┘              ▼
                                              Storage.distribute()  (read-only view)
                                              HouseEvolution.check_all()
                                              Walkers / Decay / Caesar / Rebellion
                                                        │
                                              game_window HUD reads it all
```

The crucial design rule, stated in several module docstrings: **the economy keeps one global `resources` dict**. Storage, warehouses, and the HUD are *views* over that dict, not separate ledgers. This avoids a combinatorial test-rewrite but means "where physically is this good" is an estimate, not ground truth (an intentional, documented trade-off).

---

## Chapter 3 — The tick: loops, conditionals & gates

The single most important control structure is **the two-pass loop in `_calc_production`** (`economy.py:563`). Understanding its conditionals is the key to understanding the whole sim.

### The gate cascade (per building, evaluated in this order)

```
disconnected?  (requires_road AND not connectivity())     → throttle 0, no workers drawn
   else: classify():
     no production AND no consumption AND no workers?        → IDLE   (roads, wells)
     workers needed but 0 filled?                            → UNSTAFFED (throttle 0)
     any consumed input at 0 supply?                         → STARVED  (throttle 0, binary)
     money cost > treasury?                                  → STARVED (money)
     worker_ratio or input_ratio < 1.0?                      → PARTIAL (throttle in (0,1))
     else                                                    → ACTIVE  (throttle 1.0)
```

Then output for an active/partial building is multiplied by a stack of factors:

```
output = base × throttle × water_factor × feature_factor × tile_count_factor × depletion_factor
```

- **`water_factor`** — farms only; scales from `farm_water_min_factor` (0.4) to 1.0 with coverage.
- **`feature_factor`** — terrain bonus (farm on fertile soil = 1.5×); searches the footprint for the *largest* bonus.
- **`tile_count_factor`** — fisheries; *per fish tile* in Chebyshev-1 catchment (this is why `fishery` lists `fish:350` — it's per-tile, and 0 fish tiles → idle).
- **`depletion_factor`** — extractors on finite veins/forests; the `feature_tap` callback debits the tile and returns the fraction actually extracted.

### Other important loops

- **Eat-loop** (`update`): greedy drain of the priority pool `bread → NUTRIENTS → food`, breaking when `remaining_need <= 0`. Records `food_eaten_by_good` for the satiety HUD.
- **Worker distribution**: a single integer `workers_remaining` is walked down the building list in placement order — first-come-first-served, *not* category-prioritised (documented as a future tuning knob; see Bugs/Improvements).
- **`Storage.distribute`**: round-robin capacity-proportional fill with a second remainder-spreading pass, per good, per eligible warehouse.
- **`HouseEvolution._evaluate`**: devolution-check-first, then a *streak* requirement (2 consecutive passing checks) before promotion — deliberately to prevent flicker.

### Save/load conditional surface

`from_dict` defensively `.get()`s every post-v0.23 key so old saves load with sane defaults, and *recomputes* all derived fields on the next tick rather than trusting the save. This is good defensive practice. The v0.50 `commercial_roads` and v0.51 `voyages` blocks follow the same rule — a save missing either loads to an empty layer.

### Where commerce ticks (post-economy)

The inter-city commerce tick runs in `game_window.on_update` *after* `economy.update`: `trade_manager.update()` then `voyage_manager.update(world)`. **As of v0.53** the commercial-road *per-tick route* layer is retired, so `trade_manager.update()` no longer drives any inter-city routes — it only runs whatever generic ambient `TradeRoute`s exist (none, in normal play), and `voyage_manager.update(world)` (completes arrivals, then considers departures) is the sole inter-city export path. Both managers credit the treasury directly; the window then records their attribution into `economy.finance_breakdown` (`trade_routes`, `voyages`) for the `$` panel — a recording step, not a second credit. The `trade_routes` line is therefore ~0 in normal play. See **Chapter 12** and **Chapter 14**.

---

## Chapter 4 — Code-quality assessment

**Overall: this is a high-quality, unusually disciplined hobby/indie codebase.** The signals:

- **Pure-core / impure-shell separation** is real and respected. The sim has no arcade dependency, which is why it's so testable.
- **Documentation density is exceptional.** Nearly every non-obvious decision carries a `# vX.Y:` comment explaining *why*, often with the playtest observation that motivated it. This is the most valuable thing in the repo for a new maintainer.
- **Tuning is centralised** in one frozen dataclass. Modders change numbers without touching logic — and `frozen=True` prevents accidental runtime mutation.
- **Tests are extensive** (~990 tests, ~970 green) and assert on *data shapes and behaviour*, not pixels.
- **Clean compile, zero bare `except:`, zero stray `TODO/FIXME`** across the sim modules.
- **Serialization is version-tolerant** and recomputes derived state on load.

This is well above the median for a project of this genre. The criticisms below are refinements, not rescues.

---

## Chapter 5 — The combat & walker subsystem (deep dive)

Everything mobile on the map lives in `walkers.py` (~2,370 lines, the largest single sim module) plus the unit definitions in `units.py`. Like the economy, it's **pure logic with one thin arcade dependency** (drawing), and it's exercised by ~67 tests across `test_combat`, `test_walkers`, `test_military_chain`, `test_morale`, and `test_delivery_walkers` — all green.

### 5.1 — Class hierarchy

```
Walker (random-walk citizen; atmosphere only)
├─ DeliveryWalker     — A*-pathed, carries one good producer→consumer; feeds HouseEvolution
├─ CombatWalker       — adds HP / damage / take_damage / HP-bar draw
│   ├─ Soldier        — friendly; morale, retreat, armed flag, per-unit stats, cavalry-shock ledger
│   └─ Enemy          — hostile raider; walks toward an objective tile; faction-tagged
└─ Fireman            — civic; patrols a prefecture radius, extinguishes adjacent fires
Projectile            — visual-only arrow/bolt, no collision, ages out
WalkerManager         — owns the population, drives every tick, resolves all combat
```

The `Walker`/`DeliveryWalker` split is deliberate (documented): the random and A* paths "share almost nothing," so a mode-flagged single class would be a sea of `if self.is_delivery`. That call is correct — the subclasses stay readable.

### 5.2 — `WalkerManager.update` — the per-tick pipeline

The manager runs a fixed 7-step sequence each tick (`walkers.py:1118`):

1. **Citizen spawn/cull** — target count = `min(max_walkers, pop // POP_PER_WALKER)`, gated by a spawn timer.
2. **Delivery dispatch** — one delivery walker per interval, capped, requires a pathfinder.
3. **Soldier dispatch** — garrisons top up to per-building caps; *consumes one `weapons` from the economy per spawn*, spawning `armed=False` when the stockpile is empty (the broken-supply-chain made visible).
4. **Raid roll** — barbarians enter from a map edge when the city is big enough and below the concurrent cap.
5. **Fireman dispatch** — one per prefecture up to a cap.
6. **Movement** — soldiers use the enemy-aware `pick_target_with_world`; everyone else uses their own `pick_target`. Delivery walkers also deposit onto the goods-flow heatmap, which then decays + prunes.
7. **Combat resolution** → fireman sweep → reap dead → tick projectiles → trim excess citizens (combat/delivery/fireman walkers are spared the cull because "they have a job").

The ordering is intentional and documented: combat runs *after* movement (so a corpse doesn't also move), and the fireman sweep runs *after* combat (so a fireman next to a freshly-struck building starts containing it the same tick).

### 5.3 — `_resolve_combat` — the combat heart

This single method (~400 lines) does five passes in order:

1. **Soldier→enemy damage.** For each soldier, engage every enemy within range (`RANGED_ATTACK_RANGE=2` for ranged units, `1` for melee). Outgoing damage stacks four multipliers:
   `damage × (1 + DEFENCE_BONUS × defence_coverage) × armed_mult × retreat_mult × shock_mult`.
   - **armed_mult** = 0.0 if unarmed (useless in melee, by design).
   - **retreat_mult** = 0.5 if the soldier is routing (low morale *or* low HP) — a fighting retreat.
   - **shock_mult** = 1.75 for a cavalry unit's *first* strike on a given enemy (tracked per-soldier in a `_struck_targets` set of `id()`s). A genuinely thoughtful mechanic: the token burns on any non-zero swing, re-engaging doesn't refresh it, and a separate `("shock", …)` event carries the *bonus portion* so the HUD can say "Cavalry charge!".
2. **Enemy retaliation** — only if the enemy is in melee range (`d ≤ 1`); this is exactly the bowman's stand-off advantage (shoot at 2, take nothing).
3. **Ally morale on death** — when a soldier dies, friendlies within Chebyshev 2 lose morale ("watching your buddy fall").
4. **Enemy → citizen kills (probabilistic) + building burn.** An adjacent civilian dies at `KILLS_CITIZEN_CHANCE=0.5`; the building under the enemy's feet loses HP, gains a `burn` level, and gets the `on_fire` flag set. HP is lazily allocated into `building_state` so untouched buildings carry no extra bytes.
5. **Ranged-vs-building (fire arrows)** + **fire propagation.** Ranged soldiers can ignite enemy-held buildings; every on-fire building then takes a passive `FIRE_PROPAGATION_DAMAGE=1`/tick *regardless of enemy presence* — the persistent crisis the Fireman exists to answer. The propagation pass iterates `building_state` directly (O(burning), not O(map)) and snapshots keys first because `remove_building` mutates the dict mid-iteration.

### 5.4 — Data-driven units (`units.py`)

`UnitDef` (frozen dataclass) is loaded from `data/units.json` with a hard-coded built-in fallback so the game still launches if the file is missing or corrupt — the same defensive pattern as `BuildingRegistry`. Garrison buildings declare a `spawn_unit` id; the manager looks it up and instantiates a `Soldier` with the unit's stats. Faction (`roman`/`barbarian`) is read off the unit, replacing the old `isinstance(w, Enemy)` checks, which leaves the door open for a "barbarian archer" riding the same ranged pass with no subclassing. The seven shipped units (scout, light/heavy infantry, heavy cavalry, bowman, ballista, barbarian infantry) cover the genre's archetypes.

### 5.5 — Quality verdict on combat

Strong. The combat model is more sophisticated than the genre usually attempts (morale, retreat, charge bonus, persistent fire, ranged stand-off) and every mechanic carries a `# vX.Y:` rationale tied to a player request. The back-compat discipline is notable: every new `Soldier`/`Enemy` parameter defaults to the legacy value so old saves and direct-construction tests keep working. The main *correctness* caveats are the simplifications the code openly admits (sight is raw Chebyshev with no LOS; enemies have no "engaged" state; multi-tile buildings use the anchor tile for range, not the closest footprint tile) plus the dead-field issue in 6.x below.

---

## Chapter 6 — The editor & authoring subsystem (deep dive)

The project ships an unusually ambitious suite of in-game authoring tools — a map editor, a buildings-definition editor, a unit editor, a trigger editor, a scheduled-events panel, a cutscene editor + runtime player, and an RPG-request editor. The UI lives in `mixins/` (extracted from `game_window.py` in v0.37) and three large standalone editors (`triggers_editor.py` 1,544 / `rpg_request_editor.py` 1,409 / `cutscene_editor.py` 869). The data they author is consumed by matching runtime modules (`triggers.py`, `cutscene_player.py`, `rpg_requests.py`, `events.py`).

### 6.1 — The shared editor pattern

Every definition editor follows the same shape, and it's a good one:

- **Scrollable id list (left) + editable form (right)**, with hit-rects rebuilt each frame into a `_*_btn_rects` list and consumed by a `_*_handle_click` on the next click. Stateless render → click-dispatch is a clean, testable separation.
- **Atomic save** — write to `<path>.tmp`, then `os.replace(tmp, path)`. This is the right way to avoid bricking a data file on a partial write, and it's used consistently across the buildings, unit, trigger, RPG, and map editors.
- **Hot-reload** — after save, the editor rebuilds the live registry (`UnitRegistry.from_json_file`, etc.) and pushes it into the running systems (e.g. `walker_manager.unit_registry`) so the next spawn picks up the change without a restart. Nice.
- **DATA_DIR indirection** — after the mixin split, save methods resolve paths through the shared `DATA_DIR` constant rather than `Path(__file__)`, which would otherwise point at the mixin's location (a subtle bug the refactor correctly anticipated, documented in `REFACTORING.md`).

### 6.2 — The trigger runtime (`triggers.py`) is the strongest piece

`TriggerManager` is the model citizen of the codebase's defensive style. `_when_matches` dispatches on a `kind` string across ~10 condition types (`at_tick`, `on_population_above`, `on_treasury_below`, `random_after_tick`, `on_flag`, `on_event_fired`, the v0.37 `in_season_random` and `on_building_ratio_below`, …). Every branch coerces its inputs with `try/except`, an unknown `kind` is logged-once and skipped (never raised — "a bad trigger shouldn't tank an entire scenario"), and the seasonal check soft-imports `SEASON_BY_MONTH` with a hard-coded fallback table so `triggers.py` stays headless-importable for tests. Effect dispatch (`_dispatch` → `_do_fire_cutscene` / `_do_fire_request` / `_do_fire_event` / `_do_set_flag` / `_do_schedule_event`) each null-checks its wired callback and logs a clear "requested but no callback wired" warning rather than crashing. This is exactly how author-facing tooling should fail.

### 6.3 — The cutscene path is fully wired end-to-end

`cutscene_player.py` is a complete state machine: `start` / `advance` / `current_slide` / `is_last_slide` / `draw` / `handle_click` / `close`, driven from `game_window` (`fire_cutscene_for_flag` at ~3766, drawn at ~1487). A trigger can set a flag, the flag fires a cutscene, and the player sees a full-screen scene + portrait + advancing slides. This is the reference implementation for how the authoring→runtime loop is *supposed* to close.

### 6.4 — Quality verdict on editors

The editor *infrastructure* is excellent: consistent patterns, atomic writes, hot-reload, defensive runtime. The weakness is not in how the editors are built but in **two places where the authoring loop doesn't close** — the editor writes data that the runtime then ignores. Those are the headline new findings (6.x below). They matter more than any single bug because they represent *player-visible features that silently do nothing*, and because the test suite can't catch them: each layer is unit-tested in isolation, so the missing integration endpoint passes CI clean.

---

## Chapter 7 — Bugs & correctness issues

### 7.1 — The RPG-request feature is a dead-end: authored, queued, but never displayed (priority: **highest**, severity: feature-broken)

This is the most significant finding of the second pass. The RPG-request pipeline is built end-to-end *except the final consumer*:

- `rpg_request_editor.py` authors `data/rpg_requests.json` (NPC, portrait, scene, text, decisions with effects/delays/flags). ✔
- `rpg_requests.py` loads it. ✔
- A trigger's `fire_request` effect calls `TriggerManager._do_fire_request` → the wired `fire_request_for_id` callback. ✔
- `fire_request_for_id` (`game_window.py:3861`) looks the request up and **appends it to `self._pending_rpg_requests`**. ✔
- **Nothing ever reads `_pending_rpg_requests`.** Confirmed: it is written in exactly one place and read in zero (across all modules and mixins). The full-screen scene + portrait + decision-button panel — the entire *point* of the feature, and the thing the v0.31 README promised for v0.32 — was never built.

The consequence: an author can build a Caesar-tribute interaction, wire a trigger to fire it, and **nothing appears on screen**; the decision effects, the per-decision `delay_ticks`, the `set_flag` and `fire_event` hooks all never execute. The `fire_request_for_id` docstring still describes the consumer in the future tense ("when the RPG request dispatcher ships … it consumes `_pending_rpg_requests`").

Why CI is green anyway: `test_v032_features.py` exercises the *editor* and the *loader* (and asserts `DEFAULT_DECISION` shape, blank-decision creation, round-trip save/load), but never asserts that firing a request displays anything or applies a decision. Each layer is tested in isolation; the missing integration endpoint has no test because there's no code to test. **Recommendation:** either build the consumer (mirror the already-complete `cutscene_player.py` state machine — it's the obvious template), or, if it's deliberately deferred, gate `fire_request` behind a clear "not yet implemented" warning and document the feature as incomplete in the README so authors don't waste time building content that can't render.

### 7.2 — Unit editor exposes six fields the combat engine ignores (priority: **high**, severity: editable-but-inert)

`UnitDef` carries `defense`, `training_time`, `cost`, `upkeep`, `weapon_cost`, and `skills` (the v0.25 unit-editor additions), and the unit editor presents every one as a slider or toggle chip (`mixins/unit_editor.py:42-49,61`). But a full search of the engine shows **none of them is consumed**: `_dispatch_soldiers` doesn't deduct `cost`/`weapon_cost` or honour `training_time`, `_resolve_combat` never reads `defense` (armour reduction still keys off the old `armoured` bool via `ARMOURED_INCOMING_DAMAGE_MULT`), nothing applies per-tick `upkeep`, and no combat branch reads `skills` (`mood_booster`/`surgery` do nothing). A player who dials a unit's `cost` to 2000 and `training_time` to 500 sees zero change in game. This is the same failure class as 7.1 (authoring with no runtime), just at field granularity. **Recommendation:** wire the cheap ones first — `cost`/`weapon_cost` at spawn and `upkeep` per tick are a few lines each in `_dispatch_soldiers`/`update` and would make the editor honest — or, for anything genuinely deferred, hide the slider and note it in the editor as "planned." An inert slider trains the player to distrust the whole editor.

### 7.3 — Editor save reads its JSON with no error guard (severity: low)

`_unit_editor_save_to_disk` does `with path.open() as f: raw = json.load(f)` with no `try/except` (`mixins/unit_editor.py:480`). The *write* is atomic and the *registry reload* is defensive, but if `units.json` is hand-edited to invalid JSON, hitting Save raises and (depending on the click handler) can take down the editor or the window. The buildings/trigger editors share the read-then-merge approach; worth a uniform guard that surfaces a "couldn't parse units.json" notice instead of throwing.

### 7.4 — Stale documentation that now *understates* the project (severity: cosmetic, but misleading)

Two docstrings describe shipped features as future work: `rpg_requests.py` says the runtime dispatcher is "TBD … v0.32" (the trigger/queue half *did* ship — only the consumer is missing, see 7.1), and `mixins/map_editor.py` comments still say the editor ceiling is "v0.27: 128" though `EDITOR_GRID_MAX` is now 64 (matching the README's v0.29 change). These are harmless to the engine but actively mislead a maintainer reading the source to understand current behaviour.

### 7.5 — 16 failing tests are "known-broken" and stale (priority: **high**, severity: low-but-corrosive)

`REFACTORING.md` proudly notes "953 passed, 16 failed" and frames the 16 as pre-existing and unrelated. They are pre-existing — but **all 16 share one root cause and several are trivially fixable**, and leaving a permanent red baseline is a real process risk: it trains the team to ignore a failing suite, so a *new* regression hiding among the 16 would go unnoticed.

The clusters:

- **`test_water_and_starvation.py` (4), `test_road_network.py` (1), `test_stats.py` (1), `test_v013` fertile-soil (2)** — every one asserts the farm produces **`food`** directly (e.g. `food_balance == 8 - 100`, `production_per_tick["food"] == 16`). But since v0.34 the farm produces `wheat:12` only and `food_balance` is **bread-only**. *The economy code is correct; the tests encode a retired chain.* These should be rewritten to assert on `wheat` (and on a full farm→windmill→bakery chain for `food_balance`), not deleted.
- **`test_debug_logging.py` (8)** — the test builds a `SimpleNamespace` mock window that lacks the `trigger_editor_open` attribute the click handler now reads (`game_window.py:5072` raises `AttributeError`). Pure fixture rot; the fix is one line in the mock.

**Recommendation:** fix or formally `xfail`/`skip` these with a reason string, so the green bar is honest. A permanently-red suite is a latent bug-incubator.

### 7.6 — `int()` truncation silently destroys fractional output every tick (severity: low, but balance-relevant)

In `update()`, both production and consumption do `scaled = int(amount * eff)` (`economy.py:360,383`). When global efficiency `eff < 1.0`, a building producing 10 bread at 0.67 efficiency yields `int(6.7) = 6`, losing 0.7/tick. Across a large city with many partially-staffed buildings this is a systematic downward bias on output (and a matching under-draw on consumption, which partially compensates but not symmetrically). It also makes balance non-linear near efficiency boundaries. Consider banking fractional remainders, or rounding instead of truncating, or keeping `resources` as floats end-to-end (they already are typed `float`) and only formatting to int at the HUD.

### 7.7 — `reset_overflow()` is called *before* `distribute()`, contradicting its own docstring (severity: cosmetic)

`storage.py`'s `reset_overflow` docstring says it should run "after the HUD has had a chance to surface any overflow notification." In `game_window.py:1178-1179` it runs immediately before `distribute()` repopulates overflow. In practice the HUD reads the fresh value so it's harmless, but the ordering contradicts the documented contract and would surprise the next maintainer. Either fix the call order or update the docstring.

### 7.8 — Dead/confusing branch in `Storage.distribute` (severity: cosmetic / maintainability)

The `elif bt == "warehouse":` arm (`storage.py:134`) sets `accepts = None` and explains in a comment that it'll be handled later via the parallel `is_warehouse` flag. The branch is a no-op that exists only to host a comment; it iterates `get_building_positions()` *twice* (once to build `live`, once to build `is_warehouse`). Fold the warehouse flag into the first pass and drop the no-op branch — it'll read more clearly and halve that iteration.

### 7.9 — Signal subscription is never disconnected (severity: low, by design but worth a guard)

`EconomyManager.__init__` connects `_on_building_removed` to a process-global signal bus and deliberately never disconnects (comment says the manager is replaced, not mutated, on new-game). That's fine for the running game, but in the **test suite** many `EconomyManager` instances are created per process; each adds a live subscriber to the global bus, so a `building_removed` signal fired in one test can invoke handlers on stale managers from prior tests. Today nothing fires that signal in those tests, so it's latent — but it's a footgun. Consider a `weakref`-based connection or an explicit teardown in the fixture.

### 7.10 — Worker allocation is placement-order, not need-aware (severity: gameplay, not a crash)

Documented in three places as a known simplification: the global worker pool is handed out in building placement order. A player who happens to place a fort before a farm can literally starve their city by staffing the military first. This is arguably a *feature* (placement matters) but it's surprising and undiscoverable. At minimum it deserves a player-facing note; ideally a category priority (food → civic → military) as the docstrings already anticipate.

---

## Chapter 8 — Missing features & gaps

These are gaps relative to the genre and to the project's own roadmap, not defects:

- **No real warehouse ledger / trade-walker routing.** Storage is an allocation *view*, not a physical ledger; goods teleport into the global pool. The module docstring already scopes the real version as future work. This is the single biggest gap between "city-builder feel" and the current model.
- **Static barter/trade prices.** `bartering.STOCK_PRICES` and the trade routes use fixed prices; there's no supply/demand drift, so arbitrage is risk-free and the economy can't have market shocks. The docstrings flag stochastic drift as deferred.
- **Buy == sell price** in `gold_trade` (the only spread is the flat 100-fee). A real spread would make commerce a meaningful decision.
- **No price/affordability feedback into production.** Buildings never throttle on *demand*; a market with no buyers still mints money from bread. There's no concept of a saturated good.
- **Happiness model is global, not local.** Happiness is a single city-wide scalar. Caesar III's charm was per-house desirability; the current model can't express "the rich district is happy, the slums riot."
- **No difficulty/economy scenario knobs surfaced** beyond starting stock — `Balance` is one global instance; per-scenario balance overrides exist in tests but aren't a first-class scenario feature.

### Combat / walker gaps

- **The RPG-request runtime (the headline gap).** As detailed in 7.1, the entire authoring→trigger→queue chain exists but the on-screen decision panel does not. This is the largest single missing feature in the project — it's *most* of a narrative system with the last 10 % unbuilt.
- **Inert unit attributes** (7.2) — `defense`, `training_time`, `cost`, `upkeep`, `weapon_cost`, `skills` are a missing combat-economy layer: recruitment cost, upkeep drain, training delay, and leader/medic skills are all designed and editable but unimplemented.
- **No line-of-sight, no terrain combat modifiers.** Sight and engagement are raw Chebyshev distance; a soldier "sees" through walls and buildings, and there's no high-ground / chokepoint advantage. Acceptable for the current scope but a ceiling on tactical depth.
- **No enemy AI beyond "walk to objective."** Raiders head for a fixed tile and opportunistically burn/kill what they pass; they don't focus-fire, retreat, or target high-value buildings. The faction field is in place to support smarter behaviour later.
- **Soldiers don't pick up dropped weapons / can't be re-armed** once spawned unarmed (documented). A broken weapons chain permanently neuters that cohort until it dies and re-spawns.

### Editor / authoring gaps

- **No in-editor validation or "does this field do anything" signalling.** The unit editor will happily let you tune inert fields (7.2); the trigger editor's effect/modifier pickers are limited to a hard-coded key list (`TRIGGER_EDITOR_EFFECT_KEYS`) that doesn't include the full nutrient/luxury resource set, so an author can't grant `wine` or `bread` via a trigger effect from the UI even though the apply path would accept it.
- **No undo/redo in any editor**, and no schema/version stamp on the authored JSON — a future schema change has no migration hook.
- **RPG/cutscene content can't be previewed from its editor** — the author must wire a trigger and enter the game to see a cutscene, and (per 7.1) can't see an RPG request at all.

---

## Chapter 9 — Optimisation opinion (is there room?)

**Short answer: yes, but almost none of it is in the economy, and you probably shouldn't chase it yet.**

### Where the economy is fine

The economy tick is O(buildings) with small constants and runs twice a second. Even a 64×64 map with a few hundred buildings is trivial. Profiling effort here would be wasted — the pure-sim core is not your bottleneck. Don't micro-optimise `_calc_production`; its clarity is worth more than the microseconds.

### Where the real cost is

1. **`storage.distribute()` walks `get_building_positions()` twice every tick** and re-allocates *all* stocks from scratch each tick (full reset + round-robin). For a storage-heavy city this is the most wasteful per-tick loop in the sim. Easy win: single pass to build the warehouse list (fixes 7.8 too), and only re-distribute when stocks or warehouse layout actually changed (dirty flag).

2. **The walker/combat tick is the other O(n²) hotspot.** `_resolve_combat` is soldiers × enemies for the melee pass, plus enemies × citizens for the civilian-kill pass, plus ranged-soldiers × enemy-held-buildings — all naive nested loops. At typical raid sizes (a handful of each) this is nothing, but a large invasion wave on a dense city is the one place the sim could actually stutter. If it ever shows up in a profile, a coarse spatial bucket (grid cell → occupants) collapses every one of those passes to near-linear. Also: step 6 rebuilds the `enemies` list and re-`isinstance`-scans `self.walkers` several times per tick; one pass partitioning walkers into typed lists would remove the repeated scans.

3. **`game_window.py` rendering, not the sim.** `REFACTORING.md` itself identifies ~2,400 lines of `on_draw`/HUD as the next cluster. The fishery's nested 4-deep neighbour scan (`tile_count_factor`) and any per-tile overlay grids (`jobs.jobless_overlay_grid` allocates a full row×col matrix) are the kind of thing that bites on big maps — but again, only at draw time.

4. **The 288 KB monolith was the real debt, and it's already half-paid.** The mixin extraction is the right move. The honest optimisation opinion is **structural, not algorithmic**: keep extracting `game_window.py` (rendering and the `on_key_press` dispatch table are the obvious next two), because maintainability is the scarce resource here, not CPU.

### Concrete, ranked optimisation list

1. **(Cheapest, do first)** Single-pass + dirty-flag `Storage.distribute`. Removes the only genuinely redundant per-tick work.
2. Partition `walkers` into typed lists once per tick (soldiers / enemies / citizens / firemen / deliveries) and reuse them across the movement, combat, fireman, and trim passes — kills the repeated `isinstance` scans for free.
3. Cache the fishery catchment count per building (it only changes when terrain near it changes — i.e. never, mid-game).
4. Move `resources` to all-float arithmetic and format-to-int only at the HUD, killing the 7.6 truncation bias *and* a class of int/float papercuts at once.
5. Spatial bucketing in `_resolve_combat` — only worth it if large invasion waves ever profile hot; otherwise leave the readable nested loops.
6. Continue the `game_window.py` extraction (rendering mixin) — the maintainability ROI dwarfs any sim speedup.

---

## Chapter 10 — v0.38 changes: RPG runtime, naval system, trade spread & a green test bar

This chapter documents the work layered on top of the v0.37 baseline and — as specifically requested — explains exactly what the 16 standing test failures are and why they persist.

### 10.1 — What v0.38 added

Three things were built on top of the audited v0.37 tree:

1. **The RPG-request runtime (closes finding 7.1).** A new `rpg_player.py` presenter (sibling of `cutscene_player.py`) plus six wiring points in `game_window.py` (`__init__` state, `on_update` queue drain, `_game_tick` delayed-effect processing, `on_draw` panel, `on_mouse_press` and `on_key_press` dispatch) and an `apply_rpg_decision` method. A queued request now actually displays a scene + portrait + decision buttons, and a chosen decision's `effects`, `delay_ticks`, `set_flag`, `fire_event`, `happy`, and `diplomacy` all execute through the existing systems. Covered by `tests/test_rpg_player.py` (12 tests) — the integration endpoint test that was missing.

2. **The naval system.** New walker classes in `walkers.py` — `Ship` (base, moves on the inverse `_impassable_sea` domain) and `TradeShip` / `TransportShip` / `Warship` — plus a `WalkerManager` ship API (`spawn_trade_ship`, `spawn_transport_ship`, `spawn_warship`) and `_process_ships` for per-tick business logic (trade-route income + reversal, troop disembark onto the shore). Three `naval`-category unit defs were added to `units.json`. A scenario's `naval` block is spawned at load time by `mapfile._spawn_scenario_navy`, so the roster is live, not inert data. Covered by `tests/test_naval.py` (10 tests).

3. **Save/load persistence for the RPG delayed-effect queue** (see 10.3).

Full suite after these changes: **1018 passed, 0 failed** — +48 tests over the v0.37 baseline of 970, and the 16 standing failures are now all fixed (10.2). The build also added two smaller items:

- **Buy/sell trade spread.** `bartering.TRADE_SPREAD` (default 0.15) makes `gold_trade` buy *above* and sell *below* the listed `STOCK_PRICES` price, so flipping a good loses ~2×spread plus the flat fee — commerce becomes a decision to cover a shortage, not risk-free arbitrage. This is the economic depth behind the trade ships. Set the spread to 0.0 to restore the old symmetric behaviour. Covered by `tests/test_trade_spread.py` (4 tests).
- **Warships fight.** Warships were sailing but invisible to `_resolve_combat`; they're now in the friendly-combatant list (with an `is_retreating()` stub and shock-ledger compatibility), so they engage coastal raiders in the ranged pass. Land soldiers and warships never collide because enemies path on land and ships on water — they meet only at the coast, which is exactly the screening role we want.

### 10.2 — The 16 failing tests, explained

The 16 are not new and not caused by the v0.38 work. They are the identical set documented at the v0.37 baseline, and they fall into two clusters with a single root cause each.

Cluster A — **the stale farm-output tests (8 tests).** `test_water_and_starvation.py` (4), `test_v013_features.py` fertile-soil (2), `test_stats.py::test_production_aggregates_correctly` (1), and `test_road_network.py::test_connecting_road_resumes_production` (1). Every one asserts that a farm produces the resource `food` directly — e.g. `food_balance == 8 - 100`, `production_per_tick["food"] == 16`, or a fertile-soil bonus expressed in `food`. But since v0.34 the farm produces `wheat`, and the food chain is `farm → wheat → windmill → flour → bakery → bread`; `food_balance` is now bread-only and the literal resource `food` is retired from production. **The economy code is correct; these tests encode the pre-v0.34 chain.** The fix is to rewrite them to assert on `wheat` (and, for `food_balance`, to stand up a full farm→windmill→bakery chain) — not to change any engine code.

Cluster B — **the debug-logging fixture rot (8 tests).** `test_debug_logging.py` builds a `SimpleNamespace` mock window as a duck-typed stand-in. Since the mock was written, the click handler it drives began reading a `trigger_editor_open` attribute the mock doesn't define, so the call raises `AttributeError` before it reaches the logging assertion. This is pure fixture rot: the production code is fine; the test double is missing one attribute. The fix is a one-line addition to the mock (or an `autospec`).

Why they persisted before v0.38: `REFACTORING.md` had frozen them as a "known broken, pre-existing" baseline. As the audit noted in 7.5, that is itself a process risk — a permanently-red bar trains the team to ignore the suite, so a *new* regression hiding among the 16 could go unnoticed. **v0.38 fixed all 16 and restored a fully green bar (1018 passed, 0 failed).** Cluster A was rewritten to assert on `wheat` and `gross_production` (and, where a test genuinely needed the connectivity/water-gating *behaviour*, that behaviour is now measured on wheat deltas rather than the dead `food_balance`); the `test_stats` aggregation test now asserts `"food" not in production_per_tick`. Cluster B was fixed by replacing the brittle `SimpleNamespace` mock with a defaulting namespace that returns falsy for any unset editor/modal flag and `[]` for `*_rects` lists — so the next time the click handler grows a new gate, the fixture won't rot again. A bonus: the `test_version_module_exposes_current_version` test, which hard-coded `== "v0.37"` and broke on the v0.38 bump, was made bump-robust (it now parses `vMAJOR.MINOR` and asserts `>= (0, 37)`).

### 10.3 — `_rpg_delayed_effects` persistence in `saveload.py` (now fixed)

The first cut of the RPG runtime left a gap: the delayed-effect queue (`game._rpg_delayed_effects`, which holds a decision's deferred `effects` for the "pay next month" case) lived only in memory. A save/load between choosing "pay next month" and the payment falling due would silently drop the obligation — the player would get the deferred benefit (or escape the deferred cost) for free. This is now closed:

- `saveload.CURRENT_VERSION` was bumped 13 → 14.
- `save_game` serialises `_rpg_delayed_effects` (tuples → JSON lists as `[fire_at, effects, pop, happy]`) and the not-yet-presented `_pending_rpg_requests`.
- `load_game` coerces them back to the `(int, dict, int, int)` tuple shape, with a length guard so a malformed entry is skipped rather than crashing the load.
- A v13 → v14 migration defaults both keys to empty for older saves, so a pre-feature save loads cleanly with no pending obligations.

Covered by `tests/test_rpg_persistence.py` (4 tests: migration, round-trip shape, and the malformed-entry guard).

### 10.4 — What still needs in-game verification

The pure logic of all three features is unit-tested headless. What the build environment *cannot* verify (no GL context) is the actual rendering and input: the RPG panel drawing/clicking, the ship sprites on water, and ship placement/selection UI. Before relying on any of this in a shipped scenario, load `data/scenarios/mare_nostrum/` in the running game and confirm: the tick-0/1 cutscenes fire, the transports sail to the west shore and disembark their legionaries, and a `fire_request` trigger presents a decision panel that resolves on click. The logic is proven; the pixels are not.

---

## Appendix — Quick reference for a new contributor

- **Want to change balance?** Edit `balance.py` only. Don't hard-code numbers in `economy.py`.
- **Want to add a building?** Edit `data/buildings.json`; the registry and economy pick it up. Add it to `Storage.STANDARD_GOODS` and `bartering.STOCK_PRICES` if it's a new good.
- **Want to understand a tick?** Read `economy.py:update` top to bottom, then `_calc_production`, then `building_status.classify`. That's the whole loop.
- **Tests** live in `tests/`, run headless: `python -m pytest tests/ -q`. Expect 16 known failures (Chapter 5.1) until those are fixed.
- **The version** is in `version.py` (`v0.37`); bump it on release.
- **Don't** add arcade imports to any pure-sim module — that boundary is the project's most valuable invariant.

---

## Chapter 11 — Naval system evolution (v0.40 → v0.46)

The v0.38 naval system shipped ships that *moved* but not much else. v0.40–v0.46 turned it into a working subsystem. This chapter is the current-state reference; the per-release detail lives in `CHANGELOG_v0.4x.md`.

### 11.1 — What each release added

- **v0.40 — harbor-transfer PoC.** Trade ships carry real cargo loaded from the goods ledger; export income is proportional to cargo (no stock → no phantom income). Also fixed the `mare_nostrum` intro cutscenes (they never fired — the runtime lookup ignored the scenario's embedded cutscene library, and the player read different slide keys than scenarios authored).
- **v0.41 — shipyard build queue.** Shipyards no longer auto-launch on a timer; the player queues hulls from the building info panel (`+ Build Cargo ship / Warship`, queue list with per-hull progress + remove). Queue persists in `building_state` (no save-schema bump). `_process_shipyards` drains the queue.
- **v0.42 — port/harbor role fix + upkeep.** Storage moved off harbors onto the **commercial port** (the data was backwards); harbors are shelter only (10 `ship_slots`, no storage). Military port stores no goods. New `upkeep` field on `BuildingDef`; the six naval buildings cost 1 gold/tick, deducted in `economy.update` and shown in the HUD Cost line. Ship build time cut to ~30s for dev feedback.
- **v0.43 — ship pathfinding (BFS).** `find_sea_path` replaced the greedy one-step movement that deadlocked on concave coastlines. Ships cache a path and fall back to greedy when a goal is unreachable.
- **v0.44 — A* + overlay + supply-chain routing.** Pathfinder upgraded BFS → A* (Manhattan heuristic, adaptive expansion cap) so large maps don't false-negative; a 'P'-key debug overlay draws ship routes. Port↔store routing: `storage.store_class_for_good` (nutrients → granary, else warehouse, shared with `distribute()`), `WalkerManager.nearest_store_for_good`, and a `cargo_store_class` recorded on loaded trade ships.
- **v0.45 — berths + naval combat.** `harbor_berth_occupancy` computes per-harbor berth use (idle, role-matched ships in the footprint-adjacent water zone, capped at `ship_slots`); the harbor info panel shows `Berths: N/10`. `EnemyShip` (a sea-sailing `Enemy` subclass) is intercepted by warships via the existing combat pass; `_resolve_sea_raids` lets raiders ram trade ships, making escort meaningful.
- **v0.46 — raid trigger, goods-flow walker, carriers, sprites, trireme cap.** `spawn_naval_raid` + the `force_naval_raid` trigger effect close the v0.45 gap (combat existed but nothing spawned enemy ships). `CargoCarrier` is the visible port→store goods-flow walker (`dispatch_cargo_carrier`). Every `Ship` gained a generic `manifest` cargo hold (load/unload/capacity) so ships are unified carriers. Placeholder ship sprites added under `assets/textures/walkers/` (`trade_ship`, `warship`, `transport_ship`, `barbarian_ship`, `cargo_carrier`). The trireme (`TransportShip`) now caps at **10 troop slots** (`TROOP_SLOTS`, `free_slots`, `embark`, `is_full`); `data/units.json` records `troop_slots: 10`.

### 11.2 — Updated audit status

- **7.1 (RPG request never displayed)** — addressed earlier (v0.38 runtime); the `fire_request` path and panel exist.
- **7.2 (inert unit-editor fields)** — STILL OPEN. Six `UnitDef` fields the engine never reads. Untouched by the naval work.
- **9.1 (`Storage.distribute` per-tick rebuild)** — STILL OPEN. The naval supply-chain routing reuses the existing classifier rather than the per-warehouse ledger, so it didn't change this.

### 11.3 — What still needs a GL context to verify

All naval logic is headless-tested, but the build environment has no GL context. Unverified on screen: shipyard queue panel rendering/clicks, the `Berths: N/10` line, the 'P' route overlay, ship/cargo-carrier sprites in motion, and enemy-ship sea battles. The placeholder sprites are deliberately crude — replace with real art.

### 11.4 — Known structural gaps / next candidates

- **Military-unit command interface.** There is no UI for selecting, grouping, or ordering units (land or naval) — no group/split/embark/disembark/join/navpoint/attack/defend/patrol/retreat/siege/rest/scout commands. Soldiers patrol from their garrison and auto-engage; ships follow scripted goals. A real RTS-style command layer is unscoped (see the design note tracked outside this doc).
- **Embark/disembark is automatic, not commanded.** Transports disembark on reaching a shore goal; there's no player-driven "load these soldiers here, sail there, land them" flow. The trireme's 10-slot `embark` method is the data-model groundwork for it.
- **Sprite art** remains placeholder for ships and the six naval buildings (the building textures referenced in `textures.json` don't exist on disk → coloured-rect fallback).

---

## Chapter 12 — Inter-city commerce & the finance panel (v0.50 → v0.51)

By v0.49 the game had four commerce surfaces (`diplomacy`, `bartering`, `gold_trade`, `trade`) but no *standing relationship* with foreign cities and no single place to read the budget. v0.50–v0.51 add both: a two-tier inter-city trade model and the `$` finance panel. Per-release detail lives in `CHANGELOG_v0.50.md` / `CHANGELOG_v0.51.md`; this chapter is the current-state reference.

### 12.1 — Two tiers of inter-city trade *(historical; tier 1 retired in v0.53)*

> **v0.53 update:** the *per-tick route* tier described next was **retired entirely**. Inter-city commerce is now per-trip only — only the voyage tier below survives. The text here is kept as the v0.50–v0.51 historical record; see **Chapter 14** for the current state.

At v0.50–v0.51 there were **two** ways to trade with a linked foreign city, sharing the link (you must open a commercial road first) but nothing else:

- **Per-tick routes (v0.50, `commercial_roads.py`).** A `CommercialRoadManager` links cities (`link_city`, one-off gold cost from `TRADE_CITIES[].link_cost`) and runs persistent auto-trade `TradeRoute`s through the shared `TradeRouteManager`. Each route sells a fixed quantity of a good *every tick* for gold. Steady, abstract, no ships involved. UI: the **Commercial roads** window (`R`). *(v0.52 removed the gold; v0.53 removed the layer.)*
- **Per-trip voyages (v0.51, `voyages.py`).** The model the brief asked for: inter-city goods trading requires **free commercial ships** to carry the goods, and the goods↔gold exchange happens **per trip, not per tick**. This is the focus below — and, as of v0.53, the only inter-city export path.

At v0.50–v0.51 the two coexisted on the same city; a player typically picked one. They were independent in save data (`commercial_roads` vs `voyages` blocks) and in the tick. v0.53 removed the per-tick half.

### 12.2 — The voyage model (`voyages.py`, pure logic)

A *voyage* is one round trip of one free commercial ship. `VoyageManager.update(world)` is called once per tick and:

1. **Completes arrivals first** — any `ActiveVoyage` whose `arrive_tick` has come is sold: the treasury is credited **once** (`qty × unit_price`, the price locked at load time), a record is archived, and the ship is freed.
2. **Then considers departures** — for each linked, enabled, configured, off-cooldown city, in deterministic id order, dispatch one voyage iff all four conditions hold.

**The four departure conditions** (all from the brief, all answered by a `VoyageWorld` adapter the window supplies — the manager never touches the map or walkers):

| Condition | Method | Window adapter answers from |
|---|---|---|
| Free commercial ship available | `ships_available()` | commercial-harbour berths (`ship_slots`, `port_role=="commercial"`) − voyages at sea |
| Good in stock at the port | `port_stock(good)` / `take_from_port` | the global `economy.resources` pool |
| Destination not at war | `no_war_at_destination(city)` | no hostile `Enemy` walker on the board |
| Sea route clear of pirates | `no_piracy_on_route()` | no hostile `EnemyShip` afloat |

**Capacity:** a hull carries `min(VOYAGE_SHIP_CAPACITY, stock)` = up to **1000 units** ("usually 1000 total unit of goods"). Partial holds sail; empty ones don't.

**Interval (distance-scaled):** after a launch the city is on cooldown for `VOYAGE_INTERVAL_BASE + VOYAGE_INTERVAL_PER_DISTANCE × distance` ticks — the modelled there-and-back sailing time. Each `TRADE_CITIES` entry gained a `distance` field (Roma 2 → ~18 ticks; Alexandria 9 → ~46). **This is what makes "interval depends on distance" concrete.**

**Payout:** `voyage_unit_price` reuses `bartering.STOCK_PRICES` scaled by `VOYAGE_PROFIT_FRACTION` and the city's `profit_mult`, floored at 1 — so an expensive good to a far city earns the most.

**Why pure / why a separate module:** identical split to `bartering`/`gold_trade`/`commercial_roads` — all the economics (when a trip may leave, cargo, payout, cooldown, save shape) live in `voyages.py` with no arcade import, unit-testable headless; the window owns the thin `_voyage_world` adapter. A modder rebalances commerce without touching engine or window code.

### 12.3 — Wiring

- **Creation:** `_ensure_commercial_roads` (already the single rebuild point for the road manager) now also builds `voyage_manager`, so the two share a lifecycle and never hold a stale economy reference.
- **Tick:** `on_update` runs `trade_manager.update()` then `voyage_manager.update(self._voyage_world())`; completed trips raise a green notification.
- **UI:** the Commercial-roads city-detail view gained a **Per-trip voyages** block (good, interval, unit price, free-ship count, Pause/Resume, Stop) and a **Ship by trip** button. *(At v0.51 this sat beside an **Add route** button; v0.53 removed the per-tick route controls, leaving Ship-by-trip and the good picker — see Chapter 14.)* Unlinking a city calls `voyage_manager.clear_city` (in-flight ships still return).
- **Save:** a `voyages` block (configs, cooldowns, in-flight voyages, lifetime tallies, tick clock); older saves load empty.

### 12.4 — The finance panel (`$`)

`$` (Shift+4; both `DOLLAR` and `KEY_4`+Shift accepted, bare `4` stays the palette hotkey) opens a read-only **Finance** modal in the diagnostics mixin (`_draw_finance_panel`). Three blocks in gold/tick: **Income** (taxes, production, trade routes, voyages), **Upkeep & expenses** (wages, upkeep, money-consumption), and **Balance** (net/tick green/red + current treasury), with a voyage lifetime footnote.

Its data source is the new `economy.finance_breakdown` dict, stamped each tick with the economy-owned lines (`tax`, `production`, `wages`, `upkeep`, `consumption`). The window injects the two out-of-economy income lines (`trade_routes` from `TradeRouteManager.last_tick_profit`, `voyages` from the gold landed this tick) after the tick — attribution only, not a second credit. Voyage income is lumpy (0 on most ticks, a spike on a return), so the panel also shows the lifetime total.

### 12.5 — Audit status after v0.51

- **7.2 (inert unit-editor fields)** — STILL OPEN, untouched.
- **9.1 (`Storage.distribute` per-tick rebuild)** — STILL OPEN; voyages read the global pool directly, so unchanged.
- **New (minor):** the voyage `VoyageWorld` war/piracy proxies are *coarse* — any enemy walker closes *all* destinations, any enemy ship closes *all* sea lanes. There is no per-foreign-city war state or per-route piracy zone yet. Adequate for the current single-coastline maps; a candidate refinement if per-city diplomacy ever lands.
- **New (minor):** ship availability counts *commercial-harbour berths* as the hull pool rather than spawning a visible `TradeShip` per voyage. The voyage layer is abstract (like the per-tick routes) — it does not yet drive an on-screen ship sprite. Tying a launched voyage to a real `TradeShip` walker (which already exists, see Chapter 11) is the obvious next step to make voyages visible.

### 12.6 — What still needs a GL context to verify

All voyage logic and the finance breakdown are headless-tested (`tests/test_v051_voyages.py`, 45 tests; +2 in `test_saveload.py`; suite 1241 green). Unverified on screen: the Per-trip voyages block + Ship-by-trip/Pause/Stop clicks, the `$` finance panel rendering and per-tick updates, and the green arrival notification.

---

## Chapter 13 — v0.52: visible voyages, the `!` fleet panel, and two design questions answered

v0.52 acts on a focused brief and, in the process, answers two
architecture questions that have been hovering over the project since
the monolith split: *why mixins?* and *should rendering be batched /
should the object soup become numpy arrays?* This chapter records the
v0.52 changes and then gives a considered answer to each.

### 13.1 — What v0.52 changed

- **Inter-city commerce earns gold only per trip.** `TradeRoute` gained
  a `pays_treasury` flag (default `True` — the generic ambient-route
  primitive and all its tests are untouched). The commercial-roads
  window now builds its routes with `pays_treasury=False`: stock still
  moves out each tick (the export is real, `total_traded` still
  accrues), but no gold is credited. Gold comes only from the per-trip
  voyage layer. The flag round-trips through save/load; pre-v0.52 saves
  load as paying.
- **Voyages are visible.** `VoyageManager.update` now calls optional
  `on_voyage_launched` / `on_voyage_completed` hooks on the duck-typed
  `VoyageWorld`. The window's adapter spawns a real `TradeShip` from a
  commercial-harbour water tile to the nearest map-edge water tile on
  launch and turns it home on completion. The ship is tagged
  `_voyage_ship` so `_process_ships` skips its export-income credit and
  reload — the voyage layer owns the economics, the ship is cosmetic,
  and there is no double-credit. This closes the "obvious next step"
  flagged in 12.5.
- **`!` commerce-ships panel.** Busy (at-sea) voyages — cargo,
  destination, payout, ETA, soonest-first — vs free commercial berths,
  with a lifetime footnote. Same overlay tier / mutex as `$`.
- **"Mapping keys keyboards" splash button.** A pre-game overlay: key
  bindings (from the shared `HELP_TEXT`) plus the absolute on-disk
  folders the game saves to / loads from (`SAVES_DIR`, `MAPS_DIR`,
  `DATA_DIR`).

Suite: **1263 green** (was 1241 at v0.51; +22, no regressions).

### 13.2 — Design note: why mixins, and not the alternatives

`CaesarGameWindow` is composed from ~20 mixin classes
(`SplashMenuMixin`, `DiagnosticPanelsMixin`, `TradePanelsMixin`, …) that
the window subclasses. New contributors reasonably ask why this pattern
and not one of the textbook alternatives. The honest answer is that the
mixin split was a *pragmatic extraction of a 288 KB monolith*, and among
the realistic options it was the one with the lowest friction for this
specific codebase. The reasoning, option by option:

**The forces at play.** The methods being split out (panel draws, key
handlers, splash actions) share one enormous amount of mutable
state — the live `GameWindow`: `self.economy`, `self.walker_manager`,
`self.game_map`, dozens of `show_*` flags, the cameras, the texture
atlas. They are not independent subsystems with clean inputs and
outputs; they are *views and controllers over one god-object*. Any
refactor has to preserve cheap access to all of that shared state from
every extracted method, because that is what the methods fundamentally
*are*. That single fact is what decides between the patterns.

**Mixins (chosen).** A mixin method is still an ordinary method on the
final `GameWindow` instance: `self` is the window, so every extracted
method keeps O(1), zero-ceremony access to all shared state with **no
signature changes and no call-site changes**. Extraction is almost
mechanical — cut a cluster of methods into a `…Mixin`, add it to the
base list — which is exactly what you want when paying down a monolith
incrementally with a green test bar between each step. The cost is real
and we acknowledge it: a flat `self.` namespace shared across all
mixins (name collisions are possible and only caught by reading), MRO
order matters, and the type of `self` inside a mixin is "the eventual
window, trust me." For a single-window desktop app with one composed
class, those costs are bounded and visible; for a library exposing many
combinations they would not be.

**Why not composition / delegation (the usual first suggestion).** The
clean-architecture answer is "make `FinancePanel`, `TradePanel`, etc.
separate objects the window *has*, not *is*." We rejected this as the
primary structure because every one of those objects would need a
back-reference to the window to read the shared state
(`self.window.economy.…`), which reintroduces the coupling the pattern
claims to remove while adding a layer of `.window.` indirection to
*every* state access in code that is overwhelmingly state-access. You
trade an implicit-`self` namespace for an explicit-`self.window`
namespace and a pile of constructor wiring, and you still can't move a
panel to a different host. The coupling is inherent to the domain (a
panel *is* a view over the live game), so paying ceremony to "decouple"
it buys little. Where a piece genuinely *is* separable, we already use
composition — `EconomyManager`, `WalkerManager`, `VoyageManager`,
`TriggerManager` are plain owned objects with their own state and
narrow interfaces, and `voyages.py` is deliberately pure and
window-free (see the hook design in 13.3). The rule we follow:
**composition for subsystems that own state and have a real interface;
mixins for methods that are views/controllers over the window's
state.**

**Why not one giant class (the status quo ante).** That was the 288 KB
monolith. It "works" and has the same `self.` access, but it is the
thing being fixed: unnavigable, untestable in pieces, and a merge-
conflict magnet. Mixins keep the identical runtime shape (one composed
class, one `self`) while giving the source file-level seams.

**Why not a component/ECS or a plugin registry.** An entity-component
system is the right tool when you have *many* entities with *varying*
combinations of behaviour — which is true of the walkers, and indeed
the walker hierarchy is where an ECS would pay off if anywhere (see
13.4). It is the wrong tool for *one* window: there is a single
`GameWindow`, so "composable components" solves a multiplicity problem
we don't have at the window level, at the cost of a registry, message
plumbing, and indirection on every state touch. Likewise a formal
plugin/event-bus between panels would be over-engineering for code that
just needs to read `self.economy` and draw.

**The honest verdict.** Mixins here are not held up as the platonically
correct OOP design; they are the *correct extraction strategy for a
state-saturated god-object you are de-monolithing incrementally*. They
preserve behaviour exactly (the green bar proves it at every step),
require no call-site churn, and keep the door open to promoting any
genuinely-separable cluster to a composed subsystem later
(`voyages.py` is the model for that). The discipline that keeps the
pattern healthy: keep pure logic *out* of mixins and in window-free
modules (economy, voyages, trade, walkers), so the mixins stay thin
view/controller layers and the testable core never depends on the
window.

### 13.3 — How the visible-ship hooks preserve the pure core

The visible-ship feature is a small case study in the 13.2 discipline.
`voyages.py` must stay pure (it is headless-tested with a scripted fake
world), yet a launched voyage must now reach out and spawn an Arcade
walker. The resolution is the **duck-typed world boundary that already
existed**: `VoyageManager` never imports the window; it calls
`getattr(world, "on_voyage_launched", None)` and invokes it only if
present, inside a guard. A world without the hooks (every test's fake)
behaves exactly as before; a hook that raises cannot roll back the
launch or lose the gold credit. All the Arcade/walker knowledge lives
in the window's `_voyage_world` adapter. The pure core gained a feature
without gaining a dependency — which is precisely why voyages was built
as a composed, window-free subsystem rather than a mixin.

### 13.4 — Should we batch SpriteLists for faster rendering?

**Yes — this is the single largest available performance win, and it is
a rendering change, not a sim change (consistent with Chapter 9's
verdict that the cost is at draw time, not in the economy).**

The current renderer is **fully immediate-mode**. `_draw_terrain`,
the building pass, and every `Walker.draw()` call `arcade.draw_texture_rect`
once per visible object, every frame. On a 64×64 map that is up to
~4,096 terrain draw calls per frame *before* buildings and walkers —
each one a separate Python→GL round trip. This is the textbook case
Arcade's `SpriteList` exists to fix: a `SpriteList` uploads its sprites'
geometry to the GPU once and redraws them in a **single batched draw
call**, so N objects cost ~one call instead of N.

Recommended, in priority order:

1. **Terrain → one (or a few) `SpriteList`(s).** Terrain is static:
   tiles don't move and rarely change. Build a `SpriteList` of terrain
   sprites once at map load, mutate individual sprites only when a tile
   actually changes (place/bulldoze), and draw the whole thing in one
   call. This removes the largest per-frame draw cost outright and is
   the cheapest to implement because the data is static. Arcade can
   cull off-screen sprites for a `SpriteList`, so a large map off the
   viewport stays cheap.
2. **Buildings → a `SpriteList`** keyed by building id, updated on
   place/bulldoze (same dirty-on-change discipline as terrain).
3. **Walkers → a `SpriteList`** whose sprite positions are updated each
   frame from walker `(row, col)` interpolation. Walkers move, so this
   one is rewritten per frame, but it is still one batched draw instead
   of one call per walker, and walkers are far fewer than tiles.
4. **Overlays** (jobs/jobless grids, route overlays) are the same shape
   and benefit identically.

Caveats worth stating so the change is done correctly: a `SpriteList`
is most valuable when its contents are *stable* (static terrain is the
ideal); for per-frame-mutated lists the win is the batched draw, not
zero work. The migration is mechanical but touches the hottest, most
visible path, so it should be done one layer at a time (terrain first)
with screenshots/manual GL verification between steps — the headless
suite cannot see pixels, so this is exactly the kind of change Chapter 9
means by "structural, verify on screen." Expect the largest FPS
improvement on big, zoomed-out maps where the immediate-mode call count
is highest.

### 13.5 — Can we cut object count with numpy data arrays?

**Partly — and the distinction matters. numpy helps the *grid/sim*
data, not the *rendering* and not the heterogeneous walker objects.**

Where numpy is a genuine fit:

- **The terrain grid.** It is currently a list-of-lists of small
  values. A 2-D `numpy` array (one `uint8`/`int16` per tile for terrain
  type, parallel arrays or a small struct-of-arrays for overlays) is
  the natural representation: less memory, cache-friendly, and it makes
  whole-grid operations (counting, masking, neighbour scans, the
  jobless/jobs overlay matrices Chapter 9 flags) vectorised one-liners
  instead of Python loops. This is the clearest, lowest-risk win and it
  composes nicely with the terrain `SpriteList` above (the array is the
  source of truth; the sprite list is its view).
- **Per-tile overlay computations** (`jobs.jobless_overlay_grid`,
  catchment/coverage maps, fishery neighbour scans) — these are exactly
  the array-wide arithmetic numpy is built for, and they currently
  allocate full row×col Python matrices each time.
- **The `resources` ledger / economy aggregates** — already small and
  O(buildings), so numpy buys little here, but moving resources to
  all-float arithmetic (Chapter 9 item 4) pairs well with an array
  representation and kills the 7.6 truncation bias.

Where numpy does **not** help, and would hurt:

- **The walkers.** They are heterogeneous Python objects with behaviour
  (state machines, pathfinding, combat, per-class `update`/`draw`). A
  numpy struct-of-arrays could in principle hold their `(row, col, hp)`,
  but their *logic* is polymorphic Python; you cannot vectorise
  `Soldier.update` vs `TradeShip.update` vs `Fireman.update` into array
  math without rewriting the entire walker model into a data-oriented
  ECS. That is a large, risky rewrite for a population (tens, low
  hundreds) that is *not* the bottleneck — Chapter 9 already showed the
  walker cost is the O(n²) combat passes, which are fixed far more
  cheaply by spatial bucketing than by an ECS. So: **don't numpy the
  walkers.** If walker counts ever exploded, the right move is the
  data-oriented ECS discussed in 13.2, not bolting arrays onto the
  current objects.
- **Rendering.** Reducing Python object *count* is not what speeds up
  draw; reducing draw *calls* is (that's the `SpriteList` answer in
  13.4). numpy and `SpriteList` are complementary: numpy for the grid
  the sim reasons about, `SpriteList` for the sprites the GPU draws.

**Net recommendation.** Do the terrain `SpriteList` first (biggest,
safest FPS win), back it with a numpy terrain array (clean source of
truth + vectorised overlays), and leave the walker objects as objects —
fixing their only real hotspot (combat) with spatial bucketing, not a
numpy rewrite. This keeps every change aligned with where the cost
actually is and with the maintainability-first verdict of Chapter 9.

### 13.6 — Audit status after v0.52

- **7.2 (inert unit-editor fields)** — STILL OPEN, untouched.
- **9.1 (`Storage.distribute` per-tick rebuild)** — STILL OPEN.
- **12.5 "visible voyages" next-step** — DONE (13.1/13.3).
- **12.5 coarse war/piracy proxies** — STILL OPEN (unchanged; adequate
  for current maps).
- **New (now documented, not yet done):** immediate-mode rendering
  (13.4) and the list-of-lists terrain grid (13.5) are the two ranked
  structural performance items, both at draw/grid level, neither in the
  sim core.

### 13.7 — What still needs a GL context to verify

All v0.52 logic is headless-tested (`tests/test_trade.py`,
`test_v050_features.py`, `test_v051_voyages.py`, `test_v052_keymap.py`;
suite 1263 green). Unverified on screen: the `TradeShip` actually
sailing out and back per voyage; the `!` commerce-ships panel
rendering and its busy/free rows updating as ships sail/return; and the
splash "Mapping keys keyboards" overlay rendering its bindings + the
three absolute folder paths, closing on Esc/click.

---

## Chapter 14 — v0.53: the per-tick commercial-road route layer is retired

**One-line summary:** inter-city commerce is now **per-trip only**. The
v0.50 per-tick auto-trade route layer — which v0.52 had already stripped
of gold but left moving stock for free every tick — is removed. A linked
foreign city exports goods *only* when a free commercial ship sails a
voyage and sells the cargo on its return (`voyages.py`, Chapter 12).

### 14.1 — Why retire it (the bug behind the change)

By v0.52 the per-tick route had become a **silent value leak**. Each
commercial-road route was built with `pays_treasury=False`, so
`TradeRoute.execute` did `economy.resources[good] -= rate` every tick
but credited **no gold** — and nothing ever connected that export to a
voyage payout. The two layers were fully decoupled: a player running a
`wheat ×50/tick` route while shipping `cheese` by voyage saw the wheat
drain to zero for nothing. Worse, the panel labelled each route row
"gold paid by trip", actively implying the export was monetised. It
wasn't. The audit verdict: a per-tick route under a "gold only by trip"
rule is all cost (stock out, UI complexity, a misleading label) and no
benefit. The faithful model — and the one the original brief asked for
("trading happens per trip, not per tick") — is a single per-trip path.

### 14.2 — What changed

- **`commercial_roads.py` → link-bookkeeping only.** `CommercialRoadManager`
  keeps `link_city` / `unlink_city` / `is_linked` / `link_cost` and the
  `linked` set. The route methods are now deprecated **no-op stubs**:
  `add_route(...)` creates nothing, registers nothing with the shared
  `TradeRouteManager`, logs a warning, returns `None`; `remove_route`
  returns `False`; `routes_for` returns `[]`; `total_routes` returns `0`.
  Because no caller ever builds a `pays_treasury=False` route again, the
  tick loop moves no inter-city stock and credits no inter-city gold.
- **`trade.py` untouched in behaviour.** The generic `TradeRoute` /
  `TradeRouteManager` primitive (ambient routes, `pays_treasury=True`
  default) is unchanged and still available — only the commercial-roads
  *use* of it is gone. The `pays_treasury` flag is retained for the
  ambient primitive, save-compat, and tests.
- **Save/load.** `to_dict` emits `routes: {}` for on-disk shape
  stability; `from_dict` reads the linked-city set and **ignores** any
  legacy `routes` block — those routes are *not* re-registered, so a
  pre-v0.53 save stops leaking stock the instant it loads. A city that
  existed only under the old `routes` block is still promoted to
  `linked` (the player keeps the road they paid for).
- **`mixins/trade_panels.py` → per-trip UI.** Removed the "Stock-moving
  routes" list, the "Add a route" staging area, the rate stepper
  (`-10…+10`), and the "Add route" button; removed the `addroute:` /
  `removeroute:` / `rate:` click actions (a stale `addroute:` action now
  safely no-ops). Kept the good picker and **Ship by trip**; the detail
  view shows a per-trip earnings preview (capacity × unit price ×
  interval), and the city-list row shows voyage state
  (`voyages shipping: <good>` / `paused` / `no voyage set`) instead of a
  route count.

### 14.3 — Migration

Old saves load fine: per-tick routes are dropped, the implied city links
are preserved. Re-enable commerce by opening the city (`R`) and pressing
**Ship by trip**. Modders/scripts calling `add_route` now get `None` +
a warning; configure a voyage via
`VoyageManager.configure(city_id, good=..., enabled=True)` instead.

### 14.4 — Forward design: terrestrial caravans

The deliberate shape of this change is to make the *trip* the unit of
inter-city commerce, so the planned **terrestrial caravan** layer slots
in as another per-trip mechanism rather than a new per-tick drip. The
intended design mirrors `voyages.py`: a pure `CaravanManager` consuming a
`CaravanWorld` adapter (road reachability / pack-animal availability /
banditry on the route, paralleling the voyage adapter's
ship/stock/war/piracy checks), sharing the trip-economy shape (locked
unit price at load, distance-scaled cooldown, gold once on return) and,
eventually, a unified commerce panel covering sea voyages and land
caravans together. See the ROADMAP "Inter-city commerce, next steps"
entry.

### 14.5 — Audit status after v0.53

- **7.2 (inert unit-editor fields)** — STILL OPEN, untouched.
- **9.1 (`Storage.distribute` per-tick rebuild)** — STILL OPEN.
- **12.5 coarse war/piracy proxies** — STILL OPEN (unchanged).
- **The per-tick-route value leak (this chapter)** — RESOLVED by removal.
- **Cleanup (minor, deferred):** the deprecated `CommercialRoadManager`
  route stubs can be deleted outright once no external script/mod is
  expected to call them.

### 14.6 — Tests

The six per-tick route tests in `tests/test_v050_features.py` were
rewritten to assert the retired-no-op contract (`add_route` → `None`,
nothing registered, no per-tick export or gold), plus a new
`test_legacy_save_routes_recover_link_not_routes` proving a pre-v0.53
save recovers the link but no routes and moves no stock on tick. The
window tests `test_add_route_via_click_handler` and
`test_rate_step_clamps_min_one` became `test_ship_by_trip_via_click_handler`
(+ a stale-action no-op test) and `test_good_pick_via_click_handler`.
`tests/test_saveload.py::test_commercial_roads_round_trip` now asserts
links persist, the city route never registers, and the ambient route is
unaffected. Full suite: **1264 passed, 0 failed.**
