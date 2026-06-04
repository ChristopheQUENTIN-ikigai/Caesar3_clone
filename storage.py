"""Per-warehouse goods storage.

The economy keeps one global ``resources`` dict — that's what production,
consumption, and the HUD all read. The Storage module's job is to
remember *where* those goods physically live so the warehouse inspector
can show real per-instance stocks rather than the v0.8 proportional
estimate.

Design choice — why an *allocation* layer rather than a real ledger?

Two reasons. First: rewriting `economy.resources` into per-warehouse
dicts would touch every economy test (40+ assertions about resource
totals, food balance, tax math, …) for what is fundamentally a UI win.
Second: a real ledger needs trade-walker routing between warehouses,
loading order, partial loads, blocked routes — those are gameplay
features in their own right, not preconditions for "show me what's in
this warehouse." Splitting them lets v0.9 ship the visible win and
leaves the routing depth for a future release without breaking
compatibility.

What this means in practice:

  * ``Storage.distribute(global_resources)`` runs each tick after the
    economy update. It greedily fills warehouses in placement order,
    one resource at a time, until capacity is exhausted.
  * Goods that don't fit are tracked as ``overflow`` — surplus
    that has nowhere to go. The economy already caps additions at
    ``storage_capacity``, so overflow on a freshly-distributed snapshot
    is ~0 in normal play; it appears when a warehouse is demolished
    mid-tick or when capacity drops faster than goods deplete.
  * ``Storage.contents(row, col)`` returns the per-warehouse view.

The allocation is deterministic: same global pool + same warehouse
positions → same per-warehouse split. That makes saveload
straightforward (we don't have to persist the allocation; we recompute
it from the global pool on load) and makes the panel readings stable
across renders within one tick.

A future "real ledger" version replaces the body of ``distribute`` and
adds fields to the round-trip; the public API of ``contents`` stays.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from building import BuildingRegistry
    from game_map import GameMap

log = logging.getLogger("caesar3.storage")


# v0.44: single source of truth for "which kind of store does this good
# belong in?" — used both by the per-tick distribute() routing and by
# port↔store supply-chain routing (a docked ship loads nutrients from /
# unloads them to a granary, and everything else to/from a warehouse).
# The rule mirrors the type-based accept defaults distribute() already
# applies (granary accepts NUTRIENTS_SET; warehouse rejects nutrients),
# kept here so both paths agree and neither hard-codes the nutrient list.
def store_class_for_good(good: str) -> str:
    """Return ``"granary"`` for nutrient goods (bread, vegetables, fish,
    wine, …) or ``"warehouse"`` for everything else (wood, iron, tools,
    pottery, …). Pure classification — no map lookup."""
    from constants import NUTRIENTS_SET
    return "granary" if good in NUTRIENTS_SET else "warehouse"


def is_nutrient_good(good: str) -> bool:
    """Convenience predicate: does this good route to a granary?"""
    from constants import NUTRIENTS_SET
    return good in NUTRIENTS_SET



class Storage:
    """Per-warehouse stocks, allocated from the global resource pool."""

    def __init__(self, game_map: "GameMap", registry: "BuildingRegistry"):
        self.game_map = game_map
        self.registry = registry
        # Per-warehouse stocks: {(row, col): {good: qty}}.
        # Empty dict for newly-placed warehouses; populated by distribute().
        self._stocks: dict[tuple[int, int], dict[str, int]] = {}
        # Goods that didn't fit anywhere this distribution. Used by the
        # HUD to warn the player ("Storage full!") and by tests.
        self.overflow: dict[str, int] = {}

    # ── Per-frame distribution ───────────────────────────────────────────
    def distribute(self, global_resources: dict[str, float]) -> None:
        """Greedy-fill every storage building from the global pool.

        ``global_resources`` is a snapshot of ``economy.resources`` after
        the tick's production/consumption ran. This method is read-only
        from the economy's POV — it doesn't mutate the input.

        Allocation order: warehouses in `get_building_positions()` order
        (which is placement order). Within each warehouse, we deposit
        every present resource until the building's `storage` capacity
        is reached. This rewards "build a big warehouse first" without
        being clever about distance from producers — that smarter
        version is a future trade-walker concern.

        v0.11: each warehouse can have a per-good ``accepts`` filter
        stored in ``game_map.building_state[(r, c)]["accepts"]``. When
        present, only goods in the filter set are stored at that
        warehouse; rejected goods skip this warehouse and try the next.
        Absent / None / empty-after-default behaviour: ``None`` means
        "accept everything" (the default for newly-placed warehouses);
        an explicit empty set means "accept nothing" (used by the
        'Empty' inspector button to drain a warehouse over time).

        v0.16: granaries vs warehouses now have *type-based* default
        accept filters. A granary with no explicit filter accepts only
        the ten nutrients (bread, vegetables, fruits, meat, fish,
        cheese, oil, honey, spice, wine); a warehouse with no explicit
        filter accepts only non-nutrient goods (stone, planks, iron,
        tools, weapons, …). The player can still override per-instance
        — pressing 'Drain' on a granary still empties it; explicitly
        toggling on a non-nutrient at a granary's inspector adds it
        to the granary's accepts. The default is just the *initial*
        behaviour, not a hard rule.
        """
        from constants import NUTRIENTS_SET
        # Reset all stocks for this tick.
        live: list[tuple[int, int, int, set[str] | None]] = []
        # (row, col, capacity, accepts-or-None)
        for bt, r, c in self.game_map.get_building_positions():
            bd = self.registry.get(bt)
            if bd is None or bd.storage <= 0:
                continue
            # v0.11: read the accept filter off building_state. None means
            # "accept all" (the legacy default for storage buildings);
            # a set means "accept only these goods".
            # v0.16: when the player hasn't set an explicit filter, the
            # default depends on the building's type — granaries take
            # nutrients only, warehouses take non-nutrients only.
            accepts: set[str] | None = None
            state = self.game_map.building_state.get((r, c))
            if state is not None and "accepts" in state:
                raw = state["accepts"]
                # Saveload round-trips JSON arrays; coerce defensively.
                if raw is None:
                    accepts = None
                elif isinstance(raw, (set, list, tuple)):
                    accepts = set(raw)
                else:
                    accepts = None
            if accepts is None:
                # v0.16 type-based defaults. Granary id is "granary";
                # warehouse id is "warehouse". Anything else (a future
                # third storage building, a modder-added stockpile)
                # keeps the legacy "accept all" behaviour.
                if bt == "granary":
                    accepts = set(NUTRIENTS_SET)
                elif bt == "warehouse":
                    # Warehouse rejects nutrients but accepts every
                    # other good. We materialise this as a callable
                    # via the ``acc is None`` arm being repurposed —
                    # use a sentinel set that the eligibility check
                    # below interprets specially. To keep the data
                    # path simple, we leave ``accepts = None`` here
                    # and tag the warehouse rows below with a flag.
                    accepts = None
            live.append((r, c, bd.storage, accepts))
        # v0.16: build a parallel "is warehouse" flag so the eligibility
        # check below can apply the warehouse-specific "no nutrients"
        # rule without polluting every accepts set with a giant
        # not-nutrients listing (which would also break round-tripping
        # through the inspector — toggling "wheat" on a warehouse
        # would otherwise add wheat to a list that already implicitly
        # had it).
        is_warehouse: dict[tuple[int, int], bool] = {}
        for bt, r, c in self.game_map.get_building_positions():
            if bt == "warehouse":
                is_warehouse[(r, c)] = True
        live_set = {(r, c) for r, c, _, _ in live}
        # Drop any stale warehouses (demolished since last tick).
        stale = [k for k in self._stocks if k not in live_set]
        for k in stale:
            del self._stocks[k]

        # Fresh allocation. Walk goods round-robin so each warehouse
        # gets a *mix* rather than the first warehouse hoarding all the
        # food. Round-robin == "for each good, divide remaining stock
        # across remaining warehouses by capacity-share, then floor". A
        # second pass picks up the remainders.
        remaining = {g: int(q) for g, q in global_resources.items() if int(q) > 0}
        # Reset stocks before re-allocation.
        for key in list(self._stocks):
            self._stocks[key] = {}
        for r, c, _cap, _acc in live:
            self._stocks.setdefault((r, c), {})

        if live and remaining:
            for good, qty in remaining.items():
                if qty <= 0:
                    continue
                # v0.11: only warehouses that accept this good participate
                # in the proportional split. If none do, the good is left
                # in the global pool and reported as overflow at the end.
                # v0.16: warehouses without an explicit accepts filter
                # reject nutrients by default — the granaries are the
                # canonical home for those.
                is_nutrient = good in NUTRIENTS_SET
                eligible = [
                    (r, c, cap)
                    for r, c, cap, acc in live
                    if (
                        # Explicit filter wins regardless of building type.
                        (acc is not None and good in acc)
                        # No explicit filter + warehouse → reject nutrients.
                        or (acc is None and is_warehouse.get((r, c), False) and not is_nutrient)
                        # No explicit filter + non-warehouse, non-granary
                        # storage building → accept all (legacy behaviour
                        # for any future custom storage type).
                        or (acc is None and not is_warehouse.get((r, c), False))
                    )
                ]
                if not eligible:
                    self.overflow[good] = self.overflow.get(good, 0) + qty
                    continue
                eligible_total_cap = sum(cap for _, _, cap in eligible)
                if eligible_total_cap <= 0:
                    continue
                allocated = 0
                # First pass: capacity-proportional split, floored.
                for r, c, cap in eligible:
                    share = int(qty * cap / eligible_total_cap)
                    bin_remaining = cap - sum(self._stocks[(r, c)].values())
                    take = min(share, bin_remaining)
                    if take > 0:
                        self._stocks[(r, c)][good] = (
                            self._stocks[(r, c)].get(good, 0) + take
                        )
                        allocated += take
                # Second pass: spread the rounding remainder.
                left = qty - allocated
                if left > 0:
                    for r, c, cap in eligible:
                        if left <= 0:
                            break
                        bin_remaining = cap - sum(self._stocks[(r, c)].values())
                        if bin_remaining <= 0:
                            continue
                        take = min(left, bin_remaining)
                        self._stocks[(r, c)][good] = (
                            self._stocks[(r, c)].get(good, 0) + take
                        )
                        left -= take
                # Anything still unallocated is overflow.
                if left > 0:
                    self.overflow[good] = self.overflow.get(good, 0) + left

        # Goods present in the global pool but with no warehouse to put
        # them in are *also* overflow. They sit in the global pool and
        # the economy still treats them as available — overflow here
        # means "not visible in any warehouse," not "lost."
        if not live:
            for g, q in remaining.items():
                if q > 0:
                    self.overflow[g] = self.overflow.get(g, 0) + q

    def reset_overflow(self) -> None:
        """Clear the overflow tracker. Called once per tick after the
        HUD has had a chance to surface any overflow notification."""
        self.overflow = {}

    # ── Read API ─────────────────────────────────────────────────────────
    def contents(self, row: int, col: int) -> dict[str, int]:
        """Return ``{good: qty}`` for the warehouse at (row, col).

        Empty dict if no warehouse there or the warehouse hasn't been
        distribute()'d into yet. Returned dict is a copy — mutating it
        won't affect storage.
        """
        return dict(self._stocks.get((row, col), {}))

    def used(self, row: int, col: int) -> int:
        """Total goods stored at one warehouse."""
        return sum(self._stocks.get((row, col), {}).values())

    def free_slots(self, row: int, col: int) -> int:
        """Capacity remaining at one warehouse."""
        cell = self.game_map.grid[row][col]
        if cell is None:
            return 0
        bid = cell[0]
        bd = self.registry.get(bid)
        if bd is None or bd.storage <= 0:
            return 0
        return max(0, bd.storage - self.used(row, col))

    # ── v0.11: per-warehouse accept filter ───────────────────────────────
    def accepts(self, row: int, col: int) -> set[str] | None:
        """Return the accept filter for one warehouse.

        ``None`` (the default) means "accept any good". An empty set
        means "accept nothing" — used by the inspector's Empty button
        to drain a warehouse over a few ticks. A populated set means
        "only these goods are stored here".
        """
        state = self.game_map.building_state.get((row, col))
        if state is None:
            return None
        if "accepts" not in state:
            return None
        raw = state["accepts"]
        if raw is None:
            return None
        if isinstance(raw, (set, list, tuple)):
            return set(raw)
        return None

    def set_accepts(
        self, row: int, col: int, accepts: set[str] | None,
    ) -> None:
        """Set the accept filter for one warehouse.

        Pass ``None`` to clear (accept all). Pass a set (possibly
        empty) to restrict. The change takes effect on the *next*
        ``distribute()`` call — the player sees the warehouse drain or
        fill on the next tick, not mid-tick.

        Stored in ``game_map.building_state`` so it round-trips through
        save/load alongside house tier and decay condition.
        """
        cell = self.game_map.grid[row][col]
        if cell is None:
            return
        bid = cell[0]
        bd = self.registry.get(bid)
        if bd is None or bd.storage <= 0:
            return
        state = self.game_map.building_state.setdefault((row, col), {})
        if accepts is None:
            state.pop("accepts", None)
        else:
            # Persist as a sorted list for stable, JSON-friendly saves.
            state["accepts"] = sorted(accepts)

    def toggle_accept(self, row: int, col: int, good: str) -> bool:
        """Flip whether a single good is accepted at this warehouse.

        Returns the new state for that good (True = accepted). If the
        filter was previously ``None`` (accept-all), this materialises
        it as the full set of currently-known goods minus ``good``,
        which is the natural "the player wants to exclude one thing"
        outcome. If the filter is a set, we add or remove ``good``.
        """
        cell = self.game_map.grid[row][col]
        if cell is None:
            return False
        current = self.accepts(row, col)
        if current is None:
            # Materialise the implicit accept-all set, then remove `good`.
            # We seed from the union of all goods seen in the global
            # ledger today plus the canonical chain — anything not in
            # that union just gets accepted by being absent from the
            # restriction list, which is the right default.
            seed = set(self._stocks.get((row, col), {}).keys())
            seed.update(self.STANDARD_GOODS)
            new = seed - {good}
            self.set_accepts(row, col, new)
            return False
        new = set(current)
        if good in new:
            new.discard(good)
            self.set_accepts(row, col, new)
            return False
        new.add(good)
        self.set_accepts(row, col, new)
        return True

    # Canonical list of goods the inspector offers as accept-toggles.
    # A modder adding a new good can extend this tuple; absence here
    # only affects the UI — the distribute() filter still respects any
    # custom good in the accepts set.
    # v0.16: nutrients added — the inspector now lists every nutrient
    # the granary can hold (bread, vegetables, fruits, meat, fish,
    # cheese, oil, honey, spice, wine) plus the legacy non-nutrient
    # goods that warehouses store (planks, stone_blocks, …). Order:
    # nutrients first (granary-side), then non-nutrient inputs and
    # outputs. The inspector renders them in this order.
    STANDARD_GOODS: tuple[str, ...] = (
        # Nutrients (granary-side).
        "bread", "vegetables", "fruits", "meat", "fish",
        "cheese", "oil", "honey", "spice", "wine",
        # Legacy generic food (still produced by farm subsistence).
        "food",
        # Raw / intermediate (warehouse-side).
        "wheat", "flour", "wood", "planks",
        "iron", "tools", "weapons",
        "stone", "stone_blocks",
        "olives", "grapes", "clay", "pottery",
        # v0.16: livestock, horses.
        "livestock", "horses",
    )
