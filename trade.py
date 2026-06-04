"""Trade routes — inter-city commerce.

Pure logic: takes / mutates an `EconomyManager`. No arcade dependency.
"""
from __future__ import annotations

import logging

log = logging.getLogger("caesar3.trade")


class TradeRoute:
    def __init__(self, name: str, goods: str, rate: int, profit_per_unit: float,
                 pays_treasury: bool = True):
        self.name = name
        self.goods = goods
        self.rate = rate
        self.profit_per_unit = profit_per_unit
        self.active = True
        self.total_traded: int = 0
        self.total_profit: float = 0.0
        # v0.52: whether this route credits gold to the treasury on each
        # tick it trades. The default True is the only path used in
        # practice now — it's the generic ambient-route primitive.
        # v0.52 had the commercial-roads window create inter-city routes
        # with pays_treasury=False (move stock, no gold). v0.53 retired
        # that layer entirely (inter-city export is per-trip via
        # voyages.py), so no caller passes False anymore; the flag is
        # kept for the ambient primitive, save-compat, and tests.
        self.pays_treasury: bool = bool(pays_treasury)

    def execute(self, economy) -> dict | None:  # noqa: ANN001
        """Run one trade tick. Returns a dict describing the trade or None.

        v0.52: ``pays_treasury`` gates the gold credit only — a
        non-paying route still debits stock and reports the trade (so
        the export is visible and the goods genuinely leave), but the
        ``profit`` it returns is 0 and the treasury is untouched. This
        is how inter-city commerce became "income only by trip": the
        commercial-roads routes move goods without dripping gold, and
        the voyage layer pays on each ship's return instead.
        """
        if not self.active:
            return None
        avail = economy.resources.get(self.goods, 0)
        amt = min(self.rate, avail)
        if amt <= 0:
            return None
        economy.resources[self.goods] -= amt
        self.total_traded += amt
        if self.pays_treasury:
            profit = amt * self.profit_per_unit
            economy.treasury += profit
            self.total_profit += profit
            log.debug("%s traded %d %s → +%.0f dn", self.name, amt, self.goods, profit)
            return {"goods": self.goods, "amount": amt, "profit": profit}
        # Non-paying (inter-city) route: stock moved, no gold this tick.
        log.debug("%s moved %d %s (no per-tick gold; paid by trip)",
                  self.name, amt, self.goods)
        return {"goods": self.goods, "amount": amt, "profit": 0.0}

    def to_dict(self) -> dict:
        return {
            "name": self.name, "goods": self.goods,
            "rate": self.rate, "profit_per_unit": self.profit_per_unit,
            "active": self.active,
            "total_traded": self.total_traded, "total_profit": self.total_profit,
            # v0.52: persist the per-tick gold gate. Older saves without
            # the key load as True (the original always-paying behaviour),
            # so pre-v0.52 saves are unaffected.
            "pays_treasury": self.pays_treasury,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TradeRoute":
        r = cls(d["name"], d["goods"], d["rate"], d["profit_per_unit"],
                pays_treasury=bool(d.get("pays_treasury", True)))
        r.active = d.get("active", True)
        r.total_traded = d.get("total_traded", 0)
        r.total_profit = d.get("total_profit", 0.0)
        return r


class TradeRouteManager:
    def __init__(self, economy):  # noqa: ANN001
        self.economy = economy
        self.routes: list[TradeRoute] = []
        # v0.51: total profit credited by all routes on the last update()
        # tick. Read by the finance panel; 0.0 before the first tick.
        self.last_tick_profit: float = 0.0

    def add_route(self, route: TradeRoute) -> None:
        self.routes.append(route)
        log.info("Trade route added: %s", route.name)

    def update(self) -> None:
        # v0.51: accumulate this tick's total route profit so the
        # finance ('$') panel can show trade-route income as its own
        # line. Reset each tick; a route that trades nothing adds 0.
        self.last_tick_profit: float = 0.0
        for r in self.routes:
            rec = r.execute(self.economy)
            if rec:
                self.last_tick_profit += float(rec.get("profit", 0.0))

    def get_summary(self) -> list[str]:
        return [
            f" [{'ON' if r.active else 'OFF'}] {r.name}: "
            f"{r.goods} x{r.rate} @{r.profit_per_unit} gold"
            for r in self.routes
        ]

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.routes]

    def from_list(self, lst: list[dict]) -> None:
        self.routes = [TradeRoute.from_dict(d) for d in lst]
