"""House evolution: tier up/down based on service coverage and goods supply.

Houses are the only evolvable building in v0.5. Their `tier` (0..tier_max)
is stored in `GameMap.building_state[(row, col)]["tier"]`. Each evolution
check (every `BALANCE.house_evolution_check_interval` ticks) asks two
questions:

1. **Service coverage** — does the ServiceMap report enough water / food /
   religion / entertainment in this tile? (v0.4 logic, unchanged.)

2. **Goods supply** *(v0.5)* — has a delivery walker carrying the required
   good (oil for tier 3, oil+pottery for tier 4) passed within
   `DELIVERY_REACH` of this house *recently*? "Recently" means within the
   last `BALANCE.house_supply_memory_ticks` ticks — long enough that one
   walker can rotate through a block of houses without leaving the
   already-supplied ones to devolve.

Both checks must pass for promotion. Either failing triggers devolution.
The visible effect today is colour-shift / sprite-shift and changed
housing capacity (more residents per house) and tax revenue (richer
houses pay more) — both wired through the economy.

The walker manager is optional. When None (e.g. tests of the evolution
system in isolation), goods requirements are silently skipped — useful
for old tests, dangerous for new ones, so v0.5 tests pass it explicitly.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balance import HOUSE_TIER_GOODS, HOUSE_TIER_REQUIREMENTS, BALANCE, Balance

if TYPE_CHECKING:
    from building import BuildingRegistry
    from game_map import GameMap
    from services import ServiceMap
    from walkers import WalkerManager

log = logging.getLogger("caesar3.evolution")


# Colour by tier — interpolates from a dim "shack" brown to a bright
# "villa" cream. Sprites replace this in v0.6.
TIER_COLORS: dict[int, tuple[int, int, int]] = {
    0: (130, 100, 70),      # shack
    1: (180, 140, 100),     # insula (current "house" colour)
    2: (210, 175, 130),     # simple domus
    3: (230, 200, 160),     # domus
    4: (245, 230, 200),     # villa
}


class HouseEvolution:
    """Evolves houses based on service coverage and goods supply. Pure
    logic, no rendering."""

    def __init__(
        self,
        game_map: "GameMap",
        registry: "BuildingRegistry",
        service_map: "ServiceMap",
        balance: Balance = BALANCE,
        walker_manager: "WalkerManager | None" = None,
    ):
        self.game_map = game_map
        self.registry = registry
        self.service_map = service_map
        self.balance = balance
        self.walker_manager = walker_manager
        # Per-house goods supply memory: {(row, col): {good: tick_last_seen}}.
        # A good is "supplied" if tick_last_seen is within the memory window
        # of the current tick. Cleared along with the house when it's
        # demolished (keyed off building_state, which is similarly keyed).
        self._supply_seen: dict[tuple[int, int], dict[str, int]] = {}

    def record_deliveries(self, current_tick: int) -> None:
        """Pull this tick's fresh deliveries from the walker manager and
        stamp the supply memory. Called by the game loop *before*
        ``check_all`` runs, so promotions immediately see this tick's
        deliveries."""
        if self.walker_manager is None:
            return
        for row, col, good in self.walker_manager.fresh_deliveries():
            self._supply_seen.setdefault((row, col), {})[good] = current_tick

    def check_all(self, tick: int) -> list[tuple[int, int, int, int]]:
        """Run an evolution check on every house. Returns a list of
        `(row, col, old_tier, new_tier)` for any house that changed."""
        changes: list[tuple[int, int, int, int]] = []
        if tick % self.balance.house_evolution_check_interval != 0:
            return changes

        for bt, orow, ocol in self.game_map.get_building_positions():
            bd = self.registry.get(bt)
            if bd is None or not bd.is_evolvable:
                continue
            state = self.game_map.building_state.setdefault(
                (orow, ocol), {"tier": 0, "streak": 0},
            )
            old_tier = state["tier"]
            new_tier = self._evaluate(orow, ocol, bd.tier_max, state, tick)
            if new_tier != old_tier:
                state["tier"] = new_tier
                state["streak"] = 0
                changes.append((orow, ocol, old_tier, new_tier))
                log.info(
                    "House (%d,%d) %s tier %d → %d",
                    orow, ocol,
                    "evolves to" if new_tier > old_tier else "devolves to",
                    old_tier, new_tier,
                )

        # Garbage-collect supply memory for houses that no longer exist.
        # Cheap: only runs once per evolution-check interval.
        self._gc_supply()
        return changes

    def _evaluate(
        self, row: int, col: int, tier_max: int, state: dict, tick: int,
    ) -> int:
        """Decide the new tier for one house. Updates state['streak']."""
        cur = state["tier"]

        # Devolution check first: are current-tier requirements still met?
        cur_reqs = HOUSE_TIER_REQUIREMENTS[cur] if cur < len(HOUSE_TIER_REQUIREMENTS) else {}
        cur_goods = HOUSE_TIER_GOODS[cur] if cur < len(HOUSE_TIER_GOODS) else set()
        if not self._meets(cur_reqs, cur_goods, row, col, tick):
            state["streak"] = 0
            return max(0, cur - 1)

        # Already at max tier? hold.
        if cur >= tier_max:
            state["streak"] = 0
            return cur

        # Promotion check: do we meet next-tier requirements?
        next_reqs = HOUSE_TIER_REQUIREMENTS[cur + 1] if cur + 1 < len(HOUSE_TIER_REQUIREMENTS) else {}
        next_goods = HOUSE_TIER_GOODS[cur + 1] if cur + 1 < len(HOUSE_TIER_GOODS) else set()
        if self._meets(next_reqs, next_goods, row, col, tick):
            state["streak"] = state.get("streak", 0) + 1
            if state["streak"] >= self.balance.house_evolution_streak:
                return cur + 1
        else:
            state["streak"] = 0
        return cur

    def _meets(
        self,
        reqs: dict[str, float],
        goods: set[str],
        row: int,
        col: int,
        tick: int,
    ) -> bool:
        # Service coverage (water/food/religion/entertainment, etc).
        for service, threshold in reqs.items():
            if threshold <= 0:
                continue
            if self.service_map.coverage(service, row, col) < threshold:
                return False
        # Goods supply memory (oil, pottery, etc). Skip if no walker
        # manager — the test fixtures may not need delivery gating.
        if goods and self.walker_manager is not None:
            seen = self._supply_seen.get((row, col), {})
            window = self.balance.house_supply_memory_ticks
            for good in goods:
                last = seen.get(good)
                if last is None or tick - last > window:
                    return False
        return True

    def _gc_supply(self) -> None:
        """Drop supply memory for houses that no longer exist."""
        live = {
            (r, c)
            for bt, r, c in self.game_map.get_building_positions()
            if bt == "house"
        }
        stale = [k for k in self._supply_seen if k not in live]
        for k in stale:
            del self._supply_seen[k]


# ── Helpers used by economy & rendering ──────────────────────────────────

def get_house_tier(game_map: "GameMap", row: int, col: int) -> int:
    """Convenience: tier of the house at (row, col), or 0."""
    state = game_map.building_state.get((row, col))
    return state["tier"] if state else 0


def tier_color(tier: int) -> tuple[int, int, int]:
    return TIER_COLORS.get(tier, TIER_COLORS[0])
