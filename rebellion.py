"""Rebellion — sustained starvation turns citizens hostile.

The mechanic in one paragraph: every tick, if the city's food coverage
is below par, a "pressure" counter ramps up; if it's fine, the counter
decays back. When pressure crosses a threshold, rebels spawn from
random house tiles (using the existing Enemy walker class) and the
counter resets. The threshold + per-tick ramp + decay rate live in
`balance.py`, so a modder can make the city patient or volatile
without engine edits.

Why a separate tracker? It's a stateful loop with one number (pressure)
and one input (fed_fraction). Putting it in the economy would entangle
its serialisation with the food bookkeeping; putting it in the walker
manager would entangle it with combat. As its own module it's trivially
testable and saves into one dict field.
"""
from __future__ import annotations

import logging

from balance import BALANCE, Balance

log = logging.getLogger("caesar3.rebellion")


class RebellionTracker:
    def __init__(self, balance: Balance = BALANCE):
        self.balance = balance
        # 0.0 = at peace; rises while starving, decays while fed. Crossing
        # `rebel_pressure_threshold` triggers a spawn.
        self.pressure: float = 0.0
        # Cooldown ticks: after a spawn we sit at 0 for a few ticks so a
        # single famine doesn't dump every house's worth of rebels onto
        # the map at once.
        self._cooldown: int = 0
        # For the HUD: how many spawns since the city was founded.
        self.total_spawned: int = 0

    def update(self, fed_fraction: float, current_rebel_count: int) -> int:
        """Advance the tracker by one tick. Returns the number of rebels
        the *caller* should spawn this tick.

        Args:
            fed_fraction: 0.0 = total famine, 1.0 = everyone ate.
            current_rebel_count: number of Enemy walkers currently on
                the map. We respect the global rebel cap so the player
                isn't dogpiled by a single sustained famine.
        """
        b = self.balance
        if self._cooldown > 0:
            self._cooldown -= 1
            return 0

        # Starving = fed_fraction below 0.7. Anything above counts as fed
        # for decay purposes — a 90%-fed city should still feel safe.
        if fed_fraction < 0.7:
            # Ramp scales with how starved we are. A 50% fed city ramps
            # at half-speed of a totally famined city; a 0% fed city
            # ramps at full speed. This makes the loop's *signal* the
            # same as the player's intuition: short, light hunger →
            # nothing happens; deep, sustained hunger → rebels.
            severity = (0.7 - fed_fraction) / 0.7   # 0..1
            self.pressure += b.rebel_pressure_per_starve_tick * severity
        else:
            self.pressure = max(0.0, self.pressure - b.rebel_pressure_decay)

        if self.pressure < b.rebel_pressure_threshold:
            return 0

        # Crossed the threshold: how many to spawn?
        # 1-3 typically, scaled mildly by how *over* the threshold we are
        # (so neglecting the city for a long time is worse than barely
        # crossing). Capped by the rebel max.
        overshoot = self.pressure / b.rebel_pressure_threshold
        n = min(3, max(1, int(overshoot)))
        room = max(0, b.rebel_max_concurrent - current_rebel_count)
        n = min(n, room)
        if n <= 0:
            # Cap reached — chill out for a bit before re-ramping.
            self.pressure = 0.0
            self._cooldown = 30
            return 0

        # Spawn and reset.
        self.pressure = 0.0
        self._cooldown = 30
        self.total_spawned += n
        log.warning("Rebellion threshold crossed: spawning %d rebels", n)
        return n

    # ── Persistence ──────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "pressure": self.pressure,
            "cooldown": self._cooldown,
            "total_spawned": self.total_spawned,
        }

    def from_dict(self, d: dict) -> None:
        self.pressure = float(d.get("pressure", 0.0))
        self._cooldown = int(d.get("cooldown", 0))
        self.total_spawned = int(d.get("total_spawned", 0))
