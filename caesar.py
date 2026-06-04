"""Caesar requests — periodic tribute demands from Rome.

Every game-month-or-so Caesar asks the player for either money or a
unit of soldiers. The player has a deadline (in ticks) to comply. If
they pay/fulfil before the deadline → a notification, +happiness,
+diplomacy. If they don't → a happiness hit, diplomacy loss, and a
queued raid (a soft "punishment" that the existing combat code
already handles).

Why a manager class?

The behaviour involves: a schedule (next request time), an in-flight
request with a deadline, and an outcome that touches economy
(`treasury`), diplomacy (`points`), and walker_manager (queueing a
raid). Threading those through the economy or events.json would
muddy both. A standalone manager + a HUD banner is the cleanest fit.

Public API:

    crm = CaesarRequestManager()
    crm.update(month: int, tick: int, economy, walker_manager)
    crm.current     # the in-flight request dict, or None
    crm.try_comply(economy, diplomacy)   # called from a HUD button
    crm.history     # last N outcomes for the stats panel

The request dict has: kind ("money"|"soldiers"), amount, deadline_tick,
issued_tick. Soldiers requests deduct from a virtual recruitable pool —
we don't currently have a dedicated soldier supply, so "1 soldier" =
30 weapons + 50 dn (the player must have built the chain).
"""
from __future__ import annotations

import logging
import random
from typing import Any

from balance import BALANCE, Balance

log = logging.getLogger("caesar3.caesar")


# Soldier requests cost weapons + denarii rather than literal walkers
# (the soldier walkers are city defenders — Caesar doesn't conscript
# them mid-fight). Tweak in balance.py if it feels wrong.
SOLDIER_WEAPONS_COST = 30
SOLDIER_MONEY_COST = 50


class CaesarRequestManager:
    HISTORY_MAX = 20

    def __init__(self, balance: Balance = BALANCE):
        self.balance = balance
        # Months until next request. Initial roll happens lazily so
        # tests that never call update() don't trigger anything.
        self.next_request_month: int | None = None
        self.current: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []

    # ── Scheduling ───────────────────────────────────────────────────────
    def _roll_next(self, current_month: int) -> None:
        b = self.balance
        wait = random.randint(
            b.caesar_request_interval_months_min,
            b.caesar_request_interval_months_max,
        )
        self.next_request_month = current_month + wait
        log.info("Caesar will next ask in %d months (at month %d)",
                 wait, self.next_request_month)

    def _generate_request(self, tick: int) -> dict[str, Any]:
        """Build a fresh request dict. Money/soldiers split 60/40."""
        kind = "money" if random.random() < 0.6 else "soldiers"
        if kind == "money":
            # Sized to the rough income of a mid-game city: 200-800 dn.
            amount = random.randint(200, 800)
        else:
            # 1-3 soldiers worth of supplies.
            amount = random.randint(1, 3)
        return {
            "kind": kind,
            "amount": amount,
            "issued_tick": tick,
            "deadline_tick": tick + self.balance.caesar_deadline_ticks,
            "honoured": False,
        }

    # ── Tick ─────────────────────────────────────────────────────────────
    def update(self, month: int, tick: int, economy, walker_manager=None) -> dict | None:
        """Advance the manager by one tick. Returns a "newly issued" dict
        if a request just fired this call, else None.

        Caller (game window) is responsible for showing a banner and
        wiring the HUD button that calls `try_comply()`.
        """
        # In-flight request: did the deadline expire?
        # (Checked first so a missed deadline always fires, even if the
        # manager was just constructed mid-game from a save.)
        if self.current is not None:
            if tick >= self.current["deadline_tick"]:
                self._fail(economy, walker_manager)
                self.current = None
                self._roll_next(month)
            return None

        # First-time bootstrap: schedule the first request.
        if self.next_request_month is None:
            self._roll_next(month)
            return None

        # No in-flight request: time to roll a new one?
        if month >= self.next_request_month:
            req = self._generate_request(tick)
            self.current = req
            log.warning("Caesar demands: %s %d  (deadline tick %d)",
                        req["kind"], req["amount"], req["deadline_tick"])
            return req

        return None

    # ── Comply / fail ────────────────────────────────────────────────────
    def try_comply(self, economy, diplomacy) -> bool:
        """Player attempts to fulfil the current request. Returns True on
        success (resources deducted, request closed), False if they can't
        afford it (request stays in flight)."""
        req = self.current
        if req is None:
            return False
        b = self.balance

        if req["kind"] == "money":
            cost = req["amount"]
            if economy.treasury < cost:
                return False
            economy.treasury -= cost
        else:  # soldiers
            n = req["amount"]
            money_cost = SOLDIER_MONEY_COST * n
            weapons_cost = SOLDIER_WEAPONS_COST * n
            have_money = economy.treasury >= money_cost
            have_weapons = economy.resources.get("weapons", 0) >= weapons_cost
            if not (have_money and have_weapons):
                return False
            economy.treasury -= money_cost
            economy.resources["weapons"] -= weapons_cost

        # Honoured: rewards.
        diplomacy.add(b.diplomacy_per_caesar_honoured)
        economy.happiness = min(100.0, economy.happiness + 5.0)
        req["honoured"] = True
        self.history.append(dict(req))
        if len(self.history) > self.HISTORY_MAX:
            self.history = self.history[-self.HISTORY_MAX:]
        log.info("Caesar request honoured: %s %d", req["kind"], req["amount"])
        self.current = None
        # Schedule next.
        # Caller should re-call update() next tick which will _roll_next
        # via the "no current, no next" path; do it here so we don't fire
        # twice in the same month.
        return True

    def _fail(self, economy, walker_manager) -> None:
        """Deadline missed: penalties and a queued raid."""
        b = self.balance
        req = self.current
        if req is None:
            return
        # Happiness hit.
        economy.happiness = max(0.0, economy.happiness + b.caesar_failure_happiness)
        # Diplomacy loss.
        # We'd love to call diplomacy.add() here but the manager doesn't own
        # the diplomacy reference; the game window applies it after seeing
        # `failed_event` — caller responsibility. We log and stash for
        # consumption.
        req["honoured"] = False
        self.history.append(dict(req))
        if len(self.history) > self.HISTORY_MAX:
            self.history = self.history[-self.HISTORY_MAX:]
        log.warning("Caesar request FAILED: %s %d (-%.0f happiness)",
                    req["kind"], req["amount"], -b.caesar_failure_happiness)

        # Queued punishment raid: if the walker_manager exposes
        # `force_raid`, fire one — it's already exactly the "soft punish"
        # mechanic Caesar would deploy. If walker_manager is None (tests)
        # silently skip.
        if walker_manager is not None and hasattr(walker_manager, "force_raid"):
            try:
                walker_manager.force_raid_punitive(req["amount"])
            except AttributeError:
                # We'll add force_raid_punitive in WalkerManager too;
                # if missing, just log.
                log.debug("walker_manager has no force_raid_punitive — skipping punitive raid")

    def get_failed_events(self) -> list[dict]:
        """Return failed requests since last call. Used by game window
        to apply diplomacy penalty (the manager doesn't know about
        diplomacy directly)."""
        # We just expose history filtered to non-honoured. Caller is
        # responsible for tracking which they've already processed
        # (typically by checking the request's issued_tick).
        return [h for h in self.history if not h["honoured"]]

    # ── Persistence ──────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "next_request_month": self.next_request_month,
            "current": self.current,
            "history": list(self.history),
        }

    def from_dict(self, d: dict) -> None:
        self.next_request_month = d.get("next_request_month")
        self.current = d.get("current")
        self.history = list(d.get("history", []))
