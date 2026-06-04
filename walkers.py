"""Walkers — visual agents that move on the map.

Two flavours:

* ``Walker``: random-walk citizen. Pure atmosphere. Spawns from any
  populated building, wanders, despawns when over budget. Unchanged
  semantics from v0.4.

* ``DeliveryWalker``: A*-pathed agent that carries a single good from a
  producer to a consumer. When it arrives at the consumer's access tile,
  it "delivers" — the engine treats this as a per-tick supply event for
  the consumer building, which is what `HouseEvolution` reads to gate
  tier consumption requirements.

The walker's *type* (worker / trader / citizen) comes from the spawning
building's `worker_role` field in the building registry. Modders adding a
new building set `"worker_role": "trader"` and the walker just works —
no edits here required.

Why two classes instead of one mode-flagged class? Because the random and
delivery code paths share almost nothing — no shared movement, no shared
target selection. A unified class would be a sea of `if self.is_delivery`
branches. Subclassing keeps each path readable.
"""
from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

import arcade

from building import BuildingRegistry
from constants import GRID_COLS, GRID_ROWS, TERRAIN_WATER, TERRAIN_MOUNTAINS, TILE_SIZE


def _impassable(game_map: GameMap, row: int, col: int) -> bool:
    """v0.27: a single hop into the game_map oracle. Walkers can't cross
    water or mountains by default, but a finished bridge over water
    becomes passable. The check is centralised on the game_map so
    adding a new impassable terrain type (or a new "passable
    structure on impassable terrain", like the bridge) doesn't mean
    hunting through every walker subclass.

    Pre-v0.27 this function compared terrain ids directly. The
    rewrite keeps the same signature so call sites are unchanged,
    and the new function on `GameMap` is positive-form
    (`is_passable_for_walker`) which is easier to read; we negate
    here. The old direct-terrain check is preserved as a fallback
    for the (rare) test that builds a GameMap without the helper
    method available.
    """
    is_passable = getattr(game_map, "is_passable_for_walker", None)
    if is_passable is not None:
        return not is_passable(row, col)
    # Fallback (pre-v0.27 GameMap shape): no bridges, no bounds-aware
    # passability check. Used only by tests that mock GameMap with a
    # bare `terrain` array.
    t = game_map.terrain[row][col]
    return t == TERRAIN_WATER or t == TERRAIN_MOUNTAINS


def _impassable_sea(game_map: GameMap, row: int, col: int) -> bool:
    """v0.38: passability for *naval* walkers — the mirror image of the
    land check. Water is passable; land (everything that isn't water)
    is impassable. Out-of-bounds is impassable. A naval walker can sit
    on a water tile spanned by a bridge too, so we treat bridged water
    as water (passable) — ships pass *under* the conceptual bridge.

    Kept as a free function alongside ``_impassable`` so naval movement
    is a one-line domain swap in the naval walker rather than a flag
    threaded through every land call site.
    """
    rows = getattr(game_map, "rows", GRID_ROWS)
    cols = getattr(game_map, "cols", GRID_COLS)
    if not (0 <= row < rows and 0 <= col < cols):
        return True
    try:
        t = game_map.terrain[row][col]
    except (IndexError, AttributeError):
        return True
    return t != TERRAIN_WATER


# v0.43: dedicated sea pathfinding. v0.44: upgraded BFS → A*.
# Ships previously moved with a greedy one-step heuristic (sort the four
# neighbours by Manhattan distance to the goal, take the best passable
# one). That has no backtracking, so it deadlocks on concave coastlines
# and channels — a ship can walk itself into a dead-end inlet and sit
# there. find_sea_path finds an actual shortest step-path over the water
# domain, or returns None when the goal is genuinely unreachable (so the
# caller can fall back to the old greedy walk rather than freeze).
#
# v0.44: the frontier is now an A* priority queue ordered by
# g + Manhattan(h). On the 4-connected grid the Manhattan heuristic is
# admissible and consistent, so A* still returns a shortest path — but it
# explores far fewer tiles toward a directed goal than BFS did, which is
# what lets the expansion cap comfortably cover large open maps (BFS
# flood-filled corner-to-corner and tripped the cap; A* beelines).
SEA_PATH_MAX_EXPANSIONS = 4000  # default/min cap; scaled up for big maps


def _sea_path_cap(game_map: GameMap, override: int | None) -> int:
    """The expansion cap to use. An explicit override wins; otherwise we
    scale with map area so a large map doesn't false-negative on a
    legitimately reachable far goal. A* keeps the *actual* explored count
    far below this on typical geometry — the cap is only a stall guard."""
    if override is not None:
        return override
    rows = getattr(game_map, "rows", GRID_ROWS)
    cols = getattr(game_map, "cols", GRID_COLS)
    # Allow up to ~the whole grid to be explored in pathological cases,
    # but never less than the historical default.
    return max(SEA_PATH_MAX_EXPANSIONS, rows * cols)


def find_sea_path(
    game_map: GameMap,
    start: tuple[int, int],
    goal: tuple[int, int],
    *,
    max_expansions: int | None = None,
) -> list[tuple[int, int]] | None:
    """A* shortest path over the *sea* domain (the tiles a naval walker
    may occupy, per ``_impassable_sea``).

    Returns the full tile path including both ``start`` and ``goal``
    (so ``path[0] == start`` and ``path[-1] == goal``), or ``None`` if no
    water route exists within the expansion cap.

    Design notes:
      * A* with a Manhattan heuristic. On a 4-connected unit-cost grid
        Manhattan distance is admissible and consistent, so the first
        time the goal is popped we have a shortest path. A* explores a
        directed wedge toward the goal rather than BFS's omnidirectional
        flood, so it scales to large maps.
      * ``max_expansions`` defaults to ``None`` → an adaptive cap scaled
        to map area (see ``_sea_path_cap``). Pass an int to force a
        specific cap (tests use a tiny one to exercise the bail-out).
      * Goal must be water and reachable; a land goal (e.g. a dock tile)
        is never enqueued and yields ``None`` — callers pass a water goal
        (the adjacent berth tile) for "dock beside" semantics.
      * Out-of-bounds / blocked start or goal → ``None`` (no crash).
      * Tie-break: the heap entries carry a monotonic counter so equal-f
        nodes pop in insertion order — deterministic paths, and no
        attempt to compare the (row,col) tuples when f and g collide.
    """
    import heapq

    rows = getattr(game_map, "rows", GRID_ROWS)
    cols = getattr(game_map, "cols", GRID_COLS)
    sr, sc = start
    gr, gc = goal
    if not (0 <= sr < rows and 0 <= sc < cols):
        return None
    if not (0 <= gr < rows and 0 <= gc < cols):
        return None
    if _impassable_sea(game_map, sr, sc):
        return None
    if start == goal:
        return [start]
    # A land goal can never be reached on the sea domain.
    if _impassable_sea(game_map, gr, gc):
        return None

    cap = _sea_path_cap(game_map, max_expansions)

    def h(r: int, c: int) -> int:
        return abs(r - gr) + abs(c - gc)

    # Heap entries: (f, tiebreak, (r, c)). g tracked separately.
    counter = 0
    open_heap: list[tuple[int, int, tuple[int, int]]] = [(h(sr, sc), counter, start)]
    came_from: dict[tuple[int, int], tuple[int, int]] = {start: start}
    g_score: dict[tuple[int, int], int] = {start: 0}
    expansions = 0
    found = False
    while open_heap:
        _f, _t, cur = heapq.heappop(open_heap)
        if cur == goal:
            found = True
            break
        expansions += 1
        if expansions > cap:
            return None
        cr, cc = cur
        base_g = g_score[cur]
        for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nr, nc = cr + dr, cc + dc
            nxt = (nr, nc)
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if _impassable_sea(game_map, nr, nc):
                continue
            tentative = base_g + 1
            if tentative < g_score.get(nxt, 1 << 30):
                g_score[nxt] = tentative
                came_from[nxt] = cur
                counter += 1
                heapq.heappush(open_heap, (tentative + h(nr, nc), counter, nxt))
    if not found:
        return None
    # Reconstruct start → goal.
    path: list[tuple[int, int]] = [goal]
    node = goal
    while node != start:
        node = came_from[node]
        path.append(node)
    path.reverse()
    return path

from game_map import GameMap
from textures import TextureRegistry

if TYPE_CHECKING:
    from pathfinding import Pathfinder

log = logging.getLogger("caesar3.walkers")


# Sprites are drawn at this footprint (smaller than a tile so the walker
# reads as a "person" not a building).
WALKER_SPRITE_SIZE = 16

# v0.19.x: delivery walkers paint a small cargo-icon overlay so the
# player sees what good is being carried. ⅔ the walker sprite — big
# enough to read at 1× zoom, small enough not to hide the body.
CARGO_ICON_SIZE = 11

# How close (Chebyshev) a delivery walker must be to a house for that
# house to count as "served" with the carried good. 1 = walker is on the
# tile next to the house; effectively the walker delivers as it passes.
DELIVERY_REACH = 2


# ── Base walker (random walk, citizen) ──────────────────────────────────────
class Walker:
    """Random-walking citizen. Moves between adjacent grass/road tiles."""

    COLORS: dict[str, tuple[int, int, int]] = {
        "worker":  (220, 180, 60),
        "trader":  (60, 160, 220),
        "citizen": (200, 200, 200),
        "soldier": (200, 60, 60),
        "enemy":   (60, 30, 30),
        # v0.29: civic firefighter spawned from prefectures.
        "fireman": (240, 130, 30),
    }

    def __init__(
        self,
        role: str,
        row: int,
        col: int,
        textures: TextureRegistry | None = None,
    ):
        self.role = role
        self.row = row
        self.col = col
        self.target_row = row
        self.target_col = col
        self.progress = 0.0
        self.speed = 0.05
        self.idle = 0
        self.color = self.COLORS.get(role, (200, 200, 200))
        # Resolved once per walker — `textures` is the shared registry, so
        # this is a cache lookup, not a disk hit.
        self._texture = textures.walker(role) if textures is not None else None
        # True once the walker has finished its job and should be removed
        # by the manager. Random walkers are never "done" by themselves.
        self.done = False

    # ── Pathing ───────────────────────────────────────────────────────────
    def pick_target(self, game_map: GameMap) -> None:
        dirs = [(0, 1), (0, -1), (1, 0), (-1, 0)]
        random.shuffle(dirs)
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        for dr, dc in dirs:
            nr, nc = self.row + dr, self.col + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                if not _impassable(game_map, nr, nc):
                    self.target_row, self.target_col = nr, nc
                    self.progress = 0.0
                    return
        self.idle = 5

    def update(self, game_map: GameMap, dt_scale: float = 1.0) -> None:
        # ``dt_scale`` (v0.56) scales the per-frame progress increment so
        # motion is frame-rate independent; default 1.0 == one tick step.
        if self.idle > 0:
            self.idle -= 1
            if self.idle <= 0:
                self.pick_target(game_map)
            return
        if self.row == self.target_row and self.col == self.target_col:
            self.pick_target(game_map)
            return
        self.progress += self.speed * dt_scale
        if self.progress >= 1.0:
            self.row, self.col = self.target_row, self.target_col
            self.progress = 0.0

    # ── Drawing ───────────────────────────────────────────────────────────
    def draw(self) -> None:
        # Route through grid_to_world_center so an isometric projection
        # would only need to change the helper, not this code.
        x1, y1 = GameMap.grid_to_world_center(self.row, self.col)
        x2, y2 = GameMap.grid_to_world_center(self.target_row, self.target_col)
        t = self.progress
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        if self._texture is not None:
            arcade.draw_texture_rect(
                self._texture,
                arcade.LBWH(
                    x - WALKER_SPRITE_SIZE / 2, y - WALKER_SPRITE_SIZE / 2,
                    WALKER_SPRITE_SIZE, WALKER_SPRITE_SIZE,
                ),
            )
        else:
            arcade.draw_circle_filled(x, y, 4, self.color)
            arcade.draw_circle_outline(x, y, 4, (30, 25, 20), 1)


# ── Delivery walker (A* pathing, carries a good) ────────────────────────────
class DeliveryWalker(Walker):
    """Walks an A* path on roads carrying a single good.

    On every tick the walker advances one tile along its precomputed path.
    Houses within ``DELIVERY_REACH`` tiles get the good registered (read by
    house evolution to gate tier requirements). When the walker reaches
    the path's last tile it's marked ``done`` and the manager removes it.

    The path is a list of (row, col) road tiles produced by `Pathfinder`.
    If the path is empty or invalid, the walker is born ``done`` and
    despawns on the next sweep.
    """

    def __init__(
        self,
        good: str,
        path: list[tuple[int, int]],
        textures: TextureRegistry | None = None,
    ):
        if not path:
            # Defensive — shouldn't happen given the manager validates,
            # but if it does, fail closed instead of crashing.
            super().__init__("trader", 0, 0, textures=textures)
            self.done = True
            self.good = good
            self._path: list[tuple[int, int]] = []
            self._path_idx = 0
            # v0.19.x: pre-resolve the cargo icon. Empty path → never
            # drawn anyway, but keep the attribute so draw() doesn't
            # branch on hasattr.
            self._cargo_texture = (
                textures.resource(good) if textures is not None else None
            )
            return

        super().__init__("trader", path[0][0], path[0][1], textures=textures)
        self.good = good
        self._path = path
        self._path_idx = 0
        self.speed = 0.10  # Delivery walkers move twice as fast — they have a job.
        # v0.19.x: pre-resolve the cargo icon at spawn so the per-frame
        # draw is a single texture reference. Cached miss is fine —
        # the registry returns the same None for every subsequent
        # walker carrying that good if the file is absent.
        self._cargo_texture = (
            textures.resource(good) if textures is not None else None
        )
        # Set the first move target.
        if len(path) > 1:
            self.target_row, self.target_col = path[1]
            self._path_idx = 1
        else:
            # 1-tile "path" — already at destination; deliver instantly.
            self.done = True

    # Override base random-walk behaviour with path-following.
    def pick_target(self, game_map: GameMap) -> None:  # noqa: ARG002
        # Advance to next path step. If we've consumed the whole path,
        # the manager will reap us.
        self._path_idx += 1
        if self._path_idx >= len(self._path):
            self.done = True
            return
        self.target_row, self.target_col = self._path[self._path_idx]

    def update(self, game_map: GameMap, dt_scale: float = 1.0) -> None:
        # ``dt_scale`` (v0.56) scales the per-frame progress increment so
        # motion is frame-rate independent; default 1.0 == one tick step.
        if self.done:
            return
        if self.row == self.target_row and self.col == self.target_col:
            self.pick_target(game_map)
            return
        self.progress += self.speed * dt_scale
        if self.progress >= 1.0:
            self.row, self.col = self.target_row, self.target_col
            self.progress = 0.0

    # ── Drawing ───────────────────────────────────────────────────────────
    # v0.19.x: delivery walkers paint the carried good as a small icon
    # above-and-right of the walker sprite, so the player sees a
    # *wheat-laden* trader walk from farm to windmill. The base
    # Walker.draw() handles the body sprite; we overlay on top.
    #
    # Why above-right rather than centred? At 16-px walker size the
    # icon would otherwise hide the walker silhouette entirely; the
    # offset reads as "carrying a sack" — closer to the C3 visual
    # vocabulary, and it keeps the walker's path visibly trace-able.
    # If the cargo texture is missing (no resources/<good>.png), we
    # fall back to a flat-coloured square so the player still gets a
    # signal that *something* is being carried.
    def draw(self) -> None:
        super().draw()
        if self.done:
            return
        x1, y1 = GameMap.grid_to_world_center(self.row, self.col)
        x2, y2 = GameMap.grid_to_world_center(self.target_row, self.target_col)
        t = self.progress
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        # Offset above-and-right of the walker body. Sized to be readable
        # but not overwhelm the walker silhouette (~⅔ the walker sprite).
        ix = x + WALKER_SPRITE_SIZE / 2 - CARGO_ICON_SIZE / 2 + 2
        iy = y + WALKER_SPRITE_SIZE / 2 - CARGO_ICON_SIZE / 2 + 2
        if self._cargo_texture is not None:
            arcade.draw_texture_rect(
                self._cargo_texture,
                arcade.LBWH(ix, iy, CARGO_ICON_SIZE, CARGO_ICON_SIZE),
            )
        else:
            # No icon on disk — draw a tiny square in a deterministic
            # per-good colour so the player still sees *that* a cargo
            # is being carried. Hash the good name so each good gets
            # its own colour, stable across runs.
            h = abs(hash(self.good))
            color = (
                80 + (h & 0x7F),
                80 + ((h >> 8) & 0x7F),
                80 + ((h >> 16) & 0x7F),
            )
            arcade.draw_lrbt_rectangle_filled(
                ix, ix + CARGO_ICON_SIZE,
                iy, iy + CARGO_ICON_SIZE,
                color,
            )
            arcade.draw_lrbt_rectangle_outline(
                ix, ix + CARGO_ICON_SIZE,
                iy, iy + CARGO_ICON_SIZE,
                (30, 25, 20), 1,
            )


# ── v0.46: CargoCarrier — visible port→store goods flow ────────────────────
class CargoCarrier(Walker):
    """Walks a straight tile path from a port to its routed store,
    carrying a quantity of one good — the *visible* half of the v0.44
    port↔store supply chain (which until now only classified cargo, with
    no on-screen movement).

    A carrier is dispatched with a ``(good, qty)`` manifest and a path
    (port tile → store tile). It marches one tile per step (like
    DeliveryWalker) and is marked ``done`` on arrival; the manager
    deposits its goods into the destination store's ledger at that point.
    Uses the ``cargo_carrier`` sprite; the carried good is painted as a
    small icon overlay, same idiom as DeliveryWalker.

    Deliberately simple: no A* on land (ports and stores are close and
    road routing for a flavour walker would be over-engineering for the
    PoC), no collision. If the path is empty it's born done."""

    SPEED = 0.08

    def __init__(
        self,
        good: str,
        qty: int,
        path: list[tuple[int, int]],
        dest_store: tuple[int, int] | None = None,
        textures: TextureRegistry | None = None,
    ):
        start = path[0] if path else (0, 0)
        super().__init__("cargo_carrier", start[0], start[1], textures=textures)
        self.good = str(good)
        self.qty = int(qty)
        self.dest_store = dest_store
        self._path = list(path)
        self._path_idx = 0
        self.speed = self.SPEED
        self._cargo_texture = (
            textures.resource(good) if textures is not None else None
        )
        if len(self._path) > 1:
            self.target_row, self.target_col = self._path[1]
            self._path_idx = 1
        else:
            self.done = True

    def pick_target(self, game_map: GameMap) -> None:  # noqa: ARG002
        self._path_idx += 1
        if self._path_idx >= len(self._path):
            self.done = True
            return
        self.target_row, self.target_col = self._path[self._path_idx]

    def update(self, game_map: GameMap, dt_scale: float = 1.0) -> None:
        # ``dt_scale`` (v0.56) scales the per-frame progress increment so
        # motion is frame-rate independent; default 1.0 == one tick step.
        if self.done:
            return
        if self.row == self.target_row and self.col == self.target_col:
            self.pick_target(game_map)
            return
        self.progress += self.speed * dt_scale
        if self.progress >= 1.0:
            self.row, self.col = self.target_row, self.target_col
            self.progress = 0.0

    def draw(self) -> None:
        super().draw()
        if self.done:
            return
        x1, y1 = GameMap.grid_to_world_center(self.row, self.col)
        x2, y2 = GameMap.grid_to_world_center(self.target_row, self.target_col)
        t = self.progress
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        ix = x + WALKER_SPRITE_SIZE / 2 - CARGO_ICON_SIZE / 2 + 2
        iy = y + WALKER_SPRITE_SIZE / 2 - CARGO_ICON_SIZE / 2 + 2
        if self._cargo_texture is not None:
            arcade.draw_texture_rect(
                self._cargo_texture,
                arcade.LBWH(ix, iy, CARGO_ICON_SIZE, CARGO_ICON_SIZE),
            )


