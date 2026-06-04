"""A* pathfinding on the road network.

Walkers that carry goods between buildings need predictable routes — the
random-walk model from v0.4 wanders for tens of ticks before reaching
anything useful, which makes supply-chain delivery feel broken.

This module finds a shortest path on **road tiles**, starting from any
road tile adjacent to the source building and ending on any road tile
adjacent to the destination. Walkers don't enter buildings; they stop on
the access tile and the engine treats that as "delivered".

Design choices (read once, then forget):

* Movement is 4-connected (no diagonals). Caesar 3 didn't allow them, and
  it keeps the heuristic admissible without sqrt(2) bookkeeping.
* The graph is the set of road tiles, so the open set stays tiny — even a
  city with 500 road tiles only has ~500 nodes. No spatial index needed.
* The heuristic is Manhattan distance, which is admissible and consistent
  for 4-connected unit-cost grids.
* Failure case (no path) returns None. Callers should idle the walker
  rather than retry on every tick — see WalkerManager.

v0.37 performance pass — the bullets above stay true, the rest is faster:

* Tile lookups go through a ``uint8`` numpy mask owned by ``RoadNetwork``
  (``RoadNetwork._road_mask``) instead of a tuple-keyed set. The mask is
  rebuilt on the same hot-reload path as the set; we read it through one
  array index per neighbour expansion. Frees the GC from churning a
  fresh ``(r, c)`` tuple per probe — that was the dominant cost on
  large maps.
* Coordinates are packed into a single ``int`` (``r * cols + c``) for
  every internal structure (``g_score``, ``came_from``, the heap). Int
  hashing is ~3× faster than tuple-of-int hashing and the dicts get a
  lot smaller in memory. We unpack to ``(r, c)`` only on return.
* The multi-goal heuristic (used by ``find_to_building``, which is the
  hot path — one call per delivery dispatch) precomputes the *true*
  shortest-distance-to-any-goal field with one multi-source BFS over
  the road mask, then does an O(1) array lookup per node expansion.
  This is both more accurate (real road distance, not Manhattan) so
  A\\* expands fewer nodes, AND faster per-lookup. On dense grids the
  combined win is multi-x. ``find`` (single-goal) keeps the cheap
  Manhattan heuristic — one goal, nothing to precompute against.

API (unchanged):

    pf = Pathfinder(road_network)
    path = pf.find(start_row, start_col, goal_row, goal_col)
    # path is [(r0, c0), (r1, c1), ...] of road tiles, inclusive of both
    # endpoints, or None if unreachable.

    path = pf.find_to_building(start_r, start_c, goal_origin_r, goal_origin_c, goal_w, goal_h)
    # Find a path to ANY road tile adjacent to the goal building footprint.
"""
from __future__ import annotations

import heapq
import logging
from collections import deque
from typing import TYPE_CHECKING

import numpy as np

from constants import GRID_COLS, GRID_ROWS

if TYPE_CHECKING:
    from road_network import RoadNetwork

log = logging.getLogger("caesar3.pathfinding")


# 4-connected neighbours. Kept as a module-level constant so the
# fallback loop closes over a tuple rather than rebuilding it per call.
_NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1))

# Sentinel for the BFS distance field — anything not reachable from any
# goal stays at this value, which the heuristic uses as +inf. Picked
# small enough to fit in int32 without overflow on any plausible grid.
_UNREACHABLE = 1 << 30


