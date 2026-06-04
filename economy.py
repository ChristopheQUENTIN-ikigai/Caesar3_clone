"""Economy Manager — resources, taxes, production, population, happiness.

Pure simulation: no arcade imports. Takes a `BuildingRegistry` (so the
production / consumption / housing / storage / workers values come from the
data file) and a `Balance` (so all tuning numbers are in one place).

Public API:
    EconomyManager(registry, balance=BALANCE)
    .update(building_ids: list[str])      run one tick
    .can_afford(cost) / .spend(cost)
    .cycle_tax_rate()
    .apply_event_effects(effects, pop, happy)
    .get_status_lines()                    HUD strings
    .to_dict() / .from_dict(d)             save / load
"""
from __future__ import annotations

import logging
from typing import Iterable

from balance import BALANCE, Balance
from building import BuildingRegistry
from constants import TAX_RATES

log = logging.getLogger("caesar3.economy")


class EconomyManager:
    def __init__(self, registry: BuildingRegistry, balance: Balance = BALANCE):
        self.registry = registry
        self.balance = balance

        # Resources (food/wood/iron/tools). 'money' is treasury; 'happiness'
        # is its own state, never stored here.
        # v0.11: wheat/flour/planks are intermediate goods on the new
        # chain. Pre-stocking them makes the first ~10 ticks pleasant
        # while producers come online.
        # v0.13: stone (raw, from quarry) and stone_blocks (from
        # stonemason) added for the construction-material gate. Houses
        # need 1 plank to build; civic buildings need stone_blocks; a
        # senate needs both. Starter inventory covers ~5-10 buildings.
        # v0.16: bread is the new headline staple (the bakery makes it).
        # The starter stock is small — a few ticks of subsistence so
        # the new nutrient layer doesn't feel empty on game start, but
        # not so generous that the player ignores building a bakery.
        self.resources: dict[str, float] = {
            "food":         balance.start_food,
            "wood":         balance.start_wood,
            "iron":         balance.start_iron,
            # v0.22: iron_ore is the mine's new output; the smelter
            # refines it into iron. start_iron is kept (small buffer of
            # already-refined iron) so the weapon smith / factory can
            # start producing before the smelter is built. start_weapons
            # arms the first few soldiers from a freshly placed barracks
            # — see balance.py for the rationale.
            "iron_ore":     balance.start_iron_ore,
            "weapons":      balance.start_weapons,
            "tools":        balance.start_tools,
            "wheat":        balance.start_wheat,
            "flour":        balance.start_flour,
            "planks":       balance.start_planks,
            "stone":        balance.start_stone,
            "stone_blocks": balance.start_stone_blocks,
            "bread":        balance.start_bread,
        }
        self.treasury: float = balance.start_treasury
        self.tax_rate_index = 0
        self.tax_rate = TAX_RATES[0]
        self.population: int = balance.start_population
        self.happiness: float = balance.start_happiness

        # Derived (recomputed each tick by _calc_production).
        self.employed: int = 0
        self.housing_capacity: int = 0
        self.storage_capacity: int = balance.base_storage

        # Telemetry for HUD / tests.
        self.tick_count: int = 0
        self.income_per_tick: float = 0.0
        self.expenses_per_tick: float = 0.0
        # v0.42: portion of expenses that is flat building upkeep (gold/
        # tick), tracked separately for the inspector/stats if wanted.
        self.upkeep_per_tick: float = 0.0
        # v0.51: granular per-tick finance accounting for the '$' budget
        # panel. Each key is a named income or expense line; values are
        # gold/tick from the LAST completed tick. ``income_per_tick`` and
        # ``expenses_per_tick`` remain the rolled-up totals (HUD 'Cost'
        # line) — this dict just decomposes them so the budget panel can
        # show *where* the gold comes from and goes. Trade-route and
        # voyage income are injected by the owning game window after the
        # economy tick (they live outside the economy), so those keys may
        # be absent on a pure-economy test that never calls the injector.
        self.finance_breakdown: dict[str, float] = {
            "tax": 0.0,            # citizen taxes
            "production": 0.0,     # money-producing buildings (banks, etc.)
            "wages": 0.0,          # worker wage bill
            "upkeep": 0.0,         # flat per-building gold upkeep
            "consumption": 0.0,    # buildings that consume 'money'
        }
        self.food_balance: float = 0.0
        # v0.8: fraction of citizens fed this tick. 1.0 = everyone ate;
        # 0.0 = total famine. Read by the rebellion system and by the
        # tax block (tax scales with this).
        self.fed_fraction: float = 1.0
        # v0.16: how many of the 10 nutrients the city currently has
        # in stock (count of distinct nutrients with qty > 0). Drives
        # the diversity bonus: when this hits 10, happiness target
        # gets a flat bonus and pop growth is a touch faster. Updated
        # each tick at the end of `update`. Stays a plain int (not a
        # float) so HUD code can read it directly.
        self.nutrient_diversity: int = 0
        # v0.21: latest breakdown of the happiness-target equation,
        # stashed each tick so the 'Z' debug overlay can surface every
        # term (base, tax_pen, food_bon, house_bon, …) without the UI
        # having to re-derive them. Empty dict before the first tick;
        # the panel falls back to a "no data yet" message in that case.
        self.last_happiness_breakdown: dict[str, float] = {}
        # Average tax multiplier across houses (set per tick by _calc_production).
        self._tax_mult_avg: float = 1.0
        # v0.13: wage payment ratio. 1.0 means last tick's wages were
        # paid in full; <1.0 means the treasury couldn't cover them and
        # this tick's effective workforce is reduced proportionally.
        # Read by _calc_production at the start of each tick; written
        # by the tax/wage block at the end.
        self.wage_payment_ratio: float = 1.0
        # v0.12: per-building activity status keyed by (row, col). Populated
        # each tick by _calc_production and read by the inspector, the flow
        # graph, and the stats panel. Empty when called via the legacy
        # positionless API (no row/col available to key on). Each value is
        # a BuildingStatus dataclass — see building_status.py for the
        # state semantics.
        from building_status import BuildingStatus
        self.building_status: dict[tuple[int, int], BuildingStatus] = {}
        # v0.15: gross production this tick (pre-storage-cap). The HUD
        # food balance line and the stats panel read this so a city
        # with no storage headroom still displays the producers'
        # output honestly. Populated each tick by `update`.
        self.gross_production: dict[str, float] = {}
        # v0.35: per-nutrient eaten this tick. The eat-loop drains the
        # nutrient pool in priority order; this dict captures the
        # breakdown so the right-panel "Satiety" block can report
        # delivery coverage (eaten/pop) rather than residual stock.
        # Residual stock was the v0.17 readout, but it reads 0% when
        # bread is consumed as fast as it's produced — the bakery
        # screenshot's pop-980 / bread-10/tick city showed Bread: 0%
        # despite a healthy active bakery. Rebuilt each tick inside
        # the eat-loop in `update`.
        self.food_eaten_by_good: dict[str, float] = {}
        # v0.23.x: per-building cumulative production / consumption,
        # keyed by (row, col). Each value is a dict {resource: total}
        # accumulated over every tick the building has been alive. The
        # info panel reads these so the player can see "this farm has
        # produced 540 wheat lifetime" alongside the static "produces
        # 15 wheat/tick" headline. Reset to {} only on new game / load
        # (saveload persists the totals). When a building is demolished
        # its entry is dropped — re-placing on the same tile starts
        # fresh, matching the player's intuition that a new building
        # has no history.
        self.produced_lifetime: dict[tuple[int, int], dict[str, float]] = {}
        self.consumed_lifetime: dict[tuple[int, int], dict[str, float]] = {}
        # v0.28: economic modifiers from timed events.  Distinct from
        # `service_modifiers` (which lives on ServiceMap and dims water /
        # food / etc. coverage). Keys this code consults today:
        #
        #   "wood_consumption_factor" — every building's wood
        #       consumption is multiplied by this factor per tick.
        #       Used by Blizzard (factor 2.0) to double the heat /
        #       fuel demand of the city.
        #
        # The dict is read at the start of each tick and applied
        # inside _calc_production. The game loop refreshes it from
        # EconomicEventManager.current_modifiers() every tick.
        self.economic_modifiers: dict[str, float] = {}
        # v0.28: starvation-style death triggers driven by event
        # modifiers. The blizzard sets this to ("wood", 1) so when
        # wood is at zero the city loses 1 pop per tick — citizens
        # freezing because the fuel pile ran out. List of
        # (resource_id, pop_per_tick) tuples; empty when no event
        # demands it. The game loop refreshes it from the event
        # manager's current_modifiers().
        self.pop_kill_when_resource_zero: list[tuple[str, int]] = []
        # Subscribe to building_removed so demolishing a building clears
        # its lifetime entries — replacing it starts fresh. The signal
        # bus is shared across the whole app; we deliberately don't
        # disconnect because the EconomyManager lives for the duration
        # of one game and is replaced (not mutated) on new-game.
        from signals import signals as _signals
        _signals.connect("building_removed", self._on_building_removed)

    def _on_building_removed(self, building_id: str, row: int, col: int) -> None:
        """Drop per-building lifetime totals when the building is gone."""
        self.produced_lifetime.pop((row, col), None)
        self.consumed_lifetime.pop((row, col), None)
        self.building_status.pop((row, col), None)

    # ── Serialisation ─────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "resources": dict(self.resources),
            "treasury": self.treasury,
            "tax_rate_index": self.tax_rate_index,
            "population": self.population,
            "happiness": self.happiness,
            "tick_count": self.tick_count,
            # v0.23.x: per-building lifetime totals. Serialised as a flat
            # list because JSON can't key on tuples; the row/col are
            # carried with each entry. Empty list for new games.
            "produced_lifetime": [
                [r, c, dict(v)]
                for (r, c), v in self.produced_lifetime.items()
            ],
            "consumed_lifetime": [
                [r, c, dict(v)]
                for (r, c), v in self.consumed_lifetime.items()
            ],
        }

    def from_dict(self, d: dict) -> None:
        self.resources = dict(d["resources"])
        self.treasury = d["treasury"]
        self.tax_rate_index = d["tax_rate_index"]
        self.tax_rate = TAX_RATES[self.tax_rate_index]
        self.population = d["population"]
        self.happiness = d["happiness"]
        self.tick_count = d.get("tick_count", 0)
        # v0.23.x: lifetime totals. Pre-v0.23.x saves don't have these
        # keys; we start fresh in that case so the displayed totals
        # reflect post-load play rather than fabricated numbers.
        self.produced_lifetime = {}
        for entry in d.get("produced_lifetime", []):
            if len(entry) >= 3:
                r, c, totals = entry[0], entry[1], entry[2]
                self.produced_lifetime[(int(r), int(c))] = {
                    k: float(v) for k, v in dict(totals).items()
                }
        self.consumed_lifetime = {}
        for entry in d.get("consumed_lifetime", []):
            if len(entry) >= 3:
                r, c, totals = entry[0], entry[1], entry[2]
                self.consumed_lifetime[(int(r), int(c))] = {
                    k: float(v) for k, v in dict(totals).items()
                }
        # Reset derived fields — they'll be recomputed on next update().
        # We zero them explicitly so the HUD doesn't flash stale values.
        self.employed = 0
        self.housing_capacity = 0
        self.storage_capacity = self.balance.base_storage
        self.income_per_tick = 0.0
        self.expenses_per_tick = 0.0
        self.food_balance = 0.0
        self._tax_mult_avg = 1.0
        # v0.35: clear per-nutrient eaten map on load — the next tick
        # will rebuild it. Doing it eagerly here would leave one tick
        # of stale data visible on load.
        self.food_eaten_by_good = {}
        # v0.16: nutrient_diversity is a per-tick derived value; we
        # reset it on load and let the next tick recompute. Doing it
        # eagerly here from `resources` would give a one-tick-correct
        # HUD reading on load but is duplicate work — the next tick
        # is a couple hundred milliseconds away.
        self.nutrient_diversity = 0

    # ── Public economy API ────────────────────────────────────────────────
    def cycle_tax_rate(self) -> None:
        self.tax_rate_index = (self.tax_rate_index + 1) % len(TAX_RATES)
        self.tax_rate = TAX_RATES[self.tax_rate_index]
        log.info("Tax rate → %d%%", int(self.tax_rate * 100))

    def can_afford(self, cost: float) -> bool:
        return self.treasury >= cost

    def spend(self, cost: float) -> bool:
        if self.can_afford(cost):
            self.treasury -= cost
            return True
        return False

    def refund(self, amount: float) -> None:
        self.treasury += amount

    def apply_event_effects(
        self, effects: dict[str, int], pop_effect: int, happiness_effect: int,
    ) -> None:
        for res, amount in effects.items():
            if res == "money":
                self.treasury += amount
            elif res in self.resources:
                self.resources[res] = max(0, self.resources[res] + amount)
            # Unknown resources are silently ignored — modders can introduce
            # new resources in events without the engine knowing yet.
        self.population = max(self.balance.pop_min, self.population + pop_effect)
        self.happiness = max(0.0, min(100.0, self.happiness + happiness_effect))

    # ── Tick ──────────────────────────────────────────────────────────────
    def update(
        self,
        building_ids: Iterable,
        *,
        connectivity=None,
        tier_lookup=None,
        service_lookup=None,
        feature_lookup=None,
        feature_tap=None,
    ) -> None:
        """Run one simulation tick.

        Args:
            building_ids: Either a list of building id strings (legacy
                signature) or a list of `(building_id, row, col)` tuples
                (preferred, enables road-connectivity gating).
            connectivity: Optional callable `(building_id, row, col) -> bool`
                that returns True if the building's production should run.
                Used to gate disconnected production buildings. If None,
                every building runs at full output (legacy behaviour).
            tier_lookup: Optional callable `(row, col) -> int` returning the
                house tier. If None, all houses are treated as tier 1
                (the historical default capacity).
            service_lookup: Optional callable
                ``(service_name, row, col) -> coverage_float``. If supplied,
                farms scale their output by water coverage (down to
                ``balance.farm_water_min_factor``) and houses without any
                water tank their happiness contribution. If None, water
                gating is bypassed (legacy behaviour, used by tests that
                don't construct a ServiceMap).
            feature_lookup: v0.13 — optional callable ``(row, col) -> str|None``
                returning the terrain feature at one cell. Buildings with
                a ``feature_yield_bonus`` map (e.g. farms on fertile soil)
                multiply their output by the matching multiplier. If
                None, no bonuses apply — keeps the v0.12 behaviour for
                tests that don't construct a GameMap.
            feature_tap: v0.14 — optional callable
                ``(building_id, row, col, amount) -> float`` that
                debits the gated feature tile by ``amount`` and
                returns what was actually extracted (zero if the
                vein/forest is depleted, partial if mid-tick exhaustion).
                Only invoked for buildings that gate on a feature
                via ``needs_feature``. If None, extraction is unmetered
                — pre-v0.14 behaviour, used by tests that don't care
                about depletion.
        """
        self.tick_count += 1
        b = self.balance
        # v0.12: per-building status is rebuilt each tick. Stale entries
        # for demolished buildings would otherwise linger and confuse
        # the inspector / flow graph.
        self.building_status = {}

        # Normalise to (id, row, col) tuples. A bare list of strings is
        # treated as positionless and connectivity is bypassed.
        positioned: list[tuple[str, int | None, int | None]] = []
        any_pos = False
        for item in building_ids:
            if isinstance(item, tuple) and len(item) == 3:
                positioned.append(item)
                any_pos = True
            else:
                positioned.append((item, None, None))

        prod, cons, eff, h_prod = self._calc_production(
            positioned,
            connectivity if any_pos else None,
            tier_lookup if any_pos else None,
            service_lookup if any_pos else None,
            feature_lookup if any_pos else None,
            feature_tap if any_pos else None,
        )

        # ── Production (scaled by efficiency) ─────────────────────────────
        # v0.15: stash gross production (pre-cap) so the stats panel and
        # food balance reflect what farms *would* deliver into a city
        # with infinite storage. Without this, a small-storage city
        # appears to "produce nothing" the moment its stock briefly
        # exceeds capacity (the screenshot bug from v0.14 — start_food
        # was 9500 while base_storage was 2000, so the very first tick
        # of any food production clamped the stock down).
        self.gross_production = dict(prod)
        income = 0.0
        production_money = 0.0  # v0.51: money from producing buildings only
        for res, amount in prod.items():
            scaled = int(amount * eff)
            if res == "money":
                self.treasury += scaled
                income += scaled
                production_money += scaled
            else:
                # v0.15: clamp the *added* amount against headroom, not
                # the resulting total. If stock is already over capacity
                # (e.g. start_food > base_storage on a fresh game) we
                # don't slam it down to capacity — we just refuse to
                # accept more until the player builds storage. The cap
                # still bites the moment headroom hits zero, which is
                # the player-facing rule we want.
                stock = self.resources.get(res, 0)
                headroom = max(0, self.storage_capacity - stock)
                accepted = min(scaled, headroom)
                self.resources[res] = stock + accepted

        # ── Consumption (also scaled by efficiency — fixes 1.1) ───────────
        # An unstaffed factory shouldn't drain inputs. We scale consumption
        # the same way production scales, so a factory at 50% staff consumes
        # 50% of its inputs and outputs 50% of its products.
        expenses = 0.0
        consumption_money = 0.0  # v0.51: money burned by consuming buildings
        for res, amount in cons.items():
            scaled = int(amount * eff)
            if res == "money":
                self.treasury -= scaled
                expenses += scaled
                consumption_money += scaled
            else:
                self.resources[res] = max(self.resources.get(res, 0) - scaled, 0)

        # ── v0.42: flat per-building gold upkeep ──────────────────────────
        # Independent of staffing or efficiency: you pay to keep a
        # building standing even when it's idle or disconnected. Almost
        # everything has upkeep 0; the naval buildings (ports, harbors,
        # shipyards) cost 1 gold/tick each. Summed into expenses so it
        # shows in the HUD 'Cost' line alongside wages and money-consumption.
        upkeep_total = 0
        for bid, _r, _c in positioned:
            bd = self.registry.get(bid)
            if bd is not None and getattr(bd, "upkeep", 0):
                upkeep_total += int(bd.upkeep)
        if upkeep_total:
            self.treasury -= upkeep_total
            expenses += upkeep_total
        self.upkeep_per_tick = float(upkeep_total)

        # ── Population food consumption ───────────────────────────────────
        # v0.16: subsistence is now drawn from a *pool* of edible goods,
        # not just `food`. Bread is the new headline staple (the bakery
        # produces it; tier-1 houses depend on it). The other nutrients
        # — vegetables, fruits, meat, fish, cheese, oil, honey, spice,
        # wine — are also edible and will feed the population if bread
        # runs out.
        #
        # v0.34: retired the legacy `food` resource from the staple
        # path. No building in data/buildings.json has produced `food`
        # since v0.16 (farm produces wheat → windmill → bakery → bread),
        # so adding `prod.get("food", 0)` to staple_produced and giving
        # `food` priority in eat_order was dead weight that misled the
        # HUD's `food_balance` line: a city with a farm-only chain and
        # no bakery would *display* a balanced food_balance because the
        # audit was double-counting wheat-via-farm-subsistence that no
        # longer exists. `staple_produced` is now bread-only. The
        # `food` resource can still hold a stockpile (trade-route
        # imports still target it by default), and the eat-loop will
        # drain it as the very last fallback after every nutrient is
        # exhausted — that preserves the v0.16 "don't let a player
        # starve on a save that pre-dates the nutrient layer" promise
        # without giving food a *privileged* slot that the HUD reflects.
        #
        # Eating order (greedy, in priority): bread → other nutrients
        # in NUTRIENTS order → legacy food (last-resort backstop).
        # Bread is what the bakery makes, so it's the staple the
        # player's chain is producing. The other nutrients sit in the
        # middle: eating, say, all your wine is a strictly worse
        # outcome than eating bread (high-tier houses *need* wine for
        # evolution) but it's still better than starving. Legacy food
        # comes last because draining it carries no opportunity cost
        # (it backs no house tier, no luxury chain) — perfect as a
        # final buffer.
        from constants import NUTRIENTS as _NUTRIENTS
        food_needed = self.population
        # Producer-side total used by `food_balance` (HUD line). Bread
        # is the staple; the eat-loop still pulls from other nutrients
        # and legacy food, but the headline number reported to the
        # player is bread delivery vs. population demand.
        staple_produced = prod.get("bread", 0)
        # Consumer-side: drain the pool in priority order.
        eat_order: list[str] = ["bread"]
        for n in _NUTRIENTS:
            if n not in eat_order:
                eat_order.append(n)
        # v0.34: legacy food is the last-resort fallback. Pre-v0.34 it
        # sat at index 1 (right after bread); now it sits last.
        if "food" not in eat_order:
            eat_order.append("food")
        food_eaten = 0
        remaining_need = food_needed
        # v0.35: reset and accumulate per-nutrient eaten so the HUD
        # satiety block can report delivery coverage, not residual
        # stock. We rebuild the dict from scratch each tick — partial
        # carry-over from the previous tick would mis-report a
        # nutrient that just ran dry.
        self.food_eaten_by_good = {good: 0.0 for good in eat_order}
        for good in eat_order:
            if remaining_need <= 0:
                break
            stock = self.resources.get(good, 0)
            if stock <= 0:
                continue
            take = min(remaining_need, stock)
            self.resources[good] = max(0, stock - take)
            food_eaten += take
            remaining_need -= take
            self.food_eaten_by_good[good] = float(take)
        self.food_balance = staple_produced * eff - food_needed
        # v0.8: fraction of citizens who actually got fed this tick. Used
        # by the tax block (you can't tax someone who's starving) and by
        # the rebellion system (sustained low fed_fraction → unrest).
        self.fed_fraction = (
            food_eaten / food_needed if food_needed > 0 else 1.0
        )

        # ── Taxes ─────────────────────────────────────────────────────────
        # Tier-aware: each citizen pays a base rate scaled by the average
        # tax multiplier of all houses (a city of villas pays more per
        # capita than a city of shacks). `_tax_mult_avg` is computed in
        # _calc_production.
        # v0.8: tax fed citizens, not raw citizens. Clamped to a floor so
        # a brief food gap doesn't zero revenue (citizens have personal
        # reserves), but a sustained famine collapses the treasury — which
        # is what the player should *feel* before the city falls apart.
        tax_fed_fraction = max(b.tax_min_fed_fraction, self.fed_fraction)
        taxable = self.population * tax_fed_fraction
        tax = (
            taxable * self.tax_rate
            * b.tax_revenue_per_capita * self._tax_mult_avg
        )
        self.treasury += tax
        income += tax

        # ── v0.13: wages ──────────────────────────────────────────────────
        # Each currently-employed worker draws wage_per_worker dn/tick
        # from the treasury. Wages are paid AFTER taxes (the tax round
        # has just topped up the treasury). If the treasury can't cover
        # the full bill, we pay what we can and stash the affordability
        # ratio — next tick's _calc_production reads it and proportionally
        # reduces the effective worker pool. The visible effect: a city
        # in deficit sees its buildings flicker to `partial` and then
        # `unstaffed` over a few ticks until the player raises taxes or
        # demolishes idle staff.
        #
        # Why next-tick rather than this-tick? This-tick gating would
        # require a pre-pass over buildings to compute total demand
        # before classification, which means classify() gets called
        # twice. Cheaper and cleaner to lag by one tick — and the lag
        # is invisible at 2 ticks/sec.
        wage_bill = self.employed * b.wage_per_worker
        if wage_bill <= 0:
            paid = 0.0
            self.wage_payment_ratio = 1.0
        elif self.treasury >= wage_bill:
            paid = wage_bill
            self.treasury -= paid
            self.wage_payment_ratio = 1.0
        else:
            # Pro-rata: we pay what we can. Some workers go home unpaid;
            # next tick they'll show up at reduced numbers.
            paid = max(0.0, self.treasury)
            self.treasury -= paid
            self.wage_payment_ratio = paid / wage_bill if wage_bill > 0 else 1.0
        expenses += paid

        self.income_per_tick = income
        self.expenses_per_tick = expenses

        # v0.51: stash the granular breakdown for the '$' budget panel.
        # We preserve any externally-injected lines (trade routes /
        # voyages, written by the game window after this tick) by only
        # overwriting the economy-owned keys. ``tax``/``production`` sum
        # to ``income``; ``wages``/``upkeep``/``consumption`` sum to
        # ``expenses`` (minus the externally-injected income lines).
        self.finance_breakdown["tax"] = float(tax)
        self.finance_breakdown["production"] = float(production_money)
        self.finance_breakdown["wages"] = float(paid)
        self.finance_breakdown["upkeep"] = float(upkeep_total)
        self.finance_breakdown["consumption"] = float(consumption_money)

        # ── v0.16: nutrient diversity ───────────────────────────────────
        # Count distinct nutrients with non-zero stock at the END of the
        # tick (after the eating loop has drawn from the pool). This
        # is the snapshot the HUD will show next frame and the input
        # to the diversity bonus on happiness + growth.
        self.nutrient_diversity = sum(
            1 for n in _NUTRIENTS if self.resources.get(n, 0) > 0
        )

        # ── Happiness target (smoothed) ───────────────────────────────────
        target, breakdown = self._compute_happiness_target_with_breakdown(
            food_eaten, food_needed, eff, h_prod,
        )
        # Stash the latest breakdown so the 'Z' debug overlay can
        # surface every term to the player without the engine having
        # to recompute. Updated each tick — paused frames keep the
        # last value, which is the right behaviour for debugging.
        self.last_happiness_breakdown: dict[str, float] = breakdown
        self.happiness += (target - self.happiness) * b.happiness_smoothing

        # ── Population growth / shrink ────────────────────────────────────
        if self.tick_count % b.pop_growth_check_interval == 0:
            self._update_population(food_eaten, food_needed)

        # v0.28: event-driven population kill. Blizzard sets
        # pop_kill_when_resource_zero = [("wood", 1)] for its duration —
        # if the city has run out of wood, 1 citizen freezes per tick.
        # Distinct from food-driven starvation in _update_population
        # because (a) it's event-bound rather than always-on, and
        # (b) the resource is arbitrary (wood today, could be water
        # / food / anything tomorrow). The hook respects pop_min so
        # the city can't be erased entirely.
        for res_id, per_tick in self.pop_kill_when_resource_zero:
            if self.resources.get(res_id, 0) <= 0 and self.population > b.pop_min:
                killed = min(per_tick, self.population - b.pop_min)
                if killed > 0:
                    self.population -= killed
                    log.warning(
                        "Event pop-kill: -%d pop (no %s); population now %d",
                        killed, res_id, self.population,
                    )

    # ── Internals ─────────────────────────────────────────────────────────
    def _calc_production(
        self,
        positioned: list[tuple[str, int | None, int | None]],
        connectivity=None,
        tier_lookup=None,
        service_lookup=None,
        feature_lookup=None,
        feature_tap=None,
    ) -> tuple[dict[str, int], dict[str, int], float, int]:
        """Aggregate production / consumption / housing / storage from one
        snapshot of buildings. Returns (prod, cons, efficiency, happy_prod).

        If `connectivity` is provided, production buildings whose
        connectivity check fails skip production AND consumption (a
        disconnected farm doesn't burn workers either).

        If `tier_lookup` is provided, housing capacity is scaled by per-
        house tier multipliers and the per-capita tax multiplier is set
        to the average across all houses (weighted by their capacity).

        If `service_lookup` is provided (v0.8), farms scale their food
        output by the water coverage at their tile (clamped to
        ``balance.farm_water_min_factor`` so a brand new game with no
        water network still produces *something*). Houses are unaffected
        here — water gates *evolution* (in `house_evolution.py`) and
        *happiness* (in `_compute_happiness_target`), but never housing
        capacity itself: a thirsty city should be miserable, not roofless.

        v0.12: each producer/consumer is now classified through
        ``building_status.classify`` and its production/consumption is
        gated by a per-building throttle (workers × min(input ratio)).
        A tavern with no wine produces 0 happiness this tick instead of
        emitting "free" output the way v0.11 did. The status snapshot
        is stashed on ``self.building_status`` for the inspector and
        the flow graph to read.
        """
        from building_status import (
            BuildingStatus, classify, ACTIVE, PARTIAL, IDLE,
        )

        prod: dict[str, int] = {}
        cons: dict[str, int] = {}
        workers_needed = 0
        housing = 0
        extra_storage = 0
        h_prod = 0
        # For tier-weighted tax averaging.
        tax_weighted_sum = 0.0
        housing_for_tax = 0
        b = self.balance
        # v0.8: count houses without water for the happiness penalty.
        houses_total = 0
        houses_dry = 0

        # ── Pass 1: housing, storage, water bookkeeping, total worker demand ─
        # We need total worker demand before we can know per-building
        # worker fill, and we need the resource snapshot from the *start*
        # of the tick to classify input shortages — so all the per-tick
        # scratch is computed up front.
        connected_demanders: list[tuple[str, int | None, int | None]] = []
        for bt, row, col in positioned:
            bd = self.registry.get(bt)
            if bd is None:
                continue
            extra_storage += bd.storage

            # Apply per-house tier multipliers to housing capacity.
            if bd.is_evolvable and tier_lookup is not None and row is not None and col is not None:
                tier = tier_lookup(row, col)
                cap_mult = b.house_tier_capacity_mult[
                    min(tier, len(b.house_tier_capacity_mult) - 1)
                ]
                tax_mult = b.house_tier_tax_mult[
                    min(tier, len(b.house_tier_tax_mult) - 1)
                ]
                this_housing = int(bd.housing * cap_mult)
                housing += this_housing
                tax_weighted_sum += tax_mult * this_housing
                housing_for_tax += this_housing
            else:
                housing += bd.housing
                if bd.housing > 0:
                    tax_weighted_sum += 1.0 * bd.housing
                    housing_for_tax += bd.housing

            if bd.is_evolvable and bd.housing > 0 and service_lookup is not None and row is not None:
                houses_total += 1
                if service_lookup("water", row, col) <= 0.0:
                    houses_dry += 1

            # Connectivity gate: feeds into status classification, but
            # also short-circuits worker demand (a disconnected building
            # doesn't compete for workers).
            disconnected = (
                connectivity is not None
                and row is not None
                and col is not None
                and bd.requires_road
                and not connectivity(bt, row, col)
            )
            if disconnected:
                # Stash the disconnected status now; we still want it on
                # the inspector even though the building doesn't draw
                # from any pool this tick.
                if row is not None and col is not None:
                    self.building_status[(row, col)] = BuildingStatus(
                        state="disconnected",
                        throttle=0.0,
                        workers_filled=0,
                        workers_needed=bd.workers,
                        reason="Idle: no road link",
                    )
                continue

            workers_needed += bd.workers
            connected_demanders.append((bt, row, col))

        # Compute housing-derived state immediately (needed before pop
        # eats and tests like test_pop_grows_when_conditions_good can
        # check housing_capacity > population).
        self.housing_capacity = housing
        self.storage_capacity = self.balance.base_storage + extra_storage
        self.employed = min(self.population, workers_needed)
        self._tax_mult_avg = (
            tax_weighted_sum / housing_for_tax
            if housing_for_tax > 0 else 1.0
        )
        self._dry_house_fraction = (
            houses_dry / houses_total if houses_total > 0 else 0.0
        )
        # Global efficiency = same number returned to update() so the
        # downstream consumption-scaling and happiness math sees the
        # exact value v0.11 saw. Per-building gating is applied INSIDE
        # this loop to the prod/cons aggregates, so the caller's "scaled
        # = int(amount * eff)" call is now ~idempotent (most aggregates
        # already account for the throttle; eff covers any residual).
        global_eff = (
            min(1.0, self.population / workers_needed)
            if workers_needed > 0 else 1.0
        )

        # ── Pass 2: per-building classification & gated aggregation ──
        # Resource snapshot taken at the START of the tick — every
        # building sees the same pool when deciding "do I have enough
        # input?". Inputs added by other buildings this tick land in
        # the pool but don't affect this tick's classification (the
        # producer's gift gets received next tick). This matches the
        # batch-update spirit of the v0.7+ economy.
        resource_snapshot = dict(self.resources)

        # Distribute the global worker pool across connected demanders
        # in placement order. A more sophisticated version would prefer
        # food-producing buildings first; left for a future tuning pass.
        # v0.13: scale by wage_payment_ratio. If last tick the treasury
        # couldn't cover wages, the unpaid workers don't show up this
        # tick — workers_remaining drops proportionally and downstream
        # buildings get classified as `partial` or `unstaffed` exactly
        # as if the population had shrunk. Resets to 1.0 once the
        # treasury catches up.
        workers_remaining = int(self.population * self.wage_payment_ratio)

        for bt, row, col in connected_demanders:
            bd = self.registry[bt]
            # Workers actually filled at this building.
            wf = min(bd.workers, workers_remaining)
            workers_remaining = max(0, workers_remaining - bd.workers)

            # Classify: do we have inputs / staff / road?
            status = classify(
                requires_road=bd.requires_road,
                connected=True,                 # filtered above
                workers_needed=bd.workers,
                workers_filled=wf,
                consumption={k: v for k, v in bd.consumption.items() if k != "money"},
                available=lambda g: resource_snapshot.get(g, 0),
                has_production=bool(bd.production),
            )
            # Money consumption is checked separately because the treasury
            # isn't in the resource dict but the player still feels it.
            money_cost = bd.consumption.get("money", 0)
            if money_cost > 0 and self.treasury < money_cost:
                # Insufficient treasury → starved-by-money. We blend with
                # any other starvation reason rather than overwriting.
                from building_status import STARVED, label_for_state
                missing = list(status.missing_inputs)
                if "money" not in missing:
                    missing.append("money")
                status = BuildingStatus(
                    state=STARVED,
                    throttle=0.0,
                    missing_inputs=missing,
                    workers_filled=wf,
                    workers_needed=bd.workers,
                    reason=label_for_state(STARVED, missing, wf, bd.workers),
                )

            if row is not None and col is not None:
                self.building_status[(row, col)] = status

            throttle = status.throttle
            if throttle <= 0.0:
                # Starved / unstaffed buildings produce nothing AND consume
                # nothing — same invariant the v0.7 audit (1.1) locked in
                # for unstaffed buildings, now extended to input-starved.
                continue

            # v0.8: water factor applies to farm output. Stacks with the
            # input throttle multiplicatively — a half-watered farm at
            # 80% staff gets ~0.4 effective output.
            water_factor = 1.0
            if (
                bt == "farm"
                and service_lookup is not None
                and row is not None
                and col is not None
            ):
                cov = service_lookup("water", row, col)
                water_factor = (
                    b.farm_water_min_factor
                    + (1.0 - b.farm_water_min_factor) * min(1.0, cov)
                )

            # v0.13: terrain feature yield bonus. A farm on fertile_soil
            # produces 50% more food and wheat (multiplier 1.5). The
            # building's footprint is searched for the *largest* bonus
            # — if a 2×2 farm has one fertile_soil tile and three plain
            # grass tiles, the bonus applies. Bonuses stack
            # multiplicatively with water_factor. ``feature_lookup`` is
            # an optional callable from the caller (game_window) that
            # returns the feature id at (r, c) or None; tests that
            # don't construct one keep the legacy "no bonus" behaviour.
            feature_factor = 1.0
            if (
                bd.feature_yield_bonus
                and feature_lookup is not None
                and row is not None
                and col is not None
            ):
                best = 1.0
                for dr in range(bd.height):
                    for dc in range(bd.width):
                        feat = feature_lookup(row + dr, col + dc)
                        if feat in bd.feature_yield_bonus:
                            best = max(best, bd.feature_yield_bonus[feat])
                feature_factor = best

            # v0.25: per-tile multiplicative yield for water extractors
            # (fisheries). The fishery's `production.fish` rate is
            # quoted PER fishy tile in catchment; with 4 fish tiles in
            # range, the building produces 4× the headline rate. Plain
            # water (no fish feature) means tile_count_factor=0 — the
            # depletion path below will then flip the building to idle.
            #
            # We compute this by walking the footprint + Chebyshev-1
            # neighbours and counting `fish` features. The geometry
            # matches the fishery placement / tap rule.
            tile_count_factor = 1.0
            if (
                bd.needs_terrain == "water"
                and feature_lookup is not None
                and row is not None
                and col is not None
            ):
                fish_tiles = 0
                seen: set[tuple[int, int]] = set()
                for dr_f in range(bd.height):
                    for dc_f in range(bd.width):
                        rr = row + dr_f
                        cc = col + dc_f
                        for dn_r in (-1, 0, 1):
                            for dn_c in (-1, 0, 1):
                                nr = rr + dn_r
                                nc = cc + dn_c
                                if (nr, nc) in seen:
                                    continue
                                seen.add((nr, nc))
                                if feature_lookup(nr, nc) == "fish":
                                    fish_tiles += 1
                tile_count_factor = float(fish_tiles)
                # No fish in range → fishery is idle this tick. We
                # surface a clear reason on the status now (without
                # waiting for the tap path, which won't even run when
                # requested=0).
                if fish_tiles == 0:
                    from building_status import IDLE, BuildingStatus
                    self.building_status[(row, col)] = BuildingStatus(
                        state=IDLE,
                        throttle=0.0,
                        workers_filled=status.workers_filled,
                        workers_needed=status.workers_needed,
                        reason="Idle: no fish in range",
                    )

            # v0.14: depletion of gated features. An extractor that
            # gates on `needs_feature` (lumber mill on forest, mine on
            # gold/copper vein) drains the tile reserves with each
            # tick of production. When the underlying feature tile is
            # depleted, the tap returns 0 and we zero the building's
            # production for this tick. Inexhaustible features
            # (stone deposit, fertile soil, groundwater) skip this
            # gate — the tap returns the requested amount unchanged.
            #
            # For partial extraction (mid-tick exhaustion: tile had 5
            # reserves, we asked for 8), we scale the production
            # output by the actual ratio. The classifier above
            # already ran, so the building's status doesn't flip to
            # `idle` this tick — that happens *next* tick when the
            # placement-time feature gate (still satisfied as long as
            # the feature id is on the tile, even if reserves=0)
            # combined with `_classify_depletion_state` flips the
            # status. We tag the status in-place below so the
            # inspector reads `Idle: vein exhausted` immediately.
            depletion_factor = 1.0
            # v0.25: three classes of building trigger the feature
            # tap, not just `needs_feature` extractors. The bridge in
            # game_window._feature_tap handles the per-class routing;
            # the economy just needs to know whether to call it.
            should_tap = (
                feature_tap is not None
                and row is not None
                and col is not None
                and (
                    bd.needs_feature
                    or bd.feature_yield_bonus
                    or bd.needs_terrain == "water"
                )
            )
            if should_tap:
                # Pick the headline output's amount as the "requested"
                # tap size. A mine at full output asks for 6 iron;
                # the tap drains 6 from reserves. If the building has
                # multiple outputs, we tap once for the largest
                # because draining all of them is more punitive than
                # the design intends.
                non_happy_prod = {
                    r: a for r, a in bd.production.items() if r != "happiness"
                }
                if non_happy_prod:
                    headline = max(non_happy_prod.values())
                    requested = (
                        headline * throttle * water_factor
                        * feature_factor * tile_count_factor
                    )
                    extracted = feature_tap(bt, row, col, requested)
                    if requested > 0 and extracted <= 0:
                        depletion_factor = 0.0
                        # Override the status so the player sees the
                        # right reason on the inspector / hover
                        # tooltip without waiting for next tick.
                        from building_status import IDLE, BuildingStatus
                        if bd.needs_feature:
                            feat = bd.needs_feature[0]
                        elif bd.needs_terrain == "water":
                            feat = "fish"
                        elif bd.feature_yield_bonus:
                            feat = next(iter(bd.feature_yield_bonus))
                        else:
                            feat = "resource"
                        reason = f"Idle: {feat.replace('_', ' ')} exhausted"
                        self.building_status[(row, col)] = BuildingStatus(
                            state=IDLE,
                            throttle=0.0,
                            workers_filled=status.workers_filled,
                            workers_needed=status.workers_needed,
                            reason=reason,
                        )
                    elif requested > 0:
                        depletion_factor = extracted / requested

            # We accumulate per-tick aggregates pre-scaled by per-building
            # throttle. The downstream code in update() multiplies again
            # by ``global_eff``; for buildings whose only throttle factor
            # IS the workforce, this is a no-op (throttle * eff ≈ same
            # number) and v0.11 tests keep passing. For input-starved
            # buildings, the throttle is 0 and they contribute nothing.
            #
            # v0.23.x: also accumulate per-building lifetime totals so the
            # info panel can show "produced 540 wheat lifetime" next to
            # the per-tick rate. Keyed by (row, col); only populated when
            # the positioned API is in use (the legacy positionless test
            # path skips this — row/col would be None). We bank the
            # *post-scaling* amount (after throttle, water, feature, and
            # depletion factors) but *before* the global_eff division —
            # the global_eff factor exists to redistribute output among
            # storage-capped or wage-rationed producers; for "what did
            # this building actually make" the throttled amount is the
            # right number to show.
            track_lifetime = row is not None and col is not None
            for r, a in bd.production.items():
                scaled = (
                    a * throttle * water_factor
                    * feature_factor * tile_count_factor * depletion_factor
                )
                if r == "happiness":
                    h_prod += int(scaled / max(global_eff, 1e-9))
                else:
                    prod[r] = prod.get(r, 0) + scaled / max(global_eff, 1e-9)
                if track_lifetime and r != "happiness" and scaled > 0:
                    bucket = self.produced_lifetime.setdefault(
                        (row, col), {},
                    )
                    bucket[r] = bucket.get(r, 0.0) + scaled
            for r, a in bd.consumption.items():
                # v0.28: per-resource economic modifier (blizzard etc.).
                # E.g. blizzard sets `wood_consumption_factor: 2.0` and
                # every wood-burning building doubles its draw for
                # the duration. The map is keyed by `<resource>_consumption_factor`;
                # default 1.0 (no effect).
                ce_factor = self.economic_modifiers.get(
                    f"{r}_consumption_factor", 1.0,
                )
                cons[r] = cons.get(r, 0) + (
                    a * throttle * depletion_factor * ce_factor
                    / max(global_eff, 1e-9)
                )
                if track_lifetime:
                    drawn = a * throttle * depletion_factor * ce_factor
                    if drawn > 0:
                        bucket = self.consumed_lifetime.setdefault(
                            (row, col), {},
                        )
                        bucket[r] = bucket.get(r, 0.0) + drawn

        return prod, cons, global_eff, h_prod

    def _compute_happiness_target(
        self, food_eaten: int, food_needed: int, eff: float, h_prod: int,
    ) -> float:
        target, _breakdown = self._compute_happiness_target_with_breakdown(
            food_eaten, food_needed, eff, h_prod,
        )
        return target

    def _compute_happiness_target_with_breakdown(
        self, food_eaten: int, food_needed: int, eff: float, h_prod: int,
    ) -> tuple[float, dict[str, float]]:
        """Same as ``_compute_happiness_target`` but also returns a
        per-term breakdown of the additive equation. Used by the
        ``Z`` debug overlay so the player can see exactly which
        levers move happiness."""
        b = self.balance
        tax_pen = self.tax_rate * b.happiness_tax_penalty_mult
        food_bon = b.happiness_food_bonus if food_eaten >= food_needed else b.happiness_food_penalty
        house_bon = (
            b.happiness_housing_bonus
            if self.housing_capacity >= self.population
            else b.happiness_housing_penalty
        )
        emp_bon = (
            b.happiness_employment_bonus
            if eff >= b.happiness_employment_threshold
            else b.happiness_employment_penalty
        )
        # v0.8: water penalty. Scales with the fraction of houses that
        # have zero water coverage, so a single dry house in a big city
        # is a small drag, but a city with no water network at all loses
        # the full penalty.
        dry_frac = getattr(self, "_dry_house_fraction", 0.0)
        water_pen = b.house_no_water_happiness_penalty * dry_frac
        # v0.16: nutrient diversity bonus. Linear in the count of
        # nutrients in stock, capped at full_count = 10 nutrients.
        # 0 nutrients → 0 bonus; 10 nutrients → full bonus. The cap
        # is intentionally generous compared to other happiness
        # levers (matches the player effort: building all ten
        # chains is an end-game goal, so it should *feel* like a
        # tangible reward when achieved).
        diversity_frac = min(
            1.0, self.nutrient_diversity / max(1, b.nutrient_diversity_full_count),
        )
        diversity_bon = b.nutrient_diversity_happiness_max * diversity_frac
        raw = (
            b.happiness_base - tax_pen + food_bon + house_bon
            + emp_bon + water_pen + diversity_bon + h_prod
        )
        target = max(0.0, min(100.0, raw))
        breakdown = {
            "happiness_base": b.happiness_base,
            "tax_pen": -tax_pen,
            "food_bon": food_bon,
            "house_bon": house_bon,
            "emp_bon": emp_bon,
            "water_pen": water_pen,
            "diversity_bon": diversity_bon,
            "h_prod": float(h_prod),
            "raw_sum": raw,
            "target": target,
            # Inputs the breakdown was computed from — useful so the
            # debug panel can print "food_eaten=120 food_needed=100"
            # without the player needing a separate stats dump.
            "tax_rate": self.tax_rate,
            "food_eaten": float(food_eaten),
            "food_needed": float(food_needed),
            "housing_capacity": float(self.housing_capacity),
            "population": float(self.population),
            "employment_eff": eff,
            "employment_threshold": b.happiness_employment_threshold,
            "dry_house_fraction": dry_frac,
            "nutrient_diversity": float(self.nutrient_diversity),
            "nutrient_diversity_full_count": float(b.nutrient_diversity_full_count),
            "happiness_current": self.happiness,
            "smoothing": b.happiness_smoothing,
        }
        return target, breakdown

    def _update_population(self, food_eaten: int, food_needed: int) -> None:
        b = self.balance
        if (
            self.happiness > b.pop_growth_min_happiness
            and food_eaten >= food_needed
            and self.housing_capacity > self.population
        ):
            # v0.16: nutrient diversity multiplies the per-tick growth
            # increment. With diversity = 0 the multiplier is 1.0
            # (legacy growth rate); with diversity = 10 the multiplier
            # is `nutrient_diversity_growth_mult_max` (default 1.5×).
            # Scales linearly so each new chain coming online is
            # immediately visible in the population graph.
            diversity_frac = min(
                1.0, self.nutrient_diversity / max(1, b.nutrient_diversity_full_count),
            )
            growth_mult = (
                1.0
                + (b.nutrient_diversity_growth_mult_max - 1.0) * diversity_frac
            )
            g = max(1, int(self.population * b.pop_growth_rate * growth_mult))
            self.population = min(self.population + g, self.housing_capacity)
            log.debug("Pop +%d → %d  (div=%d, mult=%.2f)",
                      g, self.population, self.nutrient_diversity, growth_mult)
        elif (
            self.happiness < b.pop_shrink_max_happiness
            or food_eaten < food_needed * b.pop_shrink_food_ratio
        ):
            s = max(1, int(self.population * b.pop_shrink_rate))
            self.population = max(b.pop_min, self.population - s)
            log.debug("Pop -%d → %d", s, self.population)

    # ── HUD ───────────────────────────────────────────────────────────────
    # Luxury / chained goods. Only displayed when the city has produced
    # at least one unit, so a brand new game shows the same compact panel
    # as v0.4. Order matters: this is the order they appear in the HUD.
    # v0.11: wheat/flour/planks added — the new supply-chain intermediates.
    # v0.13: stone/stone_blocks added — the construction-material chain.
    # v0.16: nutrients (bread, vegetables, fruits, meat, fish, cheese,
    # honey, spice) added so the HUD shows them as soon as production
    # starts. Wine and oil were already here; bread is the new staple.
    # Order: nutrients first (more interesting to a player learning
    # the chain), then intermediates, then construction stock, then
    # luxury crafts.
    LUXURY_GOODS: tuple[str, ...] = (
        # Nutrients (the v0.16 layer).
        "bread", "vegetables", "fruits", "meat", "fish",
        "cheese", "oil", "honey", "spice", "wine",
        # Intermediates / raw → refined chains.
        "wheat", "flour", "planks", "stone", "stone_blocks",
        "olives", "grapes", "clay", "pottery", "weapons",
        # v0.16: livestock (raw animal stock for slaughterhouse/cheese)
        # and horses (cavalry from the stable).
        "livestock", "horses",
    )

    def get_status_lines(self) -> list[str]:
        from constants import NUTRIENT_LABELS, NUTRIENTS
        lines = [
            f"Population: {self.population} / {self.housing_capacity}",
            f"Employed: {self.employed}",
            f"Happiness: {self.happiness:.0f}%",
            f"Treasury: {self.treasury:.0f} gold",
            f"Tax: {self.tax_rate*100:.0f}%",
            # v0.16: nutrient diversity readout. "Nutrients: 4/10" tells
            # the player how many distinct nutrients are stocked, with
            # the 10 cap matching the spec list. Always shown (even at
            # 0) so the player learns it's a tracked metric from frame
            # zero.
            f"Nutrients: {self.nutrient_diversity}/{self.balance.nutrient_diversity_full_count}",
            # v0.17: per-nutrient satiety section header. Replaces the
            # old single "Food: N (+/-K/t)" line which didn't reflect
            # the multi-nutrient reality — a city with 200 fish and
            # 0 bread used to show "Food: 200" with no clue that 9 of
            # the 10 nutrients were missing. Now we show one row per
            # nutrient, listing how many ticks of full subsistence
            # the current stock provides ("100%" = at least one full
            # tick covered, capped). The header is a section marker
            # the renderer colours differently so the eye groups the
            # block together.
            "── Satiety ──",
        ]
        pop = max(1, self.population)
        # v0.35: satiety now reports delivery coverage (eaten this
        # tick / population) rather than end-of-tick stock. The old
        # stock-based formula showed 0% whenever bread was consumed
        # as fast as it was produced — pop 980 + bakery output 10/t
        # → stock drops to 0 every tick → "Bread: 0%" despite a
        # healthy chain. The new formula reads the per-nutrient eaten
        # map populated in the eat-loop; the residual stock is still
        # shown in parens as a buffer indicator.
        eaten_map = getattr(self, "food_eaten_by_good", {}) or {}
        for good in NUTRIENTS:
            qty = self.resources.get(good, 0)
            eaten = eaten_map.get(good, 0)
            satiety = max(0, min(100, int(100 * eaten / pop)))
            label = NUTRIENT_LABELS.get(good, good.capitalize())
            lines.append(f"  {label}: {satiety}%  ({int(qty)})")
        # v0.34: replaced the v0.17 "Food (legacy): N (+/-K/t)" line
        # with a clean "Food balance: +/-K/t" line. The legacy `food`
        # resource is no longer in the producer path (see economy
        # update), so showing its raw stock as a separate readout was
        # misleading — players read "Food (legacy): 9500" as a buffer
        # they could draw on, but with no producer it's a one-way
        # stockpile that drains only as the eat-loop's last-resort
        # fallback. The new line is what the HUD *actually* drives
        # off: the per-tick delta of staple (bread) production vs.
        # population demand, signed so a negative number reads as
        # "you're starving".
        lines.append(
            f"Food balance: {self.food_balance:+.0f}/t"
        )
        # ── Non-nutrient goods (raw / intermediate / construction) ──
        lines.append("── Goods ──")
        for good in ("wood", "iron", "tools"):
            lines.append(f"  {good.capitalize()}: {self.resources.get(good, 0):.0f}")
        for good in self.LUXURY_GOODS:
            # Skip nutrients here — they got their own block above.
            if good in NUTRIENTS:
                continue
            qty = self.resources.get(good, 0)
            if qty > 0:
                label = NUTRIENT_LABELS.get(good, good.capitalize())
                lines.append(f"  {label}: {qty:.0f}")
        lines.append(
            f"Income: +{self.income_per_tick:.0f}  Cost: -{self.expenses_per_tick:.0f}"
        )
        return lines

    # v0.14: per-line tone hints for the right-panel renderer. The tone
    # is one of "normal" (default), "good" (resource trending up,
    # treasury healthy), "bad" (deficit, starving), "warn" (running
    # low but not yet critical), "section" (a header line). The
    # renderer maps these to colours; tests can assert on tones
    # without coupling to specific RGB values.
    #
    # We keep `get_status_lines()` unchanged for callers that only
    # want strings (the tests, anyone scripting the HUD); the
    # renderer calls this method to also pick colours.
    def get_status_lines_with_tones(self) -> list[tuple[str, str]]:
        lines = self.get_status_lines()
        tones: list[str] = []
        for line in lines:
            tones.append(self._tone_for_line(line))
        return list(zip(lines, tones))

    def _tone_for_line(self, line: str) -> str:
        if line.startswith("Happiness:"):
            return ("good" if self.happiness >= 60 else
                    "bad"  if self.happiness <= 30 else "warn")
        if line.startswith("Treasury:"):
            return ("good" if self.treasury >= 1000 else
                    "bad"  if self.treasury < 100  else "warn")
        # v0.17: section headers (── Satiety ──, ── Goods ──) are
        # rendered as faint gold dividers so the eye groups the
        # following block together.
        if line.startswith("──"):
            return "section"
        # v0.34: keyed off the new "Food balance:" line that replaces
        # the v0.17 "Food (legacy):" readout. Tone semantics unchanged
        # — a negative balance reads bad, positive reads good.
        if line.startswith("Food balance:"):
            return ("bad"  if self.food_balance < 0 else
                    "good" if self.food_balance > 0 else "normal")
        # v0.17: per-nutrient satiety lines start with two spaces
        # (the indent under the section header). We tone each one by
        # its own percentage: green at 100, warn at 25–99, bad at 0.
        if line.startswith("  ") and "%" in line and ":" in line:
            try:
                # Parse "  Bread: 75% (123)" → 75
                pct_str = line.split(":", 1)[1].strip().split("%", 1)[0]
                pct = int(pct_str)
                if pct >= 100:
                    return "good"
                if pct >= 25:
                    return "warn"
                return "bad"
            except (ValueError, IndexError):
                return "normal"
        if line.startswith("Nutrients:"):
            full = self.balance.nutrient_diversity_full_count
            if self.nutrient_diversity >= full:
                return "good"
            if self.nutrient_diversity >= max(1, full // 2):
                return "warn"
            return "normal"
        if line.startswith("Income:"):
            net = self.income_per_tick - self.expenses_per_tick
            return ("good" if net > 0 else
                    "bad"  if net < 0 else "normal")
        return "normal"
