"""Scenario-specific starter layouts and skirmish wave scheduler.

Holds the deterministic city seeds for the default, gallic_war, and
skirmish scenarios, plus the per-tick wave dispatcher for skirmish. These
were extracted from game_window.py for clarity — the layouts are pure
data, the methods read only `self.scenario`, `self.game_map`, `self.game_time`,
`self.walker_manager`, and the SKIRMISH_* class constants defined on
CaesarGameWindow."""
from __future__ import annotations

from constants import COLOR_GOLD, COLOR_RED

class ScenarioLayoutsMixin:
    """ScenarioLayoutsMixin — see module docstring."""

    def _place_starter_city(self) -> None:
        # Starter city is set up to be road-connected so the new
        # `requires_road` rules don't immediately starve the player. A
        # starter well covers the houses so they don't all immediately
        # devolve to tier 0 in the first few minutes.
        #
        # v0.5: seeds an olive farm + olive press supply chain so the
        # delivery walker mechanic is visible from frame zero.
        # v0.6: also drops a school, a clinic, and a prefecture in range
        # of the houses so a new player sees the new service overlays
        # actually do something. Adds a barracks too — the soldiers
        # patrolling near the starter block teach the combat layer
        # before the first raid (which only fires above pop=80).
        # v0.11: starter city seeds the new supply chain so the player
        # sees flour and planks land in the warehouses without having
        # to build five buildings before anything moves. Wheat farm + a
        # tiny windmill + bakery is the food chain; lumber mill + a
        # sawmill is the construction chain. Tier-3+ houses still need
        # markets in range to receive the chain output via walkers.
        # v0.15: granary, windmill, warehouse explicitly seeded — the
        # v0.14 starter inventory put start_food at 9500 against a
        # base_storage of 2000, so the very first food production tick
        # used to clamp the stock down (the food-not-producing
        # screenshot bug). With a granary (+800) and a warehouse
        # (+500) seeded by the starter city, the headroom is 3300
        # at frame zero — comfortable for the start_food buffer
        # (now 9870) plus production while the player builds out.
        # Roads are extended so every new building is connected.
        if self.scenario == "gallic_war":
            starters = self._gallic_starter_layout()
        elif self.scenario == "skirmish":
            # v0.30: skirmish layout — one of each military building.
            starters = self._skirmish_starter_layout()
        else:
            starters = self._default_starter_layout()
        # Starter buildings use the seeding bypass (economy=None) — the
        # starter material costs would otherwise wipe out the player's
        # opening planks/stone_blocks before they've taken a turn.
        for bid, r, c in starters:
            self.game_map.place_building(r, c, bid, economy=None)

    def _default_starter_layout(self) -> list[tuple[str, int, int]]:
        # v0.17: starter expanded from 5 → 12 houses. With
        # start_population=100, the previous 5-house starter only
        # offered 25 capacity at tier 0 (0.5× base_housing of 10),
        # forcing the city to shrink immediately. 12 houses = 60 at
        # tier 0, 120 at tier 1. The well and services below all sit
        # within radius of every starter house so tier-up triggers
        # within ~10 ticks.
        return [
            # ── 12 houses in a 4x3 block at rows 11-13, cols 7-10 ──
            # The original 5-house cluster expanded to a denser grid.
            # The well at (11, 9) sits in the centre so all 12 houses
            # are within service radius (default 4 tiles).
            ("house", 11, 7), ("house", 11, 8),
            ("house", 11, 10),
            ("house", 12, 7), ("house", 12, 8), ("house", 12, 9), ("house", 12, 10),
            ("house", 13, 7), ("house", 13, 8), ("house", 13, 9), ("house", 13, 10),
            # 12th house just south so we hit exactly 12.
            ("house", 14, 7),
            ("farm", 15, 8),
            ("lumber_mill", 14, 11),
            ("market", 12, 12),
            ("road", 12, 11), ("road", 13, 11),
            ("road", 13, 12),
            # Make sure the farm and lumber mill are road-adjacent.
            ("road", 14, 8), ("road", 14, 9), ("road", 14, 10),
            # Starter well at (11, 9) covers all 12 houses (all within
            # radius 4). Sits in the gap between the (11,8) and (11,10)
            # houses that we left open for it.
            ("well", 11, 9),
            # ── v0.5 olive supply chain ────────────────────────────────
            ("road", 13, 13), ("road", 13, 14), ("road", 13, 15),
            ("road", 13, 16), ("road", 13, 17),
            ("olive_farm", 14, 14),
            ("olive_press", 12, 16),
            # ── v0.11 wheat→flour→bread chain ──────────────────────────
            ("sawmill", 14, 6),
            ("road", 15, 6),
            # ── v0.6 civic services ────────────────────────────────────
            ("school", 16, 9),
            ("road", 17, 8),
            # Clinic + prefecture moved to ensure they cover the
            # expanded 12-house block (radius 4 from origins).
            ("clinic", 10, 8),
            ("prefecture", 10, 10),
            ("engineer_post", 14, 12),
            # ── v0.6 military foundation ───────────────────────────────
            ("barracks", 17, 12),
            ("road", 16, 12),
            # ── v0.13: stone chain ─────────────────────────────────────
            ("road", 4, 2), ("road", 4, 3), ("road", 4, 4), ("road", 5, 4),
            ("road", 6, 4), ("road", 7, 4), ("road", 8, 4), ("road", 9, 4),
            ("road", 10, 4),
            ("quarry", 2, 2),
            ("stonemason", 4, 5),
            # ── v0.15 storage + grain chain ────────────────────────────
            ("granary", 10, 5),
            ("road", 9, 5), ("road", 9, 6),
            ("road", 12, 5), ("road", 12, 6),
            ("warehouse", 11, 13),
            ("road", 10, 13), ("road", 10, 14),
            ("windmill", 16, 2),
            ("road", 16, 4), ("road", 16, 5),
            ("bakery", 12, 18),
            ("road", 12, 19),
        ]

    def _gallic_starter_layout(self) -> list[tuple[str, int, int]]:
        """v0.15: Gallic War scenario — a more rugged opening with the
        houses on the eastern bank near the river. Road spine on row
        13 runs east-west; houses sit north (row 11..12), industries
        south (row 15..16). Picked to feel different from the default
        and to put the player closer to the new mountain/hills terrain
        added on the western side of the map.
        """
        starters: list[tuple[str, int, int]] = []
        # Road spine east-west on row 13, columns 6..28.
        for c in range(6, 29):
            starters.append(("road", 13, c))
        # Housing block north of the spine. v0.17: expanded from 6 to
        # 12 houses to support start_population=100. Two 4-wide rows
        # at rows 10..12, cols 25..28. The well at (11, 29) covers
        # the entire block.
        starters += [
            ("house", 10, 26), ("house", 10, 27), ("house", 10, 28),
            ("house", 11, 26), ("house", 11, 27), ("house", 11, 28),
            ("house", 12, 26), ("house", 12, 27), ("house", 12, 28),
            ("house", 14, 26), ("house", 14, 27), ("house", 14, 28),
        ]
        # Farm 2×2 south of the spine on the east side.
        starters += [
            ("farm", 15, 24),         # 2×2 → (15,24)..(16,25)
            ("road", 14, 24), ("road", 14, 25),
        ]
        # Windmill 2×2 north of the housing, off the col-25 ladder.
        starters += [
            ("windmill", 9, 24),      # 2×2 → (9,24)..(10,25)
            ("road", 11, 25),
        ]
        # Granary 2×2 north-east of the housing.
        starters += [
            ("granary", 9, 27),       # 2×2 → (9,27)..(10,28)
        ]
        # Lumber mill on the existing forest tile at (14, 11).
        # Sawmill nearby to refine wood → planks. Both connect to
        # the row-13 spine via roads on col 10.
        starters += [
            ("lumber_mill", 14, 11),
            ("sawmill", 12, 11),
            ("road", 14, 10), ("road", 12, 10),
        ]
        # Market on the spine.
        starters += [
            ("market", 12, 23),
            ("road", 11, 23),
        ]
        # Civic services around the houses.
        starters += [
            ("well", 11, 29),
            ("clinic", 14, 27),
            ("prefecture", 14, 26),
            # School 2×2 well west of the housing on row 11..12.
            ("school", 11, 18),       # 2×2 → (11,18)..(12,19)
        ]
        # Engineer post south of the spine.
        starters += [
            ("engineer_post", 14, 19),
        ]
        # Warehouse south of the spine, west of the engineer post.
        starters += [
            ("warehouse", 14, 21),    # 2×1 → (14,21)..(14,22)
        ]
        return starters

    # ── v0.30: Skirmish scenario ─────────────────────────────────────────
    # Tunable class attributes so playtesters can subclass / monkey-
    # patch without diving into the dispatch logic.
    SKIRMISH_UNITS_PER_BUILDING: int = 10
    # First wave at t=60 (~30s @ 2 TPS), then every 120 ticks (~60s).
    # Enemy count starts at SKIRMISH_WAVE_BASE_COUNT and grows by
    # SKIRMISH_WAVE_GROWTH each wave. After SKIRMISH_MAX_WAVES the
    # scheduled waves stop and the game falls back to ambient raids.
    SKIRMISH_FIRST_WAVE_TICK: int = 60
    SKIRMISH_WAVE_INTERVAL: int = 120
    SKIRMISH_WAVE_BASE_COUNT: int = 4
    SKIRMISH_WAVE_GROWTH: int = 2
    SKIRMISH_MAX_WAVES: int = 10

    def _skirmish_starter_layout(self) -> list[tuple[str, int, int]]:
        """v0.30: Skirmish starter layout.

        One of each military building, plus a prefecture (Fireman +
        safety service), a well (for the prefecture/fireman radius),
        and a road spine connecting them. Designed for the default
        40×30 grid.

        Building list (one of each — the layout doubles as a tour of
        every military unit type):

          * prefecture           → Fireman + safety service
          * tower (Watchtower)   → Scout Squadron
          * barracks             → Light Infantry
          * fort                 → Heavy Infantry Legionary
          * fort_cavalry         → Heavy Cavalry
          * archery_range        → Bowman
          * military_manufacture → Ballista

        Garrisons (barracks / fort / fort_cavalry / archery_range /
        military_manufacture) are 3×3 in this project, so their
        origins are spaced 4 columns apart with a single-column gap
        between footprints. Each garrison is pre-staffed to
        ``SKIRMISH_UNITS_PER_BUILDING`` units in
        ``WalkerManager.skirmish_pre_staff_garrisons`` after this
        layout is stamped.
        """
        starters: list[tuple[str, int, int]] = []
        # Garrisons (3×3) spaced 4 cols apart on rows 8..10. With
        # five 3×3 garrisons starting at cols 4, 8, 12, 16, 20 the
        # row occupies cols 4..22.
        garrisons = [
            ("military_manufacture", 8, 4),    # cols 4..6
            ("barracks",             8, 8),    # cols 8..10
            ("fort",                 8, 12),   # cols 12..14
            ("fort_cavalry",         8, 16),   # cols 16..18
            ("archery_range",        8, 20),   # cols 20..22
        ]
        starters += garrisons
        # Road spine east-west on row 12, just south of the garrison
        # block (row 11 is the southern edge of each garrison's
        # footprint, so row 12 is the first free row). Spans cols
        # 2..26 so it overhangs both ends — gives walkers room to
        # reach the prefecture / tower wings.
        for c in range(2, 27):
            starters.append(("road", 12, c))
        # Tower (1×1) at the western end, on the spine's row + 1
        # (row 13) so it has a line of sight south while still
        # bordering a road tile.
        starters += [("tower", 13, 2)]
        # Prefecture (1×1) + well (1×1) at the eastern end of the
        # spine, on row 13 (the prefecture needs road, and row 13
        # col 25 is adjacent to the spine).
        starters += [
            ("prefecture", 13, 25),
            ("well",       13, 23),
        ]
        return starters

    def _skirmish_init(self) -> None:
        """v0.30: per-game bootstrap for the skirmish scenario.

        Called from ``_start_new_game`` after ``_place_starter_city``
        has stamped the skirmish layout. Three jobs:

          1. Pre-staff every garrison to
             ``SKIRMISH_UNITS_PER_BUILDING`` via
             ``WalkerManager.skirmish_pre_staff_garrisons`` (which
             bypasses the weapons-stockpile gate — the skirmish is a
             combat sandbox, not a supply-chain puzzle).
          2. Seed enough materials (gold, iron, weapons, planks,
             stone_blocks) that the player can fund repairs and
             top-up garrison spawns during a long defence.
          3. Initialise ``self._skirmish_state``: a small dict the
             per-tick scheduler reads to know when the next wave is
             due and how many barbarians it should bring.
        """
        spawned = self.walker_manager.skirmish_pre_staff_garrisons(
            self.game_map,
            per_building=self.SKIRMISH_UNITS_PER_BUILDING,
        )
        # Combat materials. The skirmish layout includes a Military
        # Manufacture so the player CAN make ballistae, but the
        # supply chain (iron → weapons) needs starter stock to
        # bootstrap. Sized to last roughly through wave 5 if the
        # player isn't producing more.
        self.economy.resources["weapons"] = max(
            float(self.economy.resources.get("weapons", 0)), 40.0,
        )
        self.economy.resources["iron"] = max(
            float(self.economy.resources.get("iron", 0)), 200.0,
        )
        self.economy.resources["planks"] = max(
            float(self.economy.resources.get("planks", 0)), 80.0,
        )
        self.economy.resources["stone_blocks"] = max(
            float(self.economy.resources.get("stone_blocks", 0)), 60.0,
        )
        self.economy.treasury = max(float(self.economy.treasury), 3000.0)
        # Wave scheduler state. Read by ``_skirmish_tick``.
        self._skirmish_state = {
            "next_wave_tick": self.SKIRMISH_FIRST_WAVE_TICK,
            "wave_index": 0,
        }
        # Skirmish is a combat sandbox — disable ambient economic
        # events (Drought, Bumper Harvest, etc.) so they don't muddy
        # the picture. The dedicated wave events fire on their own
        # schedule below.
        if hasattr(self, "event_manager"):
            self.event_manager.disable_random_events = True
        self._notify(
            f"Skirmish: {spawned} soldier(s) ready. First wave in "
            f"{self.SKIRMISH_FIRST_WAVE_TICK} ticks.",
            COLOR_GOLD,
        )

    def _skirmish_tick(self) -> None:
        """v0.30: per-tick wave dispatch for the skirmish scenario.

        Called from ``_game_tick`` only when ``self.scenario ==
        "skirmish"``. Reads ``self._skirmish_state``, compares
        ``self.game_time`` against the next scheduled wave, and if
        due, dispatches one invasion wave via
        ``WalkerManager.force_invasion_wave``.

        Each wave's enemy count is::

            count = SKIRMISH_WAVE_BASE_COUNT
                    + SKIRMISH_WAVE_GROWTH × wave_index

        ...so wave 1 has 4 enemies, wave 5 has 12, wave 10 has 22.
        After ``SKIRMISH_MAX_WAVES`` the scheduler stops queueing
        new waves and the ambient raid spawner takes over.
        """
        state = getattr(self, "_skirmish_state", None)
        if state is None:
            return
        if self.game_time < state["next_wave_tick"]:
            return
        wave_index = state["wave_index"] + 1
        if wave_index > self.SKIRMISH_MAX_WAVES:
            # Final wave already fired; let ambient raids take over.
            # Park `next_wave_tick` far in the future so this branch
            # short-circuits cheaply on every subsequent tick.
            state["next_wave_tick"] = 10**9
            return
        count = (
            self.SKIRMISH_WAVE_BASE_COUNT
            + self.SKIRMISH_WAVE_GROWTH * (wave_index - 1)
        )
        spawned = self.walker_manager.force_invasion_wave(
            self.game_map, count=count,
        )
        state["wave_index"] = wave_index
        state["next_wave_tick"] = (
            self.game_time + self.SKIRMISH_WAVE_INTERVAL
        )
        self._notify(
            f"Skirmish wave {wave_index}/{self.SKIRMISH_MAX_WAVES}: "
            f"{spawned} barbarian(s) at the map edge!",
            COLOR_RED,
        )
