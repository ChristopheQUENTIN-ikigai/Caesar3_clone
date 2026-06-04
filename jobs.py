"""Jobs / employment statistics — v0.17.

A read-only synthesis of ``GameMap`` + ``BuildingRegistry`` +
``EconomyManager.building_status`` that answers the questions a player
asks when their economy is wobbling:

* Where are my workers actually working?
* Which buildings can't fill their slots?
* How does my city's labour pool break down by role (worker / trader /
  citizen / soldier)?
* What's my net workforce headroom — am I about to starve a chain
  because the next building can't hire?

Pure logic — no arcade imports — so the math is testable without a GL
context. The game window's ``J`` keypress opens a panel that renders
this module's output; tests in ``test_jobs.py`` exercise the data
shape directly.

Public API:

    snapshot_jobs(game_map, registry, building_status, population) -> JobsSnapshot

``JobsSnapshot`` is a dataclass with three buckets:

* ``totals`` — city-wide aggregates (pool, employed, demand, jobless,
  shortfall) and per-role distributions.
* ``per_building`` — one row per placed building with workers field
  values.
* ``per_role`` — `{role: (filled, needed)}` for the four roles.

The panel renders the totals as headline numbers, the per-role table as
a small grid, and the per-building list as a scrollable table. The
"jobless / missing workers" overlay reads ``per_building`` entries
where ``workers_needed > workers_filled`` and shades those tiles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from building import BuildingRegistry
    from game_map import GameMap
    from building_status import BuildingStatus


# Worker roles the game knows about. Order is HUD-display order (worker
# is the bulk of the labour, traders are commerce, citizens are
# unemployable extras, soldiers stand apart).
WORKER_ROLES: tuple[str, ...] = ("worker", "trader", "soldier", "citizen")


@dataclass
class BuildingJobRow:
    """One per placed building. The panel renders these as table rows
    ordered by category, then shortfall, then name."""
    bid: str
    name: str
    row: int
    col: int
    role: str            # worker / trader / soldier / citizen
    workers_filled: int
    workers_needed: int
    state: str           # active / partial / unstaffed / disconnected / …
    reason: str          # human-readable hint for the panel

    @property
    def shortfall(self) -> int:
        """Workers the building wants but isn't getting."""
        return max(0, self.workers_needed - self.workers_filled)

    @property
    def is_short(self) -> bool:
        return self.shortfall > 0


@dataclass
class JobsSnapshot:
    """Aggregated employment view — a one-tick snapshot the panel can
    render and the overlay can shade."""
    population: int = 0
    # Total job slots advertised by buildings (sum of ``workers``).
    total_demand: int = 0
    # Total slots actually filled this tick (sum of ``workers_filled``).
    total_filled: int = 0
    # Citizens not currently employed. ``population - total_filled``
    # clamped at 0 — never negative.
    jobless: int = 0
    # Slots demanded but unfilled. Sum across buildings; never the
    # naive ``total_demand - total_filled`` because a building's
    # filled count is capped by its own slot count, so per-building
    # shortfall is the right measure.
    total_shortfall: int = 0
    # Per-role ``(filled, needed)`` totals.
    per_role: dict[str, tuple[int, int]] = field(default_factory=dict)
    # One row per placed building, ordered by category → shortfall →
    # name (the order the panel displays them).
    per_building: list[BuildingJobRow] = field(default_factory=list)

    @property
    def short_buildings(self) -> list[BuildingJobRow]:
        """Subset of per_building rows that have unfilled slots."""
        return [r for r in self.per_building if r.is_short]


def snapshot_jobs(
    game_map: "GameMap",
    registry: "BuildingRegistry",
    building_status: dict[tuple[int, int], "BuildingStatus"],
    population: int,
) -> JobsSnapshot:
    """Build a ``JobsSnapshot`` from the live game state.

    ``building_status`` is the dict the economy populates each tick
    (``EconomyManager.building_status``). When a building has no
    status entry (e.g. early in the first tick before ``update`` has
    run, or for an idle pseudo-building like a road), we fall back to
    treating it as zero-filled / zero-needed so it doesn't pollute
    the snapshot.

    The function is pure: it doesn't mutate any of its inputs. Callers
    can call it as often as they like; cost is O(buildings).
    """
    snap = JobsSnapshot(population=population)
    per_role: dict[str, list[int]] = {role: [0, 0] for role in WORKER_ROLES}
    rows: list[BuildingJobRow] = []

    for bt, r, c in game_map.get_building_positions():
        bd = registry.get(bt)
        if bd is None or bd.workers <= 0:
            # Roads, wells, idle decorative tiles — no slot to track.
            continue
        status = building_status.get((r, c))
        if status is not None:
            wf = int(status.workers_filled)
            wn = int(status.workers_needed)
            state = status.state
            reason = status.reason
        else:
            # No status yet — treat as zero filled. The shortfall will
            # surface once the economy ticks.
            wf = 0
            wn = int(bd.workers)
            state = "idle"
            reason = ""
        # Normalise the role to one of the four canonical buckets.
        role = bd.worker_role if bd.worker_role in per_role else "citizen"
        per_role[role][0] += wf
        per_role[role][1] += wn
        snap.total_filled += wf
        snap.total_demand += wn
        snap.total_shortfall += max(0, wn - wf)
        rows.append(BuildingJobRow(
            bid=bt, name=bd.name, row=r, col=c, role=role,
            workers_filled=wf, workers_needed=wn,
            state=state, reason=reason,
        ))

    snap.per_role = {role: (vals[0], vals[1]) for role, vals in per_role.items()}
    snap.jobless = max(0, population - snap.total_filled)
    # Sort: short buildings first (so the panel surfaces problems),
    # then by category for grouping, then by name for stability.
    rows.sort(key=lambda r: (
        0 if r.is_short else 1,
        registry[r.bid].category if r.bid in registry else "z",
        r.name,
    ))
    snap.per_building = rows
    return snap


def jobless_heatmap_value(
    snap: JobsSnapshot, row: int, col: int,
) -> float:
    """Return a 0..1 "missing workers intensity" for the given tile.

    Used by the overlay system. Walks the snapshot's per-building rows,
    finds the building whose footprint covers ``(row, col)``, and
    returns ``shortfall / max(needed, 1)``. A tile not under a
    worker-using building returns 0.
    """
    for entry in snap.per_building:
        # We only know the origin (entry.row, entry.col) here — not
        # the footprint — so the overlay caller does footprint
        # expansion via the GameMap directly. This helper is the
        # single-cell origin-only version, used by tests and as a
        # building block for the overlay.
        if entry.row == row and entry.col == col:
            if entry.workers_needed <= 0:
                return 0.0
            return entry.shortfall / entry.workers_needed
    return 0.0


def jobless_overlay_grid(
    snap: JobsSnapshot,
    game_map: "GameMap",
    registry: "BuildingRegistry",
    rows: int,
    cols: int,
) -> list[list[float]]:
    """Expand the per-building shortfall into a row × col grid for
    the overlay renderer. Each tile of a short-staffed building's
    footprint gets the building's shortfall ratio; everything else
    is 0.
    """
    grid = [[0.0 for _ in range(cols)] for _ in range(rows)]
    for entry in snap.per_building:
        if not entry.is_short:
            continue
        bd = registry.get(entry.bid)
        if bd is None or entry.workers_needed <= 0:
            continue
        intensity = entry.shortfall / entry.workers_needed
        for dr in range(bd.height):
            for dc in range(bd.width):
                rr = entry.row + dr
                cc = entry.col + dc
                if 0 <= rr < rows and 0 <= cc < cols:
                    grid[rr][cc] = intensity
    return grid
