"""Commercial roads — link foreign cities for inter-city sea commerce.
(v0.50, reworked v0.53)

The player opens the in-game *Commercial roads* window (hotkey ``R``)
to link any of the seven foreign cities in ``constants.TRADE_CITIES``
(Massilia, Roma, Carthage, Athina, Istanbul, Tunis, Alexandria). A link
is a one-off treasury cost that opens the road; once a city is linked,
the player runs **per-trip voyages** to it (see ``voyages.py``).

v0.53 — per-trip only
---------------------

Earlier versions (v0.50/v0.51) also ran *persistent per-tick* trade
routes to a linked city: a steady drip of goods out of the city every
tick. v0.52 stopped those routes paying gold per tick (income moved to
voyages), but they still moved stock for free every tick — a pure value
leak. v0.53 **retires the per-tick route layer entirely**: inter-city
commerce now happens *only per trip*. A city exports goods only when a
free commercial ship sails a voyage and sells the cargo for gold on its
return — never per tick.

This manager is therefore now just the **city-link bookkeeping layer**:
it owns the linked-city set, prices links and (for display) per-unit
profit off ``bartering.STOCK_PRICES``, and persists the link state
across save-load. The route-management methods (``add_route`` /
``remove_route`` / ``routes_for`` / ``total_routes``) survive only as
deprecated no-op stubs so nothing crashes; they create and report no
routes. A planned terrestrial **caravan** layer will be the land
counterpart to voyages — and, like voyages, it will be per-trip, not a
per-tick drip.

Design — why a separate module
-------------------------------

Same split as ``bartering.py`` / ``gold_trade.py``: the *logic* (what a
link costs, how a route's per-unit profit is derived, the save/load
shape) lives here so the UI mixin stays a thin draw + click layer, and
so a modder can rebalance commerce without touching engine or window
code. The window calls into ``CommercialRoadManager``; the manager owns
the city-link set.

Persistence
-----------

``CommercialRoadManager.to_dict`` / ``from_dict`` round-trip the linked
set. An empty ``routes`` object is still emitted for shape stability,
and a legacy ``routes`` block in a pre-v0.53 save is read only to
recover which cities were linked — the routes themselves are dropped.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from constants import (
    TRADE_CITIES,
    TRADE_CITY_DEFAULT_RATE,
    TRADE_CITY_PROFIT_FRACTION,
)
from trade import TradeRoute

if TYPE_CHECKING:
    from economy import EconomyManager
    from trade import TradeRouteManager

log = logging.getLogger("caesar3.commercial_roads")


# City-id → metadata dict, built once from the constants table for O(1)
# lookups. Keys: "name", "link_cost", "profit_mult".
_CITY_BY_ID: dict[str, dict] = {c["id"]: c for c in TRADE_CITIES}


def city_ids() -> list[str]:
    """Return the ordered list of trade-city ids (constants order)."""
    return [c["id"] for c in TRADE_CITIES]


def city_meta(city_id: str) -> dict | None:
    """Return the metadata dict for ``city_id`` or None if unknown."""
    return _CITY_BY_ID.get(city_id)


def city_name(city_id: str) -> str:
    """Human-readable name for a city id; falls back to the id."""
    meta = _CITY_BY_ID.get(city_id)
    return meta["name"] if meta else city_id


def route_profit_per_unit(good: str, city_id: str) -> float:
    """Compute the per-unit profit a route to ``city_id`` earns selling
    ``good``. Reads ``bartering.STOCK_PRICES`` so it tracks the same
    price table the barter / gold-trade panels use.

    Unknown goods price at a floor of 1 so a route is never free money
    nor a crash. Unknown cities use a 1.0 multiplier.
    """
    from bartering import STOCK_PRICES
    base = STOCK_PRICES.get(good, 1)
    meta = _CITY_BY_ID.get(city_id)
    mult = float(meta["profit_mult"]) if meta else 1.0
    return max(1.0, round(base * TRADE_CITY_PROFIT_FRACTION * mult))


class CommercialRoadManager:
    """Owns the set of linked cities. (v0.53: link bookkeeping only.)

    Per-tick routes are retired — the manager no longer ticks anything
    and no longer registers routes with the game's ``TradeRouteManager``.
    Inter-city export is per-trip via ``voyages.py``. The ``routes``
    attribute is kept as an always-empty dict purely so the deprecated
    ``routes_for`` / ``total_routes`` stubs and the persistence shape
    stay well-formed.
    """

    def __init__(self, trade_manager: "TradeRouteManager"):
        self.trade_manager = trade_manager
        # Set of linked city ids.
        self.linked: set[str] = set()
        # Retired (v0.53): formerly city_id → list[TradeRoute] of the
        # per-tick routes. Kept as an always-empty dict so the no-op
        # route stubs and persistence don't special-case its absence.
        self.routes: dict[str, list[TradeRoute]] = {}

    # ── Links ─────────────────────────────────────────────────────────
    def is_linked(self, city_id: str) -> bool:
        return city_id in self.linked

    def link_cost(self, city_id: str) -> int:
        meta = _CITY_BY_ID.get(city_id)
        return int(meta["link_cost"]) if meta else 0

    def link_city(self, economy, city_id: str) -> bool:  # noqa: ANN001
        """Establish a commercial road to ``city_id``, charging the
        link cost to the treasury. Returns True on success, False if the
        city is unknown, already linked, or unaffordable.
        """
        if city_id not in _CITY_BY_ID:
            return False
        if city_id in self.linked:
            return False
        cost = self.link_cost(city_id)
        if economy.treasury < cost:
            return False
        economy.treasury -= cost
        self.linked.add(city_id)
        self.routes.setdefault(city_id, [])
        log.info("Linked city %s for %d dn", city_id, cost)
        return True

    def unlink_city(self, city_id: str) -> bool:
        """Tear down the commercial road to ``city_id``. Returns True if
        the city was linked.

        v0.53: there are no per-tick routes to dismantle anymore. The
        caller (window) separately clears the per-trip voyage config for
        the city; in-flight voyages still complete on return.
        """
        if city_id not in self.linked:
            return False
        self.routes.pop(city_id, None)
        self.linked.discard(city_id)
        log.info("Unlinked city %s", city_id)
        return True

    # ── Routes ────────────────────────────────────────────────────────
    def add_route(
        self, city_id: str, good: str,
        rate: int = TRADE_CITY_DEFAULT_RATE,
    ) -> TradeRoute | None:
        """RETIRED (v0.53). Per-tick stock-moving routes no longer exist.

        Inter-city commerce is now *entirely* per-trip: a city ships
        goods only when a free commercial ship sails a voyage (see
        ``voyages.py``), and gold is exchanged once per completed round
        trip — never per tick. The old per-tick route both dripped gold
        (removed in v0.52) and *moved stock for free every tick* (a value
        leak with no income); v0.53 removes the whole layer so the only
        export path is a trip.

        This method is kept as a deprecated no-op so any lingering
        caller (or a modder's script) doesn't crash — it creates no
        route, registers nothing with the tick loop, and returns None.
        A planned terrestrial *caravan* layer will be its successor, but
        it too will be per-trip (a caravan is a land voyage), not a
        per-tick drip.
        """
        log.warning(
            "add_route is retired (v0.53): inter-city export is per-trip "
            "only — ignoring request to route %s to %s. Use voyages.",
            good, city_id,
        )
        return None

    def remove_route(self, city_id: str, index: int) -> bool:
        """RETIRED (v0.53). No per-tick routes exist to remove. Kept as a
        no-op returning False so old UI/click paths don't crash."""
        return False

    def routes_for(self, city_id: str) -> list[TradeRoute]:
        """RETIRED (v0.53). Always empty — the per-tick route layer is
        gone. Retained so callers that iterate routes get a clean empty
        list instead of an AttributeError."""
        return []

    def total_routes(self) -> int:
        """RETIRED (v0.53). Always 0 — no per-tick routes exist."""
        return 0

    # ── Persistence ───────────────────────────────────────────────────
    def to_dict(self) -> dict:
        """JSON-ready snapshot of the linked-city set.

        v0.53: per-tick routes are retired, so we no longer persist a
        per-city route list. We still emit an empty ``routes`` object so
        the on-disk shape is unchanged for any external reader and a
        round-trip through an older loader stays well-formed.
        """
        return {
            "linked": sorted(self.linked),
            "routes": {},
        }

    def from_dict(self, d: dict) -> None:
        """Restore the linked-city set from a snapshot. Idempotent —
        fully replaces state.

        v0.53: per-tick routes are retired. Any ``routes`` block in an
        older save is *intentionally ignored* — those routes are NOT
        re-registered with the tick loop, so a pre-v0.53 save stops
        leaking stock the moment it loads. A city that only appeared
        under the old ``routes`` block (never in ``linked``) is still
        promoted to linked so the player keeps the commercial road they
        paid for; they just re-establish commerce via per-trip voyages.
        """
        # Belt-and-braces: drop anything we might still be tracking from a
        # prior load in the same session out of the shared tick manager.
        for rs in getattr(self, "routes", {}).values():
            for r in rs:
                try:
                    self.trade_manager.routes.remove(r)
                except ValueError:
                    pass
        self.linked = set(d.get("linked", []))
        # routes is retained as an always-empty dict for internal
        # consistency (routes_for / total_routes read it harmlessly).
        self.routes = {}
        # Preserve paid-for links that only existed under the legacy
        # routes block, without re-registering any route.
        for cid in (d.get("routes") or {}).keys():
            self.linked.add(cid)
