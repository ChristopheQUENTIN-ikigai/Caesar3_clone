"""Scheduled-events panel — the modal layered over the map editor that
lets the player attach per-map scheduled events (`at_year`,
`on_population_above`, etc.) to the map being authored. Extracted
from game_window.py for clarity (~475 lines)."""
from __future__ import annotations

import logging

import arcade

from constants import (
    COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_RED,
    COLOR_UI_BORDER, COLOR_WHITE,
)

log = logging.getLogger("caesar3.window")

class ScheduledEventsPanelMixin:
    """ScheduledEventsPanelMixin — see module docstring."""

    # ── v0.28: Scheduled-events panel (map editor) ───────────────────
    SCHEDULED_PANEL_W = 540
    SCHEDULED_PANEL_H = 460
    SCHEDULED_ROW_H = 28
    SCHEDULED_TRIGGER_KINDS: list[str] = [
        "at_year",
        "at_month",
        "at_tick",
        "random_after_tick",
        "on_population_above",
        "on_treasury_below",
    ]

    def _draw_scheduled_panel(self) -> None:
        """Draw the per-map scheduled-events panel over the map editor.

        Layout: header (with disable_random_events toggle) + scrollable
        list of scheduled-event rows (each row: event-name dropdown,
        trigger-kind dropdown, value text field, remove button) +
        footer with Add and Close buttons.

        Mutates ``game.event_manager.scheduled`` in-place. Save round-
        trips via the existing ``save_map`` plumbing (the panel itself
        has no Save button — the player saves via the toolbar's "Save
        map", same as everything else in the editor).
        """
        self._scheduled_panel_rects = []
        em = getattr(self, "event_manager", None)
        if em is None:
            # Defensive: the editor bootstrap always seeds an
            # event_manager, but a future entry point that opens the
            # panel without one shouldn't crash.
            return
        # Dim background behind the panel — softer than the editor
        # modal because the player should still see the map.
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 130),
        )
        cx = self.width / 2
        cy = self.height / 2
        pw = self.SCHEDULED_PANEL_W
        ph = self.SCHEDULED_PANEL_H
        l = cx - pw / 2
        r = cx + pw / 2
        b = cy - ph / 2
        t = cy + ph / 2
        # Clamp.
        if t > self.height - 20:
            shift = (self.height - 20) - t
            t += shift
            b += shift
        if b < 20:
            shift = 20 - b
            t += shift
            b += shift
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_scheduled"):
            self._txt_scheduled: dict[str, arcade.Text] = {}

        def sched_text(key: str, x: float, y: float, size: int = 11,
                       color=COLOR_WHITE, bold: bool = False,
                       anchor_x: str = "left") -> arcade.Text:
            t_obj = self._txt_scheduled.get(key)
            if t_obj is None:
                t_obj = arcade.Text("", x, y, color, size,
                                    bold=bold, anchor_x=anchor_x)
                self._txt_scheduled[key] = t_obj
            t_obj.x = x
            t_obj.y = y
            t_obj.color = color
            return t_obj

        # Header.
        title = sched_text("title", cx, t - 24, 13, COLOR_GOLD, True,
                           anchor_x="center")
        title.text = "SCHEDULED EVENTS"
        title.draw()
        hint = sched_text("hint", cx, t - 42, 9, COLOR_GRAY, anchor_x="center")
        hint.text = "Wire events to fire at specific points in the scenario."
        hint.draw()

        # disable_random_events toggle.
        tg_l = l + 16
        tg_r = tg_l + 22
        tg_t = t - 60
        tg_b = tg_t - 18
        arcade.draw_lrbt_rectangle_filled(tg_l, tg_r, tg_b, tg_t, (22, 18, 14))
        arcade.draw_lrbt_rectangle_outline(tg_l, tg_r, tg_b, tg_t, COLOR_UI_BORDER, 1)
        if em.disable_random_events:
            # Filled square = on.
            arcade.draw_lrbt_rectangle_filled(
                tg_l + 4, tg_r - 4, tg_b + 4, tg_t - 4, COLOR_GOLD,
            )
        tg_lbl = sched_text(
            "tg_lbl", tg_r + 8, tg_b + 4, 11, COLOR_WHITE,
        )
        tg_lbl.text = "Disable random events (scripted-only scenario)"
        tg_lbl.draw()
        self._scheduled_panel_rects.append(
            (tg_l, tg_r + 280, tg_b, tg_t, "toggle_random")
        )

        # ── Row area ─────────────────────────────────────────────────
        rows_l = l + 16
        rows_r = r - 16
        rows_t = tg_b - 12
        rows_b = b + 56
        arcade.draw_lrbt_rectangle_filled(
            rows_l, rows_r, rows_b, rows_t, (22, 18, 14, 230),
        )
        arcade.draw_lrbt_rectangle_outline(
            rows_l, rows_r, rows_b, rows_t, COLOR_UI_BORDER, 1,
        )
        # Column headers.
        col_event_x = rows_l + 8
        col_kind_x  = rows_l + 178
        col_val_x   = rows_l + 360
        col_rm_x    = rows_r - 30
        hdr_y = rows_t - 16
        for x, label in (
            (col_event_x, "Event"),
            (col_kind_x,  "Trigger"),
            (col_val_x,   "Value"),
        ):
            ht = sched_text(f"hdr_{label}", x, hdr_y, 10, COLOR_GRAY, True)
            ht.text = label
            ht.draw()

        # Render rows.
        row_y = rows_t - 36
        # We don't bother scrolling — keep the panel sized to fit a
        # reasonable cap (~10 rows). If a map has more than that, the
        # later rows clip; nothing in the game's authoring loop has
        # hit that limit yet, and a scroll bar is more complexity
        # than v0.28 wants.
        for ri, entry in enumerate(em.scheduled):
            if row_y - self.SCHEDULED_ROW_H < rows_b + 4:
                break
            row_top = row_y
            row_bot = row_y - 22
            # Event dropdown.
            ev_name = str(entry.get("event") or "(none)")
            ev_l = col_event_x
            ev_r = col_kind_x - 6
            arcade.draw_lrbt_rectangle_filled(ev_l, ev_r, row_bot, row_top, (50, 40, 32))
            arcade.draw_lrbt_rectangle_outline(
                ev_l, ev_r, row_bot, row_top,
                COLOR_GOLD if self.scheduled_dropdown_open == f"event:{ri}"
                else COLOR_UI_BORDER,
                2 if self.scheduled_dropdown_open == f"event:{ri}" else 1,
            )
            evt = sched_text(f"ev_{ri}", ev_l + 6, row_bot + 4, 10, COLOR_WHITE)
            evt.text = ev_name if len(ev_name) <= 18 else ev_name[:17] + "…"
            evt.draw()
            self._scheduled_panel_rects.append(
                (ev_l, ev_r, row_bot, row_top, f"open_event_dd:{ri}")
            )
            # Kind dropdown.
            kind = str(entry.get("trigger", {}).get("kind") or "(none)")
            kd_l = col_kind_x
            kd_r = col_val_x - 6
            arcade.draw_lrbt_rectangle_filled(kd_l, kd_r, row_bot, row_top, (50, 40, 32))
            arcade.draw_lrbt_rectangle_outline(
                kd_l, kd_r, row_bot, row_top,
                COLOR_GOLD if self.scheduled_dropdown_open == f"kind:{ri}"
                else COLOR_UI_BORDER,
                2 if self.scheduled_dropdown_open == f"kind:{ri}" else 1,
            )
            kdt = sched_text(f"kd_{ri}", kd_l + 6, row_bot + 4, 10, COLOR_WHITE)
            kdt.text = kind if len(kind) <= 20 else kind[:19] + "…"
            kdt.draw()
            self._scheduled_panel_rects.append(
                (kd_l, kd_r, row_bot, row_top, f"open_kind_dd:{ri}")
            )
            # Value text input.
            val = entry.get("trigger", {}).get("value")
            val_str = "" if val is None else str(val)
            vl_l = col_val_x
            vl_r = col_rm_x - 6
            editing_val = self.scheduled_text_field == f"val:{ri}"
            arcade.draw_lrbt_rectangle_filled(vl_l, vl_r, row_bot, row_top, (22, 18, 14))
            arcade.draw_lrbt_rectangle_outline(
                vl_l, vl_r, row_bot, row_top,
                COLOR_GOLD if editing_val else COLOR_UI_BORDER,
                2 if editing_val else 1,
            )
            vlt = sched_text(f"vl_{ri}", vl_l + 6, row_bot + 4, 10,
                             COLOR_GOLD if editing_val else COLOR_WHITE)
            vlt.text = (self._scheduled_text_buffer + "_") if editing_val else val_str
            vlt.draw()
            self._scheduled_panel_rects.append(
                (vl_l, vl_r, row_bot, row_top, f"edit_val:{ri}")
            )
            # Remove button.
            rm_l = col_rm_x
            rm_r = col_rm_x + 20
            arcade.draw_lrbt_rectangle_filled(rm_l, rm_r, row_bot, row_top, (90, 40, 35))
            arcade.draw_lrbt_rectangle_outline(rm_l, rm_r, row_bot, row_top, COLOR_UI_BORDER, 1)
            rmt = sched_text(f"rm_{ri}", (rm_l + rm_r) / 2, row_bot + 4,
                             10, COLOR_WHITE, True, anchor_x="center")
            rmt.text = "x"
            rmt.draw()
            self._scheduled_panel_rects.append(
                (rm_l, rm_r, row_bot, row_top, f"remove_row:{ri}")
            )
            row_y -= self.SCHEDULED_ROW_H

        # ── Footer: Add row · Close ──────────────────────────────────
        btn_w = 110
        btn_h = 28
        btn_t = b + 38
        btn_b = btn_t - btn_h
        add_l = l + 16
        add_r = add_l + btn_w
        cl_r = r - 16
        cl_l = cl_r - btn_w
        for x1, x2, action, lbl_txt, fill in (
            (add_l, add_r, "add_row", "+ Add row", (50, 80, 50)),
            (cl_l,  cl_r,  "close",   "Close",     (60, 50, 40)),
        ):
            arcade.draw_lrbt_rectangle_filled(x1, x2, btn_b, btn_t, fill)
            arcade.draw_lrbt_rectangle_outline(x1, x2, btn_b, btn_t, COLOR_UI_BORDER, 1)
            ft = sched_text(f"foot_{action}", (x1 + x2) / 2, btn_b + 8,
                            11, COLOR_WHITE, True, anchor_x="center")
            ft.text = lbl_txt
            ft.draw()
            self._scheduled_panel_rects.append(
                (x1, x2, btn_b, btn_t, action)
            )
        # Hint about saving.
        save_hint = sched_text(
            "save_hint", cx, btn_b + 8, 9, COLOR_GRAY, anchor_x="center",
        )
        save_hint.text = "Changes persist when you Save map."
        save_hint.draw()

        # Draw any open dropdown last so it sits over the rows.
        if self.scheduled_dropdown_open:
            self._draw_scheduled_dropdown(em, sched_text, rows_l, rows_r)

    def _draw_scheduled_dropdown(
        self, em, sched_text, rows_l: float, rows_r: float,
    ) -> None:
        """Overlay the open dropdown — either event-name or trigger-
        kind. Lists clickable options stacked vertically.
        """
        kind_str = self.scheduled_dropdown_open
        if ":" not in kind_str:
            return
        which, idx_s = kind_str.split(":", 1)
        try:
            ri = int(idx_s)
        except ValueError:
            return
        if ri < 0 or ri >= len(em.scheduled):
            return
        # Pick the list of options.
        if which == "event":
            options = [e["name"] for e in em.events]
            action_prefix = f"set_event:{ri}:"
        elif which == "kind":
            options = self.SCHEDULED_TRIGGER_KINDS
            action_prefix = f"set_kind:{ri}:"
        else:
            return
        if not options:
            return
        # Position the dropdown directly under the row.
        dd_l = rows_l + 8
        dd_r = rows_l + 168 if which == "event" else rows_l + 350
        if which == "kind":
            dd_l = rows_l + 178
            dd_r = rows_l + 354
        row_height = 22
        # Compute the y of the row from the layout — we don't have it
        # cached, so recompute. rows_t in the parent is tg_b - 12; we
        # got rows_l/rows_r from the caller. Walk down to row `ri`.
        # Easier: the dropdown is drawn directly below the click rect,
        # so look up the matching rect from this draw cycle.
        dd_top = None
        for x1, x2, y1, y2, action in self._scheduled_panel_rects:
            if action == f"open_{which}_dd:{ri}":
                dd_top = y1  # row_bot of the originating dropdown
                dd_l = x1
                dd_r = x2
                break
        if dd_top is None:
            return
        dd_h = row_height * len(options)
        dd_b = dd_top - dd_h
        # Background.
        arcade.draw_lrbt_rectangle_filled(dd_l, dd_r, dd_b, dd_top, (28, 22, 18))
        arcade.draw_lrbt_rectangle_outline(dd_l, dd_r, dd_b, dd_top, COLOR_GOLD, 1)
        for oi, opt in enumerate(options):
            oy_t = dd_top - oi * row_height
            oy_b = oy_t - row_height
            arcade.draw_lrbt_rectangle_outline(
                dd_l, dd_r, oy_b, oy_t, COLOR_UI_BORDER, 1,
            )
            ot = sched_text(
                f"dd_{which}_{ri}_{oi}", dd_l + 6, oy_b + 4,
                10, COLOR_WHITE,
            )
            label = opt if len(opt) <= 22 else opt[:21] + "…"
            ot.text = label
            ot.draw()
            self._scheduled_panel_rects.append(
                (dd_l, dd_r, oy_b, oy_t, action_prefix + opt)
            )

    def _scheduled_panel_handle_click(self, x: int, y: int) -> bool:
        """Hit-test the scheduled-events panel. Returns True if the
        click was consumed."""
        for x1, x2, y1, y2, action in self._scheduled_panel_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                # Clicking somewhere else closes any open dropdown
                # or text-edit unless we're clicking back into the
                # same one.
                if (
                    self.scheduled_dropdown_open is not None
                    and not action.startswith("set_")
                    and not action.startswith(
                        f"open_{self.scheduled_dropdown_open.split(':', 1)[0]}_dd:"
                    )
                ):
                    self.scheduled_dropdown_open = None
                if (
                    self.scheduled_text_field is not None
                    and action != self.scheduled_text_field.replace("val:", "edit_val:")
                ):
                    self._scheduled_commit_value()
                self._scheduled_panel_dispatch(action)
                return True
        # Click outside any rect — close dropdown / commit text.
        if self.scheduled_dropdown_open is not None:
            self.scheduled_dropdown_open = None
        if self.scheduled_text_field is not None:
            self._scheduled_commit_value()
        return True  # eat the click so it doesn't fall through to world

    def _scheduled_commit_value(self) -> None:
        """Commit whatever's in the text buffer to the matching row's
        trigger value. Called when the player tabs away from the
        field, presses Enter, or clicks outside it."""
        field = self.scheduled_text_field
        if field is None:
            return
        try:
            ri = int(field.split(":", 1)[1])
        except (ValueError, IndexError):
            self.scheduled_text_field = None
            self._scheduled_text_buffer = ""
            return
        em = getattr(self, "event_manager", None)
        if em is not None and 0 <= ri < len(em.scheduled):
            entry = em.scheduled[ri]
            buf = (self._scheduled_text_buffer or "").strip()
            trig = entry.setdefault("trigger", {})
            if buf == "":
                trig.pop("value", None)
            else:
                # Trigger values are integers (year / month / tick /
                # population / treasury). Reject non-numeric input
                # silently by leaving the previous value alone.
                try:
                    trig["value"] = int(buf)
                except ValueError:
                    self._notify("Trigger value must be a whole number", COLOR_RED)
        self.scheduled_text_field = None
        self._scheduled_text_buffer = ""

    def _scheduled_panel_dispatch(self, action: str) -> None:
        em = getattr(self, "event_manager", None)
        if em is None:
            return
        if action == "close":
            self.show_scheduled_panel = False
            self.scheduled_dropdown_open = None
            if self.scheduled_text_field is not None:
                self._scheduled_commit_value()
            return
        if action == "toggle_random":
            em.disable_random_events = not em.disable_random_events
            log.info(
                "Scheduled panel: disable_random_events → %s",
                em.disable_random_events,
            )
            return
        if action == "add_row":
            # Seed with the first available event and the first kind.
            default_event = em.events[0]["name"] if em.events else None
            em.scheduled.append({
                "event": default_event,
                "trigger": {"kind": "at_year", "value": 1},
                "fired": False,
            })
            return
        if action.startswith("remove_row:"):
            ri = int(action.split(":", 1)[1])
            if 0 <= ri < len(em.scheduled):
                del em.scheduled[ri]
            return
        if action.startswith("open_event_dd:"):
            ri = int(action.split(":", 1)[1])
            tag = f"event:{ri}"
            self.scheduled_dropdown_open = (
                None if self.scheduled_dropdown_open == tag else tag
            )
            return
        if action.startswith("open_kind_dd:"):
            ri = int(action.split(":", 1)[1])
            tag = f"kind:{ri}"
            self.scheduled_dropdown_open = (
                None if self.scheduled_dropdown_open == tag else tag
            )
            return
        if action.startswith("set_event:"):
            _, idx_s, name = action.split(":", 2)
            ri = int(idx_s)
            if 0 <= ri < len(em.scheduled):
                em.scheduled[ri]["event"] = name
                em.scheduled[ri]["fired"] = False  # reset firing state
            self.scheduled_dropdown_open = None
            return
        if action.startswith("set_kind:"):
            _, idx_s, kind = action.split(":", 2)
            ri = int(idx_s)
            if 0 <= ri < len(em.scheduled):
                trig = em.scheduled[ri].setdefault("trigger", {})
                trig["kind"] = kind
                em.scheduled[ri]["fired"] = False
            self.scheduled_dropdown_open = None
            return
        if action.startswith("edit_val:"):
            ri = int(action.split(":", 1)[1])
            self.scheduled_text_field = f"val:{ri}"
            em_entry = em.scheduled[ri] if 0 <= ri < len(em.scheduled) else {}
            val = em_entry.get("trigger", {}).get("value")
            self._scheduled_text_buffer = "" if val is None else str(val)
            return
        log.warning("Scheduled panel: unknown action %r", action)

    def _scheduled_text_handle_key(
        self, symbol: int, modifiers: int,
    ) -> bool:
        """Buffer mutation for the scheduled panel's value field.
        Only accepts digits and a leading minus (population values
        are non-negative, but the field accepts whatever — the
        commit path int-parses and rejects malformed input)."""
        if self.scheduled_text_field is None:
            return False
        if symbol in (arcade.key.ENTER, arcade.key.RETURN, arcade.key.NUM_ENTER):
            self._scheduled_commit_value()
            return True
        if symbol == arcade.key.ESCAPE:
            self.scheduled_text_field = None
            self._scheduled_text_buffer = ""
            return True
        if symbol == arcade.key.BACKSPACE:
            self._scheduled_text_buffer = (
                self._scheduled_text_buffer or ""
            )[:-1]
            return True
        ch = None
        if arcade.key.KEY_0 <= symbol <= arcade.key.KEY_9:
            ch = chr(ord("0") + symbol - arcade.key.KEY_0)
        elif symbol == arcade.key.MINUS:
            ch = "-"
        if ch is not None and len(self._scheduled_text_buffer or "") < 12:
            self._scheduled_text_buffer = (
                self._scheduled_text_buffer or ""
            ) + ch
            return True
        return False
