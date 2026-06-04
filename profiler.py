"""Lightweight per-frame profiler (added post-v0.28).

Wraps named sections in a context manager, keeps a rolling window of the
last N samples (default 60 — about one second at 60 FPS), and renders a
small HUD overlay sorted by mean millisecond cost. Designed to be
toggled live with Ctrl+P so a player chasing a stutter can see which
section is the offender without restarting the game.

The implementation deliberately avoids any dependency on the game
window or arcade beyond ``arcade.Text`` for the HUD; the timing core
is pure stdlib (``time.perf_counter`` + ``collections.deque``) so it
can be unit-tested headlessly.

Numbers are wall-clock — fine for a single-threaded arcade game, where
the only meaningful contention is between the section and the GPU
present. ``perf_counter`` is monotonic and ~ns-resolution on all
supported platforms.
"""
from __future__ import annotations

import collections
import time
from contextlib import contextmanager
from typing import Iterator


class Profiler:
    """Rolling per-section frame profiler.

    Usage::

        prof = Profiler()
        with prof.section("walkers.update"):
            walker_manager.update(...)
        ...
        prof.draw_hud(x=20, y=400)

    The HUD is intentionally minimal: each section gets one line
    ``name  mean_ms  (max_ms)`` sorted by mean descending so the
    expensive bits float to the top.
    """

    def __init__(self, window_size: int = 60):
        # Bare deques keyed by section name; one deque per section, all
        # capped at the same window size. ``defaultdict`` would be tidier
        # but we want an explicit factory so ``window_size`` is honoured.
        self.window_size: int = window_size
        self.samples: dict[str, collections.deque[float]] = {}
        # Last frame's per-section count, so a section that fires multiple
        # times per frame (rare, but the API allows it) reports total cost,
        # not just the last hit. Reset by ``begin_frame``.
        self._frame_totals: dict[str, float] = {}

    @contextmanager
    def section(self, name: str) -> Iterator[None]:
        """Time the contained block, accumulating into the current frame."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self._frame_totals[name] = self._frame_totals.get(name, 0.0) + dt_ms

    def end_frame(self) -> None:
        """Roll the per-frame totals into the sample windows.

        Call once per frame (after ``on_draw``). Sections that didn't
        fire this frame do NOT push a zero — a section can legitimately
        be inactive (e.g. minimap when hidden) and we don't want its
        rolling mean polluted by inactive frames.
        """
        for name, total in self._frame_totals.items():
            dq = self.samples.get(name)
            if dq is None:
                dq = collections.deque(maxlen=self.window_size)
                self.samples[name] = dq
            dq.append(total)
        self._frame_totals.clear()

    # ── Reporting ────────────────────────────────────────────────────────
    def stats(self) -> list[tuple[str, float, float, int]]:
        """Return ``[(name, mean_ms, max_ms, n_samples), ...]`` sorted by
        mean descending. Sections with no samples are omitted.
        """
        out: list[tuple[str, float, float, int]] = []
        for name, dq in self.samples.items():
            if not dq:
                continue
            mean = sum(dq) / len(dq)
            mx = max(dq)
            out.append((name, mean, mx, len(dq)))
        out.sort(key=lambda r: r[1], reverse=True)
        return out

    def reset(self) -> None:
        """Drop all collected samples. Used by tests; also useful if the
        player toggles the profiler off and back on and wants a fresh
        window (call manually if so — the toggle itself doesn't reset)."""
        self.samples.clear()
        self._frame_totals.clear()

    # ── HUD rendering ─────────────────────────────────────────────────────
    def draw_hud(self, x: float, y: float) -> None:  # pragma: no cover - draw path
        """Render the stats as a small overlay anchored at top-left
        ``(x, y)``. Lazy-imports arcade so the module stays usable from
        headless tests.

        We deliberately allocate Text objects on the fly here. The HUD
        is opt-in and short-lived; the per-frame allocation cost is
        cheaper than the bookkeeping of caching them across resizes.
        """
        import arcade

        stats = self.stats()
        if not stats:
            arcade.Text(
                "profiler: collecting...",
                x, y,
                (220, 220, 220),
                12,
            ).draw()
            return

        # Backing panel sized to the row count.
        pad = 6
        line_h = 16
        width = 280
        height = pad * 2 + line_h * (len(stats) + 1)
        arcade.draw_lrbt_rectangle_filled(
            x - pad, x + width,
            y - height + line_h, y + line_h,
            (10, 10, 10, 200),
        )
        arcade.Text(
            "section          mean ms   max ms",
            x, y, (255, 215, 0), 12,
        ).draw()
        for i, (name, mean, mx, _) in enumerate(stats):
            arcade.Text(
                f"{name:<18} {mean:6.2f}   {mx:6.2f}",
                x, y - (i + 1) * line_h,
                (220, 220, 220),
                11,
                font_name="monospace",
            ).draw()
