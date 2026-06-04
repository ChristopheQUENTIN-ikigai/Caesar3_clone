"""Map editor — the splash-launched modal that lets the player paint
terrain and features into a fresh world, save / load / rename map
files, and resize the grid. Extracted from game_window.py for
clarity (~900 lines)."""
from __future__ import annotations

import logging

import arcade

from caesar import CaesarRequestManager
from constants import (
    COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_RED,
    COLOR_UI_BORDER, COLOR_WHITE,
    DATA_DIR,
    GRID_COLS, GRID_ROWS,
    TERRAIN_GRASS, TERRAIN_GRASS_ALT, TERRAIN_WATER,
    TERRAIN_HILLS, TERRAIN_MOUNTAINS, TERRAIN_DESERT,
)
from decay import DecayManager
from diplomacy import DiplomacyTracker
from game_map import GameMap
from house_evolution import HouseEvolution
from pathfinding import Pathfinder
from rebellion import RebellionTracker
from road_network import RoadNetwork
from services import ServiceMap
from storage import Storage
from trade import TradeRouteManager
from walkers import WalkerManager

# Note: TOP_BAR_H lives in game_window.py. We deliberately do NOT
# do ``import game_window`` at module top — that would create a
# circular import (game_window imports this mixin). The one method
# that needs it does a local ``import game_window`` inside the
# function body, which resolves at call time after game_window's
# top-level code has finished.

log = logging.getLogger("caesar3.window")

