# Caesar III Clone — Project Audit & AI-Training Guide (v0.54)

Scope of this document:

1. **Whole-project audit** — health, strengths, and the concrete things worth fixing or improving.
2. **Headless AI training** — how to make this codebase trainable by a computer-controlled agent, what's already in your favour, and what needs to change.
3. **The `R` recording gap** — why "many features are missing in the records," and exactly what to add.

I extracted and ran the project (Python 3.11+, Arcade 3.3.3). The full suite is **1264 passed in ~5 s**, and — importantly — it runs with **no display**. I also stood up a working headless driver (`headless_env.py`, included) that places buildings and advances ticks with no window at all.

---

## 1. Project audit

### 1.1 Overall health: strong

This is a mature, unusually disciplined hobby/educational engine. A few things stand out as genuinely above-average:

- **A pure, deterministic simulation core.** `economy.py`, `game_map.py`, `jobs.py`, `storage.py`, `triggers.py`, `voyages.py` and friends have *no* Arcade imports, no wall-clock, no randomness baked into the math. Same inputs → same outputs. This is the single most valuable property in the repo and it's what makes everything below (testing, headless training, replay) possible.
- **Test coverage is serious.** 1264 tests, organised both by subsystem (`test_economy.py`, `test_combat.py`) and by release (`test_v050_features.py`). The suite runs headless in seconds.
- **Documentation density is exceptional.** Almost every non-obvious line carries a `# vX.Y:` comment explaining *why*, usually with the playtest observation that motivated it. `DEV_AUDIT.md` (80 KB) is a real architectural review, not a stub.
- **Authoring tools are first-class** — map editor, buildings editor, unit editor, trigger editor, cutscene editor, RPG-request editor, all using the same atomic `.tmp → rename` save pattern.

`DEV_AUDIT.md` already catalogues most correctness issues honestly (Chapter 7). I won't re-list all of them; instead I'll group what matters and add what the audit doesn't cover.

### 1.2 The big structural issue: `game_window.py` is a 7,277-line monolith

| Module | Lines |
|---|---|
| `game_window.py` | **7,277** |
| `walkers.py` | 3,765 |
| `game_map.py` | 1,634 |
| `economy.py` | 1,308 |

`game_window.py` owns: rendering, input, the **tick orchestration** (`_game_tick`), HUD panels, modal windows, and the recorder wiring. The mixins (`mixins/*.py`) peel off the editors, but the core game loop and ~30 panels still live in one file.

Why this matters beyond aesthetics: **the simulation orchestration is trapped inside the GL window.** The individual subsystems are pure, but the per-tick *sequencing* of them — construction → services → economy → storage → walkers → decay → triggers → rebellion → commerce — exists only as `CaesarGameWindow._game_tick()`. You can't run "one real game tick" without a `CaesarGameWindow` instance. This is the root cause of the headless-training friction in §2.

**Recommendation (high-leverage, enables §2 and §3):** Extract a headless `Simulation` (or `GameCore`) class that owns the model state (`economy`, `game_map`, `walker_manager`, `service_map`, `storage`, `decay`, `triggers`, `rebellion`, `voyage_manager`, `game_time/month/year`) and a single `tick()` method containing the body of `_game_tick`. `CaesarGameWindow` then *holds* a `Simulation` and only does rendering/input. Nothing about gameplay changes; tests, the recorder, and an AI environment all get to call `sim.tick()` directly.

### 1.3 Correctness / honesty issues worth closing

These are the ones I'd prioritise (several already flagged in `DEV_AUDIT.md` Ch. 7, confirmed still present in v0.54):