# ── Combat walkers (v0.6) ───────────────────────────────────────────────────
#
# Two flavours: friendly Soldier (spawns from barracks/fort, patrols and
# engages nearby enemies) and hostile Enemy (spawns from a map edge during
# a raid, walks toward the city centre, hits any soldier it bumps into).
#
# Combat is intentionally simple and grid-aligned — at each tick, every
# combat walker checks for an opponent within Chebyshev distance 1 and
# trades HP. No facing, no armour, no morale. The defence-service overlay
# (forts, towers) buffs soldier damage by `1 + COMBAT_DEFENCE_BONUS *
# intensity` so building a tower next to a fort actually means something.
class CombatWalker(Walker):
    """Mixin-ish base class for soldier / enemy walkers.

    The shared bits: HP bookkeeping, damage application, draw with a
    small HP bar above the sprite when wounded. Movement is identical to
    base Walker (random / target-driven) — subclasses override
    pick_target so soldiers patrol a defence radius and enemies head
    toward city centre.
    """

    def __init__(
        self,
        role: str,
        row: int,
        col: int,
        hp: int,
        damage: int,
        textures: TextureRegistry | None = None,
    ):
        super().__init__(role, row, col, textures=textures)
        self.hp = hp
        self.max_hp = hp
        self.damage = damage
        # Most recent (tick, target_id) we hit, for the debug log.
        self._last_strike: tuple[int, int] | None = None

    def take_damage(self, amount: int) -> None:
        self.hp = max(0, self.hp - amount)
        if self.hp <= 0:
            self.done = True

    def draw(self) -> None:
        super().draw()
        # HP bar above wounded combatants. Hidden when at full HP — keeps
        # the map clean for the common case (no active raid).
        if self.hp >= self.max_hp:
            return
        x1, y1 = GameMap.grid_to_world_center(self.row, self.col)
        x2, y2 = GameMap.grid_to_world_center(self.target_row, self.target_col)
        t = self.progress
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        bar_w = WALKER_SPRITE_SIZE
        bar_h = 2
        bx = x - bar_w / 2
        by = y + WALKER_SPRITE_SIZE / 2 + 2
        frac = self.hp / self.max_hp
        # Background (dark)
        arcade.draw_lrbt_rectangle_filled(
            bx, bx + bar_w, by, by + bar_h, (40, 30, 30),
        )
        # Foreground (green→red as HP falls)
        col_r = int(220 * (1 - frac) + 60 * frac)
        col_g = int(60 * (1 - frac) + 200 * frac)
        arcade.draw_lrbt_rectangle_filled(
            bx, bx + bar_w * frac, by, by + bar_h, (col_r, col_g, 40),
        )


class Soldier(CombatWalker):
    """Friendly combat walker. Spawned from barracks / fort.

    Movement: random walk inside a small radius around its home tile when
    no enemies are visible. When an Enemy walker is within sight (a small
    grid radius), it locks on and walks toward it instead. Sight is just
    Chebyshev distance — no LOS computation.

    v0.22 — equipment & morale:

    * ``armed`` — set at spawn from the city's weapons stockpile. An
      armed soldier hits at full damage; an unarmed one hits at
      ``COMBAT_UNARMED_DAMAGE_MULT × damage`` (default 0.0 — useless in
      melee). The armed flag never changes after spawn; the soldier
      doesn't pick up dropped weapons.
    * ``morale`` — float in [0,1]. Decays when the soldier takes damage
      or sees an adjacent ally die; recovers when no enemies are nearby
      and HP is full. Below ``COMBAT_MORALE_THRESHOLD`` the soldier
      retreats: it walks back toward its home tile and refuses to
      engage. A wounded soldier (HP < ``COMBAT_RETREAT_HP_FRAC × max_hp``)
      retreats regardless of morale — even brave troops know when to
      bandage up.

    v0.23.x — JSON-driven unit types:

    * ``unit_id`` — id of the unit type spawned (e.g. ``"scout"``,
      ``"heavy_cavalry"``). Defaults to ``"light_infantry"`` for back-
      compat with v0.22 callers that don't pass it; tests that
      instantiate a Soldier directly via the legacy ``Soldier(r, c, hp,
      damage)`` signature still get the same behaviour.
    * ``sight``, ``patrol_radius``, ``speed``, ``ranged``, ``armoured`` —
      now stored per-soldier (previously class constants). The
      WalkerManager sources these from the unit registry; legacy
      callers fall through to the v0.22 defaults.
    """

    # Class-level defaults preserved for legacy tests / saveload paths
    # that don't go through the unit registry. Production code overrides
    # these on the instance from the UnitDef.
    SIGHT = 6  # tiles
    PATROL_RADIUS = 8

    def __init__(
        self,
        home_row: int,
        home_col: int,
        hp: int,
        damage: int,
        textures: TextureRegistry | None = None,
        armed: bool = True,
        morale: float | None = None,
        unit_id: str = "light_infantry",
        sight: int | None = None,
        patrol_radius: int | None = None,
        speed: float | None = None,
        ranged: bool = False,
        armoured: bool = False,
        category: str = "infantry",
    ):
        super().__init__("soldier", home_row, home_col, hp, damage, textures=textures)
        self.home_row = home_row
        self.home_col = home_col
        self._target: "Enemy | None" = None
        # v0.22: equipment & morale.
        self.armed: bool = bool(armed)
        if morale is None:
            from balance import (
                COMBAT_MORALE_INITIAL, COMBAT_UNARMED_INITIAL_MORALE,
            )
            morale = (
                COMBAT_MORALE_INITIAL if self.armed
                else COMBAT_UNARMED_INITIAL_MORALE
            )
        self.morale: float = float(max(0.0, min(1.0, morale)))
        # v0.23.x: per-unit type info. None → fall back to class defaults
        # so existing tests keep working.
        self.unit_id: str = str(unit_id)
        self.sight_radius: int = int(
            sight if sight is not None else self.SIGHT
        )
        self.patrol_radius_tiles: int = int(
            patrol_radius if patrol_radius is not None else self.PATROL_RADIUS
        )
        if speed is not None:
            self.speed = float(speed)
        self.ranged: bool = bool(ranged)
        self.armoured: bool = bool(armoured)
        # v0.29: unit category — drives the cavalry-shock charge bonus
        # in WalkerManager._resolve_combat. "cavalry" gets the bonus on
        # first contact per target; other categories ignore the field.
        # Defaults to "infantry" for back-compat with v0.22-style
        # Soldier(r, c, hp, damage) callers that don't go through the
        # unit registry. Pre-v0.29 saves load with the default — the
        # only observable diff is they don't get a charge bonus until
        # the next garrison-spawn rotation refreshes the walker pool.
        self.category: str = str(category)
        # v0.29: per-target charge-bonus ledger. Each entry is the
        # id() of an Enemy this soldier has already struck — used to
        # gate the shock multiplier to *first* contact only. Stored
        # as a set of object ids rather than Enemy refs so a dead
        # enemy can be GC'd; we never iterate this set for combat,
        # only check ``in``.
        self._struck_targets: set[int] = set()
        # v0.23.x: prefer the unit-specific sprite (e.g. heavy_cavalry.png).
        # Falls back to the generic 'soldier' sprite already set by the
        # base Walker init — which itself falls back to the colour dot
        # when no PNG is present.
        if textures is not None and unit_id and unit_id != "light_infantry":
            unit_tex = textures.walker(unit_id)
            if unit_tex is not None:
                self._texture = unit_tex

    # v0.23.x: armoured units take reduced incoming damage.
    def take_damage(self, amount: int) -> None:
        if self.armoured:
            from units import ARMOURED_INCOMING_DAMAGE_MULT
            amount = max(1, int(round(amount * ARMOURED_INCOMING_DAMAGE_MULT)))
        # v0.22: taking damage costs morale on top of HP. Wrapping the
        # base method keeps the death/HP bookkeeping shared with the
        # Enemy class.
        # Inline the parent CombatWalker.take_damage so the armour
        # reduction lands ahead of HP bookkeeping. (Direct super() call
        # would re-apply the original `amount`.)
        self.hp = max(0, self.hp - amount)
        if self.hp <= 0:
            self.done = True
            return
        from balance import COMBAT_MORALE_DECAY_ON_LOSS
        # Half decay on a single hit; full decay reserved for the
        # adjacent-ally-death event in WalkerManager._resolve_combat.
        self.lose_morale(COMBAT_MORALE_DECAY_ON_LOSS * 0.5)

    # ── v0.22 morale helpers ──────────────────────────────────────────
    def is_retreating(self) -> bool:
        """True when the soldier should head home rather than engage.

        Two triggers, ORed: morale below threshold, OR HP fraction below
        the retreat fraction. Either alone sends the unit home; combat
        resolution skips its outgoing damage while retreating, but it
        still takes incoming hits — a routing army is vulnerable.
        """
        from balance import COMBAT_MORALE_THRESHOLD, COMBAT_RETREAT_HP_FRAC
        hp_frac = self.hp / self.max_hp if self.max_hp > 0 else 0.0
        return (
            self.morale < COMBAT_MORALE_THRESHOLD
            or hp_frac < COMBAT_RETREAT_HP_FRAC
        )

    def lose_morale(self, amount: float) -> None:
        """Clamp-aware morale decay. Public so the WalkerManager combat
        sweep can call it on adjacent allies of a slain soldier."""
        self.morale = max(0.0, self.morale - amount)

    def gain_morale(self, amount: float) -> None:
        self.morale = min(1.0, self.morale + amount)

    def _apply_command_move(self, game_map: GameMap) -> bool:
        """v0.47: if this unit is under a player MOVE order, step toward
        the order's target tile and return True (suppressing autonomous
        behaviour this tick). STOP clears any active order. Arrival clears
        the MOVE order so the unit resumes patrol/engage. False when
        there's no movement command to act on."""
        from commands import Verb
        cs = getattr(self, "command", None)
        if cs is None or cs.order is None:
            return False
        verb = cs.order.verb
        if verb == Verb.STOP:
            cs.clear_order()
            return False
        if verb == Verb.MOVE and cs.order.target_tile is not None:
            tr, tc = cs.order.target_tile
            if (self.row, self.col) == (tr, tc):
                cs.clear_order()  # arrived → resume autonomy
                return False
            self._step_toward(tr, tc, game_map)
            return True
        return False

    def _apply_command_stance(self, game_map, enemies) -> bool:
        """v0.49: resolve a persistent stance order. Returns True if the
        stance handled this tick's movement (caller returns), False to
        fall through to default autonomy.

        Cheap stances (reuse existing morale/home/patrol machinery):
          * DEFEND — hold an anchor tile; engage an enemy in sight but
            never chase beyond it; drift back to the anchor when clear.
          * REST — hold position and recover morale faster; never engage.
          * PATROL — loop between the anchor and the order's waypoint.
          * RETREAT — walk home, ignore enemies (a commanded fall-back).
        Other verbs return False (resolved elsewhere or not yet)."""
        from commands import Verb
        cs = getattr(self, "command", None)
        if cs is None or cs.order is None:
            return False
        verb = cs.order.verb

        if verb == Verb.RETREAT:
            self._target = None
            self._step_toward(self.home_row, self.home_col, game_map)
            if (self.row, self.col) == (self.home_row, self.home_col):
                cs.clear_order()  # reached home → resume autonomy
            return True

        if verb == Verb.REST:
            from balance import COMBAT_MORALE_RECOVERY
            self.gain_morale(COMBAT_MORALE_RECOVERY * 2.0)  # 2x recovery
            self.idle = 1  # hold position, don't engage
            return True

        if verb == Verb.DEFEND:
            anchor = getattr(self, "_defend_anchor", None)
            if anchor is None:
                anchor = (self.row, self.col)
                self._defend_anchor = anchor
            tgt = self._find_enemy(enemies)
            if tgt is not None:
                # Engage only if the enemy is within sight of the anchor —
                # hold the line, don't get drawn off it.
                if (max(abs(tgt.row - anchor[0]), abs(tgt.col - anchor[1]))
                        <= self.sight_radius):
                    self._step_toward(tgt.row, tgt.col, game_map)
                    return True
            if (self.row, self.col) != anchor:
                self._step_toward(anchor[0], anchor[1], game_map)  # re-form
            else:
                self.idle = 1
            return True

        if verb == Verb.PATROL:
            way = cs.order.target_tile
            if way is None:
                return False  # no waypoint → fall through to autonomy
            anchor = getattr(self, "_patrol_anchor", None)
            if anchor is None:
                anchor = (self.row, self.col)
                self._patrol_anchor = anchor
            leg = getattr(self, "_patrol_leg", way)
            if (self.row, self.col) == leg:
                leg = anchor if leg == way else way  # flip at each end
            self._patrol_leg = leg
            self._step_toward(leg[0], leg[1], game_map)
            return True

        return False

    def pick_target_with_world(
        self, game_map: GameMap, enemies: list["Enemy"],
    ) -> None:
        """Choose next move tile.

        v0.22 retreat: if the soldier's morale or HP have cratered, it
        ignores the enemy list entirely and walks back toward home.
        Otherwise: locks onto the closest in-sight enemy if any;
        otherwise wanders within PATROL_RADIUS of home.
        """
        # v0.47: a player-issued command takes precedence over the
        # autonomous patrol/engage behaviour. Only MOVE/STOP resolve in
        # this drop; other verbs are stored on the unit but not yet acted
        # on (later campaign drops add their resolution). A MOVE order
        # steps the soldier toward its target tile and clears itself on
        # arrival, after which the unit resumes autonomous behaviour.
        if self._apply_command_move(game_map):
            return
        # v0.49: persistent stance commands (DEFEND/REST/PATROL/RETREAT)
        # layer over autonomy — they re-assert each tick until the player
        # changes the order. Resolved before the default patrol/engage so
        # a stance overrides wandering but DEFEND still engages intruders.
        if self._apply_command_stance(game_map, enemies):
            return
        # v0.22: retreat first — overrides target selection.
        if self.is_retreating():
            self._target = None
            self._step_toward(self.home_row, self.home_col, game_map)
            return
        # Drop dead targets.
        if self._target is not None and self._target.done:
            self._target = None
        # Acquire a new target if needed.
        if self._target is None:
            self._target = self._find_enemy(enemies)
        if self._target is not None:
            er, ec = self._target.row, self._target.col
            self._step_toward(er, ec, game_map)
            return
        # No target & no enemies in sight: morale recovers slowly while
        # full-HP soldiers patrol home turf.
        if not enemies and self.hp >= self.max_hp:
            from balance import COMBAT_MORALE_RECOVERY
            self.gain_morale(COMBAT_MORALE_RECOVERY)
        # Wander.
        self._patrol(game_map)

    def _find_enemy(self, enemies: list["Enemy"]) -> "Enemy | None":
        nearest: Enemy | None = None
        # v0.23.x: per-soldier sight radius (was class-constant self.SIGHT).
        nearest_d = self.sight_radius + 1
        for e in enemies:
            if e.done:
                continue
            d = max(abs(e.row - self.row), abs(e.col - self.col))
            if d < nearest_d:
                nearest, nearest_d = e, d
        return nearest

    def _step_toward(self, er: int, ec: int, game_map: GameMap) -> None:
        """Move one tile in the (signed) direction of (er, ec)."""
        dr = (er > self.row) - (er < self.row)  # -1 / 0 / 1
        dc = (ec > self.col) - (ec < self.col)
        # Prefer the longer axis to avoid back-and-forth on diagonals.
        if abs(er - self.row) >= abs(ec - self.col):
            candidates = [(dr, 0), (0, dc), (-dr, 0), (0, -dc)]
        else:
            candidates = [(0, dc), (dr, 0), (0, -dc), (-dr, 0)]
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        for ddr, ddc in candidates:
            if ddr == 0 and ddc == 0:
                continue
            nr, nc = self.row + ddr, self.col + ddc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if _impassable(game_map, nr, nc):
                continue
            self.target_row, self.target_col = nr, nc
            self.progress = 0.0
            return

    def _patrol(self, game_map: GameMap) -> None:
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        for _ in range(8):
            dr, dc = random.choice([(0, 1), (0, -1), (1, 0), (-1, 0)])
            nr, nc = self.row + dr, self.col + dc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if _impassable(game_map, nr, nc):
                continue
            d = max(abs(nr - self.home_row), abs(nc - self.home_col))
            # v0.23.x: per-soldier patrol radius (was class-constant).
            if d > self.patrol_radius_tiles:
                continue
            self.target_row, self.target_col = nr, nc
            self.progress = 0.0
            return
        self.idle = 5

    # base Walker.update calls self.pick_target — soldiers ignore that and
    # use the enemy-aware picker instead. CombatManager drives it.


class Enemy(CombatWalker):
    """Hostile combat walker. Spawns from a map edge during raids.

    Movement: heads toward (target_row, target_col) — usually the city
    centre. If a soldier comes adjacent, the combat manager's per-tick
    sweep will deal damage; the enemy keeps moving regardless (no
    "engaged-in-combat" state to keep the model honest about its
    simplicity).

    v0.33: ``unit_id`` declares which barbarian unit type this is.
    Defaults to ``"barbarian_infantry"`` (the canonical raider) for
    back-compat with v0.32 callers that don't pass it; the registry
    is consulted to resolve a per-unit sprite (so a barbarian shows
    up with its own art instead of the generic ``enemy`` dot).
    Faction is read off the unit registry; combat code that needs
    to distinguish "roman vs. barbarian" reads ``self.faction`` here
    rather than checking ``isinstance(w, Enemy)``.
    """

    def __init__(
        self,
        row: int,
        col: int,
        objective_row: int,
        objective_col: int,
        hp: int,
        damage: int,
        textures: TextureRegistry | None = None,
        unit_id: str = "barbarian_infantry",
        faction: str = "barbarian",
    ):
        super().__init__("enemy", row, col, hp, damage, textures=textures)
        self.objective_row = objective_row
        self.objective_col = objective_col
        # v0.33: unit type + faction. Mirrors the Soldier-side fields
        # so combat code can treat both sides symmetrically (and so a
        # future "barbarian archer" unit type can ride the same
        # ranged-attack pass the bowman uses without subclassing).
        self.unit_id: str = str(unit_id)
        self.faction: str = str(faction)
        # Prefer the unit-specific sprite if a PNG exists under
        # ``assets/textures/walkers/<unit_id>.png``. Falls back to
        # the generic 'enemy' sprite already set by the base init.
        if textures is not None and unit_id and unit_id != "enemy":
            unit_tex = textures.walker(unit_id)
            if unit_tex is not None:
                self._texture = unit_tex

    def pick_target(self, game_map: GameMap) -> None:
        """Step one tile toward the objective. Falls back to random walk
        if every direction is blocked (water on all sides — shouldn't
        happen in practice but defensive)."""
        dr = (self.objective_row > self.row) - (self.objective_row < self.row)
        dc = (self.objective_col > self.col) - (self.objective_col < self.col)
        if abs(self.objective_row - self.row) >= abs(self.objective_col - self.col):
            candidates = [(dr, 0), (0, dc), (-dr, 0), (0, -dc)]
        else:
            candidates = [(0, dc), (dr, 0), (0, -dc), (-dr, 0)]
        # Always include random fallbacks at the end.
        candidates += [(1, 0), (-1, 0), (0, 1), (0, -1)]
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        for ddr, ddc in candidates:
            if ddr == 0 and ddc == 0:
                continue
            nr, nc = self.row + ddr, self.col + ddc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if _impassable(game_map, nr, nc):
                continue
            self.target_row, self.target_col = nr, nc
            self.progress = 0.0
            return
        # Truly stuck — idle a tick.
        self.idle = 5


