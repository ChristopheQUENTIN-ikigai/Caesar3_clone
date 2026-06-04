"""Building registry.

Buildings are defined in `data/buildings.json` and loaded into typed
`Building` dataclasses at startup. Modders add a building by appending an
entry to the JSON file — no engine code edits required.

Schema (one entry per building id). All fields except `name`, `color`, `size`,
`cost` are optional with sensible defaults — that way old `buildings.json`
entries keep working when new fields are added to the engine.

    "farm": {
        "name": "Wheat Farm",
        "color": [200, 180, 60],
        "size": [2, 2],                 // [cols, rows]
        "cost": 80,
        "production": {"food": 15},
        "consumption": {},
        "workers": 6,
        "housing": 0,                   // optional, default 0
        "storage": 0,                   // optional, default 0
        "worker_role": "worker",        // optional: worker | trader | citizen
        "description": "Produces food",
        "needs_terrain": "any",         // optional: any | water (port only)

        // ── v0.4 additions ───────────────────────────────────────────────
        "category": "industry",         // palette grouping (default: "infra")
        "requires_road": true,          // production needs road connection
        "provides_service": "water",    // emits a named service in a radius
        "service_radius": 4,            // tile radius (Chebyshev distance)
        "service_intensity": 1.0,       // strength at the source
        "needs_services": {"water": 1}, // services this building needs
        "tier_max": 4,                  // max evolution tier (houses only)
        "needs_water_source": false     // aqueducts need a river-connected chain
    }
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("caesar3.building")


@dataclass(frozen=True)
class Building:
    """Type-safe building definition. Built from a JSON dict."""
    id: str
    name: str
    color: tuple[int, int, int]
    size: tuple[int, int]                  # (cols, rows)
    cost: int
    production: dict[str, int] = field(default_factory=dict)
    consumption: dict[str, int] = field(default_factory=dict)
    workers: int = 0
    housing: int = 0
    storage: int = 0
    # v0.42: flat gold upkeep deducted from the treasury each tick the
    # building exists (independent of staffing). 0 for almost everything;
    # the naval buildings (ports/harbors/shipyards) cost 1 gold/tick.
    upkeep: int = 0
    worker_role: str = "citizen"
    description: str = ""
    needs_terrain: str = "any"             # "any" or "water"

    # ── v0.4 additions ───────────────────────────────────────────────────
    category: str = "infra"                # palette grouping
    requires_road: bool = False            # production gated by road link
    provides_service: str | None = None    # service name, e.g. "water"
    service_radius: int = 0                # tiles
    service_intensity: float = 0.0         # strength at the source
    needs_services: dict[str, float] = field(default_factory=dict)
    tier_max: int = 0                      # 0 = no evolution; houses use 4
    needs_water_source: bool = False       # aqueducts need a river-connected chain

    # ── v0.13 additions ──────────────────────────────────────────────────
    # Natural-resource gating. ``needs_feature`` is a list — placement
    # is allowed if at least one cell of the footprint has any one of
    # the listed features. Empty list means "no feature required, place
    # anywhere on grass". A list of one is the common case (lumber mill
    # on forest); a list of two lets a building exploit either of two
    # resources (mine on gold OR copper vein).
    needs_feature: list[str] = field(default_factory=list)
    # ``needs_water_adjacent`` requires the building's footprint to
    # touch (Chebyshev distance 1) at least one water tile. The
    # reservoir uses this — it gathers groundwater but must sit beside
    # a lake or river to actually fill. Distinct from needs_terrain
    # ("water" — the building sits IN water, like a port) and
    # needs_water_source (an aqueduct chain, queried at runtime).
    needs_water_adjacent: bool = False
    # Construction materials: a dict of resource id → quantity. The
    # cost is paid from the global resource pool when the building is
    # placed. ``cost`` (dn) is the labour/treasury cost, paid from
    # the treasury; ``material_cost`` is the physical inputs (planks,
    # stone). A building with no material_cost can be built anywhere
    # there's treasury; a building with material_cost: {planks: 4}
    # also needs 4 planks in stockpile or the placement is refused.
    material_cost: dict[str, int] = field(default_factory=dict)
    # ``feature_yield_bonus`` lets a building multiply its production
    # output when it sits on a *bonus* feature (as opposed to a hard
    # gate). Farms use this — they default to producing on plain grass,
    # but a farm on fertile_soil produces 50% more food and wheat. The
    # dict maps feature id → multiplier. The classifier picks the
    # *largest* bonus among features present in the footprint.
    feature_yield_bonus: dict[str, float] = field(default_factory=dict)

    # v0.23.x: military garrisons (barracks, fort) declare which unit
    # type they spawn. Looked up in ``UnitRegistry`` at dispatch time;
    # empty string means "use the per-building default in
    # WalkerManager._resolve_spawn_unit" (barracks → light_infantry,
    # fort → heavy_infantry, tower → scout). Non-military buildings
    # leave this empty.
    spawn_unit: str = ""

    # v0.25: construction time. The number of ticks (~0.5 seconds each
    # at TICKS_PER_SECOND=2) between placement and the building going
    # operational. 0 = instant build (legacy default — preserves
    # save compat and small/cheap buildings like roads). A bakery
    # might take 20 ticks (~10 seconds), a fort 80, a senate 200.
    # During construction the building does not produce, consume,
    # employ workers, or provide services; the renderer shows a
    # progress bar instead of the building art (well — building art
    # tinted, with a small progress strip on top).
    construction_ticks: int = 0

    # v0.27: "placement brush" — a building that's not actually a
    # building, just a stamp pattern the click handler uses to place
    # N copies of another id in a row/column. Used for the road
    # brushes (road_h5, road_h10, road_v5, road_v10). When non-None,
    # the schema is::
    #
    #     {"stamp_id": "road", "axis": "h" | "v", "length": int}
    #
    # The click handler reads this, walks `length` tiles in the
    # given axis, and calls `place_building(stamp_id, …)` per tile.
    # If any tile rejects (collision, out-of-bounds), placement
    # stops and the *partial* row stays placed — same forgiving
    # behaviour as dragging a road in Caesar 3. Cost is the brush's
    # own ``cost`` field (typically `length * road.cost`).
    #
    # The building registry treats brushes as a special category:
    # they appear in the palette like regular buildings (size [1, 1]
    # so the hover preview shows a single highlighted tile at the
    # stamp's starting corner), they ARE registered in the registry,
    # but they are NEVER placed on the grid as themselves —
    # `place_building` refuses them, the editor's "select brush"
    # path skips the grid-placement branch entirely.
    placement_brush: dict[str, Any] = field(default_factory=dict)

    # v0.27: bridges. A building with ``bridges_water=True`` is the
    # opposite of `needs_terrain="water"`: the port sits IN water
    # (a single tile inside its footprint can be water); a bridge
    # MUST sit on water across its ENTIRE footprint. Bridges turn
    # impassable water into walker-passable terrain. The road
    # network treats bridge tiles as road tiles, so a bridge's
    # ends connect to adjacent roads automatically.
    #
    # Two flavours, both encoded purely in data: a wooden bridge
    # (cheap, fast to build, no material cost) and a stone bridge
    # (planks + stone, longer construction). Per-length variants
    # (5- and 10-tile, horizontal and vertical) ship 8 building
    # ids total. The shared engine behaviour comes from this flag,
    # not from per-id hardcoding.
    bridges_water: bool = False

    # v0.39: naval infrastructure.
    #   * ``ship_slots`` — for harbors: how many idle ships this harbor
    #     can berth. 0 for everything that isn't a harbor. The harbor is
    #     a buffer: ships dock here to load/unload goods or troops
    #     between the ship and the city's warehouses/granaries.
    #   * ``ship_kind`` — for shipyards: which ship a yard builds and
    #     launches onto an adjacent water tile. "" for non-shipyards.
    #     Recognised values mirror the naval unit ids: "trade_ship",
    #     "transport_ship", "warship".
    #   * ``port_role`` — "commercial" | "military" | "" — distinguishes
    #     the two harbor/port flavours so the UI and ship routing can
    #     keep trade and troop traffic in separate berths.
    ship_slots: int = 0
    ship_kind: str = ""
    port_role: str = ""

    @property
    def width(self) -> int:
        return self.size[0]

    @property
    def height(self) -> int:
        return self.size[1]

    @property
    def is_evolvable(self) -> bool:
        """True if this building can change tier over time (e.g. houses)."""
        return self.tier_max > 0


class BuildingRegistry:
    """Loads building definitions and exposes them by id."""

    def __init__(self, definitions: dict[str, Building]):
        self._defs = definitions

    # ── Lookups ───────────────────────────────────────────────────────────
    def __contains__(self, building_id: str) -> bool:
        return building_id in self._defs

    def __getitem__(self, building_id: str) -> Building:
        return self._defs[building_id]

    def get(self, building_id: str) -> Building | None:
        return self._defs.get(building_id)

    def all_ids(self) -> list[str]:
        return list(self._defs.keys())

    def all(self) -> dict[str, Building]:
        return dict(self._defs)

    def by_category(self) -> dict[str, list[str]]:
        """Return {category: [building_id, ...]} for HUD palette grouping.

        The 'empty' pseudo-building and back-compat aliases (e.g.
        ``workshop`` → ``lumber_mill``) are excluded so the palette
        doesn't double-list the same building.
        """
        # v0.11: aliases are pairs that point at the same Building name.
        # We hide ids whose canonical building has a different id.
        aliases = {"workshop"}
        out: dict[str, list[str]] = {}
        for bid, bd in self._defs.items():
            if bid == "empty":
                continue
            if bid in aliases:
                continue
            out.setdefault(bd.category, []).append(bid)
        return out

    # ── Construction ──────────────────────────────────────────────────────
    @classmethod
    def from_json_file(cls, path: str | Path) -> "BuildingRegistry":
        path = Path(path)
        with path.open() as f:
            raw = json.load(f)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, dict[str, Any]]) -> "BuildingRegistry":
        defs: dict[str, Building] = {}
        for bid, data in raw.items():
            defs[bid] = cls._build_one(bid, data)
        # v0.11: register back-compat aliases. The "workshop" id was
        # renamed to "lumber_mill" (it never refined anything; it just
        # produced raw wood). Old saves and tests that say "workshop"
        # still resolve to the same Building. Aliases share the alias
        # name as their id so display strings remain consistent for
        # legacy callers.
        if "lumber_mill" in defs and "workshop" not in defs:
            lm = defs["lumber_mill"]
            defs["workshop"] = Building(
                id="workshop",
                name=lm.name,
                color=lm.color,
                size=lm.size,
                cost=lm.cost,
                production=dict(lm.production),
                consumption=dict(lm.consumption),
                workers=lm.workers,
                housing=lm.housing,
                storage=lm.storage,
                worker_role=lm.worker_role,
                description=lm.description,
                needs_terrain=lm.needs_terrain,
                category=lm.category,
                requires_road=lm.requires_road,
                provides_service=lm.provides_service,
                service_radius=lm.service_radius,
                service_intensity=lm.service_intensity,
                needs_services=dict(lm.needs_services),
                tier_max=lm.tier_max,
                needs_water_source=lm.needs_water_source,
            )
        log.info("Loaded %d building definitions", len(defs))
        return cls(defs)

    @staticmethod
    def _build_one(bid: str, data: dict[str, Any]) -> Building:
        try:
            size_raw = data["size"]
            if not isinstance(size_raw, (list, tuple)) or len(size_raw) != 2:
                raise TypeError(f"size must be a 2-element list/tuple, got {size_raw!r}")
            size = (int(size_raw[0]), int(size_raw[1]))
            color_raw = data["color"]
            if not isinstance(color_raw, (list, tuple)) or len(color_raw) != 3:
                raise TypeError(f"color must be a 3-element list/tuple, got {color_raw!r}")
            color = (int(color_raw[0]), int(color_raw[1]), int(color_raw[2]))
            return Building(
                id=bid,
                name=data["name"],
                color=color,
                size=size,
                cost=int(data["cost"]),
                production=dict(data.get("production", {})),
                consumption=dict(data.get("consumption", {})),
                workers=int(data.get("workers", 0)),
                housing=int(data.get("housing", 0)),
                storage=int(data.get("storage", 0)),
                upkeep=int(data.get("upkeep", 0)),
                worker_role=data.get("worker_role", "citizen"),
                description=data.get("description", ""),
                needs_terrain=data.get("needs_terrain", "any"),
                category=data.get("category", "infra"),
                requires_road=bool(data.get("requires_road", False)),
                provides_service=data.get("provides_service"),
                service_radius=int(data.get("service_radius", 0)),
                service_intensity=float(data.get("service_intensity", 0.0)),
                needs_services={
                    k: float(v) for k, v in data.get("needs_services", {}).items()
                },
                tier_max=int(data.get("tier_max", 0)),
                needs_water_source=bool(data.get("needs_water_source", False)),
                needs_feature=list(data.get("needs_feature", [])),
                needs_water_adjacent=bool(data.get("needs_water_adjacent", False)),
                material_cost={
                    k: int(v) for k, v in data.get("material_cost", {}).items()
                },
                feature_yield_bonus={
                    k: float(v) for k, v in data.get("feature_yield_bonus", {}).items()
                },
                spawn_unit=str(data.get("spawn_unit", "")),
                construction_ticks=int(data.get("construction_ticks", 0)),
                placement_brush=dict(data.get("placement_brush", {})),
                bridges_water=bool(data.get("bridges_water", False)),
                ship_slots=int(data.get("ship_slots", 0)),
                ship_kind=str(data.get("ship_kind", "")),
                port_role=str(data.get("port_role", "")),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Bad building definition '{bid}': {e}") from e
