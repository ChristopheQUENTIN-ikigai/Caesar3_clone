"""Game map — 2D grid: terrain, building placement, rendering.

Coordinate conventions (read this once, then forget about it):

  * The grid is indexed by `(row, col)` with row=0 at the bottom.
  * World space uses `(x, y)` where x grows to the right, y grows upward.
  * A grid cell `(row, col)` lives at world position
        x = col * TILE_SIZE
        y = row * TILE_SIZE
    so `col → x`, `row → y`. Always.

  * Building sizes are stored as `(width, height) = (cols, rows)`.

If you need to change the projection (e.g. isometric), only `grid_to_world`
and `world_to_grid` should change. Walker and HUD code go through them.
"""
from __future__ import annotations

import arcade

from building import Building, BuildingRegistry
from constants import (
    GRID_COLS, GRID_ROWS, TILE_SIZE,
    COLOR_GRASS, COLOR_GRASS_ALT, COLOR_WATER, COLOR_GRID_LINE,
    COLOR_HILLS, COLOR_MOUNTAINS, COLOR_DESERT,
    COLOR_HIGHLIGHT, COLOR_INVALID,
    TERRAIN_GRASS, TERRAIN_GRASS_ALT, TERRAIN_WATER,
    TERRAIN_HILLS, TERRAIN_MOUNTAINS, TERRAIN_DESERT,
    FEATURE_FOREST, FEATURE_GOLD_VEIN, FEATURE_COPPER_VEIN, FEATURE_IRON_VEIN,
    FEATURE_STONE_DEPOSIT, FEATURE_FERTILE_SOIL, FEATURE_GROUNDWATER,
    FEATURE_FISH, FEATURE_CLAY_DEPOSIT,
    FEATURE_COLORS,
)
from signals import signals
from textures import TextureRegistry


# A building cell stores (building_id, origin_row, origin_col).
BuildingCell = tuple[str, int, int]

# Terrain id → texture name on disk (assets/textures/terrain/<name>.png).
_TERRAIN_TEX_NAMES = {
    TERRAIN_GRASS: "grass",
    TERRAIN_GRASS_ALT: "grass_alt",
    TERRAIN_WATER: "water",
    # v0.15: hills/mountains have placeholder textures. If the PNGs
    # aren't present the texture registry returns None and we fall
    # back to the colour-rectangle path in `_draw_terrain` below.
    TERRAIN_HILLS: "hills",
    TERRAIN_MOUNTAINS: "mountains",
    # v0.18: desert renders as a sandy yellow-brown tile. As with
    # hills/mountains there is no PNG yet — `_draw_terrain` falls
    # back to a colour fill from DESERT_COLOR if the texture is
    # absent from the registry.
    TERRAIN_DESERT: "desert",
}


# v0.21: human-readable labels for the middle-click extractable bubble.
# Keyed by feature id (and 'fish' for water-extracting fisheries). Falls
# back to a Title-Cased version of the id if unmapped.
_EXTRACTABLE_LABELS: dict[str, str] = {
    FEATURE_FOREST:        "Trees",
    FEATURE_GOLD_VEIN:     "Gold ore",
    FEATURE_COPPER_VEIN:   "Copper ore",
    FEATURE_IRON_VEIN:     "Iron ore",
    FEATURE_STONE_DEPOSIT: "Stone",
    FEATURE_FERTILE_SOIL:  "Fertility",
    FEATURE_GROUNDWATER:   "Groundwater",
    FEATURE_CLAY_DEPOSIT:  "Clay",
    "fish":                "Fish",
}


