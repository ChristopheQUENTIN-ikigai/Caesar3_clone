# Changelog — v0.55

**Theme:** the recorder finally captures **what the player did**, not
just what the world looked like — the missing half of the data an
imitation-learning agent needs. Plus the long-standing `R` → `Ctrl+R`
documentation drift is fixed.

## Shipped

### Action recorder (`action_recorder.py`, new)

A companion to the v0.19 `TickRecorder`. Where the state recorder
snapshots the world each tick, `ActionRecorder` logs **player
decisions** as a sparse, event-driven JSONL stream:

    {"tick":412,"action":"place","building":"house","row":10,"col":8,"cost":30,"ok":true}
    {"tick":418,"action":"demolish","row":12,"col":5,"removed":"farm","refund":40}
    {"tick":440,"action":"set_tax","rate":0.08}
    {"tick":470,"action":"yield_to_caesar","ok":true}

- **Same toggle.** `Ctrl+R` now starts/stops *both* recorders. They
  share one wall-clock timestamp, so a session writes
  `session_<ts>.jsonl` (state) **and** `session_<ts>.actions.jsonl`
  (actions) side by side.
- **Join on `tick`.** Both logs carry the `game_time` tick, so
  `state.merge(actions, on="tick")` reconstructs the
  `(observation, action)` stream behavioural cloning needs.
- **Open schema.** `note(action, **fields)` writes whatever fields the
  call site passes; a new action kind needs a new call, not a schema
  change here.

### Wiring (`game_window.py`)

- New `ActionRecorder` instance alongside `TickRecorder`; both toggle
  on `Ctrl+R`, the action file inheriting the state file's timestamp.
- New `_record_action(action, **fields)` helper, guarded with `getattr`
  so gameplay never depends on a recorder being wired (test stubs and
  headless harnesses that don't construct one simply skip recording).
- `note(...)` calls added at the four player-action sites: building
  placement (`_try_place`), demolition (right-click handler), tax change
  (`T`), and yield-to-Caesar (`Y`).

### Documentation drift fixed

- `data/recordings/README.md`, `recorder.py` docstring, and the two
  `README.md` recorder mentions corrected from "press `R`" to
  "press `Ctrl+R`" (the binding moved in v0.50; `R` opens commercial
  roads). The recordings README now documents the two-file layout and
  the join recipe.

## Tests

- `tests/test_action_recorder.py` (new, 5 tests) — inactive-by-default
  no-op, one-line-per-action writes, arbitrary-field passthrough,
  shared-timestamp file pairing, idempotent stop.
- `tests/test_debug_logging.py` — fixture binds the real
  `_record_action` onto the duck-typed window (mirrors the existing
  `_try_place` binding); the defaulting namespace makes the inner
  recorder lookup a clean no-op.
- Full suite green: **1269 passed** (1264 prior + 5 new).

## Migration notes

- **Saves unaffected.** The recorders write external JSONL; nothing is
  persisted into save files.
- **No gameplay change.** Recording is opt-in via `Ctrl+R` and the
  action hooks are guarded no-ops when no recorder is attached.
