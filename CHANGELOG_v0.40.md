# Changelog — v0.40

**Theme:** start the v0.40 roadmap thread (harbor transfer), make the
mare_nostrum intro cinematics actually play, and lock down the naval
combat / disembark behaviour with verification tests against the real
walker manager.

## Shipped

### Harbor transfer — proof of concept (roadmap v0.40 #1, first slice)

Trade ships now carry **real cargo** loaded from the goods ledger — the
harbor buffer in miniature — instead of crediting a flat fixed price at
the map edge regardless of stock.

- `TradeShip` gained `cargo_qty` and a `LOAD_AMOUNT` (20 units/trip).
- New `WalkerManager._load_trade_ship(ship, economy)` debits up to
  `LOAD_AMOUNT` units of the ship's good from `economy.resources` and
  records the amount actually loaded. A dry buffer leaves the hull empty
  (no negative stock, no crash).
- Loading happens on launch (in `_process_shipyards`) and again each
  time a ship returns to its home endpoint (in `_process_ships`).
- Export income at the exit is now `price × cargo_qty` — no stock, no
  cargo, no phantom income. The pre-v0.40 flat single-unit credit is
  retained as a backward-compat fallback for a ship that was never
  loaded (`cargo_qty == 0`), so old saves and direct headless callers
  behave exactly as before. The v0.38 naval-combat-event 5-tuple
  contract is unchanged.

Still on the v0.40 list for the full drop: military-harbor troop
embark, and a per-warehouse (rather than global-ledger) buffer.

### mare_nostrum intro cinematics now actually play

The scenario shipped a full authored intro chain (Caesar's landing order
→ naval landing → colony → raid warning → victory) inside its
`map.json` `scenario_libraries` block, with all scene/portrait art
present on disk — but two latent bugs meant **none of it played**:

- **Scenario cutscenes were never in the lookup.** The runtime cutscene
  lookup only read the on-disk `data/cutscenes.json` library and never
  merged the loaded scenario's embedded cutscenes, so every
  `play_cutscene` trigger missed ("cutscene not in library"). New
  `CaesarGameWindow._effective_cutscenes()` merges the scenario library
  into both the by-id and by-flag lookups (disk entries win on an id
  collision).
- **Slide-schema mismatch.** The runtime player reads `image` /
  `caption` / `body`; scenario/editor slides author `scene` /
  `portrait` / `text`. New `cutscene_player._normalize_cutscene()`
  aliases `scene → image` and `text → body` (preserving `portrait`,
  never overriding an explicit `image` / `body`, never mutating the
  shared library dict), so authored scenario slides render their art and
  narration instead of the "(no image)" placeholder.
- Synced the stale standalone `data/scenarios/mare_nostrum/cutscenes.json`
  (it was missing `cs_naval_landing`) with the authoritative embedded
  library.

No new art was needed — every referenced scene/portrait PNG already
existed on disk; the gap was wiring, not assets.

### Naval battle & troop disembarkation — verified

New verification tests drive the **real** `WalkerManager` (real
registry, real `_resolve_combat` / `_process_ships`) over a coastal
map geometry like mare_nostrum's western beach:

- A warship sinks a coastal raider over successive ticks (HP monotone
  down), ignores out-of-range raiders, and emits valid 5-tuple combat
  events.
- A transport lands a real legion onto the shore; troops hold while at
  sea (no adjacent land); landed soldiers are ordinary land units that
  then fight a raider.

## Tests

- `tests/test_harbor_transfer.py` — 6 tests (load/debit, stock cap, dry
  buffer, proportional income, fallback, full round trip).
- `tests/test_scenario_cutscenes.py` — 9 tests (library merge, id
  collision precedence, malformed/empty safety, real-map intro-chain
  resolution, standalone/embedded sync, player schema normalization).
- `tests/test_naval_battle_verification.py` — 6 tests (warship combat,
  range gating, combat events; transport disembark, hold-at-sea,
  landed-legion-fights).

Suite: **1048 passed, 0 failed** (was 1027 at v0.39 entry; +21).

## Files touched

- `walkers.py` — TradeShip cargo, `_load_trade_ship`, load wiring.
- `game_window.py` — `_effective_cutscenes()` merge in both lookups.
- `cutscene_player.py` — `_normalize_cutscene()` slide-schema aliasing.
- `data/scenarios/mare_nostrum/cutscenes.json` — synced with map.json.
- `version.py` — `v0.39` → `v0.40`.