class GameMap:
    def __init__(
        self,
        registry: BuildingRegistry,
        textures: TextureRegistry | None = None,
        rows: int | None = None,
        cols: int | None = None,
    ):
        self.registry = registry
        self.textures = textures or TextureRegistry()
        # v0.26: grid dimensions are now per-instance so the map
        # editor can vary world size per scenario. Defaults to the
        # module-level GRID_ROWS / GRID_COLS so all existing code
        # paths (tests, saveload, the default new-game flow) keep
        # working without an explicit dimension. Callers that *do*
        # specify get those numbers honoured for every internal
        # array, bounds check, and minimap.
        self.rows: int = int(rows) if rows is not None else GRID_ROWS
        self.cols: int = int(cols) if cols is not None else GRID_COLS
        self.grid: list[list[BuildingCell | None]] = [
            [None for _ in range(self.cols)] for _ in range(self.rows)
        ]
        self.terrain: list[list[int]] = [
            [TERRAIN_GRASS for _ in range(self.cols)] for _ in range(self.rows)
        ]
        # v0.13: terrain features (forest, ore veins, fertile soil, …)
        # are a separate layer from the base grass/water terrain. None
        # for an empty cell; a feature id string otherwise. See
        # constants.FEATURE_* for the canonical values.
        self.terrain_features: list[list[str | None]] = [
            [None for _ in range(self.cols)] for _ in range(self.rows)
        ]
        # v0.14: per-feature-tile state keyed by (row, col). The
        # primary contents is `reserves: float | None` — None means
        # inexhaustible (stone, fertile soil, groundwater); a float
        # is decremented by extractors each tick until zero, at which
        # point the feature is "depleted" and the tile is downgraded
        # to plain grass on the visual layer (terrain_features cleared).
        # See `tap_feature` / `is_feature_depleted` below.
        self.terrain_feature_state: dict[tuple[int, int], dict] = {}
        self._generate_terrain()
        self._generate_terrain_features()
        self._init_feature_reserves()
        # v0.6: tile labels are gone — buildings now read from their texture.
        # The cache field is kept (empty) for back-compat with code paths
        # that called `_invalidate_label`. Removing it would break old
        # tests; keeping it is free.
        self._label_cache: dict[tuple[int, int], object] = {}
        # Per-building state keyed by (origin_row, origin_col). Stores
        # things that aren't on the immutable Building dataclass: house
        # tier, evolution streak counter, etc. Cleared on remove.
        self.building_state: dict[tuple[int, int], dict] = {}
        # v0.25: per-building construction progress, keyed by origin
        # cell. Holds the number of ticks elapsed since placement.
        # Entries only exist for buildings still under construction;
        # once a building reaches its `construction_ticks` threshold
        # the entry is removed and the building is treated as fully
        # operational (the rest of the simulation reads "absence of
        # key = ready"). Buildings with construction_ticks=0 never
        # acquire an entry, preserving legacy instant-build behaviour
        # for any data file that doesn't set the new field.
        self.construction_progress: dict[tuple[int, int], int] = {}

        # v0.56: cached render layers. The terrain and feature layers are
        # almost entirely static, yet the old _draw_terrain re-issued one
        # immediate-mode draw call PER TILE PER FRAME (~2,400 on a 40×30
        # map, ~8,000 on a 64×64 editor map) plus a line per grid edge.
        # We now build arcade SpriteLists once and let the GPU batch them
        # in a single draw call each, rebuilding lazily only when terrain
        # or features actually change (see invalidate_render_cache). Tiles
        # whose texture is missing (no PNG shipped — common in tests and
        # the placeholder asset set) fall back to the original colour/
        # glyph path, so textureless environments render identically.
        self._terrain_sprites: "arcade.SpriteList | None" = None
        self._feature_sprites: "arcade.SpriteList | None" = None
        self._grid_shapes = None
        # Tiles handled by the colour/glyph fallback (no texture). Built
        # alongside the sprite lists; iterated (culled to the viewport)
        # only when non-empty, so a fully-textured map pays nothing here.
        self._fallback_terrain: list[tuple[int, int, int]] = []
        self._fallback_features: list[tuple[int, int, str]] = []
        self._render_cache_dirty: bool = True

    # ── Terrain ───────────────────────────────────────────────────────────
    def _generate_terrain(self) -> None:
        for r in range(self.rows):
            for c in range(self.cols):
                self.terrain[r][c] = (
                    TERRAIN_GRASS if (r + c) % 2 == 0 else TERRAIN_GRASS_ALT
                )
                # River near the right edge.
                river_c = self.cols - 5
                if river_c <= c <= river_c + 1:
                    self.terrain[r][c] = TERRAIN_WATER
        # Small lake.
        lc_r, lc_c = self.rows - 6, 6
        for dr in range(-1, 2):
            for dc in range(-1, 2):
                rr, cc = lc_r + dr, lc_c + dc
                if 0 <= rr < self.rows and 0 <= cc < self.cols:
                    self.terrain[rr][cc] = TERRAIN_WATER

        # v0.15: hills and mountains for visual + gameplay variety.
        # Mountains form a small massif in the upper-right corner
        # (north of the gold vein at (3, 30..31)), and a second
        # smaller ridge in the lower-left near the lake. Hills
        # buffer both — they read as "foothills" leading up to the
        # peaks. The placement is deterministic and stays clear
        # of the starter cities (default rows 9..18 cols 2..19;
        # gallic rows 9..16 cols 6..29) so neither layout fails to
        # place against terrain. Stone deposits are inside the
        # foothills band (where you'd expect a quarry) — features
        # are an overlay, so this stacks visually without
        # conflicting with placement gates.
        self._generate_hills_and_mountains()

    def _generate_hills_and_mountains(self) -> None:
        """Lay down mountains and surrounding hills. Kept in its own
        method so test fixtures and the editor (a future feature)
        can reroute terrain without rewriting the river/lake logic.

        v0.26: every cell write is now bounds-checked. The hardcoded
        coordinates (e.g. col 25..30 for the northern massif) were
        safe at the 30×40 default but blow up the moment a smaller
        map is requested — adding a uniform guard keeps the
        procedural layout intact on the default size while
        gracefully no-op'ing the unreachable cells on small maps.
        """
        # Northern massif: mountains around (1..2, 26..29). These
        # sit just north of the gold vein at (3, 30..31), so the
        # mining area reads as "the slope below the peak".
        for r in range(0, 3):
            for c in range(25, 30):
                if not (0 <= r < self.rows and 0 <= c < self.cols):
                    continue
                # Sparse — a solid block of mountains looks like a wall.
                if (r * 5 + c * 3) % 4 != 0:
                    self.terrain[r][c] = TERRAIN_MOUNTAINS
        # Hills buffer around the massif (rows 0..4, cols 23..32).
        for r in range(0, 5):
            for c in range(23, 33):
                if 0 <= r < self.rows and 0 <= c < self.cols:
                    if self.terrain[r][c] in (TERRAIN_GRASS, TERRAIN_GRASS_ALT):
                        # Closer to the massif → more likely to be hill.
                        dist = max(abs(r - 1), abs(c - 27))
                        if dist <= 3 and (r + c) % 3 != 0:
                            self.terrain[r][c] = TERRAIN_HILLS

        # South-eastern foothills (rows self.rows-6..self.rows-1,
        # cols 28..36). Sits south of the copper vein at
        # (self.rows-4, self.cols-8..self.cols-7) → again, the
        # mining area is the slope leading up to the rocks.
        for r in range(self.rows - 3, self.rows):
            for c in range(self.cols - 12, self.cols - 5):
                if 0 <= r < self.rows and 0 <= c < self.cols:
                    if self.terrain[r][c] in (TERRAIN_GRASS, TERRAIN_GRASS_ALT):
                        if (r + c) % 3 == 0:
                            self.terrain[r][c] = TERRAIN_MOUNTAINS
                        elif (r * 2 + c) % 3 != 0:
                            self.terrain[r][c] = TERRAIN_HILLS

    def is_water(self, row: int, col: int) -> bool:
        return self.terrain[row][col] == TERRAIN_WATER

    def is_hills(self, row: int, col: int) -> bool:
        """v0.15: hills are passable but unbuildable."""
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return False
        return self.terrain[row][col] == TERRAIN_HILLS

    def is_mountains(self, row: int, col: int) -> bool:
        """v0.15: mountains are impassable and unbuildable."""
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return False
        return self.terrain[row][col] == TERRAIN_MOUNTAINS

    def is_desert(self, row: int, col: int) -> bool:
        """v0.18: desert is unbuildable but passable for walkers.
        Behaves like grass for traversal (walkers cross it freely)
        but rejects every building footprint just like mountains.
        """
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return False
        return self.terrain[row][col] == TERRAIN_DESERT

    def is_buildable_terrain(self, row: int, col: int) -> bool:
        """v0.15: True if a building's footprint can sit on this tile.
        Grass (both shades) is buildable; water/hills/mountains/desert are not.
        Port-style buildings still get a separate `needs_terrain='water'`
        gate inside `can_place` — this method is the negative form,
        used by the placement check to reject hills/mountains/desert.
        """
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return False
        return self.terrain[row][col] in (TERRAIN_GRASS, TERRAIN_GRASS_ALT)

    # v0.27: bridges. A finished bridge tile turns impassable water
    # into a road-equivalent crossing. `is_bridge_at` returns True
    # if the building at (row, col) is a bridges_water=True building.
    # `is_passable_for_walker` rolls up the full passability rule:
    # grass (any) / hills / desert / road are passable; water and
    # mountains are not — UNLESS the water tile is covered by a
    # finished bridge, in which case it's passable again. This is
    # the single entry point walkers should use; the legacy
    # `_impassable` helper in walkers.py was rewritten to call it.
    def is_bridge_at(self, row: int, col: int) -> bool:
        """True if a bridges_water=True building's footprint covers this
        tile AND the bridge has finished construction. An under-
        construction bridge does NOT carry walkers — fall in the water.
        """
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return False
        cell = self.grid[row][col]
        if cell is None:
            return False
        bid, orow, ocol = cell
        bd = self.registry.get(bid)
        if bd is None or not bd.bridges_water:
            return False
        # Half-built bridge: no walkers yet. The progress bar is over
        # the water; the player can see it isn't a crossing yet.
        if self.is_under_construction(orow, ocol):
            return False
        return True

    def is_passable_for_walker(self, row: int, col: int) -> bool:
        """v0.27: the single passability oracle for walker AI. Water
        and mountains are impassable; hills, desert, grass and roads
        are passable. A finished bridges_water building on a water
        tile flips it back to passable. Out-of-bounds is impassable.
        """
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return False
        t = self.terrain[row][col]
        if t == TERRAIN_MOUNTAINS:
            return False
        if t == TERRAIN_WATER:
            return self.is_bridge_at(row, col)
        # Grass (both shades), hills, desert — all passable.
        return True

    # ── v0.13: terrain features ──────────────────────────────────────────
    def _generate_terrain_features(self) -> None:
        """Seed the default map with a hand-placed set of natural-resource
        spots. The layout is deterministic — same map every new game —
        so the starter city's chains have predictable land underneath
        them. The map editor (v0.14) will let scenario authors place
        these freely.

        Distribution rationale:
          * Two forest patches near the centre — the lumber mill in the
            starter city sits next to one of them.
          * One stone deposit and one gold vein near the mountains
            (top-left and top-right quadrants).
          * Fertile soil along the middle latitudes — farms cluster
            here naturally.
          * Groundwater spots scattered for fountains away from the
            river.
          * Copper vein at the south edge for a future second metal
            chain.

        v0.26: each cell write is bounds-checked so smaller editor
        maps (e.g. 12×20) don't blow up on the hardcoded coordinates
        below. The default 30×40 layout is unaffected — every check
        passes inside it.
        """
        def safe_set(r: int, c: int, feat: str) -> None:
            if 0 <= r < self.rows and 0 <= c < self.cols:
                self.terrain_features[r][c] = feat
        # Forest patches: a 3x3 cluster near (10, 4) and a 2x2 near (16, 5).
        for r in range(9, 12):
            for c in range(3, 6):
                if (r + c) % 3 != 0:        # sparse fill, not a solid block
                    safe_set(r, c, FEATURE_FOREST)
        for r in range(15, 17):
            for c in range(4, 6):
                safe_set(r, c, FEATURE_FOREST)

        # Stone deposit: 2x2 in the upper-left.
        for r in range(2, 4):
            for c in range(2, 4):
                safe_set(r, c, FEATURE_STONE_DEPOSIT)

        # Gold vein: small 1x2 in the upper-right.
        safe_set(3, 30, FEATURE_GOLD_VEIN)
        safe_set(3, 31, FEATURE_GOLD_VEIN)

        # Copper vein: lower-right corner.
        safe_set(self.rows - 4, self.cols - 8, FEATURE_COPPER_VEIN)
        safe_set(self.rows - 4, self.cols - 7, FEATURE_COPPER_VEIN)

        # v0.26: iron vein — historically the generic `mine` accepted
        # gold/copper/iron, so the starter map didn't need an iron
        # patch. v0.26 split the three mines so each is restricted to
        # its own ore; without an iron_vein on disk the player can't
        # place an Iron Mine on the starter map. Seed two iron tiles
        # in the lower-left so the iron→smelter→weapon chain stays
        # buildable from frame zero.
        safe_set(self.rows - 5, 3, FEATURE_IRON_VEIN)
        safe_set(self.rows - 5, 4, FEATURE_IRON_VEIN)

        # v0.26: clay deposit — the Clay Pit now requires a
        # clay_deposit tile (consistent with the other extractive
        # industries). Seed a small patch on safe grass terrain so
        # the pottery chain is reachable from frame zero.
        for (r, c) in [(20, 25), (20, 26)]:
            if (0 <= r < self.rows and 0 <= c < self.cols
                    and self.terrain[r][c]
                    in (TERRAIN_GRASS, TERRAIN_GRASS_ALT)):
                self.terrain_features[r][c] = FEATURE_CLAY_DEPOSIT

        # Fertile soil band across the middle latitudes (rows 14-17).
        for r in range(14, 18):
            for c in range(7, 18):
                if (r * 7 + c * 3) % 5 == 0:   # ~20% density
                    if (0 <= r < self.rows and 0 <= c < self.cols
                            and self.terrain_features[r][c] is None):
                        self.terrain_features[r][c] = FEATURE_FERTILE_SOIL

        # Groundwater spots — three scattered.
        for (r, c) in [(8, 12), (12, 22), (20, 16)]:
            if 0 <= r < self.rows and 0 <= c < self.cols:
                self.terrain_features[r][c] = FEATURE_GROUNDWATER

        # v0.25: fish features on water tiles. Same reasoning as
        # fertile_soil for farms: a fishery on a water tile WITH a
        # `fish` feature catches fish; on plain water it catches
        # nothing. We sprinkle fish on a deterministic subset of the
        # river/lake tiles so the player has a few obvious fishing
        # spots near the starter coast and across the map.
        for r in range(self.rows):
            for c in range(self.cols):
                if self.terrain[r][c] != TERRAIN_WATER:
                    continue
                # ~25% density on water tiles, deterministic.
                if (r * 11 + c * 5) % 4 == 0:
                    self.terrain_features[r][c] = FEATURE_FISH

        # v0.13 starter convenience: ensure the starter city's lumber
        # mill at (14, 11) sits on a forest tile, and the farm at
        # (15, 7) overlaps a fertile-soil tile so the player sees the
        # +50% bonus from frame zero. These two edits make the v0.9
        # starter layout playable under the new feature gates without
        # rearranging buildings.
        # v0.26: bounds-checked so small editor maps don't blow up.
        if 0 <= 14 < self.rows and 0 <= 11 < self.cols:
            self.terrain_features[14][11] = FEATURE_FOREST
        if 0 <= 15 < self.rows and 0 <= 7 < self.cols:
            self.terrain_features[15][7] = FEATURE_FERTILE_SOIL

    def feature_at(self, row: int, col: int) -> str | None:
        """Return the terrain feature at one cell, or None.

        Out-of-bounds coordinates return None — defensive default for
        callers iterating around a building's footprint without
        bounds-checking themselves.
        """
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return None
        return self.terrain_features[row][col]

    def has_feature_in_footprint(
        self, row: int, col: int, building: Building, feature: str,
    ) -> bool:
        """True if any cell of the building's footprint sits on the
        named feature. Used by the placement check for buildings that
        gate on a natural resource (lumber mill on forest, quarry on
        stone deposit, etc.)."""
        for rr, cc in self._iter_footprint(row, col, building):
            if self.feature_at(rr, cc) == feature:
                return True
        return False

    # ── v0.14: feature depletion ──────────────────────────────────────────
    def _init_feature_reserves(self) -> None:
        """Stamp each feature tile with its starting reserves. Pulled out
        so it runs after `_generate_terrain_features` and so saveload's
        migration path can re-stamp on pre-v11 saves."""
        # Imported here (not at module top) to avoid a circular import:
        # balance.py → constants? — currently fine, but keeping the
        # import local keeps the dependency surface tight.
        from balance import FEATURE_RESERVES_DEFAULT
        for r in range(self.rows):
            for c in range(self.cols):
                feat = self.terrain_features[r][c]
                if feat is None:
                    continue
                default = FEATURE_RESERVES_DEFAULT.get(feat)
                # None → inexhaustible. We *still* store an entry so
                # the saveload round-trip is symmetric and so
                # `tap_feature` doesn't have to special-case the
                # missing-entry case.
                # v0.23: also stash `initial` (the value at discovery
                # time) so the middle-click bubble can render
                # "9000 left of 85000". For inexhaustible features
                # initial stays None — there's no "depletion budget"
                # to compare against.
                self.terrain_feature_state[(r, c)] = {
                    "reserves": default,
                    "initial":  default,
                }

    def feature_reserves(self, row: int, col: int) -> float | None:
        """Return the remaining reserves at this feature tile, or None
        if the tile is inexhaustible (or has no feature)."""
        st = self.terrain_feature_state.get((row, col))
        if st is None:
            return None
        return st.get("reserves")

    def is_feature_depleted(self, row: int, col: int) -> bool:
        """True if this tile carries a feature whose reserves have
        been spent. Inexhaustible tiles never deplete; tiles with no
        feature aren't depleted (they're empty)."""
        st = self.terrain_feature_state.get((row, col))
        if st is None:
            return False
        reserves = st.get("reserves")
        return reserves is not None and reserves <= 0

    def tap_feature_in_footprint(
        self, row: int, col: int, building: Building, amount: float,
        wanted_features: set[str] | None = None,
        include_neighbours: bool = False,
    ) -> float:
        """Drain `amount` units from the feature tile(s) under (or near)
        the building's footprint. Returns the amount actually extracted —
        zero if every applicable tile is depleted, less than `amount`
        if the last live tile runs out mid-tick.

        Strategy: consume from the first non-depleted tile in
        footprint-iteration order. Spreading the drain across all
        tiles would be more "physical" but requires fractional
        accounting; in practice players don't notice which exact
        tile within a 2×2 footprint drained first.

        v0.25: when a tile's reserves hit zero we now CLEAR the
        ``terrain_features`` entry (and its state) so the texture
        disappears and the tile reverts to plain terrain — a depleted
        fertile_soil becomes plain grass, a depleted fish tile becomes
        plain water. Previously we left the visual in place; the new
        behaviour gives the player a clear signal that the resource
        is gone and they can move the extractor.

        v0.25: ``wanted_features`` filters which feature ids we will
        drain. ``None`` keeps the legacy behaviour (drain any feature
        carrying reserves under the footprint). A set restricts the
        drain — farms pass ``{"fertile_soil"}`` so they don't
        accidentally drain a non-bonus feature that happens to share
        the tile; fisheries pass ``{"fish"}``.

        v0.25: ``include_neighbours`` extends the search to the
        building's Chebyshev-1 neighbours. Used by fisheries, which
        sit on land adjacent to water and need to drain ``fish``
        features on the water side.
        """
        if amount <= 0:
            return 0.0
        # Build the candidate tile list: footprint, optionally
        # expanded by one Chebyshev step. We dedupe via a set because
        # the expansion creates overlapping neighbours.
        candidates: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for rr, cc in self._iter_footprint(row, col, building):
            if include_neighbours:
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        nr, nc = rr + dr, cc + dc
                        key = (nr, nc)
                        if key in seen:
                            continue
                        if not (0 <= nr < self.rows and 0 <= nc < self.cols):
                            continue
                        seen.add(key)
                        candidates.append(key)
            else:
                key = (rr, cc)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(key)

        for rr, cc in candidates:
            feat = self.terrain_features[rr][cc]
            if feat is None:
                continue
            if wanted_features is not None and feat not in wanted_features:
                continue
            st = self.terrain_feature_state.get((rr, cc))
            if st is None:
                continue
            reserves = st.get("reserves")
            if reserves is None:
                # Inexhaustible — no debit needed, full amount "extracted".
                return amount
            if reserves <= 0:
                # Already depleted but feature still on the map (rare —
                # the cleanup below should have caught it). Belt-and-
                # braces: clear it now and continue searching for a
                # live tile.
                self._clear_depleted_feature(rr, cc)
                continue
            taken = min(amount, reserves)
            new_reserves = reserves - taken
            st["reserves"] = new_reserves
            if new_reserves <= 0:
                # Texture vanishes; tile reverts to plain terrain.
                self._clear_depleted_feature(rr, cc)
            return taken
        return 0.0

    def _clear_depleted_feature(self, row: int, col: int) -> None:
        """v0.25: remove the feature overlay from a tile whose reserves
        have run out. After this call the tile renders as plain terrain
        (grass for a fertile_soil tile, water for a fish tile) and any
        future extractor placement check that gates on the feature
        will refuse — the resource is truly gone.

        We also drop the ``terrain_feature_state`` entry so a fresh
        feature later painted on the same tile (e.g. via the editor)
        starts with a clean reserves value rather than inheriting the
        depleted state.
        """
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return
        self.terrain_features[row][col] = None
        self.terrain_feature_state.pop((row, col), None)
        # v0.56: a feature sprite disappeared — refresh the cached layers.
        self.invalidate_render_cache()

    def all_footprint_features_depleted(
        self, row: int, col: int, building: Building, feature: str,
    ) -> bool:
        """True if every footprint tile carrying ``feature`` has zero
        reserves. Returns False if the building has at least one live
        feature tile to tap, *or* if no footprint tile carries the
        feature at all (placement check is upstream — this method
        exists only for already-placed extractors)."""
        any_matching = False
        for rr, cc in self._iter_footprint(row, col, building):
            if self.feature_at(rr, cc) != feature:
                continue
            any_matching = True
            if not self.is_feature_depleted(rr, cc):
                return False
        return any_matching

    # ── v0.21: extractable-resource bubble (middle-click) ─────────────────
    def extractable_summary(
        self, row: int, col: int, building: Building,
    ) -> dict | None:
        """Summarise what an extracting building can still draw from
        the land under it. Returns a dict the UI consumes::

            {
              "kind":     "forest" | "iron_vein" | "fertile_soil" | "fish" | …,
              "label":    "Trees"  | "Iron"      | "Fertility"     | "Fish",
              "reserves": 540.0   |  None  (None == inexhaustible)
              "initial":  800.0   |  None  (discovery-time reserves; v0.23)
              "depleted": False,
              "tiles":    3,        # how many footprint tiles contributed
            }

        Returns ``None`` if the building isn't an extractor — the
        middle-click handler then falls back to a generic "not an
        extractor" notification.

        Strategy:
          * ``needs_feature`` extractors (mines, lumber mills,
            quarries): sum reserves across footprint tiles that
            match *any* required feature. If every matching tile is
            inexhaustible we report ``reserves=None``.
          * Farms / orchards / vegetable farms / spice fields with
            a ``feature_yield_bonus`` map (typically
            ``fertile_soil``): same shape, but the feature is
            optional rather than gating, and fertile_soil is
            inexhaustible by design — so the bubble shows
            "Inexhaustible" with the count of bonus tiles.
          * ``needs_terrain='water'`` extractors (fishery): we have
            no per-water-tile fish stock model yet, so we report
            adjacent-water-tile count and ``reserves=None``
            (inexhaustible). Future work could plug a depletion
            model in here without touching the call site.
        """
        # 1) Feature-based gating extractors (forest, ore veins, etc.).
        if building.needs_feature:
            wanted = set(building.needs_feature)
            return self._summarise_feature_tiles(row, col, building, wanted)
        # 2) Yield-bonus extractors (farms etc.). The feature isn't
        # required for placement but the player benefits from
        # standing on it — and the question "how much fertility do
        # I have under this farm" is meaningful even when the
        # answer is "inexhaustible (3 tiles)".
        bonus_map = getattr(building, "feature_yield_bonus", None) or {}
        if bonus_map:
            wanted = set(bonus_map.keys())
            summary = self._summarise_feature_tiles(
                row, col, building, wanted,
            )
            if summary is not None:
                return summary
            # No bonus tiles under this footprint — return a "no
            # bonus" reading rather than None. The player middle-
            # clicked a farm; they want feedback.
            kind = next(iter(wanted)) if wanted else "fertile_soil"
            return {
                "kind":     kind,
                "label":    _EXTRACTABLE_LABELS.get(
                    kind, kind.replace("_", " ").title(),
                ),
                "reserves": 0.0,
                "initial":  None,
                "depleted": False,
                "tiles":    0,
            }
        # 3) Fisheries (needs_terrain='water'). v0.25: now uses the
        # `fish` feature on water tiles. We count fish tiles in the
        # footprint plus Chebyshev-1 neighbours (the fishery sits on
        # land touching water, so the fish are on the neighbour side)
        # and sum their reserves. A fishery placed adjacent to plain
        # water (no fish feature) reports zero reserves — the player
        # gets a clear "no fish here" signal.
        if building.needs_terrain == "water":
            return self._summarise_fish_tiles(row, col, building)
        return None

    def _summarise_fish_tiles(
        self, row: int, col: int, building: Building,
    ) -> dict:
        """v0.25: sum fish-feature reserves in the fishery's catchment.

        Catchment is the footprint plus Chebyshev-1 neighbours, same
        rule used by `tap_feature_in_footprint(include_neighbours=True)`.
        """
        fish_tiles = 0
        total_reserves = 0.0
        total_initial = 0.0
        any_inexhaustible = False
        seen: set[tuple[int, int]] = set()
        for rr, cc in self._iter_footprint(row, col, building):
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nr, nc = rr + dr, cc + dc
                    key = (nr, nc)
                    if key in seen:
                        continue
                    if not (0 <= nr < self.rows and 0 <= nc < self.cols):
                        continue
                    seen.add(key)
                    if self.terrain_features[nr][nc] != "fish":
                        continue
                    fish_tiles += 1
                    st = self.terrain_feature_state.get(key, {})
                    rsv = st.get("reserves")
                    init = st.get("initial")
                    if rsv is None:
                        any_inexhaustible = True
                    else:
                        total_reserves += float(rsv)
                    if init is None:
                        any_inexhaustible = True
                    else:
                        total_initial += float(init)
        if fish_tiles == 0:
            return {
                "kind":     "fish",
                "label":    "Fish",
                "reserves": 0.0,
                "initial":  None,
                "depleted": True,
                "tiles":    0,
            }
        return {
            "kind":     "fish",
            "label":    "Fish",
            "reserves": None if any_inexhaustible else total_reserves,
            "initial":  None if any_inexhaustible else total_initial,
            "depleted": (
                False if any_inexhaustible
                else total_reserves <= 0
            ),
            "tiles":    fish_tiles,
        }

    def _summarise_feature_tiles(
        self, row: int, col: int, building: Building, wanted: set[str],
    ) -> dict | None:
        """Helper for extractable_summary — sum reserves across the
        footprint for any feature tile in ``wanted``. Returns the
        summary dict or None if no footprint tile matches any
        ``wanted`` feature."""
        total: float = 0.0
        total_initial: float = 0.0
        tiles = 0
        any_inexhaustible = False
        depleted_tiles = 0
        kind: str | None = None
        for rr, cc in self._iter_footprint(row, col, building):
            feat = self.feature_at(rr, cc)
            if feat is None or feat not in wanted:
                continue
            tiles += 1
            kind = feat
            st = self.terrain_feature_state.get((rr, cc))
            reserves = st.get("reserves") if st else None
            initial = st.get("initial") if st else None
            if reserves is None:
                any_inexhaustible = True
                continue
            if reserves <= 0:
                depleted_tiles += 1
            total += max(0.0, reserves)
            # v0.23: sum the discovery-time reserves so the bubble
            # can quote "X / Y". A footprint that straddles a fresh
            # forest tile and a half-tapped one shows the combined
            # discovery total — exactly what the player wants when
            # asking "how much of this footprint's lifetime is
            # left".
            if initial is not None:
                total_initial += max(0.0, float(initial))
            else:
                # Legacy save with no initial recorded for an
                # exhaustible tile — fall back to the current value
                # so we don't undercount.
                total_initial += max(0.0, reserves)
        if tiles == 0 or kind is None:
            return None
        depleted = (
            not any_inexhaustible
            and depleted_tiles == tiles
        )
        return {
            "kind":     kind,
            "label":    _EXTRACTABLE_LABELS.get(
                kind, kind.replace("_", " ").title(),
            ),
            "reserves": None if any_inexhaustible else total,
            # v0.23: discovery-time reserves, mirroring `reserves`. None
            # when at least one matching tile is inexhaustible — there's
            # no meaningful "discovery max" for an inexhaustible source.
            "initial":  None if any_inexhaustible else total_initial,
            "depleted": depleted,
            "tiles":    tiles,
        }

    def raw_feature_summary(self, row: int, col: int) -> dict | None:
        """v0.23.x: summarise the natural feature at a single tile, with
        no building required. Used by the middle-click handler so the
        player can scout the map (e.g. inspect an iron vein) BEFORE
        committing to a mine. The returned dict has the same shape as
        :py:meth:`extractable_summary` so the bubble renderer doesn't
        need a separate code path.

        Returns ``None`` when the clicked tile carries no inspectable
        feature — plain grass, hills, mountains, etc. Water tiles
        return a fish summary mirroring the fishery bubble so the
        player can see "this shore has 4 fishable cells" before
        building the fishery.
        """
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return None
        feat = self.feature_at(row, col)
        if feat is not None:
            st = self.terrain_feature_state.get((row, col))
            reserves = st.get("reserves") if st else None
            initial = st.get("initial") if st else None
            depleted = (
                reserves is not None and reserves <= 0
            )
            return {
                "kind":     feat,
                "label":    _EXTRACTABLE_LABELS.get(
                    feat, feat.replace("_", " ").title(),
                ),
                "reserves": reserves,
                "initial":  initial,
                "depleted": depleted,
                "tiles":    1,
            }
        # Water tile? v0.25: fish are now a proper feature, so plain
        # water (no `fish` feature on the clicked tile) means no fish
        # here. We still report the fishable-tile catchment so the
        # player can scout: count the clicked tile + neighbours and
        # see how many of THEM carry the `fish` feature.
        if self.is_water(row, col):
            fish_tiles = 0
            total_reserves = 0.0
            total_initial = 0.0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nr, nc = row + dr, col + dc
                    if not (
                        0 <= nr < self.rows
                        and 0 <= nc < self.cols
                    ):
                        continue
                    if self.terrain_features[nr][nc] != "fish":
                        continue
                    fish_tiles += 1
                    st = self.terrain_feature_state.get((nr, nc), {})
                    rsv = st.get("reserves")
                    init = st.get("initial")
                    if rsv is not None:
                        total_reserves += float(rsv)
                    if init is not None:
                        total_initial += float(init)
            if fish_tiles == 0:
                # Plain water with no fish in range — nothing to extract.
                return {
                    "kind":     "fish",
                    "label":    "Fish",
                    "reserves": 0.0,
                    "initial":  None,
                    "depleted": True,
                    "tiles":    0,
                }
            return {
                "kind":     "fish",
                "label":    "Fish",
                "reserves": total_reserves,
                "initial":  total_initial if total_initial > 0 else None,
                "depleted": total_reserves <= 0,
                "tiles":    fish_tiles,
            }
        return None

    def has_water_neighbour(
        self, row: int, col: int, building: Building,
    ) -> bool:
        """True if any cell of the building's footprint is adjacent
        (Chebyshev distance 1) to a water tile. Reservoir uses this —
        it must touch a lake or river to fill, even though the
        reservoir itself doesn't sit on water (that's the port's
        ``needs_terrain='water'`` rule)."""
        for rr, cc in self._iter_footprint(row, col, building):
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = rr + dr, cc + dc
                    if (
                        0 <= nr < self.rows
                        and 0 <= nc < self.cols
                        and self.is_water(nr, nc)
                    ):
                        return True
        return False

    # ── Coordinate helpers ────────────────────────────────────────────────
    @staticmethod
    def grid_to_world(row: int, col: int) -> tuple[float, float]:
        """`(row, col)` → world `(x, y)` of tile bottom-left corner."""
        return col * TILE_SIZE, row * TILE_SIZE

    @staticmethod
    def grid_to_world_center(row: int, col: int) -> tuple[float, float]:
        """`(row, col)` → world `(x, y)` of tile center."""
        return col * TILE_SIZE + TILE_SIZE / 2, row * TILE_SIZE + TILE_SIZE / 2

    @staticmethod
    def world_to_grid(wx: float, wy: float) -> tuple[int | None, int | None]:
        """World `(x, y)` → `(row, col)` or `(None, None)` if outside grid.

        Uses floor division so negative coordinates land outside the grid
        instead of being truncated toward zero (which would put -10 → 0).

        Bounds use module-level GRID_ROWS / GRID_COLS so this remains a
        @staticmethod. For per-instance dimensions (v0.26 editor map
        resize), call ``self.world_to_grid_instance`` which checks
        against ``self.rows / self.cols``.
        """
        import math
        c = math.floor(wx / TILE_SIZE)
        r = math.floor(wy / TILE_SIZE)
        if 0 <= r < GRID_ROWS and 0 <= c < GRID_COLS:
            return r, c
        return None, None

    def world_to_grid_instance(self, wx: float, wy: float) -> tuple[int | None, int | None]:
        """Per-instance variant of world_to_grid that bounds-checks
        against ``self.rows / self.cols`` (the actual map dimensions)
        rather than the module-level GRID_ROWS / GRID_COLS. Use this
        anywhere the map may have been resized (editor-loaded
        scenarios with custom width/height)."""
        import math
        c = math.floor(wx / TILE_SIZE)
        r = math.floor(wy / TILE_SIZE)
        if 0 <= r < self.rows and 0 <= c < self.cols:
            return r, c
        return None, None

    # ── Placement ─────────────────────────────────────────────────────────
    def can_place(self, row: int, col: int, building_id: str) -> bool:
        bd = self.registry.get(building_id)
        if bd is None:
            return False
        # v0.27: placement brushes are never placed on the grid. They
        # are click-time stamps that call place_building(stamp_id, …)
        # per tile. A naive caller asking can_place("road_h5", …)
        # gets False; the click handler in game_window resolves the
        # brush first and asks can_place(brush.stamp_id, …) on each
        # tile of the row/column.
        if bd.placement_brush:
            return False
        for rr, cc in self._iter_footprint(row, col, bd):
            if not (0 <= rr < self.rows and 0 <= cc < self.cols):
                return False
            if self.grid[rr][cc] is not None:
                return False
            # v0.15: hills and mountains block placement for every
            # building. Ports / reservoirs that need water still get
            # rejected here — you can't sit a port on a mountain even
            # if it touches water somewhere else in the footprint.
            # v0.18: desert is treated identically to hills/mountains
            # for placement — unbuildable for every building type.
            if (
                self.is_hills(rr, cc)
                or self.is_mountains(rr, cc)
                or self.is_desert(rr, cc)
            ):
                return False
            water = self.is_water(rr, cc)
            if bd.needs_terrain == "water":
                # Ports must touch water somewhere in their footprint, but
                # they don't need *every* cell to be water. We require all
                # cells be either grass or water (no obstruction), and we
                # check the touch separately below.
                continue
            # v0.27: bridges are the opposite of needs_terrain="water":
            # every tile of the footprint MUST be water. They convert
            # impassable water into walker-passable, road-network-
            # connected tiles. A bridge over half water + half grass
            # would defeat the point — that's just a road sitting on
            # water, which we already disallow. So we enforce the
            # full-water footprint here and refuse otherwise.
            if bd.bridges_water:
                if not water:
                    return False
                continue
            if water:
                return False
        # Water-requiring buildings need at least one water cell in footprint.
        if bd.needs_terrain == "water":
            if not any(self.is_water(rr, cc)
                       for rr, cc in self._iter_footprint(row, col, bd)
                       if 0 <= rr < self.rows and 0 <= cc < self.cols):
                return False

        # v0.13: terrain-feature gate. Lumber mills must sit on a
        # forest tile, quarries on stone, mines on gold/copper veins.
        # The check is "at least one cell in the footprint has any of
        # the listed features" — so a 2×2 building needs only one
        # qualifying tile in its corner, not all four. Empty
        # needs_feature list means "no feature required" (the default
        # for buildings that don't depend on natural resources).
        if bd.needs_feature:
            if not any(
                self.has_feature_in_footprint(row, col, bd, feat)
                for feat in bd.needs_feature
            ):
                return False

        # v0.13: water-adjacency gate (reservoir). Must touch a water
        # tile (Chebyshev=1) somewhere in the footprint's neighbourhood.
        # Distinct from needs_terrain='water' (port — sits IN water).
        if bd.needs_water_adjacent:
            if not self.has_water_neighbour(row, col, bd):
                return False

        return True

    def can_afford_materials(
        self, building_id: str, resources: dict[str, float],
    ) -> bool:
        """v0.13: True if the global resource pool has enough planks /
        stone_blocks / etc. to construct this building. Only consults
        the building's ``material_cost`` field; treasury is checked
        separately by the caller via ``economy.can_afford(bd.cost)``.

        ``resources`` is the live dict from ``EconomyManager.resources``;
        we read but don't mutate.
        """
        bd = self.registry.get(building_id)
        if bd is None:
            return False
        for good, qty in bd.material_cost.items():
            if resources.get(good, 0) < qty:
                return False
        return True

    def place_building(
        self,
        row: int,
        col: int,
        building_id: str,
        economy=None,
        bypass_validation: bool = False,
        bypass_construction: bool = False,
    ) -> bool:
        """Place a building. v0.13: when ``economy`` is supplied, the
        building's ``material_cost`` is validated against and deducted
        from ``economy.resources``. Refuses (returns False) on
        insufficient materials. Treasury cost (``bd.cost`` in dn) is
        the caller's responsibility — this method only handles physical
        building inputs.

        Tests and starter-city seeding pass ``economy=None`` to skip
        the material check; the loaded scenario or a freshly-loaded
        save would also bypass this since the buildings already exist.

        ``bypass_validation`` = True skips can_place entirely. Used by
        saveload's from_dict — the persisted buildings were valid at
        save time, and re-validating would reject buildings whose
        feature was edited away (e.g. a forest tile turned into grass
        by the editor) when the right behaviour is to keep them
        placed and let the player demolish if they want.

        ``bypass_construction`` = True skips the v0.25 construction-
        progress entry: the building goes live immediately. Used by
        tests that place a service provider and then assert the
        service appears (otherwise they'd have to spin
        ``construction_ticks`` rounds of ``advance_construction``
        first), and by saveload's from_dict for the same reason as
        ``bypass_validation``. Game flow (``on_update`` mouse-click
        placement) always passes False so the player sees the new
        v0.26 construction bar.
        """
        if not bypass_validation:
            if not self.can_place(row, col, building_id):
                return False
        else:
            # Even with bypass, we still need basic safety: bounds and
            # non-overlap. Otherwise a corrupt save could place two
            # buildings on the same cell.
            bd_check = self.registry.get(building_id)
            if bd_check is None:
                return False
            for rr, cc in self._iter_footprint(row, col, bd_check):
                if not (0 <= rr < self.rows and 0 <= cc < self.cols):
                    return False
                if self.grid[rr][cc] is not None:
                    return False
        bd = self.registry[building_id]
        # Material cost gate: only enforced when an economy ledger is
        # supplied. The default (None) preserves the v0.12 behaviour
        # for tests that don't construct an economy.
        if economy is not None and bd.material_cost:
            if not self.can_afford_materials(building_id, economy.resources):
                return False
            for good, qty in bd.material_cost.items():
                economy.resources[good] = max(
                    0.0, economy.resources.get(good, 0) - qty
                )
        cell: BuildingCell = (building_id, row, col)
        for rr, cc in self._iter_footprint(row, col, bd):
            self.grid[rr][cc] = cell
        self._invalidate_label(row, col)
        # Initial per-building state. Houses start at tier 0 (shack).
        if bd.is_evolvable:
            self.building_state[(row, col)] = {"tier": 0, "streak": 0}
        # v0.25: register construction. Buildings with a non-zero
        # `construction_ticks` start at progress=0 and tick up via
        # `advance_construction` each game tick. Until they finish,
        # the simulation skips them when iterating positioned
        # buildings (see `is_under_construction`). Roads and other
        # buildings with construction_ticks=0 skip this — they go
        # live immediately, the same as every building did pre-v0.25.
        #
        # v0.26: `bypass_construction` is honoured here too — both
        # saveload (re-placing already-finished buildings) and unit
        # tests (asserting immediate service coverage) need the
        # building to go live without spinning ticks.
        if (
            bd.construction_ticks > 0
            and not bypass_validation
            and not bypass_construction
        ):
            self.construction_progress[(row, col)] = 0
        signals.emit("building_placed", building_id, row, col)
        return True

    def remove_building(self, row: int, col: int) -> str | None:
        cell = self.grid[row][col]
        if cell is None:
            return None
        building_id, orow, ocol = cell
        bd = self.registry[building_id]
        for rr, cc in self._iter_footprint(orow, ocol, bd):
            self.grid[rr][cc] = None
        self._invalidate_label(orow, ocol)
        self.building_state.pop((orow, ocol), None)
        # v0.25: clean up any in-flight construction.
        self.construction_progress.pop((orow, ocol), None)
        signals.emit("building_removed", building_id, orow, ocol)
        return building_id

    # ── v0.25: construction progress ──────────────────────────────────────
    def is_under_construction(self, row: int, col: int) -> bool:
        """True if the building anchored at (row, col) hasn't finished
        construction yet. False for ready buildings AND for cells
        that don't host a building."""
        return (row, col) in self.construction_progress

    def construction_fraction(self, row: int, col: int) -> float:
        """Progress fraction in [0.0, 1.0]. Returns 1.0 for buildings
        that are already operational (no entry in the dict). Used by
        the renderer to draw the progress bar."""
        progress = self.construction_progress.get((row, col))
        if progress is None:
            return 1.0
        cell = self.grid[row][col]
        if cell is None:
            return 1.0
        bd = self.registry.get(cell[0])
        if bd is None or bd.construction_ticks <= 0:
            return 1.0
        return max(0.0, min(1.0, progress / bd.construction_ticks))

    def advance_construction(self) -> list[tuple[str, int, int]]:
        """Bump every in-progress building's tick counter by 1.
        Returns the list of `(building_id, row, col)` tuples that
        finished construction this tick — so the caller can fire a
        notification / signal per completion. The renderer doesn't
        need this return value; it polls `is_under_construction`
        directly.
        """
        completed: list[tuple[str, int, int]] = []
        # Snapshot keys because we mutate the dict during iteration.
        for (r, c) in list(self.construction_progress.keys()):
            cell = self.grid[r][c]
            if cell is None:
                # The building was removed mid-construction — clean
                # up the orphaned progress entry.
                self.construction_progress.pop((r, c), None)
                continue
            bd = self.registry.get(cell[0])
            if bd is None or bd.construction_ticks <= 0:
                # Building's definition no longer requires construction
                # (modder edited the JSON live) — release it immediately.
                self.construction_progress.pop((r, c), None)
                completed.append((cell[0], r, c))
                continue
            self.construction_progress[(r, c)] += 1
            if self.construction_progress[(r, c)] >= bd.construction_ticks:
                self.construction_progress.pop((r, c), None)
                completed.append((cell[0], r, c))
                # Fire the same signal as a fresh placement so the
                # road network / service map rebuild and treat the
                # newly-finished building as a first-class active
                # building. We use a dedicated signal name so listeners
                # that DO want to distinguish "newly built" from
                # "completed construction" can — but most listeners
                # treat them identically.
                signals.emit(
                    "construction_completed", cell[0], r, c,
                )
        return completed

    @staticmethod
    def _iter_footprint(row: int, col: int, bd: Building):
        """Yield `(row, col)` for every cell the building occupies."""
        for dr in range(bd.height):
            for dc in range(bd.width):
                yield row + dr, col + dc

    def _invalidate_label(self, row: int, col: int) -> None:
        self._label_cache.pop((row, col), None)

    # ── Queries ───────────────────────────────────────────────────────────
    def get_all_buildings(self) -> list[str]:
        """Return every placed building id (one entry per building, not per tile)."""
        return [bt for bt, _, _ in self.get_building_positions()]

    def get_building_at(self, row: int, col: int) -> tuple[str, int, int] | None:
        """Return (id, origin_row, origin_col) for the building covering (row, col)."""
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return None
        return self.grid[row][col]

    def get_building_positions(self) -> list[BuildingCell]:
        """Return one `(building_id, origin_row, origin_col)` per building."""
        out: list[BuildingCell] = []
        seen: set[tuple[int, int]] = set()
        for r in range(self.rows):
            for c in range(self.cols):
                cell = self.grid[r][c]
                if cell is None:
                    continue
                key = (cell[1], cell[2])
                if key in seen:
                    continue
                seen.add(key)
                out.append(cell)
        return out

    # ── Serialisation ─────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        # v0.13: terrain_features is encoded as a list of (row, col, feature)
        # tuples rather than a 30×40 dense matrix — most cells are None,
        # so the sparse format saves ~95% of the bytes.
        features = [
            [r, c, self.terrain_features[r][c]]
            for r in range(self.rows)
            for c in range(self.cols)
            if self.terrain_features[r][c] is not None
        ]
        # v0.14: terrain_feature_state — currently just a `reserves`
        # value per feature tile, but the dict shape leaves room for
        # future per-tile attributes (drought-affected, polluted, etc.)
        # without another saveload bump. Inexhaustible tiles serialise
        # as `null` reserves so the round-trip is symmetric.
        # v0.23: each entry is now a 4-tuple [r, c, reserves, initial]
        # so the bubble can render "X left of Y at discovery". Loaders
        # accept the legacy 3-tuple for back-compat (initial fills in
        # from the current reserves — the worst case is a save mid-
        # depletion shows "100/100" and ticks down from there).
        feature_state = [
            [r, c, st.get("reserves"), st.get("initial", st.get("reserves"))]
            for (r, c), st in sorted(self.terrain_feature_state.items())
        ]
        # v0.15: terrain (water/hills/mountains) is also persisted now.
        # Same sparse format — grass/grass_alt is the assumed default
        # and isn't serialised. Without this, a saved game on a custom
        # map would reload with the default river+lake terrain
        # underneath the saved buildings, which was a latent bug from
        # v0.13 onwards (no one had noticed because terrain was
        # procedurally identical for every game). Added now because
        # hills/mountains *aren't* the default and we want loaded
        # games to look the same as when they were saved.
        terrain_overrides = [
            [r, c, self.terrain[r][c]]
            for r in range(self.rows)
            for c in range(self.cols)
            if self.terrain[r][c] not in (TERRAIN_GRASS, TERRAIN_GRASS_ALT)
        ]
        return {
            "buildings": [
                {
                    "type": bt, "row": orow, "col": ocol,
                    "state": self.building_state.get((orow, ocol), {}),
                }
                for bt, orow, ocol in self.get_building_positions()
            ],
            "terrain_features": features,
            "terrain_feature_state": feature_state,
            "terrain_overrides": terrain_overrides,
            # v0.25: in-flight construction progress. Empty dict when
            # nothing is mid-build. Encoded as a list of triples so the
            # JSON is symmetric with terrain_feature_state.
            "construction_progress": [
                [r, c, ticks]
                for (r, c), ticks in sorted(self.construction_progress.items())
            ],
        }

    def from_dict(self, d: dict) -> None:
        self.grid = [[None for _ in range(self.cols)] for _ in range(self.rows)]
        self._label_cache.clear()
        self.building_state.clear()
        # v0.15: terrain overrides. Pre-v0.15 saves don't have this
        # key — they keep the procedural default (which was correct
        # for the v0.13/v0.14 maps that lacked hills/mountains).
        # When the key IS present, regenerate the base grass pattern
        # then layer the overrides on top — this way the river/lake
        # are also driven by the saved data, not by re-running the
        # procedural generator (which would hide whatever the editor
        # had painted).
        if "terrain_overrides" in d:
            for r in range(self.rows):
                for c in range(self.cols):
                    self.terrain[r][c] = (
                        TERRAIN_GRASS if (r + c) % 2 == 0 else TERRAIN_GRASS_ALT
                    )
            for r, c, t in d["terrain_overrides"]:
                if 0 <= r < self.rows and 0 <= c < self.cols:
                    self.terrain[r][c] = int(t)
        # v0.13: replace the procedural feature layer with whatever was
        # saved. Old saves (no terrain_features key) keep the procedural
        # default — they were generated under that layout originally so
        # the buildings still sit on the right tiles.
        if "terrain_features" in d:
            self.terrain_features = [
                [None for _ in range(self.cols)] for _ in range(self.rows)
            ]
            for r, c, feature in d["terrain_features"]:
                if 0 <= r < self.rows and 0 <= c < self.cols:
                    self.terrain_features[r][c] = feature
        # v0.14: load per-tile feature state (reserves). Pre-v0.14 saves
        # don't have this key — re-stamp with the default reserves so
        # depletion is enabled going forward; the player loses no
        # gameplay history because old saves never depleted anything
        # in the first place.
        self.terrain_feature_state = {}
        if "terrain_feature_state" in d:
            for entry in d["terrain_feature_state"]:
                # v0.23: entries grew from [r, c, reserves] to
                # [r, c, reserves, initial]. Accept both shapes —
                # legacy entries fill in initial from the current
                # reserves so the discovery readout has *some* number
                # to compare against (worst case: a save mid-depletion
                # reads as "fresh" until the next tap).
                if len(entry) >= 4:
                    r, c, reserves, initial = entry[0], entry[1], entry[2], entry[3]
                else:
                    r, c, reserves = entry[0], entry[1], entry[2]
                    initial = reserves
                if 0 <= r < self.rows and 0 <= c < self.cols:
                    self.terrain_feature_state[(r, c)] = {
                        "reserves": reserves,
                        "initial":  initial,
                    }
        else:
            self._init_feature_reserves()
        for b in d.get("buildings", []):
            # v0.13: persisted buildings were valid at save time; bypass
            # re-validation so post-load terrain edits or balance changes
            # don't suddenly evict a saved building.
            self.place_building(
                b["row"], b["col"], b["type"], bypass_validation=True,
            )
            state = b.get("state")
            if state:
                self.building_state[(b["row"], b["col"])] = dict(state)
        # v0.25: restore in-flight construction progress. Pre-v0.25
        # saves have no key — every persisted building was instant-
        # built, so it's safe to leave construction_progress empty
        # (every loaded building is operational).
        self.construction_progress = {}
        for entry in d.get("construction_progress", []):
            r, c, ticks = int(entry[0]), int(entry[1]), int(entry[2])
            if 0 <= r < self.rows and 0 <= c < self.cols:
                self.construction_progress[(r, c)] = ticks
        # v0.56: terrain and features were just replaced wholesale — the
        # cached render layers must rebuild on the next draw.
        self.invalidate_render_cache()

    # ── Drawing ───────────────────────────────────────────────────────────
    def draw(
        self,
        hover_row: int | None = None,
        hover_col: int | None = None,
        selected_building: str | None = None,
        visible_bounds: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Render the map.

        ``visible_bounds`` is an optional ``(min_row, max_row, min_col,
        max_col)`` inclusive tile window. It culls the colour/glyph
        fallback pass to on-screen tiles only; the SpriteList path is
        already GPU-batched and needs no manual culling. ``None`` draws
        every tile (the behaviour the test suite and minimap rely on).
        """
        self._draw_terrain(visible_bounds)
        self._draw_grid_lines()
        self._draw_buildings()
        self._draw_hover(hover_row, hover_col, selected_building)

    # ── v0.56: cached render layers ───────────────────────────────────────
    def invalidate_render_cache(self) -> None:
        """Mark the cached terrain/feature/grid layers stale.

        Called whenever terrain or features change — feature depletion,
        editor painting, or a fresh map load. The next ``draw`` rebuilds
        the SpriteLists lazily. Cheap to call repeatedly; only the first
        call after a draw does any work.
        """
        self._render_cache_dirty = True

    def _build_render_cache(self) -> None:
        """(Re)build the terrain + feature SpriteLists, the grid-line
        shape list, and the colour/glyph fallback tile lists.

        Tiles whose texture is present become batched sprites; tiles
        without a texture are recorded for the immediate-mode fallback
        so textureless environments (tests, partial asset sets) render
        exactly as before. Building this is O(rows×cols) but happens
        only on change, not every frame.
        """
        # arcade may be a lightweight stub in headless tests; guard the
        # whole sprite path so a stub without SpriteList still works via
        # the fallback lists (which need no GPU objects until drawn).
        make_sprites = hasattr(arcade, "SpriteList") and hasattr(arcade, "Sprite")
        terrain_sl = arcade.SpriteList() if make_sprites else None
        feature_sl = arcade.SpriteList() if make_sprites else None
        fb_terrain: list[tuple[int, int, int]] = []
        fb_features: list[tuple[int, int, str]] = []

        half = TILE_SIZE / 2
        for r in range(self.rows):
            for c in range(self.cols):
                t = self.terrain[r][c]
                cx = c * TILE_SIZE + half
                cy = r * TILE_SIZE + half
                tex = self.textures.terrain(_TERRAIN_TEX_NAMES.get(t, "grass"))
                if tex is not None and terrain_sl is not None:
                    spr = arcade.Sprite(tex, center_x=cx, center_y=cy)
                    spr.width = TILE_SIZE
                    spr.height = TILE_SIZE
                    terrain_sl.append(spr)
                else:
                    fb_terrain.append((r, c, t))

                feat = self.terrain_features[r][c]
                if feat is not None:
                    feat_tex = self.textures.feature(feat)
                    if feat_tex is not None and feature_sl is not None:
                        spr = arcade.Sprite(feat_tex, center_x=cx, center_y=cy)
                        spr.width = TILE_SIZE
                        spr.height = TILE_SIZE
                        feature_sl.append(spr)
                    else:
                        fb_features.append((r, c, feat))

        self._terrain_sprites = terrain_sl
        self._feature_sprites = feature_sl
        self._fallback_terrain = fb_terrain
        self._fallback_features = fb_features
        self._build_grid_shapes()
        self._render_cache_dirty = False

    def _build_grid_shapes(self) -> None:
        """Bake the grid lines into a single batched ShapeElementList.

        Grid geometry only changes when the map is resized, so it lives
        in the render cache and is rebuilt alongside the tile sprites.
        Falls back to ``None`` (immediate-mode lines at draw time) if the
        arcade build in use lacks the shape API.
        """
        create_lines = getattr(arcade.shape_list, "create_lines", None) \
            if hasattr(arcade, "shape_list") else None
        if create_lines is None or not hasattr(arcade, "shape_list"):
            self._grid_shapes = None
            return
        total_w = self.cols * TILE_SIZE
        total_h = self.rows * TILE_SIZE
        points: list[tuple[float, float]] = []
        for r in range(self.rows + 1):
            yy = r * TILE_SIZE
            points.append((0, yy))
            points.append((total_w, yy))
        for c in range(self.cols + 1):
            xx = c * TILE_SIZE
            points.append((xx, 0))
            points.append((xx, total_h))
        try:
            sl = arcade.shape_list.ShapeElementList()
            sl.append(create_lines(points, COLOR_GRID_LINE, line_width=1))
            self._grid_shapes = sl
        except Exception:
            self._grid_shapes = None

    def _draw_terrain(
        self, visible_bounds: tuple[int, int, int, int] | None = None,
    ) -> None:
        if self._render_cache_dirty:
            self._build_render_cache()

        # Batched textured layers — one GPU draw call each.
        if self._terrain_sprites is not None:
            self._terrain_sprites.draw()
        if self._feature_sprites is not None:
            self._feature_sprites.draw()

        # Immediate-mode fallback for textureless tiles, culled to the
        # viewport when bounds are supplied. A fully-textured map has
        # empty fallback lists and pays nothing here.
        if self._fallback_terrain:
            self._draw_fallback_terrain(visible_bounds)
        if self._fallback_features:
            self._draw_fallback_features(visible_bounds)

    @staticmethod
    def _in_bounds(
        r: int, c: int, b: tuple[int, int, int, int] | None,
    ) -> bool:
        if b is None:
            return True
        return b[0] <= r <= b[1] and b[2] <= c <= b[3]

    def _draw_fallback_terrain(
        self, b: tuple[int, int, int, int] | None,
    ) -> None:
        half = TILE_SIZE / 2
        for r, c, t in self._fallback_terrain:
            if not self._in_bounds(r, c, b):
                continue
            x, y = c * TILE_SIZE, r * TILE_SIZE
            # v0.15: hills/mountains added to the colour fallback so all
            # five terrains read distinctly even without sprites.
            if t == TERRAIN_WATER:
                color = COLOR_WATER
            elif t == TERRAIN_GRASS_ALT:
                color = COLOR_GRASS_ALT
            elif t == TERRAIN_HILLS:
                color = COLOR_HILLS
            elif t == TERRAIN_MOUNTAINS:
                color = COLOR_MOUNTAINS
            elif t == TERRAIN_DESERT:
                color = COLOR_DESERT
            else:
                color = COLOR_GRASS
            arcade.draw_lrbt_rectangle_filled(
                x, x + TILE_SIZE, y, y + TILE_SIZE, color
            )
            if t == TERRAIN_MOUNTAINS:
                cx, cy = x + half, y + half
                # Two overlapping dark triangles → "two peaks".
                arcade.draw_triangle_filled(
                    cx - 8, cy - 8, cx + 4, cy - 8, cx - 2, cy + 8,
                    (60, 50, 45),
                )
                arcade.draw_triangle_filled(
                    cx - 2, cy - 8, cx + 10, cy - 8, cx + 4, cy + 6,
                    (75, 65, 60),
                )
            elif t == TERRAIN_HILLS:
                cx, cy = x + half, y + half
                # Two small bumps — a low mound profile.
                arcade.draw_arc_filled(cx - 4, cy - 2, 8, 6, (70, 80, 55), 0, 180)
                arcade.draw_arc_filled(cx + 4, cy - 2, 8, 6, (70, 80, 55), 0, 180)

    def _draw_fallback_features(
        self, b: tuple[int, int, int, int] | None,
    ) -> None:
        half = TILE_SIZE / 2
        for r, c, feat in self._fallback_features:
            if not self._in_bounds(r, c, b):
                continue
            fc = FEATURE_COLORS.get(feat)
            if fc is None:
                continue
            cx = c * TILE_SIZE + half
            cy = r * TILE_SIZE + half
            if feat == FEATURE_FOREST:
                # Triangle pointing up — tree silhouette.
                arcade.draw_triangle_filled(
                    cx, cy + 8, cx - 6, cy - 4, cx + 6, cy - 4, fc,
                )
            elif feat == FEATURE_FERTILE_SOIL:
                # Three small dots in a triangle — "rich soil".
                for dx, dy in ((-4, -3), (4, -3), (0, 4)):
                    arcade.draw_circle_filled(cx + dx, cy + dy, 1.5, fc)
            else:
                # Generic round marker for ore / stone / water.
                arcade.draw_circle_filled(cx, cy, 4, fc)

    def _draw_grid_lines(self) -> None:
        if self._render_cache_dirty:
            self._build_render_cache()
        if self._grid_shapes is not None:
            self._grid_shapes.draw()
            return
        # Fallback: immediate-mode lines (arcade build without shape_list,
        # or a headless stub). Same look as the pre-v0.56 renderer.
        total_w = self.cols * TILE_SIZE
        total_h = self.rows * TILE_SIZE
        for r in range(self.rows + 1):
            yy = r * TILE_SIZE
            arcade.draw_line(0, yy, total_w, yy, COLOR_GRID_LINE, 1)
        for c in range(self.cols + 1):
            xx = c * TILE_SIZE
            arcade.draw_line(xx, 0, xx, total_h, COLOR_GRID_LINE, 1)

    def _draw_buildings(self) -> None:
        for bt, orow, ocol in self.get_building_positions():
            bd = self.registry[bt]
            x, y = self.grid_to_world(orow, ocol)
            w, h = bd.width * TILE_SIZE, bd.height * TILE_SIZE
            # v0.25: under-construction state. We render the building
            # art normally (so the player can see what they placed)
            # but darken it and overlay a progress bar; finished
            # buildings skip both.
            under_construction = self.is_under_construction(orow, ocol)

            # Resolve which texture (if any) to use for this building.
            tex: arcade.Texture | None = None
            tier: int | None = None
            if bd.is_evolvable:
                state = self.building_state.get((orow, ocol))
                if state is not None:
                    tier = state.get("tier", 0)
                    tex = self.textures.house_tier(tier)
            if tex is None:
                # v0.17: try the id first (e.g. "vegetable_farm"); if
                # the manifest doesn't list it, the registry's fallback
                # also tries `<id>.<ext>` directly. We additionally
                # try the human-readable name (e.g. "Vegetable Grower")
                # so the manifest example { "wheat farm": "wheat_farm.jpg" }
                # works without renaming files.
                tex = self.textures.building(bt)
                if tex is None and bd.name:
                    tex = self.textures.building_by_name(bd.name)

            if tex is not None:
                # Sprite path — draw the texture filling the footprint.
                # The thin outline kept here gives the player a clear
                # building boundary even with semi-transparent sprites.
                arcade.draw_texture_rect(tex, arcade.LBWH(x, y, w, h))
                arcade.draw_lrbt_rectangle_outline(
                    x + 1, x + w - 1, y + 1, y + h - 1, (40, 35, 30), 1,
                )
            else:
                # Fallback: original colored-rectangle look.
                color = bd.color
                if bd.is_evolvable and tier is not None:
                    from house_evolution import tier_color
                    color = tier_color(tier)
                arcade.draw_lrbt_rectangle_filled(x + 1, x + w - 1, y + 1, y + h - 1, color)
                arcade.draw_lrbt_rectangle_outline(x + 1, x + w - 1, y + 1, y + h - 1, (40, 35, 30), 2)

            # v0.25: progress-bar overlay for buildings under construction.
            # Tint the footprint dark, then draw a horizontal bar near
            # the bottom showing how far along construction is. Bar
            # colour is goldenrod so it reads as "in progress" without
            # being mistaken for a health bar.
            if under_construction:
                # Dark scaffolding tint over the entire footprint.
                arcade.draw_lrbt_rectangle_filled(
                    x + 1, x + w - 1, y + 1, y + h - 1, (0, 0, 0, 120),
                )
                # Hatched scaffolding outline so the player sees this
                # is a worksite, not a damaged building. Two crossing
                # diagonals do the job at 32px.
                arcade.draw_line(
                    x + 2, y + 2, x + w - 2, y + h - 2,
                    (160, 130, 60, 200), 1,
                )
                arcade.draw_line(
                    x + 2, y + h - 2, x + w - 2, y + 2,
                    (160, 130, 60, 200), 1,
                )
                # Progress bar near the bottom of the footprint.
                frac = self.construction_fraction(orow, ocol)
                bar_pad = 3
                bar_l = x + bar_pad
                bar_r = x + w - bar_pad
                bar_b = y + bar_pad
                bar_t = bar_b + 4
                # Backdrop.
                arcade.draw_lrbt_rectangle_filled(
                    bar_l, bar_r, bar_b, bar_t, (30, 25, 20, 220),
                )
                # Filled portion.
                fill_r = bar_l + (bar_r - bar_l) * frac
                # Colour ramp: red at start, gold mid, green near done.
                if frac < 0.34:
                    fill_color = (200, 80, 60, 230)
                elif frac < 0.75:
                    fill_color = (220, 180, 60, 230)
                else:
                    fill_color = (90, 180, 90, 230)
                arcade.draw_lrbt_rectangle_filled(
                    bar_l, fill_r, bar_b, bar_t, fill_color,
                )
                arcade.draw_lrbt_rectangle_outline(
                    bar_l, bar_r, bar_b, bar_t, (60, 50, 40, 220), 1,
                )

            # v0.6: no more text labels on tiles. The texture is the
            # identity. If the user hovers a tile, the right-panel info
            # popup gives the building's full name — far more readable
            # than 3 cramped letters jammed into a 32×32 sprite.

    def _draw_hover(
        self, hover_row: int | None, hover_col: int | None, selected_building: str | None,
    ) -> None:
        if hover_row is None or hover_col is None or not selected_building:
            return
        bd = self.registry.get(selected_building)
        if bd is None:
            return
        can = self.can_place(hover_row, hover_col, selected_building)
        clr = COLOR_HIGHLIGHT if can else COLOR_INVALID
        for rr, cc in self._iter_footprint(hover_row, hover_col, bd):
            if 0 <= rr < self.rows and 0 <= cc < self.cols:
                x, y = self.grid_to_world(rr, cc)
                arcade.draw_lrbt_rectangle_filled(x, x + TILE_SIZE, y, y + TILE_SIZE, clr)
