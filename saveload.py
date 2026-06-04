"""Save / Load — robust JSON-based game state persistence.

Features added in this audit:
  * try/except around JSON load — corrupt save no longer crashes the game (1.4).
  * Version field is checked; older versions go through `_migrate()` (1.5).
  * Transient game state (notifications, paused, hover, tick accumulator,
    derived economy fields) is reset on load (1.3).
  * Walkers are cleared (already done; explicit now via `WalkerManager.clear`).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from constants import SAVES_DIR
from signals import signals

log = logging.getLogger("caesar3.saveload")

CURRENT_VERSION = 14
DEFAULT_SLOT = "quicksave.json"


def _ensure_dir() -> None:
    os.makedirs(SAVES_DIR, exist_ok=True)


def _ambient_trade_routes(game) -> list[dict]:  # noqa: ANN001
    """v0.50: serialise only the *ambient* trade routes — those NOT
    owned by the commercial-road manager. City routes are persisted
    separately under "commercial_roads" so a load restores each route
    exactly once (re-adding city routes to the shared TradeRouteManager
    via CommercialRoadManager.from_dict).

    Falls back to the full route list when there's no commercial-road
    manager (older code paths / tests).
    """
    crm = getattr(game, "commercial_roads", None)
    routes = getattr(game.trade_manager, "routes", [])
    if crm is None:
        return [r.to_dict() for r in routes]
    # Build an identity set of city-owned route objects.
    city_routes = {id(r) for rs in crm.routes.values() for r in rs}
    return [r.to_dict() for r in routes if id(r) not in city_routes]


def save_game(game, slot: str = DEFAULT_SLOT) -> str:  # noqa: ANN001
    """Serialise the full game state to a JSON file. Returns the path."""
    _ensure_dir()
    state = {
        "version": CURRENT_VERSION,
        "economy": game.economy.to_dict(),
        "map": game.game_map.to_dict(),
        # v0.50: split ambient routes from city (commercial-road) routes
        # so a load restores each exactly once. ``_ambient_trade_routes``
        # returns every TradeRoute NOT owned by the commercial-road
        # manager; the city routes live under "commercial_roads".
        "trade_routes": _ambient_trade_routes(game),
        "commercial_roads": (
            game.commercial_roads.to_dict()
            if getattr(game, "commercial_roads", None) is not None
            else {"linked": [], "routes": {}}
        ),
        # v0.51: the per-TRIP voyage layer (city configs, cooldown
        # clocks, in-flight voyages, lifetime tallies). Independent of
        # the per-tick "commercial_roads" routes block above. Older saves
        # with no "voyages" key load to an empty voyage layer.
        "voyages": (
            game.voyage_manager.to_dict()
            if getattr(game, "voyage_manager", None) is not None
            else {}
        ),
        "events": game.event_manager.to_dict(),
        "calendar": {
            "year": game.year, "month": game.month, "game_time": game.game_time,
        },
        "camera": {
            "pos_x": game.world_camera.position[0],
            "pos_y": game.world_camera.position[1],
            "zoom": game.world_camera.zoom,
        },
        "speed": game.speed_multiplier,
        # v0.8 managers. All optional in load (older saves migrate cleanly).
        "diplomacy": game.diplomacy.to_dict(),
        "caesar": game.caesar.to_dict(),
        "rebellion": game.rebellion.to_dict(),
        # v0.38 (audit follow-on): persist the RPG delayed-effect queue
        # and any not-yet-presented requests so a "pay next month"
        # promise — and a request fired but not yet answered at save
        # time — survive a save/load. Tuples become lists in JSON; the
        # loader coerces them back.
        "rpg_delayed_effects": [
            [int(fire_at), dict(effects), int(pop), int(happy)]
            for (fire_at, effects, pop, happy)
            in getattr(game, "_rpg_delayed_effects", [])
        ],
        "rpg_pending_requests": list(
            getattr(game, "_pending_rpg_requests", [])
        ),
    }
    path = os.path.join(SAVES_DIR, slot)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    log.info("Game saved → %s", path)
    signals.emit("game_saved", path)
    return path


def load_game(game, slot: str = DEFAULT_SLOT) -> bool:  # noqa: ANN001
    """Restore game state from a JSON file. Returns False on any failure."""
    path = os.path.join(SAVES_DIR, slot)
    if not os.path.exists(path):
        log.warning("Save file not found: %s", path)
        return False

    # ── Read & parse (corrupt-save resilience) ────────────────────────────
    try:
        with open(path) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("Failed to read save file %s: %s", path, e)
        return False

    # ── Version check & migration ─────────────────────────────────────────
    version = state.get("version", 1)
    if version > CURRENT_VERSION:
        log.error(
            "Save file is version %d but engine supports up to %d. "
            "Upgrade the game or use an older save.",
            version, CURRENT_VERSION,
        )
        return False
    if version < CURRENT_VERSION:
        try:
            state = _migrate(state, version)
        except Exception:
            log.exception("Save migration failed (from v%d).", version)
            return False

    # ── Apply state ───────────────────────────────────────────────────────
    try:
        game.economy.from_dict(state["economy"])
        game.game_map.from_dict(state["map"])
        game.trade_manager.from_list(state.get("trade_routes", []))
        # v0.50: restore the commercial-road manager (links + city
        # routes). Done AFTER trade_manager.from_list — from_list resets
        # the shared route list to the ambient routes, then from_dict
        # re-adds each city route to that same shared manager so the
        # per-tick update drives them again. Defensive: ensure a manager
        # exists (older code paths may not have created one yet).
        if getattr(game, "commercial_roads", None) is None \
                and hasattr(game, "_ensure_commercial_roads"):
            game._ensure_commercial_roads()
        if getattr(game, "commercial_roads", None) is not None:
            game.commercial_roads.from_dict(
                state.get("commercial_roads", {"linked": [], "routes": {}})
            )
        # v0.51: restore the per-TRIP voyage layer. _ensure_commercial_roads
        # (called above when needed) creates the voyage manager alongside
        # the road manager, so it exists here. Older saves with no
        # "voyages" key restore to an empty layer (from_dict tolerates {}).
        if getattr(game, "voyage_manager", None) is not None:
            game.voyage_manager.from_dict(state.get("voyages", {}))
        game.event_manager.from_dict(state.get("events", {}))

        cal = state.get("calendar", {})
        game.year = cal.get("year", 1)
        game.month = cal.get("month", 1)
        game.game_time = cal.get("game_time", 0)

        cam = state.get("camera", {})
        game.world_camera.position = (cam.get("pos_x", 0), cam.get("pos_y", 0))
        game.world_camera.zoom = cam.get("zoom", 1.0)

        game.speed_multiplier = state.get("speed", 1)

        # v0.8 managers. ``getattr`` guards because some test doubles in
        # the old fixture didn't have these attributes — the migration
        # has already populated default dicts in ``state``, so the calls
        # here are unconditional once the manager exists on the game.
        if hasattr(game, "diplomacy"):
            game.diplomacy.from_dict(state.get("diplomacy", {}))
        if hasattr(game, "caesar"):
            game.caesar.from_dict(state.get("caesar", {}))
        if hasattr(game, "rebellion"):
            game.rebellion.from_dict(state.get("rebellion", {}))

        # v0.38: restore the RPG delayed-effect queue + pending requests.
        # Coerce the JSON lists back into the (int, dict, int, int) tuple
        # shape the runtime expects. Guarded so older saves (pre-v14,
        # migrated to have empty defaults) and test doubles without the
        # attribute both load cleanly.
        if hasattr(game, "_rpg_delayed_effects"):
            game._rpg_delayed_effects = [
                (int(e[0]), dict(e[1]), int(e[2]), int(e[3]))
                for e in state.get("rpg_delayed_effects", [])
                if isinstance(e, (list, tuple)) and len(e) == 4
            ]
        if hasattr(game, "_pending_rpg_requests"):
            game._pending_rpg_requests = list(
                state.get("rpg_pending_requests", [])
            )
    except (KeyError, TypeError, ValueError) as e:
        log.error("Save file %s is missing required fields: %s", path, e)
        return False

    # ── Reset transient state (1.3) ───────────────────────────────────────
    game.walker_manager.clear()
    if hasattr(game, "reset_transient_state"):
        game.reset_transient_state()

    log.info("Game loaded ← %s (v%d)", path, version)
    signals.emit("game_loaded")
    return True


def _migrate(state: dict[str, Any], from_version: int) -> dict[str, Any]:
    """Forward-migrate older save formats to CURRENT_VERSION.

    Each step is a simple in-place tweak. Add new branches here when
    introducing breaking save changes.
    """
    if from_version < 2:
        # v1 → v2: no real changes, version field added.
        state["version"] = 2
        from_version = 2
    if from_version < 3:
        # v2 → v3: events block grew current_event_name and display_timer.
        ev = state.setdefault("events", {})
        ev.setdefault("current_event_name", None)
        ev.setdefault("display_timer", 0)
        state["version"] = 3
        from_version = 3
    if from_version < 4:
        # v3 → v4: each building entry gained an optional "state" dict
        # (used for house tier/streak). Default to empty dict — pre-v4
        # houses start at tier 0 and re-evolve naturally.
        m = state.setdefault("map", {})
        for b in m.get("buildings", []):
            b.setdefault("state", {})
        state["version"] = 4
        from_version = 4
    if from_version < 5:
        # v4 → v5: no schema change. New buildings (olive_farm, olive_press,
        # vineyard, wine_press, clay_pit, pottery_workshop, weapon_smith,
        # granary, senate) added to data/buildings.json — old saves don't
        # contain them, which is fine. New resources (olives/oil/grapes/
        # wine/clay/pottery/weapons) start at 0 in `economy.from_dict`,
        # which already handles unknown keys gracefully via .get(). House
        # supply memory is per-runtime and intentionally not persisted —
        # it'll repopulate within ~30 ticks of load.
        state["version"] = 5
        from_version = 5
    if from_version < 6:
        # v5 → v6: no schema change. New buildings (reservoir, school,
        # library, clinic, hospital, prefecture, engineer_post, barracks,
        # fort, tower) — old saves simply lack them, which is fine.
        # Soldier / enemy walker state is not persisted (it's runtime
        # combat scratch — soldiers respawn from their garrisons within
        # SOLDIER_SPAWN_INTERVAL ticks of load, and any in-flight raid
        # is intentionally cancelled on load).
        state["version"] = 6
        from_version = 6
    if from_version < 7:
        # v6 → v7: three new manager blocks (diplomacy, caesar, rebellion).
        # Default to empty dicts; each manager's ``from_dict`` falls back
        # to its construction defaults for missing keys (start_diplomacy,
        # no in-flight Caesar request, zero rebellion pressure). This
        # means a v6 save loaded under v7 effectively starts the new
        # mechanics from scratch, which is the intended behaviour — the
        # alternative ("Caesar already wants tribute on load") would be
        # a worse player experience.
        state.setdefault("diplomacy", {})
        state.setdefault("caesar", {})
        state.setdefault("rebellion", {})
        state["version"] = 7
        from_version = 7
    if from_version < 8:
        # v7 → v8: building condition (decay) is stored *inside* the
        # existing per-building `state` dict, not as its own top-level
        # block. So there's nothing to migrate at the file level —
        # buildings without a `condition` key implicitly start at 100,
        # which is what `DecayManager.tick` does via setdefault. We bump
        # the version anyway so a forward-incompatible v9 change has a
        # clean breakpoint to migrate from.
        state["version"] = 8
        from_version = 8
    if from_version < 9:
        # v8 → v9: supply chain rework. Three things to handle:
        # 1. The "workshop" building was renamed to "lumber_mill" (it
        #    was always a wood extractor; calling it a workshop suggested
        #    refining that didn't happen). The registry keeps a
        #    "workshop" alias so old saves still resolve, but we
        #    *prefer* the canonical id — rewrite the saved building
        #    type so the inspector and saveload tests both reflect the
        #    canonical name.
        # 2. New resources (wheat, flour, planks) didn't exist in v8.
        #    economy.from_dict already tolerates missing resource keys
        #    (the constructor seeds them with the start_* defaults), so
        #    nothing to do at the file level.
        # 3. Per-warehouse `accepts` filter is new. Buildings without
        #    an `accepts` entry implicitly accept all goods, which is
        #    the desired default behaviour. No migration needed.
        m = state.setdefault("map", {})
        for b in m.get("buildings", []):
            if b.get("type") == "workshop":
                b["type"] = "lumber_mill"
        state["version"] = 9
        from_version = 9
    if from_version < 10:
        # v9 → v10: terrain features layer + new construction resources
        # (stone, stone_blocks). Three things to handle:
        # 1. Pre-v10 saves don't have a terrain_features key on the map
        #    block. The map's __init__ runs the procedural generator
        #    before from_dict; from_dict only overrides if the key is
        #    present. So absence is correctly handled — old saves keep
        #    the procedural feature layout the engine generates.
        # 2. New resources (stone, stone_blocks). economy.from_dict
        #    tolerates missing keys; the constructor seeds them.
        # 3. wage_payment_ratio is a per-tick scratch value, not
        #    persisted. On load, defaults to 1.0 (full payment) until
        #    the next wage tick computes it — same pattern as
        #    food_balance and fed_fraction.
        state["version"] = 10
        from_version = 10
    if from_version < 11:
        # v10 → v11: terrain feature depletion (reserves per tile) +
        # population food de-duplication. Two things to handle:
        # 1. Pre-v11 saves have no terrain_feature_state. GameMap's
        #    from_dict re-stamps default reserves when the key is
        #    absent — old saves get depletion enabled going forward,
        #    starting from the default reserves. They never depleted
        #    anything before, so this is a clean entry point with
        #    zero gameplay history loss.
        # 2. Pre-v11 saves may carry the old `house.consumption.food=2`
        #    in their building snapshots — but houses don't actually
        #    persist their consumption (it's read live from the
        #    registry, not saved per-building). So no migration is
        #    needed at the save level; the registry change in
        #    `data/buildings.json` automatically applies on load.
        state["version"] = 11
        from_version = 11
    if from_version < 12:
        # v11 → v12: nutrient layer added. Three things to handle:
        # 1. The `bread` resource didn't exist; it'll seed at 0 via
        #    economy.from_dict's resource-dict copy (which only sets
        #    keys present in the saved dict — the constructor already
        #    seeded `bread` with start_bread=30, then from_dict
        #    overwrites with the saved snapshot, leaving bread out
        #    of the snapshot means it falls back to 0). That's the
        #    right behaviour: an old save's "food" stockpile remains;
        #    the player just needs to build a bakery to start
        #    producing the new staple.
        # 2. Nine new nutrients (vegetables, fruits, meat, fish,
        #    cheese, oil, honey, spice — wine pre-existed) likewise
        #    seed at 0. The diversity bonus is reachable as soon as
        #    the player builds the corresponding chain.
        # 3. The bakery's output renamed from `food` to `bread`. Saves
        #    don't persist per-building outputs (they're read from
        #    the registry), so no migration needed at the save level.
        # 4. New buildings (vegetable_farm, orchard, …, stable) —
        #    saves don't list them; they'll just be absent from the
        #    saved building positions and the player can place them.
        state["version"] = 12
        from_version = 12
    if from_version < 13:
        # v12 → v13: military / production chain rework.
        # 1. The mine's output good was renamed: pre-v13 saves carry
        #    "iron" as the mine's production; the v13 mine produces
        #    "iron_ore" and a new smelter refines it into iron. Saves
        #    don't persist per-building production dicts (they're read
        #    from the registry on load), so no per-building rewrite is
        #    needed at the file level — the registry change applies
        #    automatically.
        # 2. New resources: "iron_ore" + "weapons" starter buffer. Old
        #    saves don't have these keys; economy.from_dict tolerates
        #    missing keys via the constructor's seed dict, so they
        #    materialise at the v13 default values on load.
        # 3. Soldier walker state (armed/morale) is not persisted in
        #    this format — soldiers respawn from their garrisons within
        #    SOLDIER_SPAWN_INTERVAL ticks of load (matches the v0.6
        #    save-compat note in the v5→v6 block above). New soldiers
        #    spawn armed if the weapons stockpile permits; otherwise
        #    unarmed. This is the right behaviour: a player loading an
        #    old save shouldn't lose their citizens to a sudden routing
        #    of the loaded-in army.
        # 4. New smelter building doesn't exist on old maps; it's
        #    placeable from v13 onwards in the industry tab.
        state["version"] = 13
        from_version = 13

    if from_version == 13:
        # v13 → v14: the RPG runtime (audit 7.1) added a delayed-effect
        # queue and a pending-request list to the save. Old saves have
        # neither; default both to empty so a "pay next month" promise
        # simply doesn't exist for a save made before the feature.
        state.setdefault("rpg_delayed_effects", [])
        state.setdefault("rpg_pending_requests", [])
        state["version"] = 14
        from_version = 14
    return state


def list_saves() -> list[str]:
    _ensure_dir()
    return [f for f in os.listdir(SAVES_DIR) if f.endswith(".json")]
