"""Unit editor — modal that lets the player tweak data/units.json
(HP, damage, speed, skills, flags) and reloads the live UnitRegistry.

Extracted from game_window.py for clarity (~490 lines). Methods read
and mutate `self.unit_editor_*` state on CaesarGameWindow."""
from __future__ import annotations

import logging

import arcade

from constants import (
    COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_RED,
    COLOR_UI_BORDER, COLOR_WHITE,
    DATA_DIR,
)

log = logging.getLogger("caesar3.window")

class UnitEditorMixin:
    """UnitEditorMixin — see module docstring."""

    # ── v0.19: Unit editor (read-only) ────────────────────────────
    # ══════════════════════════════════════════════════════════════════════
    #  UNIT EDITOR  (v0.25 rewrite — was roster viewer through v0.24)
    # ══════════════════════════════════════════════════════════════════════
    # Definition-editor for ``data/units.json``. Mirrors the buildings
    # editor pattern: a scrollable list of unit ids on the left, an
    # editable form on the right (HP / damage / speed / sight / patrol
    # / training time / cost / upkeep / weapon cost / defense), a flag
    # row for boolean toggles (ranged, armoured), and a skill chip row.
    # Save writes back through a tmp-then-rename to ``data/units.json``
    # and reloads the live UnitRegistry so the next garrison spawn
    # picks up the new stats.

    # Scalar field metadata. Each entry: (key, label, step, max).
    # Integer fields except `speed` which is a fractional tile-per-tick
    # and gets a 0.005 step / 0.5 max.
    UNIT_EDITOR_FIELDS: list[tuple[str, str, float, float]] = [
        ("hp",             "HP at spawn",         5,    300),
        ("damage",         "Attack strength",     1,    200),
        ("defense",        "Defense",             1,    100),
        ("speed",          "Walking speed",       0.005, 0.5),
        ("sight",          "Sight (ranged)",      1,    30),
        ("patrol_radius",  "Patrol radius",       1,    30),
        ("training_time",  "Training time (ticks)", 5,  500),
        ("cost",           "Initial cost (gold)", 5,   2000),
        ("upkeep",         "Upkeep / tick",       1,    100),
        ("weapon_cost",    "Weapons per spawn",   1,    20),
    ]

    # Boolean flags. Each entry: (key, label).
    UNIT_EDITOR_FLAGS: list[tuple[str, str]] = [
        ("ranged",   "Ranged attack"),
        ("armoured", "Armoured (½ incoming dmg)"),
    ]

    # Skill catalogue presented as toggle chips. Modders can extend
    # this list; the runtime combat system reads the strings.
    UNIT_EDITOR_SKILLS: list[tuple[str, str]] = [
        ("mood_booster",       "Mood booster (leader)"),
        ("battlefield_surgery", "Battlefield surgery (medic)"),
        ("siege_engineer",     "Siege engineer"),
        ("scouting",           "Scouting"),
        ("formation_drill",    "Formation drill"),
    ]

    def _unit_editor_snapshot(self, uid: str) -> None:
        """Pull the editable values for a unit id into the working
        snapshot. Called when the editor opens or when the player
        selects a different unit from the list."""
        ureg = self._ensure_unit_registry()
        ud = ureg.get(uid)
        if ud is None:
            self.unit_editor_values = {k: 0.0 for k, _, _, _ in self.UNIT_EDITOR_FIELDS}
            self.unit_editor_flags = {k: False for k, _ in self.UNIT_EDITOR_FLAGS}
            self.unit_editor_skills = set()
            return
        self.unit_editor_values = {
            "hp":             float(ud.hp),
            "damage":         float(ud.damage),
            "defense":        float(ud.defense),
            "speed":          float(ud.speed),
            "sight":          float(ud.sight),
            "patrol_radius":  float(ud.patrol_radius),
            "training_time":  float(ud.training_time),
            "cost":           float(ud.cost),
            "upkeep":         float(ud.upkeep),
            "weapon_cost":    float(ud.weapon_cost),
        }
        self.unit_editor_flags = {
            "ranged":   bool(ud.ranged),
            "armoured": bool(ud.armoured),
        }
        self.unit_editor_skills = set(ud.skills)
        self.unit_editor_dirty = False

    def _open_unit_editor(self) -> None:
        """Initialise the editor and show it. Called from the menu /
        splash entry points."""
        self.show_unit_editor = True
        # Snapshot whatever unit is currently selected (default
        # "light_infantry" — set in __init__). If a previous session
        # selected something else, that selection persists.
        self._unit_editor_snapshot(self.unit_editor_selected_id)

    def _draw_unit_editor(self) -> None:
        """v0.25: unit definition editor.

        Two columns: a unit list on the left, an edit form on the
        right. Below the form: a Skills chip strip and a flag row.
        Footer: Cancel / Save. Save writes to ``data/units.json`` and
        reloads the live UnitRegistry. Esc / Cancel discards.
        """
        # Lazy-init the snapshot so the editor opens cleanly even when
        # an external caller (Esc menu) flipped show_unit_editor
        # directly without going through _open_unit_editor.
        if not self.unit_editor_values:
            self._unit_editor_snapshot(self.unit_editor_selected_id)

        self._unit_editor_btn_rects = []

        # Dim background.
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 170),
        )
        # Panel.
        cx, cy = self.width / 2, self.height / 2
        pw, ph = 820, 600
        l = cx - pw / 2
        r = cx + pw / 2
        b = cy - ph / 2
        t = cy + ph / 2
        # Clamp to screen.
        if t > self.height - 20:
            shift = (self.height - 20) - t
            t += shift; b += shift
        if b < 20:
            shift = 20 - b
            t += shift; b += shift
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_unit_editor"):
            self._txt_unit_editor: dict[str, arcade.Text] = {}

        def line(key: str, text: str, x: float, y: float,
                 size: int = 11, color=COLOR_WHITE,
                 bold: bool = False, anchor_x: str = "left") -> None:
            obj = self._txt_unit_editor.get(key)
            if obj is None:
                obj = arcade.Text(
                    text, x, y, color, size,
                    bold=bold, anchor_x=anchor_x,
                )
                self._txt_unit_editor[key] = obj
            obj.text = text
            obj.x = x; obj.y = y
            obj.color = color
            obj.draw()

        # Title + hint.
        line("title", "UNIT EDITOR", cx, t - 26,
             size=14, bold=True, anchor_x="center", color=COLOR_GOLD)
        line("hint", "Tweak fields and Save to write to data/units.json",
             cx, t - 44, size=10, color=COLOR_GRAY, anchor_x="center")

        # ── Left column: unit list ────────────────────────────────────
        list_l = l + 14
        list_r = list_l + 180
        list_t = t - 64
        list_b = b + 60
        arcade.draw_lrbt_rectangle_filled(
            list_l, list_r, list_b, list_t, (22, 18, 14, 230),
        )
        arcade.draw_lrbt_rectangle_outline(
            list_l, list_r, list_b, list_t, COLOR_UI_BORDER, 1,
        )

        ureg = self._ensure_unit_registry()
        unit_ids = sorted(ureg.all_ids())
        row_h = 22
        rows_visible = int((list_t - list_b) / row_h)
        max_scroll = max(0, len(unit_ids) - rows_visible)
        self.unit_editor_scroll = max(0, min(self.unit_editor_scroll, max_scroll))

        # Scroll arrows.
        sx1 = list_r + 2
        sx2 = sx1 + 20
        arcade.draw_lrbt_rectangle_filled(sx1, sx2, list_t - 22, list_t, (60, 50, 40))
        arcade.draw_lrbt_rectangle_outline(sx1, sx2, list_t - 22, list_t, COLOR_UI_BORDER, 1)
        line("u_scroll_up", "^", (sx1+sx2)/2, list_t - 16,
             size=12, bold=True, color=COLOR_WHITE, anchor_x="center")
        self._unit_editor_btn_rects.append(
            (sx1, sx2, list_t - 22, list_t, "scroll_up")
        )
        arcade.draw_lrbt_rectangle_filled(sx1, sx2, list_b, list_b + 22, (60, 50, 40))
        arcade.draw_lrbt_rectangle_outline(sx1, sx2, list_b, list_b + 22, COLOR_UI_BORDER, 1)
        line("u_scroll_dn", "v", (sx1+sx2)/2, list_b + 6,
             size=12, bold=True, color=COLOR_WHITE, anchor_x="center")
        self._unit_editor_btn_rects.append(
            (sx1, sx2, list_b, list_b + 22, "scroll_down")
        )

        for vi in range(rows_visible):
            idx = self.unit_editor_scroll + vi
            if idx >= len(unit_ids):
                break
            uid = unit_ids[idx]
            row_top = list_t - vi * row_h
            row_bot = row_top - row_h
            selected = (uid == self.unit_editor_selected_id)
            if selected:
                arcade.draw_lrbt_rectangle_filled(
                    list_l, list_r, row_bot, row_top, (90, 70, 50, 220),
                )
            ud = ureg.get(uid)
            label_text = ud.name if ud else uid
            line(f"u_row_{vi}", f"  {label_text}",
                 list_l + 4, row_bot + 5,
                 size=11, color=COLOR_GOLD if selected else COLOR_WHITE)
            self._unit_editor_btn_rects.append(
                (list_l, list_r, row_bot, row_top, f"select:{uid}")
            )

        # ── Right pane: form ──────────────────────────────────────────
        form_l = list_r + 30
        form_r = r - 14
        form_t = list_t
        form_b = list_b
        ud = ureg.get(self.unit_editor_selected_id)
        if ud is None:
            return

        # Header.
        line("u_header", ud.name, form_l, form_t - 22,
             size=16, bold=True, color=COLOR_GOLD)
        line("u_meta", f"id: {ud.id}  ·  category: {ud.category}",
             form_l, form_t - 42, size=10, color=COLOR_GRAY)

        # Scalar fields — two columns of 5 rows each.
        field_top = form_t - 70
        col_w = (form_r - form_l) / 2 - 6
        field_h = 32
        for fi, (key, label, step, _max) in enumerate(self.UNIT_EDITOR_FIELDS):
            col = fi // 5
            row = fi % 5
            base_x = form_l + col * (col_w + 12)
            row_y = field_top - row * field_h
            # Label.
            line(f"u_lbl_{key}", label, base_x, row_y,
                 size=10, color=COLOR_WHITE, bold=True)
            # +/-/value row.
            val = self.unit_editor_values.get(key, 0.0)
            # Format speed as a float, everything else as integer.
            if key == "speed":
                val_text = f"{val:.3f}"
            else:
                val_text = str(int(val))
            ctrl_y_b = row_y - 22
            ctrl_y_t = row_y - 2
            mb_l = base_x + 130
            mb_r = mb_l + 24
            arcade.draw_lrbt_rectangle_filled(mb_l, mb_r, ctrl_y_b, ctrl_y_t, (60, 45, 35))
            arcade.draw_lrbt_rectangle_outline(mb_l, mb_r, ctrl_y_b, ctrl_y_t, COLOR_UI_BORDER, 1)
            line(f"u_minus_{key}", "-", (mb_l+mb_r)/2, ctrl_y_b + 2,
                 size=12, bold=True, color=COLOR_WHITE, anchor_x="center")
            self._unit_editor_btn_rects.append(
                (mb_l, mb_r, ctrl_y_b, ctrl_y_t, f"minus:{key}")
            )
            vb_l = mb_r + 4
            vb_r = vb_l + 58
            arcade.draw_lrbt_rectangle_filled(vb_l, vb_r, ctrl_y_b, ctrl_y_t, (22, 18, 14))
            arcade.draw_lrbt_rectangle_outline(vb_l, vb_r, ctrl_y_b, ctrl_y_t, COLOR_UI_BORDER, 1)
            line(f"u_val_{key}", val_text, (vb_l+vb_r)/2, ctrl_y_b + 2,
                 size=11, bold=True, color=COLOR_GOLD, anchor_x="center")
            pb_l = vb_r + 4
            pb_r = pb_l + 24
            arcade.draw_lrbt_rectangle_filled(pb_l, pb_r, ctrl_y_b, ctrl_y_t, (60, 45, 35))
            arcade.draw_lrbt_rectangle_outline(pb_l, pb_r, ctrl_y_b, ctrl_y_t, COLOR_UI_BORDER, 1)
            line(f"u_plus_{key}", "+", (pb_l+pb_r)/2, ctrl_y_b + 2,
                 size=12, bold=True, color=COLOR_WHITE, anchor_x="center")
            self._unit_editor_btn_rects.append(
                (pb_l, pb_r, ctrl_y_b, ctrl_y_t, f"plus:{key}")
            )

        # ── Flags row (ranged / armoured) ──
        flags_y = field_top - 5 * field_h - 10
        line("u_flags_hdr", "── Flags ──",
             form_l, flags_y, size=11, bold=True, color=COLOR_GOLD)
        flag_y2 = flags_y - 22
        for fi, (key, label) in enumerate(self.UNIT_EDITOR_FLAGS):
            base_x = form_l + fi * (col_w + 12)
            on = self.unit_editor_flags.get(key, False)
            # Toggle box.
            tb_l = base_x
            tb_r = tb_l + 16
            tb_b = flag_y2 - 2
            tb_t = flag_y2 + 14
            arcade.draw_lrbt_rectangle_filled(
                tb_l, tb_r, tb_b, tb_t,
                (90, 130, 70) if on else (60, 45, 35),
            )
            arcade.draw_lrbt_rectangle_outline(tb_l, tb_r, tb_b, tb_t, COLOR_UI_BORDER, 1)
            if on:
                line(f"u_flag_check_{key}", "x", (tb_l+tb_r)/2, tb_b + 2,
                     size=11, bold=True, color=COLOR_WHITE, anchor_x="center")
            else:
                # Hide the checkmark when off — set text to empty.
                line(f"u_flag_check_{key}", "", (tb_l+tb_r)/2, tb_b + 2,
                     size=11)
            # Label.
            line(f"u_flag_lbl_{key}", label, tb_r + 8, tb_b + 2,
                 size=10, color=COLOR_WHITE)
            self._unit_editor_btn_rects.append(
                (tb_l, tb_r + 220, tb_b, tb_t, f"toggle_flag:{key}")
            )

        # ── Skills chip row ──
        skills_y = flag_y2 - 30
        line("u_skills_hdr", "── Skills ──",
             form_l, skills_y, size=11, bold=True, color=COLOR_GOLD)
        chip_y_b = skills_y - 26
        chip_y_t = skills_y - 6
        chip_x = form_l
        for skill_key, skill_label in self.UNIT_EDITOR_SKILLS:
            chip_w = len(skill_label) * 6 + 16
            if chip_x + chip_w > form_r:
                # Wrap to next row.
                chip_x = form_l
                chip_y_b -= 24
                chip_y_t -= 24
            on = skill_key in self.unit_editor_skills
            arcade.draw_lrbt_rectangle_filled(
                chip_x, chip_x + chip_w, chip_y_b, chip_y_t,
                (90, 130, 70) if on else (50, 40, 32),
            )
            arcade.draw_lrbt_rectangle_outline(
                chip_x, chip_x + chip_w, chip_y_b, chip_y_t,
                COLOR_GOLD if on else COLOR_UI_BORDER, 1,
            )
            line(f"u_skill_{skill_key}", skill_label,
                 chip_x + chip_w / 2, chip_y_b + 3,
                 size=10, color=COLOR_WHITE if on else COLOR_GRAY,
                 anchor_x="center")
            self._unit_editor_btn_rects.append(
                (chip_x, chip_x + chip_w, chip_y_b, chip_y_t,
                 f"toggle_skill:{skill_key}")
            )
            chip_x += chip_w + 6

        # ── Footer buttons (Cancel / Save) ──
        btn_w, btn_h = 110, 30
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
        line("u_btn_cancel", "Cancel",
             (cancel_x_l+cancel_x_r)/2, btn_y_bot + 8,
             size=12, bold=True, color=COLOR_WHITE, anchor_x="center")
        self._unit_editor_btn_rects.append(
            (cancel_x_l, cancel_x_r, btn_y_bot, btn_y_top, "cancel")
        )
        save_fill = (90, 70, 30) if self.unit_editor_dirty else (50, 40, 35)
        arcade.draw_lrbt_rectangle_filled(
            save_x_l, save_x_r, btn_y_bot, btn_y_top, save_fill,
        )
        arcade.draw_lrbt_rectangle_outline(
            save_x_l, save_x_r, btn_y_bot, btn_y_top,
            COLOR_GOLD if self.unit_editor_dirty else COLOR_UI_BORDER,
            2 if self.unit_editor_dirty else 1,
        )
        line("u_btn_save",
             "Save" + (" *" if self.unit_editor_dirty else ""),
             (save_x_l+save_x_r)/2, btn_y_bot + 8,
             size=12, bold=True,
             color=COLOR_GOLD if self.unit_editor_dirty else COLOR_WHITE,
             anchor_x="center")
        self._unit_editor_btn_rects.append(
            (save_x_l, save_x_r, btn_y_bot, btn_y_top, "save")
        )

    # ── Unit-editor click handling ─────────────────────────────────────
    def _unit_editor_handle_click(self, x: int, y: int) -> bool:
        """Hit-test the unit editor's button rects. Returns True if
        the click was handled (and the caller should not fall through
        to world-click handling)."""
        for x1, x2, y1, y2, action in self._unit_editor_btn_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                self._unit_editor_dispatch(action)
                return True
        # The modal eats clicks outside its hit rects too.
        return True

    def _unit_editor_dispatch(self, action: str) -> None:
        if action == "cancel":
            self._unit_editor_close(commit=False)
            return
        if action == "save":
            self._unit_editor_close(commit=True)
            return
        if action == "scroll_up":
            self.unit_editor_scroll = max(0, self.unit_editor_scroll - 1)
            return
        if action == "scroll_down":
            self.unit_editor_scroll += 1  # clamped next draw
            return
        if action.startswith("select:"):
            new_id = action.split(":", 1)[1]
            if new_id != self.unit_editor_selected_id:
                self.unit_editor_selected_id = new_id
                self._unit_editor_snapshot(new_id)
            return
        if action.startswith("minus:") or action.startswith("plus:"):
            sign, _, field = action.partition(":")
            step_map = {k: s for k, _l, s, _m in self.UNIT_EDITOR_FIELDS}
            max_map = {k: m for k, _l, _s, m in self.UNIT_EDITOR_FIELDS}
            step = step_map.get(field, 1)
            delta = -step if sign == "minus" else step
            cur = self.unit_editor_values.get(field, 0.0)
            new_val = max(0.0, min(max_map.get(field, 1e9), cur + delta))
            # Round speed to 3dp; integers stay integer.
            if field == "speed":
                new_val = round(new_val, 3)
            else:
                new_val = float(int(round(new_val)))
            if new_val != cur:
                self.unit_editor_values[field] = new_val
                self.unit_editor_dirty = True
            return
        if action.startswith("toggle_flag:"):
            key = action.split(":", 1)[1]
            self.unit_editor_flags[key] = not self.unit_editor_flags.get(key, False)
            self.unit_editor_dirty = True
            return
        if action.startswith("toggle_skill:"):
            key = action.split(":", 1)[1]
            if key in self.unit_editor_skills:
                self.unit_editor_skills.discard(key)
            else:
                self.unit_editor_skills.add(key)
            self.unit_editor_dirty = True
            return
        log.warning("UnitEditor: unknown action %r", action)

    def _unit_editor_close(self, commit: bool) -> None:
        if commit and self.unit_editor_dirty:
            try:
                self._unit_editor_save_to_disk()
                self._notify("Units saved.", COLOR_GREEN)
            except Exception as e:  # noqa: BLE001
                log.exception("Unit editor save failed")
                self._notify(f"Save failed: {e}", COLOR_RED)
                return  # leave open so player can retry
        self.show_unit_editor = False
        self.unit_editor_dirty = False

    def _unit_editor_save_to_disk(self) -> None:
        """Apply the editor's working values to ``data/units.json``
        and rebuild the live UnitRegistry. Atomic tmp-then-rename to
        avoid bricking on a partial write — same pattern as the
        buildings editor."""
        import json
        import os
        # DATA_DIR is the project's data directory (resolved relative
        # to constants.py, not __file__) — the editor lived in
        # game_window.py before the mixin split where ``Path(__file__)``
        # happened to give the right answer; using DATA_DIR keeps the
        # behaviour identical regardless of which module the method is
        # defined in.
        path = DATA_DIR / "units.json"
        with path.open() as f:
            raw = json.load(f)
        uid = self.unit_editor_selected_id
        if uid not in raw:
            # The unit was loaded from the built-in fallback (e.g. a
            # missing data/units.json) — seed a new entry rather than
            # erroring out.
            raw[uid] = {}
        entry = raw[uid]
        for key, _label, _step, _max in self.UNIT_EDITOR_FIELDS:
            val = self.unit_editor_values.get(key, 0.0)
            if key == "speed":
                entry[key] = float(val)
            else:
                # Drop the field if the value is 0 and it wasn't
                # explicitly in the file — keeps the JSON tight.
                if int(val) != 0 or key in entry:
                    entry[key] = int(val)
        for key, _label in self.UNIT_EDITOR_FLAGS:
            entry[key] = bool(self.unit_editor_flags.get(key, False))
        skills_list = sorted(self.unit_editor_skills)
        if skills_list:
            entry["skills"] = skills_list
        elif "skills" in entry:
            del entry["skills"]
        # Write atomically.
        tmp = str(path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, indent=4)
        os.replace(tmp, path)
        log.info("UnitEditor: wrote %s for unit %s", path, uid)
        # Hot-reload the live registry.
        from units import UnitRegistry
        new_ureg = UnitRegistry.from_json_file(path)
        self.unit_registry = new_ureg
        # Push it into the walker manager so the next spawn picks it up.
        if hasattr(self, "walker_manager") and self.walker_manager is not None:
            self.walker_manager.unit_registry = new_ureg
