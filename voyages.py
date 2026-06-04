"""Voyages — per-TRIP inter-city sea commerce. (v0.51)

Where ``commercial_roads.py`` runs *persistent per-tick* trade routes
(a steady drip of goods → gold every single tick), this module models
the brief's stricter rule: **inter-city goods trading requires free
commercial ships to carry stuff, and the exchange of goods for gold
happens per TRIP, not per tick.**

A *voyage* is one round trip of one free commercial ship: it loads up
to ``VOYAGE_SHIP_CAPACITY`` units of a good at the commercial port,
sails to a linked foreign city, sells the cargo for gold, and sails
home. The gold lands once, when the trip *completes* — never per tick.

The four departure conditions (all from the brief)
---------------------------------------------------

A voyage only *departs* when every one of these holds, checked against a
``VoyageWorld`` snapshot the caller supplies each tick:

  1. **A free commercial ship is available.** The city's commercial
     harbours shelter idle trade ships; ``ships_available`` counts the
     berths not already out on a voyage. No free ship → no departure.
  2. **The good is in stock at the commercial port** *before* the ship
     departs: ``port_stock(good) >= 1``. The ship loads
     ``min(capacity, stock)`` — a partial hold is fine, an empty one
     never sails.
  3. **The destination is safe** — ``no_war_at_destination(city_id)``.
     A city at war refuses the merchantman; the trip is held.
  4. **The sea road is clear of pirates** — ``no_piracy_on_route()``.
     Enemy ships at sea (raiders) close the lane for everyone until
     they're dealt with.

The interval (distance-scaled cooldown)
---------------------------------------

Even with a free ship and clear seas, a city can't dispatch again until
its previous voyage's there-and-back sailing time has elapsed. That
cooldown is ``city_interval(city_id)`` ticks:

    VOYAGE_INTERVAL_BASE + VOYAGE_INTERVAL_PER_DISTANCE * distance

so a near city cycles fast and a far one ties a ship up for much longer.
This is what makes "interval depends on distance" concrete.

Design — why a separate module (and why pure)
---------------------------------------------

Same split as ``bartering`` / ``gold_trade`` / ``commercial_roads``: the
*logic* (when a trip may leave, how much it carries, what it earns, how
the cooldown scales, the save shape) lives here with **no arcade import
and no direct world access**, so it's unit-testable headless and a
modder can rebalance commerce without touching engine or window code.
The window owns a thin ``VoyageWorld`` adapter that answers the four
condition questions from live game state (harbours, port buffer, enemy
walkers); this module just consumes that interface.

Public surface
--------------

  VoyageWorld (Protocol-ish duck type the caller implements)
      ships_available() -> int
      port_stock(good)  -> int
      take_from_port(good, qty) -> int     # debit; returns amount taken
      no_war_at_destination(city_id) -> bool
      no_piracy_on_route() -> bool

  CityVoyageConfig
      Per-city player config: which good to ship, enabled flag.

  VoyageManager(economy, road_manager)
      .configure(city_id, good=..., enabled=...)
      .update(world)        # called once per tick; may launch/complete trips
      .active_voyages       # list[ActiveVoyage] in flight
      .last_completed       # list[dict] records, most-recent first (HUD)
      .lifetime_gold        # total gold earned by all completed voyages
      .to_dict() / .from_dict(d)

The completed-trip record dict (also what the recorder/HUD reads)::

    {
      "city":   str,    # destination city id
      "good":   str,
      "qty":    int,    # units actually delivered/sold
      "gold":   int,    # gold credited to the treasury this trip
      "tick":   int,    # tick the trip completed
    }
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from constants import (
    VOYAGE_INTERVAL_BASE,
    VOYAGE_INTERVAL_PER_DISTANCE,
    VOYAGE_PROFIT_FRACTION,
    VOYAGE_SHIP_CAPACITY,
)
from commercial_roads import city_meta, city_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from economy import EconomyManager
    from commercial_roads import CommercialRoadManager

log = logging.getLogger("caesar3.voyages")

# How many completed-trip records to keep for the HUD / finance panel.
# Older records fall off the end — this is a display ring buffer, not a
# ledger (lifetime totals are tracked separately and never truncate).
_RECENT_KEEP = 12


def city_interval(city_id: str) -> int:
    """Round-trip cooldown in ticks for ``city_id`` — the modelled
    there-and-back sailing time. Scales with the city's ``distance`` so
    a far city ties a ship up far longer. Unknown cities fall back to
    the base interval (distance 0)."""
    meta = city_meta(city_id)
    dist = int(meta["distance"]) if meta and "distance" in meta else 0
    return max(1, VOYAGE_INTERVAL_BASE + VOYAGE_INTERVAL_PER_DISTANCE * dist)


def voyage_unit_price(good: str, city_id: str) -> int:
    """Gold the foreign market pays per unit for ``good`` shipped to
    ``city_id``. Reads ``bartering.STOCK_PRICES`` (the shared price
    table) scaled by ``VOYAGE_PROFIT_FRACTION`` and the city's
    ``profit_mult``. Floors at 1 so a trip is never literally free money
    nor a zero-value sail."""
    from bartering import STOCK_PRICES
    base = STOCK_PRICES.get(good, 1)
    meta = city_meta(city_id)
    mult = float(meta["profit_mult"]) if meta else 1.0
    return max(1, int(round(base * VOYAGE_PROFIT_FRACTION * mult)))


class CityVoyageConfig:
    """Per-city player configuration for the voyage layer.

    ``good`` is the resource the city's voyages ship; ``enabled`` lets
    the player pause a city's commerce without unconfiguring it. A city
    with no config (or ``good`` unset) never dispatches.
    """

    def __init__(self, good: str = "", enabled: bool = True):
        self.good: str = str(good)
        self.enabled: bool = bool(enabled)

    def to_dict(self) -> dict:
        return {"good": self.good, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict) -> "CityVoyageConfig":
        return cls(good=str(d.get("good", "")),
                   enabled=bool(d.get("enabled", True)))


class ActiveVoyage:
    """One commercial ship in flight on a round trip.

    Created at *departure* with the cargo already loaded (debited from
    the port buffer) and an ``arrive_tick`` = launch + the city's
    interval. The manager completes it on the tick the clock reaches
    ``arrive_tick``: the cargo is sold for gold and the record archived.
    Carrying the gold computation forward (rather than locking it at
    launch) would let a mid-flight price change apply; we deliberately
    *lock the unit price at load time* so a trip's payout is a contract
    struck when the goods left the dock — simpler to reason about and to
    test, and it matches the brief's "exchange per trip" framing.
    """

    def __init__(self, city_id: str, good: str, qty: int,
                 unit_price: int, launch_tick: int, arrive_tick: int):
        self.city_id = city_id
        self.good = good
        self.qty = int(qty)
        self.unit_price = int(unit_price)
        self.launch_tick = int(launch_tick)
        self.arrive_tick = int(arrive_tick)

    @property
    def gold(self) -> int:
        """Locked-in payout: cargo size × unit price at load time."""
        return self.qty * self.unit_price

    def to_dict(self) -> dict:
        return {
            "city": self.city_id, "good": self.good, "qty": self.qty,
            "unit_price": self.unit_price,
            "launch_tick": self.launch_tick, "arrive_tick": self.arrive_tick,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ActiveVoyage":
        return cls(
            city_id=str(d["city"]), good=str(d["good"]),
            qty=int(d.get("qty", 0)), unit_price=int(d.get("unit_price", 1)),
            launch_tick=int(d.get("launch_tick", 0)),
            arrive_tick=int(d.get("arrive_tick", 0)),
        )


class VoyageManager:
    """Owns per-city voyage configs, the in-flight voyages, and the
    cooldown clocks. Driven once per tick by ``update(world)``.

    The manager holds a reference to the ``CommercialRoadManager`` only
    to read which cities are *linked* — a voyage can only run to a
    linked city (you must open the commercial road first, exactly like
    the per-tick routes). It does NOT touch the road manager's per-tick
    routes; the two layers coexist (a city can run both a steady drip
    and discrete trips, though typically a player picks one).
    """

    def __init__(self, economy: "EconomyManager",
                 road_manager: "CommercialRoadManager"):
        self.economy = economy
        self.roads = road_manager
        # city_id -> CityVoyageConfig
        self.configs: dict[str, CityVoyageConfig] = {}
        # city_id -> tick the city is free to dispatch again. Absent /
        # past tick means "ready now".
        self.ready_tick: dict[str, int] = {}
        # Voyages currently at sea, in launch order.
        self.active_voyages: list[ActiveVoyage] = []
        # Ring buffer of recent completed-trip records (most recent
        # first) for the HUD; lifetime_gold is the never-truncated total.
        self.last_completed: list[dict] = []
        self.lifetime_gold: int = 0
        self.lifetime_trips: int = 0
        # The tick clock the manager reads. The owner advances this each
        # tick (or passes it to update); kept here so save/load and the
        # cooldown maths have a single source.
        self.tick: int = 0

    # ── Config ────────────────────────────────────────────────────────
    def configure(self, city_id: str, *, good: str | None = None,
                  enabled: bool | None = None) -> CityVoyageConfig:
        """Create or update a city's voyage config. Returns the config.
        Passing ``good``/``enabled`` None leaves that field unchanged."""
        cfg = self.configs.get(city_id)
        if cfg is None:
            cfg = CityVoyageConfig()
            self.configs[city_id] = cfg
        if good is not None:
            cfg.good = str(good)
        if enabled is not None:
            cfg.enabled = bool(enabled)
        return cfg

    def config_for(self, city_id: str) -> CityVoyageConfig | None:
        return self.configs.get(city_id)

    def clear_city(self, city_id: str) -> None:
        """Drop a city's config and cooldown (called when it's unlinked).
        In-flight voyages already launched still complete — the ship is
        at sea and will return; we don't strand its cargo."""
        self.configs.pop(city_id, None)
        self.ready_tick.pop(city_id, None)

    # ── Per-tick drive ────────────────────────────────────────────────
    def update(self, world) -> list[dict]:  # noqa: ANN001 - duck-typed world
        """Advance the voyage layer one tick.

        Order matters: we **complete arrivals first** (freeing the ships
        they tie up) and then **consider new departures** — so a ship
        that returns this tick can immediately sail again if its city's
        cooldown has elapsed. Returns the list of completed-trip records
        produced this tick (possibly empty) so the caller can surface
        notifications.

        v0.52: ``world`` may optionally expose two notification hooks so
        the engine can mirror each abstract voyage with a *visible*
        ``TradeShip`` on the water (see game_window ``_voyage_world``):

          * ``on_voyage_launched(voyage)`` — called once when a trip
            departs, after its cargo is debited and it's added to
            ``active_voyages``. The engine spawns a sailing TradeShip.
          * ``on_voyage_completed(record, voyage)`` — called once when a
            trip's round trip elapses, after the gold is credited. The
            engine retires / turns the matching ship home.

        Both are *optional* — the manager stays pure logic and headless-
        testable; it only calls a hook when the duck-typed world provides
        one (``getattr(world, name, None)``). A world without the hooks
        (every existing test's fake world) behaves exactly as before.
        """
        self.tick += 1
        completed = self._complete_arrivals(world)
        self._consider_departures(world)
        return completed

    def _complete_arrivals(self, world=None) -> list[dict]:  # noqa: ANN001
        """Sell the cargo of every voyage whose ``arrive_tick`` has come.
        Credits the treasury once per trip and archives a record."""
        if not self.active_voyages:
            return []
        done: list[ActiveVoyage] = [
            v for v in self.active_voyages if self.tick >= v.arrive_tick
        ]
        if not done:
            return []
        records: list[dict] = []
        # v0.52: optional engine hook to retire the visible TradeShip
        # that mirrored this voyage. Resolved once; a world without it
        # (headless tests) leaves this None and nothing visible happens.
        on_completed = getattr(world, "on_voyage_completed", None)
        for v in done:
            gold = v.gold
            self.economy.treasury += gold
            self.lifetime_gold += gold
            self.lifetime_trips += 1
            rec = {
                "city": v.city_id, "good": v.good, "qty": v.qty,
                "gold": int(gold), "tick": self.tick,
            }
            records.append(rec)
            self.last_completed.insert(0, rec)
            log.info(
                "Voyage complete: %d %s → %s for +%d dn",
                v.qty, v.good, v.city_id, gold,
            )
            if on_completed is not None:
                try:
                    on_completed(rec, v)
                except Exception:  # noqa: BLE001 - a cosmetic hook never
                    # breaks the economic core: a failed ship-retire must
                    # not lose the gold credit or desync the ledger.
                    log.exception("on_voyage_completed hook failed")
        # Trim the HUD ring buffer.
        if len(self.last_completed) > _RECENT_KEEP:
            del self.last_completed[_RECENT_KEEP:]
        # Drop completed voyages from the in-flight list.
        arrived = set(id(v) for v in done)
        self.active_voyages = [
            v for v in self.active_voyages if id(v) not in arrived
        ]
        return records

    def _consider_departures(self, world) -> None:  # noqa: ANN001
        """For each linked, enabled, configured, ready city: try to
        dispatch one voyage if all four conditions hold. At most one
        departure per city per tick (the cooldown then gates the next).
        Cities are considered in a stable order so behaviour is
        deterministic and testable."""
        # Sea-wide gate first: piracy closes the lane for *everyone*. One
        # check spares us re-asking per city.
        if not world.no_piracy_on_route():
            return
        for city_id in self._dispatch_order():
            cfg = self.configs.get(city_id)
            if cfg is None or not cfg.enabled or not cfg.good:
                continue
            if not self.roads.is_linked(city_id):
                continue
            # Cooldown: city must be free to dispatch this tick.
            if self.tick < self.ready_tick.get(city_id, 0):
                continue
            # Condition: destination not at war.
            if not world.no_war_at_destination(city_id):
                continue
            # Condition: a free commercial ship available right now.
            if world.ships_available() <= 0:
                continue
            # Condition: good in stock at the commercial port.
            good = cfg.good
            stock = world.port_stock(good)
            if stock <= 0:
                continue
            # Load the hull: up to capacity, capped by available stock.
            qty = min(VOYAGE_SHIP_CAPACITY, int(stock))
            taken = world.take_from_port(good, qty)
            if taken <= 0:
                # Race / adapter said no — skip without arming cooldown.
                continue
            unit = voyage_unit_price(good, city_id)
            interval = city_interval(city_id)
            voyage = ActiveVoyage(
                city_id=city_id, good=good, qty=taken, unit_price=unit,
                launch_tick=self.tick, arrive_tick=self.tick + interval,
            )
            self.active_voyages.append(voyage)
            # Arm the cooldown: the city can't dispatch again until the
            # round trip's interval elapses.
            self.ready_tick[city_id] = self.tick + interval
            log.info(
                "Voyage launched: %d %s → %s (arrive @%d, +%d dn on return)",
                taken, good, city_id, voyage.arrive_tick, voyage.gold,
            )
            # v0.52: optional engine hook to spawn a *visible* TradeShip
            # mirroring this trip. The manager stays pure: it just calls
            # the hook if the duck-typed world provides one.
            on_launched = getattr(world, "on_voyage_launched", None)
            if on_launched is not None:
                try:
                    on_launched(voyage)
                except Exception:  # noqa: BLE001 - a failed visual spawn
                    # must never undo the launch / cargo debit.
                    log.exception("on_voyage_launched hook failed")

    def _dispatch_order(self) -> list[str]:
        """Stable per-tick consideration order: configured cities sorted
        by id. Deterministic so tests and replays agree."""
        return sorted(self.configs.keys())

    # ── Read helpers (HUD / finance panel) ─────────────────────────────
    def active_count(self) -> int:
        return len(self.active_voyages)

    def summary_lines(self) -> list[str]:
        """Human-readable lines for an info panel: one per configured
        city plus an in-flight tally."""
        lines: list[str] = []
        for city_id in sorted(self.configs.keys()):
            cfg = self.configs[city_id]
            name = city_name(city_id)
            if not cfg.good:
                lines.append(f"{name}: (no good set)")
                continue
            state = "on" if cfg.enabled else "paused"
            interval = city_interval(city_id)
            unit = voyage_unit_price(cfg.good, city_id)
            lines.append(
                f"{name}: {cfg.good} every ~{interval}t "
                f"@{unit} dn/u [{state}]"
            )
        if self.active_voyages:
            lines.append(f"At sea: {len(self.active_voyages)} voyage(s)")
        return lines

    # ── Persistence ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        """JSON-ready snapshot: configs, cooldowns, in-flight voyages,
        lifetime tallies, and the tick clock. The HUD ring buffer
        (``last_completed``) is display-only and intentionally NOT
        persisted — it rebuilds as trips complete after load."""
        return {
            "configs": {cid: c.to_dict() for cid, c in self.configs.items()},
            "ready_tick": dict(self.ready_tick),
            "active": [v.to_dict() for v in self.active_voyages],
            "lifetime_gold": int(self.lifetime_gold),
            "lifetime_trips": int(self.lifetime_trips),
            "tick": int(self.tick),
        }

    def from_dict(self, d: dict) -> None:
        """Restore from a snapshot. Idempotent — fully replaces state.
        Tolerates a missing / partial dict (older saves) by resetting to
        an empty voyage layer."""
        d = d or {}
        self.configs = {}
        for cid, cd in (d.get("configs") or {}).items():
            try:
                self.configs[cid] = CityVoyageConfig.from_dict(cd)
            except Exception:  # noqa: BLE001 - skip malformed entries
                continue
        self.ready_tick = {
            str(k): int(v) for k, v in (d.get("ready_tick") or {}).items()
        }
        self.active_voyages = []
        for vd in (d.get("active") or []):
            try:
                self.active_voyages.append(ActiveVoyage.from_dict(vd))
            except Exception:  # noqa: BLE001
                continue
        self.lifetime_gold = int(d.get("lifetime_gold", 0))
        self.lifetime_trips = int(d.get("lifetime_trips", 0))
        self.tick = int(d.get("tick", 0))
        self.last_completed = []