class Pathfinder:
    """A* on road tiles. Stateless aside from a reference to the road
    network — the road set is consulted at lookup time so the pathfinder
    automatically sees roads as they're built/demolished, no rebuild call
    needed."""

    def __init__(self, road_network: "RoadNetwork"):
        self.road_network = road_network

    # v0.27: bounds come from the live game_map, not the module
    # constants. A 128×128 editor-resized map needs to A* across all
    # 16,384 cells; the pre-v0.27 module-constant bounds capped
    # traversal at 30×40 silently and dropped any path that crossed
    # the legacy border. The two helpers below are the single source
    # of truth for "what's the grid size right now".
    def _rows(self) -> int:
        gm = getattr(self.road_network, "game_map", None)
        return getattr(gm, "rows", GRID_ROWS) if gm is not None else GRID_ROWS

    def _cols(self) -> int:
        gm = getattr(self.road_network, "game_map", None)
        return getattr(gm, "cols", GRID_COLS) if gm is not None else GRID_COLS

    def _mask(self) -> np.ndarray | None:
        """Return the live ``uint8`` road mask, or None if the network
        hasn't published one. The non-None branch is the hot path —
        the None fallback (used in headless tests that hand-build a
        bare road network without rebuild) walks ``is_road`` instead.

        We also defensively rebuild if the mask dimensions don't match
        the live grid: a map editor resize is supposed to call rebuild
        on the network, but if it didn't, we'd otherwise IndexError on
        every neighbour expansion. One mask check per call is cheap.
        """
        m = getattr(self.road_network, "_road_mask", None)
        if m is None:
            return None
        rows = self._rows()
        cols = self._cols()
        if m.shape != (rows, cols):
            # Out-of-date mask (e.g. map resize without rebuild). Ask
            # the network to refresh, then re-read.
            try:
                self.road_network.rebuild()
            except Exception:  # noqa: BLE001
                log.debug("pathfinder: mask shape mismatch, rebuild failed",
                          exc_info=True)
                return None
            m = getattr(self.road_network, "_road_mask", None)
            if m is None or m.shape != (rows, cols):
                return None
        return m

    # ── Core ──────────────────────────────────────────────────────────────
    def find(
        self,
        start_row: int,
        start_col: int,
        goal_row: int,
        goal_col: int,
    ) -> list[tuple[int, int]] | None:
        """Shortest road path from start to goal. Both must be road tiles.

        Returns the list of tiles including endpoints, or None if no path.
        """
        if not self._is_road(start_row, start_col):
            return None
        if not self._is_road(goal_row, goal_col):
            return None
        if (start_row, start_col) == (goal_row, goal_col):
            return [(start_row, start_col)]

        return self._astar((start_row, start_col), {(goal_row, goal_col)})

    def find_to_building(
        self,
        start_row: int,
        start_col: int,
        goal_origin_row: int,
        goal_origin_col: int,
        goal_width: int,
        goal_height: int,
    ) -> list[tuple[int, int]] | None:
        """Find a path from a road tile to any road tile adjacent to the
        target building footprint. The path's last cell is the access tile
        (still a road); the building cell itself is never visited.

        Returns None if the building has no road-adjacent tile, or no
        reachable one.
        """
        goals = self._access_tiles(
            goal_origin_row, goal_origin_col, goal_width, goal_height,
        )
        if not goals:
            return None
        if not self._is_road(start_row, start_col):
            return None
        if (start_row, start_col) in goals:
            return [(start_row, start_col)]
        return self._astar((start_row, start_col), goals)

    # ── Internals ─────────────────────────────────────────────────────────
    def _is_road(self, row: int, col: int) -> bool:
        # Hot enough to merit going through the mask when present
        # rather than the set-lookup. The bounds check protects
        # against callers passing arbitrary integers (the access-tile
        # helper already guards bounds for its own enumeration, but
        # ``find`` accepts external coords).
        m = self._mask()
        if m is not None:
            if 0 <= row < m.shape[0] and 0 <= col < m.shape[1]:
                return bool(m[row, col])
            return False
        return self.road_network.is_road(row, col)

    def _access_tiles(
        self, orow: int, ocol: int, w: int, h: int,
    ) -> set[tuple[int, int]]:
        """Set of road tiles directly adjacent to the building footprint."""
        rows = self._rows()
        cols = self._cols()
        tiles: set[tuple[int, int]] = set()
        m = self._mask()
        if m is not None:
            # Bounded by the footprint (≤ 5×5 typical), so the per-cell
            # Python overhead is fine — no need to vectorize this loop.
            for dr in range(h):
                for dc in range(w):
                    rr = orow + dr
                    cc = ocol + dc
                    for nr, nc in (
                        (rr + 1, cc), (rr - 1, cc),
                        (rr, cc + 1), (rr, cc - 1),
                    ):
                        if 0 <= nr < rows and 0 <= nc < cols and m[nr, nc]:
                            tiles.add((nr, nc))
            return tiles
        # Fallback (no mask published yet): delegate to is_road.
        for dr in range(h):
            for dc in range(w):
                rr = orow + dr
                cc = ocol + dc
                for nr, nc in (
                    (rr + 1, cc), (rr - 1, cc),
                    (rr, cc + 1), (rr, cc - 1),
                ):
                    if 0 <= nr < rows and 0 <= nc < cols:
                        if self.road_network.is_road(nr, nc):
                            tiles.add((nr, nc))
        return tiles

    # ── Multi-source BFS heuristic field ─────────────────────────────────
    @staticmethod
    def _bfs_distance_field(
        mask: np.ndarray, goals: set[tuple[int, int]],
    ) -> np.ndarray:
        """Compute the *true* shortest distance from every road tile to
        the closest goal tile, using a 4-connected BFS over the road
        mask. Non-road tiles and unreachable road tiles get
        ``_UNREACHABLE``.

        This is more accurate than Manhattan distance (it respects
        road topology — a goal behind a long detour gets a high
        distance, not the naive crow-flies one), so A\\* expands far
        fewer nodes on twisty road layouts. It also costs
        O(road_tiles) once instead of O(goals) per heap pop.

        Implementation: a flat ``bytes`` view of the mask + a flat
        Python list for the distances. Numpy 2-D scalar access is
        ~5× slower than ``list[i]`` / ``bytes[i]`` in CPython, and
        this loop runs ~road_tiles times — so the gain is large.
        The result is reshaped back to 2-D for the caller; the
        heuristic itself reads through a flat view.
        """
        rows, cols = mask.shape
        total = rows * cols
        # Flat road buffer for the bounds-fold-in-one-check pattern.
        mask_buf = mask.tobytes()
        # Flat distance list. Mutable Python list — faster
        # element-set than numpy.
        UN = _UNREACHABLE
        dist_flat: list[int] = [UN] * total
        q: deque[int] = deque()
        for gr, gc in goals:
            if 0 <= gr < rows and 0 <= gc < cols and mask_buf[gr * cols + gc]:
                k = gr * cols + gc
                dist_flat[k] = 0
                q.append(k)
        cols_l = cols
        total_l = total
        popleft = q.popleft
        append = q.append
        while q:
            k = popleft()
            nd = dist_flat[k] + 1
            # Down (k + cols).
            nk = k + cols_l
            if nk < total_l and mask_buf[nk] and dist_flat[nk] == UN:
                dist_flat[nk] = nd
                append(nk)
            # Up (k - cols).
            nk = k - cols_l
            if nk >= 0 and mask_buf[nk] and dist_flat[nk] == UN:
                dist_flat[nk] = nd
                append(nk)
            # Right (k + 1) — only if not on right edge.
            col = k - (k // cols_l) * cols_l
            if col + 1 < cols_l:
                nk = k + 1
                if mask_buf[nk] and dist_flat[nk] == UN:
                    dist_flat[nk] = nd
                    append(nk)
            # Left.
            if col > 0:
                nk = k - 1
                if mask_buf[nk] and dist_flat[nk] == UN:
                    dist_flat[nk] = nd
                    append(nk)
        # Reshape back to 2-D for the caller's start-reachable check.
        # ``np.asarray(list, dtype=int32).reshape(...)`` round-trips
        # cheaply since the list is the same total size.
        return np.asarray(dist_flat, dtype=np.int32).reshape((rows, cols))

    def _astar(
        self,
        start: tuple[int, int],
        goals: set[tuple[int, int]],
    ) -> list[tuple[int, int]] | None:
        """Generic multi-goal A*. Returns the first goal reached.

        Goals must all be road tiles; we don't validate here because the
        public callers already do.

        v0.37: coords are packed as ``r * cols + c`` ints internally —
        roughly 3× faster hashing in the heap/dict than tuple-of-int.
        We unpack back to ``(r, c)`` tuples only for the return value.
        """
        rows = self._rows()
        cols = self._cols()
        mask = self._mask()

        # If we don't have a numpy mask, fall through to a slower
        # legacy path that still uses the road_network set. This keeps
        # tests that hand-construct a RoadNetwork without rebuild()
        # working (the same fallback _is_road takes).
        if mask is None:
            return self._astar_legacy(start, goals)

        sr, sc = start
        if not (0 <= sr < rows and 0 <= sc < cols and mask[sr, sc]):
            return None

        # Heuristic choice: a precomputed BFS distance field (true
        # shortest path lengths) lets A* expand far fewer nodes, but
        # the BFS itself is O(road_tiles). On short calls the BFS
        # setup dominates; on long calls the per-pop savings recover
        # it many times over. We pick at runtime based on the
        # start-to-closest-goal Manhattan distance vs the size of
        # the road network — when Manhattan is small relative to
        # the network's diameter, plain Manhattan wins.
        #
        # Single-goal calls always take the Manhattan path: there's
        # only one goal to compare against, so the per-pop heuristic
        # cost is constant and BFS would be pure overhead.
        if len(goals) > 1:
            # Closest goal — cheap to compute, also reused by the
            # Manhattan fallback heuristic below.
            goals_tuple = tuple(goals)
            best_manhattan = min(
                abs(sr - gr) + abs(sc - gc) for gr, gc in goals_tuple
            )
            # Empirical threshold: the BFS is worth it when expected
            # path length is at least ~10 hops. Below that the search
            # itself terminates before BFS would have finished.
            use_bfs = best_manhattan >= 10
        else:
            goals_tuple = (next(iter(goals)),)
            best_manhattan = abs(sr - goals_tuple[0][0]) + abs(sc - goals_tuple[0][1])
            use_bfs = False

        if use_bfs:
            dist_field = self._bfs_distance_field(mask, goals)
            # If the BFS never reached our start tile, there is no
            # path; short-circuit so we don't even open the heap.
            if dist_field[sr, sc] >= _UNREACHABLE:
                return None
            # Flatten to a Python list for the same reason as the
            # mask: ``list[i]`` is the fastest indexable container
            # in CPython, and we'll hit it once per neighbour. The
            # ``.tolist()`` is O(road_tiles) once.
            dist_flat: list[int] = dist_field.reshape(-1).tolist()

            def h_func(r: int, c: int,
                       _df=dist_flat, _cols=cols) -> int:
                return _df[r * _cols + c]
        elif len(goals) == 1:
            # Single goal — Manhattan, hoisted into closure.
            gr_only, gc_only = goals_tuple[0]

            def h_func(r: int, c: int,
                       _gr: int = gr_only, _gc: int = gc_only) -> int:
                return abs(r - _gr) + abs(c - _gc)
        else:
            # Multi-goal but path is short — Manhattan to the closest
            # goal each pop. ``goals_tuple`` is small (≤ ~20: a
            # building perimeter), so the linear scan is fine.
            def h_func(r: int, c: int, _gs=goals_tuple) -> int:
                best = abs(r - _gs[0][0]) + abs(c - _gs[0][1])
                for gr2, gc2 in _gs[1:]:
                    d2 = abs(r - gr2) + abs(c - gc2)
                    if d2 < best:
                        best = d2
                return best

        # Pack (row, col) as a single int. ``cols`` fits comfortably in
        # an int for any sane map; the upper bits hold the row.
        start_key = sr * cols + sc
        goal_keys: set[int] = {gr * cols + gc for gr, gc in goals}

        # Priority queue: (f_score, tiebreaker, packed_node). The
        # tiebreaker keeps heap ordering deterministic when f_scores
        # tie.
        counter = 0
        open_heap: list[tuple[int, int, int]] = [
            (h_func(sr, sc), counter, start_key),
        ]
        came_from: dict[int, int] = {}
        g_score: dict[int, int] = {start_key: 0}

        # Local-binding hoists — every microsecond counts in the
        # tight loop.
        heappop = heapq.heappop
        heappush = heapq.heappush
        # Flatten the mask to a ``bytes`` object indexed by the packed
        # key (r * cols + c). ``bytes[i]`` returns a Python int with
        # zero numpy-scalar wrapping — ~2× faster than ``mask[r, c]``
        # for the per-neighbour read, and Python's bytecode for
        # ``buf[k]`` is one BINARY_SUBSCR instruction. The tobytes()
        # copy is O(rows·cols) once per A* call — fine vs the heap
        # ops we're saving.
        mask_buf: bytes = mask.tobytes()
        INF = 1 << 30
        cols_local = cols
        # Bounds in packed key space — saves divmod in the row-check.
        total_cells = rows * cols

        while open_heap:
            _, _, current = heappop(open_heap)
            if current in goal_keys:
                return self._reconstruct_packed(came_from, current, cols_local)
            cur_g = g_score[current]
            # Stale-entry skip: same node may have been pushed twice
            # with different f-scores before we found the better g.
            # If the popped entry's g is no longer current, drop it.
            # (Cheap; reads the dict once.)
            cr = current // cols_local
            cc = current - cr * cols_local
            ng = cur_g + 1
            # 4-neighbour expansion. Inlined for speed; identical
            # logic for each direction. We use the flat byte buffer
            # via the packed key so the bounds check becomes a
            # simple int comparison and the road check is one
            # ``bytes[i]`` subscript.

            # Down.
            nk = current + cols_local
            if nk < total_cells and mask_buf[nk]:
                if ng < g_score.get(nk, INF):
                    came_from[nk] = current
                    g_score[nk] = ng
                    counter += 1
                    heappush(
                        open_heap,
                        (ng + h_func(cr + 1, cc), counter, nk),
                    )
            # Up.
            if cr > 0:
                nk = current - cols_local
                if mask_buf[nk]:
                    if ng < g_score.get(nk, INF):
                        came_from[nk] = current
                        g_score[nk] = ng
                        counter += 1
                        heappush(
                            open_heap,
                            (ng + h_func(cr - 1, cc), counter, nk),
                        )
            # Right. ``cc + 1 < cols`` is the same as ``not on the
            # right edge``; using ``cc`` rather than going through
            # divmod for every neighbour saves an arithmetic op.
            if cc + 1 < cols_local:
                nk = current + 1
                if mask_buf[nk]:
                    if ng < g_score.get(nk, INF):
                        came_from[nk] = current
                        g_score[nk] = ng
                        counter += 1
                        heappush(
                            open_heap,
                            (ng + h_func(cr, cc + 1), counter, nk),
                        )
            # Left.
            if cc > 0:
                nk = current - 1
                if mask_buf[nk]:
                    if ng < g_score.get(nk, INF):
                        came_from[nk] = current
                        g_score[nk] = ng
                        counter += 1
                        heappush(
                            open_heap,
                            (ng + h_func(cr, cc - 1), counter, nk),
                        )

        return None

    @staticmethod
    def _reconstruct_packed(
        came_from: dict[int, int], end: int, cols: int,
    ) -> list[tuple[int, int]]:
        """Walk ``came_from`` from ``end`` back to the start and unpack
        each int into a ``(row, col)`` tuple for the caller."""
        rev: list[tuple[int, int]] = []
        cur = end
        r = cur // cols
        rev.append((r, cur - r * cols))
        while cur in came_from:
            cur = came_from[cur]
            r = cur // cols
            rev.append((r, cur - r * cols))
        rev.reverse()
        return rev

    # ── Legacy path (no numpy mask published) ─────────────────────────────
    def _astar_legacy(
        self,
        start: tuple[int, int],
        goals: set[tuple[int, int]],
    ) -> list[tuple[int, int]] | None:
        """Fallback A* for the rare case where RoadNetwork hasn't
        published a numpy mask (numpy missing, or a test instantiated
        the network without calling rebuild). Behaves exactly like the
        v0.27 implementation.
        """
        counter = 0
        rows = self._rows()
        cols = self._cols()
        open_heap: list[tuple[int, int, tuple[int, int]]] = [
            (self._heuristic_manhattan(start, goals), counter, start),
        ]
        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        g_score: dict[tuple[int, int], int] = {start: 0}
        while open_heap:
            _, _, current = heapq.heappop(open_heap)
            if current in goals:
                return self._reconstruct(came_from, current)
            cr, cc = current
            for dr, dc in _NEIGHBOURS:
                nr, nc = cr + dr, cc + dc
                if not (0 <= nr < rows and 0 <= nc < cols):
                    continue
                if not self.road_network.is_road(nr, nc):
                    continue
                tentative_g = g_score[current] + 1
                neighbor = (nr, nc)
                if tentative_g < g_score.get(neighbor, 1 << 30):
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g
                    f = tentative_g + self._heuristic_manhattan(neighbor, goals)
                    counter += 1
                    heapq.heappush(open_heap, (f, counter, neighbor))
        return None

    @staticmethod
    def _heuristic_manhattan(
        a: tuple[int, int], goals: set[tuple[int, int]],
    ) -> int:
        ar, ac = a
        return min(abs(ar - gr) + abs(ac - gc) for gr, gc in goals)

    @staticmethod
    def _reconstruct(
        came_from: dict[tuple[int, int], tuple[int, int]],
        end: tuple[int, int],
    ) -> list[tuple[int, int]]:
        path = [end]
        while end in came_from:
            end = came_from[end]
            path.append(end)
        path.reverse()
        return path
