# Tick recorder output

When the player presses **Ctrl+R** in-game, two append-only JSONL files
are written here per session (plain `R` opens the commercial-roads
window — the recorder moved to Ctrl+R in v0.50):

    session_YYYYMMDD_HHMMSS.jsonl           # per-tick WORLD STATE
    session_YYYYMMDD_HHMMSS.actions.jsonl   # per-action PLAYER DECISIONS

The two files share a timestamp and both carry a `tick` field, so they
join on `tick`:

    import pandas as pd
    state   = pd.read_json("data/recordings/session_*.jsonl", lines=True)
    actions = pd.read_json("data/recordings/session_*.actions.jsonl", lines=True)
    paired  = state.merge(actions, on="tick", how="left")  # (obs, action) stream

Schemas:
  * State   — see `recorder.py`         (population, treasury, buildings, …)
  * Actions — see `action_recorder.py`  (place / demolish / set_tax / barter / yield…)

The state log answers "what was the world like each tick?"; the action
log answers "what did the player do, and when?". Together they're the
(observation, action) pairs imitation learning needs — see
`AUDIT_AND_TRAINING_GUIDE.md` §3.

This directory is created on demand at first record; the README is
shipped so a fresh checkout makes the layout explicit.
