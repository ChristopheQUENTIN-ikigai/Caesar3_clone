"""Diplomacy — a single floating "favour with Rome" score.

Why a separate module rather than a field on EconomyManager?
The economy already mixes resources, taxes, population and happiness;
adding a fourth concern in there makes the tick path harder to read.
Diplomacy is small, has different read/write patterns (mutated by
discrete events, decayed slowly per tick), and has its own
serialisation surface — keeping it out of the economy keeps both
clean.

Public surface:

    dip = DiplomacyTracker(start=20.0)
    dip.tick(senate_count: int)          # called once per game tick
    dip.add(amount: float)               # honoured Caesar request, etc.
    dip.points                            # read for HUD / gating
    dip.can_afford(cost) / dip.spend(cost)
    dip.to_dict() / dip.from_dict(d)     # save / load round-trip

Senates accrue diplomacy slowly. Caesar requests honoured add a chunk;
failed requests subtract a bigger chunk. Trade routes spend points to
open. The numbers come from `balance.py` so modders can tune them.
"""
from __future__ import annotations

import logging

from balance import BALANCE, Balance

log = logging.getLogger("caesar3.diplomacy")


class DiplomacyTracker:
    """Single-counter diplomacy state.

    Floor at zero — you can't have *negative* diplomatic standing in
    this model; you simply have none, which means no new trade routes,
    no Caesar leniency. (A full reputation/grudge system would be its
    own multi-tribe layer; not in scope for v0.8.)
    """

    def __init__(self, balance: Balance = BALANCE):
        self.balance = balance
        self.points: float = float(balance.diplomacy_start)

    # ── Per-tick accrual ─────────────────────────────────────────────────
    def tick(self, senate_count: int) -> None:
        """Each placed senate adds ``diplomacy_per_senate_tick`` per tick."""
        if senate_count <= 0:
            return
        self.points += senate_count * self.balance.diplomacy_per_senate_tick

    # ── Discrete adjustments ─────────────────────────────────────────────
    def add(self, amount: float) -> None:
        self.points = max(0.0, self.points + amount)
        log.debug("Diplomacy %+.1f → %.1f", amount, self.points)

    # ── Gating helpers (used by trade route purchase, etc.) ──────────────
    def can_afford(self, cost: float) -> bool:
        return self.points >= cost

    def spend(self, cost: float) -> bool:
        if not self.can_afford(cost):
            return False
        self.points -= cost
        return True

    # ── Persistence ──────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"points": self.points}

    def from_dict(self, d: dict) -> None:
        self.points = float(d.get("points", self.balance.diplomacy_start))
