"""Bartering — international resource exchange. (v0.19)

A standalone exchange table: the player can swap resource A for resource
B at a fixed price ratio, paying a flat 100-denarii transaction fee on
each barter. Bartering is the **player-driven** counterpart to the
passive trade routes in ``trade.py``: routes auto-execute every tick at
a single buy/sell price, while a barter is a one-shot, two-sided swap
the player initiates from the 'b' menu.

Why a separate file? The user asked for prices to be tunable in
``./bartering.py`` so a modder can change the equilibrium without
touching engine code. Keeping the table in this module — and exposing
it as a plain dict — means an editor or a script can rewrite the
numbers and the next barter sees them immediately.

Public API
----------

  STOCK_PRICES (dict)
      ``{resource_id: gold_per_unit_at_market}``. The "market price" of
      one unit, in denarii. Bartering computes the swap ratio from
      these: 1 unit of A is worth ``STOCK_PRICES[A]`` denarii, so
      ``q_A`` of A buys ``q_A * STOCK_PRICES[A] / STOCK_PRICES[B]``
      units of B (rounded down). The player also pays the fee from
      treasury.

  TRANSACTION_FEE (int)
      Flat fee per barter, in gold. Set to 100 per the spec.

  compute_barter(give_id, give_qty, receive_id) -> (recv_qty, fee)
      Pure helper: returns how much of ``receive_id`` the player gets
      and the fee owed. ``recv_qty`` is an int (rounded down). Raises
      ValueError if either resource is unknown or ``give_qty`` is
      non-positive.

  execute_barter(economy, give_id, give_qty, receive_id) -> dict | None
      Mutates the economy's resource pool. Returns a record on
      success, ``None`` on failure (insufficient give-stock, treasury
      can't cover the fee, or the receive amount rounds to zero).
      The record is the same shape the recorder writes to ledger
      so the 'r' debug log can include barter events.

Design notes
------------

  * Prices are *static*: the spec says "stocks price ratios should be
    determined in file ./bartering.py", which we read as "pin them
    here, mod-friendly". A future tuning pass could add stochastic
    drift on a per-tick timer; the table-only design ships first.
  * The fee is taken from the treasury (gold), not from the goods
    pool. A barter on an empty treasury fails with reason
    "no_fee_funds" — the caller surfaces that to the player.
  * We round the received quantity DOWN. Rounding up would let a
    player extract 1 unit of an expensive good for nothing on
    rounding error; down is the safer rule.
  * No nutrient/non-nutrient routing — the barter just edits
    ``economy.resources`` (the global pool) and the per-warehouse
    distributor in ``storage.distribute()`` reallocates next tick.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from economy import EconomyManager


# ─── Stock prices (gold per unit, as if traded with a foreign buyer) ──
# Tuned from the typical production rates: a barley→bread chain
# produces bread at ~10/tick, so bread sits middling. Olive oil is rare
# (one chain, slow) and sits high. Wine/spice/honey are luxury → high.
# Stone/wood are bulk → low. The 1:N ratios below match the v0.5 trade
# route table where it overlaps (food=8, iron=12) and extrapolate
# from there.
#
# Modders: rewriting this table is the supported way to rebalance
# barter without touching engine code. Add new resource ids as the
# game gains them — unknown ids in compute_barter raise ValueError,
# which the menu catches and surfaces.
STOCK_PRICES: dict[str, int] = {
    # Subsistence
    "bread":        8,
    "vegetables":   6,
    "fruits":       7,
    "meat":         18,
    "fish":         10,
    "cheese":       14,
    "oil":          22,    # olive oil — luxury, rare chain
    "honey":        20,
    "spice":        25,
    "wine":         24,
    # Raw / intermediate
    "wheat":        4,
    "flour":        7,
    "olives":       6,
    "grapes":       6,
    "clay":         3,
    "pottery":      12,
    "livestock":    14,
    "horses":       40,
    # Industry / civic
    "wood":         3,
    "planks":       5,
    "stone":        4,
    "stone_blocks": 8,
    "iron":         12,
    "tools":        15,
    "weapons":      30,
    # v0.26: mining tab — copper and gold join iron as refined metals.
    # Ore intermediates priced low; refined ingots scale up.
    # Gold is the highest-priced refined metal — it's both a luxury
    # and a treasury input. Copper sits between iron and gold.
    "copper_ore":   5,
    "gold_ore":     12,
    "copper":       16,
    "gold":         40,
}

# Flat per-transaction fee in gold (denarii). Per spec.
TRANSACTION_FEE: int = 100

# v0.38: buy/sell spread. The market buys from you below the listed
# price and sells to you above it — the merchant's margin. A spread of
# 0.15 means you BUY at price×1.15 and SELL at price×0.85, so flipping a
# good immediately loses ~30% plus the flat fee. This makes commerce a
# real decision (you trade to cover a *shortage*, not to arbitrage) and
# is the economic depth behind the v0.38 trade ships. Set to 0.0 to
# restore the pre-v0.38 symmetric behaviour where the only cost was the
# flat fee.
TRADE_SPREAD: float = 0.15


def known_resources() -> list[str]:
    """Return the resource ids the table prices, in declaration order.

    The bartering menu shows two columns picked from this list — the
    declaration order above mirrors the player-mental order
    (subsistence → intermediate → industry).
    """
    return list(STOCK_PRICES.keys())


def compute_barter(
    give_id: str, give_qty: int, receive_id: str,
) -> tuple[int, int]:
    """Return ``(receive_qty, fee)`` for swapping ``give_qty`` of
    ``give_id`` for ``receive_id``.

    Pure / deterministic — no economy mutation. The caller checks
    affordability before executing.

    Raises:
        ValueError: if either resource is unknown, ``give_id ==
        receive_id``, or ``give_qty <= 0``.
    """
    if give_qty <= 0:
        raise ValueError(f"give_qty must be positive, got {give_qty}")
    if give_id == receive_id:
        raise ValueError("Can't barter a resource for itself")
    if give_id not in STOCK_PRICES:
        raise ValueError(f"Unknown resource: {give_id!r}")
    if receive_id not in STOCK_PRICES:
        raise ValueError(f"Unknown resource: {receive_id!r}")
    pa = STOCK_PRICES[give_id]
    pb = STOCK_PRICES[receive_id]
    # value-of-A / price-of-B, integer floor.
    receive_qty = (give_qty * pa) // pb
    return receive_qty, TRANSACTION_FEE


def execute_barter(
    economy, give_id: str, give_qty: int, receive_id: str,
) -> dict | None:  # noqa: ANN001 — economy is concrete but typed-hint avoids cycle
    """Apply a barter to ``economy``. Returns a record dict on success
    or ``None`` on failure.

    Failure modes:
      * give-stock insufficient
      * treasury can't cover the fee
      * receive_qty rounds to zero (give_qty too small for the ratio)
      * unknown resource id (caught from compute_barter)
    """
    try:
        recv_qty, fee = compute_barter(give_id, give_qty, receive_id)
    except ValueError:
        return None
    if recv_qty <= 0:
        return None
    have = economy.resources.get(give_id, 0)
    if have < give_qty:
        return None
    if economy.treasury < fee:
        return None
    # Apply.
    economy.resources[give_id] = have - give_qty
    economy.resources[receive_id] = economy.resources.get(receive_id, 0) + recv_qty
    economy.treasury -= fee
    return {
        "give":    give_id,
        "give_qty": int(give_qty),
        "recv":    receive_id,
        "recv_qty": int(recv_qty),
        "fee":     int(fee),
    }
