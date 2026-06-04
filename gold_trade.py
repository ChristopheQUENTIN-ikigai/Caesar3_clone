"""Gold trade — international goods↔gold exchange. (v0.35)

Sibling of ``bartering.py``. Where ``bartering`` swaps good A for good B
at a fixed ratio, ``gold_trade`` swaps a good for gold (or gold for a
good) at ``bartering.STOCK_PRICES`` — the same price table both modules
read so a modder rebalancing one rebalances both. 100 dn flat fee per
transaction, paid from treasury on both sides.

Why a separate file? Same reasoning as bartering.py: a modder rewriting
gold-trade behaviour (e.g. introducing a buy/sell spread) shouldn't
need to touch engine code. Pricing reads STOCK_PRICES; everything else
lives here.

Public API
----------

  compute_buy(resource, qty)  -> (gold_cost, fee)
      Cost in gold to BUY ``qty`` of ``resource`` from foreign markets.
      Pure helper; raises ValueError on unknown resource or qty <= 0.

  compute_sell(resource, qty) -> (gold_gain, fee)
      Gold gained from SELLING ``qty`` of ``resource``. Pure helper.
      Raises ValueError on unknown resource or qty <= 0.

  execute_buy(economy, resource, qty)  -> dict | None
  execute_sell(economy, resource, qty) -> dict | None
      Mutating versions. Return a record dict on success, ``None`` on
      failure (insufficient gold / stock / fee).

The record dict shape:
    {
      "side":      "buy" | "sell",
      "resource":  str,
      "qty":       int,
      "gold":      int,   # gold amount (cost on buy, gain on sell)
      "fee":       int,
    }
"""
from __future__ import annotations

from bartering import STOCK_PRICES, TRANSACTION_FEE, TRADE_SPREAD


def compute_buy(resource: str, qty: int) -> tuple[int, int]:
    """How much gold does it cost to BUY ``qty`` of ``resource``?

    Returns ``(cost, fee)``. Pure / deterministic — no economy
    mutation. The caller checks treasury affordability before
    executing.

    Raises:
        ValueError: on unknown resource or non-positive qty.
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")
    if resource not in STOCK_PRICES:
        raise ValueError(f"Unknown resource: {resource!r}")
    # v0.38: buy ABOVE the listed price by the spread (the merchant's
    # margin). int() floors after the markup so cheap goods still round
    # to a sensible cost.
    unit = int(round(STOCK_PRICES[resource] * (1.0 + TRADE_SPREAD)))
    cost = qty * unit
    return cost, TRANSACTION_FEE


def compute_sell(resource: str, qty: int) -> tuple[int, int]:
    """How much gold do we gain from SELLING ``qty`` of ``resource``?

    The market buys from you BELOW the listed price by the spread, so
    buy and sell are asymmetric: flipping a good immediately loses
    ~2×spread plus the flat fee (v0.38). Set ``bartering.TRADE_SPREAD``
    to 0.0 to restore the old symmetric behaviour.

    Raises:
        ValueError: on unknown resource or non-positive qty.
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")
    if resource not in STOCK_PRICES:
        raise ValueError(f"Unknown resource: {resource!r}")
    unit = int(round(STOCK_PRICES[resource] * (1.0 - TRADE_SPREAD)))
    gain = qty * max(0, unit)
    return gain, TRANSACTION_FEE


def execute_buy(economy, resource: str, qty: int) -> dict | None:  # noqa: ANN001
    """Spend gold for goods. Treasury must cover cost+fee."""
    try:
        cost, fee = compute_buy(resource, qty)
    except ValueError:
        return None
    if economy.treasury < cost + fee:
        return None
    economy.treasury -= (cost + fee)
    economy.resources[resource] = economy.resources.get(resource, 0) + qty
    return {
        "side":     "buy",
        "resource": resource,
        "qty":      int(qty),
        "gold":     int(cost),
        "fee":      int(fee),
    }


def execute_sell(economy, resource: str, qty: int) -> dict | None:  # noqa: ANN001
    """Give up goods for gold. Stock must cover ``qty``; treasury must
    cover the fee (the broker takes it FIRST, then credits the sale
    proceeds). Returns None if either constraint fails.
    """
    try:
        gain, fee = compute_sell(resource, qty)
    except ValueError:
        return None
    have = economy.resources.get(resource, 0)
    if have < qty:
        return None
    if economy.treasury < fee:
        return None
    economy.resources[resource] = have - qty
    economy.treasury += (gain - fee)
    return {
        "side":     "sell",
        "resource": resource,
        "qty":      int(qty),
        "gold":     int(gain),
        "fee":      int(fee),
    }


def known_resources() -> list[str]:
    """Resources this module can trade. Mirrors bartering.known_resources."""
    return list(STOCK_PRICES.keys())
