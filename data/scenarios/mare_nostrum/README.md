# Mare Nostrum — Island Landing (scenario design & build guide)

A crafted 64×64 scenario: Caesar orders the legion across the sea to land on a
great island, establish a colony, explore and exploit it, and defend it against
raids from the arid south. This bundle contains everything that is **authorable
today** with the four editors, plus a clearly-scoped backlog of the engine work
the naval/raid mechanics need.

---

## What's in this bundle

```
mare_nostrum/
├── map.json          64×64 island: sea ring, mountain spine (N), desert (S),
│                     forests / ore veins / fish shoals, a west-coast colony,
│                     embedded triggers + scheduled events + content libraries.
├── cutscenes.json    4 cinematics (also embedded in map.json's scenario_libraries)
├── rpg_requests.json 1 multi-choice tribute decision (GATED — see backlog)
└── events.json       2 economy events (Southern Raiders, Caesar's Wrath)
```

To install, drop the folder under `data/scenarios/mare_nostrum/` (mirrors the
`tribute_crisis` layout). The map loads via `mapfile.load_map(game, path)` and
has been validated against the live loader: 100 % of the land is reachable from
the colony, the sea border ring is complete, and fish shoals sit in water.

---

## The four pillars, and where each one stands

| Pillar | Status | How it's built |
|---|---|---|
| **Caesar's landing order** | ✅ authored | `cs_caesar_order` cutscene on tick 0 (throne room → harbor). |
| **Exploration / colonization** | ✅ authored | Flags (`mission_accepted`, `colony_established`, `island_secured`) gated by population triggers; resource features reward expansion. |
| **Defence vs. southern raids** | ⚠️ partial | Modelled as the `Southern Raiders` *economy event* (pop/happiness hit) on a schedule. A real raid of enemy *walkers from the south edge* needs engine work (below). |
| **Naval troop transport** | ❌ blocked | No ship walker, no water-crossing for units. Pure fiction today (the colony is pre-landed). Needs engine work (below). |

The honest framing: this ships as a **narrative scenario** — the landing and the
raids are scripted story beats, exactly as much of Caesar III's campaign always
was. The literal ship-and-invade mechanic is a separate engine task.

---

## The narrative spine (how the editors wire together)

```
Trigger editor  ── when:condition ──►  do: play_cutscene / set_flag / fire_event / fire_request
      │
      ├─► Cutscene editor   ─► full-screen scene + portrait + slides   (✅ runtime wired)
      ├─► Event editor      ─► economy shock (pop/happiness/resources)  (verify in-game)
      └─► RPG editor        ─► multi-choice decision panel              (⚠️ see finding 7.1)

Flags carry story state forward: an RPG/cutscene/trigger sets a flag,
and a later trigger keys on it (on_flag) to unlock the next beat.
```

Authored beat sequence in `map.json`:

1. **tick 0** → `cs_caesar_order` (the order).
2. **tick 2** → set `mission_accepted` (RPG-panel-free acceptance — see fork below).
3. **pop > 80** → `cs_colony_thrives` + flag `colony_established`.
4. **month 8** → `cs_raid_warning`; **month 9 / year 2** → `Southern Raiders` event.
5. **pop > 200** → flag `island_secured` + `cs_victory` (win condition).

---

## ⚠️ Dependency you must resolve before authoring richer choices

The **RPG decision panel is audit finding 7.1**: requests are loaded, queued via
`fire_request_for_id`, but **never displayed** — the consumer of
`_pending_rpg_requests` was never built. The cutscene path, by contrast, is fully
wired end-to-end and is the template for fixing it.

So there's a fork for the "Caesar asks for tribute" beat:

- **Option A (ships today):** use the `mission_accepted` auto-flag + a cutscene.
  The `rpg_caesar_tribute` request is included in the bundle but will not appear
  in-game until 7.1 is fixed.
- **Option B (richer):** build the RPG consumer first (mirror `cutscene_player.py`'s
  state machine: pause → draw scene+portrait+decision buttons → on click, apply
  the decision's `effects` via `economy.apply_event_effects`, honour `delay_ticks`,
  set the flag / fire the event). Then the three-way tribute choice becomes real.

Verify Option-A's event path too: confirm the `Event editor`'s output actually
fires in-game (the audit didn't fully trace the event runtime).

---

## Engine backlog (the parts no editor can author)

These are code tasks in `walkers.py` / `units.py` / `constants.py`, ordered by
how much scenario value they unlock per unit of effort:

1. **`force_raid` as a trigger effect + a spawn-edge parameter.** `WalkerManager`
   already has `force_raid` / `force_raid_punitive`; expose it in
   `triggers_editor.EFFECT_TYPES` and let it take a `side` ("south" → spawn from
   the high-row edge). This turns the scripted `Southern Raiders` event into an
   actual wave of enemy walkers from the south. **Smallest, highest payoff.**
2. **A `TransportShip` walker + embark/disembark.** New walker class that *is*
   allowed on `TERRAIN_WATER` (relax `_impassable` for that class only), carries
   N soldiers, and unloads them on an adjacent land tile. This is the literal
   "transport legions to land" mechanic. Medium effort, self-contained.
3. **A naval unit type in `units.json`** (`faction`, `category: "naval"`) and a
   ship sprite. Cheap once #2 exists.
4. **Ship-vs-ship combat pass** in `_resolve_combat` (optional; only if you want
   sea battles, not just transport). Largest, do last.

None of these block the narrative scenario — it's playable now and they slot into
the same bundle when ready.

---

## Map facts (for reference)

- 64×64, ~2400 sea tiles, ~1690 land tiles, full border sea ring.
- Mountain spine north-of-centre (mining/relief), arid desert band in the south
  (the raider approach), grass interior for farms/housing.
- 230 terrain features: forests, fertile soil, iron/copper/gold veins, stone &
  clay deposits, groundwater, and 40 coastal fish shoals (in water → fisheries work).
- Roman beachhead colony on the west coast: forum, houses, farm, well, granary,
  warehouse, prefecture, barracks, linked by roads.
- Starting state: pop 40, treasury 1500, 5 infantry, stocked for early building.
