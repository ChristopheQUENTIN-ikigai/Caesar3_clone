"""Simulation balance — all tuning values in one frozen dataclass.

Modders / balancers tweak these without hunting through `economy.py`.
The dataclass is frozen so the running game can't accidentally mutate balance.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Balance:
    # ── Storage ───────────────────────────────────────────────────────────
    base_storage: int = 2000

    # ── Population ────────────────────────────────────────────────────────
    pop_min: int = 10
    pop_growth_rate: float = 0.03
    pop_shrink_rate: float = 0.02
    pop_growth_check_interval: int = 5      # ticks between growth checks
    pop_growth_min_happiness: float = 60.0
    pop_shrink_max_happiness: float = 30.0
    pop_shrink_food_ratio: float = 0.5      # fed < 50% of need → shrink

    # ── Tax & money ───────────────────────────────────────────────────────
    tax_revenue_per_capita: float = 5.0     # treasury += pop * rate * this

    # ── v0.13: worker wages ───────────────────────────────────────────────
    # Each employed worker draws a small daily wage from the treasury.
    # If the treasury can't cover the full wage bill, the bill is paid
    # pro rata and the unpaid workers don't show up — buildings are
    # classified as `unstaffed` for the affected slots, the same way
    # the v0.12 input-starvation gate flips a tavern to `starved`. This
    # closes the loop: a city that taxes too little to pay its workers
    # has visibly empty workshops, which is the right player-facing
    # signal.
    #
    # Default = 0.2 dn/worker/tick. With pop=50 and the v0.13 starting
    # treasury (1000 dn), the city has ~100 ticks of runway before
    # needing tax income at the lowest rate. A conservative number
    # — easy to crank up if playtest says wages should bite earlier.
    wage_per_worker: float = 0.2

    # ── Happiness model ───────────────────────────────────────────────────
    happiness_base: float = 50.0
    happiness_tax_penalty_mult: float = 200.0
    happiness_food_bonus: float = 10.0
    happiness_food_penalty: float = -20.0
    happiness_housing_bonus: float = 5.0
    happiness_housing_penalty: float = -15.0
    happiness_employment_bonus: float = 10.0
    happiness_employment_penalty: float = -5.0
    happiness_employment_threshold: float = 0.8
    happiness_smoothing: float = 0.1        # exponential approach factor

    # ── Starting state ────────────────────────────────────────────────────
    # v0.13: starting food bumped 500 → 9500. The v0.11 chain rewiring
    # (farm now produces 8 food + 12 wheat instead of 15 food) made the
    # first ~200 ticks unforgiving — by the time the player got the
    # windmill+bakery online, the 500-food buffer had been eaten through
    # by the starter population. 9500 covers ~95 pop * 100 ticks at full
    # consumption, which is roughly the time-to-first-bakery window in
    # playtest. This is the easiest of the v0.13 changes and also the
    # cheapest to revert if balance feedback says it's too generous.
    start_food: int = 9500
    start_wood: int = 300
    start_iron: int = 150
    # v0.22: iron-ore is the new mine output; the smelter refines it to
    # iron. A small starter buffer means the player who builds the
    # smelter early doesn't sit idle waiting for the mine's first batch.
    # Identical reasoning to start_wheat/start_flour above.
    start_iron_ore: int = 30
    # v0.22: a small starter weapons cache so the first barracks placed
    # on the map can arm its first 2-3 soldiers without already having
    # the iron→smelter→smith chain online. After those are spent, no
    # more soldiers spawn until the chain is built.
    start_weapons: int = 3
    start_tools: int = 50
    # v0.11: starter buffers for the new intermediate goods so a freshly
    # placed bakery / sawmill doesn't sit idle for 30 ticks waiting for
    # the first walker. Small numbers — they're consumed within ~10 ticks
    # of regular play and the player's chain takes over from there.
    start_wheat: int = 50
    start_flour: int = 20
    start_planks: int = 20
    # v0.13: stone is the new construction material (along with planks).
    # 100 covers ~5 medium-cost buildings at the start without forcing
    # the player to build a quarry on tick zero.
    start_stone: int = 100
    # stone_blocks is the refined version (stonemason output) used by
    # most civic buildings. 50 covers a senate or a couple of temples.
    start_stone_blocks: int = 50
    # v0.16: starter bread. The bakery is a tier-2 build (flour →
    # bread), so the first ~50 ticks would otherwise have no bread at
    # all and the new diversity bonus would be unreachable. 30 bread
    # covers a few dozen ticks of subsistence and lets the player
    # see the nutrient HUD populate with at least one entry from
    # game start.
    start_bread: int = 30
    start_treasury: float = 1000.0
    # v0.17: bumped 50 → 100. The starter city seeds enough houses for
    # 100 citizens at tier-1 capacity (shacks at 0.5× evolve to insulae
    # at 1.0× within ~10 ticks once water/food coverage kicks in). 50
    # was a relic from v0.4 when the starter only had 5 houses; with
    # the v0.17 expansion to 12 houses the cap is a healthy 120 at
    # full tier-1 evolution.
    start_population: int = 100
    start_happiness: float = 60.0

    # ── v0.16: nutrient diversity bonus ──────────────────────────────────
    # When the city has all 10 nutrients in stock simultaneously (bread,
    # vegetables, fruits, meat, fish, cheese, oil, honey, spice, wine),
    # happiness gets a flat bonus on top of the base, and population
    # growth runs faster. This is the headline "feed your city well
    # and it thrives" loop the v0.16 nutrient system was added for.
    #
    # The bonus scales linearly with the count of nutrients in stock,
    # so the player feels each new chain coming online. With 0
    # nutrients (bread out, no luxury) the bonus is 0; with all 10 it
    # peaks at the values below.
    nutrient_diversity_full_count: int = 10
    nutrient_diversity_happiness_max: float = 15.0
    # Multiplicative bonus on the per-tick growth rate when full. The
    # population check still gates on happiness/food/housing the same
    # way; this just makes the *amount* of growth on a successful
    # check ~1.5× faster. Set to 1.0 to disable the growth bonus.
    nutrient_diversity_growth_mult_max: float = 1.5

    # ── v0.4: house evolution ────────────────────────────────────────────
    # Houses check evolution every N ticks. Lower = snappier feedback.
    house_evolution_check_interval: int = 5
    # Tier-up requires consecutive successful checks (avoids flicker).
    house_evolution_streak: int = 2
    # Per-tier housing capacity multiplier vs base. Tier 0 = shack (small),
    # tier 4 = villa (luxurious). Index by tier.
    house_tier_capacity_mult: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 3.0)
    # Per-tier tax multiplier — wealthier houses pay more per resident.
    house_tier_tax_mult: tuple[float, ...] = (0.5, 1.0, 1.3, 1.8, 2.5)

    # ── v0.5: house goods consumption ─────────────────────────────────────
    # How many ticks after a delivery walker passes a house counts as
    # "still supplied" with that good. Higher = more forgiving (you can
    # cover a 20-house block with one walker rotating through). Lower =
    # punishing (walkers per house must be near 1:1).
    house_supply_memory_ticks: int = 30

    # ── v0.8: water gating & rebellion pressure ──────────────────────────
    # Fraction of farm output retained when water service coverage is below
    # 1.0. A farm with no water in range still produces this fraction so
    # the early game isn't impossible — the farmhand draws water from the
    # nearest well/aqueduct on foot. Cap (1.0) means "fully watered, no
    # penalty". Anywhere in between scales linearly with coverage.
    farm_water_min_factor: float = 0.4
    # Tax is collected per *fed* citizen, not per raw citizen. This is the
    # single biggest fix for "perpetual growth even when starving": a city
    # whose food balance is -50% pays half tax. The fraction is clamped
    # to [tax_min_fed_fraction, 1.0] so a brief food gap doesn't zero
    # revenue (some citizens always have stored food at home).
    tax_min_fed_fraction: float = 0.2
    # Houses without water service entirely (coverage 0.0) cap their
    # happiness contribution. Empirically this is what makes a player
    # *feel* the pressure — a thirsty city's happiness ceiling drops fast.
    house_no_water_happiness_penalty: float = -20.0

    # Rebels: a sustained food deficit makes citizens turn hostile. We
    # spawn rebel walkers from existing house tiles (not map edges — these
    # are *your* people). Tracked via a "starvation pressure" counter
    # that ramps up while food is short and decays back when fed.
    rebel_pressure_threshold: float = 1.0   # pressure level that triggers a spawn
    rebel_pressure_per_starve_tick: float = 0.05
    rebel_pressure_decay: float = 0.02      # per tick when fed
    rebel_max_concurrent: int = 8

    # ── v0.8: Caesar requests ────────────────────────────────────────────
    # Caesar asks for tribute every N months on average (Gaussian-ish via
    # uniform jitter). Each request demands either money or soldiers; the
    # player has a deadline to comply. Failing has consequences — happiness
    # tanks, an extra raid is queued, or rebel pressure spikes.
    caesar_request_interval_months_min: int = 12
    caesar_request_interval_months_max: int = 24
    caesar_deadline_ticks: int = 200
    caesar_failure_happiness: float = -25.0

    # ── v0.8: diplomacy & trade route cost ───────────────────────────────
    # Diplomatic points accrue slowly from the senate (per tick) and
    # quickly from honoured Caesar requests. Trade routes cost both money
    # and points — a city with no senate and a record of ignoring Caesar
    # cannot expand its commerce.
    diplomacy_per_senate_tick: float = 0.1
    diplomacy_per_caesar_honoured: float = 25.0
    diplomacy_per_caesar_failed: float = -40.0
    diplomacy_start: float = 20.0
    trade_route_cost_money: int = 800
    trade_route_cost_diplomacy: int = 30

    # ── v0.9: building decay ──────────────────────────────────────────
    # Master switch — flip to False for sandbox mods that don't want
    # the decay layer at all.
    decay_enabled: bool = True
    # Buildings are checked every N ticks (cheap; default 5 = every
    # 2.5 seconds at 2 ticks/sec). The decay amount is scaled to keep
    # the rate the same regardless of this interval.
    decay_check_interval: int = 5
    # Condition lost per tick at zero maintenance coverage. With the
    # default 0.05/tick, an unmaintained building goes from 100 to 0 in
    # 2000 ticks ≈ 16 in-game minutes. Engineer post coverage 1.0
    # zeroes the decay; 0.5 halves it.
    decay_rate_per_tick: float = 0.05
    # Refund fraction when a building collapses (vs. demolish, which
    # refunds 50%). Lower is more punishing.
    decay_collapse_refund: float = 0.20
    # v0.19.x: engineer post REPAIRS — covered buildings regenerate
    # condition. Spec: "engineers should be possible to fix building
    # maintenance needs fixing rate is one percent per tick". So a
    # building under engineer-post coverage gains 1 condition per
    # game tick (capped at 100). The repair only kicks in when
    # coverage > 0; partial coverage scales the repair linearly so
    # half-coverage from a fading post heals at half-rate. The
    # repair runs on the same decay_check_interval cadence as the
    # decay system itself; the per-check amount is scaled up to
    # match the spec's per-tick rate (1% × interval).
    engineer_repair_per_tick: float = 1.0

    # ── v0.14: terrain feature depletion ─────────────────────────────────
    # Some natural-resource features are exhaustible — chop a forest
    # long enough and it's gone, mine a vein long enough and it
    # collapses. Others (stone deposits, fertile soil, groundwater)
    # are inexhaustible by design: stone is geologically unlimited at
    # this scale, soil regenerates with crop rotation, groundwater is
    # capped by the well's catchment (drought is a separate event).
    #
    # Reserves are in "production units" — a forest with 800 reserves
    # supports 800 wood units of extraction before depletion. At a
    # lumber mill's full output of 8 wood/tick that's 100 ticks
    # (50 in-game seconds at 2 ticks/sec) per tile; with a 1×1 mill
    # on one forest tile, a forest patch lasts ~30 game-minutes of
    # full operation.
    #
    # Setting these to None disables depletion for that feature, which
    # is also the saveload pre-v11 default.
    feature_reserves_forest: float = 800.0
    feature_reserves_gold_vein: float = 400.0
    feature_reserves_copper_vein: float = 400.0
    # v0.18: iron vein reserves. Same depletion budget as gold/copper —
    # the player asked for explicit iron deposits in the editor, and
    # economically iron is consumed at roughly the same cadence as
    # gold/copper output (one mine, ~6/tick), so the balance lines up.
    feature_reserves_iron_vein: float = 400.0


# v0.14: depletion table by feature id. Built outside the Balance
# dataclass so dict-typed fields don't break frozen-dataclass behaviour.
# Modders can monkey-patch this for harder/easier scenarios. None
# means "inexhaustible" — the per-tick debit is skipped entirely.
FEATURE_RESERVES_DEFAULT: dict[str, float | None] = {
    "forest":         800.0,
    "gold_vein":      400.0,
    "copper_vein":    400.0,
    "iron_vein":      400.0,    # v0.18
    "stone_deposit":  None,    # inexhaustible
    # v0.25: fertile_soil is now depletable. A 35000-wheat budget per
    # tile means a farm at 12 wheat/tick × 1.5 fertile bonus = 18/tick
    # drains a tile in ~1950 ticks (~16 minutes at 2 ticks/sec) before
    # the soil reverts to plain grass and the +50% bonus disappears.
    # Player can move the farm or accept the unbuffed yield.
    "fertile_soil":   35000.0,
    "groundwater":    None,    # inexhaustible (drought handles temporary loss)
    # v0.25: fish stocks on water tiles. A fishery extracts 350 fish
    # per fishy tile per tick (set on the fishery's `production.fish`
    # rate × tile count). 35000 fish per tile → 100 ticks (~50s) to
    # deplete one tile before the texture clears and the water
    # reverts to plain (non-fishing) water.
    "fish":           35000.0,
    # v0.26: clay deposits join the depletable extractive features.
    # Larger budget than ore veins because the clay_pit consumes
    # nothing and produces 10 clay per tick — a 600-clay vein gives
    # ~60 ticks (~30 seconds) of full output, comparable to the
    # iron-mine playthrough cadence.
    "clay_deposit":   600.0,
}


# Default balance instance used by the game. Tests and tools can construct
# their own Balance() with overrides without monkey-patching this one.
BALANCE = Balance()


# Per-tier service requirements. A house at tier N needs the union of all
# requirements at tiers 0..N+1 to evolve to tier N+1; if it fails any of the
# requirements at its current tier, it devolves.
#
# Lives outside the Balance dataclass because dicts aren't hashable for a
# frozen dataclass field. Modders can monkey-patch this list at startup.
#
# v0.6: tier-3 picks up education (children must learn to read), tier-4
# picks up health (you don't get to live in a villa if the well makes you
# sick). These are *additions* to the v0.5 list, not rewrites — old saves
# load fine and houses re-evaluate against the new requirements.
# v0.7: tier-1 picks up a food requirement. v0.6 let any house tagged with
# "well in range" promote from shack (tier 0) to insula (tier 1) immediately
# at game start, which made the first tier feel automatic rather than
# earned. Adding food: 0.5 means a market walker has to actually reach the
# block before the upgrade fires — which is the same gating that tier 2
# already had, just stricter at the entry point.
HOUSE_TIER_REQUIREMENTS: list[dict[str, float]] = [
    {},                                                              # tier 0 (shack)
    {"water": 0.5, "food": 0.5},                                     # tier 1 (insula)
    {"water": 1.0, "food": 1.0},                                     # tier 2 (simple domus)
    {"water": 1.0, "food": 1.0, "religion": 0.5, "education": 0.5},  # tier 3 (domus)
    {"water": 1.0, "food": 1.0, "religion": 1.0,                     # tier 4 (villa)
     "entertainment": 1.0, "education": 1.0, "health": 1.0},
]


# v0.5 additions: goods that must be *delivered* to a house (not just
# produced somewhere) for it to reach a given tier. Walker delivery counts;
# global stockpile alone does not. The supply-memory window in
# Balance.house_supply_memory_ticks controls how long after a delivery the
# house still counts as supplied — so a walker doesn't need to pass every
# single tick.
#
# Indexed identically to HOUSE_TIER_REQUIREMENTS: index N is the
# requirement to *be at* tier N. Empty entries mean "no goods needed".
HOUSE_TIER_GOODS: list[set[str]] = [
    set(),                              # tier 0 (shack)
    set(),                              # tier 1 (insula)
    set(),                              # tier 2 (simple domus) — services-only
    {"oil"},                            # tier 3 (domus) needs olive oil
    {"oil", "pottery", "planks"},       # tier 4 (villa) needs oil, pottery + fine planks
    # v0.11: planks added at tier 4 — fine houses are timber-framed with
    # finished interiors. The lumber→sawmill→planks chain plus a market
    # in delivery range is required for villas; tier-3 domus still gets
    # by on services + oil. Wine remains a non-gating luxury.
]


# v0.6: combat tuning. Soldier vs. enemy is a simple HP exchange — no
# armour types, no morale, just numbers. Modders can tune these without
# touching the walker code.
COMBAT_DEFAULT_HP: int = 30
COMBAT_SOLDIER_DAMAGE: int = 10
COMBAT_ENEMY_DAMAGE: int = 8
# Defence service intensity at the soldier's tile boosts soldier damage
# multiplicatively by `1 + COMBAT_DEFENCE_BONUS * intensity`.
COMBAT_DEFENCE_BONUS: float = 0.5
# Probability per tick that a barbarian raid spawns from a map edge once
# the city has at least RAID_MIN_POPULATION citizens. Capped by how many
# raiders are already on the map.
RAID_TICK_CHANCE: float = 0.005
RAID_MIN_POPULATION: int = 80
RAID_MAX_CONCURRENT: int = 6


# v0.22: equipment-based combat efficiency, soldier morale, retreat.
# A Soldier consumes one `weapons` from the city stockpile when it
# spawns; if none is available, it spawns *unarmed* (armed=False) and
# does only COMBAT_UNARMED_DAMAGE_MULT × its base damage in combat —
# the default 0.0 means an unarmed soldier is a sitting duck. Tune
# this up (e.g. 0.25) to model fists-and-rocks defence.
COMBAT_UNARMED_DAMAGE_MULT: float = 0.0
# Initial morale for a freshly spawned soldier (range 0..1). Armed
# soldiers spawn at 1.0; unarmed soldiers at COMBAT_UNARMED_INITIAL_MORALE
# (lower because they know they're underequipped).
COMBAT_MORALE_INITIAL: float = 1.0
COMBAT_UNARMED_INITIAL_MORALE: float = 0.5
# Below this morale fraction, soldiers stop engaging and walk back
# toward their home tile (retreat). Mirrored in
# diagnostics.ProductionDiagnostics.MORALE_THRESHOLD so the LOW_MORALE
# diagnostic cause and the runtime retreat trigger off the same number.
COMBAT_MORALE_THRESHOLD: float = 0.4
# How much morale a soldier loses each tick when an adjacent ally
# soldier dies, or when *they* take damage. The default 0.15 means
# ~3 bad ticks crater a soldier into retreat — enough to make
# unarmed-vs-armed clashes visibly demoralising without softening
# armed-vs-armed brawls into perma-retreat.
COMBAT_MORALE_DECAY_ON_LOSS: float = 0.15
# Per-tick recovery while no enemies are in sight and HP is full.
# Slow on purpose: a defeated army shouldn't bounce back in 5 ticks.
COMBAT_MORALE_RECOVERY: float = 0.01
# Soldier retreats below this HP fraction (independent of morale).
# A soldier at 30% HP heads home regardless of how brave it feels.
COMBAT_RETREAT_HP_FRAC: float = 0.3
# v0.22: retreating soldiers don't fight at full strength but they
# aren't pure prey either — they swing at this multiplier as a fighting
# retreat. 0.5 means a routing army still bleeds the pursuer for half
# damage; 0.0 would mean the routing animation is "stand and die",
# which feels punishing in a way that surprised playtesters.
COMBAT_RETREAT_DAMAGE_MULT: float = 0.5

# ── Post-v0.28: enemies attack the city itself, not just soldiers ─────
# Probability, per enemy walker per tick, that an adjacent (Chebyshev≤1)
# citizen is killed outright. 0.5 means roughly half the time a wandering
# citizen is in the wrong tile next to a raider, they die — enough to
# create a real reason to garrison the city centre, not so much that a
# single uncontested enemy genocides the population in one tick.
COMBAT_ENEMY_KILLS_CITIZEN_CHANCE: float = 0.5
# Damage dealt to a building per tick when an enemy is *standing on its
# footprint*. Building HP defaults to BUILDING_DEFAULT_HP, so 2 dmg/tick
# means an undefended house falls in ~15 ticks — long enough for the
# player to react if they're watching, fast enough that ignoring raiders
# costs real infrastructure.
COMBAT_ENEMY_BURN_DAMAGE: int = 2
# Default HP for any building under enemy attack. Stored lazily inside
# ``game_map.building_state[(orow, ocol)]['burn']`` only after the first
# hit — buildings that never get attacked carry no extra state, and the
# save format inherits the persistence for free since ``building_state``
# already round-trips through saveload.
BUILDING_DEFAULT_HP: int = 30

# ── v0.29: cavalry shock, ranged-vs-buildings, persistent fire ─────────
# Damage multiplier applied to a cavalry soldier's *first* strike on a
# given enemy target. Models the historical reality that a mounted
# charge connecting at speed is far more devastating than the subsequent
# melee back-and-forth. Tracked per-(soldier, enemy id) pair; once the
# soldier has struck a target, the bonus is consumed — disengaging and
# re-engaging the same target does NOT restore it (otherwise a single
# cavalry walker could whittle a tank enemy down with repeated
# "charges", which feels wrong). A new enemy is a new shock event. The
# multiplier stacks with the existing defence / armed / retreat
# multipliers (so a routing cavalry unit on shock still hits at
# ``base * 1.75 * 0.5 = ~0.875 ×base`` — better than no shock, but not
# at full charge power).
COMBAT_CAVALRY_SHOCK_MULT: float = 1.75

# v0.29: ranged units (bowmen, ballistae) can damage adjacent buildings
# at this multiplier of their base damage. The original v0.6 combat
# model only let soldiers hit enemies; v0.28 let enemies hit buildings;
# v0.29 closes the symmetry — bowmen lobbing fire arrows into a
# besieged structure (e.g. an enemy hiding in a captured granary) now
# actually damage it. Set to 0.0 to disable (legacy behaviour). 0.5
# means a damage-8 bowman deals 4 building damage per tick — enough
# to matter, low enough that the player needs the targeting cue to
# bring real pressure on a structure. Melee soldiers do NOT get this
# capability — they have no realistic way to set a stone insula on
# fire while standing next to it.
COMBAT_RANGED_BUILDING_DAMAGE_MULT: float = 0.5

# v0.29: when an enemy burns a building, the building is now flagged
# ``on_fire=True`` persistently in building_state. Even after the
# enemy walks away (or dies), the fire keeps dealing this much damage
# per tick until a Fireman walker extinguishes it or the building
# collapses. Set lower than COMBAT_ENEMY_BURN_DAMAGE so an unattended
# fire is *slow* — the player has time, but ignoring fires is
# catastrophic. Default 1 means a 30-HP house burns out in ~30 ticks
# of unattended fire (vs. ~15 ticks with an enemy actively burning
# it). The "fire keeps burning" rule is what creates the new gameplay
# loop: prefectures must be reachable from the population centre or
# raids leave permanent scars.
COMBAT_FIRE_PROPAGATION_DAMAGE: int = 1

# v0.29: each tick a Fireman walker is adjacent (Chebyshev ≤ 1) to an
# on-fire building, the building's ``burn`` level is reduced by this
# amount. When ``burn`` reaches 0, the ``on_fire`` flag clears and
# the building stops self-damaging — at which point engineer-post
# coverage can resume HP repair via the existing decay system.
# Default 4 means a 4–5 tick visit by one fireman puts out the
# accumulated burn from one enemy hit (2 burn). Two firemen
# overlapping clear it in a single tick. Tunable upward to make
# firefighting feel cheap; downward to make raids more devastating.
COMBAT_FIREMAN_EXTINGUISH_RATE: int = 4

# v0.29: engineer posts no longer repair buildings that are currently
# on fire (``building_state[...].get('on_fire', False) is True``). The
# decay system reads this gate before applying engineer repair, so the
# fireman must arrive *first*. The flag is the single source of truth;
# the new ``COMBAT_FIRE_PROPAGATION_DAMAGE`` per-tick damage keeps
# decrementing HP regardless of engineer coverage as long as on_fire
# is true. Once a fireman has driven burn back to 0, on_fire clears
# and the engineer's repair tick resumes normally — modelling the
# real-world ordering (extinguish, then rebuild).
# (No constant needed for the gate itself — it's a hard-coded branch.)
