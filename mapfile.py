"""Scenario map files — JSON-based persistence for the map editor.

A *map file* is a self-contained scenario: terrain + features +
buildings + roads, plus the starting *budget* the player gets when
they load it (population, treasury, raw stocks, nutrients in granaries,
army composition, in-game date). It's distinct from a *save file*
(``saveload.py``) — saves are mid-campaign snapshots that persist
walkers, decay, Caesar pressure, and other transient state. Maps are
the *initial conditions* the player is dropped into.

Why a separate format?

  * Saves carry runtime state (walker positions, supply memory,
    feature reserves) that's meaningless for a fresh scenario.
  * Maps are authored — the editor is the canonical writer; an
    author wants to set "you start with 200 stone and 50 fish"
    without inheriting the previous game's wage_payment_ratio.
  * The schema is intentionally smaller and stable — adding a new
    walker class doesn't bump the map version.

File layout (``./data/maps/<slug>.json``):

::

    {
      "version": 1,
      "name": "Riverside Trade Hub",
      "scenario": "default",          # carried into game.scenario on load

      // ── Starting budget ────────────────────────────────────────
      "nutrients": {                  # initial granary stocks
        "bread": 100, "vegetables": 0, "fruits": 0,
        "meat": 0,    "fish": 0,      "cheese": 0,
        "oil": 0,     "honey": 0,     "spice": 0, "wine": 0
      },
      "population": 50,
      "workers": {                    # role-keyed worker distribution
        "worker": 30, "trader": 10, "soldier": 0, "citizen": 10
      },
      "treasury": 1000,
      "stocks": {                     # warehouse-side raw goods
        "stone": 100, "stone_blocks": 50,
        "iron": 150, "wood": 300, "tools": 50,
        "planks": 20, "wheat": 50, "flour": 20,
        "weapons": 0, "olives": 0, "grapes": 0,
        "clay": 0, "pottery": 0, "livestock": 0, "horses": 0
      },
      "army": {                       # cavalry / infantry composition
        "infantry": 0, "cavalry": 0
      },
      "calendar": {"year": 1, "month": 1},

      // ── World ──────────────────────────────────────────────────
      "terrain": [[r, c, t], ...],            # overrides over default grass
      "terrain_features": [[r, c, f], ...],   # forest, vein, fertile, …
      "terrain_feature_state": [[r, c, reserves|null], ...],
      "buildings": [{"type": "...", "row": int, "col": int}, ...],
      "roads": [[r, c], ...]          # convenience: roads listed
                                      # separately so the editor can
                                      # show a "X roads placed" stat
                                      # without scanning buildings
    }

The ``roads`` list is redundant with ``buildings`` (a road is just a
building of type "road") but cheap and useful — the editor's stats
panel reads it directly without iterating the full building list.

Versioning: bump ``version`` when the schema changes. ``load_map``
migrates older files forward; the migration table mirrors
``saveload._migrate``.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from constants import MAPS_DIR, NUTRIENTS, SCENARIOS_DIR

log = logging.getLogger("caesar3.mapfile")

CURRENT_MAP_VERSION = 1

# Resources the warehouse-side budget is allowed to seed. Keeping
# this list explicit (rather than "everything not a nutrient") gives
# the editor a deterministic UI: a fixed grid of stocks the author
# can edit, even if the game adds new resources later.
WAREHOUSE_STOCKS: tuple[str, ...] = (
    "stone", "stone_blocks",
    "iron", "wood", "tools",
    "planks", "wheat", "flour",
    "weapons",
    "olives", "grapes",
    "clay", "pottery",
    "livestock", "horses",
)

# Worker role keys the game tracks. Mirrors the ``worker_role`` field
# in ``data/buildings.json`` (worker / trader / citizen / soldier).
# The editor presents them in this order.
WORKER_ROLES: tuple[str, ...] = ("worker", "trader", "soldier", "citizen")


def _ensure_dir() -> None:
    os.makedirs(MAPS_DIR, exist_ok=True)


def slugify(name: str) -> str:
    """Convert a human-readable map name to a safe filename stem.

    Lower-cases, replaces non-alphanumerics with underscores, collapses
    runs, strips leading/trailing underscores. Empty / all-symbols
    names fall through to "untitled" so the caller never gets an
    empty filename.
    """
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return s or "untitled"


def list_maps() -> list[str]:
    """Return the list of ``.json`` files in ``./data/maps``.

    Filenames only — no path prefix, no extension stripped. The
    splash "Load map" picker iterates this and shows the bare names.
    Order is alphabetical for stability across runs.
    """
    _ensure_dir()
    return sorted(
        f for f in os.listdir(MAPS_DIR) if f.endswith(".json")
    )


def list_scenarios() -> list[dict[str, str]]:
    """Return the list of scripted scenarios in ``./data/scenarios``.

    A *scenario* is a subdirectory of ``data/scenarios/`` that
    contains a ``map.json`` at its top level. Other files in the
    same folder (``cutscenes.json``, ``rpg_requests.json``,
    ``events.json``, ``README.md``, ``assets/``) are scenario-local
    libraries / docs / placeholders — the runtime resolves them via
    the map's ``scenario_libraries`` block at load time. We
    deliberately don't validate those here; an in-progress scenario
    with only a ``map.json`` is still loadable for testing.

    Each returned entry is::

        {"id":   "tribute_crisis",          # folder name
         "name": "The Tribute Crisis",      # map.json "name" (or id)
         "path": "scenarios/tribute_crisis/map.json"}  # relative to DATA_DIR

    The path is data-dir-relative on purpose so the picker can
    pass it straight to ``load_map_path``. Folders without a
    ``map.json`` are skipped silently (in-progress authoring is not
    an error). Order is alphabetical by id for stability.

    Returns an empty list if ``data/scenarios/`` doesn't exist —
    nothing to enumerate, no exception.
    """
    if not SCENARIOS_DIR.is_dir():
        return []
    out: list[dict[str, str]] = []
    for child in sorted(SCENARIOS_DIR.iterdir()):
        if not child.is_dir():
            continue
        mp = child / "map.json"
        if not mp.is_file():
            continue
        # Pull a friendly name out of the map JSON. Failure to parse
        # is non-fatal here: we still surface the scenario by id so
        # the author can pick it and see the real error in load_map.
        friendly = child.name
        try:
            with open(mp) as f:
                data = json.load(f)
            n = data.get("name") if isinstance(data, dict) else None
            if isinstance(n, str) and n.strip():
                friendly = n.strip()
        except (OSError, json.JSONDecodeError):
            log.warning(
                "list_scenarios: %s exists but is unreadable; "
                "listing by folder name only", mp,
            )
        out.append({
            "id":   child.name,
            "name": friendly,
            "path": f"scenarios/{child.name}/map.json",
        })
    return out


def default_budget() -> dict[str, Any]:
    """Return a fresh "starting budget" dict the editor can hand to
    the UI. Every field is set to a sensible zero/default; the author
    edits up from here.
    """
    from constants import GRID_COLS, GRID_ROWS
    return {
        "nutrients": {n: 0 for n in NUTRIENTS},
        "population": 0,
        "workers": {role: 0 for role in WORKER_ROLES},
        "treasury": 0,
        "stocks": {g: 0 for g in WAREHOUSE_STOCKS},
        "army": {"infantry": 0, "cavalry": 0},
        "calendar": {"year": 1, "month": 1},
        # v0.26: grid dimensions. Default to the engine's module-level
        # GRID_COLS / GRID_ROWS so a fresh map is the standard size;
        # the editor's +/- buttons step these and on save they go into
        # the JSON so reloading recreates the same world.
        "grid": {"width": GRID_COLS, "height": GRID_ROWS},
    }


def collect_map_state(game) -> dict[str, Any]:  # noqa: ANN001
    """Snapshot the current game world into a map-file dict.

    Reads from the live ``game.game_map`` and ``game.economy``. Used
    by the map editor's "Save map" button. The result is JSON-ready
    (no sets, no datetimes, no dataclass instances).
    """
    eco = game.economy
    gm = game.game_map
    map_data = gm.to_dict()
    # Pull nutrients out of resources; everything else lands in stocks
    # if it's in WAREHOUSE_STOCKS.
    nutrients = {n: int(eco.resources.get(n, 0)) for n in NUTRIENTS}
    stocks = {g: int(eco.resources.get(g, 0)) for g in WAREHOUSE_STOCKS}
    # Worker distribution: zero by default; the editor can edit these
    # post-snapshot. We compute current totals from the live economy
    # so a player who built a city and then wants to "save it as a
    # map" gets accurate numbers.
    workers = _aggregate_worker_roles(game)
    # Roads list (convenience side-channel for the stats panel).
    roads = _collect_road_positions(gm)
    # Army: read from walker manager if present; otherwise zeros.
    army = _aggregate_army(game)
    return {
        "version": CURRENT_MAP_VERSION,
        "name": getattr(game, "editor_map_name", "Untitled"),
        "scenario": getattr(game, "scenario", "default"),
        "nutrients": nutrients,
        "population": int(eco.population),
        "workers": workers,
        "treasury": int(eco.treasury),
        "stocks": stocks,
        "army": army,
        "calendar": {
            "year": int(getattr(game, "year", 1)),
            "month": int(getattr(game, "month", 1)),
        },
        # v0.26: persist grid dimensions so a map with custom size
        # reloads at that size. Read directly from the live GameMap
        # (which is the source of truth — the editor's +/- buttons
        # mutate it in place via the resize-on-rebuild flow).
        "grid": {
            "width":  int(gm.cols),
            "height": int(gm.rows),
        },
        "terrain": map_data.get("terrain_overrides", []),
        "terrain_features": map_data.get("terrain_features", []),
        "terrain_feature_state": map_data.get("terrain_feature_state", []),
        "buildings": map_data.get("buildings", []),
        "roads": roads,
        # v0.28: scheduled events the map author wired up. Each
        # entry: {"event": name, "trigger": {"kind": ..., "value": ...}}.
        # The `fired` runtime flag is NOT persisted — a freshly-loaded
        # map starts with every trigger un-fired, which is the right
        # default for a scenario "begin from t=0" load.
        "scheduled_events": _collect_scheduled_events(game),
        "disable_random_events": bool(
            _read_disable_random_events(game)
        ),
        # v0.36: trigger wiring — authored by the Event editor. The
        # `triggers` block lives on the game object as `scenario_
        # triggers` (and on the live TriggerManager as `.triggers`);
        # we read whichever is the live source of truth — manager
        # first, fallback to the stashed list for pre-init saves.
        "triggers": _collect_triggers(game),
        # v0.36: per-scenario library file overrides. Points the
        # runtime at scenario-specific cutscenes / requests / events
        # JSON files; absent (default {}) means "use the global
        # data/*.json libraries".
        "scenario_libraries": dict(
            getattr(game, "scenario_libraries", {}) or {}
        ),
    }


def _collect_scheduled_events(game) -> list[dict[str, Any]]:  # noqa: ANN001
    """Read the author-edited scheduled list off the live game (the
    map editor mutates it in-place via the trigger-editor panel).
    Returns just the persistable fields — drops the runtime `fired`
    flag.
    """
    em = getattr(game, "event_manager", None)
    if em is None or not getattr(em, "scheduled", None):
        return []
    out = []
    for s in em.scheduled:
        out.append({
            "event": s.get("event"),
            "trigger": dict(s.get("trigger", {})),
        })
    return out


def _collect_triggers(game) -> list[dict[str, Any]]:  # noqa: ANN001
    """v0.36: read the trigger wiring off the live game.

    Prefers the live ``TriggerManager.triggers`` (so any
    schedule_event / set_flag effects the manager has dispatched are
    captured), falling back to ``game.scenario_triggers`` for the
    cold-load case (manager constructed but never ticked, or game
    object built without one).

    The ``fired`` flag is included so save → load round-trips the
    "this one-shot already happened" state — same contract as
    ``EconomicEventManager.to_dict`` keeping ``fired`` per entry.
    """
    tm = getattr(game, "trigger_manager", None)
    if tm is not None and getattr(tm, "triggers", None):
        return [dict(t) for t in tm.triggers]
    return list(getattr(game, "scenario_triggers", []) or [])


def _read_disable_random_events(game) -> bool:  # noqa: ANN001
    em = getattr(game, "event_manager", None)
    if em is None:
        return False
    return bool(getattr(em, "disable_random_events", False))


def _aggregate_worker_roles(game) -> dict[str, int]:  # noqa: ANN001
    """Sum worker counts by role for every placed building.

    The game doesn't keep a running total — the per-tick ``employed``
    field aggregates across all roles. We walk the placed buildings
    and add ``workers`` per role from the registry. This is the
    *capacity* (max workers if fully staffed), not the live filled
    count, because that's what an author writing a scenario wants:
    "this map's labour pool".
    """
    out = {role: 0 for role in WORKER_ROLES}
    for bt, _r, _c in game.game_map.get_building_positions():
        bd = game.registry.get(bt)
        if bd is None or bd.workers <= 0:
            continue
        role = bd.worker_role if bd.worker_role in out else "citizen"
        out[role] = out.get(role, 0) + bd.workers
    return out


def _collect_road_positions(game_map) -> list[list[int]]:  # noqa: ANN001
    """Return ``[[r, c], …]`` for every road cell (NOT origin —
    every cell, but roads are 1×1 so origin == cell)."""
    out: list[list[int]] = []
    for bt, r, c in game_map.get_building_positions():
        if bt == "road":
            out.append([r, c])
    return out


def _aggregate_army(game) -> dict[str, int]:  # noqa: ANN001
    """Best-effort army snapshot. Walks the WalkerManager looking for
    soldier-type walkers. The walker classes aren't a stable contract,
    so we look up by class name string — modders adding a new soldier
    type can extend ``SOLDIER_CLASS_NAMES`` below.
    """
    SOLDIER_CLASS_NAMES = {"Soldier", "SoldierWalker", "Cavalry", "CavalryWalker"}
    infantry = 0
    cavalry = 0
    wm = getattr(game, "walker_manager", None)
    if wm is None:
        return {"infantry": 0, "cavalry": 0}
    walkers = getattr(wm, "walkers", None) or []
    for w in walkers:
        cn = type(w).__name__
        if cn in SOLDIER_CLASS_NAMES:
            if "Cavalry" in cn:
                cavalry += 1
            else:
                infantry += 1
    return {"infantry": infantry, "cavalry": cavalry}


def save_map(game, name: str) -> str:  # noqa: ANN001
    """Snapshot the current game world to ``./data/maps/<slug>.json``.

    Returns the full path written. The caller (editor "Save" button)
    typically shows a notification with the path.
    """
    _ensure_dir()
    state = collect_map_state(game)
    state["name"] = name or state.get("name") or "Untitled"
    slug = slugify(state["name"])
    path = os.path.join(MAPS_DIR, f"{slug}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)
    log.info("Map saved → %s", path)
    return path


def load_map(game, filename: str) -> bool:  # noqa: ANN001
    """Load ``./data/maps/<filename>`` into the live game state.

    Thin compatibility shim: existing callers (splash "Play custom
    map" picker, tests) pass a bare filename rooted at ``MAPS_DIR``.
    The actual loader lives in :func:`load_map_path`, which the
    v0.36 "Load scenario" picker uses with a data-dir-relative path.
    Returns False on any I/O / parse / migration failure.
    """
    _ensure_dir()
    return load_map_path(game, os.path.join(MAPS_DIR, filename))


def load_map_path(game, path: str | os.PathLike) -> bool:  # noqa: ANN001
    """Load a map JSON from ``path`` into the live game state.

    ``path`` is taken as-is: callers can pass an absolute path or
    anything resolvable from the current working directory. The
    splash "Load scenario" flow uses this with a path rooted at
    ``data/scenarios/<id>/map.json``; ``load_map`` uses it with a
    ``MAPS_DIR``-rooted path.

    Returns False on any I/O / parse / migration failure. The
    caller is expected to have already torn down or freshly
    constructed the relevant managers (game_map, economy,
    walker_manager) — this function mutates them in place.

    Behaviour:
      * Terrain overrides + features + feature reserves replace the
        procedural defaults.
      * Buildings are placed via ``place_building(..., bypass_validation=True)``
        so a saved layout that overlaps the new procedural terrain
        won't be rejected (matches saveload's behaviour).
      * Economy resources are replaced with the budget values; the
        treasury and population follow.
      * Calendar is reset to the saved year/month.
      * Walker manager is cleared; soldiers respawn from their
        garrisons within a few ticks (legacy behaviour, identical
        to saveload).
    """
    path = os.fspath(path)
    if not os.path.exists(path):
        log.warning("Map file not found: %s", path)
        return False
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("Failed to read map file %s: %s", path, e)
        return False

    version = data.get("version", 1)
    if version > CURRENT_MAP_VERSION:
        log.error(
            "Map %s is version %d but engine supports up to %d.",
            path, version, CURRENT_MAP_VERSION,
        )
        return False
    if version < CURRENT_MAP_VERSION:
        try:
            data = _migrate_map(data, version)
        except Exception:
            log.exception("Map migration failed (from v%d).", version)
            return False

    try:
        # ── World layer ───────────────────────────────────────────
        # v0.26: honor per-map grid dimensions. The editor stamps
        # `{"grid": {"width": W, "height": H}}` into the JSON; on
        # load we rebuild the GameMap at that size BEFORE pouring
        # buildings/terrain into it. Missing key → keep the current
        # GameMap (back-compat with v0.26-and-earlier maps that
        # didn't carry the field).
        grid_spec = data.get("grid")
        if isinstance(grid_spec, dict):
            from constants import (
                GRID_COLS as _DEF_COLS, GRID_ROWS as _DEF_ROWS,
                EDITOR_GRID_MAX as _MAX,
            )
            new_w = int(grid_spec.get("width", _DEF_COLS))
            new_h = int(grid_spec.get("height", _DEF_ROWS))
            # v0.27: upper bound is EDITOR_GRID_MAX (128), not the
            # module-level defaults. The pre-v0.27 ceiling at the
            # defaults was a holdover from when services / walkers
            # iterated the module constants; with the v0.27 refactor
            # the live game_map dimensions drive iteration everywhere,
            # so 128×128 maps round-trip cleanly. Lower bound 8 stays
            # — smaller would be cramped at any zoom level.
            new_w = max(8, min(_MAX, new_w))
            new_h = max(8, min(_MAX, new_h))
            if (new_w, new_h) != (game.game_map.cols, game.game_map.rows):
                _rebuild_game_map_at_size(game, new_h, new_w)
        # Build the dict shape that GameMap.from_dict expects, mapping
        # the editor-facing field names back onto the saveload shape.
        world = {
            "buildings": data.get("buildings", []),
            "terrain_features": data.get("terrain_features", []),
            "terrain_feature_state": data.get("terrain_feature_state", []),
            "terrain_overrides": data.get("terrain", []),
        }
        game.game_map.from_dict(world)

        # ── Economy budget ────────────────────────────────────────
        eco = game.economy
        # Reset every known resource first — otherwise a previously-
        # loaded game's stocks would leak through if the new map's
        # budget doesn't mention that resource.
        for k in list(eco.resources):
            eco.resources[k] = 0
        for k, v in data.get("nutrients", {}).items():
            eco.resources[k] = float(v)
        for k, v in data.get("stocks", {}).items():
            eco.resources[k] = float(v)
        # `food` (legacy) isn't in the editor UI; seed it from the
        # nutrients-bread total so a freshly-loaded map doesn't insta-
        # famine the population.
        if eco.resources.get("food", 0) == 0:
            eco.resources["food"] = max(0.0, eco.resources.get("bread", 0))

        eco.treasury = float(data.get("treasury", 0))
        eco.population = int(data.get("population", 0))
        eco.tick_count = 0
        eco.income_per_tick = 0.0
        eco.expenses_per_tick = 0.0
        eco.food_balance = 0.0
        eco.fed_fraction = 1.0
        eco.nutrient_diversity = 0
        eco._tax_mult_avg = 1.0
        eco.tax_rate_index = 0
        # Tax rate index seeds at 0 (lowest); the player can cycle T
        # post-load. We don't carry a tax setting in the map file —
        # it's a player decision, not a scenario property.
        from constants import TAX_RATES
        eco.tax_rate = TAX_RATES[0]

        # ── Calendar ──────────────────────────────────────────────
        cal = data.get("calendar", {})
        game.year = int(cal.get("year", 1))
        game.month = int(cal.get("month", 1))
        game.game_time = 0

        # ── Walkers, transient state ──────────────────────────────
        if hasattr(game, "walker_manager"):
            game.walker_manager.clear()
        if hasattr(game, "reset_transient_state"):
            game.reset_transient_state()
        # The map's recorded scenario id is informational — it lets
        # events.json keyed scenarios resume on the right campaign.
        game.scenario = data.get("scenario", "default")
        # Stash the loaded name so the editor's HUD shows it. We
        # fall back to the file's basename (sans extension) if the
        # map JSON didn't supply a friendly name — matches the
        # pre-refactor behaviour that used the bare filename.
        game.editor_map_name = data.get(
            "name", os.path.splitext(os.path.basename(path))[0],
        )
        # v0.28: apply scheduled events / random-event toggle to the
        # live event manager. Pre-v0.28 maps don't carry these fields;
        # we leave the event manager alone in that case (its defaults
        # already match "no scripted events, random enabled").
        em = getattr(game, "event_manager", None)
        if em is not None:
            sched = data.get("scheduled_events")
            if sched is not None:
                em.scheduled = []
                for entry in sched:
                    em.scheduled.append({
                        "event": entry.get("event"),
                        "trigger": dict(entry.get("trigger", {})),
                        "fired": False,
                    })
            if "disable_random_events" in data:
                em.disable_random_events = bool(data["disable_random_events"])
        # v0.36: trigger wiring (Event editor output). Two-step apply:
        # always stash the raw list on the game object so a save round-
        # trips even if no live manager exists yet (cold load on splash
        # before the simulation spins up), then hand it to the live
        # TriggerManager if present.
        triggers = data.get("triggers", [])
        game.scenario_triggers = list(triggers) if triggers else []
        game.scenario_libraries = dict(data.get("scenario_libraries", {}) or {})
        tm = getattr(game, "trigger_manager", None)
        if tm is not None:
            # Replace the manager's working list rather than rebuilding
            # the manager — keeps any wired callbacks / event_manager
            # reference intact across map loads.
            tm.triggers = []
            for t in (triggers or []):
                t = dict(t)
                t.setdefault("fired", False)
                t.setdefault("once", True)
                tm.triggers.append(t)
            tm.flags = set()
            tm._pending_flags = set()
            tm._pending_cleared_flags = set()
            tm._pending_events = set()

        # v0.38: spawn the scenario's naval roster (the "naval" block).
        # Done here so the conquest scenario's transports/warships/trade
        # ships exist the moment the map loads, rather than relying on a
        # trigger. Guarded: missing block → no ships; missing manager
        # (some test doubles) → skipped.
        navy = data.get("naval") or {}
        wm = getattr(game, "walker_manager", None)
        if navy and wm is not None:
            _spawn_scenario_navy(game, wm, navy)
    except (KeyError, TypeError, ValueError) as e:
        log.error("Map file %s is malformed: %s", path, e)
        return False

    log.info("Map loaded ← %s (v%d)", path, version)
    return True


def _spawn_scenario_navy(game, wm, navy: dict[str, Any]) -> None:  # noqa: ANN001
    """v0.38: launch the ships declared in a scenario's ``naval`` block.

    Schema::

        "naval": {
          "transports": [{"start": [r,c], "landing_goal": [r,c],
                          "troops": int, "unit": "transport_ship"}],
          "warships":   [{"start": [r,c], "patrol_goal": [r,c]}],
          "trade_ships":[{"home": [r,c], "exit_tile": [r,c],
                          "good": "wine"}]
        }

    Transport ``troops`` are materialised as ``Soldier`` instances from
    the unit registry (falling back to a plain light-infantry stat line
    if the registry isn't available) and embarked as cargo — they only
    enter the live walker list when the transport disembarks them on the
    shore, via ``WalkerManager._process_ships``.
    """
    import walkers as _w

    def _mk_soldier(landing):
        # Build a basic legionary for the cargo. We keep this defensive
        # so a scenario can declare transports even on a stripped test
        # game without a full unit registry.
        reg = getattr(game, "unit_registry", None) or getattr(wm, "unit_registry", None)
        udef = None
        if reg is not None:
            try:
                udef = reg.get("light_infantry")
            except Exception:  # noqa: BLE001
                udef = None
        hp = getattr(udef, "hp", 30)
        dmg = getattr(udef, "damage", 8)
        return _w.Soldier(
            landing[0], landing[1], hp, dmg,
            textures=getattr(wm, "textures", None),
            unit_id="light_infantry", category="infantry",
        )

    for t in navy.get("transports", []):
        start = tuple(t.get("start", [0, 0]))
        goal = tuple(t.get("landing_goal", start))
        n = int(t.get("troops", 0))
        cargo = [_mk_soldier(goal) for _ in range(n)]
        wm.spawn_transport_ship(start, goal, cargo)

    for s in navy.get("warships", []):
        start = tuple(s.get("start", [0, 0]))
        goal = s.get("patrol_goal")
        wm.spawn_warship(start, tuple(goal) if goal else None)

    for tr in navy.get("trade_ships", []):
        home = tuple(tr.get("home", [0, 0]))
        exit_tile = tuple(tr.get("exit_tile", home))
        wm.spawn_trade_ship(home, exit_tile, good=tr.get("good", "wine"))

    log.info(
        "Scenario navy spawned: %d transports, %d warships, %d traders",
        len(navy.get("transports", [])),
        len(navy.get("warships", [])),
        len(navy.get("trade_ships", [])),
    )


def _migrate_map(data: dict[str, Any], from_version: int) -> dict[str, Any]:
    """Forward-migrate older map files to ``CURRENT_MAP_VERSION``.

    Currently a no-op (we're at v1). Add branches here when the
    schema changes — same pattern as ``saveload._migrate``.
    """
    return data


def _rebuild_game_map_at_size(game, rows: int, cols: int) -> None:  # noqa: ANN001
    """v0.26: rebuild ``game.game_map`` at a new ``(rows, cols)``.

    The GameMap holds the grid arrays; child managers (RoadNetwork,
    Pathfinder, ServiceMap, Storage, Decay) cache references to it.
    Tearing down GameMap mid-game would leave those caches dangling,
    so we tear it down AND every manager that owns a ref. The set
    of managers that need re-wiring mirrors the editor bootstrap
    path in ``GameWindow._splash_open_map_editor``.

    Existing buildings on the old map are dropped: this is an
    *empty world* resize, executed before ``from_dict`` repopulates
    it from the JSON's building list. Callers should always invoke
    this *before* the ``from_dict`` call so the freshly-sized map
    receives the saved layout.
    """
    from game_map import GameMap
    from road_network import RoadNetwork
    from pathfinding import Pathfinder
    from services import ServiceMap
    from storage import Storage
    from decay import DecayManager

    textures = game.game_map.textures
    game.game_map = GameMap(
        game.registry, textures=textures, rows=rows, cols=cols,
    )
    # Re-wire managers that hold a GameMap reference. Each of them
    # has the same constructor shape used at editor bootstrap; we
    # don't reach into private state here, just rebuild fresh.
    if hasattr(game, "road_network"):
        game.road_network = RoadNetwork(game.game_map, game.registry)
    if hasattr(game, "pathfinder") and hasattr(game, "road_network"):
        game.pathfinder = Pathfinder(game.road_network)
    if hasattr(game, "service_map"):
        game.service_map = ServiceMap(game.game_map, game.registry)
        if hasattr(game, "_wire_service_staffing_gate"):
            game._wire_service_staffing_gate()
    if hasattr(game, "storage"):
        game.storage = Storage(game.game_map, game.registry)
    if hasattr(game, "decay") and hasattr(game, "balance"):
        game.decay = DecayManager(
            game.game_map, game.registry, game.service_map, game.balance,
        )
    # WalkerManager keeps a pathfinder ref — refresh it.
    if hasattr(game, "walker_manager"):
        game.walker_manager.pathfinder = game.pathfinder
        game.walker_manager.service_map = game.service_map
        game.walker_manager.clear()
    log.info("GameMap resized to %d×%d (rows×cols)", rows, cols)