# ── v0.29: Fireman walker ───────────────────────────────────────────────────
class Fireman(Walker):
    """Civic walker that extinguishes burning buildings.

    Spawned periodically from each Prefecture (the building that already
    provides the ``safety`` service). The fireman wanders within the
    prefecture's service radius like a typical Caesar-3-style civic
    walker, and on each tick checks Chebyshev≤1 for any building with
    ``building_state[(orow, ocol)]['on_fire'] is True``. When found, it
    reduces ``burn`` by ``COMBAT_FIREMAN_EXTINGUISH_RATE`` per tick.
    When ``burn`` reaches 0 the ``on_fire`` flag is cleared and the
    building stops self-damaging — at which point the existing
    engineer-post repair tick can resume HP recovery.

    Why a walker and not just a service-radius effect? Two reasons:

    1. The player asked for "a fireman, not just engineer coverage" —
       the visible walker is the game-feel point. A radius effect would
       silently extinguish fires the moment the prefecture was placed;
       a walker makes firefighting *visible* (you watch the figure
       sprint between blazes) and creates failure modes (fireman on
       the wrong side of the city when a raid hits the granary).
    2. It composes with the existing pathing/idle infrastructure —
       Fireman behaves like any other Walker except for the per-tick
       burn-reduction sweep. No new movement code, no new pathfinder
       call site.

    The fireman has no concept of "I'm done" — it stays alive and
    patrols indefinitely. The Prefecture's spawn cooldown caps how
    many firemen exist simultaneously (one per prefecture by default,
    via the ``_fireman_count_for_home`` check in WalkerManager).
    """

    # Per-tile patrol radius around the home prefecture. Mirrors the
    # service_radius of the prefecture in data/buildings.json (4) so
    # the walker stays inside the area the prefecture is supposed to
    # protect — modders changing the prefecture's radius should also
    # bump this. Kept as a class constant rather than a JSON field
    # to avoid plumbing UnitRegistry into civic walkers; if a future
    # drop needs per-prefecture tuning we'll lift it then.
    PATROL_RADIUS = 4

    def __init__(
        self,
        home_row: int,
        home_col: int,
        textures: TextureRegistry | None = None,
    ):
        super().__init__("fireman", home_row, home_col, textures=textures)
        self.home_row = home_row
        self.home_col = home_col
        # Slightly brisker than a citizen — firemen are responding to
        # an emergency. Matches scout speed (0.1).
        self.speed = 0.1

    def pick_target(self, game_map: GameMap) -> None:
        """Random walk inside the patrol radius around home.

        Preferring tiles near a burning building would be ideal but
        requires a scan of building_state every step — for now we let
        the random walk find them. The patrol radius keeps the
        fireman near the prefecture, which is typically placed in
        the dense urban core where fires occur.
        """
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        for _ in range(8):
            dr, dc = random.choice([(0, 1), (0, -1), (1, 0), (-1, 0)])
            nr, nc = self.row + dr, self.col + dc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if _impassable(game_map, nr, nc):
                continue
            d = max(abs(nr - self.home_row), abs(nc - self.home_col))
            if d > self.PATROL_RADIUS:
                continue
            self.target_row, self.target_col = nr, nc
            self.progress = 0.0
            return
        self.idle = 5

    def extinguish_adjacent(self, game_map: GameMap) -> list[tuple[int, int, int]]:
        """Reduce burn on any on-fire building within Chebyshev 1.

        Returns a list of ``(orow, ocol, amount_extinguished)`` tuples
        for the WalkerManager to surface as combat-feed events. The
        method mutates ``game_map.building_state`` in place. When
        ``burn`` reaches 0 the ``on_fire`` flag is cleared (and the
        ``burn`` key removed entirely to keep the state dict tidy —
        engineer-post repair can now resume).
        """
        from balance import COMBAT_FIREMAN_EXTINGUISH_RATE
        rows = getattr(game_map, "rows", None)
        cols = getattr(game_map, "cols", None)
        if rows is None or cols is None:
            return []
        results: list[tuple[int, int, int]] = []
        # Scan the 3×3 Chebyshev box around the fireman.
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                nr, nc = self.row + dr, self.col + dc
                if not (0 <= nr < rows and 0 <= nc < cols):
                    continue
                cell = game_map.grid[nr][nc]
                if cell is None:
                    continue
                _bid, orow, ocol = cell
                state = game_map.building_state.get((orow, ocol))
                if state is None or not state.get("on_fire", False):
                    continue
                burn = state.get("burn", 0)
                # Cap the reduction to the current burn level so we
                # don't go negative.
                applied = min(burn, COMBAT_FIREMAN_EXTINGUISH_RATE)
                if applied <= 0:
                    # Defensive: on_fire=True but burn=0 (shouldn't
                    # happen, but if it does, normalise.)
                    state.pop("on_fire", None)
                    state.pop("burn", None)
                    continue
                state["burn"] = burn - applied
                results.append((orow, ocol, applied))
                if state["burn"] <= 0:
                    state.pop("on_fire", None)
                    state.pop("burn", None)
        return results


# ── v0.38: Naval walkers (ships move on the sea domain) ─────────────────────
class Ship(CombatWalker):
    """Base class for all naval walkers. A ship moves on water (the
    inverse passability domain of land walkers) toward an optional goal
    tile, falling back to a random water-walk when it has no goal.

    Ships reuse ``CombatWalker`` for HP / take_damage / the HP-bar draw
    (a warship needs all three; trade/transport ships benefit from being
    sinkable by raiders). Movement is overridden to use ``_impassable_sea``
    so the existing land call sites are untouched.

    The three concrete ship roles:
      * ``TradeShip`` — carries a good between a coastal city and a map
        edge; the naval analogue of ``DeliveryWalker``. Drives sea-route
        trade income (see ``trade.py`` sea-route gating).
      * ``TransportShip`` — carries N embarked soldiers; on reaching a
        land-adjacent water tile it *disembarks* them onto the shore.
        This is the "transport legions to land" mechanic.
      * ``Warship`` — a ranged naval combatant; engages enemy ships and
        coastal raiders within range.
    """

    SHIP_SPEED = 0.035  # slightly slower than a land walker — ships are big.

    def __init__(
        self,
        role: str,
        row: int,
        col: int,
        hp: int = 60,
        damage: int = 0,
        textures: TextureRegistry | None = None,
        goal: tuple[int, int] | None = None,
        faction: str = "roman",
    ):
        super().__init__(role, row, col, hp, damage, textures=textures)
        self.speed = self.SHIP_SPEED
        self.goal: tuple[int, int] | None = goal
        self.faction: str = str(faction)
        # v0.47: every ship is naval — the command system's land/naval
        # verb legality reads this. Warship/EnemyShip set it too; putting
        # it on the base makes trade/transport ships classify correctly.
        self.category: str = "naval"
        # v0.43: cached BFS sea path (list of tiles, current position
        # first) and the goal it was computed for. Recomputed lazily in
        # pick_target when the goal changes or the path is exhausted/stale.
        self._sea_path: list[tuple[int, int]] | None = None
        self._sea_path_goal: tuple[int, int] | None = None
        # v0.46: every ship is a carrier/container. ``manifest`` is a
        # generic {good: qty} cargo hold shared by all ship types — the
        # unified model behind TradeShip.cargo_qty (a single-good
        # convenience) and TransportShip.cargo (troops). Capacity is
        # CARGO_CAPACITY total units. Helpers below keep it consistent.
        self.manifest: dict[str, int] = {}

    CARGO_CAPACITY = 100  # total units a hull can hold across all goods

    def cargo_total(self) -> int:
        """Total units currently aboard across every good in the manifest."""
        return sum(self.manifest.values())

    def cargo_space(self) -> int:
        """Remaining free capacity."""
        return max(0, self.CARGO_CAPACITY - self.cargo_total())

    def load_cargo(self, good: str, qty: int) -> int:
        """Load up to ``qty`` units of ``good``, capped by free space.
        Returns the amount actually loaded (0 if the hull is full or qty
        is non-positive)."""
        if qty <= 0:
            return 0
        take = min(qty, self.cargo_space())
        if take > 0:
            self.manifest[good] = self.manifest.get(good, 0) + take
        return take

    def unload_cargo(self, good: str, qty: int | None = None) -> int:
        """Unload ``qty`` units of ``good`` (or all of it if qty is None).
        Returns the amount actually removed."""
        have = self.manifest.get(good, 0)
        give = have if qty is None else max(0, min(qty, have))
        if give > 0:
            remaining = have - give
            if remaining > 0:
                self.manifest[good] = remaining
            else:
                self.manifest.pop(good, None)
        return give

    def _recompute_sea_path(self, game_map: GameMap) -> None:
        """(Re)compute the BFS path from the current tile to the goal.
        Leaves ``_sea_path`` None if no route exists (caller falls back to
        the greedy step)."""
        if self.goal is None:
            self._sea_path = None
            self._sea_path_goal = None
            return
        path = find_sea_path(game_map, (self.row, self.col), self.goal)
        self._sea_path = path
        self._sea_path_goal = self.goal

    def _next_path_step(self, game_map: GameMap) -> tuple[int, int] | None:
        """Return the next tile to move to along the cached BFS path, or
        None if there's no usable path (goal reached, unreachable, or the
        cache is stale and a recompute still found nothing).

        The cache is rebuilt when: the goal changed since it was built;
        there is no cache yet; or the ship isn't on the path anymore
        (e.g. it was nudged, or the path is exhausted)."""
        if self.goal is None:
            return None
        here = (self.row, self.col)
        stale = (
            self._sea_path is None
            or self._sea_path_goal != self.goal
            or here not in self._sea_path
        )
        if stale:
            self._recompute_sea_path(game_map)
        if not self._sea_path or here not in self._sea_path:
            return None
        idx = self._sea_path.index(here)
        if idx + 1 >= len(self._sea_path):
            return None  # already at the goal end of the path
        nxt = self._sea_path[idx + 1]
        # Defend against a path that crosses a tile that became blocked
        # (terrain edits, bridges): if the next step isn't water anymore,
        # force a recompute once.
        if _impassable_sea(game_map, nxt[0], nxt[1]):
            self._recompute_sea_path(game_map)
            if not self._sea_path or here not in self._sea_path:
                return None
            idx = self._sea_path.index(here)
            if idx + 1 >= len(self._sea_path):
                return None
            nxt = self._sea_path[idx + 1]
            if _impassable_sea(game_map, nxt[0], nxt[1]):
                return None
        return nxt

    def pick_target(self, game_map: GameMap) -> None:
        """v0.43: follow a cached BFS sea path toward ``goal``; fall back
        to the pre-v0.43 greedy one-step heuristic when there's no path
        (unreachable goal, or no goal → random water-walk). The greedy
        fallback guarantees a ship is never *worse* off than before: if
        BFS can't help, behaviour is exactly the old behaviour."""
        # v0.48: a player MOVE command overrides the ship's scripted goal
        # — set the goal to the ordered tile so the existing A* pathing
        # sails there. STOP clears the order (ship resumes scripted goal
        # or idles). Only MOVE/STOP act here; other verbs are stored.
        cs = getattr(self, "command", None)
        if cs is not None and cs.order is not None:
            from commands import Verb
            if cs.order.verb == Verb.STOP:
                cs.clear_order()
            elif cs.order.verb == Verb.MOVE and cs.order.target_tile is not None:
                self.goal = cs.order.target_tile
                if (self.row, self.col) == self.goal:
                    cs.clear_order()  # arrived → resume scripted behaviour
        # Try the real pathfinder first.
        nxt = self._next_path_step(game_map)
        if nxt is not None:
            self.target_row, self.target_col = nxt
            self.progress = 0.0
            return
        # ── Greedy fallback (pre-v0.43 behaviour) ─────────────────────
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        candidates = [(0, 1), (0, -1), (1, 0), (-1, 0)]
        if self.goal is not None:
            gr, gc = self.goal
            candidates.sort(
                key=lambda d: abs((self.row + d[0]) - gr)
                + abs((self.col + d[1]) - gc)
            )
        else:
            random.shuffle(candidates)
        for dr, dc in candidates:
            nr, nc = self.row + dr, self.col + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                if not _impassable_sea(game_map, nr, nc):
                    self.target_row, self.target_col = nr, nc
                    self.progress = 0.0
                    return
        self.idle = 5

    def at_goal(self) -> bool:
        return self.goal is not None and (self.row, self.col) == self.goal

    def adjacent_land(self, game_map: GameMap) -> tuple[int, int] | None:
        """Return a land tile orthogonally adjacent to the ship, or None.
        Used by TransportShip to find a shore to disembark onto."""
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nr, nc = self.row + dr, self.col + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                # Land = passable for a land walker (not water/mountain).
                if not _impassable(game_map, nr, nc):
                    return (nr, nc)
        return None


class TradeShip(Ship):
    """Naval trader — the sea analogue of DeliveryWalker. Carries one
    good from a coastal source toward a map-edge "trade exit" goal; the
    manager credits the economy when it reaches the goal, then the ship
    turns around (goal swapped back to its home dock).

    v0.40 (harbor-transfer PoC): a trade ship now carries a real
    ``cargo_qty`` of ``good``, loaded from the goods ledger at its home
    endpoint (the harbor buffer). Export income at the exit is
    proportional to what it actually loaded — no stock, no cargo, no
    phantom income. ``LOAD_AMOUNT`` is the per-trip cap a single hull
    can carry."""

    LOAD_AMOUNT = 20  # max units a trade ship loads from the buffer per trip

    def __init__(self, row, col, home, exit_tile, good="wine",
                 textures=None, faction="roman"):
        super().__init__("trade_ship", row, col, hp=50, damage=0,
                         textures=textures, goal=exit_tile, faction=faction)
        self.home: tuple[int, int] = home
        self.exit_tile: tuple[int, int] = exit_tile
        self.good: str = str(good)
        self.outbound: bool = True  # heading to exit; False = returning
        # v0.40: units of ``good`` currently aboard. 0 means the ship is
        # sailing empty (e.g. it found the buffer dry when it loaded).
        self.cargo_qty: int = 0
        # v0.44: which store class this cargo routes through —
        # "granary" for nutrients, "warehouse" otherwise. Set when loaded.
        self.cargo_store_class: str = ""

    def flip_route(self) -> None:
        """Reached an endpoint — reverse direction."""
        self.outbound = not self.outbound
        self.goal = self.exit_tile if self.outbound else self.home


class TransportShip(Ship):
    """Troop transport (the trireme). Carries up to ``TROOP_SLOTS``
    embarked Soldier instances across sea tiles and disembarks them onto
    the first land tile adjacent to its goal. The manager handles the
    actual hand-off (moving soldiers from ``cargo`` into the live walker
    list) when ``ready_to_unload`` is True.

    v0.46: capacity is capped at 10 troop slots — a trireme is a fixed-
    size hull, not an unbounded bag. Embarking past the cap is refused
    (the extra soldiers stay ashore / wait for the next sailing).
    """

    TROOP_SLOTS = 10  # a trireme carries at most this many legionaries

    def __init__(self, row, col, landing_goal, cargo=None,
                 textures=None, faction="roman"):
        super().__init__("transport_ship", row, col, hp=70, damage=0,
                         textures=textures, goal=landing_goal, faction=faction)
        # Respect the slot cap even on construction: a caller passing 20
        # soldiers gets the first 10 aboard, not an over-full hull.
        incoming = list(cargo or [])
        self.cargo: list = incoming[:self.TROOP_SLOTS]
        self.overflow: list = incoming[self.TROOP_SLOTS:]
        self.unloaded: bool = False

    def free_slots(self) -> int:
        """Empty troop slots remaining."""
        return max(0, self.TROOP_SLOTS - len(self.cargo))

    def is_full(self) -> bool:
        return len(self.cargo) >= self.TROOP_SLOTS

    def embark(self, soldiers) -> int:
        """Board as many of ``soldiers`` (an iterable of Soldier) as fit
        in the free slots. Returns the number actually embarked; any that
        don't fit are simply not taken (the caller keeps them)."""
        boarded = 0
        for s in soldiers:
            if self.is_full():
                break
            self.cargo.append(s)
            boarded += 1
        return boarded

    def ready_to_unload(self, game_map: GameMap) -> bool:
        """True once the transport is adjacent to land near its goal and
        still has cargo. The manager calls this each tick; on True it
        pulls the cargo onto the shore."""
        if self.unloaded or not self.cargo:
            return False
        return self.adjacent_land(game_map) is not None


class Warship(Ship):
    """Ranged naval combatant. Reuses the Soldier-style ranged stats but
    floats. The combat resolver treats a Warship like a ranged soldier
    that happens to sit on water — it engages enemy ships / coastal
    enemies within ``RANGED_ATTACK_RANGE``."""

    def __init__(self, row, col, hp=90, damage=14, textures=None,
                 goal=None, faction="roman", ranged=True):
        super().__init__("warship", row, col, hp=hp, damage=damage,
                         textures=textures, goal=goal, faction=faction)
        self.ranged: bool = bool(ranged)
        self.armed: bool = True
        self.category: str = "naval"
        # Combat-resolver compatibility: the friendly-combatant loop in
        # _resolve_combat calls s.is_retreating() directly. A warship
        # doesn't rout (no morale model), so it never retreats. The
        # shock-ledger access in the same loop is getattr-guarded, so we
        # don't need _struck_targets — a warship simply never gets a
        # cavalry charge bonus (category != "cavalry").
        self._struck_targets: set[int] = set()

    def is_retreating(self) -> bool:
        return False


