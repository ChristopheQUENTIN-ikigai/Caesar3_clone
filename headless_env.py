"""Headless simulation environment — proof of concept (companion to AUDIT).

This module shows that the Caesar III clone's *simulation* can be driven
with no GL window, no `arcade.run()`, and no display — the same property
the 1264-test suite already relies on.

It is intentionally small and honest about its limits:

  * It drives the **economy + map + a subset of subsystems** by hand,
    mirroring the part of `game_window._game_tick` that is pure logic.
  * It does **not** yet replicate every subsystem the real window ticks
    (walkers movement, services coverage recompute, decay, triggers,
    rebellion, voyages). Those live as methods on `CaesarGameWindow`.
    See AUDIT §"Headless training" for the recommended refactor that
    would let this harness call the *real* tick instead of a hand-rolled
    subset.

Run:
    python headless_env.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from constants import BUILDINGS_PATH
from building import BuildingRegistry
from game_map import GameMap
from economy import EconomyManager
from balance import BALANCE


@dataclass
class StepResult:
    """One environment step's outcome — the raw material for an RL reward."""
    tick: int
    population: int
    treasury: float
    happiness: float
    fed_fraction: float
    placed_ok: bool | None  # None when the action wasn't a placement


class HeadlessCity:
    """Minimal headless driver around the pure simulation core.

    Action vocabulary (enough to demonstrate the loop):
        ("noop",)                       -> just advance a tick
        ("place", building_id, row, col)-> try to build, then advance a tick
        ("demolish", row, col)          -> remove a building, then advance

    Observation: a flat dict of scalars + the building grid. A real RL
    wrapper would turn this into a fixed-size tensor (see AUDIT).
    """

    def __init__(self, seed: int | None = None) -> None:
        self.registry = BuildingRegistry.from_json_file(BUILDINGS_PATH)
        self.game_map = GameMap(self.registry)
        self.economy = EconomyManager(self.registry, BALANCE)
        self.game_time = 0
        self.month = 1
        self.year = 1

    # ── Gym-style API ────────────────────────────────────────────────────
    def reset(self) -> dict:
        self.game_map = GameMap(self.registry)
        self.economy = EconomyManager(self.registry, BALANCE)
        self.game_time = 0
        self.month = 1
        self.year = 1
        return self.observe()

    def step(self, action: tuple) -> StepResult:
        placed_ok: bool | None = None

        kind = action[0]
        if kind == "place":
            _, bid, row, col = action
            bd = self.registry.get(bid)
            if bd is not None and self.economy.can_afford(bd.cost):
                placed_ok = self.game_map.place_building(
                    row, col, bid, economy=self.economy,
                    bypass_construction=True,
                )
                if placed_ok:
                    self.economy.spend(bd.cost)
            else:
                placed_ok = False
        elif kind == "demolish":
            _, row, col = action
            # GameMap exposes removal; refund handling omitted for brevity.
            placed_ok = self.game_map.remove_building(row, col) \
                if hasattr(self.game_map, "remove_building") else None

        self._tick()
        return StepResult(
            tick=self.game_time,
            population=int(self.economy.population),
            treasury=float(self.economy.treasury),
            happiness=float(self.economy.happiness),
            fed_fraction=float(self.economy.fed_fraction),
            placed_ok=placed_ok,
        )

    # ── One simulation tick (subset of game_window._game_tick) ───────────
    def _tick(self) -> None:
        self.game_time += 1
        if self.game_time % 10 == 0:
            self.month += 1
            if self.month > 12:
                self.month = 1
                self.year += 1

        positioned = [
            (bt, r, c) for (bt, r, c) in self.game_map.get_building_positions()
            if not self.game_map.is_under_construction(r, c)
        ]
        # Pure economy tick. We pass no service/feature lookups here, which
        # means water/feature gating is bypassed — acceptable for a PoC,
        # NOT for a faithful environment. See AUDIT.
        self.economy.update(positioned)

    # ── Observation ──────────────────────────────────────────────────────
    def observe(self) -> dict:
        return {
            "tick": self.game_time,
            "population": int(self.economy.population),
            "treasury": float(self.economy.treasury),
            "happiness": float(self.economy.happiness),
            "fed_fraction": float(self.economy.fed_fraction),
            "resources": {k: int(v) for k, v in self.economy.resources.items()},
            "n_buildings": len(list(self.game_map.get_building_positions())),
        }


def _demo() -> None:
    """Run a tiny scripted episode so you can see the loop work."""
    env = HeadlessCity()
    print("reset:", env.reset())

    # A scripted "policy": plop a few houses and a farm, then idle.
    script = [
        ("place", "house", 10, 10),
        ("place", "house", 10, 12),
        ("place", "farm", 14, 14),
        ("noop",), ("noop",), ("noop",), ("noop",), ("noop",),
    ]
    for a in script:
        r = env.step(a)
        print(f"t={r.tick:>3}  pop={r.population:>4}  "
              f"treasury={r.treasury:>8.0f}  happy={r.happiness:>5.1f}  "
              f"fed={r.fed_fraction:>4.2f}  placed={r.placed_ok}")

    print("final obs:", env.observe())


if __name__ == "__main__":
    _demo()