1. **RPG-request feature is authored but never displayed at runtime (DEV_AUDIT 7.1, "highest").** The editor writes `data/rpg_requests.json`, triggers can `fire_request`, but there's no consumer that draws the panel and applies the decision. `cutscene_player.py` is the obvious template to mirror. Either build the consumer or gate `fire_request` behind an explicit "not implemented" warning so scenario authors don't build content that can't render.
2. **Unit editor exposes six inert fields (DEV_AUDIT 7.2).** `defense`, `training_time`, `cost`, `upkeep`, `weapon_cost`, `skills` are all editable but **none is consumed** by combat/spawn/upkeep code. A player who sets `cost=2000` sees no change. Wire the cheap ones (`cost`/`weapon_cost` at spawn, `upkeep` per tick are a few lines each) or hide the sliders and label them "planned." An inert slider teaches players to distrust the whole editor.
3. **Documentation drift around the `R` key (new, see §3).** `data/recordings/README.md` and the recorder docstring still say "press `R`," but as of v0.50 recording moved to **Ctrl+R** (`R` now opens commercial roads). `HELP_TEXT` is correct; the README and docstring are stale. This is almost certainly contributing to your "recording" confusion.
4. **`int()` truncation drops fractional output every tick (DEV_AUDIT 7.6).** Small, but it biases the economy and makes per-tick analysis noisier. Worth tracking fractional carry rather than truncating.
5. **`reset_overflow()` called before `distribute()`, contradicting its own docstring (7.7); dead warehouse branch double-iterates building positions (7.8).** Both cosmetic/maintainability; cheap to fix.
6. **Worker allocation is placement-order, not need-aware (7.10).** Gameplay-relevant: the first buildings you placed win the labour pool regardless of how badly a later, more critical building needs staff. Worth a priority pass if you want fairer staffing.

### 1.4 Process risk: the "known-broken" baseline

`REFACTORING.md` historically framed 16 failures as "pre-existing and unrelated." The current suite is green (1264/0), so this appears resolved — **good**. Keep it that way: a permanently-red baseline trains the team to ignore the suite, which is exactly where a real regression hides. If any test is genuinely deferred, mark it `xfail(reason=...)`/`skip(reason=...)` rather than leaving it red.

### 1.5 Performance

Per `DEV_AUDIT.md` Ch. 9 and what I see: the economy is fine; the real per-frame cost is rendering and walker iteration, not the math. Don't reach for numpy on the economy — it's not the bottleneck and would cost the readable, testable purity that's the repo's main asset. If you do optimise, target `walkers.py` (3.7 k lines, per-frame) and SpriteList batching, as the audit already suggests.

### 1.6 Smaller polish items

- `game_window.py` should be split (panels → mixins, tick → `Simulation`).
- `walkers.py` wants splitting too (combat resolver vs. movement vs. manager).
- Add a top-level `requirements.txt`/`pyproject.toml` — right now setup is "pip install arcade" in the README; pin versions.
- No `--headless`/CLI entry point exists; `main.py` always opens a window (see §2).

---

## 2. Training a computer AI to play in headless mode

**Good news up front:** the hard part is already done. The simulation is pure and deterministic, and the entire test suite proves it runs with no display. I verified this directly — `headless_env.py` (shipped alongside this audit) creates a map, an economy, places buildings, and advances ticks **without ever opening a window**:

```
reset: {'tick': 0, 'population': 100, 'treasury': 1000.0, ...}
t=  1  pop= 100  treasury=     995  happy= 58.5  fed=1.00  placed=True
t=  3  pop= 100  treasury=     934  happy= 55.9  fed=1.00  placed=True
...
```

So "can the model run headless?" — **yes, already.** What's missing is a *clean seam* to drive the **whole** game (not just the economy) and a *standard environment API* an RL/IL library can talk to.

### 2.1 The one blocker, and the fix

The blocker is §1.2: the full per-tick orchestration lives in `CaesarGameWindow._game_tick()`, which needs a GL window to instantiate. My `headless_env.py` works by *re-implementing a subset* of that tick (economy only — no walkers, services, decay, triggers). That's fine for a proof of concept but **not faithful** — an agent trained against it would learn a different game than the one players play.

**The fix is the §1.2 refactor.** Extract `Simulation.tick()`. Then a faithful headless env is:

```python
sim = Simulation(scenario="rome")     # owns all model state, no GL
sim.place("house", 10, 10)            # player action
sim.tick()                            # ONE real game tick
obs = sim.observe()                   # state -> tensor
```

Until that refactor lands, you have two interim options:

- **(a) Subclass the window without `arcade.run()`.** `CaesarGameWindow` can be instantiated headless in tests via `__new__` + manual attribute setup (see `tests/test_v052_keymap.py` for the pattern). You can build a full window, call `setup()`, then call `_game_tick()` in a loop yourself and never call `arcade.run()`. This gives a *faithful* tick today, at the cost of dragging the GL window object along (works headless, just heavier).
- **(b) Use the PoC `headless_env.py`** for fast economy-only experiments (resource/treasury optimisation), accepting it ignores walkers/services/military.