# ── v0.45: EnemyShip — hostile naval raider ────────────────────────────────
class EnemyShip(Enemy):
    """A hostile vessel — the naval analogue of ``Enemy``. Subclasses
    Enemy so the combat resolver's ``enemies`` set picks it up for free
    and friendly warships engage it exactly like a land raider; the
    difference is purely movement: an EnemyShip sails the *sea* domain
    (A* over water) toward its objective rather than marching on land.

    Two roles it plays in naval combat depth (v0.45):
      * a target a Warship must intercept at sea (it's in the enemy set,
        so the existing ranged pass already shoots it once in range); and
      * a threat to trade ships — see ``WalkerManager._resolve_sea_raids``,
        which lets an EnemyShip damage a nearby TradeShip (trade ships
        are non-combatants the combat resolver otherwise ignores).

    It reuses Enemy's HP/damage/objective fields. ``ranged`` is False by
    default (a raider rams/boards at melee range), but the field exists
    so a future "enemy ballista ship" can ride the ranged pass.
    """

    SHIP_SPEED = 0.035

    def __init__(
        self,
        row: int,
        col: int,
        objective_row: int,
        objective_col: int,
        hp: int = 80,
        damage: int = 10,
        textures: TextureRegistry | None = None,
        unit_id: str = "barbarian_ship",
        faction: str = "barbarian",
    ):
        super().__init__(row, col, objective_row, objective_col, hp, damage,
                         textures=textures, unit_id=unit_id, faction=faction)
        self.speed = self.SHIP_SPEED
        self.category = "naval"
        self.ranged = False
        # A* path cache, same idiom as Ship.
        self.goal: tuple[int, int] = (objective_row, objective_col)
        self._sea_path: list[tuple[int, int]] | None = None
        self._sea_path_goal: tuple[int, int] | None = None

    def pick_target(self, game_map: GameMap) -> None:
        """Sail one step toward the objective along an A* sea path; fall
        back to a greedy water step (then idle) when no path exists. Same
        structure as ``Ship.pick_target`` but without the no-goal random
        walk — a raider always has an objective."""
        goal = (self.objective_row, self.objective_col)
        here = (self.row, self.col)
        stale = (
            self._sea_path is None
            or self._sea_path_goal != goal
            or here not in self._sea_path
        )
        if stale:
            self._sea_path = find_sea_path(game_map, here, goal)
            self._sea_path_goal = goal
        if self._sea_path and here in self._sea_path:
            idx = self._sea_path.index(here)
            if idx + 1 < len(self._sea_path):
                nxt = self._sea_path[idx + 1]
                if not _impassable_sea(game_map, nxt[0], nxt[1]):
                    self.target_row, self.target_col = nxt
                    self.progress = 0.0
                    return
        # Greedy fallback over water.
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        cands = sorted(
            [(0, 1), (0, -1), (1, 0), (-1, 0)],
            key=lambda d: abs((self.row + d[0]) - goal[0])
            + abs((self.col + d[1]) - goal[1]),
        )
        for dr, dc in cands:
            nr, nc = self.row + dr, self.col + dc
            if 0 <= nr < rows and 0 <= nc < cols and not _impassable_sea(game_map, nr, nc):
                self.target_row, self.target_col = nr, nc
                self.progress = 0.0
                return
        self.idle = 5
class Projectile:
    """Lightweight visual for a ranged attack — arrow or ballista bolt.

    Spawned by the combat resolver whenever a ranged Soldier connects
    with an Enemy (or rains a fire-arrow on an enemy-held building).
    Damage was already applied at spawn time; the projectile exists
    only to show the shot travelling across the map, then despawn.

    Design notes:

    * No collision, no pathfinding, no targeting recalculation. The
      shot starts at the shooter's pixel position at spawn time and
      ends at the target's pixel position at spawn time — if the
      target moves between spawn and arrival, the trail still reads
      correctly because at 1-2 tile range and ~10 frames lifetime
      the discrepancy is invisible.
    * Drawn as a simple primitive (line for ``kind="arrow"``, thicker
      rectangle for ``kind="bolt"``) — no PNG, because at 2-tile range
      and ~10-frame lifetime the projectile is a transient streak,
      not a sprite the player would inspect.
    * The ``Walker`` base class isn't a fit (no row/col tile motion,
      no service or combat behaviour), so this is a standalone class
      managed alongside ``walkers`` in WalkerManager.
    """

    # Lifetime in ticks. Long enough to be visible at the typical 0.5s/tick
    # rate, short enough that it always arrives well before the next combat
    # sweep so we don't accumulate trails. 6 ticks ≈ 3 seconds at default
    # game speed; at 4× speed-up it's ~0.75 s — still readable.
    LIFETIME_TICKS: int = 6

    def __init__(
        self,
        kind: str,
        from_row: float,
        from_col: float,
        to_row: float,
        to_col: float,
    ):
        # "arrow" (bowman, thin line) | "bolt" (ballista, thicker line).
        # Unknown kinds fall through to the arrow path so a future
        # "javelin" placeholder still renders something.
        self.kind = kind
        self.from_row = float(from_row)
        self.from_col = float(from_col)
        self.to_row = float(to_row)
        self.to_col = float(to_col)
        self.age = 0
        self.done = False

    def update(self) -> None:
        self.age += 1
        if self.age >= self.LIFETIME_TICKS:
            self.done = True

    def draw(self) -> None:
        # Position along the flight at the current age. Fraction goes
        # 0 → 1 over the lifetime.
        denom = max(1, self.LIFETIME_TICKS - 1)
        t = min(1.0, self.age / denom)
        x1, y1 = GameMap.grid_to_world_center(self.from_row, self.from_col)
        x2, y2 = GameMap.grid_to_world_center(self.to_row, self.to_col)
        # Draw a short streak: the head is at the lerp point, the tail
        # is a few pixels behind. Trail length is a tile-fraction so it
        # scales with zoom level (which scales TILE_SIZE in the world
        # transform). For a single-tile shot the trail covers ~25% of
        # the full path; for a longer ballista shot it's still readable
        # because it's anchored to the head, not the start.
        head_x = x1 + (x2 - x1) * t
        head_y = y1 + (y2 - y1) * t
        # Tail offset: a fixed fraction of the full vector, capped so
        # the trail doesn't extend back past the shooter.
        tail_frac = min(t, 0.25)
        tail_x = head_x - (x2 - x1) * tail_frac
        tail_y = head_y - (y2 - y1) * tail_frac
        if self.kind == "bolt":
            # Thicker, darker — ballista projectile.
            arcade.draw_line(tail_x, tail_y, head_x, head_y, (60, 40, 20), 3)
            # Bright head.
            arcade.draw_circle_filled(head_x, head_y, 2, (200, 180, 100))
        else:
            # Default: bowman arrow.
            arcade.draw_line(tail_x, tail_y, head_x, head_y, (255, 230, 180), 1)
            # Tiny head pixel for visibility against grass.
            arcade.draw_circle_filled(head_x, head_y, 1.5, (255, 240, 200))


def projectile_kind_for(soldier: "Soldier") -> str:
    """Pick the right projectile flavour for a ranged unit.

    Ballista fires bolts; everything else with ``ranged=True`` fires
    arrows. Centralised so the two spawn sites (anti-enemy strike,
    anti-building fire-arrow) stay in sync, and so a future
    barbarian_archer / catapult unit can hook in by adding one
    branch here rather than scattering kind-picking logic.
    """
    if getattr(soldier, "unit_id", "") == "ballista":
        return "bolt"
    return "arrow"


