#!/usr/bin/env python3
"""Balance audit — compute supply / demand ratios across the building set.

Run from the project root::

    python tools/balance_audit.py
    python tools/balance_audit.py --pop 200       # sanity-check at a target population
    python tools/balance_audit.py --resource food # zoom in on one resource

What this gives you
-------------------
The numbers you actually want when tuning a city builder are *not* the raw
production/consumption numbers in ``data/buildings.json``. They are:

  * **Per-citizen demand** — how much of resource X does one citizen need
    per tick (read off houses + houses-only consumers).
  * **Per-tile supply** — how much of resource X does one production
    building yield per tick (per *tile* of footprint, so a 2x2 farm and
    a 1x1 well are comparable).
  * **Citizens fed per building** — derived from the two above. This is
    the number you see in your head when you place a farm and ask "how
    many houses can I feed off this?"
  * **Chain efficiency** — for goods that need processing (olives→oil,
    grapes→wine, clay→pottery, iron+wood→tools/weapons), the script
    walks the chain and tells you the *bottleneck* and the implied
    citizens-per-chain.

It also flags **rate cliffs** — resources where one building feeds way
more citizens than the next-tightest in the same chain, which is usually
the root cause of "things are too easy" (a single farm carrying the
whole map) or "things are too hard" (a single missing input starves
five downstream buildings).

How to use it
-------------
You'll typically run this when you want to:

  * Make houses tier up *less* easily — bump per-citizen demand
    (`consumption.food` on house, or add more required goods at higher
    tiers in `HOUSE_TIER_GOODS`).
  * Make a particular industry feel more meaningful — drop its per-tile
    yield, or raise its workers/tile so labour demand competes with
    other industries.
  * Sanity-check a new building before adding it — drop its JSON in,
    rerun the audit, see if it slots into the existing tiers cleanly.

The output is plain text and meant to be diffed across versions: commit
the audit output to a `BALANCE_AUDIT.txt` if you want a human-readable
log of how the economy shape changes release-to-release.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make sure we can import balance.py / constants.py whether the script is
# called from the project root or from inside tools/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from balance import (  # noqa: E402
    BALANCE,
    HOUSE_TIER_GOODS,
    HOUSE_TIER_REQUIREMENTS,
)


def load_buildings() -> dict[str, dict]:
    path = PROJECT_ROOT / "data" / "buildings.json"
    return json.loads(path.read_text())


# ── Per-citizen demand ────────────────────────────────────────────────────
def per_citizen_demand(buildings: dict[str, dict]) -> dict[str, float]:
    """How much of each resource one citizen needs per tick.

    v0.14: food demand is the canonical population-food rule
    (``economy.tick`` deducts ``self.population`` food/tick), i.e.
    1.0 food / citizen / tick. Housing-keyed consumers (barracks,
    taverns, factories don't apply — they consume independently of
    population) still contribute via their ``consumption.X / housing``
    entries; the only such consumer pre-v0.14 was the house's own
    food entry, which was a duplicate of the population drain and
    has been removed from the data.

    v0.16: bread is the new headline staple (the bakery makes it,
    tier-1 houses depend on it). The eating loop in ``economy``
    drains a *pool* of edible goods — bread first, then the other
    nutrients. From the audit's POV, what matters is "what does each
    producer back?" — so we track each edible good as 1.0/citizen/tick
    *separately*. The bakery shows up in the "bread" row at 10
    citizens/building.

    v0.34: removed the legacy "food" demand entry. No building in
    ``data/buildings.json`` writes to ``food`` (farm produces wheat
    → windmill → bakery → bread since v0.16), so listing
    ``food: 1.0/citizen/tick`` on the audit produced a phantom row
    with no producer — a modder reading the report saw "food" as a
    population need with zero satisfying buildings and went hunting
    for the missing building. The cleanup removes the entry; the
    audit now shows ``bread: 1.0/citizen/tick`` as the single
    canonical edible demand.
    """
    out: dict[str, float] = {"bread": 1.0}
    for bid, bd in buildings.items():
        cons = bd.get("consumption") or {}
        housing = bd.get("housing") or 0
        if housing <= 0 or not cons:
            continue
        for r, amt in cons.items():
            out[r] = out.get(r, 0.0) + amt / housing
    return out


# ── Per-tile supply ───────────────────────────────────────────────────────
def per_tile_supply(buildings: dict[str, dict]) -> dict[str, list[tuple[str, float]]]:
    """Per resource → list of ``(building_id, units_per_tile_per_tick)``.

    Sorted descending so the biggest yielder is first — that's the one
    a player will instinctively reach for, so it's the one whose number
    most affects perceived difficulty.
    """
    out: dict[str, list[tuple[str, float]]] = {}
    for bid, bd in buildings.items():
        prod = bd.get("production") or {}
        if not prod:
            continue
        size = bd.get("size", [1, 1])
        tiles = max(1, size[0] * size[1])
        for r, amt in prod.items():
            if r == "happiness":
                continue
            out.setdefault(r, []).append((bid, amt / tiles))
    for r in out:
        out[r].sort(key=lambda x: x[1], reverse=True)
    return out


# ── Citizens fed per building ─────────────────────────────────────────────
def citizens_per_building(
    demand: dict[str, float],
    buildings: dict[str, dict],
) -> dict[str, list[tuple[str, float]]]:
    """For each demanded resource, how many citizens does each producer
    sustain at full capacity?

    Returns ``{resource: [(building_id, citizens_supported), …]}``.
    """
    out: dict[str, list[tuple[str, float]]] = {}
    for r, per_cap in demand.items():
        if per_cap <= 0:
            continue
        for bid, bd in buildings.items():
            prod = bd.get("production") or {}
            if r not in prod:
                continue
            citizens = prod[r] / per_cap
            out.setdefault(r, []).append((bid, citizens))
    for r in out:
        out[r].sort(key=lambda x: x[1], reverse=True)
    return out


# ── Chain analysis (raw → intermediate → finished) ────────────────────────
def chain_bottlenecks(buildings: dict[str, dict]) -> list[dict]:
    """Walk producer → consumer pairs to detect input-starvation chains.

    For every (producer, consumer) edge where ``consumer.consumption[r]``
    is fed by ``producer.production[r]``, compute how many producers it
    takes to saturate one consumer. If that ratio is far from 1.0, it's
    a balance smell (either the chain has too much friction or the
    end-product is a fire-hose).
    """
    edges: list[dict] = []
    for cid, cd in buildings.items():
        cons = cd.get("consumption") or {}
        if not cons:
            continue
        for r, c_amt in cons.items():
            for pid, pd in buildings.items():
                prod = pd.get("production") or {}
                if r not in prod:
                    continue
                p_amt = prod[r]
                ratio = c_amt / p_amt if p_amt > 0 else float("inf")
                edges.append({
                    "resource": r,
                    "producer": pid,
                    "p_per_tick": p_amt,
                    "consumer": cid,
                    "c_per_tick": c_amt,
                    "producers_per_consumer": ratio,
                })
    return edges


# ── Tier requirement summary ──────────────────────────────────────────────
def tier_summary() -> list[str]:
    lines = []
    for tier, reqs in enumerate(HOUSE_TIER_REQUIREMENTS):
        goods = HOUSE_TIER_GOODS[tier] if tier < len(HOUSE_TIER_GOODS) else set()
        cap_mult = (
            BALANCE.house_tier_capacity_mult[tier]
            if tier < len(BALANCE.house_tier_capacity_mult) else 1.0
        )
        tax_mult = (
            BALANCE.house_tier_tax_mult[tier]
            if tier < len(BALANCE.house_tier_tax_mult) else 1.0
        )
        services = ", ".join(f"{k}={v}" for k, v in sorted(reqs.items())) or "—"
        goods_str = ", ".join(sorted(goods)) if goods else "—"
        lines.append(
            f"  tier {tier}: services={services}; goods={goods_str}; "
            f"cap_mult={cap_mult}; tax_mult={tax_mult}"
        )
    return lines


# ── Pretty-printing ───────────────────────────────────────────────────────
SEP = "─" * 70


def print_section(title: str) -> None:
    print(SEP)
    print(f"  {title}")
    print(SEP)


def fmt_float(x: float) -> str:
    if x == float("inf"):
        return "∞"
    if x >= 100:
        return f"{x:.0f}"
    if x >= 10:
        return f"{x:.1f}"
    return f"{x:.2f}"


def main() -> None:
    p = argparse.ArgumentParser(description="Audit Caesar3 clone economy balance.")
    p.add_argument("--pop", type=int, default=100,
                   help="Target population to project total demand against.")
    p.add_argument("--resource", type=str, default=None,
                   help="Restrict analysis to one resource (e.g. food).")
    args = p.parse_args()

    buildings = load_buildings()

    # 1. Demand ────────────────────────────────────────────────────────────
    print_section(f"Per-citizen demand  ·  pop={args.pop}")
    demand = per_citizen_demand(buildings)
    if not demand:
        print("  (no housing-keyed consumers found in JSON)")
    else:
        for r in sorted(demand):
            if args.resource and r != args.resource:
                continue
            per_cap = demand[r]
            total = per_cap * args.pop
            print(f"  {r:10s}  per_citizen={fmt_float(per_cap)}/tick  "
                  f"total_at_pop={fmt_float(total)}/tick")

    # 2. Supply per tile ──────────────────────────────────────────────────
    print_section("Per-tile production  ·  one production tile = 1 row of footprint")
    supply = per_tile_supply(buildings)
    for r in sorted(supply):
        if args.resource and r != args.resource:
            continue
        print(f"  {r}:")
        for bid, per_tile in supply[r]:
            bd = buildings[bid]
            sz = bd.get("size", [1, 1])
            workers = bd.get("workers", 0)
            cons = bd.get("consumption") or {}
            cons_str = (
                "; needs " + ", ".join(f"{k}:{v}" for k, v in cons.items())
                if cons else ""
            )
            print(f"     {bid:18s} {sz[0]}x{sz[1]}  {fmt_float(per_tile)}/tile/tick  "
                  f"(workers={workers}{cons_str})")

    # 3. Citizens supported per building ───────────────────────────────────
    print_section("Citizens supported by ONE building at full capacity")
    cps = citizens_per_building(demand, buildings)
    for r in sorted(cps):
        if args.resource and r != args.resource:
            continue
        print(f"  {r}:")
        for bid, citizens in cps[r]:
            print(f"     {bid:18s}  {fmt_float(citizens):>6s} citizens")

    # 4. Buildings needed at target pop ────────────────────────────────────
    print_section(f"Buildings needed for pop={args.pop} (1.0× and 1.5× safety margin)")
    for r in sorted(demand):
        if args.resource and r != args.resource:
            continue
        if r not in cps or not cps[r]:
            print(f"  {r}: NO PRODUCER FOUND in registry  ← gap!")
            continue
        biggest_bid, biggest_citizens = cps[r][0]
        if biggest_citizens <= 0:
            continue
        n_min = args.pop / biggest_citizens
        n_safe = n_min * 1.5
        print(f"  {r:10s}  best producer: {biggest_bid:14s}  "
              f"need ≥ {fmt_float(n_min)} (safe: {fmt_float(n_safe)})")

    # 5. Chain analysis ────────────────────────────────────────────────────
    print_section("Production chains  ·  producers per consumer (1.0 = balanced)")
    edges = chain_bottlenecks(buildings)
    if args.resource:
        edges = [e for e in edges if e["resource"] == args.resource]
    for e in sorted(edges, key=lambda x: (x["resource"], x["consumer"])):
        ratio = e["producers_per_consumer"]
        flag = ""
        if ratio == float("inf"):
            flag = "  ← consumer needs an input no producer makes"
        elif ratio > 2.0:
            flag = "  ← bottleneck (need many producers per consumer)"
        elif ratio < 0.25:
            flag = "  ← oversupply (one producer feeds many consumers)"
        print(f"  {e['resource']:10s}  {e['producer']:18s} →  "
              f"{e['consumer']:18s}  ratio={fmt_float(ratio)}{flag}")

    # 6. Tier setup ────────────────────────────────────────────────────────
    print_section("House tier requirements (from balance.py)")
    for line in tier_summary():
        print(line)

    print_section("Done")
    print("  Tip: rerun with --resource food (or wood, oil, …) to focus.")
    print("  Edit data/buildings.json and balance.py, then rerun for a diff.")


if __name__ == "__main__":
    main()
