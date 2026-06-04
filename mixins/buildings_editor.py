"""Buildings (sprite/registry) editor — the modal that lets the player
tweak cost / workers / housing / storage / production / consumption
on any building and write back to data/buildings.json.

Extracted from game_window.py for clarity (~1450 lines). The methods
read and mutate `self.editor_*` state on CaesarGameWindow; the class
also holds the layout constants (EDITOR_PANEL_W etc.) the methods
reference via `self.`."""
from __future__ import annotations

import json
import logging

import arcade

from building import BuildingRegistry
from constants import (
    BUILDINGS_PATH,
    COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_RED,
    COLOR_UI_BORDER, COLOR_WHITE,
    DATA_DIR,
)

log = logging.getLogger("caesar3.window")

class BuildingsEditorMixin:
    """BuildingsEditorMixin — see module docstring."""

    # ══════════════════════════════════════════════════════════════════════
    #  BUILDINGS EDITOR  (v0.15)
    # ══════════════════════════════════════════════════════════════════════
    # The editor is a modal panel that lets the player tweak four
    # scalar fields per building — cost, workers, housing, storage —
    # and write the changes back to data/buildings.json. Reload of
    # the running game's registry is automatic on save, so the next
    # tick's economy uses the new numbers without restarting.
    #
    # Why only four fields, not full editing? Production / consumption
    # / category / size all involve per-resource sub-keys or careful
    # gameplay rules; getting them right needs proper text input and
    # validation that's out of scope for the simple-interface ask.
    # The four scalars are the most common balance knobs and they
    # cover ~90% of "I want to nerf the granary" / "make houses
    # cheaper" tweaks the player would reach for.
    #
    # The interface is fully click-driven: − / + buttons step each
    # value, no text entry needed. Step sizes are field-aware
    # (cost steps by 10, workers/housing/storage by 1) so the player
    # can move quickly without holding the button down.
    EDITOR_PANEL_W = 920
    # v0.19.x: bumped 600 → 660 to fit the new image preview, sprite
    # filename text input, size editor, and per-resource production /
    # consumption rows (was: a single primary entry each).
    # v0.27: bumped 660 → 720. The scalar rows grew from 3 to 5
    # buttons (-50 / -10 / val / +10 / +50) and the Production /
    # Consumption rows did too. The pane height is now CAPPED at
    # 120 px in the body of _draw_editor instead of flooding all
    # the way to the footer, so 720 px is enough vertical room
    # without wasted dead space — the bakery screenshot showed a
    # giant empty Production pane the player flagged as "too big".
    EDITOR_PANEL_H = 720
    EDITOR_LIST_W = 200
    EDITOR_ROW_H = 22
    # v0.27: strict uniform ±10 / ±50 across every editable field, per
    # player request. Pre-v0.27 the steps were field-aware (cost: 10,
    # workers: 1, housing: 1, storage: 50, construction_ticks: 5) which
    # was nicer for low-magnitude fields but inconsistent. The two
    # numbers are the step magnitudes for the minor and major
    # buttons; the editor renders -EDITOR_STEP_MAJOR / -EDITOR_STEP_MINOR
    # / value / +EDITOR_STEP_MINOR / +EDITOR_STEP_MAJOR. Step values are
    # global, not per-field, so this dict is no longer consulted by
    # the click dispatcher — it's kept for back-compat with any test
    # that reads EDITOR_FIELD_STEPS to compose an expected delta.
    # v0.35: added a third step magnitude (±1) so workers/storage/etc.
    # can be tweaked one unit at a time. The bakery defaults to 3
    # workers; ±10 jumps to 0 or 13, which overshot. Layout now shows
    # -50 -10 -1 [val] +1 +10 +50.
    EDITOR_STEP_MINOR = 10
    EDITOR_STEP_MAJOR = 50
    EDITOR_STEP_UNIT  = 1
    EDITOR_FIELD_STEPS: dict[str, int] = {
        "cost": 10, "workers": 10, "housing": 10, "storage": 10,
        "construction_ticks": 10,
    }
    EDITOR_FIELD_LABELS: dict[str, str] = {
        "cost": "Cost (gold)",
        "workers": "Workers",
        "housing": "Housing capacity",
        "storage": "Storage",
        "construction_ticks": "Construction (ticks)",
    }

    def _draw_editor(self) -> None:
        """Render the buildings-editor modal — v0.19.x rewrite.

        The form has four panes (left to right):

          * Building list (scrollable, single-select).
          * Sprite preview + filename text input.
          * Scalars (cost / workers / housing / storage / size) and
            scrollable input/output rows.
          * Footer: Cancel / Save.

        Layout is laid out fresh each frame and hit-test rects are
        appended to ``_editor_btn_rects`` for ``on_mouse_press`` to
        consume on the next click."""
        self._editor_btn_rects = []
        # Dim background.
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 170),
        )
        # Centred panel.
        cx = self.width / 2
        cy = self.height / 2
        pw = self.EDITOR_PANEL_W
        ph = self.EDITOR_PANEL_H
        l = cx - pw / 2
        r = cx + pw / 2
        b = cy - ph / 2
        t = cy + ph / 2
        # Clamp to screen.
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
        # Title.
        if not hasattr(self, "_txt_editor_title"):
            self._txt_editor_title = arcade.Text(
                "BUILDINGS EDITOR", 0, 0, COLOR_GOLD, 14, bold=True,
                anchor_x="center",
            )
            self._txt_editor_hint = arcade.Text(
                "Tweak fields and Save to write to data/buildings.json",
                0, 0, COLOR_GRAY, 10, anchor_x="center",
            )
        self._txt_editor_title.x = cx
        self._txt_editor_title.y = t - 26
        self._txt_editor_title.draw()
        self._txt_editor_hint.x = cx
        self._txt_editor_hint.y = t - 44
        self._txt_editor_hint.draw()

        # ── Left column: building list ────────────────────────────────
        list_l = l + 14
        list_r = list_l + self.EDITOR_LIST_W
        list_t = t - 64
        list_b = b + 60
        arcade.draw_lrbt_rectangle_filled(
            list_l, list_r, list_b, list_t, (22, 18, 14, 230),
        )
        arcade.draw_lrbt_rectangle_outline(
            list_l, list_r, list_b, list_t, COLOR_UI_BORDER, 1,
        )
        rows_visible = int((list_t - list_b) / self.EDITOR_ROW_H)
        # v0.33: sort by the building's *display name* (case-insensitive)
        # rather than by id. The list shows names, so id-sort surfaced
        # confusing orderings like "Cavalry Fort" appearing under 'F'
        # because its id is ``fort_cavalry``. Tie-break on id so the
        # order is deterministic when two buildings share a name (the
        # only current case is the ``workshop`` alias, which is excluded
        # below anyway). ``name`` access goes through the registry so a
        # rename via the editor immediately re-sorts on the next frame.
        def _name_key(bid: str) -> tuple[str, str]:
            bd = self.registry.get(bid)
            name = bd.name if bd is not None else bid
            return (name.lower(), bid)
        all_ids = sorted(
            (bid for bid in self.registry.all_ids()
             if bid not in ("empty", "workshop")),
            key=_name_key,
        )
        max_scroll = max(0, len(all_ids) - rows_visible)
        self.editor_scroll = max(0, min(self.editor_scroll, max_scroll))
        # v0.30: stash list-state for the keyboard navigation handler
        # (`_editor_handle_nav_key`) and the mouse-wheel handler so they
        # don't have to re-compute these values.
        self._editor_list_rows_visible = rows_visible
        self._editor_list_all_ids = all_ids
        self._editor_list_max_scroll = max_scroll
        # Scroll arrows.
        scroll_btn_w = 20
        scroll_x1 = list_r + 2
        scroll_x2 = scroll_x1 + scroll_btn_w
        arcade.draw_lrbt_rectangle_filled(
            scroll_x1, scroll_x2, list_t - 22, list_t, (60, 50, 40),
        )
        arcade.draw_lrbt_rectangle_outline(
            scroll_x1, scroll_x2, list_t - 22, list_t, COLOR_UI_BORDER, 1,
        )
        if not hasattr(self, "_txt_editor_scroll_up"):
            self._txt_editor_scroll_up = arcade.Text(
                "^", 0, 0, COLOR_WHITE, 12, bold=True, anchor_x="center",
            )
            self._txt_editor_scroll_dn = arcade.Text(
                "v", 0, 0, COLOR_WHITE, 12, bold=True, anchor_x="center",
            )
        self._txt_editor_scroll_up.x = (scroll_x1 + scroll_x2) / 2
        self._txt_editor_scroll_up.y = list_t - 16
        self._txt_editor_scroll_up.draw()
        self._editor_btn_rects.append(
            (scroll_x1, scroll_x2, list_t - 22, list_t, "scroll_up")
        )
        arcade.draw_lrbt_rectangle_filled(
            scroll_x1, scroll_x2, list_b, list_b + 22, (60, 50, 40),
        )
        arcade.draw_lrbt_rectangle_outline(
            scroll_x1, scroll_x2, list_b, list_b + 22, COLOR_UI_BORDER, 1,
        )
        self._txt_editor_scroll_dn.x = (scroll_x1 + scroll_x2) / 2
        self._txt_editor_scroll_dn.y = list_b + 6
        self._txt_editor_scroll_dn.draw()
        self._editor_btn_rects.append(
            (scroll_x1, scroll_x2, list_b, list_b + 22, "scroll_down")
        )
        # v0.30: scrollbar track + proportional thumb between the
        # ^/v arrows. Clicking the track above/below the thumb does a
        # page-jump; the thumb itself can be dragged via the new
        # on_mouse_drag handler (see ``_editor_scrollbar_drag`` state).
        # Track spans from just below the up-arrow to just above the
        # down-arrow.
        track_t = list_t - 22
        track_b = list_b + 22
        track_h = max(0, track_t - track_b)
        arcade.draw_lrbt_rectangle_filled(
            scroll_x1, scroll_x2, track_b, track_t, (40, 32, 26),
        )
        arcade.draw_lrbt_rectangle_outline(
            scroll_x1, scroll_x2, track_b, track_t, COLOR_UI_BORDER, 1,
        )
        # Thumb size scales with visible/total ratio. If everything
        # fits (max_scroll == 0), the thumb fills the whole track and
        # neither dragging nor page-jumping does anything.
        n_total = max(1, len(all_ids))
        thumb_h_ratio = min(1.0, rows_visible / n_total)
        thumb_h = max(16, int(track_h * thumb_h_ratio))
        if max_scroll > 0:
            thumb_offset = int(
                (track_h - thumb_h) * (self.editor_scroll / max_scroll)
            )
        else:
            thumb_offset = 0
        thumb_t = track_t - thumb_offset
        thumb_b = thumb_t - thumb_h
        arcade.draw_lrbt_rectangle_filled(
            scroll_x1 + 2, scroll_x2 - 2, thumb_b, thumb_t, (140, 115, 80),
        )
        arcade.draw_lrbt_rectangle_outline(
            scroll_x1 + 2, scroll_x2 - 2, thumb_b, thumb_t, COLOR_UI_BORDER, 1,
        )
        # Stash for click / drag / wheel dispatch.
        self._editor_scrollbar_rect = (
            scroll_x1, scroll_x2, track_b, track_t,
            thumb_b, thumb_t, max_scroll, track_h, thumb_h,
        )
        if not hasattr(self, "_txt_editor_rows"):
            self._txt_editor_rows = []
        while len(self._txt_editor_rows) < rows_visible:
            self._txt_editor_rows.append(
                arcade.Text("", 0, 0, COLOR_WHITE, 11),
            )
        for vi in range(rows_visible):
            idx = self.editor_scroll + vi
            if idx >= len(all_ids):
                break
            bid = all_ids[idx]
            row_top = list_t - vi * self.EDITOR_ROW_H
            row_bot = row_top - self.EDITOR_ROW_H
            selected = (bid == self.editor_selected_id)
            if selected:
                arcade.draw_lrbt_rectangle_filled(
                    list_l, list_r, row_bot, row_top, (90, 70, 50, 220),
                )
            bd = self.registry.get(bid)
            label = bd.name if bd is not None else bid
            txt = self._txt_editor_rows[vi]
            txt.text = f"  {label}"
            txt.color = COLOR_GOLD if selected else COLOR_WHITE
            txt.x = list_l + 4
            txt.y = row_bot + 5
            txt.draw()
            self._editor_btn_rects.append(
                (list_l, list_r, row_bot, row_top, f"select:{bid}")
            )

        # ── Right pane: form ──────────────────────────────────────────
        form_l = list_r + 30
        form_r = r - 14
        form_t = list_t
        form_b = list_b
        bd = self.registry.get(self.editor_selected_id)
        if bd is None:
            return

        if not hasattr(self, "_txt_editor_form"):
            self._txt_editor_form: dict[str, arcade.Text] = {}
        def form_text(key: str, x: float, y: float, size: int = 12,
                      color=COLOR_WHITE, bold: bool = False,
                      anchor_x: str = "left") -> arcade.Text:
            t_obj = self._txt_editor_form.get(key)
            if t_obj is None:
                t_obj = arcade.Text("", x, y, color, size,
                                    bold=bold, anchor_x=anchor_x)
                self._txt_editor_form[key] = t_obj
            t_obj.x = x
            t_obj.y = y
            t_obj.color = color
            return t_obj

        # Header.
        header = form_text("header", form_l, form_t - 26, 16, COLOR_GOLD, True)
        header.text = bd.name
        header.draw()
        meta = form_text("meta", form_l, form_t - 50, 10, COLOR_GRAY)
        meta.text = (
            f"{bd.category} · {'needs road' if bd.requires_road else 'no road'}"
        )
        meta.draw()

        # ── Sprite preview ───────────────────────────────────────────
        # 96×96 thumbnail of the building's sprite, top-right of the
        # form. Falls back to a colored rectangle when no texture is
        # found (the same pattern the world renderer uses).
        prev_size = 96
        prev_l = form_r - prev_size - 8
        prev_t = form_t - 26
        prev_b = prev_t - prev_size
        prev_r = prev_l + prev_size
        arcade.draw_lrbt_rectangle_filled(
            prev_l, prev_r, prev_b, prev_t, (22, 18, 14),
        )
        arcade.draw_lrbt_rectangle_outline(
            prev_l, prev_r, prev_b, prev_t, COLOR_GOLD, 1,
        )
        try:
            tex = self.textures.building(self.editor_selected_id)
        except Exception:  # noqa: BLE001
            tex = None
        if tex is not None:
            try:
                arcade.draw_texture_rect(
                    tex,
                    arcade.LBWH(
                        prev_l + 2, prev_b + 2,
                        prev_size - 4, prev_size - 4,
                    ),
                )
            except Exception:  # noqa: BLE001 — older arcade signatures
                arcade.draw_lrbt_rectangle_filled(
                    prev_l + 2, prev_r - 2, prev_b + 2, prev_t - 2, bd.color,
                )
        else:
            # Fallback: solid colour swatch.
            arcade.draw_lrbt_rectangle_filled(
                prev_l + 4, prev_r - 4, prev_b + 4, prev_t - 4, bd.color,
            )
            no_tex = form_text(
                "no_tex", (prev_l + prev_r) / 2, (prev_b + prev_t) / 2 - 4,
                10, COLOR_WHITE, anchor_x="center",
            )
            no_tex.text = "no image"
            no_tex.draw()

        # ── Sprite filename text input ───────────────────────────────
        fname_lbl = form_text("fname_lbl", form_l, form_t - 76, 10, COLOR_WHITE, True)
        fname_lbl.text = "Sprite filename (jpg/jpeg/png):"
        fname_lbl.draw()
        fb_l = form_l
        fb_r = prev_l - 10
        fb_t = form_t - 84
        fb_b = fb_t - 22
        editing_fname = (
            getattr(self, "editor_text_input_field", None) == "filename"
        )
        arcade.draw_lrbt_rectangle_filled(fb_l, fb_r, fb_b, fb_t, (22, 18, 14))
        arcade.draw_lrbt_rectangle_outline(
            fb_l, fb_r, fb_b, fb_t,
            COLOR_GOLD if editing_fname else COLOR_UI_BORDER,
            2 if editing_fname else 1,
        )
        fb_txt = form_text("fname_val", fb_l + 6, fb_b + 5, 11,
                           COLOR_GOLD if editing_fname else COLOR_WHITE)
        # Show buffer + caret while editing; static value otherwise.
        if editing_fname:
            buf = getattr(self, "_editor_text_buffer", "") or ""
            fb_txt.text = buf + "_"
        else:
            fb_txt.text = self.editor_sprite_filename or ""
        fb_txt.draw()
        self._editor_btn_rects.append((fb_l, fb_r, fb_b, fb_t, "edit_filename"))

        # ── Size editor (W × H, 1..10) ───────────────────────────────
        # v0.27: layout fix — the W / H per-axis labels sat at sz_y
        # (same y as the "Size (tiles, 1..10):" header), which made
        # them look like they were labelling the row below. Move
        # them down by ~14 px so they sit at the vertical centre of
        # the −/value/+ button strip; the controls themselves are
        # also pushed down a few px so the header text has clear
        # space.
        sz_y = form_t - 116
        sz_lbl = form_text("sz_lbl", form_l, sz_y, 11, COLOR_WHITE, True)
        sz_lbl.text = "Size (tiles, 1..10):"
        sz_lbl.draw()
        # Width control
        sw, sh = self.editor_size
        ctl_y_b = sz_y - 32
        ctl_y_t = sz_y - 10
        for axis, label, val, action_prefix in (
            (0, "W", sw, "size_w"),
            (1, "H", sh, "size_h"),
        ):
            base_x = form_l + 140 + axis * 200
            # v0.27: label sits between ctl_y_b and ctl_y_t, not at
            # the header's y. Looks like a proper inline label now.
            albl = form_text(f"sz_axis_{axis}", base_x, ctl_y_b + 4, 11, COLOR_GRAY)
            albl.text = label
            albl.draw()
            mb_l = base_x + 18
            mb_r = mb_l + 26
            arcade.draw_lrbt_rectangle_filled(mb_l, mb_r, ctl_y_b, ctl_y_t, (60, 45, 35))
            arcade.draw_lrbt_rectangle_outline(mb_l, mb_r, ctl_y_b, ctl_y_t, COLOR_UI_BORDER, 1)
            mb_txt = form_text(f"sz_minus_{axis}", (mb_l + mb_r) / 2, ctl_y_b + 4,
                               14, COLOR_WHITE, True, anchor_x="center")
            mb_txt.text = "-"
            mb_txt.draw()
            self._editor_btn_rects.append(
                (mb_l, mb_r, ctl_y_b, ctl_y_t, f"{action_prefix}_minus")
            )
            vb_l = mb_r + 6
            vb_r = vb_l + 50
            arcade.draw_lrbt_rectangle_filled(vb_l, vb_r, ctl_y_b, ctl_y_t, (22, 18, 14))
            arcade.draw_lrbt_rectangle_outline(vb_l, vb_r, ctl_y_b, ctl_y_t, COLOR_UI_BORDER, 1)
            vb_txt = form_text(f"sz_val_{axis}", (vb_l + vb_r) / 2, ctl_y_b + 4,
                               14, COLOR_GOLD, True, anchor_x="center")
            vb_txt.text = str(val)
            vb_txt.draw()
            pb_l = vb_r + 6
            pb_r = pb_l + 26
            arcade.draw_lrbt_rectangle_filled(pb_l, pb_r, ctl_y_b, ctl_y_t, (60, 45, 35))
            arcade.draw_lrbt_rectangle_outline(pb_l, pb_r, ctl_y_b, ctl_y_t, COLOR_UI_BORDER, 1)
            pb_txt = form_text(f"sz_plus_{axis}", (pb_l + pb_r) / 2, ctl_y_b + 4,
                               14, COLOR_WHITE, True, anchor_x="center")
            pb_txt.text = "+"
            pb_txt.draw()
            self._editor_btn_rects.append(
                (pb_l, pb_r, ctl_y_b, ctl_y_t, f"{action_prefix}_plus")
            )

        # ── Scalar fields (cost / workers / housing / storage / construction) ──
        # v0.27: each row now has 5 buttons:
        #     -50  -10  [ value ]  +10  +50
        # with strict uniform steps across every field. Per-field
        # step tuning is gone (was: cost=10, workers=1, housing=1,
        # storage=50, construction=5) — the player asked for global
        # ±10/±50, and the v0.27 button row reflects that. Layout
        # numbers:
        #
        #   label x: form_l (left-aligned, ~150 px reserved)
        #   buttons start at form_l + 160 (unchanged from v0.26)
        #   major button: 36 px wide (fits "-50" / "+50" with breathing room)
        #   minor button: 30 px wide ("-10" / "+10")
        #   value box: 60 px (was 80; trimmed to keep the whole row
        #             inside the form_l..form_r side of the panel
        #             without bumping into the sprite-preview column)
        #   gaps: 4 px between buttons / value
        #
        # Total row width: 36 + 4 + 30 + 4 + 60 + 4 + 30 + 4 + 36 = 208 px,
        # comfortably inside the ~300 px available to this column.
        editable_fields: list[str] = [
            "cost", "workers", "housing", "storage", "construction_ticks",
        ]
        # v0.27: anchor field_y to the size row's bottom so panel-
        # height changes don't drift the scalars off the top of the
        # form area. ctl_y_b is the bottom of the size buttons; the
        # first scalar label sits ~20 px below.
        field_y = ctl_y_b - 20
        row_stride = 30
        major = self.EDITOR_STEP_MAJOR
        minor = self.EDITOR_STEP_MINOR
        for fi, field in enumerate(editable_fields):
            row_y = field_y - fi * row_stride
            value = self.editor_values.get(field, 0)
            mb_b = row_y - 22
            mb_t = mb_b + 20
            # v0.27: align label baseline with the value-box baseline
            # so the label "Construction (ticks)" sits at the same
            # vertical centre as its number. Pre-fix the label was
            # drawn at row_y (button-top), 2 px above the buttons,
            # which read as misaligned in the bakery screenshot.
            lbl = form_text(f"lbl_{field}", form_l, mb_b + 5, 11, COLOR_WHITE, True)
            lbl.text = self.EDITOR_FIELD_LABELS[field]
            lbl.draw()
            # Five buttons left-to-right: -50, -10, value, +10, +50.
            cursor = form_l + 160
            def _btn(left: float, width: float, label_text: str, action: str,
                     is_value_box: bool = False) -> float:
                right = left + width
                if is_value_box:
                    fill = (22, 18, 14)
                    text_color = COLOR_GOLD
                else:
                    fill = (60, 45, 35)
                    text_color = COLOR_WHITE
                arcade.draw_lrbt_rectangle_filled(left, right, mb_b, mb_t, fill)
                arcade.draw_lrbt_rectangle_outline(
                    left, right, mb_b, mb_t, COLOR_UI_BORDER, 1,
                )
                t = form_text(
                    f"{action}_{field}", (left + right) / 2, mb_b + 3, 12,
                    text_color, True, anchor_x="center",
                )
                t.text = label_text
                t.draw()
                if not is_value_box:
                    self._editor_btn_rects.append((left, right, mb_b, mb_t, action))
                return right + 4  # next cursor x
            # v0.35: seven buttons left-to-right: -50 -10 -1 [val] +1 +10 +50.
            # The previous 5-button row jumped Workers (default 3) by ±10,
            # which overshot to 0 or 13. ±1 lets the player tune unit by unit.
            unit = self.EDITOR_STEP_UNIT
            # -50 (major)
            cursor = _btn(cursor, 36, f"-{major}", f"minus_major:{field}")
            # -10 (minor)
            cursor = _btn(cursor, 30, f"-{minor}", f"minus_minor:{field}")
            # -1 (unit)
            cursor = _btn(cursor, 24, f"-{unit}", f"minus_unit:{field}")
            # value
            cursor = _btn(cursor, 50, str(value), f"value:{field}", is_value_box=True)
            # +1 (unit)
            cursor = _btn(cursor, 24, f"+{unit}", f"plus_unit:{field}")
            # +10 (minor)
            cursor = _btn(cursor, 30, f"+{minor}", f"plus_minor:{field}")
            # +50 (major)
            cursor = _btn(cursor, 36, f"+{major}", f"plus_major:{field}")

        # ── Production / consumption / material-cost rows ──────────
        # Two side-by-side panes (Production / Consumption) on the top
        # row, plus a third "Build materials" pane spanning the full
        # width below them. Each row: resource id + qty + +/- + remove.
        # A trailing "Add..." button opens the resource picker.
        #
        # v0.33: added the Materials pane for ``Building.material_cost``
        # — the *non-gold* one-off cost the placement deducts from the
        # stockpile (planks, stone, horses, ...). Pre-v0.33 the field
        # was edit-only-via-JSON; now it's surfaced in the editor next
        # to the other resource flows so a modder shipping a
        # cavalry-fort variant can tell the player "this building costs
        # 1 horse on placement" without diving into the JSON file.
        #
        # Layout rationale: the obvious three-side-by-side arrangement
        # doesn't fit — each pane needs ~290 px and we only have ~660
        # of form width. Stacking Materials *below* P+C keeps each row
        # at its natural width, and the Materials pane gets a wider
        # canvas (the full P+C span) since material lists are usually
        # short (1-2 entries) and benefit more from horizontal real
        # estate than vertical.
        #
        # v0.27 had the bug where the row anchor was hard-coded to
        # four scalar fields; v0.25 added a fifth. Using
        # ``len(editable_fields)`` (5 in v0.33) keeps the layout
        # self-correcting if more scalars get added.
        rows_top = field_y - len(editable_fields) * row_stride - 12
        pane_w = 290
        # v0.37: bumped 80 → 110 so production/consumption rows actually
        # render. The v0.33 tightening to 80 was wrong: the per-row break
        # condition (`row_y - row_h < bot_y + 28` with row_y starting at
        # top_y - 38 and row_h = 22) needs at least 88 px of pane height
        # for one row to clear. With 80, every pane silently rendered as
        # "Add resource..." only — no existing rows ever appeared.
        # 110 px fits two rows comfortably (covers ~90 % of buildings —
        # bakery: bread+(flour,wood); the dense outliers truncate the
        # third row with the existing break, which is the intended
        # behaviour). Materials pane below still has its full
        # pane_h_max for its (usually short) row list.
        pane_h_max = 110
        prod_l = form_l
        prod_r = prod_l + pane_w
        cons_l = prod_r + 16
        cons_r = cons_l + pane_w
        rows_bot = max(b + 60, rows_top - pane_h_max)
        self._editor_draw_resource_pane(
            "Production", self.editor_production,
            prod_l, prod_r, rows_top, rows_bot,
            "prod", COLOR_GREEN, form_text,
        )
        self._editor_draw_resource_pane(
            "Consumption", self.editor_consumption,
            cons_l, cons_r, rows_top, rows_bot,
            "cons", COLOR_RED, form_text,
        )
        # v0.33: Materials pane sits below the P/C row. Spans the full
        # width of the P+C strip so the player has room for several
        # build-material entries on a single line.
        mat_top = rows_bot - 8
        mat_bot = max(b + 60, mat_top - pane_h_max)
        self._editor_draw_resource_pane(
            "Build materials", self.editor_material_cost,
            prod_l, cons_r, mat_top, mat_bot,
            "mat", COLOR_GOLD, form_text,
        )

        # ── Footer: Cancel · Save ──────────────────────────────────
        btn_w = 110
        btn_h = 30
        btn_y_top = b + 40
        btn_y_bot = btn_y_top - btn_h
        save_x_r = r - 24
        save_x_l = save_x_r - btn_w
        cancel_x_r = save_x_l - 16
        cancel_x_l = cancel_x_r - btn_w
        arcade.draw_lrbt_rectangle_filled(
            cancel_x_l, cancel_x_r, btn_y_bot, btn_y_top, (50, 40, 35),
        )
        arcade.draw_lrbt_rectangle_outline(
            cancel_x_l, cancel_x_r, btn_y_bot, btn_y_top, COLOR_UI_BORDER, 1,
        )
        cancel_txt = form_text(
            "btn_cancel", (cancel_x_l + cancel_x_r) / 2, btn_y_bot + 8,
            12, COLOR_WHITE, True, anchor_x="center",
        )
        cancel_txt.text = "Cancel"
        cancel_txt.draw()
        self._editor_btn_rects.append(
            (cancel_x_l, cancel_x_r, btn_y_bot, btn_y_top, "cancel")
        )
        save_fill = (90, 70, 30) if self.editor_dirty else (50, 40, 35)
        arcade.draw_lrbt_rectangle_filled(
            save_x_l, save_x_r, btn_y_bot, btn_y_top, save_fill,
        )
        arcade.draw_lrbt_rectangle_outline(
            save_x_l, save_x_r, btn_y_bot, btn_y_top,
            COLOR_GOLD if self.editor_dirty else COLOR_UI_BORDER,
            2 if self.editor_dirty else 1,
        )
        save_txt = form_text(
            "btn_save", (save_x_l + save_x_r) / 2, btn_y_bot + 8,
            12, COLOR_GOLD if self.editor_dirty else COLOR_WHITE,
            True, anchor_x="center",
        )
        save_txt.text = "Save" + (" *" if self.editor_dirty else "")
        save_txt.draw()
        self._editor_btn_rects.append(
            (save_x_l, save_x_r, btn_y_bot, btn_y_top, "save")
        )

        # Resource picker overlay (drawn last so it sits on top).
        if getattr(self, "editor_picker_open", None):
            self._editor_draw_resource_picker(form_text)

    def _editor_draw_resource_pane(
        self, title: str, items: dict, l: float, r: float,
        top_y: float, bot_y: float, action_prefix: str,
        accent_color, form_text,
    ) -> None:
        """Draw a Production or Consumption pane: header + one row
        per resource + an Add button.

        v0.27: each row gets the same 5-button strip as the scalar
        fields above:  -50  -10  [val]  +10  +50  [×].  The row
        layout is tighter than the v0.26 3-button-plus-remove because
        we're squeezing more controls in — labels truncate at 12
        chars, buttons are 26 px wide. ±10 / ±50 routes through the
        editor's EDITOR_STEP_MINOR / EDITOR_STEP_MAJOR for the same
        uniform step the scalar rows use.
        """
        arcade.draw_lrbt_rectangle_filled(l, r, bot_y, top_y, (28, 22, 18))
        arcade.draw_lrbt_rectangle_outline(l, r, bot_y, top_y, COLOR_UI_BORDER, 1)
        # Header
        hdr = form_text(
            f"{action_prefix}_hdr", (l + r) / 2, top_y - 18,
            12, accent_color, True, anchor_x="center",
        )
        hdr.text = title
        hdr.draw()
        # Rows.
        row_y = top_y - 38
        row_h = 22
        for rid, qty in sorted(items.items()):
            if row_y - row_h < bot_y + 28:
                break  # ran out of space; remaining rows hidden
            # v0.27: label baseline aligned with button text. Label
            # truncates to keep the 5-button strip from spilling out
            # of the pane.
            row_b = row_y - 18
            row_t = row_y
            lbl_text = rid if len(rid) <= 12 else rid[:11] + "…"
            lbl = form_text(
                f"{action_prefix}_lbl_{rid}", l + 6, row_b + 3,
                10, COLOR_WHITE,
            )
            lbl.text = lbl_text
            lbl.draw()
            # Five buttons + remove. Layout left-to-right starting
            # at l + 90 (label column is 84 px wide).
            cursor = l + 90
            def _rb(left: float, width: float, label_text: str, action: str,
                    fill_color, text_color) -> float:
                right = left + width
                arcade.draw_lrbt_rectangle_filled(
                    left, right, row_b, row_t, fill_color,
                )
                arcade.draw_lrbt_rectangle_outline(
                    left, right, row_b, row_t, COLOR_UI_BORDER, 1,
                )
                t = form_text(
                    f"{action_prefix}_{action}_{rid}",
                    (left + right) / 2, row_b + 3,
                    10, text_color, True, anchor_x="center",
                )
                t.text = label_text
                t.draw()
                if not action.startswith("val"):
                    self._editor_btn_rects.append(
                        (left, right, row_b, row_t, f"{action_prefix}_{action}:{rid}")
                    )
                return right + 3
            # v0.35: 8-button strip with ±1 buttons to match the scalar rows.
            cursor = _rb(cursor, 22, "-50", "minus_major", (60, 45, 35), COLOR_WHITE)
            cursor = _rb(cursor, 22, "-10", "minus_minor", (60, 45, 35), COLOR_WHITE)
            cursor = _rb(cursor, 18, "-1",  "minus_unit",  (60, 45, 35), COLOR_WHITE)
            cursor = _rb(cursor, 30, str(qty), "val",  (22, 18, 14), COLOR_GOLD)
            cursor = _rb(cursor, 18, "+1",  "plus_unit",   (60, 45, 35), COLOR_WHITE)
            cursor = _rb(cursor, 22, "+10", "plus_minor",  (60, 45, 35), COLOR_WHITE)
            cursor = _rb(cursor, 22, "+50", "plus_major",  (60, 45, 35), COLOR_WHITE)
            cursor = _rb(cursor, 22, "x",   "remove",      (90, 40, 35), COLOR_WHITE)
            row_y -= row_h
        # Add button at the bottom of the pane.
        ab_l = l + 8
        ab_r = r - 8
        ab_t = bot_y + 26
        ab_b = bot_y + 4
        arcade.draw_lrbt_rectangle_filled(ab_l, ab_r, ab_b, ab_t, (50, 60, 40))
        arcade.draw_lrbt_rectangle_outline(ab_l, ab_r, ab_b, ab_t, accent_color, 1)
        a_txt = form_text(
            f"{action_prefix}_add", (ab_l + ab_r) / 2, ab_b + 5,
            10, COLOR_WHITE, True, anchor_x="center",
        )
        a_txt.text = f"+ Add {title.lower()} resource..."
        a_txt.draw()
        self._editor_btn_rects.append(
            (ab_l, ab_r, ab_b, ab_t, f"{action_prefix}_add")
        )

    def _editor_draw_resource_picker(self, form_text) -> None:
        """Modal picker showing every known resource id; clicking one
        adds a row with qty=1 to the open pane (production /
        consumption)."""
        side = self.editor_picker_open
        # All known resource ids: union of bartering prices +
        # building production/consumption keys.
        try:
            from bartering import STOCK_PRICES as _SP
            known = set(_SP.keys())
        except Exception:  # noqa: BLE001
            known = set()
        for bd in self.registry.all().values():
            known.update(bd.production.keys())
            known.update(bd.consumption.keys())
        known.discard("happiness")  # services, not bartered goods
        known.discard("money")      # treasury flow, not a tradeable
        # Don't show what's already in the target dict.
        if side == "prod":
            target = self.editor_production
        elif side == "cons":
            target = self.editor_consumption
        else:  # "mat" — v0.33
            target = self.editor_material_cost
        choices = sorted(k for k in known if k not in target)
        # Centred panel.
        pw = 280
        ph = min(420, 60 + 22 * max(1, len(choices)))
        cx = self.width / 2
        cy = self.height / 2
        l = cx - pw / 2
        r = cx + pw / 2
        b = cy - ph / 2
        t = cy + ph / 2
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 250))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)
        hdr = form_text(
            "picker_hdr", cx, t - 22, 12, COLOR_GOLD, True, anchor_x="center",
        )
        # v0.33: third side ("mat") for build-materials.
        side_label = {
            "prod": "production",
            "cons": "consumption",
            "mat":  "build materials",
        }.get(side, str(side))
        hdr.text = f"Add resource ({side_label})"
        hdr.draw()
        row_h = 22
        row_y = t - 44
        for rid in choices:
            if row_y - row_h < b + 8:
                break
            arcade.draw_lrbt_rectangle_filled(l + 8, r - 8, row_y - row_h, row_y, (50, 40, 32))
            arcade.draw_lrbt_rectangle_outline(l + 8, r - 8, row_y - row_h, row_y, COLOR_UI_BORDER, 1)
            t_obj = form_text(
                f"picker_{side}_{rid}", l + 14, row_y - 16, 11, COLOR_WHITE,
            )
            t_obj.text = rid
            t_obj.draw()
            self._editor_btn_rects.append(
                (l + 8, r - 8, row_y - row_h, row_y, f"picker_pick:{rid}")
            )
            row_y -= row_h
        # Cancel.
        cb_l = cx - 50
        cb_r = cx + 50
        cb_b = b + 8
        cb_t = cb_b + 22
        arcade.draw_lrbt_rectangle_filled(cb_l, cb_r, cb_b, cb_t, (60, 40, 35))
        arcade.draw_lrbt_rectangle_outline(cb_l, cb_r, cb_b, cb_t, COLOR_UI_BORDER, 1)
        c_txt = form_text(
            "picker_cancel", cx, cb_b + 5, 11, COLOR_WHITE, True, anchor_x="center",
        )
        c_txt.text = "Cancel"
        c_txt.draw()
        self._editor_btn_rects.append(
            (cb_l, cb_r, cb_b, cb_t, "picker_cancel")
        )

    def _editor_handle_nav_key(self, symbol: int) -> bool:
        """v0.30: keyboard navigation for the buildings-editor list.

        Handles Up / Down (step 1), PgUp / PgDn (step a page), Home /
        End (jump to first / last). Each step both reselects the
        current building (so the right-hand form refreshes) and
        adjusts ``editor_scroll`` so the selection stays visible.

        Returns True if the key was handled (and the caller should
        skip the rest of on_key_press), False otherwise.
        """
        all_ids = getattr(self, "_editor_list_all_ids", None)
        if not all_ids:
            return False
        rows_visible = max(1, getattr(self, "_editor_list_rows_visible", 5))
        max_scroll = getattr(self, "_editor_list_max_scroll", 0)
        # Find current index in the sorted list. If the selection
        # isn't in the list (shouldn't happen, but be defensive),
        # start at 0.
        try:
            cur = all_ids.index(self.editor_selected_id)
        except ValueError:
            cur = 0
        new_idx = None
        if symbol == arcade.key.UP:
            new_idx = max(0, cur - 1)
        elif symbol == arcade.key.DOWN:
            new_idx = min(len(all_ids) - 1, cur + 1)
        elif symbol == arcade.key.PAGEUP:
            new_idx = max(0, cur - rows_visible)
        elif symbol == arcade.key.PAGEDOWN:
            new_idx = min(len(all_ids) - 1, cur + rows_visible)
        elif symbol == arcade.key.HOME:
            new_idx = 0
        elif symbol == arcade.key.END:
            new_idx = len(all_ids) - 1
        else:
            return False
        new_id = all_ids[new_idx]
        if new_id != self.editor_selected_id:
            self.editor_selected_id = new_id
            self._editor_snapshot_for(new_id)
            self.editor_dirty = False
        # Keep the selection visible. If above the viewport, scroll up
        # to put it on the first visible row; if below, scroll down to
        # put it on the last visible row.
        if new_idx < self.editor_scroll:
            self.editor_scroll = new_idx
        elif new_idx >= self.editor_scroll + rows_visible:
            self.editor_scroll = new_idx - rows_visible + 1
        self.editor_scroll = max(0, min(max_scroll, self.editor_scroll))
        return True

    def _editor_handle_click(self, x: int, y: int) -> bool:
        """Hit-test the editor's button rects. Returns True if the
        click was handled (and the caller should not fall through
        to world-click handling)."""
        for x1, x2, y1, y2, action in self._editor_btn_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                # v0.19.x: clicking the filename field re-focuses it;
                # clicking ANY OTHER button while filename input is
                # active drops the input mode (the click still fires
                # its own action). This matches what most desktop
                # forms do — focus follows clicks.
                if (
                    getattr(self, "editor_text_input_field", None) == "filename"
                    and action != "edit_filename"
                ):
                    self.editor_text_input_field = None
                    self._editor_text_buffer = ""
                self._editor_dispatch(action)
                return True
        # Click outside any button — if the filename input is active,
        # cancel it. Otherwise the click is absorbed (the modal eats
        # all clicks while open).
        if getattr(self, "editor_text_input_field", None) == "filename":
            self.editor_text_input_field = None
            self._editor_text_buffer = ""
        return True

    def _editor_dispatch(self, action: str) -> None:
        if action == "cancel":
            self._editor_close(commit=False)
            return
        if action == "save":
            self._editor_close(commit=True)
            return
        if action == "scroll_up":
            self.editor_scroll = max(0, self.editor_scroll - 1)
            return
        if action == "scroll_down":
            self.editor_scroll += 1   # _draw_editor clamps next frame
            return
        if action.startswith("select:"):
            new_id = action.split(":", 1)[1]
            if new_id != self.editor_selected_id:
                self.editor_selected_id = new_id
                self._editor_snapshot_for(new_id)
                self.editor_dirty = False
            return
        # v0.27: new 5-button row actions. Old 3-button row used
        # "minus:{field}" / "plus:{field}" with a per-field step from
        # EDITOR_FIELD_STEPS; the new row uses
        # "minus_major" / "minus_minor" / "plus_minor" / "plus_major"
        # with strict uniform EDITOR_STEP_MINOR / EDITOR_STEP_MAJOR
        # magnitudes. The legacy 3-button branch below stays for
        # back-compat with any caller still firing the old action
        # names — tests that read EDITOR_FIELD_STEPS to compose a
        # delta hit the legacy path.
        if (
            action.startswith("minus_major:")
            or action.startswith("minus_minor:")
            or action.startswith("minus_unit:")
            or action.startswith("plus_unit:")
            or action.startswith("plus_minor:")
            or action.startswith("plus_major:")
        ):
            kind, field = action.split(":", 1)
            if kind == "minus_major":
                delta = -self.EDITOR_STEP_MAJOR
            elif kind == "minus_minor":
                delta = -self.EDITOR_STEP_MINOR
            elif kind == "minus_unit":
                delta = -self.EDITOR_STEP_UNIT
            elif kind == "plus_unit":
                delta = self.EDITOR_STEP_UNIT
            elif kind == "plus_minor":
                delta = self.EDITOR_STEP_MINOR
            else:
                delta = self.EDITOR_STEP_MAJOR
            cur = self.editor_values.get(field, 0)
            new_val = max(0, cur + delta)
            if new_val != cur:
                self.editor_values[field] = new_val
                self.editor_dirty = True
            return
        if action.startswith("minus:") or action.startswith("plus:"):
            # Legacy 3-button row — kept for the unit editor and any
            # test that reads EDITOR_FIELD_STEPS. Buildings editor
            # buttons now route through the major/minor branch above.
            sign_str, field = action.split(":", 1)
            step = self.EDITOR_FIELD_STEPS.get(field, 1)
            delta = -step if sign_str == "minus" else step
            cur = self.editor_values.get(field, 0)
            new_val = max(0, cur + delta)
            if new_val != cur:
                self.editor_values[field] = new_val
                self.editor_dirty = True
            return
        # ── v0.19.x: size editor ────────────────────────────────────
        if action in ("size_w_minus", "size_w_plus", "size_h_minus", "size_h_plus"):
            w, h = self.editor_size
            if action == "size_w_minus":
                w = max(1, w - 1)
            elif action == "size_w_plus":
                w = min(10, w + 1)
            elif action == "size_h_minus":
                h = max(1, h - 1)
            elif action == "size_h_plus":
                h = min(10, h + 1)
            new_sz = (w, h)
            if new_sz != self.editor_size:
                self.editor_size = new_sz
                self.editor_dirty = True
            return
        # ── v0.19.x: filename text input toggle ─────────────────────
        if action == "edit_filename":
            self.editor_text_input_field = "filename"
            self._editor_text_buffer = self.editor_sprite_filename or ""
            return
        # ── v0.19.x / v0.27: production / consumption row actions ───
        # v0.27: action names now carry an explicit step magnitude
        # (`prod_minus_major:` / `prod_minus_minor:` / `prod_plus_minor:` /
        # `prod_plus_major:`) using the same EDITOR_STEP_MAJOR / MINOR
        # constants as the scalar rows. The legacy single-step
        # `prod_minus:` / `prod_plus:` actions are still accepted (no
        # current button emits them, but a test or external caller
        # may) — they step by 1.
        major = self.EDITOR_STEP_MAJOR
        minor = self.EDITOR_STEP_MINOR
        # v0.33: ``editor_material_cost`` is the third pane. Default to
        # an empty dict (not initialised when legacy tests bypass the
        # snapshot path and poke ``editor_production`` /
        # ``editor_consumption`` directly). ``setdefault`` would also
        # work but ``getattr`` keeps the legacy-test attribute set
        # unchanged for any other code that uses ``hasattr`` to check.
        mat_dict = getattr(self, "editor_material_cost", None)
        if mat_dict is None:
            mat_dict = {}
            self.editor_material_cost = mat_dict
        for pane_prefix, pane_dict in (
            ("prod", self.editor_production),
            ("cons", self.editor_consumption),
            # v0.33: build-materials pane. Same five-button row + remove
            # + Add as the other two; the dispatch is uniform because
            # _editor_draw_resource_pane emits the same action shape
            # under any prefix.
            ("mat",  mat_dict),
        ):
            for kind, delta in (
                (f"{pane_prefix}_minus_major:", -major),
                (f"{pane_prefix}_minus_minor:", -minor),
                (f"{pane_prefix}_minus_unit:",  -self.EDITOR_STEP_UNIT),
                (f"{pane_prefix}_plus_unit:",    self.EDITOR_STEP_UNIT),
                (f"{pane_prefix}_plus_minor:",   minor),
                (f"{pane_prefix}_plus_major:",   major),
                (f"{pane_prefix}_minus:",       -1),
                (f"{pane_prefix}_plus:",         1),
            ):
                if action.startswith(kind):
                    rid = action[len(kind):]
                    cur = pane_dict.get(rid, 0)
                    new_val = max(0, cur + delta)
                    if new_val == 0:
                        pane_dict.pop(rid, None)
                    else:
                        pane_dict[rid] = new_val
                    if new_val != cur:
                        self.editor_dirty = True
                        self._editor_refresh_primary_keys()
                    return
        if action.startswith("prod_remove:"):
            rid = action.split(":", 1)[1]
            if rid in self.editor_production:
                del self.editor_production[rid]
                self.editor_dirty = True
                self._editor_refresh_primary_keys()
            return
        if action.startswith("cons_remove:"):
            rid = action.split(":", 1)[1]
            if rid in self.editor_consumption:
                del self.editor_consumption[rid]
                self.editor_dirty = True
                self._editor_refresh_primary_keys()
            return
        # v0.33: build-materials remove.
        if action.startswith("mat_remove:"):
            rid = action.split(":", 1)[1]
            mc = getattr(self, "editor_material_cost", None)
            if mc and rid in mc:
                del mc[rid]
                self.editor_dirty = True
            return
        if action == "prod_add":
            self.editor_picker_open = "prod"
            return
        if action == "cons_add":
            self.editor_picker_open = "cons"
            return
        # v0.33: build-materials Add — opens the resource picker
        # targeted at the materials pane.
        if action == "mat_add":
            self.editor_picker_open = "mat"
            return
        if action == "picker_cancel":
            self.editor_picker_open = None
            return
        if action.startswith("picker_pick:"):
            rid = action.split(":", 1)[1]
            side = self.editor_picker_open
            if side == "prod":
                self.editor_production[rid] = self.editor_production.get(rid, 0) or 1
            elif side == "cons":
                self.editor_consumption[rid] = self.editor_consumption.get(rid, 0) or 1
            elif side == "mat":
                # v0.33: build-materials side. Default to qty=1 so the
                # player can tweak it up with the ±10/±50 buttons.
                mc = getattr(self, "editor_material_cost", None)
                if mc is None:
                    mc = {}
                    self.editor_material_cost = mc
                mc[rid] = mc.get(rid, 0) or 1
            self.editor_picker_open = None
            self.editor_dirty = True
            self._editor_refresh_primary_keys()
            return
        log.warning("Editor: unknown action %r", action)

    def _editor_close(self, commit: bool) -> None:
        if commit and self.editor_dirty:
            try:
                self._editor_save_to_disk()
                self._notify("Buildings saved.", COLOR_GREEN)
            except Exception as e:  # noqa: BLE001 — surface any I/O fault
                log.exception("Editor save failed")
                self._notify(f"Save failed: {e}", COLOR_RED)
                return  # leave the editor open so the player can retry
        self.editor_open = False
        self.editor_dirty = False

    def _editor_save_to_disk(self) -> None:
        """Apply the editor's working values to data/buildings.json
        and rebuild the live registry. The disk write goes through
        a tmp-then-rename so a half-written file can't brick the
        game.

        v0.19.x rewrite: now writes the full per-resource production
        and consumption dicts (the editor lets the player add /
        remove / tweak each entry independently), the building
        size, and the sprite filename (via the data/textures.json
        manifest — never by mutating the building entry directly,
        because the manifest is the supported override path).
        """
        import json
        import os
        # Look the path up via game_window so test fixtures that
        # monkeypatch ``game_window.BUILDINGS_PATH`` redirect this
        # write transparently. (Test isolation contract — pre-mixin
        # the constant was imported directly into game_window.py and
        # patching it there reached this function.)
        import game_window as _gw
        path = _gw.BUILDINGS_PATH
        with open(path, "r") as f:
            raw = json.load(f)
        bid = self.editor_selected_id
        if bid not in raw:
            raise RuntimeError(f"Building id {bid!r} not in {path}")
        scalar_fields = (
            "cost", "workers", "housing", "storage", "construction_ticks",
        )
        for field, val in self.editor_values.items():
            if field == "cost":
                raw[bid]["cost"] = int(val)
            elif field in scalar_fields:
                # Drop the field from the JSON entry when the value
                # is the schema default (0) and it wasn't explicitly
                # in the file — keeps the JSON tight.
                if val != 0 or field in raw[bid]:
                    raw[bid][field] = int(val)
        # Production / consumption — write the full dicts. Drop a
        # zero-valued entry (player removed it) and remove the key
        # entirely if the dict is now empty.
        # Defensive defaults for callers that bypass _editor_snapshot_for
        # (legacy tests poke editor_values directly without initialising
        # the new full-dict attributes).
        editor_production = getattr(self, "editor_production", None)
        editor_consumption = getattr(self, "editor_consumption", None)
        # v0.33: build-materials. Same defensive pattern — legacy tests
        # that bypass the snapshot don't touch material_cost.
        editor_material_cost = getattr(self, "editor_material_cost", None)
        if editor_production is None or editor_consumption is None:
            # Fall back to the registry's existing values — i.e. don't
            # touch production/consumption when the caller didn't
            # populate them.
            cur = self.registry.get(bid)
            editor_production = dict(cur.production) if cur else {}
            editor_consumption = dict(cur.consumption) if cur else {}
        if editor_material_cost is None:
            cur = self.registry.get(bid)
            editor_material_cost = dict(cur.material_cost) if cur else {}
        prod = {k: int(v) for k, v in editor_production.items() if v > 0}
        cons = {k: int(v) for k, v in editor_consumption.items() if v > 0}
        mats = {k: int(v) for k, v in editor_material_cost.items() if v > 0}
        if prod:
            raw[bid]["production"] = prod
        elif "production" in raw[bid]:
            del raw[bid]["production"]
        if cons:
            raw[bid]["consumption"] = cons
        elif "consumption" in raw[bid]:
            del raw[bid]["consumption"]
        # v0.33: round-trip material_cost. Same drop-when-empty rule
        # as production/consumption so the JSON stays tight.
        if mats:
            raw[bid]["material_cost"] = mats
        elif "material_cost" in raw[bid]:
            del raw[bid]["material_cost"]
        # Size: clamp 1..10 (defensive — the +/- handler already
        # clamps, but a hand-edited save could push it out). When
        # editor_size isn't populated, leave the existing JSON entry
        # alone.
        editor_size = getattr(self, "editor_size", None)
        if editor_size is not None:
            w = max(1, min(10, int(editor_size[0])))
            h = max(1, min(10, int(editor_size[1])))
            raw[bid]["size"] = [w, h]
        # Write atomically: write to .tmp then rename.
        tmp = str(path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, path)
        log.info("Editor: wrote %s for building %s", path, bid)
        # Sprite filename: persist via data/textures.json manifest.
        # Skip if the snapshot wasn't initialised (legacy callers).
        sprite_fn = getattr(self, "editor_sprite_filename", None)
        if sprite_fn:
            try:
                self._editor_save_sprite_filename(bid, sprite_fn)
            except Exception as e:  # noqa: BLE001
                log.warning("Editor: sprite manifest write failed: %s", e)
        # Hot-reload the registry. The economy / game_map keep their
        # references — but since BuildingRegistry.from_json_file
        # returns a fresh registry, we replace self.registry and
        # also update every system that holds a stale ref.
        new_reg = BuildingRegistry.from_json_file(_gw.BUILDINGS_PATH)
        self.registry = new_reg
        self.economy.registry = new_reg
        self.game_map.registry = new_reg
        self.road_network.registry = new_reg
        self.service_map.registry = new_reg
        self.walker_manager.registry = new_reg
        self.house_evolution.registry = new_reg
        self.storage.registry = new_reg
        self.decay.registry = new_reg

    def _editor_save_sprite_filename(self, bid: str, filename: str) -> None:
        """Persist the sprite filename to ``data/textures.json``. The
        manifest is the supported override path — modders ship a
        sprite at ``assets/textures/buildings/<filename>`` and add a
        manifest entry pointing to it.

        Validates the extension (jpg / jpeg / png — per spec).
        Empty filename means "leave the manifest alone".
        """
        if not filename:
            return
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ("jpg", "jpeg", "png"):
            log.warning(
                "Editor: refusing to write manifest entry %r — "
                "extension must be jpg/jpeg/png", filename,
            )
            return
        import json as _json
        import os
        mpath = DATA_DIR / "textures.json"
        if mpath.is_file():
            with mpath.open() as f:
                raw = _json.load(f)
        else:
            raw = {"buildings": {}}
        raw.setdefault("buildings", {})[bid] = filename
        # Also write a name-keyed alias so the manifest's two-key
        # convention stays consistent.
        bd = self.registry.get(bid)
        if bd is not None:
            raw["buildings"][bd.name.lower()] = filename
        tmp = str(mpath) + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(raw, f, indent=2)
        os.replace(tmp, mpath)
        log.info("Editor: manifest updated — %s → %s", bid, filename)
        # Drop the texture cache for this building so the next draw
        # tries to load the new file.
        try:
            cache = getattr(self.textures, "_cache", None)
            if isinstance(cache, dict):
                cache.pop(("buildings", bid), None)
        except Exception:  # noqa: BLE001
            pass



    def _menu_open_editor(self) -> None:
        """v0.15: open the buildings editor — a simple modal where the
        player can tweak cost / workers / housing / storage on each
        building and write back to data/buildings.json. Game stays
        paused while the editor is open."""
        self.show_menu = False
        self.editor_open = True
        # Default the editor's selection to whichever building the
        # player currently has selected on the palette — a sensible
        # cursor that matches the player's current focus.
        self.editor_selected_id = self.selected_building
        self.editor_dirty = False
        # Snapshot the editable values for the current selection so
        # the per-field +/- buttons can mutate them without losing
        # the original state on cancel. We mutate the snapshot on
        # tweak; only Save commits to the registry + JSON file.
        self._editor_snapshot_for(self.editor_selected_id)
        log.info("Editor opened on %s", self.editor_selected_id)

    def _editor_snapshot_for(self, bid: str) -> None:
        """Pull the editable fields off the registry into a working
        snapshot the editor mutates. The registry's Building is a
        frozen dataclass — we can't mutate it in place, so the editor
        edits a parallel dict and applies on save.

        v0.19.x rewrite: snapshot now captures the full
        production / consumption dicts (so the player can edit each
        input/output line independently and add new ones), the
        building size (width × height, 1..10 each), and the sprite
        filename pulled from data/textures.json (or built from the
        building id). Previously only the largest production /
        consumption entry was editable.
        """
        bd = self.registry.get(bid)
        if bd is None:
            self.editor_values = {}
            self.editor_production = {}
            self.editor_consumption = {}
            self.editor_material_cost = {}
            self.editor_size = (1, 1)
            self.editor_sprite_filename = ""
            return
        self.editor_values = {
            "cost": int(bd.cost),
            "workers": int(bd.workers),
            "housing": int(bd.housing),
            "storage": int(bd.storage),
            "construction_ticks": int(bd.construction_ticks),
        }
        # Full per-resource dicts the editor can add/remove/tweak.
        self.editor_production = dict(bd.production)
        self.editor_consumption = dict(bd.consumption)
        # v0.33: build-materials dict (the non-gold one-off cost paid
        # from the stockpile on placement). Empty dict if the building
        # has no material requirements — same shape as production /
        # consumption so the +/- / picker dispatch reuses the existing
        # pane handler with just an extra action_prefix ("mat").
        self.editor_material_cost = dict(bd.material_cost)
        # Size (width, height) — clamped to 1..10 in the +/- handler.
        self.editor_size = (int(bd.size[0]), int(bd.size[1]))
        # Sprite filename: prefer the textures.json manifest; fall
        # back to the building id with a .jpg suffix (the project
        # default since v0.17).
        self.editor_sprite_filename = self._editor_lookup_sprite_name(bid)
        # Reset focus / picker / text-input modes.
        self.editor_text_input_field = None  # None | "filename"
        self.editor_picker_open = None       # None | "production" | "consumption"
        # Back-compat: keep the legacy headline keys around so the
        # _editor_save_to_disk path can still write them. They're
        # derived from editor_production / editor_consumption.
        self._editor_refresh_primary_keys()

    def _editor_refresh_primary_keys(self) -> None:
        """Pick the largest production/consumption entries — used by
        legacy callers / tests that still read the old single-entry
        editor API."""
        prod_no_happy = {
            k: v for k, v in self.editor_production.items() if k != "happiness"
        }
        self._editor_primary_output_key = (
            max(prod_no_happy, key=lambda k: (prod_no_happy[k], k))
            if prod_no_happy else None
        )
        self._editor_primary_input_key = (
            max(self.editor_consumption,
                key=lambda k: (self.editor_consumption[k], k))
            if self.editor_consumption else None
        )

    def _editor_lookup_sprite_name(self, bid: str) -> str:
        """Find the manifest entry for this building id, or return
        the conventional ``<id>.jpg`` if absent."""
        try:
            import json as _json
            from constants import DATA_DIR as _DD
            mpath = _DD / "textures.json"
            if mpath.is_file():
                with mpath.open() as f:
                    raw = _json.load(f)
                buildings = raw.get("buildings", {})
                if bid in buildings:
                    return str(buildings[bid])
                # The manifest also accepts the human-readable name.
                bd = self.registry.get(bid)
                if bd is not None and bd.name.lower() in buildings:
                    return str(buildings[bd.name.lower()])
        except Exception:  # noqa: BLE001 — manifest is optional
            pass
        return f"{bid}.jpg"