class MapEditorMixin:
    """MapEditorMixin — see module docstring."""

    # ══════════════════════════════════════════════════════════════════════
    #  MAP EDITOR  (v0.16)
    # ══════════════════════════════════════════════════════════════════════
    # Layout constants for the editor's top toolbar (drawn just below
    # the main top bar, so resource readouts stay visible).
    EDITOR_TOOLBAR_H = 32
    EDITOR_TOOLBAR_BTN_W = 100
    EDITOR_TOOLBAR_BTN_PAD = 8
    # Maximum chars allowed in the map name field (keeps the
    # filename slug small and the rendered text within the panel).
    EDITOR_MAP_NAME_MAX = 32

    # v0.19.x: terrain & feature paint tools. The map editor draws a
    # second toolbar row with one button per entry; left-click on the
    # world paints the corresponding terrain (base layer) or feature
    # (overlay) on the clicked tile. The "build" entry is the
    # default — it returns to the normal building-placement flow.
    #
    # Layout: each entry is (tool_id, label, kind, payload) where
    # `kind` is "build" | "terrain" | "feature" | "clear_feature":
    #   * "build" returns to normal building placement.
    #   * "terrain" sets game_map.terrain[r][c] = payload (a TERRAIN_*
    #     int from constants).
    #   * "feature" sets game_map.terrain_features[r][c] = payload
    #     (a FEATURE_* string id), only on grass tiles.
    #   * "clear_feature" sets terrain_features[r][c] = None — the
    #     "no feature" eraser, distinct from painting plain grass.
    EDITOR_PAINT_TOOLS: list[tuple[str, str, str, object]] = [
        ("build",          "Build",      "build",         None),
        ("grass",          "Grass",      "terrain",       0),   # TERRAIN_GRASS
        ("water",          "Water",      "terrain",       2),   # TERRAIN_WATER
        ("hills",          "Hills",      "terrain",       3),   # TERRAIN_HILLS
        ("mountains",      "Mountains",  "terrain",       4),   # TERRAIN_MOUNTAINS
        ("desert",         "Desert",     "terrain",       5),   # TERRAIN_DESERT
        ("forest",         "Forest",     "feature",       "forest"),
        ("fertile",        "Fertile",    "feature",       "fertile_soil"),
        ("stone",          "Stone",      "feature",       "stone_deposit"),
        ("iron",           "Iron",       "feature",       "iron_vein"),
        ("gold",           "Gold",       "feature",       "gold_vein"),
        ("copper",         "Copper",     "feature",       "copper_vein"),
        # v0.26: clay is now a natural-resource feature (the Clay Pit
        # needs it in its footprint, same model as iron/copper/gold
        # mines and the quarry). Adding the paint tool here so map
        # authors can place clay deposits the same way they place
        # the other extractive features.
        ("clay",           "Clay",       "feature",       "clay_deposit"),
        ("groundwater",    "Water+",     "feature",       "groundwater"),
        # v0.26: fish overlay. The fishery needs a `fish` feature on
        # an adjacent water tile to produce; the editor was missing
        # the painter, so authors who wanted a coastal scenario had
        # to hand-edit JSON. The world map tools (above) already
        # paint *water* terrain; this one stamps the fish overlay
        # on top — the painter auto-flips the cell to water first
        # (see `_editor_paint_at` water guard below).
        ("fish",           "Fish",       "feature",       "fish"),
        ("clear_feature",  "Clear ftr",  "clear_feature", None),
    ]
    EDITOR_PAINT_BTN_W = 70
    EDITOR_PAINT_BTN_PAD = 4

    def _splash_open_map_editor(self) -> None:
        """Splash → Map editor. Boots a fresh, empty world (no starter
        city, no trade routes, no events) and switches to ``app_state
        = "editor"``. Placement is free; Save/Load buttons sit in a
        top toolbar beneath the resource bar.

        Why a separate state rather than reusing "playing" with a
        flag? Because too many places in the codebase check
        ``self.paused`` / ``self.show_menu`` and would still tick
        the economy, decay buildings, fire trade-routes, etc. The
        cleanest invariant is: *the editor doesn't run the sim at
        all.* on_update() bails on `app_state == "editor"`; the
        rest of the game is invisible.
        """
        log.info("Splash → Map editor")
        from balance import BALANCE
        from economy import EconomyManager
        # Reuse the same teardown sequence as `_start_new_game` —
        # everything fresh, but skip the starter-city seeding step.
        self.scenario = "editor"
        self.game_map = GameMap(self.registry, textures=self.textures)
        self.economy = EconomyManager(self.registry, self.balance)
        self.road_network = RoadNetwork(self.game_map, self.registry)
        self.pathfinder = Pathfinder(self.road_network)
        self.service_map = ServiceMap(self.game_map, self.registry)
        self._wire_service_staffing_gate()
        self.walker_manager = WalkerManager(
            self.registry, textures=self.textures, pathfinder=self.pathfinder,
            service_map=self.service_map,
            unit_registry=self._ensure_unit_registry(),
        )
        self.house_evolution = HouseEvolution(
            self.game_map, self.registry, self.service_map, self.balance,
            walker_manager=self.walker_manager,
        )
        self.diplomacy = DiplomacyTracker(self.balance)
        self.caesar = CaesarRequestManager(self.balance)
        self.rebellion = RebellionTracker(self.balance)
        self.storage = Storage(self.game_map, self.registry)
        self.decay = DecayManager(
            self.game_map, self.registry, self.service_map, self.balance,
        )
        self.trade_manager = TradeRouteManager(self.economy)
        self._ensure_commercial_roads()
        # Reset transient state.
        self.tick_accumulator = 0.0
        self.game_time = 0
        self.year = 1
        self.month = 1
        self.notifications.clear()
        self.paused = True
        self.show_help = False
        self.show_menu = False
        self.inspected = None
        self.app_state = "editor"
        self.show_credits = False
        # v0.16: editor scratch state. The map name is what the
        # save dialog seeds; the picker list is built lazily when
        # the player presses Load. Hit-test rects for the toolbar
        # are rebuilt each draw, identical to the buildings
        # editor pattern.
        self.editor_map_name: str = "Untitled"
        self._editor_toolbar_rects: list[tuple[float, float, float, float, str]] = []
        self.editor_show_load_picker: bool = False
        self.editor_show_rename_dialog: bool = False
        # The rename dialog edits this in-place; on commit, we copy
        # to editor_map_name.
        self._editor_rename_buffer: str = self.editor_map_name
        self._editor_load_picker_rects: list[tuple[float, float, float, float, str]] = []
        # v0.19.x: terrain-paint state. The map editor now exposes a
        # second toolbar row of terrain & feature paint tools; when a
        # paint tool is selected, left-clicks on the world paint that
        # terrain/feature on the clicked tile instead of placing the
        # currently-selected building. ``editor_paint_tool`` is None
        # when the player is in build-mode (the original behaviour);
        # otherwise it's a string identifying the paint target — see
        # EDITOR_PAINT_TOOLS for the full list.
        self.editor_paint_tool: str | None = None
        self._editor_paint_rects: list[tuple[float, float, float, float, str]] = []
        # v0.50: starting-budget form state. The form edits the live
        # economy (treasury / population / resources) in-place on
        # commit; while open, edits land in this scratch buffer so
        # Cancel can discard them. ``editor_show_initvals`` toggles the
        # modal; ``_editor_initvals_buffer`` mirrors the editable fields;
        # ``_editor_initvals_field`` names the field that has keyboard
        # focus (None = no text-entry, button clicks still work).
        self.editor_show_initvals: bool = False
        self._editor_initvals_buffer: dict = {}
        self._editor_initvals_field: str | None = None
        self._editor_initvals_rects: list[tuple[float, float, float, float, str]] = []
        self._editor_initvals_scroll: int = 0
        # v0.50: seed a fresh editor's economy from the default-values
        # template so a brand-new map starts with sensible numbers the
        # author can then tweak via the Init values form. A *loaded*
        # map overwrites these via mapfile.load_map, so this only
        # affects the empty-world case.
        self._editor_seed_initial_values_from_template()
        self._notify("Map editor — left-click to place, right-click to demolish", COLOR_GOLD)

    # ══════════════════════════════════════════════════════════════════════
    #  INIT VALUES FORM  (v0.50)
    # ══════════════════════════════════════════════════════════════════════
    # Layout for the starting-budget modal. A tall, single-column form:
    # gold + population at the top, then one editable row per good
    # (nutrients first, then warehouse stocks), then Confirm / Cancel.
    INITVALS_PANEL_W = 560
    INITVALS_PANEL_H = 640
    INITVALS_ROW_H = 22
    # Step buttons flanking each numeric field — same spirit as the
    # gold-trade quantity stepper, scaled per field magnitude.
    INITVALS_GOLD_STEPS = (-1000, -100, +100, +1000)
    INITVALS_POP_STEPS = (-100, -10, +10, +100)
    INITVALS_GOOD_STEPS = (-100, -10, +10, +100)

    def _editor_seed_initial_values_from_template(self) -> None:
        """Write the default_initial_values template onto the live
        economy of a freshly-booted editor world.

        Idempotent and defensive: imports are local so a missing /
        broken template never blocks the editor from opening — it just
        falls through to whatever the EconomyManager defaults already
        set. Sets treasury, population, and every templated good in
        ``economy.resources`` (creating keys that didn't exist).
        """
        try:
            from default_initial_values import build_default_initial_values
        except Exception:  # noqa: BLE001 — template optional; never block editor
            log.warning("Init-values template unavailable; using engine defaults")
            return
        try:
            tpl = build_default_initial_values()
            self.economy.treasury = float(tpl["gold"])
            self.economy.population = int(tpl["population"])
            for good, qty in tpl["goods"].items():
                self.economy.resources[good] = float(qty)
            log.info(
                "Editor seeded initial values: gold=%d pop=%d goods=%d",
                int(tpl["gold"]), int(tpl["population"]), len(tpl["goods"]),
            )
        except Exception:  # noqa: BLE001
            log.exception("Init-values template seed failed; using engine defaults")

    def _editor_initvals_goods_order(self) -> list[str]:
        """The ordered good ids the form renders as rows: nutrients
        first (NUTRIENTS order), then warehouse stocks
        (WAREHOUSE_STOCKS order), de-duplicated. Read each draw so a
        modded good list is picked up without a restart.
        """
        from constants import NUTRIENTS
        from mapfile import WAREHOUSE_STOCKS
        order: list[str] = []
        seen: set[str] = set()
        for g in (*NUTRIENTS, *WAREHOUSE_STOCKS):
            if g not in seen:
                seen.add(g)
                order.append(g)
        return order

    def _editor_open_initvals(self) -> None:
        """Open the starting-budget form, seeding the editable buffer
        from the live economy so the author sees current values (set
        either by the template on a fresh map, or by a loaded map).
        """
        goods = {g: int(self.economy.resources.get(g, 0))
                 for g in self._editor_initvals_goods_order()}
        self._editor_initvals_buffer = {
            "gold":       int(self.economy.treasury),
            "population": int(self.economy.population),
            "goods":      goods,
        }
        self._editor_initvals_field = None
        self._editor_initvals_scroll = 0
        self.editor_show_initvals = True
        # Close sibling editor sub-panels so only one modal is up.
        self.editor_show_load_picker = False
        self.editor_show_rename_dialog = False
        log.info("Editor: Init values form opened")

    def _editor_initvals_cancel(self) -> None:
        """Discard buffered edits and close the form."""
        self.editor_show_initvals = False
        self._editor_initvals_field = None
        log.info("Editor: Init values cancelled (no changes applied)")

    def _editor_initvals_reset_template(self) -> None:
        """Reset the buffer (not yet the economy) to the template
        defaults. The author still has to Confirm to apply.
        """
        try:
            from default_initial_values import build_default_initial_values
            tpl = build_default_initial_values()
        except Exception:  # noqa: BLE001
            self._notify("Template unavailable", COLOR_RED)
            return
        goods = {g: int(tpl["goods"].get(g, 0))
                 for g in self._editor_initvals_goods_order()}
        self._editor_initvals_buffer = {
            "gold":       int(tpl["gold"]),
            "population": int(tpl["population"]),
            "goods":      goods,
        }
        self._notify("Init values reset to template (Confirm to apply)", COLOR_GOLD)

    def _editor_initvals_commit(self) -> None:
        """Apply the buffered edits onto the live economy and close.

        Writes treasury / population / every buffered good into
        ``economy.resources``. These are exactly the fields
        ``mapfile.collect_map_state`` reads on Save, so the next Save
        persists them with no extra plumbing.
        """
        buf = self._editor_initvals_buffer or {}
        try:
            self.economy.treasury = float(max(0, int(buf.get("gold", 0))))
            self.economy.population = int(max(0, int(buf.get("population", 0))))
            for good, qty in (buf.get("goods") or {}).items():
                self.economy.resources[good] = float(max(0, int(qty)))
        except Exception:  # noqa: BLE001
            log.exception("Init values commit failed")
            self._notify("Init values: commit failed", COLOR_RED)
            return
        self.editor_show_initvals = False
        self._editor_initvals_field = None
        self._notify(
            f"Initial values set — {int(self.economy.treasury)} dn, "
            f"pop {int(self.economy.population)}",
            COLOR_GREEN,
        )
        log.info(
            "Editor: Init values committed (gold=%d pop=%d)",
            int(self.economy.treasury), int(self.economy.population),
        )

    def _initvals_get(self, field_key: str) -> int:
        """Read a numeric value from the buffer by field key. Keys:
        ``"gold"``, ``"population"``, or ``"good:<id>"``.
        """
        buf = self._editor_initvals_buffer or {}
        if field_key == "gold":
            return int(buf.get("gold", 0))
        if field_key == "population":
            return int(buf.get("population", 0))
        if field_key.startswith("good:"):
            gid = field_key.split(":", 1)[1]
            return int((buf.get("goods") or {}).get(gid, 0))
        return 0

    def _initvals_set(self, field_key: str, value: int) -> None:
        """Write a clamped (>=0) numeric value into the buffer."""
        buf = self._editor_initvals_buffer or {}
        value = max(0, int(value))
        if field_key == "gold":
            buf["gold"] = value
        elif field_key == "population":
            buf["population"] = value
        elif field_key.startswith("good:"):
            gid = field_key.split(":", 1)[1]
            buf.setdefault("goods", {})[gid] = value
        self._editor_initvals_buffer = buf

    def _draw_editor_initvals(self) -> None:
        """Render the starting-budget modal: gold + population steppers
        at the top, a scrollable list of per-good steppers below, and
        Reset / Confirm / Cancel along the bottom.

        Click a value box to give it keyboard focus (type digits to
        overwrite, Backspace to delete); the +/- step buttons adjust
        without focus. Mouse-wheel scrolls the goods list.
        """
        self._editor_initvals_rects = []
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 180),
        )
        cx, cy = self.width / 2, self.height / 2
        pw, ph = self.INITVALS_PANEL_W, self.INITVALS_PANEL_H
        l, r = cx - pw / 2, cx + pw / 2
        b, t = cy - ph / 2, cy + ph / 2
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 248))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_initvals"):
            self._txt_initvals: dict = {}

        def tx(key: str, text: str, x: float, y: float, *,
               size: int = 11, color=COLOR_WHITE, bold: bool = False,
               anchor_x: str = "left") -> arcade.Text:
            obj = self._txt_initvals.get(key)
            if obj is None:
                obj = arcade.Text(text, x, y, color, size,
                                  bold=bold, anchor_x=anchor_x)
                self._txt_initvals[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            return obj

        tx("title", "INITIAL VALUES — Starting Budget", cx, t - 26,
           size=14, color=COLOR_GOLD, bold=True, anchor_x="center").draw()
        tx("hint",
           "Click a box to type. +/- step. Wheel scrolls goods.",
           cx, t - 46, size=9, color=COLOR_GRAY, anchor_x="center").draw()

        focus = self._editor_initvals_field

        def draw_field(label: str, field_key: str, steps, row_top: float,
                       label_color=COLOR_WHITE) -> None:
            """Draw one labelled numeric row: label, [-..] value [..+]."""
            row_b = row_top - self.INITVALS_ROW_H
            tx(f"lbl_{field_key}", label, l + 20, row_b + 5,
               size=11, color=label_color, bold=False).draw()
            # Step buttons + value box, right-aligned within the panel.
            box_w = 78
            step_w = 30
            gap = 3
            # Lay out from the right edge inward:
            #   [+small][+big]  <- right of value
            # Actually render: -big -small [value] +small +big.
            total_w = step_w * 4 + box_w + gap * 5
            x0 = r - 20 - total_w
            neg_steps = [s for s in steps if s < 0]
            pos_steps = [s for s in steps if s > 0]
            cur = x0
            # Negative steps (largest magnitude first → so order reads -big -small).
            for s in sorted(neg_steps):
                arcade.draw_lrbt_rectangle_filled(
                    cur, cur + step_w, row_b, row_b + self.INITVALS_ROW_H,
                    (70, 50, 40),
                )
                arcade.draw_lrbt_rectangle_outline(
                    cur, cur + step_w, row_b, row_b + self.INITVALS_ROW_H,
                    COLOR_UI_BORDER, 1,
                )
                tx(f"step_{field_key}_{s}", str(s),
                   cur + step_w / 2, row_b + 5, size=8, color=COLOR_WHITE,
                   anchor_x="center").draw()
                self._editor_initvals_rects.append(
                    (cur, cur + step_w, row_b, row_b + self.INITVALS_ROW_H,
                     f"step:{field_key}:{s}")
                )
                cur += step_w + gap
            # Value box (focusable).
            focused = (focus == field_key)
            arcade.draw_lrbt_rectangle_filled(
                cur, cur + box_w, row_b, row_b + self.INITVALS_ROW_H,
                (22, 18, 14),
            )
            arcade.draw_lrbt_rectangle_outline(
                cur, cur + box_w, row_b, row_b + self.INITVALS_ROW_H,
                COLOR_GOLD if focused else COLOR_UI_BORDER,
                2 if focused else 1,
            )
            val = self._initvals_get(field_key)
            val_text = f"{val}" + ("_" if focused else "")
            tx(f"val_{field_key}", val_text, cur + box_w / 2, row_b + 5,
               size=11, color=COLOR_GOLD if focused else COLOR_WHITE,
               bold=True, anchor_x="center").draw()
            self._editor_initvals_rects.append(
                (cur, cur + box_w, row_b, row_b + self.INITVALS_ROW_H,
                 f"focus:{field_key}")
            )
            cur += box_w + gap
            # Positive steps (smallest first → +small +big).
            for s in sorted(pos_steps):
                arcade.draw_lrbt_rectangle_filled(
                    cur, cur + step_w, row_b, row_b + self.INITVALS_ROW_H,
                    (50, 70, 45),
                )
                arcade.draw_lrbt_rectangle_outline(
                    cur, cur + step_w, row_b, row_b + self.INITVALS_ROW_H,
                    COLOR_UI_BORDER, 1,
                )
                tx(f"step_{field_key}_{s}", f"+{s}",
                   cur + step_w / 2, row_b + 5, size=8, color=COLOR_WHITE,
                   anchor_x="center").draw()
                self._editor_initvals_rects.append(
                    (cur, cur + step_w, row_b, row_b + self.INITVALS_ROW_H,
                     f"step:{field_key}:{s}")
                )
                cur += step_w + gap

        # Gold + population — fixed at the top, never scroll.
        top_y = t - 64
        draw_field("Gold (treasury)", "gold", self.INITVALS_GOLD_STEPS,
                   top_y, label_color=COLOR_GOLD)
        draw_field("Population", "population", self.INITVALS_POP_STEPS,
                   top_y - (self.INITVALS_ROW_H + 6), label_color=COLOR_GOLD)

        # Divider + goods section header.
        goods_hdr_y = top_y - 2 * (self.INITVALS_ROW_H + 6) - 4
        arcade.draw_line(l + 16, goods_hdr_y, r - 16, goods_hdr_y,
                         COLOR_UI_BORDER, 1)
        tx("goods_hdr", "Goods (granary + warehouse stocks)",
           l + 20, goods_hdr_y - 16, size=10, color=COLOR_GRAY, bold=True).draw()

        # Scrollable goods list. Clip region: between the header and the
        # button row. We draw only rows that fall inside the window.
        list_top = goods_hdr_y - 30
        list_bot = b + 56
        goods = self._editor_initvals_goods_order()
        row_stride = self.INITVALS_ROW_H + 4
        max_visible = max(1, int((list_top - list_bot) // row_stride))
        scroll = max(0, min(self._editor_initvals_scroll,
                            max(0, len(goods) - max_visible)))
        self._editor_initvals_scroll = scroll
        visible = goods[scroll:scroll + max_visible]
        from constants import NUTRIENT_LABELS, NUTRIENTS_SET
        for i, gid in enumerate(visible):
            row_top = list_top - i * row_stride
            is_nutrient = gid in NUTRIENTS_SET
            label = NUTRIENT_LABELS.get(gid, gid.replace("_", " ").title())
            draw_field(label, f"good:{gid}", self.INITVALS_GOOD_STEPS,
                       row_top,
                       label_color=(COLOR_WHITE if is_nutrient else COLOR_GRAY))
        # Scroll indicator.
        if len(goods) > max_visible:
            tx("scroll_ind",
               f"{scroll + 1}-{scroll + len(visible)} of {len(goods)}",
               r - 20, list_bot - 2, size=8, color=COLOR_GRAY,
               anchor_x="right").draw()

        # Reset / Confirm / Cancel.
        btn_w, btn_h = 120, 30
        gap = 10
        row_y = b + 14
        total = btn_w * 3 + gap * 2
        x0 = cx - total / 2
        for label, action, fill in (
            ("Reset",   "reset",   (70, 60, 45)),
            ("Confirm", "confirm", (60, 90, 50)),
            ("Cancel",  "cancel",  (90, 60, 50)),
        ):
            arcade.draw_lrbt_rectangle_filled(
                x0, x0 + btn_w, row_y, row_y + btn_h, fill,
            )
            arcade.draw_lrbt_rectangle_outline(
                x0, x0 + btn_w, row_y, row_y + btn_h, COLOR_GOLD, 1,
            )
            tx(f"btn_{action}", label, x0 + btn_w / 2, row_y + 8,
               size=12, color=COLOR_WHITE, bold=True, anchor_x="center").draw()
            self._editor_initvals_rects.append(
                (x0, x0 + btn_w, row_y, row_y + btn_h, f"btn:{action}")
            )
            x0 += btn_w + gap

    def _editor_initvals_handle_click(self, x: int, y: int) -> bool:
        """Hit-test the init-values modal. Returns True (always
        consumes clicks while open — it's modal). Clicking a value box
        focuses it; +/- steps adjust; the bottom buttons reset / commit
        / cancel; a click on empty space drops keyboard focus.
        """
        for x1, x2, y1, y2, action in self._editor_initvals_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                if action.startswith("btn:"):
                    which = action.split(":", 1)[1]
                    if which == "cancel":
                        self._editor_initvals_cancel()
                    elif which == "confirm":
                        self._editor_initvals_commit()
                    elif which == "reset":
                        self._editor_initvals_reset_template()
                    return True
                if action.startswith("focus:"):
                    self._editor_initvals_field = action.split(":", 1)[1]
                    return True
                if action.startswith("step:"):
                    _, field_key, delta = action.split(":", 2)
                    # field_key may itself contain a colon ("good:wheat");
                    # rsplit the delta off the end instead.
                    head = action[len("step:"):]
                    field_key, delta = head.rsplit(":", 1)
                    self._initvals_set(
                        field_key, self._initvals_get(field_key) + int(delta),
                    )
                    return True
                return True
        # Clicked inside the modal backdrop but not a control → drop focus.
        self._editor_initvals_field = None
        return True

    def _editor_initvals_handle_scroll(self, scroll_y: float) -> bool:
        """Mouse-wheel scroll for the goods list. Returns True if the
        form is open (so the caller suppresses world zoom).
        """
        if not self.editor_show_initvals:
            return False
        # Wheel up (positive) scrolls toward the top of the list.
        self._editor_initvals_scroll = max(
            0, self._editor_initvals_scroll - int(scroll_y),
        )
        return True

    def _editor_initvals_handle_key(self, symbol: int, modifiers: int) -> bool:
        """Keyboard handling for the init-values modal. Returns True if
        consumed.

        Esc closes (cancel). Enter commits. If a value box has focus,
        digits append, Backspace deletes, and the box overwrites on the
        first digit after focusing so the author isn't fighting the
        prefilled number.
        """
        if symbol == arcade.key.ESCAPE:
            self._editor_initvals_cancel()
            return True
        if symbol in (arcade.key.ENTER, arcade.key.RETURN, arcade.key.NUM_ENTER):
            self._editor_initvals_commit()
            return True
        field = self._editor_initvals_field
        if field is None:
            # No focused field: swallow keys so they don't leak to the
            # global hotkeys behind the modal, except let nothing through.
            return True
        # Digit entry.
        digit: int | None = None
        if arcade.key.KEY_0 <= symbol <= arcade.key.KEY_9:
            digit = symbol - arcade.key.KEY_0
        elif arcade.key.NUM_0 <= symbol <= arcade.key.NUM_9:
            digit = symbol - arcade.key.NUM_0
        if digit is not None:
            cur = self._initvals_get(field)
            # Cap at a sane ceiling so a held key can't overflow the box.
            new = min(cur * 10 + digit, 99_999_999)
            self._initvals_set(field, new)
            return True
        if symbol == arcade.key.BACKSPACE:
            self._initvals_set(field, self._initvals_get(field) // 10)
            return True
        # Tab / arrows could cycle fields in a future pass; for now,
        # consume everything so the modal stays airtight.
        return True

    def _draw_editor_toolbar(self) -> None:
        """Draw the editor's top toolbar (Save / Load / Rename / map
        name display / Exit-to-splash). Sits between the main top bar
        and the world view.

        Mouse hits land on rectangles tagged with action strings; the
        click handler dispatches via ``_editor_toolbar_dispatch``.
        """
        self._editor_toolbar_rects = []
        # Bar background. Lazy-import game_window to read TOP_BAR_H
        # without a module-load-time circular import.
        import game_window as _gw
        bar_top = self.height - _gw.TOP_BAR_H
        bar_bot = bar_top - self.EDITOR_TOOLBAR_H
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, bar_bot, bar_top, (45, 36, 28, 240),
        )
        arcade.draw_lrbt_rectangle_outline(
            0, self.width, bar_bot, bar_top, COLOR_GOLD, 1,
        )
        # Lazy text pool — same pattern as the buildings editor.
        if not hasattr(self, "_txt_editor_toolbar"):
            self._txt_editor_toolbar: dict[str, arcade.Text] = {}

        def tlbl(key: str, x: float, y: float, color, size: int = 11,
                 bold: bool = False) -> arcade.Text:
            t = self._txt_editor_toolbar.get(key)
            if t is None:
                t = arcade.Text("", x, y, color, size, bold=bold)
                self._txt_editor_toolbar[key] = t
            t.x = x
            t.y = y
            t.color = color
            return t

        # Buttons (left-aligned).
        btn_y_b = bar_bot + 4
        btn_y_t = bar_top - 4
        x_cursor = self.EDITOR_TOOLBAR_BTN_PAD
        for key, label in (
            ("save",     "Save map"),
            ("load",     "Load map"),
            ("rename",   "Rename"),
            ("clear",    "Clear all"),
            # v0.50: starting-budget form. Opens a modal that edits the
            # live economy's treasury / population / per-good stocks —
            # exactly the values mapfile.collect_map_state already
            # serialises on save, so there's no new persistence path.
            # Prefilled from default_initial_values.py.
            ("initvals", "Init values"),
            # v0.28: per-map scheduled events. Opens a side panel
            # over the map editor; mutates game.event_manager.scheduled
            # in-place and round-trips via the existing save_map plumbing.
            ("triggers", "Triggers"),
        ):
            l = x_cursor
            r = l + self.EDITOR_TOOLBAR_BTN_W
            arcade.draw_lrbt_rectangle_filled(
                l, r, btn_y_b, btn_y_t, (60, 50, 40),
            )
            arcade.draw_lrbt_rectangle_outline(
                l, r, btn_y_b, btn_y_t, COLOR_UI_BORDER, 1,
            )
            t = tlbl(f"btn_{key}", (l + r) / 2, btn_y_b + 7, COLOR_WHITE, 11, True)
            t.text = label
            # Centre by re-anchoring the x.
            t.x = (l + r) / 2 - len(label) * 3
            t.draw()
            self._editor_toolbar_rects.append((l, r, btn_y_b, btn_y_t, key))
            x_cursor = r + self.EDITOR_TOOLBAR_BTN_PAD

        # Map name display + edit hint, occupies the centre.
        name_l = x_cursor + 12
        name_r = self.width - 220
        arcade.draw_lrbt_rectangle_filled(
            name_l, name_r, btn_y_b, btn_y_t, (22, 18, 14),
        )
        arcade.draw_lrbt_rectangle_outline(
            name_l, name_r, btn_y_b, btn_y_t, COLOR_UI_BORDER, 1,
        )
        nt = tlbl("map_name", name_l + 8, btn_y_b + 7, COLOR_GOLD, 11, True)
        nt.text = f"Map: {self.editor_map_name}"
        nt.draw()

        # Exit-to-splash button on the right.
        exit_r = self.width - self.EDITOR_TOOLBAR_BTN_PAD
        exit_l = exit_r - self.EDITOR_TOOLBAR_BTN_W
        arcade.draw_lrbt_rectangle_filled(
            exit_l, exit_r, btn_y_b, btn_y_t, (80, 50, 40),
        )
        arcade.draw_lrbt_rectangle_outline(
            exit_l, exit_r, btn_y_b, btn_y_t, COLOR_GOLD, 1,
        )
        et = tlbl("btn_exit", (exit_l + exit_r) / 2 - 28, btn_y_b + 7,
                  COLOR_GOLD, 11, True)
        et.text = "Exit editor"
        et.draw()
        self._editor_toolbar_rects.append((exit_l, exit_r, btn_y_b, btn_y_t, "exit"))

        # v0.19.x: second row — terrain / feature paint tools.
        self._draw_editor_paint_toolbar(bar_bot)

    def _draw_editor_paint_toolbar(self, top_y: float) -> None:
        """Draw the second toolbar row that hosts terrain & feature
        paint tools.

        Lives directly beneath the main editor toolbar (passed as
        ``top_y``). Each entry in ``EDITOR_PAINT_TOOLS`` becomes a
        clickable button; the currently-selected tool is highlighted
        gold. Click on a button → ``editor_paint_tool`` flips to the
        button's id; subsequent world-clicks paint instead of placing.
        """
        self._editor_paint_rects = []
        bar_top = top_y
        bar_bot = bar_top - self.EDITOR_TOOLBAR_H
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, bar_bot, bar_top, (38, 30, 24, 240),
        )
        arcade.draw_lrbt_rectangle_outline(
            0, self.width, bar_bot, bar_top, (90, 70, 50), 1,
        )
        # Reuse the existing toolbar text pool — keys prefixed with
        # "paint_" so they don't collide with the upper toolbar.
        if not hasattr(self, "_txt_editor_toolbar"):
            self._txt_editor_toolbar = {}

        def tlbl(key: str, x: float, y: float, color, size: int = 10,
                 bold: bool = False) -> arcade.Text:
            t = self._txt_editor_toolbar.get(key)
            if t is None:
                t = arcade.Text("", x, y, color, size, bold=bold)
                self._txt_editor_toolbar[key] = t
            t.x = x
            t.y = y
            t.color = color
            return t

        btn_y_b = bar_bot + 4
        btn_y_t = bar_top - 4
        active = getattr(self, "editor_paint_tool", None) or "build"
        x_cursor = self.EDITOR_PAINT_BTN_PAD
        for tool_id, label, _kind, _payload in self.EDITOR_PAINT_TOOLS:
            l = x_cursor
            r = l + self.EDITOR_PAINT_BTN_W
            sel = (tool_id == active)
            fill = (90, 70, 40) if sel else (50, 42, 35)
            arcade.draw_lrbt_rectangle_filled(l, r, btn_y_b, btn_y_t, fill)
            arcade.draw_lrbt_rectangle_outline(
                l, r, btn_y_b, btn_y_t,
                COLOR_GOLD if sel else (80, 70, 60),
                2 if sel else 1,
            )
            t = tlbl(
                f"paint_{tool_id}", (l + r) / 2, btn_y_b + 8,
                COLOR_GOLD if sel else COLOR_WHITE, 10, True,
            )
            t.text = label
            t.x = (l + r) / 2 - len(label) * 3
            t.draw()
            self._editor_paint_rects.append(
                (l, r, btn_y_b, btn_y_t, f"paint:{tool_id}")
            )
            x_cursor = r + self.EDITOR_PAINT_BTN_PAD

        # v0.26: third row — grid dimension controls. Drawn below the
        # paint toolbar so it doesn't intrude on the existing layout.
        self._draw_editor_grid_dim_toolbar(bar_bot)

    def _draw_editor_grid_dim_toolbar(self, top_y: float) -> None:
        """v0.26: third editor toolbar row — width / height controls.

        Shows the current map dimensions and exposes four stepping
        buttons per axis: -10, -, +, +10. Hitting a button mutates
        the live GameMap dimensions via ``_editor_resize_grid``,
        which rebuilds the GameMap (and the road network + service
        map + pathfinder) at the new size. Buildings outside the
        new bounds are dropped — same semantics as the load path.

        Layout:
        ``  Width: [-10] [-] <NN> [+] [+10]   Height: [-10] [-] <NN> [+] [+10]``
        Sits flush against the right of the world view so the
        author can read the numbers while painting on the left.
        """
        self._editor_grid_dim_rects = []
        bar_top = top_y
        bar_bot = bar_top - self.EDITOR_TOOLBAR_H
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, bar_bot, bar_top, (30, 24, 20, 240),
        )
        arcade.draw_lrbt_rectangle_outline(
            0, self.width, bar_bot, bar_top, (90, 70, 50), 1,
        )
        if not hasattr(self, "_txt_editor_toolbar"):
            self._txt_editor_toolbar = {}

        def tlbl(key: str, x: float, y: float, color,
                 size: int = 10, bold: bool = False) -> arcade.Text:
            t = self._txt_editor_toolbar.get(key)
            if t is None:
                t = arcade.Text("", x, y, color, size, bold=bold)
                self._txt_editor_toolbar[key] = t
            t.x = x
            t.y = y
            t.color = color
            return t

        btn_y_b = bar_bot + 4
        btn_y_t = bar_top - 4
        # ── Width row ───────────────────────────────────────────────
        x_cursor = self.EDITOR_PAINT_BTN_PAD
        # Section label.
        lab = tlbl("griddim_w_lbl", x_cursor, btn_y_b + 7, COLOR_GOLD, 11, True)
        lab.text = "Width:"
        lab.draw()
        x_cursor += 56

        # Four buttons + numeric readout in the middle. Reused for
        # both axes — factored into a small inline helper.
        def step_button(label: str, action: str, w: int = 32) -> None:
            nonlocal x_cursor
            l = x_cursor
            r = l + w
            arcade.draw_lrbt_rectangle_filled(l, r, btn_y_b, btn_y_t, (60, 50, 40))
            arcade.draw_lrbt_rectangle_outline(l, r, btn_y_b, btn_y_t, COLOR_UI_BORDER, 1)
            t = tlbl(f"griddim_{action}", (l + r) / 2 - len(label) * 3, btn_y_b + 7,
                     COLOR_WHITE, 11, True)
            t.text = label
            t.draw()
            self._editor_grid_dim_rects.append((l, r, btn_y_b, btn_y_t, action))
            x_cursor = r + 4

        step_button("-10", "w_minus10")
        step_button("-",   "w_minus1", w=24)
        # Numeric width readout.
        nl = x_cursor
        nr = nl + 44
        arcade.draw_lrbt_rectangle_filled(nl, nr, btn_y_b, btn_y_t, (22, 18, 14))
        arcade.draw_lrbt_rectangle_outline(nl, nr, btn_y_b, btn_y_t, COLOR_UI_BORDER, 1)
        nt = tlbl("griddim_w_val", (nl + nr) / 2 - 10, btn_y_b + 7, COLOR_GOLD, 11, True)
        nt.text = str(self.game_map.cols)
        nt.draw()
        x_cursor = nr + 4
        step_button("+",   "w_plus1",  w=24)
        step_button("+10", "w_plus10")

        # ── Height row ──────────────────────────────────────────────
        x_cursor += 30
        lab = tlbl("griddim_h_lbl", x_cursor, btn_y_b + 7, COLOR_GOLD, 11, True)
        lab.text = "Height:"
        lab.draw()
        x_cursor += 64
        step_button("-10", "h_minus10")
        step_button("-",   "h_minus1", w=24)
        nl = x_cursor
        nr = nl + 44
        arcade.draw_lrbt_rectangle_filled(nl, nr, btn_y_b, btn_y_t, (22, 18, 14))
        arcade.draw_lrbt_rectangle_outline(nl, nr, btn_y_b, btn_y_t, COLOR_UI_BORDER, 1)
        nt = tlbl("griddim_h_val", (nl + nr) / 2 - 10, btn_y_b + 7, COLOR_GOLD, 11, True)
        nt.text = str(self.game_map.rows)
        nt.draw()
        x_cursor = nr + 4
        step_button("+",   "h_plus1",  w=24)
        step_button("+10", "h_plus10")

        # Hint text on the right of the bar.
        hint = tlbl("griddim_hint", x_cursor + 18, btn_y_b + 7, COLOR_GRAY, 9, False)
        hint.text = "Resize wipes buildings outside new bounds"
        hint.draw()

    # Hard bounds for the editor grid resize. The lower floor (8) is
    # "narrower than the default viewport"; the upper ceiling is
    # EDITOR_GRID_MAX (v0.27: 128). Pre-v0.27 the upper ceiling was
    # the module-level GRID_COLS / GRID_ROWS — going larger would
    # have exceeded the bounds that services / walkers / pathfinding
    # captured at import time. The v0.27 refactor moved those
    # bounds onto the live game_map, so up to 128×128 is now safe.
    EDITOR_GRID_MIN = 8

    def _editor_resize_grid(self, dw: int = 0, dh: int = 0) -> None:
        """v0.26: apply a delta to the live GameMap dimensions.

        Positive deltas grow the map; negative ones shrink it. The
        resulting size is clamped to ``[EDITOR_GRID_MIN, EDITOR_GRID_MAX]``
        per axis. v0.27 raises the ceiling to 128 (was: GRID_COLS /
        GRID_ROWS, i.e. 40 × 30) after refactoring services /
        walkers / pathfinding to use the live game_map dimensions.

        Currently-placed buildings are preserved when they fit in the
        new bounds; those outside are dropped (a downsize is
        destructive in the same way the load path is).
        """
        from mapfile import _rebuild_game_map_at_size
        from constants import EDITOR_GRID_MAX as _MAX

        cur_w, cur_h = self.game_map.cols, self.game_map.rows
        new_w = max(self.EDITOR_GRID_MIN, min(_MAX, cur_w + dw))
        new_h = max(self.EDITOR_GRID_MIN, min(_MAX, cur_h + dh))
        if (new_w, new_h) == (cur_w, cur_h):
            self._notify("Grid size at limit", COLOR_GRAY)
            return
        # Snapshot the current world so we can re-stamp it onto the
        # resized GameMap. Buildings whose origin falls outside the
        # new bounds are dropped silently.
        snapshot = self.game_map.to_dict()
        _rebuild_game_map_at_size(self, new_h, new_w)
        # Re-stamp the snapshot. from_dict already bounds-checks via
        # `0 <= r < self.rows` (we left those internal checks in the
        # bulk rename), so out-of-bounds buildings are dropped.
        self.game_map.from_dict(snapshot)
        self._notify(f"Grid resized to {new_w}×{new_h}", COLOR_GOLD)
        log.info("Editor: grid resized to %d×%d", new_w, new_h)

    def _editor_toolbar_dispatch(self, action: str) -> None:
        if action == "save":
            self._editor_save_map()
            return
        if action == "load":
            self.editor_show_load_picker = not self.editor_show_load_picker
            return
        if action == "rename":
            self._editor_rename_buffer = self.editor_map_name
            self.editor_show_rename_dialog = True
            return
        if action == "clear":
            self._editor_clear_world()
            return
        if action == "initvals":
            # v0.50: open the starting-budget form. Seeds its editable
            # buffer from the *current* live economy (which a fresh
            # editor sets from default_initial_values via the bootstrap,
            # or which a loaded map already populated).
            self._editor_open_initvals()
            return
        if action == "triggers":
            # v0.28: toggle the scheduled-events side panel.
            self.show_scheduled_panel = not self.show_scheduled_panel
            self.scheduled_dropdown_open = None
            self.scheduled_text_field = None
            self._scheduled_text_buffer = ""
            return
        if action == "exit":
            self._editor_exit_to_splash()
            return
        log.warning("Editor toolbar: unknown action %r", action)

    def _editor_save_map(self) -> None:
        """Persist the current editor state to ``./data/maps/<slug>.json``."""
        try:
            from mapfile import save_map
            path = save_map(self, self.editor_map_name)
            self._notify(f"Map saved → {path}", COLOR_GREEN)
        except Exception as e:  # noqa: BLE001
            log.exception("Map save failed")
            self._notify(f"Save failed: {e}", COLOR_RED)

    def _editor_clear_world(self) -> None:
        """Remove every placed building. The terrain layer is left
        alone — clearing terrain features is a separate concern (the
        author may want a custom forest layout to persist while
        rearranging buildings)."""
        positions = list(self.game_map.get_building_positions())
        for _bt, r, c in positions:
            self.game_map.remove_building(r, c)
        self._notify(f"Cleared {len(positions)} buildings", COLOR_GRAY)

    def _editor_exit_to_splash(self) -> None:
        """Tear down the editor state and return to splash. The
        in-memory world is discarded — same semantics as quitting a
        playing game and going back to splash via the splash 'New
        game' flow."""
        self.app_state = "splash"
        self.show_menu = False
        self.editor_show_load_picker = False
        self.editor_show_rename_dialog = False
        self._notify("Returned to main menu", COLOR_GRAY)

    def _draw_editor_load_picker(self) -> None:
        """Modal list of available maps. One row per file in
        ``./data/maps``; click a row to load it. Click anywhere
        outside to dismiss without loading.
        """
        self._editor_load_picker_rects = []
        from mapfile import list_maps
        files = list_maps()
        # Centred panel.
        cx = self.width / 2
        cy = self.height / 2
        pw = 480
        ph = max(180, 60 + 28 * max(1, min(len(files), 12)))
        l = cx - pw / 2
        r = cx + pw / 2
        b = cy - ph / 2
        t = cy + ph / 2
        # Backdrop (eats clicks outside the panel via the dispatcher).
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 170),
        )
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_editor_picker_title"):
            self._txt_editor_picker_title = arcade.Text(
                "LOAD MAP", 0, 0, COLOR_GOLD, 14, bold=True, anchor_x="center",
            )
            self._txt_editor_picker_hint = arcade.Text(
                "Click a map to load it. Esc to cancel.",
                0, 0, COLOR_GRAY, 10, anchor_x="center",
            )
        self._txt_editor_picker_title.x = cx
        self._txt_editor_picker_title.y = t - 26
        self._txt_editor_picker_title.draw()
        self._txt_editor_picker_hint.x = cx
        self._txt_editor_picker_hint.y = t - 44
        self._txt_editor_picker_hint.draw()

        if not files:
            if not hasattr(self, "_txt_editor_picker_empty"):
                self._txt_editor_picker_empty = arcade.Text(
                    "(no maps in ./data/maps yet — Save one first)",
                    0, 0, COLOR_GRAY, 11, anchor_x="center",
                )
            self._txt_editor_picker_empty.x = cx
            self._txt_editor_picker_empty.y = cy
            self._txt_editor_picker_empty.draw()
            return

        # Lazy row text pool.
        if not hasattr(self, "_txt_editor_picker_rows"):
            self._txt_editor_picker_rows: list[arcade.Text] = []
        while len(self._txt_editor_picker_rows) < len(files):
            self._txt_editor_picker_rows.append(
                arcade.Text("", 0, 0, COLOR_WHITE, 11)
            )

        row_h = 24
        for i, fname in enumerate(files):
            row_t = t - 64 - i * row_h
            row_b = row_t - row_h
            if row_b < b + 12:
                # Off the panel; ignore overflow rows.
                break
            arcade.draw_lrbt_rectangle_filled(
                l + 8, r - 8, row_b, row_t, (45, 38, 30),
            )
            arcade.draw_lrbt_rectangle_outline(
                l + 8, r - 8, row_b, row_t, COLOR_UI_BORDER, 1,
            )
            tx = self._txt_editor_picker_rows[i]
            tx.text = f"  {fname}"
            tx.x = l + 12
            tx.y = row_b + 6
            tx.draw()
            self._editor_load_picker_rects.append(
                (l + 8, r - 8, row_b, row_t, f"pick:{fname}")
            )

    def _draw_editor_rename_dialog(self) -> None:
        """Tiny modal: shows the current map name in an editable text
        box. Typing letters/numbers in the dialog appends to the
        buffer (handled in ``on_key_press``); Backspace deletes;
        Enter commits; Esc cancels. We don't get a native text input
        from arcade, so the buffer is mutated by key events.
        """
        cx = self.width / 2
        cy = self.height / 2
        pw, ph = 460, 160
        l = cx - pw / 2
        r = cx + pw / 2
        b = cy - ph / 2
        t = cy + ph / 2
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 170),
        )
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_editor_rename_title"):
            self._txt_editor_rename_title = arcade.Text(
                "MAP NAME", 0, 0, COLOR_GOLD, 14, bold=True, anchor_x="center",
            )
            self._txt_editor_rename_hint = arcade.Text(
                "Type to edit. Enter to confirm, Esc to cancel.",
                0, 0, COLOR_GRAY, 10, anchor_x="center",
            )
            self._txt_editor_rename_buf = arcade.Text(
                "", 0, 0, COLOR_WHITE, 14, bold=True, anchor_x="center",
            )
        self._txt_editor_rename_title.x = cx
        self._txt_editor_rename_title.y = t - 28
        self._txt_editor_rename_title.draw()
        self._txt_editor_rename_hint.x = cx
        self._txt_editor_rename_hint.y = b + 16
        self._txt_editor_rename_hint.draw()
        # Text box.
        ib_l = l + 24
        ib_r = r - 24
        ib_b = cy - 16
        ib_t = cy + 16
        arcade.draw_lrbt_rectangle_filled(
            ib_l, ib_r, ib_b, ib_t, (22, 18, 14),
        )
        arcade.draw_lrbt_rectangle_outline(
            ib_l, ib_r, ib_b, ib_t, COLOR_GOLD, 1,
        )
        # Show the buffer with a trailing '_' caret indicator.
        self._txt_editor_rename_buf.text = (self._editor_rename_buffer or "") + "_"
        self._txt_editor_rename_buf.x = cx
        self._txt_editor_rename_buf.y = ib_b + 6
        self._txt_editor_rename_buf.draw()

    def _editor_paint_at(self, row: int, col: int) -> bool:
        """Apply the current paint tool to ``(row, col)``. Returns
        True when paint was applied (so the caller can skip the
        normal building-placement flow).

        Painting mutates ``game_map.terrain`` (base terrain) or
        ``game_map.terrain_features`` (overlay layer) directly. We
        avoid clobbering tiles that already host a building — the
        author can demolish the building first if they want to
        retexture the ground beneath it.
        """
        # v0.29: bounds-check against the live map, not the default
        # GRID_ROWS/GRID_COLS — otherwise the editor silently refuses
        # to paint anything past row 29 / col 39 on a resized map.
        if not (0 <= row < self.game_map.rows and 0 <= col < self.game_map.cols):
            return False
        tool_id = getattr(self, "editor_paint_tool", None)
        if not tool_id or tool_id == "build":
            return False
        # Look up the tool's kind/payload.
        spec = next(
            (entry for entry in self.EDITOR_PAINT_TOOLS if entry[0] == tool_id),
            None,
        )
        if spec is None:
            return False
        _id, _label, kind, payload = spec
        # Refuse to paint if a building sits on this cell — the
        # author should demolish first. This also dodges the case
        # where a feature change would break a building's placement
        # gate (lumber mill needs forest, etc.).
        if self.game_map.grid[row][col] is not None:
            self._notify("Demolish first before painting", COLOR_RED)
            return True   # consumed — no fallthrough to placement
        if kind == "terrain":
            self.game_map.terrain[row][col] = int(payload)
            # Painting mountains/water also clears any overlay
            # feature on that tile — a stone deposit on a water
            # tile would render confusingly.
            if int(payload) in (TERRAIN_WATER, TERRAIN_MOUNTAINS, TERRAIN_DESERT):
                self.game_map.terrain_features[row][col] = None
            self.game_map.invalidate_render_cache()  # v0.56
            log.info("Editor: painted terrain %s at (%d,%d)", payload, row, col)
            return True
        if kind == "feature":
            # v0.26: `fish` is the one feature that belongs on WATER —
            # the fishery extracts from water tiles. Auto-flip the
            # cell to water and ensure no stale overlay is there.
            # Every other feature (forest, fertile_soil, the ore
            # veins, stone_deposit, groundwater, clay_deposit) lives
            # on grass, so we auto-flip to grass for those.
            if str(payload) == "fish":
                self.game_map.terrain[row][col] = TERRAIN_WATER
            else:
                t = self.game_map.terrain[row][col]
                if t not in (TERRAIN_GRASS, TERRAIN_GRASS_ALT):
                    self.game_map.terrain[row][col] = TERRAIN_GRASS
            self.game_map.terrain_features[row][col] = str(payload)
            # v0.26: seed reserves for the newly-painted feature so
            # the middle-click bubble has a number to show and the
            # extractor logic has a stock to draw down. Lookup goes
            # through `Balance.initial_feature_reserves` (same table
            # the procedural generator uses), with a sensible None
            # fallback for inexhaustible features.
            self._seed_feature_reserves(row, col, str(payload))
            self.game_map.invalidate_render_cache()  # v0.56
            log.info("Editor: painted feature %s at (%d,%d)", payload, row, col)
            return True
        if kind == "clear_feature":
            self.game_map.terrain_features[row][col] = None
            # Also drop the per-tile state — otherwise a future
            # repaint of the same tile would see stale `initial` /
            # `reserves` values from the old feature kind.
            self.game_map.terrain_feature_state.pop((row, col), None)
            self.game_map.invalidate_render_cache()  # v0.56
            log.info("Editor: cleared feature at (%d,%d)", row, col)
            return True
        return False

    def _seed_feature_reserves(self, row: int, col: int, feat: str) -> None:
        """v0.26: stamp `terrain_feature_state[(r,c)]` for a feature the
        editor just painted.

        Uses ``balance.FEATURE_RESERVES_DEFAULT`` so a hand-painted
        clay deposit or iron vein carries the same initial stock as a
        procedurally-generated one. Inexhaustible features (stone,
        fertile soil, groundwater) get a ``None`` reserve entry — the
        round-trip is symmetric with ``GameMap._init_feature_reserves``.
        """
        try:
            from balance import FEATURE_RESERVES_DEFAULT
        except ImportError:
            FEATURE_RESERVES_DEFAULT = {}
        default = FEATURE_RESERVES_DEFAULT.get(feat)
        self.game_map.terrain_feature_state[(row, col)] = {
            "reserves": default,
            "initial":  default,
        }

    def _editor_handle_toolbar_click(self, x: int, y: int) -> bool:
        """Hit-test the editor toolbar / picker / rename dialog.
        Returns True if the click was consumed.
        """
        # v0.28: scheduled panel eats clicks before everything else
        # when open. It's a modal sub-panel on top of the editor.
        if self.show_scheduled_panel:
            if self._scheduled_panel_handle_click(x, y):
                return True
        # v0.50: init-values modal eats every click while open.
        if getattr(self, "editor_show_initvals", False):
            return self._editor_initvals_handle_click(x, y)
        # Rename dialog eats every click while open (use Enter/Esc).
        if self.editor_show_rename_dialog:
            return True
        # Load picker: click a row → load; click outside → dismiss.
        if self.editor_show_load_picker:
            for x1, x2, y1, y2, action in self._editor_load_picker_rects:
                if x1 <= x <= x2 and y1 <= y <= y2:
                    if action.startswith("pick:"):
                        fname = action.split(":", 1)[1]
                        self._editor_do_load_map(fname)
                        self.editor_show_load_picker = False
                    return True
            # Clicked outside any row → dismiss.
            self.editor_show_load_picker = False
            return True
        # Toolbar buttons.
        for x1, x2, y1, y2, action in self._editor_toolbar_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                self._editor_toolbar_dispatch(action)
                return True
        # v0.19.x: paint toolbar buttons (second row).
        for x1, x2, y1, y2, action in getattr(self, "_editor_paint_rects", []):
            if x1 <= x <= x2 and y1 <= y <= y2:
                if action.startswith("paint:"):
                    tool_id = action.split(":", 1)[1]
                    if tool_id == "build":
                        self.editor_paint_tool = None
                        log.info("Editor: paint tool cleared (build mode)")
                    else:
                        self.editor_paint_tool = tool_id
                        log.info("Editor: paint tool → %s", tool_id)
                return True
        # v0.26: grid dimension buttons (third row). Action ids encode
        # axis + step: w_minus10 / w_minus1 / w_plus1 / w_plus10 and
        # the matching h_* set. Width steps dw; height steps dh.
        for x1, x2, y1, y2, action in getattr(self, "_editor_grid_dim_rects", []):
            if x1 <= x <= x2 and y1 <= y <= y2:
                dw, dh = 0, 0
                if action == "w_minus10": dw = -10
                elif action == "w_minus1": dw = -1
                elif action == "w_plus1":  dw = +1
                elif action == "w_plus10": dw = +10
                elif action == "h_minus10": dh = -10
                elif action == "h_minus1": dh = -1
                elif action == "h_plus1":  dh = +1
                elif action == "h_plus10": dh = +10
                if dw or dh:
                    self._editor_resize_grid(dw=dw, dh=dh)
                return True
        return False

    def _editor_do_load_map(self, filename: str) -> None:
        from mapfile import load_map
        ok = load_map(self, filename)
        if ok:
            self._notify(f"Loaded {filename}", COLOR_GREEN)
        else:
            self._notify(f"Load failed: {filename}", COLOR_RED)


    def _editor_rename_commit(self) -> None:
        new = (self._editor_rename_buffer or "").strip()
        if new:
            self.editor_map_name = new[: self.EDITOR_MAP_NAME_MAX]
        self.editor_show_rename_dialog = False

    def _editor_rename_cancel(self) -> None:
        self.editor_show_rename_dialog = False
        self._editor_rename_buffer = self.editor_map_name

    def _editor_rename_handle_key(self, symbol: int, modifiers: int) -> bool:
        """Buffer mutation for the rename dialog. Returns True if the
        key was consumed. Letters/digits/space/dash/underscore go
        into the buffer; Backspace removes the last char; Enter
        commits; Esc cancels.
        """
        if symbol == arcade.key.ENTER or symbol == arcade.key.RETURN:
            self._editor_rename_commit()
            return True
        if symbol == arcade.key.ESCAPE:
            self._editor_rename_cancel()
            return True
        if symbol == arcade.key.BACKSPACE:
            self._editor_rename_buffer = (self._editor_rename_buffer or "")[:-1]
            return True
        # Map a small set of arcade key codes to characters. Anything
        # outside this set is ignored — the dialog stays predictable
        # without us trying to handle every keyboard layout.
        ch: str | None = None
        if arcade.key.A <= symbol <= arcade.key.Z:
            base = symbol - arcade.key.A
            ch = chr(ord("a") + base)
            shift = bool(modifiers & arcade.key.MOD_SHIFT)
            if shift:
                ch = ch.upper()
        elif arcade.key.KEY_0 <= symbol <= arcade.key.KEY_9:
            ch = chr(ord("0") + symbol - arcade.key.KEY_0)
        elif symbol == arcade.key.SPACE:
            ch = " "
        elif symbol == arcade.key.MINUS:
            ch = "-"
        elif symbol == arcade.key.UNDERSCORE:
            ch = "_"
        if ch is not None and len(self._editor_rename_buffer or "") < self.EDITOR_MAP_NAME_MAX:
            self._editor_rename_buffer = (self._editor_rename_buffer or "") + ch
            return True
        return False

    # ── v0.19.x: buildings-editor sprite filename text input ────────
    EDITOR_FILENAME_MAX = 64

    def _editor_filename_handle_key(self, symbol: int, modifiers: int) -> bool:
        """Buffer mutation for the buildings-editor sprite filename
        text input. Returns True if the key was consumed.

        Accepted: letters, digits, space, dash, underscore, dot.
        Enter commits the buffer to ``editor_sprite_filename`` (only
        if the extension is jpg/jpeg/png — otherwise the input stays
        active and a notification flags the rejection). Esc cancels;
        Backspace removes the last char.
        """
        if symbol in (arcade.key.ENTER, arcade.key.RETURN, arcade.key.NUM_ENTER):
            buf = (self._editor_text_buffer or "").strip()
            ext = buf.rsplit(".", 1)[-1].lower() if "." in buf else ""
            if buf and ext in ("jpg", "jpeg", "png"):
                self.editor_sprite_filename = buf
                self.editor_text_input_field = None
                self.editor_dirty = True
                log.info("Editor: filename committed → %s", buf)
            else:
                self._notify(
                    "Filename must end in .jpg / .jpeg / .png",
                    COLOR_RED,
                )
            return True
        if symbol == arcade.key.ESCAPE:
            self.editor_text_input_field = None
            self._editor_text_buffer = ""
            return True
        if symbol == arcade.key.BACKSPACE:
            self._editor_text_buffer = (self._editor_text_buffer or "")[:-1]
            return True
        ch: str | None = None
        if arcade.key.A <= symbol <= arcade.key.Z:
            base = symbol - arcade.key.A
            ch = chr(ord("a") + base)
            if bool(modifiers & arcade.key.MOD_SHIFT):
                ch = ch.upper()
        elif arcade.key.KEY_0 <= symbol <= arcade.key.KEY_9:
            ch = chr(ord("0") + symbol - arcade.key.KEY_0)
        elif symbol == arcade.key.PERIOD:
            ch = "."
        elif symbol == arcade.key.MINUS:
            ch = "-"
        elif symbol == arcade.key.UNDERSCORE:
            ch = "_"
        elif symbol == arcade.key.SPACE:
            ch = " "
        if ch is not None and len(self._editor_text_buffer or "") < self.EDITOR_FILENAME_MAX:
            self._editor_text_buffer = (self._editor_text_buffer or "") + ch
            return True
        return False
