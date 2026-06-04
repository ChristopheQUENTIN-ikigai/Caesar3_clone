"""Production-chain diagnostics — v0.22.

Pure-logic module: takes the world snapshot (game_map + registry +
economy + walker_manager + service_map + storage + rebellion + decay)
and returns a structured report explaining *why* a given resource
chain is failing. The game window shells out to this module on the
``D`` key and renders the report as a modal panel.

The module knows nothing about arcade or rendering. Every report is
a list of plain dataclasses, so tests can assert on them.

Tracked failure categories — a single chain stage can hit several at
once, in which case all matching causes are reported (the player needs
to see the whole picture, not just the first thing that broke):

* ``MISSING_BUILDING``    no producer of this stage exists at all.
* ``DISABLED``            building is placed but disconnected (no road).
* ``NO_WORKERS``          the building has zero filled worker slots.
* ``WORKER_SHORTAGE``     filled < needed (partial output).
* ``MISSING_INPUTS``      consumed inputs at zero supply.
* ``LOW_INPUTS``          consumed inputs partially supplied.
* ``DEPLETED_FEATURE``    the local tile feature (forest/iron vein /
                          fertile soil) ran out — building can't extract.
* ``STORAGE_FULL``        producer's output good has nowhere to go
                          (all granaries / warehouses for this good full).
* ``DECAY_COLLAPSED``     building was destroyed by neglect since last
                          tick (decay manager reaped it).
* ``SOCIAL_UNREST``       active rebellion / riot pressure suppresses
                          this district's productivity.
* ``NO_TRANSPORT``        producer has no road tile adjacent (delivery
                          walkers can't dispatch).
* ``LOW_MORALE``          (military chain only) garrison morale below
                          the threshold — soldiers won't deploy.

Each report entry includes:

* ``resource``    — the chain output (e.g. "weapons").
* ``stage``       — building id of the stage in question (e.g. "mine").
* ``stage_name``  — human-readable building name from the registry.
* ``cause``       — one of the constants above.
* ``detail``      — short factual sentence ("3/8 workers filled").
* ``fix``         — short suggested fix sentence.

The top-level ``diagnose_chain`` function walks the chain definition
and produces an ordered list of entries, one per failure surfaced.
``diagnose_all`` does the same for every tracked chain — used by the
``D`` panel which lists every broken chain at once.

Why this lives in its own module rather than tacked onto the economy:
the economy already does the per-tick accounting (it sets
``building_status``); diagnostics is a *read-only* analysis layer that
joins those signals with structural data (chain definitions, decay
events, rebellion pressure). Keeping it separate means the player can
hit ``D`` 30 times a second without slowing the tick loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional, TYPE_CHECKING

from building import BuildingRegistry
from building_status import (
    ACTIVE, DISCONNECTED, IDLE, PARTIAL, STARVED, UNSTAFFED,
)

if TYPE_CHECKING:
    from economy import EconomyManager
    from game_map import GameMap

log = logging.getLogger("caesar3.diagnostics")


# ── Cause constants ──────────────────────────────────────────────────────────
MISSING_BUILDING = "missing_building"
DISABLED = "disabled"
NO_WORKERS = "no_workers"
WORKER_SHORTAGE = "worker_shortage"
MISSING_INPUTS = "missing_inputs"
LOW_INPUTS = "low_inputs"
DEPLETED_FEATURE = "depleted_feature"
STORAGE_FULL = "storage_full"
DECAY_COLLAPSED = "decay_collapsed"
SOCIAL_UNREST = "social_unrest"
NO_TRANSPORT = "no_transport"
LOW_MORALE = "low_morale"
NO_OUTPUT = "no_output"


CAUSE_LABELS: dict[str, str] = {
    MISSING_BUILDING: "Missing building",
    DISABLED:         "Building disabled / disconnected",
    NO_WORKERS:       "No workers (labour shortage)",
    WORKER_SHORTAGE:  "Worker shortage",
    MISSING_INPUTS:   "Missing input resources",
    LOW_INPUTS:       "Low input resources",
    DEPLETED_FEATURE: "Local resource depleted",
    STORAGE_FULL:     "Storage full (no place to put output)",
    DECAY_COLLAPSED:  "Building collapsed (decay)",
    SOCIAL_UNREST:    "Social unrest / riots",
    NO_TRANSPORT:     "No transport access",
    LOW_MORALE:       "Low garrison morale",
    NO_OUTPUT:        "No output produced",
}


# ── Chain definitions ────────────────────────────────────────────────────────
# Each chain is an ordered list of stages from raw extraction to final
# good. A stage is (building_id, output_good). The diagnostic walks
# the chain in order so the player sees the *root* cause first
# (e.g. weapons broken because mine has no workers, not because the
# weapon smith has no iron — the smith failure is a *symptom*).
#
# Extending a chain is a one-line addition here; the diagnose_chain
# walker is data-driven.
@dataclass(frozen=True)
class ChainStage:
    building_id: str
    output: str | None  # None means "no produced good" (e.g. barracks)


CHAINS: dict[str, list[ChainStage]] = {
    "wheat": [
        ChainStage("farm", "wheat"),
    ],
    "bread": [
        ChainStage("farm", "wheat"),
        ChainStage("windmill", "flour"),
        ChainStage("bakery", "bread"),
    ],
    "wood": [
        ChainStage("lumber_mill", "wood"),
    ],
    "planks": [
        ChainStage("lumber_mill", "wood"),
        ChainStage("sawmill", "planks"),
    ],
    "iron": [
        # v0.22: mine extracts ore; the smelter (factory acting as
        # metallurgy) refines into iron bars; weaponsmith forges the
        # weapon. The mine's output good is "iron_ore" in the new
        # chain (bridged with iron for save-compat — see
        # constants.IRON_ORE_ALIAS).
        ChainStage("mine", "iron_ore"),
        ChainStage("smelter", "iron"),
    ],
    "weapons": [
        ChainStage("mine", "iron_ore"),
        ChainStage("smelter", "iron"),
        ChainStage("weapon_smith", "weapons"),
    ],
    "stone_blocks": [
        ChainStage("quarry", "stone"),
        ChainStage("stonemason", "stone_blocks"),
    ],
    "tools": [
        ChainStage("mine", "iron_ore"),
        ChainStage("smelter", "iron"),
        ChainStage("factory", "tools"),
    ],
    "wine": [
        ChainStage("vineyard", "grapes"),
        ChainStage("wine_press", "wine"),
    ],
    "oil": [
        ChainStage("olive_farm", "olives"),
        ChainStage("olive_press", "oil"),
    ],
    "pottery": [
        ChainStage("clay_pit", "clay"),
        ChainStage("pottery_workshop", "pottery"),
    ],
    "soldiers": [
        # Military readiness chain — barracks/fort consumes weapons
        # and produces armed soldiers. The chain walker treats the
        # garrison stage specially: it checks the weapons stockpile
        # and the morale of the resident soldiers.
        ChainStage("mine", "iron_ore"),
        ChainStage("smelter", "iron"),
        ChainStage("weapon_smith", "weapons"),
        ChainStage("barracks", None),
    ],
}


# ── Result types ────────────────────────────────────────────────────────────
@dataclass
class DiagnosticEntry:
    """One root cause for one stage of one chain."""
    resource: str          # chain output, e.g. "weapons"
    stage: str             # building id, e.g. "mine"
    stage_name: str        # display name, e.g. "Iron Mine"
    cause: str             # one of the constants
    detail: str            # short factual sentence
    fix: str               # short suggested fix
    severity: int = 2      # 1 info, 2 warning, 3 critical

    def to_dict(self) -> dict:
        return {
            "resource":   self.resource,
            "stage":      self.stage,
            "stage_name": self.stage_name,
            "cause":      self.cause,
            "cause_label": CAUSE_LABELS.get(self.cause, self.cause),
            "detail":     self.detail,
            "fix":        self.fix,
            "severity":   self.severity,
        }


@dataclass
class ChainReport:
    """All entries for a single chain."""
    resource: str
    entries: list[DiagnosticEntry] = field(default_factory=list)
    chain_ok: bool = True

    @property
    def is_broken(self) -> bool:
        return not self.chain_ok and bool(self.entries)


# ── Diagnostic walker ───────────────────────────────────────────────────────
class ProductionDiagnostics:
    """Pure-logic diagnostic engine.

    Construct once with the world references; call ``diagnose(resource)``
    per chain, or ``diagnose_all()`` for every tracked chain. Both are
    O(buildings × chain length) — cheap enough to call every frame.

    The constructor takes optional kwargs so the engine can pass in
    whatever subsystems exist; missing ones degrade gracefully (no
    rebellion → no SOCIAL_UNREST hits, no decay → no DECAY_COLLAPSED).
    """

    # If the rebellion pressure ratio is above this, the corresponding
    # district is flagged for SOCIAL_UNREST. The whole map shares one
    # rebellion tracker in this engine; if the ratio is high we flag
    # every chain that runs through populated tiles.
    UNREST_RATIO_THRESHOLD = 0.6

    # Below this morale fraction, garrison stages flag LOW_MORALE.
    MORALE_THRESHOLD = 0.4

    def __init__(
        self,
        game_map,           # GameMap
        registry: BuildingRegistry,
        economy,            # EconomyManager
        *,
        walker_manager=None,
        service_map=None,
        storage=None,
        rebellion=None,
        decay=None,
    ):
        self.game_map = game_map
        self.registry = registry
        self.economy = economy
        self.walker_manager = walker_manager
        self.service_map = service_map
        self.storage = storage
        self.rebellion = rebellion
        self.decay = decay

    # ── Entry points ─────────────────────────────────────────────────────
    def diagnose(self, resource: str) -> ChainReport:
        """Walk one chain and report any failures along it."""
        chain = CHAINS.get(resource)
        report = ChainReport(resource=resource)
        if chain is None:
            return report
        chain_ok = True
        for stage in chain:
            entries = self._diagnose_stage(resource, stage)
            if entries:
                report.entries.extend(entries)
                chain_ok = False
        report.chain_ok = chain_ok
        return report

    def diagnose_all(self) -> list[ChainReport]:
        """Diagnose every tracked chain. Returns a list ordered by chain
        id — the panel renders broken chains first."""
        out: list[ChainReport] = []
        for resource in CHAINS.keys():
            out.append(self.diagnose(resource))
        return out

    def broken_chains(self) -> list[ChainReport]:
        """Convenience: only chains with ≥1 entry."""
        return [r for r in self.diagnose_all() if r.is_broken]

    # ── Stage diagnosis ──────────────────────────────────────────────────
    def _diagnose_stage(
        self, resource: str, stage: ChainStage,
    ) -> list[DiagnosticEntry]:
        """Inspect one chain stage. Returns 0..N entries.

        Order of checks matters. We ask cheaper questions first so the
        most informative root cause surfaces, then add supplementary
        observations that make sense given what's already known.
        """
        bid = stage.building_id
        bd = self.registry.get(bid)
        # Building isn't in the registry (e.g. test-time minimal
        # registry, or the player hasn't unlocked the chain yet).
        if bd is None:
            return [DiagnosticEntry(
                resource=resource,
                stage=bid,
                stage_name=bid.replace("_", " ").title(),
                cause=MISSING_BUILDING,
                detail=f"No '{bid}' building defined in this scenario.",
                fix=f"Add a '{bid}' building entry or unlock the relevant tier.",
                severity=2,
            )]

        positions = [
            (r, c) for bt, r, c in self.game_map.get_building_positions()
            if bt == bid
        ]

        # ── Decay collapses ───────────────────────────────────────────
        # The decay manager exposes recently-collapsed buildings via
        # ``recent_collapses`` (tuple list). If any building of this
        # stage type collapsed in the last decay window, surface it
        # — even if a replacement now exists, the player should know.
        collapsed = self._recent_collapses_of(bid)
        for cr, cc in collapsed:
            return_entries: list[DiagnosticEntry] = [DiagnosticEntry(
                resource=resource,
                stage=bid,
                stage_name=bd.name,
                cause=DECAY_COLLAPSED,
                detail=f"A {bd.name} at ({cr},{cc}) collapsed from neglect.",
                fix="Build an Engineer Post within 4 tiles to maintain it.",
                severity=3,
            )]
            if not positions:
                return return_entries  # collapse + no replacement: just one entry

        # ── No building exists ─────────────────────────────────────────
        if not positions:
            return [DiagnosticEntry(
                resource=resource,
                stage=bid,
                stage_name=bd.name,
                cause=MISSING_BUILDING,
                detail=f"No {bd.name} has been built yet.",
                fix=f"Place a {bd.name} from the {bd.category.title()} tab.",
                severity=3,
            )]

        # Inspect each instance and aggregate the worst-state findings.
        entries: list[DiagnosticEntry] = []
        # Track if at least one instance is producing — if so, the chain
        # stage is functionally OK and we suppress lesser warnings.
        any_active = False
        any_partial = False
        worst_status_entries: list[DiagnosticEntry] = []

        for r, c in positions:
            status = self.economy.building_status.get((r, c))
            if status is None:
                # No status yet (first tick after placement) — nothing to
                # report; let the next tick speak.
                continue
            if status.state == ACTIVE:
                any_active = True
                continue
            if status.state == IDLE:
                # Idle building (passive infra like a tower) doesn't fail
                # a production chain.
                continue
            if status.state == PARTIAL:
                any_partial = True
            # All not-active states surface a stage entry.
            stage_entries = self._entries_from_status(
                resource, bd, r, c, status,
            )
            worst_status_entries.extend(stage_entries)

        # If anything is active, treat the chain stage as alive and only
        # surface partial-output as an info-level note.
        # If at least one instance is active AND we found per-instance
        # warnings on the *other* instances, demote those — the player
        # has redundancy. We do NOT early-return here: storage / feature
        # / transport / unrest checks below still need to run, because
        # a producer running fine can still be blocked by a full
        # stockpile or a depleted vein.
        if any_active and worst_status_entries:
            for e in worst_status_entries:
                e.severity = max(1, e.severity - 1)
        entries.extend(worst_status_entries)

        # ── Storage full check (output good) ─────────────────────────
        # Only meaningful if the stage actually produces something AND
        # the building is otherwise running OK — a stuck producer is
        # a real failure mode the basic status doesn't catch.
        if stage.output and (any_active or any_partial):
            full = self._is_output_storage_full(stage.output)
            if full:
                entries.append(DiagnosticEntry(
                    resource=resource,
                    stage=bid,
                    stage_name=bd.name,
                    cause=STORAGE_FULL,
                    detail=(
                        f"Stockpile of {stage.output} is at the storage cap "
                        f"({int(self.economy.resources.get(stage.output, 0))} "
                        f"/ {int(self.economy.storage_capacity)})."
                    ),
                    fix="Build a Warehouse or Granary, or open a trade route to sell.",
                    severity=2,
                ))

        # ── Local feature depletion ────────────────────────────────────
        if bd.needs_feature:
            for r, c in positions:
                if self._feature_depleted(bd, r, c):
                    entries.append(DiagnosticEntry(
                        resource=resource,
                        stage=bid,
                        stage_name=bd.name,
                        cause=DEPLETED_FEATURE,
                        detail=(
                            f"The {bd.name} at ({r},{c}) has exhausted its "
                            f"local {', '.join(bd.needs_feature).replace('_',' ')}."
                        ),
                        fix=f"Demolish and re-place the {bd.name} on a fresh deposit.",
                        severity=2,
                    ))
                    break  # one is enough to convey the pattern

        # ── No road / no transport ─────────────────────────────────────
        for r, c in positions:
            if bd.requires_road:
                # `requires_road` already surfaces as DISCONNECTED in the
                # status above. The transport check here is *separate*:
                # even if the building runs, its goods need a road
                # adjacency to be picked up by a delivery walker.
                if not self._has_adjacent_road(bd, r, c):
                    entries.append(DiagnosticEntry(
                        resource=resource,
                        stage=bid,
                        stage_name=bd.name,
                        cause=NO_TRANSPORT,
                        detail=(
                            f"The {bd.name} at ({r},{c}) has no adjacent road "
                            f"— delivery walkers can't load."
                        ),
                        fix="Place a road tile next to the building's edge.",
                        severity=2,
                    ))
                    break

        # ── Social unrest ──────────────────────────────────────────────
        if self._is_in_unrest():
            entries.append(DiagnosticEntry(
                resource=resource,
                stage=bid,
                stage_name=bd.name,
                cause=SOCIAL_UNREST,
                detail=(
                    "Active rebellion pressure is suppressing this district's "
                    "productivity."
                ),
                fix="Restore food coverage and happiness; suppress rebels.",
                severity=2,
            ))

        # ── Garrison-specific (military chain) ─────────────────────────
        if bid in ("barracks", "fort"):
            # Weapons stockpile gating the soldier "production".
            # We don't model soldier units as a goods flow in the
            # economy — instead the military system spawns them
            # only when the garrison consumed at least one weapon
            # last tick (see walkers.WalkerManager._dispatch_soldiers).
            wstock = self.economy.resources.get("weapons", 0)
            if wstock < 1:
                entries.append(DiagnosticEntry(
                    resource=resource,
                    stage=bid,
                    stage_name=bd.name,
                    cause=MISSING_INPUTS,
                    detail="No weapons available to equip soldiers.",
                    fix=(
                        "Build the iron→smelter→weapon-smith chain so the "
                        "barracks can arm new soldiers."
                    ),
                    severity=3,
                ))
            # Morale check.
            morale = self._garrison_morale()
            if morale is not None and morale < self.MORALE_THRESHOLD:
                entries.append(DiagnosticEntry(
                    resource=resource,
                    stage=bid,
                    stage_name=bd.name,
                    cause=LOW_MORALE,
                    detail=(
                        f"Garrison morale at {int(morale*100)}% (threshold "
                        f"{int(self.MORALE_THRESHOLD*100)}%)."
                    ),
                    fix=(
                        "Win a battle, or stop sending unarmed soldiers to "
                        "their deaths."
                    ),
                    severity=2,
                ))

        return entries

    # ── Helpers ─────────────────────────────────────────────────────────
    def _entries_from_status(
        self, resource: str, bd, row: int, col: int, status,
    ) -> list[DiagnosticEntry]:
        """Convert a BuildingStatus into one or more DiagnosticEntries."""
        out: list[DiagnosticEntry] = []
        if status.state == DISCONNECTED:
            out.append(DiagnosticEntry(
                resource=resource,
                stage=bd.id,
                stage_name=bd.name,
                cause=DISABLED,
                detail=(
                    f"The {bd.name} at ({row},{col}) is disconnected — "
                    f"no road link to the city."
                ),
                fix="Connect the building to the road network.",
                severity=3,
            ))
            return out
        if status.state == UNSTAFFED:
            out.append(DiagnosticEntry(
                resource=resource,
                stage=bd.id,
                stage_name=bd.name,
                cause=NO_WORKERS,
                detail=(
                    f"The {bd.name} at ({row},{col}) has 0/{status.workers_needed} "
                    f"workers."
                ),
                fix=(
                    "Grow your population, raise happiness so unemployed "
                    "citizens take the jobs, or pay wages (treasury check)."
                ),
                severity=3,
            ))
            return out
        if status.state == STARVED:
            missing = ", ".join(status.missing_inputs) or "inputs"
            out.append(DiagnosticEntry(
                resource=resource,
                stage=bd.id,
                stage_name=bd.name,
                cause=MISSING_INPUTS,
                detail=(
                    f"The {bd.name} at ({row},{col}) has no {missing} "
                    f"to consume."
                ),
                fix=(
                    f"Increase production of {missing}, or build a delivery "
                    f"route from a producer."
                ),
                severity=3,
            ))
            return out
        if status.state == PARTIAL:
            # Partial can be due to workers AND/OR inputs — surface both
            # if both are throttled.
            if (
                status.workers_needed > 0
                and status.workers_filled < status.workers_needed
            ):
                out.append(DiagnosticEntry(
                    resource=resource,
                    stage=bd.id,
                    stage_name=bd.name,
                    cause=WORKER_SHORTAGE,
                    detail=(
                        f"The {bd.name} at ({row},{col}) has "
                        f"{status.workers_filled}/{status.workers_needed} "
                        f"workers."
                    ),
                    fix="Grow population or improve happiness to fill jobs.",
                    severity=2,
                ))
            if status.missing_inputs:
                missing = ", ".join(status.missing_inputs)
                out.append(DiagnosticEntry(
                    resource=resource,
                    stage=bd.id,
                    stage_name=bd.name,
                    cause=LOW_INPUTS,
                    detail=(
                        f"The {bd.name} at ({row},{col}) is short on {missing}."
                    ),
                    fix=f"Boost {missing} production or delivery throughput.",
                    severity=2,
                ))
        return out

    def _recent_collapses_of(self, bid: str) -> list[tuple[int, int]]:
        if self.decay is None:
            return []
        out: list[tuple[int, int]] = []
        recent = getattr(self.decay, "recent_collapses", None)
        if recent is None:
            return out
        for entry in recent:
            try:
                cbid, cr, cc = entry
            except (ValueError, TypeError):
                continue
            if cbid == bid:
                out.append((cr, cc))
        return out

    def _is_output_storage_full(self, good: str) -> bool:
        cap = max(1, int(self.economy.storage_capacity))
        cur = self.economy.resources.get(good, 0)
        # 99% counts as full — the last percent is rounding.
        return cur >= cap * 0.99

    def _feature_depleted(self, bd, row: int, col: int) -> bool:
        """Check whether the building's footprint sits on a feature
        that still has reserves. Returns True if the feature is gone.

        The game_map exposes ``extractable_summary`` and
        ``feature_depleted`` (v0.21). We use whichever is available;
        absent both, we conservatively return False (no claim).
        """
        # Preferred: footprint-aware "all depleted" query.
        all_depleted = getattr(
            self.game_map, "all_footprint_features_depleted", None,
        )
        if callable(all_depleted):
            for feature in bd.needs_feature:
                try:
                    if all_depleted(row, col, bd, feature):
                        return True
                except Exception:  # noqa: BLE001
                    continue
            return False
        # Fallback: per-tile probe.
        tap = getattr(self.game_map, "is_feature_depleted", None)
        if callable(tap):
            try:
                return bool(tap(row, col))
            except Exception:  # noqa: BLE001
                return False
        return False

    def _has_adjacent_road(self, bd, row: int, col: int) -> bool:
        rn = getattr(self.game_map, "road_network", None)
        # The walker manager's pathfinder also has a road network.
        if rn is None and self.walker_manager is not None:
            pf = getattr(self.walker_manager, "pathfinder", None)
            if pf is not None:
                rn = getattr(pf, "road_network", None)
        if rn is None:
            return True   # Can't prove otherwise.
        for dr in range(bd.height):
            for dc in range(bd.width):
                rr, cc = row + dr, col + dc
                for nr, nc in (
                    (rr + 1, cc), (rr - 1, cc), (rr, cc + 1), (rr, cc - 1),
                ):
                    try:
                        if rn.is_road(nr, nc):
                            return True
                    except Exception:  # noqa: BLE001
                        return True
        return False

    def _is_in_unrest(self) -> bool:
        if self.rebellion is None:
            return False
        threshold = getattr(
            self.rebellion.balance, "rebel_pressure_threshold", 1.0,
        )
        if threshold <= 0:
            return False
        return (self.rebellion.pressure / threshold) >= self.UNREST_RATIO_THRESHOLD

    def _garrison_morale(self) -> Optional[float]:
        """Average morale (0..1) across friendly Soldier walkers, or
        None if the engine doesn't track morale yet."""
        if self.walker_manager is None:
            return None
        soldiers = []
        for w in getattr(self.walker_manager, "walkers", []):
            if getattr(w, "role", None) != "soldier":
                continue
            morale = getattr(w, "morale", None)
            if morale is None:
                continue
            soldiers.append(morale)
        if not soldiers:
            return None
        return sum(soldiers) / len(soldiers)