I'd do (a) for fidelity now and schedule the `Simulation` extraction so (a)'s ugliness goes away.

### 2.2 Defining the RL/IL problem

Once you can call `tick()`, wrap it in a Gymnasium-style env:

**Observation** — turn `observe()` into a fixed-size tensor. Two layers:
- *Scalars*: population, treasury, happiness, fed_fraction, nutrient_diversity, income/expense per tick, unrest %, wages %, jobless, housing headroom (the v0.54 G-graph series are exactly the right "things about to go wrong" features — reuse them).
- *Spatial*: the building grid as an `H×W×C` map (one channel per building category, plus terrain/feature/road channels). `game_map.to_dict()` already serialises this.

**Action space** — the player's real verbs are: select building type, place at (row,col), demolish at (row,col), set tax rate, plus the trade/barter menus. A tractable first cut:
- `MultiDiscrete([n_building_types+2, rows, cols])` where the first dim is {noop, demolish, *building_id…*} and the next two are the target tile. Mask illegal placements with `game_map.can_place()` (it exists and is pure) so the agent never wastes actions on water/occupied tiles.
- Defer the menu actions (barter/trade/tax) to a second phase.

**Reward** — start simple and dense: `Δpopulation + α·Δtreasury_clamped + β·happiness − penalty_for_starvation`. The recorder schema (§3) is basically a ready-made reward dictionary — `fed_fraction`, `happiness`, `rebellion.pressure` are all there.

**Episode** — `reset()` already works (re-instantiate economy + map). Cap at N ticks or terminate on city collapse (population 0 / rebellion fired).

### 2.3 Two training routes

1. **Reinforcement learning from scratch.** Wrap `Simulation` as a `gymnasium.Env`, plug into Stable-Baselines3 / CleanRL (PPO is the safe default for `MultiDiscrete`). Because `tick()` is pure and fast (the whole suite is 1264 ticks-of-logic in 5 s), you can run thousands of steps/sec headless and parallelise with vectorised envs trivially — no display, no audio, deterministic.
2. **Imitation learning from human play.** This is where §3 comes in: record real games as `(observation, action)` pairs and train a policy to mimic them (behavioural cloning), then optionally fine-tune with RL. **This requires recording the player's *actions*, which the current recorder does not capture (§3).** Fixing the recorder is therefore the prerequisite for the imitation-learning route.

### 2.4 A headless CLI entry point

Add a `main.py --headless` (or a `sim_cli.py`) that runs N ticks from a scenario and writes a recording, with no window. This is ~30 lines once `Simulation` exists, and it's what your training jobs and CI smoke-tests will call.

---

## 3. The `R` recording — why features are "missing"

You said: *"by pressing key 'r' for recording, many features are missing in the records."* There are **two distinct problems**, and they compound.

### 3.1 Problem A — `R` may not even be starting a recording anymore

As of **v0.50**, the binding changed:

- **`R`** now opens the **Commercial roads** window.
- **`Ctrl+R`** toggles the tick recorder.

`HELP_TEXT` documents this correctly (`R … (Ctrl+R: tick recorder)`), but `data/recordings/README.md` and the `recorder.py` module docstring **still say "press R."** If you've been pressing plain `R`, you've been opening the commerce window and writing **nothing** — which would absolutely look like "features missing from the records." 

**Fix:** press **Ctrl+R**, and update the two stale docs. (This is audit item §1.3.3.)

### 3.2 Problem B — the recorder captures *state*, never *actions*

This is the substantive gap, and it's the one that blocks imitation learning. The recorder (`recorder.py`) writes one rich JSONL line per tick: population, treasury, happiness, resources, production/consumption, jobs, per-building rows, walker counts, military, diagnostics, rebellion, caesar, barters. That's a thorough **snapshot of the world**.

But it records **nothing the player did**:

- No "placed `house` at (10,10) on tick 412."
- No "demolished (12,8)."
- No "set tax rate to 8%."
- No "launched a voyage / opened a barter / yielded to Caesar."
- No camera/selection context.

