"""Read-only diagnostic / debug panels — the nutrients coverage panel
('N'), the happiness-equation debug overlay ('Z'), and the
ProductionDiagnostics summary panel ('D'). All three are
non-modal floating panels that read live engine state and render
it; they don't mutate anything. Extracted from game_window.py for
clarity (~490 lines)."""
from __future__ import annotations

import logging

import arcade

from constants import (
    COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_RED, COLOR_WHITE,
)

# Note: TOP_BAR_H lives in game_window.py. We deliberately do NOT
# do ``import game_window`` at module top — that would create a
# circular import (game_window imports this mixin). Instead the two
# methods that need it do a local ``import game_window`` inside the
# function body, which is resolved at call time after game_window's
# top-level code has finished.

log = logging.getLogger("caesar3.window")

class DiagnosticPanelsMixin:
    """DiagnosticPanelsMixin — see module docstring."""

    # ══════════════════════════════════════════════════════════════════════
    #  NUTRIENTS PANEL  (v0.35)  — 'N' key
    # ══════════════════════════════════════════════════════════════════════
    def _draw_nutrients_panel(self) -> None:
        """v0.35: 'N' opens a panel listing each of the 10 nutrients
        with: produced this tick, eaten this tick, stock, and coverage
        percentage (eaten/population). Replaces the legacy "food" mental
        model with the actual NUTRIENTS layer — there is no aggregated
        "food" resource in the producer path since v0.16 / v0.34.

        Non-modal (read-only); same pattern as the happiness debug
        overlay. Closes on N again or Esc.
        """
        from constants import NUTRIENTS, NUTRIENT_LABELS
        eco = self.economy
        pop = max(1, eco.population)
        gross = getattr(eco, "gross_production", {}) or {}
        eaten = getattr(eco, "food_eaten_by_good", {}) or {}

        pw, ph = 520, 340
        l = self.width - pw - 20
        # Lazy-import game_window to read the module-level TOP_BAR_H
        # without a module-load-time circular import.
        import game_window as _gw
        t = self.height - _gw.TOP_BAR_H - 12
        r = l + pw
        bot = t - ph

        arcade.draw_lrbt_rectangle_filled(l, r, bot, t, (20, 16, 12, 230))
        arcade.draw_lrbt_rectangle_outline(l, r, bot, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_nutrients"):
            self._txt_nutrients: dict[str, arcade.Text] = {}

        def line(key: str, text: str, x: float, y: float,
                 size: int = 10, color: tuple[int, int, int] = COLOR_WHITE,
                 bold: bool = False) -> None:
            obj = self._txt_nutrients.get(key)
            if obj is None:
                obj = arcade.Text(text, x, y, color, size, bold=bold)
                self._txt_nutrients[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            obj.draw()

        line("title", f"NUTRIENTS — coverage for population {pop}",
             l + 10, t - 22, size=12, color=COLOR_GOLD, bold=True)
        line("hint", "N to close · provided vs needed per nutrient",
             l + 10, t - 38, size=9, color=COLOR_GRAY)

        col_y = t - 58
        line("hdr_name",  "Nutrient",   l + 10,  col_y, size=10,
             color=COLOR_WHITE, bold=True)
        line("hdr_eaten", "Eaten/Need", l + 140, col_y, size=10,
             color=COLOR_WHITE, bold=True)
        line("hdr_prod",  "Produced/t", l + 250, col_y, size=10,
             color=COLOR_WHITE, bold=True)
        line("hdr_stock", "Stock",      l + 350, col_y, size=10,
             color=COLOR_WHITE, bold=True)
        line("hdr_cov",   "Coverage",   l + 420, col_y, size=10,
             color=COLOR_WHITE, bold=True)
        line("sep_hdr", "─" * 76, l + 10, col_y - 12,
             size=10, color=COLOR_GRAY)

        row_y = col_y - 28
        row_h = 16
        produced_total = 0.0
        eaten_total = 0.0
        for good in NUTRIENTS:
            label = NUTRIENT_LABELS.get(good, good.capitalize())
            produced = float(gross.get(good, 0.0))
            ate = float(eaten.get(good, 0.0))
            stock = int(eco.resources.get(good, 0))
            coverage_pct = max(0, min(100, int(100 * ate / pop)))
            produced_total += produced
            eaten_total += ate
            if coverage_pct >= 10:
                row_color = COLOR_GREEN
            elif coverage_pct == 0:
                row_color = COLOR_RED
            else:
                row_color = COLOR_WHITE
            line(f"name_{good}",  label,                 l + 10,
                 row_y, size=10, color=row_color)
            line(f"eaten_{good}", f"{ate:.0f} / {pop}",  l + 140,
                 row_y, size=10, color=row_color)
            line(f"prod_{good}",  f"{produced:.1f}",     l + 250,
                 row_y, size=10, color=row_color)
            line(f"stock_{good}", f"{stock:d}",          l + 350,
                 row_y, size=10, color=row_color)
            line(f"cov_{good}",   f"{coverage_pct:d}%",  l + 420,
                 row_y, size=10, color=row_color, bold=True)
            row_y -= row_h

        line("sep_tot", "─" * 76, l + 10, row_y - 4,
             size=10, color=COLOR_GRAY)
        diversity = getattr(eco, "nutrient_diversity", 0)
        full = self.economy.balance.nutrient_diversity_full_count
        line("totals",
             f"  Diversity: {diversity}/{full}   "
             f"Total fed/tick: {eaten_total:.0f}/{pop}   "
             f"Total produced/tick: {produced_total:.1f}",
             l + 10, row_y - 20, size=10, color=COLOR_GOLD, bold=True)
        line("explainer",
             "Coverage = nutrient eaten this tick / population. "
             "Stock = end-of-tick reserve.",
             l + 10, row_y - 38, size=9, color=COLOR_GRAY)


    # ══════════════════════════════════════════════════════════════════════
    #  FINANCE BUDGET PANEL  (v0.51)  — '$' key
    # ══════════════════════════════════════════════════════════════════════
    def _draw_finance_panel(self) -> None:
        """v0.51: '$' opens a read-only finance budget summary.

        Three blocks, all in gold/tick (the simulation's accounting
        unit), plus the live treasury:

          * **Income** — every source that credits the treasury: citizen
            taxes, money-producing buildings (banks, forum, markets…),
            per-tick trade-route profit, and per-trip voyage gold (the
            spike on a returning ship; 0 on a quiet tick).
          * **Upkeep & expenses** — every drain: worker wages, flat
            per-building upkeep, and buildings that consume money.
          * **Balance** — income − expenses for the last tick, the net
            per-tick swing, coloured green (surplus) or red (deficit),
            and the current treasury.

        Reads ``economy.finance_breakdown`` (stamped each tick by the
        economy, with the trade-route / voyage lines injected by the
        game window after the tick) plus the voyage manager's lifetime
        tally. Non-modal / read-only; closes on '$' again or Esc.
        """
        eco = self.economy
        fb = getattr(eco, "finance_breakdown", {}) or {}

        # Income lines (label, key).
        income_lines = [
            ("Taxes (citizens)",        fb.get("tax", 0.0)),
            ("Production (buildings)",   fb.get("production", 0.0)),
            ("Trade routes (per tick)",  fb.get("trade_routes", 0.0)),
            ("Voyages (per trip)",       fb.get("voyages", 0.0)),
        ]
        # Expense / upkeep lines.
        expense_lines = [
            ("Wages (workers)",          fb.get("wages", 0.0)),
            ("Upkeep (buildings)",       fb.get("upkeep", 0.0)),
            ("Consumption (money)",      fb.get("consumption", 0.0)),
        ]
        income_total = sum(v for _, v in income_lines)
        expense_total = sum(v for _, v in expense_lines)
        balance = income_total - expense_total

        # Centred panel.
        pw, ph = 520, 430
        cx, cy = self.width / 2, self.height / 2
        l, r = cx - pw / 2, cx + pw / 2
        bot, t = cy - ph / 2, cy + ph / 2

        arcade.draw_lrbt_rectangle_filled(0, self.width, 0, self.height, (0, 0, 0, 150))
        arcade.draw_lrbt_rectangle_filled(l, r, bot, t, (22, 18, 13, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, bot, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_finance"):
            self._txt_finance: dict[str, arcade.Text] = {}

        def line(key: str, text: str, x: float, y: float,
                 size: int = 11, color: tuple[int, int, int] = COLOR_WHITE,
                 bold: bool = False, anchor_x: str = "left") -> None:
            obj = self._txt_finance.get(key)
            if obj is None:
                obj = arcade.Text(text, x, y, color, size, bold=bold,
                                  anchor_x=anchor_x)
                self._txt_finance[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            obj.draw()

        line("title", "FINANCE — city budget (gold per tick)",
             cx, t - 24, size=14, color=COLOR_GOLD, bold=True,
             anchor_x="center")
        line("hint", "$ or Esc to close",
             cx, t - 42, size=9, color=COLOR_GRAY, anchor_x="center")

        val_x = r - 30  # right-anchored value column
        y = t - 70

        # ── Income block ───────────────────────────────────────────
        line("inc_hdr", "INCOME", l + 24, y, size=12,
             color=COLOR_GREEN, bold=True)
        y -= 20
        for i, (label, val) in enumerate(income_lines):
            col = COLOR_WHITE if val else COLOR_GRAY
            line(f"inc_l_{i}", label, l + 40, y, size=10, color=col)
            line(f"inc_v_{i}", f"+{val:.0f}", val_x, y, size=10,
                 color=COLOR_GREEN if val else COLOR_GRAY, anchor_x="right")
            y -= 17
        line("inc_sep", "─" * 70, l + 24, y + 2, size=9, color=COLOR_GRAY)
        y -= 14
        line("inc_tot_l", "Total income", l + 40, y, size=11,
             color=COLOR_WHITE, bold=True)
        line("inc_tot_v", f"+{income_total:.0f}", val_x, y, size=11,
             color=COLOR_GREEN, bold=True, anchor_x="right")
        y -= 28

        # ── Upkeep & expenses block ────────────────────────────────
        line("exp_hdr", "UPKEEP & EXPENSES", l + 24, y, size=12,
             color=COLOR_RED, bold=True)
        y -= 20
        for i, (label, val) in enumerate(expense_lines):
            col = COLOR_WHITE if val else COLOR_GRAY
            line(f"exp_l_{i}", label, l + 40, y, size=10, color=col)
            line(f"exp_v_{i}", f"-{val:.0f}", val_x, y, size=10,
                 color=COLOR_RED if val else COLOR_GRAY, anchor_x="right")
            y -= 17
        line("exp_sep", "─" * 70, l + 24, y + 2, size=9, color=COLOR_GRAY)
        y -= 14
        line("exp_tot_l", "Total expenses", l + 40, y, size=11,
             color=COLOR_WHITE, bold=True)
        line("exp_tot_v", f"-{expense_total:.0f}", val_x, y, size=11,
             color=COLOR_RED, bold=True, anchor_x="right")
        y -= 30

        # ── Balance block ──────────────────────────────────────────
        line("bal_sep", "═" * 70, l + 24, y + 4, size=9, color=COLOR_GOLD)
        y -= 16
        bal_color = COLOR_GREEN if balance >= 0 else COLOR_RED
        sign = "+" if balance >= 0 else ""
        line("bal_l", "NET BALANCE / tick", l + 40, y, size=12,
             color=COLOR_GOLD, bold=True)
        line("bal_v", f"{sign}{balance:.0f}", val_x, y, size=12,
             color=bal_color, bold=True, anchor_x="right")
        y -= 24
        treasury = eco.treasury
        tre_color = COLOR_RED if treasury < 0 else COLOR_GOLD
        line("tre_l", "Treasury (current)", l + 40, y, size=12,
             color=COLOR_WHITE, bold=True)
        line("tre_v", f"{treasury:.0f} dn", val_x, y, size=12,
             color=tre_color, bold=True, anchor_x="right")
        y -= 22

        # Voyage lifetime footnote (the per-trip layer is lumpy, so the
        # per-tick line above is often 0; show the lifetime total too).
        vm = getattr(self, "voyage_manager", None)
        if vm is not None:
            life = int(getattr(vm, "lifetime_gold", 0))
            trips = int(getattr(vm, "lifetime_trips", 0))
            at_sea = vm.active_count()
            line("voy_foot",
                 f"Voyages: {trips} trips lifetime · {life} dn earned · "
                 f"{at_sea} at sea now",
                 l + 24, y, size=9, color=COLOR_GRAY)
            y -= 14
        line("foot",
             "Income/expenses are last-tick figures. "
             "Voyage gold lands on a ship's return.",
             l + 24, y, size=9, color=COLOR_GRAY)


    # ══════════════════════════════════════════════════════════════════════
    #  COMMERCE-SHIPS PANEL  (v0.52)  — '!' key
    # ══════════════════════════════════════════════════════════════════════
    def _draw_commerce_ships_panel(self) -> None:
        """v0.52: '!' opens a read-only summary of the city's commercial
        fleet — the brief's "busy commerce ships and free ships" view.

        Two blocks:

          * **Busy (at sea)** — one row per in-flight voyage: the cargo
            (qty + good), the destination city, the locked-in payout, and
            how many ticks until it returns. These are the ships tied up
            carrying goods to a foreign market.
          * **Free** — the commercial-harbour berths NOT currently out on
            a voyage, i.e. ships ready to be dispatched. This is exactly
            the count the voyage layer gates departures on.

        Reads the live ``voyage_manager`` (in-flight voyages, lifetime
        tallies) and the ``_voyage_world`` adapter (free-ship count from
        commercial-harbour berths). Non-modal / read-only; closes on '!'
        again or Esc. Mirrors the finance panel's centred layout.
        """
        vm = getattr(self, "voyage_manager", None)
        active = list(getattr(vm, "active_voyages", []) or []) if vm else []
        try:
            free = self._voyage_world().ships_available() if vm else 0
        except Exception:  # noqa: BLE001 - no map yet etc.
            free = 0
        busy = len(active)
        total = busy + max(0, free)
        cur_tick = int(getattr(vm, "tick", 0)) if vm else 0

        from commercial_roads import city_name

        pw, ph = 560, 460
        cx, cy = self.width / 2, self.height / 2
        l, r = cx - pw / 2, cx + pw / 2
        bot, t = cy - ph / 2, cy + ph / 2

        arcade.draw_lrbt_rectangle_filled(0, self.width, 0, self.height, (0, 0, 0, 150))
        arcade.draw_lrbt_rectangle_filled(l, r, bot, t, (22, 18, 13, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, bot, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_commerce_ships"):
            self._txt_commerce_ships: dict[str, arcade.Text] = {}

        def line(key: str, text: str, x: float, y: float,
                 size: int = 11, color: tuple[int, int, int] = COLOR_WHITE,
                 bold: bool = False, anchor_x: str = "left") -> None:
            obj = self._txt_commerce_ships.get(key)
            if obj is None:
                obj = arcade.Text(text, x, y, color, size, bold=bold,
                                  anchor_x=anchor_x)
                self._txt_commerce_ships[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            obj.draw()

        line("title", "COMMERCE SHIPS — fleet status",
             cx, t - 24, size=14, color=COLOR_GOLD, bold=True,
             anchor_x="center")
        line("hint", "! or Esc to close",
             cx, t - 42, size=9, color=COLOR_GRAY, anchor_x="center")

        # Headline tallies.
        y = t - 70
        line("tally",
             f"Fleet: {total} commercial berth(s)   ·   "
             f"BUSY {busy} at sea   ·   FREE {max(0, free)} ready",
             l + 24, y, size=12, color=COLOR_WHITE, bold=True)
        y -= 26

        # ── Busy (at sea) block ────────────────────────────────────
        line("busy_hdr", f"BUSY — ships carrying goods ({busy})",
             l + 24, y, size=12, color=COLOR_GOLD, bold=True)
        y -= 20
        if not active:
            line("busy_none", "  (no voyages at sea — all ships are free)",
                 l + 40, y, size=10, color=COLOR_GRAY)
            y -= 18
        else:
            # Column headers.
            line("busy_ch1", "Cargo", l + 40, y, size=9,
                 color=COLOR_GRAY, bold=True)
            line("busy_ch2", "Destination", l + 210, y, size=9,
                 color=COLOR_GRAY, bold=True)
            line("busy_ch3", "Payout", l + 360, y, size=9,
                 color=COLOR_GRAY, bold=True, anchor_x="right")
            line("busy_ch4", "Returns in", r - 30, y, size=9,
                 color=COLOR_GRAY, bold=True, anchor_x="right")
            y -= 16
            # Soonest-to-return first so the most imminent free ship is
            # at the top — that's the actionable info.
            for i, v in enumerate(sorted(active,
                                         key=lambda vv: vv.arrive_tick)):
                eta = max(0, int(v.arrive_tick) - cur_tick)
                line(f"busy_c_{i}", f"{int(v.qty)} {v.good}",
                     l + 40, y, size=10, color=COLOR_WHITE)
                line(f"busy_d_{i}", city_name(v.city_id),
                     l + 210, y, size=10, color=COLOR_WHITE)
                line(f"busy_p_{i}", f"+{int(v.gold)} dn",
                     l + 360, y, size=10, color=COLOR_GREEN, anchor_x="right")
                line(f"busy_e_{i}", f"~{eta}t",
                     r - 30, y, size=10, color=COLOR_GOLD, anchor_x="right")
                y -= 16
                if y < bot + 110:  # keep room for the free block + footer
                    line("busy_more",
                         f"  … and {len(active) - i - 1} more at sea",
                         l + 40, y, size=9, color=COLOR_GRAY)
                    y -= 16
                    break
        y -= 10

        # ── Free block ─────────────────────────────────────────────
        line("free_sep", "─" * 78, l + 24, y + 4, size=9, color=COLOR_GRAY)
        y -= 16
        free_color = COLOR_GREEN if free > 0 else COLOR_GRAY
        line("free_hdr", f"FREE — berths ready to dispatch ({max(0, free)})",
             l + 24, y, size=12, color=free_color, bold=True)
        y -= 20
        if free > 0:
            line("free_msg",
                 "  These ships can sail now if a linked city has stock "
                 "and the seas are clear.",
                 l + 40, y, size=10, color=COLOR_WHITE)
        else:
            line("free_msg",
                 "  Every commercial ship is out on a voyage — none free "
                 "to dispatch right now.",
                 l + 40, y, size=10, color=COLOR_GRAY)
        y -= 26

        # ── Lifetime footnote ──────────────────────────────────────
        if vm is not None:
            life = int(getattr(vm, "lifetime_gold", 0))
            trips = int(getattr(vm, "lifetime_trips", 0))
            line("foot_life", "═" * 78, l + 24, y + 4, size=9,
                 color=COLOR_GOLD)
            y -= 16
            line("foot_tally",
                 f"Lifetime: {trips} trip(s) completed · {life} dn earned",
                 l + 24, y, size=10, color=COLOR_GOLD, bold=True)
            y -= 18
        line("foot",
             "Build commercial harbours (berths) for more ships. "
             "Set a good per city via 'R' → Ship by trip.",
             l + 24, y, size=9, color=COLOR_GRAY)



        """v0.21: 'Z' key — small floating panel on the left side that
        prints the current happiness equation arguments and result.

        Reads ``economy.last_happiness_breakdown``: a dict the economy
        stamps each tick with the per-term values used to compute the
        target (``base``, ``tax_pen``, ``food_bon``, ``house_bon``,
        ``emp_bon``, ``water_pen``, ``diversity_bon``, ``h_prod``)
        plus the inputs they were derived from (``tax_rate``,
        ``food_eaten``, ``food_needed``, ``housing_capacity``,
        ``population``, ``employment_eff``, ``dry_house_fraction``,
        ``nutrient_diversity``).

        Intentionally NON-modal: drawn on top of the world but does
        not consume clicks. The player can keep placing buildings,
        switching tax rates, etc., and watch the panel update tick
        by tick.
        """
        b = getattr(self.economy, "last_happiness_breakdown", {}) or {}
        # v0.35: widened 360 -> 460 px and bumped height 360 -> 400 to
        # accommodate the longer human-readable term names introduced
        # below. The previous layout truncated "housing/pop = 365 / 735"
        # off the right edge — see happiness.png.
        pw, ph = 460, 400
        l = 12
        import game_window as _gw
        t = self.height - _gw.TOP_BAR_H - 12
        r = l + pw
        bot = t - ph

        arcade.draw_lrbt_rectangle_filled(l, r, bot, t, (20, 16, 12, 230))
        arcade.draw_lrbt_rectangle_outline(l, r, bot, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_happydbg"):
            self._txt_happydbg: dict[str, arcade.Text] = {}

        def line(key: str, text: str, x: float, y: float,
                 size: int = 10, color: tuple[int, int, int] = COLOR_WHITE,
                 bold: bool = False) -> None:
            obj = self._txt_happydbg.get(key)
            if obj is None:
                obj = arcade.Text(text, x, y, color, size, bold=bold)
                self._txt_happydbg[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            obj.draw()

        line("title", "HAPPINESS DEBUG (Z to close)",
             l + 10, t - 22, size=12, color=COLOR_GOLD, bold=True)

        if not b:
            line("nodata",
                 "No data yet — wait for the first game tick.",
                 l + 10, t - 50, size=10, color=COLOR_GRAY)
            return

        # v0.35: equation header reads in plain English. The old
        # "target = base - tax_pen + food_bon + house_bon + emp_bon +
        # water_pen + diversity_bon + h_prod" was three lines of
        # abbreviations that didn't match any other in-game label.
        eq_y = t - 50
        line("eq_hdr",
             "Target = Base + bonuses - penalties (each shown below)",
             l + 10, eq_y, size=10, color=COLOR_GRAY)

        # v0.35: ("internal_key", "Display label") pairs. The internal
        # keys must match the dict the economy stamps each tick (see
        # _compute_happiness_target_with_breakdown); display labels are
        # the human-readable strings the player sees.
        terms = [
            ("happiness_base",  "Base happiness",     COLOR_WHITE),
            ("tax_pen",         "Tax penalty",        COLOR_RED),
            ("food_bon",        "Food bonus",
             COLOR_GREEN if b.get("food_bon", 0.0) >= 0 else COLOR_RED),
            ("house_bon",       "Housing bonus",
             COLOR_GREEN if b.get("house_bon", 0.0) >= 0 else COLOR_RED),
            ("emp_bon",         "Employment bonus",
             COLOR_GREEN if b.get("emp_bon", 0.0) >= 0 else COLOR_RED),
            ("water_pen",       "Water penalty",
             COLOR_GREEN if b.get("water_pen", 0.0) >= 0 else COLOR_RED),
            ("diversity_bon",   "Nutrient diversity",  COLOR_GREEN),
            ("h_prod",          "Service bonus",       COLOR_GREEN),
        ]
        ty = eq_y - 24
        for i, (key, display, color) in enumerate(terms):
            val = b.get(key, 0.0)
            # v0.35: 20-char label column + right-aligned value.
            line(f"term_{key}",
                 f"  {display:<20s} = {val:+7.2f}",
                 l + 10, ty - i * 14, size=10, color=color)
        ty_after = ty - len(terms) * 14

        raw = b.get("raw_sum", 0.0)
        target = b.get("target", 0.0)
        current = b.get("happiness_current", 0.0)
        smoothing = b.get("smoothing", 0.0)
        next_h = current + (target - current) * smoothing

        line("sep1", "─" * 48, l + 10, ty_after - 4, size=10, color=COLOR_GRAY)
        line("raw",
             f"  {'Raw sum':<20s} = {raw:+7.2f}",
             l + 10, ty_after - 18, size=10, color=COLOR_WHITE)
        line("target",
             f"  {'Target':<20s} = {target:7.2f}  (clamp 0..100)",
             l + 10, ty_after - 32, size=10, color=COLOR_GOLD, bold=True)
        line("smoothing",
             f"  {'Smoothing':<20s} = {smoothing:.3f}",
             l + 10, ty_after - 46, size=10, color=COLOR_GRAY)
        line("current",
             f"  {'Current happiness':<20s} = {current:7.2f}",
             l + 10, ty_after - 60, size=10, color=COLOR_WHITE)
        line("next",
             f"  {'Next tick (approx)':<20s} = {next_h:7.2f}",
             l + 10, ty_after - 74, size=10, color=COLOR_GRAY)

        inp_y = ty_after - 96
        line("inp_hdr", "INPUTS",
             l + 10, inp_y, size=10, color=COLOR_GOLD, bold=True)
        eff = b.get("employment_eff", 0.0)
        thr = b.get("employment_threshold", 0.0)
        eff_color = COLOR_GREEN if eff >= thr else COLOR_RED
        # v0.35: full-word inputs labels — "Tax rate" not "tax_rate",
        # "Food eaten / needed" not "food_eaten/needed".
        inputs = [
            (f"Tax rate = {b.get('tax_rate', 0.0)*100:.0f}%", COLOR_WHITE),
            (
                f"Food eaten / needed = {b.get('food_eaten',0):.0f}"
                f" / {b.get('food_needed',0):.0f}",
                COLOR_GREEN if b.get('food_eaten',0) >= b.get('food_needed',0)
                else COLOR_RED,
            ),
            (
                f"Housing / population = {b.get('housing_capacity',0):.0f}"
                f" / {b.get('population',0):.0f}",
                COLOR_GREEN if b.get('housing_capacity',0) >= b.get('population',0)
                else COLOR_RED,
            ),
            (f"Employment efficiency = {eff:.2f}  (>= {thr:.2f}?)", eff_color),
            (f"Dry-house fraction = {b.get('dry_house_fraction',0.0):.2f}",
             COLOR_WHITE),
            (
                f"Nutrient diversity = {b.get('nutrient_diversity',0):.0f}"
                f" / {b.get('nutrient_diversity_full_count',10):.0f}",
                COLOR_WHITE,
            ),
        ]
        for i, (text, color) in enumerate(inputs):
            line(f"inp_{i}", "  " + text,
                 l + 10, inp_y - 16 - i * 14, size=10, color=color)

    # ══════════════════════════════════════════════════════════════════════
    #  PRODUCTION DIAGNOSTICS PANEL  (v0.22)
    # ══════════════════════════════════════════════════════════════════════
    def _draw_diagnostics_panel(self) -> None:
        """v0.22: 'D' key — modal panel listing every broken production
        chain with a root-cause explanation and a fix suggestion.

        Reads ``diagnostics.ProductionDiagnostics`` over the live game
        state (game_map + registry + economy + walker_manager + service_map
        + storage + rebellion + decay) and calls ``broken_chains()`` to
        get one entry per *broken* chain, root-cause first.

        Each row renders as:

          [icon] resource: stage_name — detail
                                       Fix: fix sentence

        Severity → icon:
          1 (info)     → ℹ blue
          2 (warning)  → ⚠ gold
          3 (critical) → ✖ red

        When every chain is healthy the panel says so and lists a couple
        of green check rows so the player knows the diagnostic ran.

        Closes on Esc or click anywhere (handled in the global ESC and
        ``on_mouse_press`` paths — no input capture here).
        """
        from diagnostics import ProductionDiagnostics

        # Build the report. Pure-logic, no rendering — fast enough to
        # run every frame even on a 50-building city. Fail-safe: any
        # exception in the engine surfaces as a single error row.
        try:
            diag = ProductionDiagnostics(
                self.game_map, self.registry, self.economy,
                walker_manager=getattr(self, "walker_manager", None),
                service_map=getattr(self, "service_map", None),
                storage=getattr(self, "storage", None),
                rebellion=getattr(self, "rebellion", None),
                decay=getattr(self, "decay", None),
            )
            reports = diag.diagnose_all()
        except Exception as exc:  # pragma: no cover — defensive
            log.exception("Diagnostics engine failed")
            reports = []
            err_text = f"Diagnostic engine error: {exc}"
        else:
            err_text = ""

        # Panel: ~70% × ~85% of the window, centred. Same proportions
        # as the jobs panel for visual consistency.
        panel_w = self.width * 0.70
        panel_h = self.height * 0.85
        cx = self.width / 2
        cy = self.height / 2
        l = cx - panel_w / 2
        r = cx + panel_w / 2
        b = cy - panel_h / 2
        t = cy + panel_h / 2

        # Backdrop dim + panel.
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 170),
        )
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        # Lazy text pool — same trick as _draw_jobs_panel.
        if not hasattr(self, "_txt_diag_lines"):
            self._txt_diag_title = arcade.Text(
                "", 0, 0, COLOR_GOLD, 16, bold=True, anchor_x="center",
            )
            self._txt_diag_subtitle = arcade.Text(
                "", 0, 0, COLOR_GRAY, 10, anchor_x="center",
            )
            self._txt_diag_lines: list[arcade.Text] = []

        # Each broken chain takes 2-4 rows (heading + 1-3 entries).
        # Pre-grow the pool generously.
        broken = [r for r in reports if r.is_broken]
        ok = [r for r in reports if not r.is_broken]
        # Worst-case: every entry of every report renders → 4 lines per
        # entry (heading + 2 detail + 1 fix). Plus 16 for headers.
        needed_slots = sum(len(rp.entries) for rp in broken) * 4 + 32
        while len(self._txt_diag_lines) < needed_slots:
            self._txt_diag_lines.append(
                arcade.Text("", 0, 0, COLOR_WHITE, 11)
            )

        # Title.
        self._txt_diag_title.text = "PRODUCTION DIAGNOSTICS"
        self._txt_diag_title.x = cx
        self._txt_diag_title.y = t - 28
        self._txt_diag_title.draw()
        self._txt_diag_subtitle.text = (
            "Press D or click anywhere to close   ·   "
            "shows the *first* broken stage in each chain"
        )
        self._txt_diag_subtitle.x = cx
        self._txt_diag_subtitle.y = t - 46
        self._txt_diag_subtitle.draw()

        # Headline summary.
        head_y = t - 80
        block_w = panel_w / 3
        head_lines = [
            (f"Chains tracked: {len(reports)}", COLOR_WHITE),
            (
                f"Healthy: {len(ok)}",
                COLOR_GREEN if ok else COLOR_GRAY,
            ),
            (
                f"Broken: {len(broken)}",
                COLOR_RED if broken else COLOR_GREEN,
            ),
        ]
        for i, (text, color) in enumerate(head_lines):
            tx = self._txt_diag_lines[i]
            tx.text = text
            tx.color = color
            tx.x = l + 16 + i * block_w
            tx.y = head_y
            tx.draw()

        # If there's an engine error, show it and stop.
        if err_text:
            tx = self._txt_diag_lines[3]
            tx.text = err_text
            tx.color = COLOR_RED
            tx.x = l + 16
            tx.y = head_y - 30
            tx.draw()
            return

        # Body: list each broken chain root-cause first. Rows are 16 px
        # tall; the panel can fit ~30-40 rows before clipping.
        body_y = head_y - 36
        line_idx = 3   # 0-2 are headline cells.
        row_h = 16

        # Severity → glyph + colour.
        sev_glyph = {1: "i", 2: "!", 3: "X"}
        sev_color = {1: (110, 170, 230), 2: COLOR_GOLD, 3: COLOR_RED}

        # Cap how many rows we draw — beyond ~36 rows the bottom of the
        # panel runs out. Crude vertical clipping; a future revision
        # could add scroll keys.
        max_rows = max(0, int((body_y - (b + 60)) / row_h))
        rendered = 0

        if not broken:
            # All-clear state. The player can hit D and see "everything's
            # fine" rather than a blank panel.
            tx = self._txt_diag_lines[line_idx]
            tx.text = "✓ All tracked chains are producing as expected."
            tx.color = COLOR_GREEN
            tx.x = l + 16
            tx.y = body_y
            tx.draw()
            line_idx += 1
            body_y -= row_h
            tx = self._txt_diag_lines[line_idx]
            tx.text = (
                f"  Healthy chains: "
                + ", ".join(rp.resource for rp in ok[:8])
                + (" ..." if len(ok) > 8 else "")
            )
            tx.color = COLOR_GRAY
            tx.x = l + 16
            tx.y = body_y
            tx.draw()
            return

        # Apply scroll: skip the first `diag_scroll` rows (clamped).
        # We compute total rows first so the scroll cap is right.
        rows_to_draw: list[tuple[str, tuple[int, int, int]]] = []
        for rp in broken:
            # Section header for the chain.
            rows_to_draw.append(
                (f"── {rp.resource.upper()} ──", COLOR_GOLD),
            )
            for entry in rp.entries:
                glyph = sev_glyph.get(entry.severity, "!")
                color = sev_color.get(entry.severity, COLOR_GOLD)
                rows_to_draw.append(
                    (
                        f"[{glyph}] {rp.resource}: {entry.stage_name} — "
                        f"{entry.detail}",
                        color,
                    ),
                )
                rows_to_draw.append(
                    (f"      Fix: {entry.fix}", COLOR_GRAY),
                )
            # Blank spacer row.
            rows_to_draw.append(("", COLOR_GRAY))

        # Clamp scroll.
        max_scroll = max(0, len(rows_to_draw) - max_rows)
        self.diag_scroll = max(0, min(self.diag_scroll, max_scroll))

        for i, (text, color) in enumerate(
            rows_to_draw[self.diag_scroll : self.diag_scroll + max_rows]
        ):
            tx = self._txt_diag_lines[line_idx + i]
            # Truncate over-long lines so they don't run off the panel.
            # ~88 chars fits at size=11 in the 70%-wide panel.
            if len(text) > 100:
                text = text[:97] + "..."
            tx.text = text
            tx.color = color
            tx.x = l + 16
            tx.y = body_y - i * row_h
            tx.draw()
            rendered += 1

        # Scroll hint at bottom if there's more.
        if max_scroll > 0:
            tx = self._txt_diag_lines[line_idx + rendered]
            tx.text = (
                f"   ({len(rows_to_draw) - max_rows - self.diag_scroll} "
                f"more rows below — PgDn / PgUp to scroll)"
                if self.diag_scroll < max_scroll
                else "   (top of list)"
            )
            tx.color = COLOR_GRAY
            tx.x = l + 16
            tx.y = b + 24
            tx.draw()
