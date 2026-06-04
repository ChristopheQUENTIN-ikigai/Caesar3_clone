"""Trade-panel modals — the bartering modal (swap good A for good B
at a flat fee) and the international-gold-trade modal (buy/sell
stockpile goods for treasury gold at the same price table).
Extracted from game_window.py for clarity (~490 lines)."""
from __future__ import annotations

import logging

import arcade

import bartering
import gold_trade
from constants import (
    COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_RED,
    COLOR_UI_BORDER, COLOR_WHITE,
)

log = logging.getLogger("caesar3.window")

class TradePanelsMixin:
    """TradePanelsMixin — see module docstring."""

    # ── v0.19: Bartering modal ────────────────────────────────────
    BARTER_PANEL_W = 720
    # v0.19.x: bumped 480 → 640 to fit all 25+ priced resources in a
    # single column. The previous height truncated at row 17 — wheat,
    # flour, olives, grapes, clay, livestock, horses, planks, iron,
    # tools, weapons all dropped off the bottom of the GIVE/RECEIVE
    # lists. Modders adding new resources to STOCK_PRICES should re-
    # check this height; a future pass could swap in a scrollable list.
    BARTER_PANEL_H = 640
    BARTER_QTY_STEPS = (1, 5, 10, 50)

    def _draw_barter(self) -> None:
        """Render the bartering modal. The player picks a 'give' resource,
        a 'receive' resource, a quantity, and confirms — the engine
        debits the give-stock + 100 fee from treasury and credits the
        receive-stock based on STOCK_PRICES.

        Layout (rebuilt each draw):
        ┌─ BARTERING ───────────────── close ─┐
        │ Treasury: 13,505 dn   Fee: 100 dn   │
        │ ┌─ GIVE ───────┬─ RECEIVE ──────┐  │
        │ │ wheat  [4 dn]│ bread   [8 dn] │  │
        │ │ <list>      │ <list>          │  │
        │ │             │                 │  │
        │ └─────────────┴─────────────────┘  │
        │ Quantity: [-1] [-5] [10] [+5][+1]  │
        │ → 5 bread for 10 wheat (+100 fee)  │
        │           [Confirm] [Cancel]        │
        └─────────────────────────────────────┘
        """
        self._barter_btn_rects = []
        # Backdrop.
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 170),
        )
        # Centred panel.
        cx, cy = self.width / 2, self.height / 2
        pw, ph = self.BARTER_PANEL_W, self.BARTER_PANEL_H
        l, r = cx - pw / 2, cx + pw / 2
        b, t = cy - ph / 2, cy + ph / 2
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        # Lazy text pool (rebuilt elements have stable keys).
        if not hasattr(self, "_txt_barter"):
            self._txt_barter: dict[str, arcade.Text] = {}

        def t_obj(key: str, text: str, x: float, y: float, *,
                  size: int = 12, color=COLOR_WHITE,
                  bold: bool = False, anchor_x: str = "left") -> arcade.Text:
            obj = self._txt_barter.get(key)
            if obj is None:
                obj = arcade.Text(
                    text, x, y, color, size,
                    bold=bold, anchor_x=anchor_x,
                )
                self._txt_barter[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            return obj

        # Title + treasury readout.
        t_obj("title", "INTERNATIONAL BARTERING", cx, t - 26,
              size=14, color=COLOR_GOLD, bold=True, anchor_x="center").draw()
        t_obj("treasury",
              f"Treasury: {int(self.economy.treasury)} dn  ·  Fee: {bartering.TRANSACTION_FEE} dn",
              cx, t - 50, size=10, color=COLOR_GRAY, anchor_x="center").draw()
        t_obj("hint",
              "Click a resource on each side, set quantity, then Confirm.",
              cx, t - 66, size=9, color=COLOR_GRAY, anchor_x="center").draw()

        # ── Two columns: Give | Receive ─────────────────────────
        col_w = (pw - 60) / 2
        col_top = t - 90
        col_bot = b + 110
        give_l = l + 16
        give_r = give_l + col_w
        recv_l = give_r + 28
        recv_r = recv_l + col_w
        # Headers.
        arcade.draw_lrbt_rectangle_filled(
            give_l, give_r, col_top - 22, col_top, (60, 50, 38),
        )
        arcade.draw_lrbt_rectangle_filled(
            recv_l, recv_r, col_top - 22, col_top, (60, 50, 38),
        )
        t_obj("give_hdr", "GIVE",
              (give_l + give_r) / 2, col_top - 16,
              size=11, color=COLOR_GOLD, bold=True, anchor_x="center").draw()
        t_obj("recv_hdr", "RECEIVE",
              (recv_l + recv_r) / 2, col_top - 16,
              size=11, color=COLOR_GOLD, bold=True, anchor_x="center").draw()

        # Resource lists (scroll-free, fixed for now — 26 known
        # resources fit in a single column at this row height).
        list_top = col_top - 24
        row_h = 16
        resources = bartering.known_resources()
        for i, rid in enumerate(resources):
            row_t = list_top - i * row_h
            row_b = row_t - row_h
            if row_b < col_bot + 4:
                break
            price = bartering.STOCK_PRICES[rid]
            stock = int(self.economy.resources.get(rid, 0))
            # Give column row
            sel = (rid == self.barter_give_id)
            if sel:
                arcade.draw_lrbt_rectangle_filled(
                    give_l, give_r, row_b, row_t, (90, 70, 50),
                )
            t_obj(f"g_{rid}",
                  f" {rid}  [stock {stock}]  {price} dn/u",
                  give_l + 4, row_b + 3,
                  size=9, color=COLOR_GOLD if sel else COLOR_WHITE).draw()
            self._barter_btn_rects.append(
                (give_l, give_r, row_b, row_t, f"give:{rid}")
            )
            # Receive column row
            sel = (rid == self.barter_recv_id)
            if sel:
                arcade.draw_lrbt_rectangle_filled(
                    recv_l, recv_r, row_b, row_t, (90, 70, 50),
                )
            t_obj(f"r_{rid}",
                  f" {rid}  [stock {stock}]  {price} dn/u",
                  recv_l + 4, row_b + 3,
                  size=9, color=COLOR_GOLD if sel else COLOR_WHITE).draw()
            self._barter_btn_rects.append(
                (recv_l, recv_r, row_b, row_t, f"recv:{rid}")
            )

        # ── Quantity controls ────────────────────────────────────
        ctrl_y = b + 76
        t_obj("qty_lbl", "Give qty:", l + 24, ctrl_y + 6,
              size=11, color=COLOR_WHITE, bold=True).draw()
        cursor = l + 110
        for step in (-50, -10, -5, -1, +1, +5, +10, +50):
            bw = 36
            arcade.draw_lrbt_rectangle_filled(
                cursor, cursor + bw, ctrl_y, ctrl_y + 24, (60, 45, 35),
            )
            arcade.draw_lrbt_rectangle_outline(
                cursor, cursor + bw, ctrl_y, ctrl_y + 24, COLOR_UI_BORDER, 1,
            )
            sign = "+" if step > 0 else ""
            t_obj(f"step_{step}", f"{sign}{step}",
                  cursor + bw / 2, ctrl_y + 6,
                  size=10, color=COLOR_WHITE, bold=True,
                  anchor_x="center").draw()
            self._barter_btn_rects.append(
                (cursor, cursor + bw, ctrl_y, ctrl_y + 24, f"step:{step}")
            )
            cursor += bw + 4

        # Current qty display
        cursor += 8
        arcade.draw_lrbt_rectangle_filled(
            cursor, cursor + 80, ctrl_y, ctrl_y + 24, (22, 18, 14),
        )
        arcade.draw_lrbt_rectangle_outline(
            cursor, cursor + 80, ctrl_y, ctrl_y + 24, COLOR_GOLD, 1,
        )
        t_obj("qty_val", str(self.barter_give_qty),
              cursor + 40, ctrl_y + 6,
              size=12, color=COLOR_GOLD, bold=True, anchor_x="center").draw()

        # ── Preview line + buttons ───────────────────────────────
        try:
            recv_qty, fee = bartering.compute_barter(
                self.barter_give_id, self.barter_give_qty, self.barter_recv_id,
            )
            preview = (
                f"→ Receive {recv_qty} {self.barter_recv_id}  "
                f"for {self.barter_give_qty} {self.barter_give_id}  "
                f"(+{fee} fee)"
            )
            preview_color = COLOR_WHITE
        except ValueError as e:
            preview = f"× {e}"
            preview_color = COLOR_RED
            recv_qty = 0
        t_obj("preview", preview, cx, b + 50,
              size=11, color=preview_color, anchor_x="center").draw()

        # Confirm + Cancel buttons.
        btn_w, btn_h = 110, 30
        confirm_l = cx - btn_w - 8
        cancel_l = cx + 8
        for label, x_l, action, fill_color in (
            ("Confirm", confirm_l, "confirm", (60, 90, 50)),
            ("Cancel", cancel_l, "cancel", (90, 60, 50)),
        ):
            arcade.draw_lrbt_rectangle_filled(
                x_l, x_l + btn_w, b + 14, b + 14 + btn_h, fill_color,
            )
            arcade.draw_lrbt_rectangle_outline(
                x_l, x_l + btn_w, b + 14, b + 14 + btn_h, COLOR_GOLD, 1,
            )
            t_obj(f"btn_{action}", label,
                  x_l + btn_w / 2, b + 14 + 8,
                  size=12, color=COLOR_WHITE, bold=True,
                  anchor_x="center").draw()
            self._barter_btn_rects.append(
                (x_l, x_l + btn_w, b + 14, b + 14 + btn_h, action)
            )

    def _barter_handle_click(self, x: int, y: int) -> None:
        for x1, x2, y1, y2, action in self._barter_btn_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                if action == "cancel":
                    self.show_barter = False
                    return
                if action == "confirm":
                    rec = bartering.execute_barter(
                        self.economy,
                        self.barter_give_id,
                        self.barter_give_qty,
                        self.barter_recv_id,
                    )
                    if rec is None:
                        self._notify(
                            "Barter failed (insufficient stock or treasury)",
                            COLOR_RED,
                        )
                    else:
                        self._notify(
                            f"Bartered {rec['give_qty']} {rec['give']} "
                            f"→ {rec['recv_qty']} {rec['recv']} "
                            f"(fee {rec['fee']})",
                            COLOR_GREEN,
                        )
                        self.barter_history.append(rec)
                        # Stash with the recorder so the next tick's
                        # JSONL line includes the trade.
                        self.recorder.note_barter(rec)
                    return
                if action.startswith("give:"):
                    self.barter_give_id = action.split(":", 1)[1]
                    return
                if action.startswith("recv:"):
                    self.barter_recv_id = action.split(":", 1)[1]
                    return
                if action.startswith("step:"):
                    delta = int(action.split(":", 1)[1])
                    self.barter_give_qty = max(1, self.barter_give_qty + delta)
                    return
                return

    # ══════════════════════════════════════════════════════════════════════
    #  GOLD TRADE MODAL  (v0.35)  — 'C' key
    # ══════════════════════════════════════════════════════════════════════
    # Sibling of the bartering modal. Where bartering swaps good A for
    # good B, gold-trade swaps a single good for gold (BUY: pay treasury
    # for goods at market price; SELL: give up goods for treasury gold).
    # Same price table (bartering.STOCK_PRICES) and same flat 100 dn fee.
    GOLD_TRADE_PANEL_W = 720
    GOLD_TRADE_PANEL_H = 640

    def _menu_open_gold_trade(self) -> None:
        """ESC-menu shortcut to open the gold-trade modal."""
        self.show_menu = False
        self._close_other_modal_panels(keep="gold_trade")
        self.show_gold_trade = True

    def _draw_gold_trade(self) -> None:
        """v0.35: international gold-trade modal. Players exchange a
        single resource for gold (BUY: pay gold, gain stock; SELL: lose
        stock, gain gold) at the per-unit price defined in
        ``bartering.STOCK_PRICES``. A 100 dn flat fee applies on both
        sides.
        """
        self._gold_trade_btn_rects = []
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 170),
        )
        cx, cy = self.width / 2, self.height / 2
        pw, ph = self.GOLD_TRADE_PANEL_W, self.GOLD_TRADE_PANEL_H
        l, r = cx - pw / 2, cx + pw / 2
        b, t = cy - ph / 2, cy + ph / 2
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_gold_trade"):
            self._txt_gold_trade: dict[str, arcade.Text] = {}

        def t_obj(key: str, text: str, x: float, y: float, *,
                  size: int = 12, color=COLOR_WHITE,
                  bold: bool = False, anchor_x: str = "left") -> arcade.Text:
            obj = self._txt_gold_trade.get(key)
            if obj is None:
                obj = arcade.Text(
                    text, x, y, color, size,
                    bold=bold, anchor_x=anchor_x,
                )
                self._txt_gold_trade[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            return obj

        t_obj("title", "INTERNATIONAL TRADE — Goods for Gold", cx, t - 26,
              size=14, color=COLOR_GOLD, bold=True, anchor_x="center").draw()
        t_obj("treasury",
              f"Treasury: {int(self.economy.treasury)} dn  ·  "
              f"Fee: {gold_trade.TRANSACTION_FEE} dn",
              cx, t - 50, size=10, color=COLOR_GRAY, anchor_x="center").draw()
        t_obj("hint",
              "Pick BUY or SELL, pick a resource, set the quantity, confirm.",
              cx, t - 66, size=9, color=COLOR_GRAY, anchor_x="center").draw()

        # BUY / SELL toggle.
        tab_y_b = t - 100
        tab_y_t = tab_y_b + 28
        tab_w = 100
        for i, (side, label) in enumerate((("buy", "BUY"), ("sell", "SELL"))):
            tab_l = l + 24 + i * (tab_w + 8)
            tab_r = tab_l + tab_w
            sel = (self.gold_trade_side == side)
            arcade.draw_lrbt_rectangle_filled(
                tab_l, tab_r, tab_y_b, tab_y_t,
                (90, 70, 50) if sel else (50, 40, 32),
            )
            arcade.draw_lrbt_rectangle_outline(
                tab_l, tab_r, tab_y_b, tab_y_t,
                COLOR_GOLD if sel else COLOR_UI_BORDER, 2 if sel else 1,
            )
            t_obj(f"side_{side}", label, (tab_l + tab_r) / 2, tab_y_b + 8,
                  size=12, color=COLOR_GOLD if sel else COLOR_WHITE,
                  bold=True, anchor_x="center").draw()
            self._gold_trade_btn_rects.append(
                (tab_l, tab_r, tab_y_b, tab_y_t, f"side:{side}")
            )

        # Resource list — single column, full width.
        list_l = l + 24
        list_r = r - 24
        list_top = tab_y_b - 12
        list_bot = b + 130
        row_h = 18
        resources = bartering.known_resources()
        for i, rid in enumerate(resources):
            row_t = list_top - i * row_h
            row_b = row_t - row_h
            if row_b < list_bot + 4:
                break
            price = bartering.STOCK_PRICES[rid]
            stock = int(self.economy.resources.get(rid, 0))
            sel = (rid == self.gold_trade_resource)
            if sel:
                arcade.draw_lrbt_rectangle_filled(
                    list_l, list_r, row_b, row_t, (90, 70, 50),
                )
            t_obj(f"row_{rid}",
                  f"  {rid:<14s} [stock {stock:>6d}]  {price:>3d} dn/u",
                  list_l + 4, row_b + 3,
                  size=10, color=COLOR_GOLD if sel else COLOR_WHITE).draw()
            self._gold_trade_btn_rects.append(
                (list_l, list_r, row_b, row_t, f"resource:{rid}")
            )

        # Quantity controls — same step layout as bartering.
        ctrl_y = b + 80
        t_obj("qty_lbl", "Quantity:", l + 24, ctrl_y + 6,
              size=11, color=COLOR_WHITE, bold=True).draw()
        cursor = l + 110
        for step in (-50, -10, -5, -1, +1, +5, +10, +50):
            bw = 36
            arcade.draw_lrbt_rectangle_filled(
                cursor, cursor + bw, ctrl_y, ctrl_y + 24, (60, 45, 35),
            )
            arcade.draw_lrbt_rectangle_outline(
                cursor, cursor + bw, ctrl_y, ctrl_y + 24, COLOR_UI_BORDER, 1,
            )
            sign = "+" if step > 0 else ""
            t_obj(f"step_{step}", f"{sign}{step}",
                  cursor + bw / 2, ctrl_y + 6,
                  size=10, color=COLOR_WHITE, bold=True,
                  anchor_x="center").draw()
            self._gold_trade_btn_rects.append(
                (cursor, cursor + bw, ctrl_y, ctrl_y + 24, f"step:{step}")
            )
            cursor += bw + 4

        cursor += 8
        arcade.draw_lrbt_rectangle_filled(
            cursor, cursor + 80, ctrl_y, ctrl_y + 24, (22, 18, 14),
        )
        arcade.draw_lrbt_rectangle_outline(
            cursor, cursor + 80, ctrl_y, ctrl_y + 24, COLOR_GOLD, 1,
        )
        t_obj("qty_val", str(self.gold_trade_qty),
              cursor + 40, ctrl_y + 6,
              size=12, color=COLOR_GOLD, bold=True, anchor_x="center").draw()

        # Preview line.
        try:
            if self.gold_trade_side == "buy":
                gold_amt, fee = gold_trade.compute_buy(
                    self.gold_trade_resource, self.gold_trade_qty,
                )
                preview = (
                    f"BUY {self.gold_trade_qty} {self.gold_trade_resource} "
                    f"for {gold_amt} dn  (+{fee} fee, total {gold_amt + fee})"
                )
            else:
                gold_amt, fee = gold_trade.compute_sell(
                    self.gold_trade_resource, self.gold_trade_qty,
                )
                preview = (
                    f"SELL {self.gold_trade_qty} {self.gold_trade_resource} "
                    f"for {gold_amt} dn  (-{fee} fee, net {gold_amt - fee})"
                )
            preview_color = COLOR_WHITE
        except ValueError as e:
            preview = f"x {e}"
            preview_color = COLOR_RED
        t_obj("preview", preview, cx, b + 50,
              size=11, color=preview_color, anchor_x="center").draw()

        # Confirm + Cancel.
        btn_w, btn_h = 110, 30
        confirm_l = cx - btn_w - 8
        cancel_l = cx + 8
        for label, x_l, action, fill_color in (
            ("Confirm", confirm_l, "confirm", (60, 90, 50)),
            ("Cancel",  cancel_l,  "cancel",  (90, 60, 50)),
        ):
            arcade.draw_lrbt_rectangle_filled(
                x_l, x_l + btn_w, b + 14, b + 14 + btn_h, fill_color,
            )
            arcade.draw_lrbt_rectangle_outline(
                x_l, x_l + btn_w, b + 14, b + 14 + btn_h, COLOR_GOLD, 1,
            )
            t_obj(f"btn_{action}", label,
                  x_l + btn_w / 2, b + 14 + 8,
                  size=12, color=COLOR_WHITE, bold=True,
                  anchor_x="center").draw()
            self._gold_trade_btn_rects.append(
                (x_l, x_l + btn_w, b + 14, b + 14 + btn_h, action)
            )

    def _gold_trade_handle_click(self, x: int, y: int) -> None:
        for x1, x2, y1, y2, action in self._gold_trade_btn_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                if action == "cancel":
                    self.show_gold_trade = False
                    return
                if action == "confirm":
                    if self.gold_trade_side == "buy":
                        rec = gold_trade.execute_buy(
                            self.economy,
                            self.gold_trade_resource,
                            self.gold_trade_qty,
                        )
                    else:
                        rec = gold_trade.execute_sell(
                            self.economy,
                            self.gold_trade_resource,
                            self.gold_trade_qty,
                        )
                    if rec is None:
                        self._notify(
                            "Trade failed (insufficient gold or stock)",
                            COLOR_RED,
                        )
                    else:
                        side_label = rec["side"].upper()
                        self._notify(
                            f"{side_label} {rec['qty']} {rec['resource']} "
                            f"({rec['gold']} dn, fee {rec['fee']})",
                            COLOR_GREEN,
                        )
                        self.gold_trade_history.append(rec)
                    return
                if action.startswith("side:"):
                    self.gold_trade_side = action.split(":", 1)[1]
                    return
                if action.startswith("resource:"):
                    self.gold_trade_resource = action.split(":", 1)[1]
                    return
                if action.startswith("step:"):
                    delta = int(action.split(":", 1)[1])
                    self.gold_trade_qty = max(
                        1, self.gold_trade_qty + delta,
                    )
                    return
                return

    # ══════════════════════════════════════════════════════════════════════
    #  COMMERCIAL ROADS — link cities, run persistent auto-trade routes (v0.50)
    # ══════════════════════════════════════════════════════════════════════
    COMMERCIAL_PANEL_W = 760
    COMMERCIAL_PANEL_H = 660
    # Per-tick quantities the player can pick when staging a new route.
    COMMERCIAL_RATE_STEPS = (1, 5, 10, 25)

    def _draw_commercial_roads(self) -> None:
        """v0.50: the Commercial roads window. Two views in one modal:

          * **City list** (default): every foreign city with its link
            status / cost and a Link or Open button.
          * **City detail** (a city is selected): the city's persistent
            routes with per-route Remove, plus an "add route" staging
            row (pick a good, pick a per-tick rate, Add).

        Routes are persistent auto-trades: each ships its good every
        tick for gold via the shared TradeRouteManager (the same engine
        the ambient trade routes use).
        """
        import bartering
        import commercial_roads as cr

        self._commercial_btn_rects = []
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 175),
        )
        cx, cy = self.width / 2, self.height / 2
        pw, ph = self.COMMERCIAL_PANEL_W, self.COMMERCIAL_PANEL_H
        l, r = cx - pw / 2, cx + pw / 2
        b, t = cy - ph / 2, cy + ph / 2
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (33, 27, 21, 248))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_commercial"):
            self._txt_commercial: dict = {}

        def tx(key: str, text: str, x: float, y: float, *,
               size: int = 11, color=COLOR_WHITE, bold: bool = False,
               anchor_x: str = "left") -> arcade.Text:
            obj = self._txt_commercial.get(key)
            if obj is None:
                obj = arcade.Text(text, x, y, color, size,
                                  bold=bold, anchor_x=anchor_x)
                self._txt_commercial[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            return obj

        mgr = getattr(self, "commercial_roads", None)
        treasury = int(self.economy.treasury)

        tx("title", "COMMERCIAL ROADS — Trade Cities", cx, t - 26,
           size=14, color=COLOR_GOLD, bold=True, anchor_x="center").draw()
        tx("treasury", f"Treasury: {treasury} dn", cx, t - 48,
           size=10, color=COLOR_GRAY, anchor_x="center").draw()

        # Close button (top-right X).
        cl_r = r - 12
        cl_l = cl_r - 26
        cl_t = t - 12
        cl_b = cl_t - 26
        arcade.draw_lrbt_rectangle_filled(cl_l, cl_r, cl_b, cl_t, (90, 60, 50))
        arcade.draw_lrbt_rectangle_outline(cl_l, cl_r, cl_b, cl_t, COLOR_GOLD, 1)
        tx("close", "X", (cl_l + cl_r) / 2, cl_b + 6, size=12,
           color=COLOR_WHITE, bold=True, anchor_x="center").draw()
        self._commercial_btn_rects.append((cl_l, cl_r, cl_b, cl_t, "close"))

        if mgr is None:
            tx("noenv", "Commercial roads unavailable in this context.",
               cx, cy, size=11, color=COLOR_RED, anchor_x="center").draw()
            return

        selected = getattr(self, "commercial_selected_city", None)
        if selected is None:
            self._draw_commercial_city_list(tx, l, r, b, t, cr, mgr, treasury)
        else:
            self._draw_commercial_city_detail(
                tx, l, r, b, t, cr, mgr, bartering, selected, treasury,
            )

    def _draw_commercial_city_list(self, tx, l, r, b, t, cr, mgr, treasury):
        """Render the city-list view: one row per trade city."""
        tx("hint_list",
           "Link a city to open a commercial road, then ship goods by voyage.",
           (l + r) / 2, t - 66, size=9, color=COLOR_GRAY,
           anchor_x="center").draw()
        row_top = t - 92
        row_h = 46
        vm = getattr(self, "voyage_manager", None)
        for cid in cr.city_ids():
            meta = cr.city_meta(cid)
            row_b = row_top - row_h
            arcade.draw_lrbt_rectangle_filled(
                l + 16, r - 16, row_b + 4, row_top, (46, 38, 30),
            )
            arcade.draw_lrbt_rectangle_outline(
                l + 16, r - 16, row_b + 4, row_top, COLOR_UI_BORDER, 1,
            )
            linked = mgr.is_linked(cid)
            tx(f"city_{cid}", meta["name"], l + 30, row_top - 20,
               size=13, color=COLOR_GOLD if linked else COLOR_WHITE,
               bold=True).draw()
            if linked:
                # v0.53: show the per-trip voyage state, not a route count.
                cfg = vm.config_for(cid) if vm is not None else None
                if cfg is not None and cfg.good:
                    state = "shipping" if cfg.enabled else "paused"
                    status = f"Linked · voyages {state}: {cfg.good}"
                else:
                    status = "Linked · no voyage set"
                status_color = COLOR_GREEN
            else:
                status = f"Not linked · link cost {meta['link_cost']} dn"
                status_color = COLOR_GRAY
            tx(f"status_{cid}", status, l + 30, row_top - 38,
               size=9, color=status_color).draw()
            tx(f"mult_{cid}",
               f"profit ×{meta['profit_mult']:.2f}",
               r - 220, row_top - 20, size=9, color=COLOR_GRAY).draw()

            # Action button on the right.
            btn_r = r - 30
            btn_l = btn_r - 110
            btn_t = row_top - 10
            btn_b = btn_t - 26
            if linked:
                arcade.draw_lrbt_rectangle_filled(
                    btn_l, btn_r, btn_b, btn_t, (50, 70, 45),
                )
                arcade.draw_lrbt_rectangle_outline(
                    btn_l, btn_r, btn_b, btn_t, COLOR_GOLD, 1,
                )
                tx(f"act_{cid}", "Open", (btn_l + btn_r) / 2, btn_b + 6,
                   size=11, color=COLOR_WHITE, bold=True,
                   anchor_x="center").draw()
                self._commercial_btn_rects.append(
                    (btn_l, btn_r, btn_b, btn_t, f"open:{cid}")
                )
            else:
                affordable = treasury >= meta["link_cost"]
                fill = (60, 80, 55) if affordable else (60, 50, 45)
                arcade.draw_lrbt_rectangle_filled(btn_l, btn_r, btn_b, btn_t, fill)
                arcade.draw_lrbt_rectangle_outline(
                    btn_l, btn_r, btn_b, btn_t,
                    COLOR_GOLD if affordable else COLOR_UI_BORDER, 1,
                )
                tx(f"act_{cid}", "Link", (btn_l + btn_r) / 2, btn_b + 6,
                   size=11, color=COLOR_WHITE if affordable else COLOR_GRAY,
                   bold=True, anchor_x="center").draw()
                self._commercial_btn_rects.append(
                    (btn_l, btn_r, btn_b, btn_t, f"link:{cid}")
                )
            row_top -= row_h

    def _draw_commercial_city_detail(self, tx, l, r, b, t, cr, mgr,
                                     bartering, cid, treasury):
        """Render the detail view for one linked city: its per-trip
        voyage config + a good picker to set the voyage good.

        v0.53: the per-tick stock-moving route list and its add-route
        staging row are gone — inter-city export is per-trip only.
        """
        meta = cr.city_meta(cid)
        # Back button.
        bk_l = l + 16
        bk_r = bk_l + 80
        bk_t = t - 66
        bk_b = bk_t - 24
        arcade.draw_lrbt_rectangle_filled(bk_l, bk_r, bk_b, bk_t, (60, 50, 40))
        arcade.draw_lrbt_rectangle_outline(bk_l, bk_r, bk_b, bk_t, COLOR_UI_BORDER, 1)
        tx("back", "< Cities", (bk_l + bk_r) / 2, bk_b + 6, size=10,
           color=COLOR_WHITE, bold=True, anchor_x="center").draw()
        self._commercial_btn_rects.append((bk_l, bk_r, bk_b, bk_t, "back"))

        tx("detail_name", meta["name"], (l + r) / 2, t - 70,
           size=13, color=COLOR_GOLD, bold=True, anchor_x="center").draw()

        # Unlink button (right).
        ul_r = r - 16
        ul_l = ul_r - 90
        ul_t = t - 66
        ul_b = ul_t - 24
        arcade.draw_lrbt_rectangle_filled(ul_l, ul_r, ul_b, ul_t, (90, 55, 45))
        arcade.draw_lrbt_rectangle_outline(ul_l, ul_r, ul_b, ul_t, COLOR_GOLD, 1)
        tx("unlink", "Unlink", (ul_l + ul_r) / 2, ul_b + 6, size=10,
           color=COLOR_WHITE, bold=True, anchor_x="center").draw()
        self._commercial_btn_rects.append((ul_l, ul_r, ul_b, ul_t, f"unlink:{cid}"))

        # v0.53: inter-city commerce is per-trip only. A short note where
        # the old per-tick route list used to sit, so the model is clear.
        tx("commerce_note",
           "Inter-city goods move by sea voyage — one trip per ship, "
           "gold paid on return. (No per-tick routes.)",
           l + 24, t - 104, size=10, color=COLOR_GRAY, bold=True).draw()

        # ── v0.51: per-trip voyage block ────────────────────────────
        # Sits above the good-picker staging area. Shows the city's
        # voyage config (which good ships by trip, on/off, the
        # distance-scaled interval and the free-ship count) plus buttons
        # to set the picked good as the voyage good, pause/resume, and
        # stop. A voyage carries up to VOYAGE_SHIP_CAPACITY units and the
        # gold lands once per completed round trip.
        import voyages as _voy
        from constants import VOYAGE_SHIP_CAPACITY as _CAP
        vm = getattr(self, "voyage_manager", None)
        voy_top = b + 268
        arcade.draw_line(l + 20, voy_top + 8, r - 20, voy_top + 8,
                         COLOR_UI_BORDER, 1)
        tx("voy_hdr",
           f"Per-trip voyages  (1 ship <= {_CAP} units, gold paid on return)",
           l + 24, voy_top - 8, size=11, color=COLOR_GOLD, bold=True).draw()
        cfg = vm.config_for(cid) if vm is not None else None
        if cfg is not None and cfg.good:
            interval = _voy.city_interval(cid)
            unit = _voy.voyage_unit_price(cfg.good, cid)
            free = self._voyage_world().ships_available() if vm is not None else 0
            state = "ON" if cfg.enabled else "PAUSED"
            scolor = COLOR_GREEN if cfg.enabled else COLOR_GRAY
            tx("voy_state",
               f"Shipping {cfg.good}  ·  every ~{interval} ticks  ·  "
               f"@{unit} dn/u  ·  free ships: {free}",
               l + 30, voy_top - 28, size=10, color=COLOR_WHITE).draw()
            tx("voy_flag", f"[{state}]", l + 30, voy_top - 44, size=10,
               color=scolor, bold=True).draw()
            # Toggle + Stop buttons (right side of this row).
            tg_r = r - 130
            tg_l = tg_r - 90
            tg_t = voy_top - 24
            tg_b = tg_t - 24
            arcade.draw_lrbt_rectangle_filled(tg_l, tg_r, tg_b, tg_t, (55, 70, 50))
            arcade.draw_lrbt_rectangle_outline(tg_l, tg_r, tg_b, tg_t, COLOR_UI_BORDER, 1)
            tx("voy_tg", "Resume" if not cfg.enabled else "Pause",
               (tg_l + tg_r) / 2, tg_b + 6, size=10, color=COLOR_WHITE,
               bold=True, anchor_x="center").draw()
            self._commercial_btn_rects.append(
                (tg_l, tg_r, tg_b, tg_t, f"voyagetoggle:{cid}")
            )
            st_r = r - 30
            st_l = st_r - 90
            st_t = voy_top - 24
            st_b = st_t - 24
            arcade.draw_lrbt_rectangle_filled(st_l, st_r, st_b, st_t, (90, 55, 45))
            arcade.draw_lrbt_rectangle_outline(st_l, st_r, st_b, st_t, COLOR_UI_BORDER, 1)
            tx("voy_stop", "Stop", (st_l + st_r) / 2, st_b + 6, size=10,
               color=COLOR_WHITE, bold=True, anchor_x="center").draw()
            self._commercial_btn_rects.append(
                (st_l, st_r, st_b, st_t, f"voyageclear:{cid}")
            )
        else:
            tx("voy_none",
               "No voyage set - pick a good below, then 'Ship by trip'.",
               l + 30, voy_top - 28, size=10, color=COLOR_GRAY).draw()

        # ── Voyage good-picker staging area ─────────────────────────
        # v0.53: this used to be the "Add a route" (per-tick) staging
        # row. Now it just picks the good a voyage will carry, then the
        # player presses "Ship by trip". No rate stepper — a voyage
        # carries up to VOYAGE_SHIP_CAPACITY per trip, not a per-tick rate.
        stage_top = b + 230
        arcade.draw_line(l + 20, stage_top + 8, r - 20, stage_top + 8,
                         COLOR_UI_BORDER, 1)
        tx("add_hdr", "Pick a good to ship by trip", l + 24, stage_top - 8,
           size=11, color=COLOR_GOLD, bold=True).draw()

        good = getattr(self, "commercial_route_good", "wheat")

        # Good picker — a compact scrollable single-column list.
        goods = bartering.known_resources()
        tx("good_lbl", "Good:", l + 24, stage_top - 30, size=10,
           color=COLOR_WHITE).draw()
        list_l = l + 24
        list_r = l + 24 + 220
        list_top = stage_top - 44
        list_bot = b + 70
        row_g = 16
        max_vis = max(1, int((list_top - list_bot) // row_g))
        scroll = max(0, min(getattr(self, "_commercial_scroll", 0),
                            max(0, len(goods) - max_vis)))
        self._commercial_scroll = scroll
        for i, gid in enumerate(goods[scroll:scroll + max_vis]):
            gy_t = list_top - i * row_g
            gy_b = gy_t - row_g
            sel = (gid == good)
            if sel:
                arcade.draw_lrbt_rectangle_filled(list_l, list_r, gy_b, gy_t, (90, 70, 50))
            stock = int(self.economy.resources.get(gid, 0))
            tx(f"good_{gid}",
               f"  {gid:<13s} [stk {stock:>5d}]",
               list_l + 4, gy_b + 2, size=9,
               color=COLOR_GOLD if sel else COLOR_WHITE).draw()
            self._commercial_btn_rects.append(
                (list_l, list_r, gy_b, gy_t, f"good:{gid}")
            )

        # Right column: per-trip earnings preview + the Ship-by-trip
        # button. No rate controls and no per-tick "Add route" anymore.
        import voyages as _voy
        from constants import VOYAGE_SHIP_CAPACITY as _CAP
        col_l = list_r + 30
        unit = _voy.voyage_unit_price(good, cid)
        interval = _voy.city_interval(cid)
        tx("trip_lbl", "Per trip:", col_l, stage_top - 30,
           size=11, color=COLOR_WHITE, bold=True).draw()
        tx("trip_preview",
           f"→ up to {_CAP} {good}/trip  ·  @{unit} dn/u  ·  every ~{interval}t "
           f"(max ~{_CAP * unit} dn on return)",
           col_l, stage_top - 50, size=10, color=COLOR_GREEN).draw()

        # Ship-by-trip button — set the picked good as this city's voyage
        # good and enable it.
        sbt_l = col_l
        sbt_r = sbt_l + 150
        sbt_t = stage_top - 70
        sbt_b = sbt_t - 30
        arcade.draw_lrbt_rectangle_filled(sbt_l, sbt_r, sbt_b, sbt_t, (50, 70, 90))
        arcade.draw_lrbt_rectangle_outline(sbt_l, sbt_r, sbt_b, sbt_t, COLOR_GOLD, 1)
        tx("sbt_btn", "Ship by trip", (sbt_l + sbt_r) / 2, sbt_b + 8, size=12,
           color=COLOR_WHITE, bold=True, anchor_x="center").draw()
        self._commercial_btn_rects.append(
            (sbt_l, sbt_r, sbt_b, sbt_t, f"voyageship:{cid}")
        )

    def _commercial_roads_handle_click(self, x: int, y: int) -> None:
        """Dispatch a click in the Commercial roads window."""
        mgr = getattr(self, "commercial_roads", None)
        for x1, x2, y1, y2, action in self._commercial_btn_rects:
            if not (x1 <= x <= x2 and y1 <= y <= y2):
                continue
            if action == "close":
                self.show_commercial_roads = False
                return
            if action == "back":
                self.commercial_selected_city = None
                self._commercial_scroll = 0
                return
            if mgr is None:
                return
            if action.startswith("open:"):
                self.commercial_selected_city = action.split(":", 1)[1]
                self._commercial_scroll = 0
                return
            if action.startswith("link:"):
                cid = action.split(":", 1)[1]
                if mgr.link_city(self.economy, cid):
                    import commercial_roads as cr
                    self._notify(
                        f"Commercial road to {cr.city_name(cid)} opened "
                        f"(-{mgr.link_cost(cid)} dn)",
                        COLOR_GREEN,
                    )
                    self.commercial_selected_city = cid
                else:
                    self._notify("Cannot link city (insufficient gold?)", COLOR_RED)
                return
            if action.startswith("unlink:"):
                cid = action.split(":", 1)[1]
                mgr.unlink_city(cid)
                # v0.51: drop the per-trip voyage config too. In-flight
                # voyages already at sea still complete (the ship returns);
                # we just stop dispatching new ones to an unlinked city.
                vm = getattr(self, "voyage_manager", None)
                if vm is not None:
                    vm.clear_city(cid)
                import commercial_roads as cr
                self._notify(f"{cr.city_name(cid)} unlinked", COLOR_GRAY)
                self.commercial_selected_city = None
                return
            if action.startswith("good:"):
                self.commercial_route_good = action.split(":", 1)[1]
                return
            # v0.53: the per-tick "rate:" stepper, "addroute:" and
            # "removeroute:" actions are gone — inter-city export is
            # per-trip only. The good-picker above feeds "Ship by trip".
            # ── v0.51: per-trip voyage controls ──────────────────────
            if action.startswith("voyageship:"):
                # Set the currently-picked good as this city's voyage good
                # (and enable it). One ship sails per round trip once a
                # free commercial ship + stock + safe seas allow.
                cid = action.split(":", 1)[1]
                vm = getattr(self, "voyage_manager", None)
                if vm is not None:
                    good = getattr(self, "commercial_route_good", "wheat")
                    vm.configure(cid, good=good, enabled=True)
                    import commercial_roads as cr
                    self._notify(
                        f"Voyages set: ship {good} to {cr.city_name(cid)} "
                        f"by trip",
                        COLOR_GREEN,
                    )
                return
            if action.startswith("voyagetoggle:"):
                cid = action.split(":", 1)[1]
                vm = getattr(self, "voyage_manager", None)
                if vm is not None:
                    cfg = vm.config_for(cid)
                    if cfg is not None:
                        vm.configure(cid, enabled=not cfg.enabled)
                return
            if action.startswith("voyageclear:"):
                cid = action.split(":", 1)[1]
                vm = getattr(self, "voyage_manager", None)
                if vm is not None:
                    vm.clear_city(cid)
                    self._notify("Voyages stopped for city", COLOR_GRAY)
                return

    def _commercial_roads_handle_scroll(self, scroll_y: float) -> None:
        """Wheel scroll for the good-picker list in the detail view."""
        self._commercial_scroll = max(
            0, getattr(self, "_commercial_scroll", 0) - int(scroll_y),
        )
