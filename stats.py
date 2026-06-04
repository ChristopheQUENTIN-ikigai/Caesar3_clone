"""City statistics — pure-logic snapshot of the current city.

Reads the registry, game map and economy and produces a structured
report that the HUD can render. No arcade dependency, so the same
calculations are testable headlessly.

Why a separate module rather than methods on EconomyManager?
The economy already does enough work per tick. The stats panel is
read-only, called only when the player presses ``S`` (or once per
second by the audit overlay if enabled later) — paying a few hundred
microseconds at that cadence is fine, but folding it into the per-tick
update path would be wasteful.

Public API:

    snap = build_snapshot(game_map, registry, economy, walker_manager=None)
    snap.buildings_by_id            # {bid: count}
    snap.production_per_tick        # {resource: float}
    snap.consumption_per_tick       # {resource: float}
    snap.jobs_by_category           # {category: jobs_filled}
    snap.jobs_by_role               # {worker_role: jobs_filled}
    snap.production_per_specialist  # {bid: {resource: per-worker-per-tick}}
    snap.consumption_per_inhabitant # {resource: per-citizen-per-tick}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from building import BuildingRegistry
    from economy import EconomyManager
    from game_map import GameMap


# Friendly labels for `worker_role` strings — what the HUD shows.
ROLE_LABELS: dict[str, str] = {
    "worker":  "Workers (farms, industry)",
    "trader":  "Merchants & dockworkers",
    "citizen": "Civic & service staff",
    "soldier": "Soldiers",
}

# Friendly labels for the existing `category` keys. New categories added
# in JSON show up unchanged; we don't need to keep this list synced —
# it's purely for prettier output.
CATEGORY_LABELS: dict[str, str] = {
    "infra":         "Infrastructure",
    "housing":       "Housing",
    "water":         "Water",
    "agriculture":   "Agriculture",
    "mining":        "Mining",
    "industry":      "Industry",
    "commerce":      "Commerce",
    "bank":          "Banking",
    "entertainment": "Entertainment",
    "religion":      "Religion",
    "politic":       "Politics",
    "education":     "Education",
    "health":        "Health",
    "security":      "Civic services",
    "military":      "Military",
}


@dataclass
class CityStats:
    """Snapshot. Mutable so callers can post-process if they want."""
    population: int = 0
    employed: int = 0
    workers_needed: int = 0
    happiness: float = 0.0
    treasury: float = 0.0
    fed_fraction: float = 1.0

    buildings_by_id: dict[str, int] = field(default_factory=dict)
    buildings_by_category: dict[str, int] = field(default_factory=dict)

    # Aggregates: total per-tick at full capacity (effective values would
    # require running a tick — these are *rated*, the player's reference).
    production_per_tick: dict[str, float] = field(default_factory=dict)
    consumption_per_tick: dict[str, float] = field(default_factory=dict)

    # Workforce: jobs *needed*, jobs *filled* (filled assumes pop is
    # distributed proportional to need — it's the same approximation
    # `economy.employed` uses).
    jobs_needed_by_role: dict[str, int] = field(default_factory=dict)
    jobs_filled_by_role: dict[str, int] = field(default_factory=dict)
    jobs_needed_by_category: dict[str, int] = field(default_factory=dict)
    jobs_filled_by_category: dict[str, int] = field(default_factory=dict)

    # Per-specialist: how much one worker at this building produces, per
    # resource. ``factory.tools = 5/10 = 0.5`` → "each factory worker
    # produces half a unit of tools per tick".
    production_per_specialist: dict[str, dict[str, float]] = field(default_factory=dict)

    # Per-citizen demand: read directly from housing buildings.
    consumption_per_inhabitant: dict[str, float] = field(default_factory=dict)

    # v0.9: building decay summary. Counts of buildings whose condition
    # has dropped into the warning / critical bands. Zero in a healthy
    # city; non-zero is an actionable nudge to expand the engineer
    # post network.
    buildings_at_risk: int = 0
    buildings_critical: int = 0


def build_snapshot(
    game_map: "GameMap",
    registry: "BuildingRegistry",
    economy: "EconomyManager",
    walker_manager=None,
    decay_manager=None,
) -> CityStats:
    """Compute the full statistics snapshot.

    ``decay_manager`` is optional (v0.9). When supplied, the snapshot's
    ``buildings_at_risk`` / ``buildings_critical`` fields surface
    actionable maintenance pressure for the HUD.
    """
    snap = CityStats(
        population=economy.population,
        employed=economy.employed,
        happiness=economy.happiness,
        treasury=economy.treasury,
        fed_fraction=getattr(economy, "fed_fraction", 1.0),
    )

    # ── Building counts ──────────────────────────────────────────────────
    for bt, _r, _c in game_map.get_building_positions():
        snap.buildings_by_id[bt] = snap.buildings_by_id.get(bt, 0) + 1
        bd = registry.get(bt)
        if bd is None:
            continue
        snap.buildings_by_category[bd.category] = (
            snap.buildings_by_category.get(bd.category, 0) + 1
        )
        # v0.9: decay band counts.
        if decay_manager is not None:
            cond = decay_manager.condition(_r, _c)
            if cond < 25:
                snap.buildings_critical += 1
            elif cond < 75:
                snap.buildings_at_risk += 1

    # ── Per-citizen demand (housing-keyed consumers) ─────────────────────
    # v0.14: population food consumption (1 food/citizen/tick, accounted
    # for in economy.tick as `food_needed = self.population`) is recorded
    # here as a per-inhabitant consumer so the stats panel doesn't lie.
    # Pre-v0.14, house also declared `consumption: {food: 2}` per housing
    # of 10 = 0.2/citizen/tick — which was a duplicate drain layered on
    # top of the population's 1/citizen/tick. The duplicate was removed
    # from buildings.json; the honest 1.0/citizen/tick is recorded here.
    snap.consumption_per_inhabitant["food"] = 1.0
    for bid, bd in registry.all().items():
        if bd.housing <= 0 or not bd.consumption:
            continue
        for r, amt in bd.consumption.items():
            snap.consumption_per_inhabitant[r] = (
                snap.consumption_per_inhabitant.get(r, 0.0) + amt / bd.housing
            )

    # ── Per-specialist production ────────────────────────────────────────
    for bid, bd in registry.all().items():
        if bd.workers <= 0 or not bd.production:
            continue
        per_worker = {
            r: amt / bd.workers
            for r, amt in bd.production.items()
            if r != "happiness"
        }
        if per_worker:
            snap.production_per_specialist[bid] = per_worker

    # ── Aggregates: prod / cons / jobs ────────────────────────────────────
    # Walk the placed buildings (not the registry) so this reflects the
    # *current city*, not the catalogue.
    snap.workers_needed = 0
    for bt, _r, _c in game_map.get_building_positions():
        bd = registry.get(bt)
        if bd is None:
            continue
        for r, amt in bd.production.items():
            if r == "happiness":
                continue
            snap.production_per_tick[r] = snap.production_per_tick.get(r, 0.0) + amt
        for r, amt in bd.consumption.items():
            snap.consumption_per_tick[r] = snap.consumption_per_tick.get(r, 0.0) + amt
        if bd.workers > 0:
            snap.workers_needed += bd.workers
            snap.jobs_needed_by_role[bd.worker_role] = (
                snap.jobs_needed_by_role.get(bd.worker_role, 0) + bd.workers
            )
            snap.jobs_needed_by_category[bd.category] = (
                snap.jobs_needed_by_category.get(bd.category, 0) + bd.workers
            )

    # v0.14: city-level food consumption from the population itself —
    # `economy.tick` deducts `self.population` food/tick. Without this
    # entry the stats panel's prod/cons/net line for food was a lie
    # (the screenshot bug: HUD said -45/t while real drain was -60/t
    # on a 50-pop city).
    snap.consumption_per_tick["food"] = (
        snap.consumption_per_tick.get("food", 0.0) + snap.population
    )

    # Filled jobs: scale needs by the same ratio `employed/workers_needed`
    # that the economy uses, then split proportionally. If workers_needed
    # is zero, every category is "filled" (because nothing needs filling).
    if snap.workers_needed > 0:
        fill_ratio = min(1.0, snap.employed / snap.workers_needed)
    else:
        fill_ratio = 1.0
    snap.jobs_filled_by_role = {
        k: int(v * fill_ratio) for k, v in snap.jobs_needed_by_role.items()
    }
    snap.jobs_filled_by_category = {
        k: int(v * fill_ratio) for k, v in snap.jobs_needed_by_category.items()
    }

    return snap


# ── Pretty-printing helpers (used by the HUD and by tests) ────────────────
def format_lines(snap: CityStats, registry: "BuildingRegistry") -> list[str]:
    """Format the snapshot as a list of strings for the stats panel."""
    out: list[str] = []
    out.append(
        f"Population {snap.population} · Employed {snap.employed}/{snap.workers_needed}  "
        f"· Happiness {snap.happiness:.0f}%  · Treasury {snap.treasury:.0f} gold"
    )
    out.append(f"Fed fraction: {snap.fed_fraction*100:.0f}%")
    if snap.buildings_at_risk or snap.buildings_critical:
        out.append(
            f"Maintenance: {snap.buildings_at_risk} at risk, "
            f"{snap.buildings_critical} CRITICAL"
        )
    out.append("")
    out.append("BUILDINGS BY CATEGORY")
    for cat in sorted(snap.buildings_by_category):
        label = CATEGORY_LABELS.get(cat, cat.capitalize())
        out.append(f"  {label:24s} x{snap.buildings_by_category[cat]}")
    out.append("")
    out.append("BUILDING COUNTS")
    for bid in sorted(snap.buildings_by_id):
        bd = registry.get(bid)
        name = bd.name if bd is not None else bid
        out.append(f"  {name:22s} x{snap.buildings_by_id[bid]}")
    out.append("")
    out.append("PRODUCTION / CONSUMPTION (rated, per tick)")
    all_res = sorted(set(snap.production_per_tick) | set(snap.consumption_per_tick))
    for r in all_res:
        prod = snap.production_per_tick.get(r, 0)
        cons = snap.consumption_per_tick.get(r, 0)
        net = prod - cons
        sign = "+" if net >= 0 else ""
        out.append(f"  {r:10s}  prod {prod:>5.0f}   cons {cons:>5.0f}   net {sign}{net:.0f}")
    out.append("")
    out.append("PER-CITIZEN DEMAND")
    if snap.consumption_per_inhabitant:
        for r, per in sorted(snap.consumption_per_inhabitant.items()):
            total = per * snap.population
            out.append(f"  {r:10s}  {per:.2f}/citizen/tick  (city: {total:.1f}/tick)")
    else:
        out.append("  (no housing-keyed consumers placed)")
    out.append("")
    out.append("PRODUCTION PER SPECIALIST (one worker, full output)")
    for bid in sorted(snap.production_per_specialist):
        bd = registry.get(bid)
        name = bd.name if bd is not None else bid
        per = snap.production_per_specialist[bid]
        per_str = ", ".join(f"{r}={v:.2f}" for r, v in per.items())
        out.append(f"  {name:22s}  {per_str}")
    out.append("")
    out.append("JOBS BY ROLE (filled / needed)")
    total_filled = sum(snap.jobs_filled_by_role.values())
    for role in sorted(snap.jobs_needed_by_role):
        label = ROLE_LABELS.get(role, role.capitalize())
        filled = snap.jobs_filled_by_role.get(role, 0)
        needed = snap.jobs_needed_by_role[role]
        pct = (filled / total_filled * 100) if total_filled > 0 else 0
        out.append(f"  {label:30s}  {filled:>4d} / {needed:<4d}  ({pct:.0f}% of workforce)")
    out.append("")
    out.append("JOBS BY CATEGORY (filled / needed)")
    for cat in sorted(snap.jobs_needed_by_category):
        label = CATEGORY_LABELS.get(cat, cat.capitalize())
        filled = snap.jobs_filled_by_category.get(cat, 0)
        needed = snap.jobs_needed_by_category[cat]
        pct = (filled / total_filled * 100) if total_filled > 0 else 0
        out.append(f"  {label:24s}  {filled:>4d} / {needed:<4d}  ({pct:.0f}% of workforce)")
    return out
