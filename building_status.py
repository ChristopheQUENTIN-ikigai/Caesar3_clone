"""Per-building activity status — v0.12.

The economy until v0.11 had a binary connectivity gate (a disconnected
producer skips both production and consumption) and a gradient
workforce gate (efficiency = pop / workers_needed). What it didn't have
was an *input gate*: a tavern with no wine still emitted happiness, a
sawmill with no wood still emitted planks. Their consumption block
silently floored at zero each tick; their production was unaffected.

v0.12 adds the input gate and a per-building status snapshot that the
inspector, the flow graph, and the stats panel can all read. The
economy populates a single dict each tick:

    economy.building_status[(row, col)] = {
        "state": "active" | "partial" | "disconnected" | "unstaffed" |
                 "starved" | "idle",
        "throttle": float,            # 0.0..1.0; effective output multiplier
        "missing_inputs": list[str],  # goods at zero supply
        "workers_filled": int,        # citizens actually working here
        "workers_needed": int,        # the building's worker slot count
        "reason": str,                # human-readable label for the inspector
    }

State semantics:
  * **active** — at full output. throttle == 1.0, all inputs supplied,
    all worker slots filled, road link OK. The "everything green" state.
  * **partial** — running but throttled. throttle in (0, 1). At least one
    contributing factor (workers, inputs) is below 1.0; nothing is at
    zero. The inspector shows the limiting factor.
  * **disconnected** — requires_road but no road link. throttle == 0.
    Treated as if the building doesn't exist for production / consumption
    / worker demand purposes.
  * **unstaffed** — workers slot non-zero but population can't fill any.
    throttle == 0. Different reason from disconnected so the inspector
    can advise differently ("hire more citizens" vs "build a road").
  * **starved** — at least one consumed input has zero supply. throttle
    == 0. The economy doesn't draw any of the available inputs either —
    starvation is binary. Output is zero this tick.
  * **idle** — no production, no consumption (e.g. roads, wells, the
    tower). Always "active" in the sense that nothing's broken; we use a
    distinct label so the graph can grey them out without implying a
    problem.

This module is pure logic. The status dict is a plain Python dict so
the inspector can mutate `reason` strings if needed without coupling.

Worker accounting is also exposed: the per-building snapshot says how
many workers this building has, and how many are currently filled. The
"how many are filled" calculation is intentionally simple — global
employment is computed by the economy as ``min(pop, total_demand)``,
and we distribute that pool to buildings in the same order they appear
in the positioned list. A more sophisticated version would prefer
buildings by category (e.g. food first, then military) — left as a
future tuning knob.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


# Public state strings. Kept as constants so the inspector / graph /
# tests don't typo them.
ACTIVE = "active"
PARTIAL = "partial"
DISCONNECTED = "disconnected"
UNSTAFFED = "unstaffed"
STARVED = "starved"
IDLE = "idle"


@dataclass
class BuildingStatus:
    """Per-building activity snapshot for one tick."""

    state: str = IDLE
    throttle: float = 0.0
    missing_inputs: list[str] = field(default_factory=list)
    workers_filled: int = 0
    workers_needed: int = 0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "throttle": self.throttle,
            "missing_inputs": list(self.missing_inputs),
            "workers_filled": self.workers_filled,
            "workers_needed": self.workers_needed,
            "reason": self.reason,
        }


def supply_ratio(have: float, need: float) -> float:
    """How much of one input's demand can be satisfied by the global pool.

    Returns a value in [0, 1]. ``need == 0`` returns 1.0 (no demand
    means the input doesn't gate anything). ``have >= need`` returns
    1.0 (fully supplied). ``have == 0`` returns 0.0 regardless of
    need (starvation is binary at zero — even a partial supply
    triggers throttling, not full stop).

    Why this shape rather than ``have / need`` linear? Because the
    economy already computes consumption-scaled-by-efficiency. If
    output were ``min(eff, have/need)`` and ``have`` were 5 against
    a need of 10, output would be 50% — but consumption would also
    be 50% (5 units), which exactly empties the pool. That's the
    right answer for a steady-state simulation.
    """
    if need <= 0:
        return 1.0
    if have <= 0:
        return 0.0
    return min(1.0, have / need)


def label_for_state(
    state: str,
    missing_inputs: list[str],
    workers_filled: int,
    workers_needed: int,
) -> str:
    """Build a human-readable reason string for the inspector."""
    if state == ACTIVE:
        return "Active"
    if state == DISCONNECTED:
        return "Idle: no road link"
    if state == UNSTAFFED:
        return "Idle: no workers"
    if state == STARVED:
        if missing_inputs:
            return f"Idle: no {', '.join(missing_inputs)}"
        return "Idle: input shortage"
    if state == PARTIAL:
        bits = []
        if workers_needed > 0 and workers_filled < workers_needed:
            bits.append(
                f"{workers_filled}/{workers_needed} workers"
            )
        if missing_inputs:
            bits.append(f"low {', '.join(missing_inputs)}")
        if not bits:
            return "Partial output"
        return "Partial: " + ", ".join(bits)
    if state == IDLE:
        return "—"
    return state


def classify(
    *,
    requires_road: bool,
    connected: bool,
    workers_needed: int,
    workers_filled: int,
    consumption: dict[str, int],
    available: Callable[[str], float],
    has_production: bool,
) -> BuildingStatus:
    """Decide a single building's status from its inputs.

    ``available(good)`` returns the global pool's current quantity for
    one good. We don't *commit* the consumption here — the caller is
    responsible for actually subtracting from the pool — we just look
    at what's there.

    Order of checks matters:

    1. Disconnected — short-circuits everything else, exactly as the
       v0.7 connectivity gate did.
    2. No production AND no consumption → idle (passive infrastructure).
    3. Unstaffed — workers needed but zero filled.
    4. Starved — at least one consumed input has zero supply.
    5. Partial — workers below capacity OR an input below 1.0 ratio.
    6. Active — everything at full.

    The function returns a populated BuildingStatus; it doesn't decide
    whether or how the economy should apply the throttle (that's the
    caller's choice — the economy multiplies production AND
    consumption by ``throttle``).
    """
    if requires_road and not connected:
        return BuildingStatus(
            state=DISCONNECTED,
            throttle=0.0,
            workers_filled=0,
            workers_needed=workers_needed,
            reason=label_for_state(DISCONNECTED, [], 0, workers_needed),
        )

    has_consumption = any(v > 0 for v in consumption.values())
    if not has_production and not has_consumption and workers_needed == 0:
        # Pure infrastructure — well, road, tower. Always "fine".
        return BuildingStatus(
            state=IDLE,
            throttle=1.0,
            workers_filled=0,
            workers_needed=0,
            reason=label_for_state(IDLE, [], 0, 0),
        )

    # Worker throttle.
    if workers_needed > 0 and workers_filled == 0:
        return BuildingStatus(
            state=UNSTAFFED,
            throttle=0.0,
            workers_filled=0,
            workers_needed=workers_needed,
            reason=label_for_state(UNSTAFFED, [], 0, workers_needed),
        )
    worker_ratio = (
        workers_filled / workers_needed if workers_needed > 0 else 1.0
    )

    # Input throttle. Money is checked here too — a building that
    # needs 'money' to operate (theatres, temples, schools) but is
    # consuming from an empty treasury is functionally starved. The
    # treasury "available" is supplied by the caller via available().
    missing: list[str] = []
    input_ratio = 1.0
    for good, need in consumption.items():
        if need <= 0:
            continue
        avail = available(good)
        ratio = supply_ratio(avail, need)
        if ratio <= 0.0:
            missing.append(good)
        input_ratio = min(input_ratio, ratio)

    if missing:
        return BuildingStatus(
            state=STARVED,
            throttle=0.0,
            missing_inputs=missing,
            workers_filled=workers_filled,
            workers_needed=workers_needed,
            reason=label_for_state(STARVED, missing, workers_filled, workers_needed),
        )

    throttle = min(worker_ratio, input_ratio)
    if throttle >= 1.0 - 1e-9:
        return BuildingStatus(
            state=ACTIVE,
            throttle=1.0,
            workers_filled=workers_filled,
            workers_needed=workers_needed,
            reason=label_for_state(ACTIVE, [], workers_filled, workers_needed),
        )
    # Partial: report the *most-throttled* input as a hint.
    partial_inputs: list[str] = []
    for good, need in consumption.items():
        if need <= 0:
            continue
        avail = available(good)
        if supply_ratio(avail, need) < 1.0:
            partial_inputs.append(good)
    return BuildingStatus(
        state=PARTIAL,
        throttle=throttle,
        missing_inputs=partial_inputs,
        workers_filled=workers_filled,
        workers_needed=workers_needed,
        reason=label_for_state(
            PARTIAL, partial_inputs, workers_filled, workers_needed,
        ),
    )
