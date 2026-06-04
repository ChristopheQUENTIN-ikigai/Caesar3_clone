"""Generic service-coverage system.

Buildings declare in JSON that they provide a named service (e.g. "water",
"food", "religion") with a radius and intensity. The `ServiceMap` aggregates
coverage per tile so that other systems (house evolution, future events) can
ask "is service X available at (row, col)?"

This intentionally does NOT hardcode any service names. New services come
from JSON only — drop a building entry with `"provides_service": "education"`
and education coverage is now a thing.

Coverage model: each provider deposits its `service_intensity` into every
tile within `service_radius` (Chebyshev distance — square footprint). Tiles
inside multiple providers' radii take the MAX intensity (not sum) so that
adding a second fountain in the same spot doesn't game the system. This is
also how Caesar 3 handled overlap.

API:

    sm = ServiceMap(game_map, registry)
    sm.coverage("water", row, col)  # → 0.0 .. 1.0+ (max provider intensity)
    sm.has("water", row, col, threshold=1.0)  # → bool

The map auto-rebuilds on `building_placed` / `building_removed` for any
service-providing building.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from constants import GRID_COLS, GRID_ROWS
from signals import signals

if TYPE_CHECKING:
    from building import BuildingRegistry
    from game_map import GameMap

log = logging.getLogger("caesar3.services")


class ServiceMap:
    def __init__(self, game_map: "GameMap", registry: "BuildingRegistry"):
        self.game_map = game_map
        self.registry = registry
        # service name -> 2D float grid [row][col]
        self._coverage: dict[str, list[list[float]]] = {}
        # v0.14: per-service multiplicative modifiers driven by timed
        # events. Drought sets `service_modifiers["water"] = 0.4` for
        # the duration of the event; `coverage()` reads it. Default
        # 1.0 (no effect). The event manager mutates this on tick
        # via `set_service_modifier()`. Storing modifiers here rather
        # than on the event manager means the lookup is a single
        # multiplication per call instead of an event-manager round-
        # trip — coverage() is in the per-tick hot path for farm
        # water gating.
        self.service_modifiers: dict[str, float] = {}
        self.rebuild()
        signals.connect("building_placed", self._on_building_changed)
        signals.connect("building_removed", self._on_building_changed)
        # v0.25: construction_completed fires when a building finishes
        # its construction phase. Treat it as a fresh placement for
        # rebuild purposes — a fountain that just finished construction
        # needs to start providing water coverage.
        signals.connect("construction_completed", self._on_building_changed)

    def set_service_modifier(self, service: str, factor: float) -> None:
        """Set or clear (factor=1.0) the multiplier for one service.
        Called once per tick from the game loop, fed by the event
        manager's `current_modifiers()`."""
        if factor == 1.0:
            self.service_modifiers.pop(service, None)
        else:
            self.service_modifiers[service] = factor

    def set_staffing_lookup(self, lookup) -> None:  # noqa: ANN001
        """v0.23: register a callable the next ``rebuild()`` will use
        to gate service coverage on whether each provider is actually
        staffed. The lookup receives ``(row, col)`` and returns
        ``(workers_filled, workers_needed)`` or ``None`` if no status
        is recorded for that tile.

        Pass ``None`` (or just don't call this method) to keep the
        legacy "always-active" semantics; this is what every test in
        the existing suite implicitly relies on.

        Why a setter instead of a constructor argument: callers tend
        to build the ServiceMap eagerly (the v0.4 ``rebuild()`` runs
        before the economy has computed its first ``building_status``
        snapshot). Wiring the staffing callback later, once the
        economy exists, keeps the bootstrap order simple — and lets
        a test or a tool that instantiates a bare ServiceMap stay
        agnostic of the new gate.
        """
        self.staffing = lookup
        # The set provider list is cached as `_coverage`; an updated
        # staffing callable changes which providers count, so the
        # next coverage query needs a fresh rebuild. We trigger one
        # right away so the new lookup is in effect immediately.
        self.rebuild()

    def disconnect(self) -> None:
        signals.disconnect("building_placed", self._on_building_changed)
        signals.disconnect("building_removed", self._on_building_changed)
        signals.disconnect("construction_completed", self._on_building_changed)

    def _on_building_changed(self, building_id: str, row: int, col: int) -> None:
        bd = self.registry.get(building_id)
        if bd is None:
            return
        # Rebuild only if this building is a service provider OR a service
        # consumer's environment changed (we don't track consumers in the
        # coverage map; they read from it). For aqueducts, the chain
        # source-check changes coverage indirectly when any aqueduct/well
        # changes; cheap to just rebuild.
        if bd.provides_service is not None or building_id in ("aqueduct", "reservoir"):
            self.rebuild()

    # ── Build ─────────────────────────────────────────────────────────────
    def rebuild(self) -> None:
        """Recompute every service grid from scratch.

        v0.23: a service-providing building with workers_needed > 0
        but workers_filled == 0 is treated as if it didn't exist —
        coverage from a fully-unstaffed engineer post / prefecture /
        clinic etc. drops out. The hook is the optional ``staffing``
        attribute on this ServiceMap (set by the economy each tick
        via ``set_staffing_lookup``); if the attribute is missing or
        returns ``None`` for a tile, we fall back to the legacy
        always-active behaviour. This keeps the headless tests in
        ``tests/test_decay.py`` and ``tests/test_civic_services.py``
        passing without modification.
        """
        self._coverage.clear()
        # v0.27: read the live grid dimensions from the game_map.
        # Pre-v0.27 the coverage grids were allocated against the
        # module constants GRID_ROWS / GRID_COLS, which silently
        # truncated coverage on a resized editor map (a fountain in
        # a 100-tile-wide map would never deposit water past col 40).
        # The replacement allocates grids at the real size every
        # rebuild, and the bounds checks in `coverage()` /
        # `services_at()` likewise read from the live map.
        gm_rows = getattr(self.game_map, "rows", GRID_ROWS)
        gm_cols = getattr(self.game_map, "cols", GRID_COLS)
        # Collect all providers first (so we know which services exist).
        providers: list[tuple[str, int, int, int, float]] = []
        # v0.23: optional callable returning (workers_filled,
        # workers_needed) for the building at (row, col), or None
        # if no status is recorded.
        staff_lookup = getattr(self, "staffing", None)
        for bt, orow, ocol in self.game_map.get_building_positions():
            bd = self.registry.get(bt)
            if bd is None or bd.provides_service is None:
                continue
            # v0.25: under-construction service providers don't emit
            # coverage. A half-built fountain doesn't carry water; a
            # half-built engineer post doesn't maintain anything.
            # Default construction_ticks=0 means most maps see this
            # branch as a no-op.
            if self.game_map.is_under_construction(orow, ocol):
                continue
            # v0.23: skip fully-unstaffed providers. workers_needed == 0
            # buildings (well, fountain, aqueduct, reservoir) keep
            # working — they don't have a worker slot to fail.
            if bd.workers > 0 and staff_lookup is not None:
                staffing = staff_lookup(orow, ocol)
                if staffing is not None:
                    filled, needed = staffing
                    if needed > 0 and filled <= 0:
                        # Empty engineer post: maintenance coverage
                        # drops to 0 over its radius, which lets
                        # decay tick on the surrounding city. This
                        # is the v0.9 spec the v0.22 release flagged
                        # but didn't fix.
                        continue
            # Aqueducts only carry water if part of a water-sourced chain.
            # We model this as: the aqueduct itself does NOT directly
            # provide coverage to houses (radius 0); it's the fountains
            # that do, and fountains require an adjacent watered aqueduct.
            # That logic lives in `_fountain_active` below, called per-tile.
            providers.append((
                bd.provides_service, orow, ocol, bd.service_radius,
                bd.service_intensity,
            ))

        if not providers:
            return

        # For fountains we need to know whether their adjacent aqueduct
        # network reaches a water tile. Compute the set of "watered"
        # aqueduct origins once.
        watered_aqueducts = self._watered_aqueducts()

        for service, orow, ocol, radius, intensity in providers:
            # Special case for fountains: skip if no watered aqueduct adjacent.
            cell = self.game_map.grid[orow][ocol]
            bid = cell[0] if cell is not None else None
            if bid == "fountain" and not self._fountain_has_water(orow, ocol, watered_aqueducts):
                continue

            grid = self._coverage.setdefault(
                service,
                [[0.0] * gm_cols for _ in range(gm_rows)],
            )
            r0 = max(0, orow - radius)
            r1 = min(gm_rows - 1, orow + radius)
            c0 = max(0, ocol - radius)
            c1 = min(gm_cols - 1, ocol + radius)
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    if intensity > grid[r][c]:
                        grid[r][c] = intensity

        log.debug("ServiceMap rebuilt: %s", {k: "ok" for k in self._coverage})

    # ── Aqueduct chain check ──────────────────────────────────────────────
    def _watered_aqueducts(self) -> set[tuple[int, int]]:
        """Return set of aqueduct (row, col) origins that connect to water.

        BFS from every aqueduct tile that is orthogonally adjacent to a
        water terrain tile *or* to a reservoir building (v0.6 — reservoirs
        act as a virtual water source so a city far from any river can
        still build an aqueduct network).

        v0.27: bounds come from the live game_map, not module constants.
        """
        gm_rows = getattr(self.game_map, "rows", GRID_ROWS)
        gm_cols = getattr(self.game_map, "cols", GRID_COLS)
        # Find all aqueduct origin tiles.
        aqueducts: set[tuple[int, int]] = set()
        # Reservoir-occupied tiles count as water-equivalent for the chain.
        reservoir_tiles: set[tuple[int, int]] = set()
        for r in range(gm_rows):
            for c in range(gm_cols):
                cell = self.game_map.grid[r][c]
                if cell is None:
                    continue
                if cell[0] == "aqueduct":
                    aqueducts.add((r, c))
                elif cell[0] == "reservoir":
                    reservoir_tiles.add((r, c))
        if not aqueducts:
            return set()

        # Sources: aqueducts adjacent to water OR to a reservoir tile.
        from collections import deque
        watered: set[tuple[int, int]] = set()
        queue: deque[tuple[int, int]] = deque()
        for r, c in aqueducts:
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nr, nc = r + dr, c + dc
                if not (0 <= nr < gm_rows and 0 <= nc < gm_cols):
                    continue
                if self.game_map.is_water(nr, nc) or (nr, nc) in reservoir_tiles:
                    watered.add((r, c))
                    queue.append((r, c))
                    break

        # BFS through aqueducts.
        while queue:
            r, c = queue.popleft()
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nr, nc = r + dr, c + dc
                if (nr, nc) in aqueducts and (nr, nc) not in watered:
                    watered.add((nr, nc))
                    queue.append((nr, nc))
        return watered

    def _fountain_has_water(
        self, orow: int, ocol: int, watered: set[tuple[int, int]],
    ) -> bool:
        """Fountain is active if any of:
        * A watered aqueduct sits in its 4-neighbours (the v0.4 rule), OR
        * v0.13: the fountain itself sits on a groundwater spot — a small
          self-sufficient water source for cities that can't run an
          aqueduct chain everywhere.
        """
        # Self-sufficient via groundwater under the fountain's tile.
        if (
            getattr(self.game_map, "terrain_features", None) is not None
            and self.game_map.feature_at(orow, ocol) == "groundwater"
        ):
            return True
        # Original rule: watered aqueduct adjacent.
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if (orow + dr, ocol + dc) in watered:
                return True
        return False

    # ── Query ─────────────────────────────────────────────────────────────
    def coverage(self, service: str, row: int, col: int) -> float:
        grid = self._coverage.get(service)
        if grid is None:
            return 0.0
        # v0.27: bounds read from live game_map. Pre-v0.27 the check
        # was against module constants which broke on resized maps.
        gm_rows = getattr(self.game_map, "rows", GRID_ROWS)
        gm_cols = getattr(self.game_map, "cols", GRID_COLS)
        if not (0 <= row < gm_rows and 0 <= col < gm_cols):
            return 0.0
        # v0.14: apply timed-event modifier (drought etc.). The
        # underlying coverage grid is unchanged; the player sees the
        # dimmed value via `services_at()` for inspector / hover and
        # via this method for farm water_factor / house water service.
        raw = grid[row][col]
        mod = self.service_modifiers.get(service, 1.0)
        return raw * mod

    def has(self, service: str, row: int, col: int, threshold: float = 1.0) -> bool:
        return self.coverage(service, row, col) >= threshold

    def services_at(self, row: int, col: int) -> dict[str, float]:
        """Snapshot of every service's coverage at one tile."""
        gm_rows = getattr(self.game_map, "rows", GRID_ROWS)
        gm_cols = getattr(self.game_map, "cols", GRID_COLS)
        if not (0 <= row < gm_rows and 0 <= col < gm_cols):
            return {}
        out = {s: g[row][col] for s, g in self._coverage.items()}
        # v0.14: dim by active modifiers — same path the per-cell
        # lookup uses, so the inspector's water reading agrees with
        # what the farm sees.
        for s, factor in self.service_modifiers.items():
            if s in out:
                out[s] = out[s] * factor
        return out