# ── Manager ─────────────────────────────────────────────────────────────────
class WalkerManager:
    SPAWN_INTERVAL = 10                  # ticks between citizen spawn attempts
    POP_PER_WALKER = 4
    DELIVERY_INTERVAL = 6                # ticks between delivery dispatch attempts
    MAX_DELIVERY_WALKERS = 12
    # v0.6 combat tuning. Soldiers spawn from barracks/fort, capped per
    # building so a single fort can't drown the map. Raids happen at
    # RAID_TICK_CHANCE per tick once population is large enough.
    SOLDIER_SPAWN_INTERVAL = 25
    MAX_SOLDIERS_PER_GARRISON = 3        # per barracks / fort building
    RAID_INTERVAL_CHECK = 20             # ticks between raid-roll checks

    # v0.29: fireman spawn cadence. Each prefecture spawns at most one
    # fireman walker; the dispatcher tops up missing firemen every
    # FIREMAN_SPAWN_INTERVAL ticks. Faster than soldier spawn (because
    # firefighting is a continuous low-grade duty, not a sortie), slower
    # than delivery (because one walker per prefecture is plenty for the
    # typical small Roman block). Aligning with citizen spawn (10) makes
    # the firefighter pool restock at the same rhythm as the labour pool,
    # which feels right when the player rebuilds a destroyed prefecture.
    FIREMAN_SPAWN_INTERVAL = 30
    MAX_FIREMEN_PER_PREFECTURE = 1

    # v0.19.x goods-flow overlay tuning. A delivery walker deposits
    # FLOW_DEPOSIT on the tile it currently occupies each tick; the
    # full map then decays by multiplying by FLOW_DECAY. At decay 0.85
    # a single deposit fades to ~0.05 in 18 ticks — the player gets
    # ~3-4 seconds of "this road was busy" trace, which is enough to
    # see a bottleneck without smearing the whole map red. Tiles below
    # FLOW_PRUNE_EPS are dropped from the dict to bound memory.
    FLOW_DEPOSIT = 1.0
    FLOW_DECAY = 0.85
    FLOW_PRUNE_EPS = 0.02

    # Producer building → list of consumer building ids that accept the
    # good. The first match in the list that exists on the map wins. This
    # is how the manager knows "where do my olives go?" without a global
    # market clearing system. House delivery is handled separately — see
    # _dispatch_house_delivery.
    DELIVERY_TARGETS: dict[str, tuple[str, ...]] = {
        "olive_farm":       ("olive_press",),
        "vineyard":         ("wine_press",),
        "clay_pit":         ("pottery_workshop",),
        # v0.22: mine → smelter (preferred — refines ore into iron) →
        # falls back to factory / weapon_smith if no smelter is built
        # so that the legacy chain (mine → factory direct) still
        # functions for old saves while authors migrate. Once a
        # smelter exists, the routing prefers it because it appears
        # first in the tuple.
        "mine":             ("smelter", "factory", "weapon_smith"),
        # v0.22: smelter output (iron) flows toward the weapon_smith
        # and factory — both consume iron in their `consumption` dict.
        "smelter":          ("weapon_smith", "factory"),
        # v0.11: lumber_mill (was "workshop") feeds the sawmill first
        # (preferred — converts to planks), and falls back to industries
        # that still consume raw wood (factory, weapon_smith, mine, port,
        # bakery for oven fuel) when no sawmill is available.
        # v0.22: smelter added to the wood-consumer fallback chain;
        # smelters need wood for their furnace.
        "lumber_mill":      ("sawmill", "factory", "weapon_smith", "smelter", "bakery"),
        "workshop":         ("sawmill", "factory", "weapon_smith", "smelter", "bakery"),
        # v0.11: wheat → flour → bread chain.
        "farm":             ("windmill",),
        "windmill":         ("bakery",),
        # v0.11: sawmill output flows toward houses via markets — see
        # _dispatch_house_delivery (planks added to market goods list).
        # Bread/wine/oil/pottery/weapons/planks all flow toward houses
        # via markets — handled by _dispatch_house_delivery, not here.
    }

    # Buildings that garrison soldiers. The integer is the per-building cap.
    # v0.23.x: cavalry fort, archery range, and tower (Scout Squadron) added.
    # Each spawns a different unit type, declared in data/buildings.json via
    # the ``spawn_unit`` field.
    GARRISON_BUILDINGS: dict[str, int] = {
        "barracks":              3,   # Light Infantry
        "fort":                  2,   # Heavy Infantry Legionary
        "fort_cavalry":          2,   # Heavy Cavalry
        "archery_range":         3,   # Bowmen
        "tower":                 1,   # Scout Squadron
        # v0.25: military manufacture trains ballistae. Cap is 1 —
        # ballistae are siege-class and expensive; one per building
        # keeps the player from auto-stacking a wall of them. They're
        # also slow, so a single ballista per workshop is meaningful
        # battlefield value rather than chaff.
        "military_manufacture":  1,
    }

    def __init__(
        self,
        registry: BuildingRegistry,
        max_walkers: int = 40,
        textures: TextureRegistry | None = None,
        pathfinder: "Pathfinder | None" = None,
        service_map=None,                   # noqa: ANN001 — circular import
        unit_registry=None,                 # noqa: ANN001 — optional UnitRegistry
    ):
        self.registry = registry
        self.textures = textures
        self.pathfinder = pathfinder  # Optional: deliveries silently no-op without it.
        self.service_map = service_map  # Optional: defence overlay for combat.
        # v0.23.x: optional unit registry. When provided, the dispatcher
        # spawns the unit type declared by the garrison building
        # (barracks → "light_infantry" by default, fort →
        # "heavy_infantry"). When None, we fall back to the legacy v0.22
        # behaviour (every garrison spawns a generic Soldier with
        # COMBAT_SOLDIER_DAMAGE / COMBAT_DEFAULT_HP) so tests that build
        # a WalkerManager without one keep passing unchanged.
        self.unit_registry = unit_registry
        self.walkers: list[Walker] = []
        self.timer = 0
        self.delivery_timer = 0
        self.soldier_timer = 0
        self.raid_timer = 0
        # v0.29: per-tick fireman dispatch cadence. Mirrors soldier_timer
        # — counts ticks since the last attempt to top up prefecture
        # firemen.
        self.fireman_timer = 0
        self.max_walkers = max_walkers
        # Per-tick set of (row, col, good) — house evolution reads this
        # to know what was delivered this tick. Cleared at the start of
        # each manager update.
        self._fresh_deliveries: set[tuple[int, int, str]] = set()
        # Per-tick combat events (attacker_role, defender_role, damage,
        # row, col). Drained by the game window for debug logging /
        # notifications. Cleared at the start of update().
        self._combat_events: list[tuple[str, str, int, int, int]] = []
        # v0.19.x: per-tile goods-flow heatmap. Each tick, every active
        # DeliveryWalker deposits FLOW_DEPOSIT into its current tile;
        # the whole map is then geometrically decayed by FLOW_DECAY.
        # Tiles below FLOW_PRUNE_EPS are dropped so the dict stays
        # bounded (worst case ~the union of every road tile a delivery
        # has touched in the last ~20 ticks at FLOW_DECAY=0.85).
        # Read by the game-window 'Goods flow' overlay.
        self._flow_heat: dict[tuple[int, int], float] = {}
        # v0.33: in-flight projectiles spawned by ranged units in
        # _resolve_combat. Damage is applied at spawn time; the
        # Projectile exists only as a visual streak that ages out
        # after ``Projectile.LIFETIME_TICKS`` ticks. Drawn in
        # WalkerManager.draw after the walker layer so streaks
        # appear *on top of* the units that fired them.
        self.projectiles: list[Projectile] = []

    # ── Properties used by other systems ──────────────────────────────────
    def fresh_deliveries(self) -> set[tuple[int, int, str]]:
        """Snapshot of (row, col, good) tuples delivered in the last tick.

        House evolution reads this to gate tier consumption — a tier-3
        house wants oil and pottery delivered to it, not just produced
        somewhere on the map.
        """
        return self._fresh_deliveries

    def combat_events(self) -> list[tuple[str, str, int, int, int]]:
        """Per-tick combat hits since the last manager update."""
        return self._combat_events

    def flow_heat(self) -> dict[tuple[int, int], float]:
        """v0.19.x: snapshot of the goods-flow heatmap.

        Returns ``{(row, col): intensity}`` where intensity is in
        roughly the [0, ~6] range (a single tile sees ~6 deposits at
        steady state with several walkers passing through). The HUD
        overlay normalises by the per-frame max for display. Returned
        dict is the live one — callers must not mutate it.
        """
        return self._flow_heat

    # ── Counts (combat aware) ────────────────────────────────────────────
    def _soldier_count(self) -> int:
        return sum(1 for w in self.walkers if isinstance(w, Soldier))

    def _enemy_count(self) -> int:
        return sum(1 for w in self.walkers if isinstance(w, Enemy))

    def _soldiers_for_home(self, hr: int, hc: int) -> int:
        return sum(
            1 for w in self.walkers
            if isinstance(w, Soldier) and w.home_row == hr and w.home_col == hc
        )

    # v0.29: fireman counts. Mirrors the soldier-per-home pattern so
    # the dispatcher can refuse to over-spawn a prefecture.
    def _firemen_for_home(self, hr: int, hc: int) -> int:
        return sum(
            1 for w in self.walkers
            if isinstance(w, Fireman) and w.home_row == hr and w.home_col == hc
        )

    def update_movement(
        self, game_map: GameMap, dt_scale: float = 1.0,
    ) -> None:
        """Advance every walker's position. Frame-cadence work only.

        Split out of :meth:`update` (v0.56) so the game window can call
        it once per *rendered frame* for smooth motion, while the heavy
        simulation logic — spawning, dispatch, combat, fire, ships,
        reaping — runs once per *game tick* in :meth:`update`.

        ``dt_scale`` decouples motion from frame rate. Each walker's
        ``speed`` is calibrated as "progress per tick"; multiplying the
        per-frame increment by ``dt_scale = frame_dt / tick_period``
        means a walker covers the same ground per simulated second
        whether the display runs at 30, 60, or 144 fps. Passing the
        default ``1.0`` reproduces the original "one step == one tick"
        behaviour the test suite and headless harnesses expect.

        Pick-target decisions stay here (not in the tick step) because a
        walker that reaches its target mid-frame must choose its next
        one immediately or it stalls visibly until the next tick.
        """
        # Soldiers need world context (enemy positions); everyone else
        # uses their own pick_target.
        enemies = [w for w in self.walkers if isinstance(w, Enemy)]
        for w in self.walkers:
            if isinstance(w, Soldier):
                if w.row == w.target_row and w.col == w.target_col:
                    w.pick_target_with_world(game_map, enemies)
                if w.idle > 0:
                    # Idle is a tick-counter; only decrement it on a full
                    # tick (dt_scale >= 1.0) so a soldier doesn't burn its
                    # idle window faster at high frame rates.
                    w.idle -= 1
                    if w.idle <= 0:
                        w.pick_target_with_world(game_map, enemies)
                else:
                    w.progress += w.speed * dt_scale
                    if w.progress >= 1.0:
                        w.row, w.col = w.target_row, w.target_col
                        w.progress = 0.0
            else:
                w.update(game_map, dt_scale)
            if isinstance(w, DeliveryWalker) and not w.done:
                self._record_delivery_radius(game_map, w)
                # v0.19.x: deposit on the goods-flow heatmap. Done
                # after movement so the deposit lands on the tile the
                # walker now occupies — the visual matches what the
                # player sees on screen.
                key = (w.row, w.col)
                self._flow_heat[key] = (
                    self._flow_heat.get(key, 0.0) + self.FLOW_DEPOSIT
                )

    def update(
        self, game_map: GameMap, economy, move: bool = True,  # noqa: ANN001
    ) -> None:
        """Run one game *tick* of walker simulation.

        ``move`` (v0.56) controls whether this also advances positions.
        The real-time game window drives motion separately, once per
        rendered frame, via :meth:`update_movement`, so it calls this
        with ``move=False`` to avoid double-stepping. Headless harnesses
        and the test suite tick via ``update`` alone and rely on the
        default ``move=True`` for a complete "one call == one tick"
        step, movement included.
        """
        # Reset per-tick scratch.
        self._fresh_deliveries.clear()
        self._combat_events.clear()
        # v0.8: stash so force_raid_punitive (called from Caesar request
        # failure) has a map to spawn enemies into.
        self._last_game_map = game_map

        # 1. Citizen spawn / cull.
        target = min(self.max_walkers, economy.population // self.POP_PER_WALKER)
        self.timer += 1
        if self.timer >= self.SPAWN_INTERVAL and self._citizen_count() < target:
            self.timer = 0
            self._spawn_citizen(game_map)

        # 2. Delivery dispatch.
        self.delivery_timer += 1
        if (
            self.delivery_timer >= self.DELIVERY_INTERVAL
            and self.pathfinder is not None
            and self._delivery_count() < self.MAX_DELIVERY_WALKERS
        ):
            self.delivery_timer = 0
            self._dispatch_delivery(game_map)

        # 3. Soldier dispatch (v0.6) — barracks/fort spawn soldiers up to
        # their per-building cap. Soldiers persist; nothing despawns them
        # except death in combat.
        # v0.22: pass economy so soldier dispatch can consume a weapon
        # from the stockpile per spawn (and spawn unarmed when empty).
        self.soldier_timer += 1
        if self.soldier_timer >= self.SOLDIER_SPAWN_INTERVAL:
            self.soldier_timer = 0
            self._dispatch_soldiers(game_map, economy)

        # 4. Raid roll (v0.6) — barbarians enter from a map edge if the
        # city is large enough and we're below the concurrent cap.
        self.raid_timer += 1
        if self.raid_timer >= self.RAID_INTERVAL_CHECK:
            self.raid_timer = 0
            self._maybe_spawn_raid(game_map, economy)

        # 4b. v0.29: fireman dispatch. Each prefecture tops up to
        # MAX_FIREMEN_PER_PREFECTURE. Firemen patrol the prefecture's
        # service radius and reduce burn on adjacent burning buildings
        # in step 6b below.
        self.fireman_timer += 1
        if self.fireman_timer >= self.FIREMAN_SPAWN_INTERVAL:
            self.fireman_timer = 0
            self._dispatch_firemen(game_map)

        # 5. Per-walker movement. Extracted to ``update_movement`` so the
        # game window can drive it once per *frame* (visual smoothness)
        # independently of the per-*tick* simulation logic in this method.
        # ``move`` lets the real-time window skip it here (it calls
        # update_movement itself each frame); headless/test callers keep
        # the default so ``update`` remains a complete tick step.
        if move:
            self.update_movement(game_map)

        # v0.19.x: decay the goods-flow heatmap after deposits. Multiply
        # every cell by FLOW_DECAY and prune anything below the epsilon
        # so the dict stays small (~50-200 entries in steady state with
        # 12 delivery walkers).
        if self._flow_heat:
            decayed: dict[tuple[int, int], float] = {}
            for k, v in self._flow_heat.items():
                nv = v * self.FLOW_DECAY
                if nv >= self.FLOW_PRUNE_EPS:
                    decayed[k] = nv
            self._flow_heat = decayed

        # 5b. v0.38: naval ship business logic (movement already happened
        # in the loop above via each ship's pick_target on the sea
        # domain). Trade ships credit income + reverse at endpoints;
        # transports disembark their cargo onto the shore.
        self._process_ships(game_map, economy)

        # 5c. v0.39: shipyards build & launch ships onto adjacent water.
        self._process_shipyards(game_map, economy)

        # 6. Combat resolution: any soldier-enemy pair within Chebyshev 1
        # trades damage. Done after movement so the same tick doesn't
        # also apply movement to a corpse.
        # Post-v0.28: enemies also kill adjacent citizens and burn the
        # buildings they're standing on — both need access to game_map
        # (to find footprint anchors / drop buildings) and economy (to
        # decrement population on citizen death).
        self._resolve_combat(game_map=game_map, economy=economy)

        # 6b. v0.29: fireman extinguish sweep. Each Fireman walker
        # scans its Chebyshev-1 neighbourhood for on-fire buildings
        # and reduces their burn level. Done *after* _resolve_combat
        # so that within a single tick, the order is:
        #     1) enemy hits building (sets on_fire, adds burn)
        #     2) fire-propagation tick fires (adds more damage)
        #     3) fireman tick fires (reduces burn)
        # This ordering means a fireman standing next to a freshly-
        # struck building immediately starts containing it the same
        # tick — the player doesn't have to wait a tick for the
        # response to kick in. Combat events are appended so the HUD
        # layer can surface "Fire contained at (r,c)" notifications.
        if game_map is not None:
            firemen = [w for w in self.walkers if isinstance(w, Fireman) and not w.done]
            for fm in firemen:
                results = fm.extinguish_adjacent(game_map)
                for orow, ocol, amt in results:
                    self._combat_events.append(
                        ("fireman", "fire", amt, orow, ocol),
                    )

        # 7. Reap done walkers (HP=0 or finished delivery).
        survivors = [w for w in self.walkers if not w.done]

        # 7b. v0.33: tick in-flight projectiles. Independent of the
        # walker lifecycle — projectiles never engage in combat and
        # never get reaped by the population trim below, they just
        # age out after Projectile.LIFETIME_TICKS. Done after the
        # combat sweep so a projectile spawned this tick gets its
        # first age increment immediately and despawns on schedule.
        for p in self.projectiles:
            p.update()
        self.projectiles = [p for p in self.projectiles if not p.done]

        # Trim excess citizens — population shrunk; combat walkers and
        # deliveries are spared from the cull (they have a job).
        # v0.29: firemen are spared too — they're civic-combat walkers
        # tied to a specific prefecture and pruning one would leave the
        # prefecture undefended against fire until the next dispatch.
        excess = (
            len(survivors) - target - 5
            - self._delivery_count_in(survivors)
            - sum(1 for w in survivors if isinstance(w, (Soldier, Enemy, Fireman)))
        )
        if excess > 0:
            kept: list[Walker] = []
            dropped = 0
            for w in survivors:
                spareable = (
                    not isinstance(w, DeliveryWalker)
                    and not isinstance(w, (Soldier, Enemy, Fireman))
                )
                if dropped < excess and spareable:
                    dropped += 1
                    continue
                kept.append(w)
            survivors = kept

        self.walkers = survivors

    # ── Combat (v0.6) ────────────────────────────────────────────────────
    def _resolve_combat(
        self,
        game_map: GameMap | None = None,
        economy=None,  # noqa: ANN001
    ) -> None:
        """Each adjacent (Chebyshev≤1) soldier-enemy pair trades damage.

        v0.6: defence service buff (forts, towers) multiplies soldier damage.

        v0.22 layers on top of that:

        * Unarmed soldiers deal ``COMBAT_UNARMED_DAMAGE_MULT × damage``
          (default 0.0). The soldier still draws on the map; it just
          can't hurt anyone.
        * Retreating soldiers (low morale OR low HP) hit at
          ``COMBAT_RETREAT_DAMAGE_MULT × damage`` — a fighting retreat,
          not stand-and-die. The enemy's swing connects at full damage
          regardless: a routing line is more vulnerable but not
          completely helpless.
        * When a soldier dies, every other friendly soldier within
          Chebyshev 2 loses ``COMBAT_MORALE_DECAY_ON_LOSS`` morale
          (the "watching your buddy fall" effect). The dying soldier's
          own morale drop happens inside its take_damage override.

        Multipliers stack: a retreating unarmed soldier deals
        ``damage × 0 × 0.5 = 0`` — no surprise there. An armed
        retreating soldier deals ``damage × 1 × 0.5 = 0.5×damage``.
        """
        from balance import (
            COMBAT_DEFENCE_BONUS, COMBAT_UNARMED_DAMAGE_MULT,
            COMBAT_MORALE_DECAY_ON_LOSS, COMBAT_RETREAT_DAMAGE_MULT,
            COMBAT_CAVALRY_SHOCK_MULT,
        )
        from units import RANGED_ATTACK_RANGE
        # v0.38: warships fight alongside soldiers. They're CombatWalkers
        # with the same ranged/armed/category attributes the loop reads,
        # so including them here is all it takes to make them engage
        # enemies (and coastal raiders) in the ranged pass. Land soldiers
        # and warships never collide because enemies path on land and
        # warships on water — they only meet where land meets sea, which
        # is exactly the coastal-defence scenario we want.
        soldiers = [
            w for w in self.walkers
            if isinstance(w, (Soldier, Warship)) and not w.done
        ]
        enemies = [w for w in self.walkers if isinstance(w, Enemy) and not w.done]
        for s in soldiers:
            # v0.23.x: ranged units (bowmen) engage at Chebyshev <=
            # RANGED_ATTACK_RANGE; melee units stay at the legacy
            # Chebyshev <= 1. The enemy can only hit back if it's within
            # melee range — the bowman's stand-off advantage is exactly
            # the gap between the two ranges (a Chebyshev-2 engagement
            # is shoot-but-don't-take-melee).
            engagement_range = (
                RANGED_ATTACK_RANGE if getattr(s, "ranged", False) else 1
            )
            for e in enemies:
                if e.done or s.done:
                    continue
                d = max(abs(s.row - e.row), abs(s.col - e.col))
                if d > engagement_range:
                    continue
                # Compute the soldier's outgoing damage. Four multipliers
                # stack: defence bonus (additive intensity), armed/unarmed,
                # retreat penalty, and the v0.29 cavalry-shock charge bonus
                # (first strike per target only).
                bonus = 0.0
                if self.service_map is not None:
                    bonus = self.service_map.coverage("defence", s.row, s.col)
                armed_mult = 1.0 if s.armed else COMBAT_UNARMED_DAMAGE_MULT
                retreat_mult = (
                    COMBAT_RETREAT_DAMAGE_MULT if s.is_retreating() else 1.0
                )
                # v0.29: cavalry shock. A cavalry soldier striking an
                # enemy it has never hit before connects at SHOCK_MULT ×
                # base damage — modelling the impact of a mounted charge
                # at speed. Subsequent strikes on the same target use the
                # ordinary 1.0 multiplier. Tracked via the per-soldier
                # ``_struck_targets`` set (object ids of Enemy instances)
                # so re-engaging a target after the soldier disengaged
                # doesn't refresh the bonus — a charge happens once per
                # pairing. Note: the bonus is GATED on dmg_s > 0; if the
                # soldier is unarmed (armed_mult=0), we don't burn the
                # charge token, because zero damage isn't a real strike.
                # The token IS burnt on a non-zero swing whether the
                # enemy survives or not.
                is_shock = (
                    getattr(s, "category", "infantry") == "cavalry"
                    and id(e) not in getattr(s, "_struck_targets", set())
                )
                shock_mult = COMBAT_CAVALRY_SHOCK_MULT if is_shock else 1.0
                dmg_s = int(
                    s.damage
                    * (1.0 + COMBAT_DEFENCE_BONUS * bonus)
                    * armed_mult
                    * retreat_mult
                    * shock_mult
                )
                if dmg_s > 0:
                    e.take_damage(dmg_s)
                    self._combat_events.append(
                        ("soldier", "enemy", dmg_s, s.row, s.col),
                    )
                    # v0.33: a ranged unit firing at standoff distance
                    # (d > 1) spawns a Projectile from shooter→target.
                    # We gate on d > 1 so that a bowman shooting an
                    # adjacent enemy (which is effectively a melee
                    # exchange — bowmen *can* engage at d == 1 too)
                    # doesn't paint an arrow on top of the two
                    # silhouettes, which would just clutter the view.
                    # The damage was applied above; the projectile is
                    # cosmetic.
                    if getattr(s, "ranged", False) and d > 1:
                        self.projectiles.append(
                            Projectile(
                                kind=projectile_kind_for(s),
                                from_row=s.row, from_col=s.col,
                                to_row=e.row, to_col=e.col,
                            ),
                        )
                    # v0.29: a non-zero strike burns the per-pair shock
                    # token. Even if this *wasn't* a shock hit (e.g.
                    # infantry, or second cavalry hit on same target),
                    # we record the pairing so the next strike isn't a
                    # shock either. The set is forgiving of duplicates;
                    # `add` is idempotent.
                    if hasattr(s, "_struck_targets"):
                        s._struck_targets.add(id(e))
                    # Emit a separate "shock" event when the bonus
                    # actually fired — lets the HUD layer surface a
                    # "Cavalry charge!" notification distinct from a
                    # routine hit. Damage value is the *bonus portion*
                    # (post-shock minus what a non-shock hit would
                    # have done) so the event log shows how much the
                    # charge added.
                    if is_shock:
                        non_shock_dmg = int(
                            s.damage
                            * (1.0 + COMBAT_DEFENCE_BONUS * bonus)
                            * armed_mult
                            * retreat_mult
                        )
                        bonus_dmg = max(0, dmg_s - non_shock_dmg)
                        self._combat_events.append(
                            ("shock", "enemy", bonus_dmg, s.row, s.col),
                        )
                    if e.done:
                        log.info("Enemy KO'd at (%d,%d) by soldier", e.row, e.col)
                        continue
                else:
                    # Zero-damage swing — record the engagement for
                    # debug log, then proceed to the enemy's hit.
                    self._combat_events.append(
                        ("soldier", "enemy", 0, s.row, s.col),
                    )
                # Enemy hits back at full damage if it's in melee range.
                # v0.23.x: bowmen engage at Chebyshev 2; an enemy 2 tiles
                # away can't reach with a melee swing, so the bowman
                # gets a clean shot. When the soldier IS in melee range
                # (d <= 1), the enemy retaliates as before regardless of
                # ranged/melee.
                if d <= 1:
                    s.take_damage(e.damage)
                    self._combat_events.append(
                        ("enemy", "soldier", e.damage, e.row, e.col),
                    )
                if s.done:
                    log.info("Soldier KO'd at (%d,%d) by enemy", s.row, s.col)
                    # v0.22: nearby allies witness the death and lose
                    # morale. Chebyshev≤2 = the ~5×5 box around the
                    # casualty. We do this once per slain soldier;
                    # the dead soldier's own morale already dropped
                    # inside its take_damage override.
                    for ally in soldiers:
                        if ally is s or ally.done:
                            continue
                        dist = max(
                            abs(ally.row - s.row),
                            abs(ally.col - s.col),
                        )
                        if dist <= 2:
                            ally.lose_morale(COMBAT_MORALE_DECAY_ON_LOSS)
                    break  # this soldier is dead; move to next

        # ── Post-v0.28: enemies kill citizens & burn buildings ──────────
        # Citizens are *any* Walker that isn't a CombatWalker — workers,
        # traders, and random "citizen" wanderers. We don't try to
        # distinguish them combat-wise; from a raider's perspective an
        # unarmed civilian is an unarmed civilian.
        from balance import (
            COMBAT_ENEMY_KILLS_CITIZEN_CHANCE,
            COMBAT_ENEMY_BURN_DAMAGE,
            BUILDING_DEFAULT_HP,
            COMBAT_RANGED_BUILDING_DAMAGE_MULT,
            COMBAT_FIRE_PROPAGATION_DAMAGE,
        )
        # Refresh the enemies list — the soldier-enemy loop above may
        # have killed some, and we don't want corpses swinging at
        # civilians.
        live_enemies = [e for e in enemies if not e.done]
        if live_enemies:
            citizens = [
                w for w in self.walkers
                if not w.done and not isinstance(w, CombatWalker)
            ]
            for e in live_enemies:
                # Citizen-kill pass: Chebyshev ≤ 1, probabilistic.
                for c in citizens:
                    if c.done:
                        continue
                    d = max(abs(e.row - c.row), abs(e.col - c.col))
                    if d > 1:
                        continue
                    if random.random() < COMBAT_ENEMY_KILLS_CITIZEN_CHANCE:
                        c.done = True
                        if economy is not None:
                            # Population can't go negative — defensive
                            # clamp in case of off-by-one between the
                            # walker count and the economy's tally.
                            economy.population = max(0, economy.population - 1)
                        self._combat_events.append(
                            ("enemy", "citizen", 0, c.row, c.col),
                        )
                        log.info(
                            "Citizen killed at (%d,%d) by enemy",
                            c.row, c.col,
                        )
                # Building-burn pass: damage the building under the
                # enemy's feet, if any. HP is lazily allocated into
                # building_state so untouched buildings carry no
                # extra bytes.
                if game_map is None:
                    continue
                rows = getattr(game_map, "rows", None)
                cols = getattr(game_map, "cols", None)
                if rows is None or cols is None:
                    continue
                if not (0 <= e.row < rows and 0 <= e.col < cols):
                    continue
                cell = game_map.grid[e.row][e.col]
                if cell is None:
                    continue
                _bid, orow, ocol = cell
                state = game_map.building_state.setdefault(
                    (orow, ocol), {},
                )
                hp = state.get("hp", BUILDING_DEFAULT_HP)
                hp -= COMBAT_ENEMY_BURN_DAMAGE
                state["hp"] = hp
                # Track *burn level* separately for any rendering layer
                # that wants to show damage stages without recomputing
                # from HP. 0 = pristine, BUILDING_DEFAULT_HP = ash.
                state["burn"] = state.get("burn", 0) + COMBAT_ENEMY_BURN_DAMAGE
                # v0.29: persistent fire flag. Once an enemy has hit a
                # building, the structure is *on fire* — independent of
                # whether the enemy stays adjacent. The fire-propagation
                # pass below (run every tick regardless of enemy
                # presence) keeps damaging on-fire buildings; only a
                # Fireman walker can clear the flag by driving burn
                # back to 0. Existing engineer-post repair is gated
                # against on_fire in decay.py — engineers can't fix
                # burning buildings.
                state["on_fire"] = True
                self._combat_events.append(
                    ("enemy", "building", COMBAT_ENEMY_BURN_DAMAGE, orow, ocol),
                )
                if hp <= 0:
                    log.info(
                        "Building at (%d,%d) destroyed by enemy fire",
                        orow, ocol,
                    )
                    game_map.remove_building(orow, ocol)
                    self._combat_events.append(
                        ("enemy", "building_destroyed", 0, orow, ocol),
                    )

        # ── v0.29: ranged soldiers can damage adjacent enemy-occupied
        # buildings (fire arrows). A bowman / ballista standing within
        # RANGED_ATTACK_RANGE of a building tile that has at least one
        # enemy on it deals COMBAT_RANGED_BUILDING_DAMAGE_MULT × its
        # base damage to that building. The use case is sieging a
        # captured structure — the player can rain fire down on an
        # enemy hiding in a damaged building, lighting it on fire
        # without having to rush a melee unit into a death zone.
        # Melee soldiers skip this pass; only ``ranged=True`` units
        # qualify.
        if (
            game_map is not None
            and COMBAT_RANGED_BUILDING_DAMAGE_MULT > 0.0
        ):
            ranged_soldiers = [
                s for s in soldiers
                if not s.done and getattr(s, "ranged", False) and s.armed
            ]
            # Map of (orow, ocol) -> list of enemies standing on it,
            # so we know which buildings count as "enemy-occupied" this
            # tick. A defending soldier doesn't volley into a friendly
            # market just because it's close.
            enemy_held: dict[tuple[int, int], list[Enemy]] = {}
            for e in [e for e in enemies if not e.done]:
                rows = getattr(game_map, "rows", None)
                cols = getattr(game_map, "cols", None)
                if rows is None or cols is None:
                    continue
                if not (0 <= e.row < rows and 0 <= e.col < cols):
                    continue
                cell = game_map.grid[e.row][e.col]
                if cell is None:
                    continue
                _bid, orow, ocol = cell
                enemy_held.setdefault((orow, ocol), []).append(e)
            for s in ranged_soldiers:
                for (orow, ocol), _occupants in enemy_held.items():
                    # Distance: closest tile of the building's footprint
                    # to the soldier. For 1×1 it's just (orow, ocol);
                    # for larger footprints we'd want the closest tile.
                    # We approximate with the anchor — sufficient for
                    # the 1-tile-Chebyshev gap the bowman has anyway.
                    d = max(abs(s.row - orow), abs(s.col - ocol))
                    if d > RANGED_ATTACK_RANGE:
                        continue
                    state = game_map.building_state.setdefault(
                        (orow, ocol), {},
                    )
                    hp = state.get("hp", BUILDING_DEFAULT_HP)
                    dmg_b = max(
                        1,
                        int(round(s.damage * COMBAT_RANGED_BUILDING_DAMAGE_MULT)),
                    )
                    hp -= dmg_b
                    state["hp"] = hp
                    state["burn"] = state.get("burn", 0) + dmg_b
                    state["on_fire"] = True
                    self._combat_events.append(
                        ("soldier", "building", dmg_b, orow, ocol),
                    )
                    # v0.33: a fire-arrow flying at an enemy-held
                    # building. Same kind-pick as anti-enemy shots
                    # (ballista → bolt, bowman → arrow). Building
                    # anchor (orow, ocol) is the target tile — for
                    # multi-tile buildings the streak terminates at
                    # the footprint's top-left, which is close
                    # enough at typical 1-2 tile range.
                    self.projectiles.append(
                        Projectile(
                            kind=projectile_kind_for(s),
                            from_row=s.row, from_col=s.col,
                            to_row=orow, to_col=ocol,
                        ),
                    )
                    if hp <= 0:
                        log.info(
                            "Building at (%d,%d) destroyed by ranged fire",
                            orow, ocol,
                        )
                        game_map.remove_building(orow, ocol)
                        self._combat_events.append(
                            ("soldier", "building_destroyed", 0, orow, ocol),
                        )
                        # Don't keep volleying at a destroyed building.
                        break

        # ── v0.29: fire propagation. Every on-fire building takes a
        # passive damage tick *regardless* of enemy presence — this is
        # what creates the persistent emergency the new Fireman walker
        # is designed to respond to. Without this pass, the existing
        # v0.28 model meant a fire was just "the enemy's last hit, in
        # arrears" — kill the raider and the threat ended. With this,
        # a raid that touches a building lights a slow-burning crisis
        # that must be actively suppressed.
        #
        # Iterating building_state directly (rather than scanning the
        # whole grid) keeps this O(burning buildings), not O(map).
        if (
            game_map is not None
            and COMBAT_FIRE_PROPAGATION_DAMAGE > 0
        ):
            # Snapshot keys — remove_building can mutate building_state
            # mid-iteration.
            burning = [
                key for key, st in list(game_map.building_state.items())
                if st.get("on_fire", False)
            ]
            for (orow, ocol) in burning:
                state = game_map.building_state.get((orow, ocol))
                if state is None:
                    # Already cleaned up (destruction earlier this tick).
                    continue
                hp = state.get("hp", BUILDING_DEFAULT_HP)
                hp -= COMBAT_FIRE_PROPAGATION_DAMAGE
                state["hp"] = hp
                state["burn"] = state.get("burn", 0) + COMBAT_FIRE_PROPAGATION_DAMAGE
                self._combat_events.append(
                    ("fire", "building", COMBAT_FIRE_PROPAGATION_DAMAGE, orow, ocol),
                )
                if hp <= 0:
                    log.info(
                        "Building at (%d,%d) destroyed by unattended fire",
                        orow, ocol,
                    )
                    if game_map.grid[orow][ocol] is not None:
                        game_map.remove_building(orow, ocol)
                    self._combat_events.append(
                        ("fire", "building_destroyed", 0, orow, ocol),
                    )

        # ── v0.45: sea raids — EnemyShips strike nearby trade ships ──────
        self._resolve_sea_raids()

    def _resolve_sea_raids(self) -> None:
        """Let hostile ships (``EnemyShip``) damage friendly trade ships
        they catch at sea. The main combat pass already handles
        warship↔EnemyShip (both are combatants in the soldier/enemy
        sets), but a ``TradeShip`` is a non-combatant the pass ignores —
        so without this, raiders would sail right through merchant
        traffic. Here an EnemyShip within Chebyshev 1 of a trade ship
        rams it for its damage each tick; a sunk trader is marked done
        and reaped normally.

        Warships are the counter: keep an EnemyShip out of merchant lanes
        and it never reaches a trader. This is deliberately one-directional
        (traders don't fight back — they have no weapons), which is what
        makes a warship escort meaningful."""
        raiders = [w for w in self.walkers
                   if isinstance(w, EnemyShip) and not w.done]
        if not raiders:
            return
        traders = [w for w in self.walkers
                   if isinstance(w, TradeShip) and not w.done]
        if not traders:
            return
        for r in raiders:
            for t in traders:
                if t.done:
                    continue
                d = max(abs(r.row - t.row), abs(r.col - t.col))
                if d <= 1:
                    t.take_damage(r.damage)
                    self._combat_events.append(
                        ("enemy_ship", "trade_ship", r.damage, r.row, r.col),
                    )
                    if t.done:
                        log.info("Trade ship sunk by raider at (%d,%d)",
                                 t.row, t.col)

    # ── Soldier dispatch ─────────────────────────────────────────────────
    def _dispatch_soldiers(self, game_map: GameMap, economy=None) -> None:  # noqa: ANN001
        """Top up garrisons up to their per-building cap.

        v0.22: spawning consumes one ``weapons`` from the economy
        stockpile. If the stockpile is empty, the soldier still spawns
        but with ``armed=False`` — an unarmed soldier shows up on the
        map (so the player visibly sees the consequence of the broken
        chain) but it does no damage in combat.

        ``economy`` is optional so existing tests that call
        ``_dispatch_soldiers`` directly without an economy still work
        — they get an armed soldier as the legacy behaviour. Production
        code passes the live economy and pays the weapon cost.

        v0.23.x: the building's ``spawn_unit`` field selects which unit
        type to recruit (e.g. ``"heavy_infantry"`` for a fort,
        ``"scout"`` for a watchtower). Looked up in
        ``self.unit_registry`` (an instance of ``UnitRegistry``) — when
        the registry isn't wired (legacy tests) we fall back to the
        v0.22 single-stats path using the building's
        ``COMBAT_DEFAULT_HP`` / ``COMBAT_SOLDIER_DAMAGE`` constants.
        """
        from balance import COMBAT_DEFAULT_HP, COMBAT_SOLDIER_DAMAGE
        for bt, orow, ocol in game_map.get_building_positions():
            cap = self.GARRISON_BUILDINGS.get(bt)
            if cap is None:
                continue
            # Spawn point: centre of the footprint.
            bd = self.registry[bt]
            sr = orow + bd.height // 2
            sc = ocol + bd.width // 2
            if self._soldiers_for_home(sr, sc) >= cap:
                continue
            # v0.22: try to draw a weapon from the stockpile. If economy
            # is None (legacy test calls), assume armed; otherwise check
            # and decrement.
            armed = True
            if economy is not None:
                wstock = economy.resources.get("weapons", 0)
                if wstock >= 1:
                    economy.resources["weapons"] = wstock - 1
                    armed = True
                else:
                    armed = False
            # v0.23.x: pick the unit type. The building JSON declares
            # `spawn_unit`; when absent, we use sensible defaults
            # (barracks → light infantry, fort → heavy infantry) so old
            # data/buildings.json files keep working.
            unit_id = self._resolve_spawn_unit(bt, bd)
            udef = (
                self.unit_registry.get(unit_id)
                if (self.unit_registry is not None and unit_id is not None)
                else None
            )
            if udef is not None:
                soldier = Soldier(
                    sr, sc,
                    hp=udef.hp,
                    damage=udef.damage,
                    textures=self.textures,
                    armed=armed,
                    unit_id=udef.id,
                    sight=udef.sight,
                    patrol_radius=udef.patrol_radius,
                    speed=udef.speed,
                    ranged=udef.ranged,
                    armoured=udef.armoured,
                    category=udef.category,
                )
            else:
                soldier = Soldier(
                    sr, sc,
                    hp=COMBAT_DEFAULT_HP,
                    damage=COMBAT_SOLDIER_DAMAGE,
                    textures=self.textures,
                    armed=armed,
                )
            self.walkers.append(soldier)
            log.info(
                "Soldier spawned at %s (%d,%d) unit=%s armed=%s",
                bt, sr, sc, soldier.unit_id, armed,
            )
            return  # one per dispatch — keeps spawn rate sane

    # ── v0.29: Fireman dispatch ─────────────────────────────────────────
    def _dispatch_firemen(self, game_map: GameMap) -> None:
        """Spawn one Fireman per prefecture, up to MAX_FIREMEN_PER_PREFECTURE.

        Walks the placed-building list, finds every prefecture, counts
        existing firemen homed at that prefecture, and tops up. Skips
        prefectures that are still under construction (the v0.25
        ``under_construction`` flag) so a half-built post doesn't emit
        a walker.

        Why prefecture and not engineer post? Two reasons:
        - The prefecture already provides the ``safety`` service, which
          conceptually covers fires (preventing them, catching arson,
          etc.). Layering active firefighting on the same building
          keeps the civic tab uncluttered.
        - The engineer post is the *maintenance* responder — it repairs
          slow decay. The fireman is the *emergency* responder for
          combat-induced fires. Splitting the role across two
          buildings would push the player toward redundancy in dense
          districts. The player asked specifically for a separate
          fireman *role*, not a separate fire-station building.

        Spawning is silent: no economy cost, no resource consumption.
        The prefecture's existing ``consumption.money`` rate already
        models the ongoing operational cost.
        """
        # Walk every placed prefecture once.
        for bt, orow, ocol in game_map.get_building_positions():
            if bt != "prefecture":
                continue
            # Skip under-construction prefectures (v0.25 construction-
            # time mechanic). State entries default to live=True.
            state = game_map.building_state.get((orow, ocol), {})
            if state.get("under_construction", False):
                continue
            bd = self.registry.get(bt)
            if bd is None:
                continue
            sr = orow + bd.height // 2
            sc = ocol + bd.width // 2
            if self._firemen_for_home(sr, sc) >= self.MAX_FIREMEN_PER_PREFECTURE:
                continue
            fm = Fireman(sr, sc, textures=self.textures)
            self.walkers.append(fm)
            log.info("Fireman spawned at prefecture (%d,%d)", sr, sc)

    @staticmethod
    def _resolve_spawn_unit(bt: str, bd) -> str | None:  # noqa: ANN001
        """Pick the unit_id a garrison building spawns.

        Reads the optional ``spawn_unit`` attribute on the building
        (set from data/buildings.json). Falls back to the historical
        per-building default so old JSON files that pre-date v0.23.x
        still produce a recognisable unit — barracks default to
        light_infantry, fort to heavy_infantry, tower to scout. Returns
        None for any other building (or when the building doesn't
        declare a spawn_unit and isn't in the default table).
        """
        configured = getattr(bd, "spawn_unit", None)
        if configured:
            return str(configured)
        DEFAULTS = {
            "barracks": "light_infantry",
            "fort":     "heavy_infantry",
            "tower":    "scout",
        }
        return DEFAULTS.get(bt)

    # ── Raid spawn ───────────────────────────────────────────────────────
    def _resolve_barbarian_unit(self) -> tuple[str, int, int]:
        """Return ``(unit_id, hp, damage)`` for the canonical raider.

        v0.33: raid-spawn sites used to bake HP/damage in from the
        ``COMBAT_DEFAULT_HP`` / ``COMBAT_ENEMY_DAMAGE`` balance
        constants. We now look up the ``barbarian_infantry`` unit in
        the unit registry first so a JSON tweak to the entry's stats
        (or a future "barbarian_archer" subclass) flows through to
        every raid path without changing the balance file. If the
        registry isn't wired (legacy tests construct a WalkerManager
        without one), or the entry is missing, we fall back to the
        v0.32 constants — preserving the previous behaviour exactly.
        """
        from balance import COMBAT_DEFAULT_HP, COMBAT_ENEMY_DAMAGE
        if self.unit_registry is not None:
            udef = self.unit_registry.get("barbarian_infantry")
            if udef is not None:
                return udef.id, int(udef.hp), int(udef.damage)
        return "barbarian_infantry", int(COMBAT_DEFAULT_HP), int(COMBAT_ENEMY_DAMAGE)

    def _maybe_spawn_raid(self, game_map: GameMap, economy) -> None:  # noqa: ANN001
        from balance import (
            COMBAT_DEFAULT_HP, COMBAT_ENEMY_DAMAGE,
            RAID_TICK_CHANCE, RAID_MIN_POPULATION, RAID_MAX_CONCURRENT,
        )
        if economy.population < RAID_MIN_POPULATION:
            return
        if self._enemy_count() >= RAID_MAX_CONCURRENT:
            return
        if random.random() >= RAID_TICK_CHANCE * self.RAID_INTERVAL_CHECK:
            # Roll combines per-tick chance × ticks-since-last-check.
            return
        # Pick a random spawnable edge tile (no water, no mountains).
        # v0.27: edges come from the live game_map, not module constants.
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        edge_tiles: list[tuple[int, int]] = []
        for c in range(cols):
            if not _impassable(game_map, 0, c):
                edge_tiles.append((0, c))
            if not _impassable(game_map, rows - 1, c):
                edge_tiles.append((rows - 1, c))
        for r in range(rows):
            if not _impassable(game_map, r, 0):
                edge_tiles.append((r, 0))
            if not _impassable(game_map, r, cols - 1):
                edge_tiles.append((r, cols - 1))
        if not edge_tiles:
            return
        sr, sc = random.choice(edge_tiles)
        # Objective: roughly the centroid of the city's housing.
        houses = [
            (r, c) for bt, r, c in game_map.get_building_positions() if bt == "house"
        ]
        if houses:
            obj_r = sum(r for r, _ in houses) // len(houses)
            obj_c = sum(c for _, c in houses) // len(houses)
        else:
            obj_r, obj_c = rows // 2, cols // 2
        # Spawn 1-3 enemies for variety.
        count = random.randint(1, 3)
        # v0.33: read HP/damage from the unit registry's
        # barbarian_infantry entry (falls back to COMBAT_DEFAULT_HP /
        # COMBAT_ENEMY_DAMAGE when the registry isn't wired).
        bunit_id, b_hp, b_dmg = self._resolve_barbarian_unit()
        for _ in range(count):
            if self._enemy_count() >= RAID_MAX_CONCURRENT:
                break
            self.walkers.append(
                Enemy(
                    sr, sc, obj_r, obj_c,
                    hp=b_hp,
                    damage=b_dmg,
                    textures=self.textures,
                    unit_id=bunit_id,
                ),
            )
        log.warning(
            "Raid! %d enemy walker(s) at edge (%d,%d) heading for (%d,%d)",
            count, sr, sc, obj_r, obj_c,
        )

    # Manual testing / debug API.
    def force_raid(self, game_map: GameMap, economy) -> None:  # noqa: ANN001
        """Trigger a raid right now, bypassing the random check.
        Tests and debug hotkeys use this."""
        from balance import COMBAT_DEFAULT_HP, COMBAT_ENEMY_DAMAGE
        if self._enemy_count() >= self.MAX_DELIVERY_WALKERS:
            return
        # Always spawn from row 0 left edge for determinism.
        sr, sc = 0, 0
        # Pick a spawnable tile.
        # v0.27: use live game_map dims.
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        for c in range(cols):
            if not _impassable(game_map, 0, c):
                sc = c
                break
        houses = [
            (r, c) for bt, r, c in game_map.get_building_positions() if bt == "house"
        ]
        if houses:
            obj_r = sum(r for r, _ in houses) // len(houses)
            obj_c = sum(c for _, c in houses) // len(houses)
        else:
            obj_r, obj_c = rows // 2, cols // 2
        # v0.33: registry-driven stats (see _resolve_barbarian_unit).
        bunit_id, b_hp, b_dmg = self._resolve_barbarian_unit()
        self.walkers.append(
            Enemy(
                sr, sc, obj_r, obj_c,
                hp=b_hp,
                damage=b_dmg,
                textures=self.textures,
                unit_id=bunit_id,
            ),
        )
        log.warning("Forced raid: enemy at (%d,%d)", sr, sc)

    # ── v0.8: Caesar's punitive raid ──────────────────────────────────────
    def force_raid_punitive(self, intensity: int) -> None:
        """Caesar dispatches a punishment raid. Intensity ≈ how badly
        the player ignored him; we spawn ``intensity`` enemies on top of
        the regular raid cap, all converging on the city centre.

        Stored game_map / economy refs aren't on this manager (raids run
        via ``update``'s explicit args), so we make do with the last
        snapshot the manager saw via ``_last_game_map`` set in ``update``.
        """
        from balance import COMBAT_DEFAULT_HP, COMBAT_ENEMY_DAMAGE
        gm = getattr(self, "_last_game_map", None)
        if gm is None:
            log.debug("force_raid_punitive called before any update() — skipping")
            return
        # Spawn from a random map edge (no water, no mountains).
        # v0.27: live dims from game_map.
        rows = getattr(gm, "rows", GRID_ROWS)
        cols = getattr(gm, "cols", GRID_COLS)
        edge_tiles: list[tuple[int, int]] = []
        for c in range(cols):
            if not _impassable(gm, 0, c):
                edge_tiles.append((0, c))
            if not _impassable(gm, rows - 1, c):
                edge_tiles.append((rows - 1, c))
        if not edge_tiles:
            return
        sr, sc = random.choice(edge_tiles)
        houses = [
            (r, c) for bt, r, c in gm.get_building_positions() if bt == "house"
        ]
        if houses:
            obj_r = sum(r for r, _ in houses) // len(houses)
            obj_c = sum(c for _, c in houses) // len(houses)
        else:
            obj_r, obj_c = rows // 2, cols // 2
        # v0.33: registry-driven stats.
        bunit_id, b_hp, b_dmg = self._resolve_barbarian_unit()
        for _ in range(max(1, intensity)):
            self.walkers.append(
                Enemy(
                    sr, sc, obj_r, obj_c,
                    hp=b_hp,
                    damage=b_dmg,
                    textures=self.textures,
                    unit_id=bunit_id,
                ),
            )
        log.warning("Caesar's PUNITIVE raid: %d enemies at (%d,%d)", intensity, sr, sc)

    # ── v0.30: Skirmish-mode helpers ─────────────────────────────────────
    def skirmish_pre_staff_garrisons(
        self,
        game_map: "GameMap",
        per_building: int = 10,
    ) -> int:
        """v0.30: Front-load every military building on the map with
        ``per_building`` ready-to-fight soldiers, bypassing the normal
        garrison-spawn rotation and weapon-stockpile gate.

        This is the skirmish-mode entry point: the player picks
        "Skirmish" from the splash, the starter layout drops one of
        each military building, and this method spawns
        ``per_building`` units per building so the city starts the
        fight already manned. Each unit is spawned ``armed=True``
        regardless of the weapons stockpile — the skirmish scenario
        is a *combat sandbox*, not a supply-chain puzzle.

        Each garrison spawns its declared ``spawn_unit`` (barracks →
        light_infantry, fort → heavy_infantry, fort_cavalry →
        heavy_cavalry, archery_range → bowman, tower → scout,
        military_manufacture → ballista). Buildings not in
        ``GARRISON_BUILDINGS`` are skipped.

        Returns the total number of soldiers spawned across all
        garrisons.
        """
        from balance import COMBAT_DEFAULT_HP, COMBAT_SOLDIER_DAMAGE
        if per_building <= 0:
            return 0
        spawned = 0
        for bt, orow, ocol in game_map.get_building_positions():
            if bt not in self.GARRISON_BUILDINGS:
                continue
            bd = self.registry.get(bt)
            if bd is None:
                continue
            sr = orow + bd.height // 2
            sc = ocol + bd.width // 2
            unit_id = self._resolve_spawn_unit(bt, bd)
            udef = (
                self.unit_registry.get(unit_id)
                if (self.unit_registry is not None and unit_id is not None)
                else None
            )
            for _ in range(per_building):
                if udef is not None:
                    soldier = Soldier(
                        sr, sc,
                        hp=udef.hp,
                        damage=udef.damage,
                        textures=self.textures,
                        armed=True,
                        unit_id=udef.id,
                        sight=udef.sight,
                        patrol_radius=udef.patrol_radius,
                        speed=udef.speed,
                        ranged=udef.ranged,
                        armoured=udef.armoured,
                        category=udef.category,
                    )
                else:
                    soldier = Soldier(
                        sr, sc,
                        hp=COMBAT_DEFAULT_HP,
                        damage=COMBAT_SOLDIER_DAMAGE,
                        textures=self.textures,
                        armed=True,
                    )
                self.walkers.append(soldier)
                spawned += 1
        log.warning(
            "Skirmish pre-staff: %d soldier(s) spawned across garrisons",
            spawned,
        )
        return spawned

    def force_invasion_wave(
        self,
        game_map: "GameMap",
        count: int,
    ) -> int:
        """v0.30: spawn ``count`` enemies at a single random map edge.

        Used by the skirmish wave scheduler. Unlike
        ``force_raid`` (which spawns one enemy at a fixed corner) or
        ``_maybe_spawn_raid`` (which spawns 1-3 with random per-tile
        spread), this packs all enemies on one edge tile so they
        arrive as a *wave* — a coherent group the player can engage
        as a unit. The objective is the centroid of placed buildings
        (falling back to map centre if nothing is placed).

        Returns the number actually spawned (clamped to ``count`` or
        0 if no spawnable edge tile exists).
        """
        from balance import COMBAT_DEFAULT_HP, COMBAT_ENEMY_DAMAGE
        if count <= 0:
            return 0
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        edge_tiles: list[tuple[int, int]] = []
        for c in range(cols):
            if not _impassable(game_map, 0, c):
                edge_tiles.append((0, c))
            if not _impassable(game_map, rows - 1, c):
                edge_tiles.append((rows - 1, c))
        for r in range(rows):
            if not _impassable(game_map, r, 0):
                edge_tiles.append((r, 0))
            if not _impassable(game_map, r, cols - 1):
                edge_tiles.append((r, cols - 1))
        if not edge_tiles:
            return 0
        sr, sc = random.choice(edge_tiles)
        # Objective: centroid of all placed buildings (skirmish maps
        # may not have houses, so we widen the centroid query).
        positioned = list(game_map.get_building_positions())
        if positioned:
            obj_r = sum(r for _, r, _ in positioned) // len(positioned)
            obj_c = sum(c for _, _, c in positioned) // len(positioned)
        else:
            obj_r, obj_c = rows // 2, cols // 2
        spawned = 0
        # v0.33: registry-driven stats (matches all other raid paths).
        bunit_id, b_hp, b_dmg = self._resolve_barbarian_unit()
        for _ in range(int(count)):
            self.walkers.append(
                Enemy(
                    sr, sc, obj_r, obj_c,
                    hp=b_hp,
                    damage=b_dmg,
                    textures=self.textures,
                    unit_id=bunit_id,
                ),
            )
            spawned += 1
        log.warning(
            "Skirmish wave: %d barbarian(s) at edge (%d,%d) → (%d,%d)",
            spawned, sr, sc, obj_r, obj_c,
        )
        return spawned

    # ── v0.8: rebels from your own houses ─────────────────────────────────
    def spawn_rebels_from_houses(self, game_map: GameMap, count: int) -> int:
        """Promote starvation pressure into actual rebels.

        Spawns ``count`` Enemy walkers at random house tiles (rebels rise
        from the population, not from the map edge — this is the whole
        point of the mechanic). Returns the number actually spawned, which
        may be less than requested if there aren't enough houses.
        """
        from balance import COMBAT_DEFAULT_HP, COMBAT_ENEMY_DAMAGE
        houses = [
            (r, c) for bt, r, c in game_map.get_building_positions() if bt == "house"
        ]
        if not houses:
            return 0
        # Objective: city centroid (so rebels march on the seat of power
        # rather than rampaging at the edge).
        obj_r = sum(r for r, _ in houses) // len(houses)
        obj_c = sum(c for _, c in houses) // len(houses)
        spawned = 0
        # v0.33: same registry-driven stats as edge raiders. Rebels
        # are mechanically Enemy walkers (same Soldier-vs-Enemy
        # combat resolution) — they just spawn from houses instead of
        # the map edge. Sharing the barbarian_infantry stats is a
        # deliberate simplification; a future split could add a
        # "rebel" unit_id with its own HP/damage if narrative-tuning
        # demands.
        bunit_id, b_hp, b_dmg = self._resolve_barbarian_unit()
        for _ in range(count):
            sr, sc = random.choice(houses)
            self.walkers.append(
                Enemy(
                    sr, sc, obj_r, obj_c,
                    hp=b_hp,
                    damage=b_dmg,
                    textures=self.textures,
                    unit_id=bunit_id,
                ),
            )
            spawned += 1
        log.warning("REBELLION: %d rebel(s) rise from the houses", spawned)
        return spawned

    # ── Citizens (random walk) ────────────────────────────────────────────
    # ── v0.38: Naval ship API ────────────────────────────────────────────
    def spawn_trade_ship(self, home, exit_tile, good="wine"):
        """Launch a trade ship that ferries ``good`` between ``home`` (a
        coastal dock water-tile) and ``exit_tile`` (a map-edge water
        tile). Returns the ship so callers/tests can inspect it."""
        ship = TradeShip(home[0], home[1], home, exit_tile, good=good,
                         textures=self.textures)
        self.walkers.append(ship)
        log.info("Trade ship launched: %s %s->%s", good, home, exit_tile)
        return ship

    def spawn_transport_ship(self, start, landing_goal, cargo):
        """Launch a troop transport from ``start`` (water) toward
        ``landing_goal`` (a water tile next to the target shore),
        carrying ``cargo`` (a list of Soldier instances). The soldiers
        are held off-map until disembark."""
        ship = TransportShip(start[0], start[1], landing_goal,
                             cargo=cargo, textures=self.textures)
        self.walkers.append(ship)
        log.info("Transport launched: %d troops %s->%s",
                 len(cargo), start, landing_goal)
        return ship

    def spawn_warship(self, start, goal=None, faction="roman"):
        ship = Warship(start[0], start[1], textures=self.textures,
                       goal=goal, faction=faction)
        self.walkers.append(ship)
        return ship

    def spawn_enemy_ship(self, start, objective, hp=80, damage=10):
        """Spawn a single hostile EnemyShip at ``start`` (a water tile)
        heading for ``objective`` (a coastal/water target). Returns it."""
        raider = EnemyShip(start[0], start[1],
                           objective_row=objective[0], objective_col=objective[1],
                           hp=hp, damage=damage, textures=self.textures)
        self.walkers.append(raider)
        log.info("Enemy ship spawned at %s → %s", start, objective)
        return raider

    def spawn_naval_raid(self, game_map, count=2, *, objective=None,
                         hp=80, damage=10):  # noqa: ANN001
        """Dispatch a wave of ``count`` EnemyShips from map-edge water
        tiles toward a coastal objective — the naval analogue of a land
        raid. This is what makes v0.45 naval combat actually *fire* in a
        scenario: a trigger calls this, raiders sail in, friendly warships
        must intercept them and they threaten trade ships en route.

        Edge water tiles are found by scanning the map border; the
        objective defaults to the city's first coastal building's nearest
        water (falls back to the map centre). Returns the list of spawned
        raiders (possibly empty if the map has no edge water)."""
        if game_map is None:
            return []
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        # Collect water tiles on the map border.
        edges: list[tuple[int, int]] = []
        for c in range(cols):
            if not _impassable_sea(game_map, 0, c):
                edges.append((0, c))
            if not _impassable_sea(game_map, rows - 1, c):
                edges.append((rows - 1, c))
        for r in range(rows):
            if not _impassable_sea(game_map, r, 0):
                edges.append((r, 0))
            if not _impassable_sea(game_map, r, cols - 1):
                edges.append((r, cols - 1))
        if not edges:
            log.warning("spawn_naval_raid: no edge water — no raiders spawned")
            return []
        if objective is None:
            objective = (rows // 2, cols // 2)
        raiders = []
        for i in range(max(1, count)):
            start = edges[(i * 7) % len(edges)]  # spread across the border
            raiders.append(
                self.spawn_enemy_ship(start, objective, hp=hp, damage=damage)
            )
        log.info("Naval raid: %d raiders inbound toward %s", len(raiders), objective)
        return raiders

    # v0.39: shipyards build & launch ships onto adjacent water.
    # v0.41: launches are now driven by a player-managed build queue
    # (``building_state[(r,c)]["ship_queue"]``) rather than an automatic
    # timer. SHIPYARD_INTERVAL is the per-yard *build time* — how long one
    # queued hull takes to finish once the yard starts on it.
    # v0.42: shortened from 600 → 60 ticks (~30s at 2 tps) for fast dev
    # feedback while the naval systems are still being built out. Bump
    # back up for a real difficulty pass.
    SHIPYARD_INTERVAL = 60  # ticks to build one queued hull (~30s at 2 tps)
    SHIPYARD_MAX_PER_YARD = 2  # don't flood the sea from one yard
    SHIPYARD_QUEUE_MAX = 8  # cap the queue so the UI list stays bounded

    # ── v0.41: shipyard build queue (persisted in building_state) ─────────
    @staticmethod
    def shipyard_queue(game_map, orow: int, ocol: int) -> list[str]:  # noqa: ANN001
        """Return the live build-queue list for the shipyard at
        (orow, ocol) — a list of ship-kind strings, oldest first. The
        list is stored on ``building_state`` so it round-trips through
        save/load with no schema bump (it's just another JSON value in
        the per-building state dict). Returns the *actual* list (not a
        copy) so callers can mutate it in place; an absent queue
        materialises as a fresh empty list attached to the state."""
        if game_map is None:
            return []
        state = game_map.building_state.setdefault((orow, ocol), {})
        q = state.get("ship_queue")
        if not isinstance(q, list):
            q = []
            state["ship_queue"] = q
        return q

    def shipyard_enqueue(self, game_map, orow: int, ocol: int,  # noqa: ANN001
                         kind: str) -> bool:
        """Append ``kind`` to a shipyard's build queue. Returns True if it
        was added, False if the queue is already at SHIPYARD_QUEUE_MAX.
        The kind is trusted to be what the yard builds — the UI only ever
        offers the yard's own ``ship_kind`` — but any non-empty string is
        accepted so a future multi-kind yard works without changes."""
        if not kind:
            return False
        q = self.shipyard_queue(game_map, orow, ocol)
        if len(q) >= self.SHIPYARD_QUEUE_MAX:
            return False
        q.append(str(kind))
        log.info("Shipyard (%d,%d) queued %s (queue now %d)",
                 orow, ocol, kind, len(q))
        return True

    def shipyard_dequeue(self, game_map, orow: int, ocol: int,  # noqa: ANN001
                         index: int) -> bool:
        """Remove the queue entry at ``index`` (the UI's per-row ✕
        button). Out-of-range index is a silent no-op returning False so
        a stale click after the queue drained can't crash."""
        q = self.shipyard_queue(game_map, orow, ocol)
        if 0 <= index < len(q):
            removed = q.pop(index)
            log.info("Shipyard (%d,%d) removed %s from queue",
                     orow, ocol, removed)
            return True
        return False

    def _adjacent_water_tile(self, game_map, orow, ocol, w, h):
        """Return a water tile orthogonally adjacent to a building's
        footprint (orow,ocol size w×h), or None. Where a launched ship
        is dropped."""
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        # Walk the footprint perimeter, checking the outward neighbour.
        for r in range(orow, orow + h):
            for c in range(ocol, ocol + w):
                for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < rows and 0 <= nc < cols:
                        if not _impassable_sea(game_map, nr, nc):
                            return (nr, nc)
        return None

    def _ships_from_yard(self, sr, sc) -> int:
        """Count live ships launched from (sr,sc), tracked via the ship's
        ``home``/``goal`` origin. Cheap: ships are few."""
        n = 0
        for w in self.walkers:
            if isinstance(w, Ship) and not w.done:
                if getattr(w, "_yard_origin", None) == (sr, sc):
                    n += 1
        return n

    # ── v0.45: harbor berths ──────────────────────────────────────────────
    # A harbor (building with ship_slots > 0) shelters idle ships. Berthing
    # is a *computed* view, not new persistent state: each tick we look at
    # which role-matching ships are sitting on water tiles adjacent to the
    # harbor footprint, and count them against the harbor's ship_slots. No
    # save-schema change; nothing to desync.
    @staticmethod
    def _berth_role_matches(harbor_role: str, ship: "Ship") -> bool:
        """A commercial harbor berths trade ships; a military harbor
        berths warships and transports. An unroled harbor berths anything."""
        if harbor_role == "commercial":
            return isinstance(ship, TradeShip)
        if harbor_role == "military":
            return isinstance(ship, (Warship, TransportShip))
        return True

    def _berth_zone(self, game_map, orow, ocol, w, h) -> set[tuple[int, int]]:
        """All water tiles orthogonally adjacent to a harbor's footprint —
        the tiles a ship can sit on to count as 'berthed' there."""
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        zone: set[tuple[int, int]] = set()
        for r in range(orow, orow + h):
            for c in range(ocol, ocol + w):
                for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < rows and 0 <= nc < cols:
                        if not _impassable_sea(game_map, nr, nc):
                            zone.add((nr, nc))
        return zone

    def _ship_is_idle(self, ship: "Ship") -> bool:
        """A ship counts as 'available to berth' when it isn't actively
        sailing a far route — i.e. it has no goal, or it has already
        reached its goal tile. Ships mid-voyage don't occupy a berth."""
        if ship.done:
            return False
        if ship.goal is None:
            return True
        return (ship.row, ship.col) == ship.goal

    def harbor_berth_occupancy(self, game_map) -> dict[tuple[int, int], dict]:  # noqa: ANN001
        """Compute per-harbor berth occupancy for this tick.

        Returns ``{(row, col): {"capacity": int, "occupied": int,
        "ships": [ship, ...]}}`` for every harbor (ship_slots > 0). A ship
        is berthed at a harbor when it is idle (see ``_ship_is_idle``),
        role-matches the harbor, and sits in the harbor's berth zone.
        Occupancy is capped at the harbor's ``ship_slots`` — overflow
        ships are simply not counted as berthed (they loiter in adjacent
        water). Each ship is assigned to at most one harbor (the first in
        building order whose zone it's in), so two harbors sharing a water
        tile don't double-count."""
        if game_map is None:
            return {}
        result: dict[tuple[int, int], dict] = {}
        harbors = []
        for bt, orow, ocol in game_map.get_building_positions():
            bd = self.registry.get(bt)
            if bd is None or bd.ship_slots <= 0:
                continue
            zone = self._berth_zone(game_map, orow, ocol, bd.width, bd.height)
            harbors.append((orow, ocol, bd.ship_slots, bd.port_role, zone))
            result[(orow, ocol)] = {
                "capacity": bd.ship_slots, "occupied": 0, "ships": [],
            }
        if not harbors:
            return result
        # Assign each idle ship to at most one harbor whose zone it's in
        # and whose role it matches and which still has a free slot.
        for w in self.walkers:
            if not isinstance(w, Ship) or not self._ship_is_idle(w):
                continue
            here = (w.row, w.col)
            for orow, ocol, slots, role, zone in harbors:
                if here not in zone:
                    continue
                if not self._berth_role_matches(role, w):
                    continue
                slot = result[(orow, ocol)]
                if slot["occupied"] >= slots:
                    continue
                slot["occupied"] += 1
                slot["ships"].append(w)
                break
        return result


    def _process_shipyards(self, game_map: GameMap, economy) -> None:  # noqa: ANN001
        """Drain each shipyard's player-managed build queue (v0.41).

        A yard with a non-empty ``ship_queue`` advances a per-yard build
        timer each tick; when the timer reaches ``SHIPYARD_INTERVAL`` the
        head-of-queue hull is launched onto an adjacent water tile and
        popped off the queue. A yard with an empty queue does nothing —
        the pre-v0.41 behaviour where every yard auto-launched its
        ``ship_kind`` on a shared timer is gone.

        Each launch is still guarded on construction state, water access,
        the per-yard live-ship cap, and affordability; a blocked launch
        leaves the entry on the queue and the timer full so it retries
        next tick (a no-op, not a crash, and not a lost hull). The build
        timer lives in ``building_state[(r,c)]["ship_build_t"]`` so it
        persists through save/load and is independent per yard."""
        if game_map is None:
            return
        for bt, orow, ocol in game_map.get_building_positions():
            bd = self.registry.get(bt)
            if bd is None or not getattr(bd, "ship_kind", ""):
                continue
            state = game_map.building_state.setdefault((orow, ocol), {})
            # Skip still-constructing yards.
            if state.get("constructing"):
                continue
            queue = self.shipyard_queue(game_map, orow, ocol)
            if not queue:
                # Nothing building — keep the timer reset so a freshly
                # queued hull takes the full build time, not a leftover.
                state["ship_build_t"] = 0
                continue
            # Advance this yard's build timer.
            t = int(state.get("ship_build_t", 0)) + 1
            if t < self.SHIPYARD_INTERVAL:
                state["ship_build_t"] = t
                continue
            # Build complete — try to launch the head of the queue.
            water = self._adjacent_water_tile(
                game_map, orow, ocol, bd.width, bd.height
            )
            if water is None:
                # No water access (shouldn't happen for a placed yard, but
                # be defensive): hold the timer full and retry next tick.
                state["ship_build_t"] = self.SHIPYARD_INTERVAL
                continue
            if self._ships_from_yard(orow, ocol) >= self.SHIPYARD_MAX_PER_YARD:
                # Sea is full of this yard's ships — wait for one to clear.
                state["ship_build_t"] = self.SHIPYARD_INTERVAL
                continue
            kind = queue[0]
            ship = self._launch_ship(game_map, economy, kind, water)
            if ship is not None:
                ship._yard_origin = (orow, ocol)
                queue.pop(0)
                state["ship_build_t"] = 0
                self._combat_events.append(
                    ("ship_launched", kind, 0, water[0], water[1])
                )
                log.info("Shipyard at (%d,%d) launched %s at %s (queue %d left)",
                         orow, ocol, kind, water, len(queue))
            else:
                # Unaffordable / unknown kind — hold and retry next tick.
                state["ship_build_t"] = self.SHIPYARD_INTERVAL

    def _launch_ship(self, game_map, economy, kind: str, water):  # noqa: ANN001
        """Spawn a ship of ``kind`` at ``water``. Factored out of
        ``_process_shipyards`` so the queue-drain path and any future
        direct-launch path share one place that knows how to build each
        ship type. Returns the ship, or None for an unknown kind."""
        if kind == "trade_ship":
            exit_tile = self._nearest_edge_water(game_map, water)
            ship = self.spawn_trade_ship(water, exit_tile or water,
                                         good="wine")
            # v0.40: load the hull from the buffer for its first outbound
            # leg (subsequent legs reload at the home endpoint).
            self._load_trade_ship(ship, economy)
            return ship
        if kind == "warship":
            return self.spawn_warship(water)
        if kind == "transport_ship":
            return self.spawn_transport_ship(water, water, cargo=[])
        log.warning("Shipyard: unknown ship kind %r — skipping", kind)
        return None

    def _nearest_edge_water(self, game_map, frm):
        """Nearest map-edge water tile to ``frm`` — the trade exit. Cheap
        scan of the four borders."""
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        best = None
        best_d = 1e9
        edges = (
            [(0, c) for c in range(cols)]
            + [(rows - 1, c) for c in range(cols)]
            + [(r, 0) for r in range(rows)]
            + [(r, cols - 1) for r in range(rows)]
        )
        for (r, c) in edges:
            if not _impassable_sea(game_map, r, c):
                d = abs(r - frm[0]) + abs(c - frm[1])
                if d < best_d:
                    best_d, best = d, (r, c)
        return best

    def _process_ships(self, game_map: GameMap, economy) -> None:  # noqa: ANN001
        """Per-tick ship business logic. Movement already happened in the
        main loop; here we handle arrivals: trade ships credit income and
        reverse at endpoints; transports disembark cargo onto the shore.
        """
        for w in self.walkers:
            if isinstance(w, TradeShip):
                # v0.52: a voyage-mirroring ship is purely visual — the
                # voyages.py layer owns its economics (cargo debit at
                # launch, gold credit on the trip's completion). It must
                # NOT also credit export income here or reload itself, or
                # the player would be paid twice. It just sails its route;
                # the voyage completion hook turns it home / retires it.
                if getattr(w, "_voyage_ship", False):
                    if w.at_goal():
                        w.flip_route()
                    continue
                if w.at_goal():
                    if w.outbound and economy is not None:
                        # Sold the good abroad. v0.40: income is now
                        # proportional to the cargo the ship actually
                        # loaded from the harbor buffer — an empty buffer
                        # means an empty hull means no phantom income.
                        # The pre-v0.40 flat single-unit credit is kept
                        # as the fallback for a ship that was never
                        # loaded (cargo_qty == 0), so headless callers
                        # and old saves that spawn a ship directly still
                        # behave as before.
                        try:
                            from bartering import STOCK_PRICES
                            price = int(STOCK_PRICES.get(w.good, 10))
                        except Exception:  # noqa: BLE001
                            price = 10
                        units = w.cargo_qty if w.cargo_qty > 0 else 1
                        gain = price * units
                        economy.treasury += gain
                        w.cargo_qty = 0  # hull emptied at the exit
                        # v0.38: combat-events entries are 5-tuples
                        # (attacker, defender, amount, row, col) — the
                        # HUD consumer in game_window unpacks exactly 5.
                        # Naval "events" reuse the shape: the 2nd slot is
                        # a sub-kind the consumer can branch on, never a
                        # 4-tuple (that crashed _game_tick).
                        self._combat_events.append(
                            ("trade_ship_sale", "trade", gain, w.row, w.col)
                        )
                    elif not w.outbound and economy is not None:
                        # Reached home — reload from the harbor buffer for
                        # the next outbound leg.
                        self._load_trade_ship(w, economy)
                    w.flip_route()
            elif isinstance(w, TransportShip):
                if w.ready_to_unload(game_map):
                    shore = w.adjacent_land(game_map)
                    if shore is not None:
                        for soldier in w.cargo:
                            soldier.row, soldier.col = shore
                            soldier.target_row, soldier.target_col = shore
                            soldier.home_row, soldier.home_col = shore
                            self.walkers.append(soldier)
                        landed = len(w.cargo)
                        w.cargo = []
                        w.unloaded = True
                        w.done = True  # transport sails off / despawns
                        self._combat_events.append(
                            ("troops_landed", "naval", landed,
                             shore[0], shore[1])
                        )
                        log.info("Transport disembarked %d troops at %s",
                                 landed, shore)

    def _load_trade_ship(self, ship: "TradeShip", economy) -> None:  # noqa: ANN001
        """Load a trade ship's hull from the goods ledger — the port
        buffer in miniature (v0.40 PoC; v0.44 supply-chain routing).

        The global ``economy.resources`` pool is the buffer the
        commercial port feeds (Storage is a derived per-building view of
        the same pool; v0.42 moved the goods storage onto the port).
        Loading debits up to ``TradeShip.LOAD_AMOUNT`` units of the
        ship's good and records the amount on ``ship.cargo_qty``.

        v0.44: the good is also classified by *store class* — nutrients
        come from a granary, other goods from a warehouse — and the class
        is recorded on ``ship.cargo_store_class`` so the supply chain (and
        the route overlay/inspector) reflect where the cargo flows. This
        mirrors the routing distribute() already applies, via the shared
        ``storage.store_class_for_good`` classifier, so both agree.
        """
        if economy is None:
            return
        resources = getattr(economy, "resources", None)
        if not isinstance(resources, dict):
            ship.cargo_qty = 0
            return
        available = int(resources.get(ship.good, 0))
        take = max(0, min(TradeShip.LOAD_AMOUNT, available))
        if take > 0:
            resources[ship.good] = available - take
        ship.cargo_qty = take
        # v0.44: record which store class this cargo routes through.
        from storage import store_class_for_good
        ship.cargo_store_class = store_class_for_good(ship.good)
        if take > 0:
            log.info("Trade ship loaded %d %s (via %s) from port buffer at %s",
                     take, ship.good, ship.cargo_store_class, ship.home)

    def nearest_store_for_good(self, game_map, frm, good):  # noqa: ANN001
        """Find the nearest storage building of the correct class for
        ``good`` — a granary for nutrients, a warehouse for other goods —
        to a reference tile ``frm``. Returns ``(row, col)`` or ``None``.

        This is the spatial half of port↔store routing: a docked ship's
        cargo flows to/from the appropriate nearby store. Pure read over
        the building list; the *classification* is the shared
        ``store_class_for_good`` so it can't drift from distribute()."""
        if game_map is None:
            return None
        from storage import store_class_for_good
        want = store_class_for_good(good)  # "granary" | "warehouse"
        fr, fc = frm
        best = None
        best_d = 1 << 30
        for bt, orow, ocol in game_map.get_building_positions():
            if bt != want:
                continue
            d = abs(orow - fr) + abs(ocol - fc)
            if d < best_d:
                best_d = d
                best = (orow, ocol)
        return best

    def dispatch_cargo_carrier(self, game_map, port, good, qty):  # noqa: ANN001
        """Send a visible CargoCarrier from ``port`` to the nearest store
        of the good's class (granary for nutrients, warehouse otherwise),
        carrying ``qty`` of ``good``. Returns the carrier, or None if
        there's no matching store. The carrier walks a straight tile path
        (port→store); on arrival the manager (or the caller) credits the
        store. This is the on-screen half of the v0.44 routing.

        The path is a simple straight-ish march via integer interpolation
        — ports and stores are near the coast, and a flavour walker
        doesn't warrant full road A*. If start==dest the carrier is a
        no-op (born done)."""
        if game_map is None:
            return None
        dest = self.nearest_store_for_good(game_map, port, good)
        if dest is None:
            return None
        path = self._straight_tile_path(port, dest)
        carrier = CargoCarrier(good, qty, path, dest_store=dest,
                               textures=self.textures)
        self.walkers.append(carrier)
        log.info("Cargo carrier: %d %s from port %s → store %s",
                 qty, good, port, dest)
        return carrier

    @staticmethod
    def _straight_tile_path(start, end):
        """A contiguous 4-connected tile path from start to end via a
        simple L-walk (all column steps, then all row steps). Good enough
        for a short coastal port→store hop; not a road-aware route."""
        (sr, sc), (er, ec) = start, end
        path = [(sr, sc)]
        c = sc
        while c != ec:
            c += 1 if ec > c else -1
            path.append((sr, c))
        r = sr
        while r != er:
            r += 1 if er > r else -1
            path.append((r, ec))
        return path

    def command_embark(self, soldiers, transport):
        """v0.48: board the given soldiers onto a transport (player
        EMBARK command). Boards only as many as fit the transport's free
        slots; embarked soldiers are removed from the live walker list
        (they're 'inside' the ship now). Returns the number embarked."""
        if transport is None or getattr(transport, "done", False):
            return 0
        if not hasattr(transport, "embark"):
            return 0
        boardable = [s for s in soldiers
                     if isinstance(s, Soldier) and not s.done]
        boarded = transport.embark(boardable)
        for s in boardable[:boarded]:
            s.done = True  # leaves the map; lives on inside transport.cargo
        log.info("Embark: %d soldier(s) boarded transport at (%d,%d)",
                 boarded, transport.row, transport.col)
        return boarded

    def command_disembark(self, transport, game_map):
        """v0.48: order a transport to unload its troops now, if it's
        adjacent to land. Returns the number of soldiers landed (0 if not
        landable or empty). Same shore hand-off as the automatic path."""
        if transport is None or getattr(transport, "done", False):
            return 0
        if not getattr(transport, "cargo", None):
            return 0
        shore = (transport.adjacent_land(game_map)
                 if hasattr(transport, "adjacent_land") else None)
        if shore is None:
            return 0
        landed = 0
        for s in list(transport.cargo):
            s.row, s.col = shore
            s.done = False
            self.walkers.append(s)
            landed += 1
        transport.cargo.clear()
        transport.unloaded = True
        log.info("Disembark: %d soldier(s) landed at %s", landed, shore)
        return landed

    def player_units_in_rect(self, r0, c0, r1, c1):
        """v0.47: return the player-controllable units (Soldiers and
        friendly Ships) whose tile falls inside the inclusive rectangle
        (r0,c0)-(r1,c1). The backend for drag-box selection — the UI
        converts a screen drag to a tile rect and calls this; keeping the
        spatial test here makes it unit-testable without a GL context.

        Player-controllable = friendly combatants the player commands:
        Soldiers and friendly (roman) Ships. Enemies, citizens, and
        delivery/cargo walkers are excluded."""
        lo_r, hi_r = (r0, r1) if r0 <= r1 else (r1, r0)
        lo_c, hi_c = (c0, c1) if c0 <= c1 else (c1, c0)
        out = []
        for w in self.walkers:
            if getattr(w, "done", False):
                continue
            is_soldier = isinstance(w, Soldier)
            is_friendly_ship = (
                isinstance(w, Ship)
                and not isinstance(w, EnemyShip)
                and getattr(w, "faction", "roman") == "roman"
            )
            if not (is_soldier or is_friendly_ship):
                continue
            if lo_r <= w.row <= hi_r and lo_c <= w.col <= hi_c:
                out.append(w)
        return out

    def _spawn_citizen(self, game_map: GameMap) -> None:
        positions = game_map.get_building_positions()
        if not positions:
            return
        bt, orow, ocol = random.choice(positions)
        bd = self.registry.get(bt)
        role = bd.worker_role if bd else "citizen"
        self.walkers.append(Walker(role, orow, ocol, textures=self.textures))

    def _citizen_count(self) -> int:
        return sum(1 for w in self.walkers if not isinstance(w, DeliveryWalker))

    def _delivery_count(self) -> int:
        return sum(1 for w in self.walkers if isinstance(w, DeliveryWalker))

    @staticmethod
    def _delivery_count_in(walkers: list[Walker]) -> int:
        return sum(1 for w in walkers if isinstance(w, DeliveryWalker))

    # ── Delivery dispatch ─────────────────────────────────────────────────
    def _dispatch_delivery(self, game_map: GameMap) -> None:
        """Pick a random producer and try to send a walker carrying its
        good to a matching consumer. Bails silently if no road links
        them — the next tick will try a different producer."""
        producers = [
            (bt, r, c)
            for bt, r, c in game_map.get_building_positions()
            if bt in self.DELIVERY_TARGETS
        ]
        if not producers:
            self._dispatch_house_delivery(game_map)
            return

        random.shuffle(producers)
        for bt, r, c in producers:
            bd = self.registry[bt]
            for consumer_id in self.DELIVERY_TARGETS[bt]:
                target = self._find_consumer(game_map, consumer_id)
                if target is None:
                    continue
                tr, tc = target
                tbd = self.registry[consumer_id]
                # Find a road tile next to the producer to start from.
                start = self._access_tile_for(bd, r, c)
                if start is None:
                    continue
                # Pick the dominant produced good as the walker's cargo.
                good = next(iter(bd.production), "goods") if bd.production else "goods"
                path = self.pathfinder.find_to_building(  # type: ignore[union-attr]
                    start[0], start[1], tr, tc, tbd.width, tbd.height,
                )
                if path is None:
                    continue
                self.walkers.append(
                    DeliveryWalker(good, path, textures=self.textures),
                )
                log.debug(
                    "Dispatched %s walker: %s(%d,%d) → %s(%d,%d), %d steps",
                    good, bt, r, c, consumer_id, tr, tc, len(path),
                )
                return
        # Fall through: no producer→consumer pair routed. Try a market
        # delivery to keep the city visibly busy.
        self._dispatch_house_delivery(game_map)

    def _dispatch_house_delivery(self, game_map: GameMap) -> None:
        """Pick a market or granary and send a walker toward a nearby house.

        Markets carry whichever good the city has surplus of; a granary
        always carries food. The walker's path goes from the building's
        access tile *toward* the closest reachable house.
        """
        if self.pathfinder is None:
            return
        dispatchers: list[tuple[str, tuple[str, ...]]] = [
            # v0.11: planks added so tier-4 houses see plank deliveries.
            # Order matters — first item is the dispatched good per
            # candidate (current implementation; future smart-routing
            # release will pick based on what the destination needs).
            ("market",  ("food", "oil", "wine", "pottery", "planks")),
            ("granary", ("food",)),
        ]
        candidates: list[tuple[str, str, int, int]] = []
        for bid, goods in dispatchers:
            for bt, r, c in game_map.get_building_positions():
                if bt == bid:
                    candidates.append((bid, goods[0], r, c))
        if not candidates:
            return
        random.shuffle(candidates)
        houses = [
            (r, c) for bt, r, c in game_map.get_building_positions() if bt == "house"
        ]
        if not houses:
            return

        for bid, good, r, c in candidates:
            bd = self.registry[bid]
            start = self._access_tile_for(bd, r, c)
            if start is None:
                continue
            # Closest house by Manhattan, then try to route there.
            houses_sorted = sorted(
                houses, key=lambda h: abs(h[0] - r) + abs(h[1] - c),
            )
            for hr, hc in houses_sorted[:5]:
                hbd = self.registry["house"]
                path = self.pathfinder.find_to_building(
                    start[0], start[1], hr, hc, hbd.width, hbd.height,
                )
                if path is not None:
                    self.walkers.append(
                        DeliveryWalker(good, path, textures=self.textures),
                    )
                    log.debug(
                        "Dispatched %s walker (house): %s(%d,%d) → house(%d,%d), %d steps",
                        good, bid, r, c, hr, hc, len(path),
                    )
                    return

    def _find_consumer(
        self, game_map: GameMap, consumer_id: str,
    ) -> tuple[int, int] | None:
        for bt, r, c in game_map.get_building_positions():
            if bt == consumer_id:
                return (r, c)
        return None

    def _access_tile_for(self, bd, orow: int, ocol: int) -> tuple[int, int] | None:
        """A road tile adjacent to this building, or None if isolated."""
        if self.pathfinder is None:
            return None
        rn = self.pathfinder.road_network
        gm = rn.game_map
        rows = getattr(gm, "rows", GRID_ROWS)
        cols = getattr(gm, "cols", GRID_COLS)
        for dr in range(bd.height):
            for dc in range(bd.width):
                rr, cc = orow + dr, ocol + dc
                for nr, nc in (
                    (rr + 1, cc), (rr - 1, cc), (rr, cc + 1), (rr, cc - 1),
                ):
                    if 0 <= nr < rows and 0 <= nc < cols:
                        if rn.is_road(nr, nc):
                            return (nr, nc)
        return None

    # ── Per-tick delivery recording ──────────────────────────────────────
    def _record_delivery_radius(self, game_map: GameMap, w: DeliveryWalker) -> None:
        """Mark every house within DELIVERY_REACH of the walker as 'served'
        with this walker's good for this tick."""
        rows = getattr(game_map, "rows", GRID_ROWS)
        cols = getattr(game_map, "cols", GRID_COLS)
        for r in range(w.row - DELIVERY_REACH, w.row + DELIVERY_REACH + 1):
            for c in range(w.col - DELIVERY_REACH, w.col + DELIVERY_REACH + 1):
                if not (0 <= r < rows and 0 <= c < cols):
                    continue
                cell = game_map.grid[r][c]
                if cell is None:
                    continue
                bid, orow, ocol = cell
                if bid == "house":
                    self._fresh_deliveries.add((orow, ocol, w.good))

    # ── Drawing & lifecycle ──────────────────────────────────────────────
    def draw(self) -> None:
        for w in self.walkers:
            w.draw()
        # v0.33: in-flight projectiles render on top of walkers so a
        # ranged shot's streak is visible over both shooter and target.
        for p in self.projectiles:
            p.draw()

    def clear(self) -> None:
        self.walkers.clear()
        self.timer = 0
        self.delivery_timer = 0
        self.soldier_timer = 0
        self.raid_timer = 0
        self._fresh_deliveries.clear()
        self._combat_events.clear()
        # v0.19.x: heatmap is per-session — drop on map load / new game.
        self._flow_heat.clear()
        # v0.33: drop in-flight projectiles when the map resets.
        self.projectiles.clear()
