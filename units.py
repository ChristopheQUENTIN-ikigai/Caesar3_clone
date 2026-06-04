"""Unit registry — v0.23.x.

Military unit definitions are loaded from ``data/units.json`` into typed
``UnitDef`` dataclasses, mirroring the building-registry pattern. The
garrison buildings (barracks, fort) declare which unit they spawn via a
``spawn_unit`` field on the building JSON entry; the WalkerManager looks
the id up here and instantiates a Soldier with the unit's stats.

Keeping units in JSON has the same payoff as keeping buildings in JSON:
modders can add a new unit type by appending an entry, no engine code
edits required (as long as the new unit fits the existing combat model).
The full v0.24 unit editor (planned) will edit this file in place.

This module is also import-safe at module load time: if ``data/units.json``
is missing the registry falls back to a hard-coded built-in set so the
game still launches. The five canonical units ship under the conventional
ids ``scout``, ``light_infantry``, ``heavy_infantry``, ``heavy_cavalry``,
``bowman`` — these are the ids documented in ``ROADMAP.md`` and used by
the tests.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("caesar3.units")


# Default sight/patrol used when an entry doesn't list them. These match
# the legacy v0.22 Soldier class so a unit definition without these
# fields is identical to a plain pre-units soldier.
_DEFAULT_SIGHT = 6
_DEFAULT_PATROL = 8
_DEFAULT_SPEED = 0.05


@dataclass(frozen=True)
class UnitDef:
    """One row of ``data/units.json``."""
    id: str
    name: str
    category: str               # "infantry" | "cavalry" | "light" | "ranged"
    hp: int
    damage: int
    speed: float
    sight: int
    patrol_radius: int
    ranged: bool
    armoured: bool
    sprite: str
    description: str = ""

    # ── v0.25 additions (unit editor) ─────────────────────────────────
    # Defence rating. Pre-v0.25 the model was a bool (`armoured`) that
    # halved incoming damage. We now expose a finer-grained integer so
    # the editor can dial defence independently of the bool — but the
    # legacy halving rule still triggers when `armoured=True`, so old
    # save and JSON data keep working. New units default `defense=0`
    # (no flat damage reduction); the editor surfaces it as a tweak
    # alongside the attack-damage / HP sliders.
    defense: int = 0
    # Training time in ticks from the moment the garrison recruits a
    # walker to when the unit is combat-ready. Combined with the
    # garrison building's existing spawn cooldown — the unit doesn't
    # appear on the map until both expire. 0 = legacy instant-spawn
    # behaviour (kept as the default).
    training_time: int = 0
    # Per-spawn gold cost. Deducted from the treasury when the
    # garrison recruits. 0 means free (covered by the garrison's own
    # building consumption.money rate).
    cost: int = 0
    # Per-tick upkeep cost. Deducted while the unit is alive on the
    # map. Stacks with the garrison building's upkeep. 0 = no extra
    # per-walker upkeep.
    upkeep: int = 0
    # Weapons consumed per spawn (on top of the garrison's
    # building-level weapons consumption). A ballista costs 3 weapons
    # to outfit a single crew; a bowman costs 1. Default 0 keeps
    # existing units silent on this rule.
    weapon_cost: int = 0
    # Free-form skill tags: e.g. ``["mood_booster"]`` for a
    # battlefield-leader buffing nearby units, ``["surgery"]`` for a
    # medical doctor unit. The combat system can branch on these at
    # runtime; the editor presents them as toggleable labels.
    skills: list[str] = field(default_factory=list)
    # v0.33: side the unit fights for. "roman" = the player's army
    # (Soldier walkers, spawned from garrisons). "barbarian" = hostile
    # raiders spawned from map edges as Enemy walkers. The combat
    # system reads this to decide whether a unit shows up on the
    # friendly or hostile side, replacing the previous hard-coded
    # COMBAT_ENEMY_HP / COMBAT_ENEMY_DAMAGE constants for any unit
    # that declares a faction. Defaults to "roman" so every legacy
    # unit entry (scout, light_infantry, heavy_infantry,
    # heavy_cavalry, bowman, ballista) keeps its current side without
    # touching data/units.json. Modders adding a new hostile type
    # set ``"faction": "barbarian"`` on their JSON entry.
    faction: str = "roman"


class UnitRegistry:
    """Loads unit definitions from JSON and exposes them by id."""

    def __init__(self, defs: dict[str, UnitDef]):
        self._defs = defs

    def __contains__(self, unit_id: str) -> bool:
        return unit_id in self._defs

    def __getitem__(self, unit_id: str) -> UnitDef:
        return self._defs[unit_id]

    def get(self, unit_id: str) -> UnitDef | None:
        return self._defs.get(unit_id)

    def all_ids(self) -> list[str]:
        return list(self._defs.keys())

    def all(self) -> dict[str, UnitDef]:
        return dict(self._defs)

    # ── Construction ─────────────────────────────────────────────────────
    @classmethod
    def from_json_file(cls, path: str | Path) -> "UnitRegistry":
        p = Path(path)
        if not p.is_file():
            log.warning(
                "Unit registry: %s missing — falling back to built-ins", p,
            )
            return cls(_builtin_defs())
        try:
            with p.open() as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(
                "Unit registry: failed to parse %s (%s) — falling back to built-ins",
                p, e,
            )
            return cls(_builtin_defs())
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, dict[str, Any]]) -> "UnitRegistry":
        defs: dict[str, UnitDef] = {}
        for uid, data in raw.items():
            if uid.startswith("_"):  # skip _comment etc.
                continue
            if not isinstance(data, dict):
                continue
            defs[uid] = cls._build_one(uid, data)
        if not defs:
            defs = _builtin_defs()
        log.info("Loaded %d unit definitions", len(defs))
        return cls(defs)

    @staticmethod
    def _build_one(uid: str, data: dict[str, Any]) -> UnitDef:
        return UnitDef(
            id=uid,
            name=str(data.get("name", uid.replace("_", " ").title())),
            category=str(data.get("category", "infantry")),
            hp=int(data.get("hp", 30)),
            damage=int(data.get("damage", 10)),
            speed=float(data.get("speed", _DEFAULT_SPEED)),
            sight=int(data.get("sight", _DEFAULT_SIGHT)),
            patrol_radius=int(data.get("patrol_radius", _DEFAULT_PATROL)),
            ranged=bool(data.get("ranged", False)),
            armoured=bool(data.get("armoured", False)),
            sprite=str(data.get("sprite", uid)),
            description=str(data.get("description", "")),
            # v0.25 fields — every one defaults so legacy entries keep working.
            defense=int(data.get("defense", 0)),
            training_time=int(data.get("training_time", 0)),
            cost=int(data.get("cost", 0)),
            upkeep=int(data.get("upkeep", 0)),
            weapon_cost=int(data.get("weapon_cost", 0)),
            skills=list(data.get("skills", [])),
            faction=str(data.get("faction", "roman")),
        )


def _builtin_defs() -> dict[str, UnitDef]:
    """Hard-coded fallback definitions. Mirrors data/units.json so the
    game runs even when the JSON file is absent or unreadable."""
    return {
        "scout": UnitDef(
            id="scout", name="Scout Squadron", category="light",
            hp=20, damage=6, speed=0.10,
            sight=10, patrol_radius=14,
            ranged=False, armoured=False, sprite="scout",
            description="Fast pathfinders.",
        ),
        "light_infantry": UnitDef(
            id="light_infantry", name="Light Infantry", category="infantry",
            hp=30, damage=10, speed=0.05,
            sight=6, patrol_radius=8,
            ranged=False, armoured=False, sprite="light_infantry",
            description="Standard garrison soldier.",
        ),
        "heavy_infantry": UnitDef(
            id="heavy_infantry", name="Heavy Infantry Legionary",
            category="infantry",
            hp=55, damage=14, speed=0.035,
            sight=5, patrol_radius=6,
            ranged=False, armoured=True, sprite="heavy_infantry",
            description="Heavily armoured legionary.",
        ),
        "heavy_cavalry": UnitDef(
            id="heavy_cavalry", name="Heavy Cavalry", category="cavalry",
            hp=50, damage=18, speed=0.08,
            sight=7, patrol_radius=12,
            ranged=False, armoured=True, sprite="heavy_cavalry",
            description="Mounted shock unit.",
        ),
        "bowman": UnitDef(
            id="bowman", name="Bowman", category="ranged",
            hp=22, damage=11, speed=0.05,
            sight=9, patrol_radius=7,
            ranged=True, armoured=False, sprite="bowman",
            description="Stand-off ranged unit.",
        ),
        # v0.25: ballista — heavy siege-class ranged unit. Slow, high
        # damage, long sight. Spawned by the Military Manufacture.
        "ballista": UnitDef(
            id="ballista", name="Ballista", category="ranged",
            hp=35, damage=22, speed=0.025,
            sight=12, patrol_radius=5,
            ranged=True, armoured=False, sprite="ballista",
            description="Heavy ranged siege engine.",
        ),
        # v0.33: barbarian infantry — the canonical hostile melee
        # raider. Spawned at map edges during raids by the combat
        # manager (in place of the previous hard-coded HP/damage
        # numbers). Stats sit between scout and light_infantry so the
        # raid difficulty matches what v0.32 shipped, but the values
        # now live in JSON and can be balance-tweaked alongside the
        # roman side.
        "barbarian_infantry": UnitDef(
            id="barbarian_infantry", name="Barbarian Infantry",
            category="infantry",
            hp=25, damage=9, speed=0.05,
            sight=6, patrol_radius=8,
            ranged=False, armoured=False, sprite="barbarian_infantry",
            description="Hostile raider. Standard barbarian melee unit.",
            faction="barbarian",
        ),
    }


# Damage multiplier applied to incoming hits when the unit is armoured.
# 0.5 = takes half damage. Tunable as a balance knob, kept here rather
# than in balance.py because it's specifically a unit-system constant.
ARMOURED_INCOMING_DAMAGE_MULT: float = 0.5

# Bowmen / other ranged units engage at Chebyshev <= this distance,
# instead of the default 1 for melee.
RANGED_ATTACK_RANGE: int = 2