I confirmed this: a full-repo search for action/command/replay recording finds only the existing *state* recorder and an unrelated trigger-replay comment. There is **no input/action log anywhere.**

So if your mental model of "recording" is "capture what I did so the AI can copy me," then yes — almost everything is missing, by design. The recorder was built as a *diagnostic* tool ("why did the weapons chain break?"), not a *demonstration* tool.

### 3.3 Other things genuinely absent from the per-tick record

Even as a state log, a few useful fields are missing or approximate:

- **`per_citizen.food_supplied_this_tick`** is documented in the schema header but the code comments admit it's not actually stored — only `fed_fraction` is authoritative. The schema docstring over-promises.
- **No map/terrain or road-network snapshot** — you can't reconstruct *where* things are relative to roads/water from the record alone (building rows have row/col, but no terrain/feature/connectivity context).
- **No voyage / commercial-road / trade-ship state** despite that being the headline feature of v0.50–v0.53.
- **No service coverage** (water/food/religion/etc. maps) — central to happiness, absent from the record.
- **No event/trigger firings as structured data** — only free-form `events_since_last` strings.

### 3.4 What to add — an action recorder for training

The cleanest fix is a **second, parallel log**: keep the state recorder as-is (it's a good diagnostic), and add an **action recorder** that appends one record per player action with the tick it happened on. Together they give you `(state_at_tick, action_at_tick)` — exactly the `(observation, action)` pairs imitation learning needs.

Minimal shape:

```json
{"tick": 412, "action": "place", "building": "house", "row": 10, "col": 8, "cost": 30, "ok": true}
{"tick": 418, "action": "demolish", "row": 12, "col": 5, "refund": 15}
{"tick": 440, "action": "set_tax", "rate": 0.08}
{"tick": 455, "action": "launch_voyage", "harbor": [20,3], "cargo": "wine"}
```

Where to emit these — every player action already funnels through a small number of methods, so this is a handful of one-line `self.action_recorder.note(...)` calls:

- **Placement / demolition:** `game_window._try_place`, `_try_place_brush`, and the right-click demolish handler.
- **Tax:** the `T` key handler (`economy.cycle_tax_rate`).
- **Trade/barter/voyage:** the barter (`B`), gold-trade (`C`), and commercial-roads (`R`) panel confirm buttons.
- **Caesar:** the `Y` yield handler.

Because all of these are in `game_window.py`, and because §1.2's `Simulation` refactor would route actions through a single `sim.place()/sim.demolish()/sim.set_tax()` API, **doing the §1.2 refactor first makes the action recorder trivial** — you instrument one place (the `Simulation` action methods) instead of a dozen UI handlers.

### 3.5 Recommended sequence

1. **Fix the docs** (`R` → `Ctrl+R`) so you're actually recording. *(minutes)*
2. **Extract `Simulation`** with `tick()` + action methods (`place/demolish/set_tax/...`). *(the big one; also fixes §2)*
3. **Add `ActionRecorder`** that logs every action method call with `game_time`. *(small, once step 2 lands)*
4. **Enrich the state recorder** with terrain/road/service/voyage snapshots, or — cleaner — have it serialise `Simulation.observe()` so the recorded state and the agent's observation are *the same object*. This guarantees training data matches what the agent will see at inference. *(medium)*
5. **Wrap `Simulation` as a Gymnasium env** and start with behavioural cloning on the recorded `(observe, action)` pairs, then PPO fine-tuning. *(the payoff)*

---

## Appendix — quick facts I verified

- Python 3.11+, Arcade 3.3.3. `pip install arcade pytest`, suite runs headless.
- **1264 passed in ~5.08 s**, no display required.
- Simulation core (`economy`, `game_map`, `jobs`, `storage`, `triggers`, `voyages`) is Arcade-free and deterministic.
- `economy.to_dict/from_dict` and `game_map.to_dict/from_dict` exist → state is serialisable, so env `reset()`/snapshotting is straightforward.
- `game_map.can_place()` is pure → use it for action masking.
- The tick orchestration is `CaesarGameWindow._game_tick()` (line ~1172) — currently the only thing coupling a full game step to the GL window.
- Recorder = state only; **no action/replay log exists** anywhere in the repo.
- `R` = commercial roads; **`Ctrl+R` = recorder** (since v0.50). README/docstring stale.
