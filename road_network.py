"""Road network connectivity.

Tracks which buildings are connected to the road network. A building is
"on the road network" if any cell of its footprint is orthogonally adjacent
to a road tile (or is itself a road tile).

Subscribes to `building_placed` / `building_removed` and rebuilds when a
road or road-adjacent building changes. The rebuild is O(grid + roads) —
fine for 40×30, fine for 100×100. If we ever need 500×500 we'll switch to
incremental union-find, but premature.

Usage:

    rn = RoadNetwork(game_map)
    if rn.is_connected("farm", row, col):
        # production runs
        ...

API:
    is_connected(building_id, origin_row, origin_col) -> bool
        Returns True if the building's footprint has at least one tile
        adjacent to a road. Buildings with `requires_road=False` are
        always considered connected.

    rebuild() -> None
        Force a full rebuild. Called automatically on relevant signals.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from constants import GRID_COLS, GRID_ROWS
from signals import signals

if TYPE_CHECKING:
    from building import BuildingRegistry
    from game_map import GameMap

log = logging.getLogger("caesar3.roads")


class RoadNetwork:
    def __init__(self, game_map: "GameMap", registry: "BuildingRegistry"):
        self.game_map = game_map
        self.registry = registry
        # Set of (row, col) road tiles.
        self._road_tiles: set[tuple[int, int]] = set()
        # v0.37: parallel uint8 mask consumed by the pathfinder. See
        # the rebuild() docstring for the rationale.
        self._road_mask = None
        self._road_generation: int = 0
        self.rebuild()
        # Hot-reload on placement events.
        signals.connect("building_placed", self._on_building_changed)
        signals.connect("building_removed", self._on_building_changed)
        # v0.25: a road that finishes construction needs to be added
        # to the routable tile set. We treat completion as a placement
        # for rebuild purposes.
        signals.connect("construction_completed", self._on_building_changed)

    def disconnect(self) -> None:
        """Detach from signals (used by tests)."""
        signals.disconnect("building_placed", self._on_building_changed)
        signals.disconnect("building_removed", self._on_building_changed)
        signals.disconnect("construction_completed", self._on_building_changed)

    # ── Signal handlers ───────────────────────────────────────────────────
    def _on_building_changed(self, building_id: str, row: int, col: int) -> None:
        # We only need to rebuild when a road changes. For other buildings
        # the road tile set is unchanged.
        # v0.27: bridges are road-network tiles too, so any bridge
        # placement or completion (a different building id per length /
        # orientation / material) also triggers a rebuild. Cheap to be
        # over-eager here: look up the building and check the flag.
        if building_id == "road":
            self.rebuild()
            return
        bd = self.registry.get(building_id)
        if bd is not None and bd.bridges_water:
            self.rebuild()

    # ── Core ──────────────────────────────────────────────────────────────
    def rebuild(self) -> None:
        """Recompute the road tile set from the current map state.

        v0.26: bound the iteration to the live ``game_map``'s
        per-instance ``rows`` / ``cols`` rather than the module-level
        GRID_ROWS / GRID_COLS. With editor-resizable maps a 12×20
        scenario would otherwise IndexError when the road network
        rebuilds at the old 30×40 bounds.

        v0.27: bridge tiles are road-network tiles. Every cell of a
        bridges_water=True building's footprint is added to
        ``_road_tiles``, so a road butted up against a bridge end
        flows through to the other shore without a special-case in
        the pathfinder.

        v0.37: also publishes ``_road_mask`` — a (rows, cols) uint8
        numpy array, 1 where ``is_road(r, c)`` would return True. The
        pathfinder reads through that mask instead of the
        tuple-keyed set for ~10x faster neighbour expansion. The set
        stays for ``is_road`` (other callers — e.g. road graphics,
        connectivity check) and as the authoritative source on save.
        Both are written from the same loop below so they can't go
        out of sync; if either is missed in a future change, the
        ``_mask_dirty`` flag in the pathfinder forces a rebuild.
        """
        roads: set[tuple[int, int]] = set()
        rows = getattr(self.game_map, "rows", GRID_ROWS)
        cols = getattr(self.game_map, "cols", GRID_COLS)
        # v0.37: parallel numpy mask. Lazy-import numpy so headless
        # tests that don't touch the pathfinder don't pay the import
        # cost — and so a system without numpy still loads roads (the
        # pathfinder is the only consumer of the mask).
        try:
            import numpy as _np
            mask = _np.zeros((rows, cols), dtype=_np.uint8)
        except ImportError:  # pragma: no cover — numpy is a hard dep
            mask = None
        for r in range(rows):
            for c in range(cols):
                cell = self.game_map.grid[r][c]
                if cell is None:
                    continue
                bid, orow, ocol = cell
                if bid == "road":
                    # v0.25: a road that hasn't finished construction
                    # doesn't carry traffic yet — exclude it from the
                    # network. Buildings adjacent to it read as
                    # "unconnected" until the road finishes building.
                    # Default construction_ticks is 0 so this is a
                    # no-op for unmodified data.
                    if self.game_map.is_under_construction(orow, ocol):
                        continue
                    roads.add((r, c))
                    if mask is not None:
                        mask[r, c] = 1
                    continue
                # v0.27: bridges. Every tile inside a finished
                # bridges_water=True building's footprint counts as
                # a road tile. Under-construction bridges are
                # excluded (same rule as roads above).
                bd = self.registry.get(bid)
                if bd is not None and bd.bridges_water:
                    if self.game_map.is_under_construction(orow, ocol):
                        continue
                    roads.add((r, c))
                    if mask is not None:
                        mask[r, c] = 1
        self._road_tiles = roads
        self._road_mask = mask
        # Bump a generation counter so the pathfinder can invalidate
        # any cached per-call structures (precomputed goal distance
        # fields, etc.) without a full reset.
        self._road_generation = getattr(self, "_road_generation", 0) + 1
        log.debug("RoadNetwork rebuilt: %d road tiles", len(roads))

    def is_road(self, row: int, col: int) -> bool:
        return (row, col) in self._road_tiles

    def is_connected(self, building_id: str, origin_row: int, origin_col: int) -> bool:
        """True if this building's footprint touches a road tile.

        Buildings with `requires_road=False` (houses, roads themselves,
        wells, fountains, aqueducts) are always considered connected so
        callers can use this uniformly.
        """
        bd = self.registry.get(building_id)
        if bd is None:
            return False
        if not bd.requires_road:
            return True
        # Check every footprint cell's 4-neighbours for a road tile. The
        # building's own footprint cells are not roads (you can't place
        # on top of a road), so we don't include them.
        for dr in range(bd.height):
            for dc in range(bd.width):
                rr, cc = origin_row + dr, origin_col + dc
                # Cardinal neighbours.
                for nr, nc in ((rr + 1, cc), (rr - 1, cc), (rr, cc + 1), (rr, cc - 1)):
                    if (nr, nc) in self._road_tiles:
                        return True
        return False

    def road_tile_count(self) -> int:
        return len(self._road_tiles)
