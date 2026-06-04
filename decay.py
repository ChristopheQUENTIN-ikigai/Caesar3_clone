"""Building decay & maintenance.

Every building has a hidden condition score in [0, 100]. Each game
tick, condition drops at a rate scaled by *unmet maintenance need* —
i.e. the gap between full maintenance coverage and the actual coverage
at the building's tile. When condition hits zero the building
collapses: it's removed from the map, the player gets a small partial
refund and a notification, and any state on it (house tier, streak) is
discarded.

Maintenance is provided by the engineer post, which has shipped in the
registry since v0.6 with no gameplay effect. v0.9 finally wires it in:
an engineer post within Chebyshev radius 4 caps decay at zero, a
half-covered tile loses condition at half-rate, and an isolated farm
in the desert decays at full rate.

Design notes — what condition means and why it's hidden by default

* Condition is **soft cosmetic** for buildings well above 50 — the
  player doesn't even see it. We surface it on the inspector below 75
  ("⚠ Needs maintenance") and below 25 ("⚠⚠ NEAR COLLAPSE!").
* The condition number itself is not on the inspector unless the player
  hovers — the decay system should *push behaviour* (building engineer
  posts) rather than dominate the HUD.
* Decay is paused for buildings the player can't reasonably maintain:
  road tiles, empty/grass, and the engineer post itself (it's the
  thing maintaining other things).
* House condition is exempt too. Houses are evolvable; decay would
  collide with the tier system in confusing ways. A future v0.10 could
  fold house wear-and-tear into the existing tier-down logic.

Some tuning is in `balance.py`; modders can flip `decay_enabled` to
`False` to disable the whole layer for sandbox modes.

Public API:

    dm = DecayManager(game_map, registry, service_map, balance)
    collapses = dm.tick(current_tick)   # → list[(building_id, row, col)]
    dm.condition(row, col)              # → int 0..100
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balance import BALANCE, Balance

if TYPE_CHECKING:
    from building import BuildingRegistry
    from game_map import GameMap
    from services import ServiceMap

log = logging.getLogger("caesar3.decay")


# Buildings that don't decay. Keyed by id — matches data/buildings.json.
# Roads are abundant, cheap, and maintaining each road tile would be
# tedious; the engineer post is the maintainer, decaying it would lock
# out the only fix; houses are gated by the tier system.
DECAY_EXEMPT: frozenset[str] = frozenset({
    "empty", "road", "engineer_post", "house",
    # Service infrastructure — degrading these would feel like "your
    # well rotted away" which v0.9 intentionally avoids. v0.10 may
    # revisit if we want infrastructure-aging gameplay.
    "well", "fountain", "aqueduct", "reservoir",
})


class DecayManager:
    # v0.22: a short rolling log of recent decay collapses, consumed by
    # the production-diagnostics module so the player (on D) can see
    # *what just collapsed*. Bounded so it doesn't grow indefinitely.
    RECENT_COLLAPSE_KEEP = 12

    def __init__(
        self,
        game_map: "GameMap",
        registry: "BuildingRegistry",
        service_map: "ServiceMap",
        balance: Balance = BALANCE,
    ):
        self.game_map = game_map
        self.registry = registry
        self.service_map = service_map
        self.balance = balance
        # v0.22: rolling list of (building_id, row, col, tick) for the
        # last few decay collapses. Diagnostics reads this; nothing else
        # mutates it. Cleared by reset().
        self.recent_collapses: list[tuple[str, int, int]] = []
        self._recent_collapse_ticks: list[int] = []

    # ── Read API ─────────────────────────────────────────────────────────
    def condition(self, row: int, col: int) -> int:
        """Current condition (0-100) for the building at (row, col).

        New buildings start at 100 (the building_state init in
        ``DecayManager.tick`` defaults to 100 the first time a building
        is touched). Empty tiles return 100 — "fine" — to keep callers
        from special-casing.
        """
        state = self.game_map.building_state.get((row, col))
        if state is None:
            return 100
        return int(state.get("condition", 100))

    def is_at_risk(self, row: int, col: int, threshold: int = 75) -> bool:
        """True if condition has dropped below the warning threshold.

        Used by the inspector to decide whether to surface a maintenance
        warning line. Empty tiles are never "at risk."
        """
        cond = self.condition(row, col)
        return cond < threshold and self.game_map.grid[row][col] is not None

    # ── Tick ─────────────────────────────────────────────────────────────
    def tick(self, current_tick: int) -> list[tuple[str, int, int]]:
        """Advance decay by one tick. Returns ``[(building_id, row, col), …]``
        for every building that collapsed *this tick*.

        We only check on a configurable interval (default every 5 ticks)
        so the per-tile work doesn't dominate the inner loop. The decay
        per-check is scaled up to compensate so the *rate* is independent
        of the check interval.
        """
        b = self.balance
        if not b.decay_enabled:
            return []
        if current_tick % b.decay_check_interval != 0:
            return []

        # Scale the per-tick rate up to a per-check rate. If we check
        # every 5 ticks, the per-check rate is 5× the per-tick rate.
        per_check_rate = b.decay_rate_per_tick * b.decay_check_interval
        # v0.19.x: engineer-post repair rate. Same cadence as decay,
        # so the per-check amount is the per-tick spec (1%) × interval.
        repair_per_check = (
            getattr(b, "engineer_repair_per_tick", 0.0) * b.decay_check_interval
        )

        collapses: list[tuple[str, int, int]] = []
        for bt, orow, ocol in self.game_map.get_building_positions():
            if bt in DECAY_EXEMPT:
                continue
            bd = self.registry.get(bt)
            if bd is None:
                continue
            state = self.game_map.building_state.setdefault(
                (orow, ocol), {},
            )
            cond = state.setdefault("condition", 100)

            # Maintenance coverage at this tile. Engineer post emits
            # "maintenance" service; coverage 1.0 → no decay; 0.0 →
            # full-rate decay; partial → linear in between.
            cov = self.service_map.coverage("maintenance", orow, ocol)
            cov = max(0.0, min(1.0, cov))
            decay_amt = per_check_rate * (1.0 - cov)
            # v0.19.x: covered buildings ALSO heal up to 100 at the
            # engineer's repair rate, scaled by coverage. The previous
            # behaviour (full coverage = no decay, but no heal either)
            # left a building stuck at whatever condition it dropped to
            # before the engineer post arrived. Now an engineer post
            # actively rebuilds neglected stock — once covered, a 30%
            # building climbs back to 100% over ~70 ticks at 1%/tick.
            repair_amt = repair_per_check * cov
            # v0.29: engineers can't repair a building that's actively
            # on fire. Driving the repair component to zero models the
            # real-world ordering (the fireman extinguishes first, the
            # engineer rebuilds afterwards). Decay still applies — a
            # burning building isn't being maintained either. Combat
            # damage from the fire-propagation pass (in walkers.py
            # _resolve_combat) is independent of this gate and runs
            # every tick on its own clock.
            if state.get("on_fire", False):
                repair_amt = 0.0
            net = repair_amt - decay_amt
            if net > 0:
                cond = min(100, int(cond + net))
                state["condition"] = cond
            elif net < 0:
                cond = max(0, int(cond + net))
                state["condition"] = cond

            if cond <= 0:
                collapses.append((bt, orow, ocol))

        # Apply collapses *after* iterating so we don't mutate the
        # building positions list during iteration.
        for bid, r, c in collapses:
            self.game_map.remove_building(r, c)
            log.warning("Building collapsed (decay): %s at (%d,%d)", bid, r, c)
            # v0.22: stash for the D-key diagnostics panel.
            self.recent_collapses.append((bid, r, c))
            self._recent_collapse_ticks.append(current_tick)
        # Bound the list — keep only the most recent N collapses.
        if len(self.recent_collapses) > self.RECENT_COLLAPSE_KEEP:
            drop = len(self.recent_collapses) - self.RECENT_COLLAPSE_KEEP
            self.recent_collapses = self.recent_collapses[drop:]
            self._recent_collapse_ticks = self._recent_collapse_ticks[drop:]
        return collapses

    # ── HUD helper ───────────────────────────────────────────────────────
    def status_label(self, row: int, col: int) -> tuple[str, str] | None:
        """Returns ``(text, severity)`` for the inspector, or None if the
        building is in fine shape. Severity is one of "warn" | "critical"
        for the HUD's colour decision.
        """
        if self.game_map.grid[row][col] is None:
            return None
        cond = self.condition(row, col)
        if cond < 25:
            return (f"⚠⚠ NEAR COLLAPSE  ({cond}%)", "critical")
        if cond < 75:
            return (f"⚠ Needs maintenance  ({cond}%)", "warn")
        return None
