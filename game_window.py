"""Main game window — rendering, input, UI, game loop.

Uses arcade.Text objects everywhere (no draw_text) for performance.
Two Camera2D instances: world_camera (zoom/pan) and gui_camera (fixed HUD).

v0.4 additions:
    * RoadNetwork wired through economy.update for connectivity gating
    * ServiceMap + HouseEvolution running each tick
    * Categorised building palette (TAB cycles)
    * Building info popup on click of an existing building
    * Mini-map (M to toggle, click to teleport)
    * ESC opens menu instead of instant quit
    * Resource icons + trend arrows in top bar
    * Building palette tooltips (hover)
"""
from __future__ import annotations

import logging
from collections import deque

import arcade

from balance import BALANCE
import bartering
import gold_trade
from building import BuildingRegistry
from constants import (
    BUILDINGS_PATH, CAMERA_PAN_SPEED,
    CAMERA_ZOOM_MAX, CAMERA_ZOOM_MIN, CAMERA_ZOOM_STEP,
    COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_RED, COLOR_UI_BG,
    COLOR_UI_BORDER, COLOR_UI_PANEL, COLOR_WHITE, COLOR_WATER,
    COLOR_GRASS, COLOR_GRASS_ALT, COLOR_HILLS, COLOR_MOUNTAINS,
    DATA_DIR, EVENTS_PATH,
    FEATURE_COLORS, FEATURE_LABELS,
    GRID_COLS, GRID_ROWS, HELP_TEXT, PALETTE_CATEGORIES,
    RESOURCE_COLORS,
    TERRAIN_GRASS, TERRAIN_GRASS_ALT, TERRAIN_WATER,
    TERRAIN_HILLS, TERRAIN_MOUNTAINS, TERRAIN_DESERT,
    TICKS_PER_SECOND,
    TILE_SIZE, TRADE_ROUTES, build_hotkey_map,
)
from caesar import CaesarRequestManager
from credits import CREDITS_TEXT
from decay import DecayManager
from diplomacy import DiplomacyTracker
from economy import EconomyManager
from events import EconomicEventManager
from game_map import GameMap
from house_evolution import HouseEvolution, get_house_tier
from pathfinding import Pathfinder
from rebellion import RebellionTracker
from recorder import TickRecorder
from action_recorder import ActionRecorder
from road_network import RoadNetwork
from saveload import load_game, save_game
from services import ServiceMap
from signals import signals
from stats import build_snapshot, format_lines
from storage import Storage
from textures import TextureRegistry
from trade import TradeRoute, TradeRouteManager
from units import UnitRegistry
from walkers import WalkerManager

# ── Mixins (cohesive method clusters extracted for maintainability) ─────────
# These mixins all operate on ``CaesarGameWindow`` state via ``self``; they
# carry no state of their own. The original method names and signatures are
# preserved so tests and other modules that reach into the window keep
# working without modification.
from mixins.scenario_layouts import ScenarioLayoutsMixin
from mixins.buildings_editor import BuildingsEditorMixin
from mixins.unit_editor import UnitEditorMixin
from mixins.map_editor import MapEditorMixin
from mixins.scheduled_events_panel import ScheduledEventsPanelMixin
from mixins.trade_panels import TradePanelsMixin
from mixins.diagnostic_panels import DiagnosticPanelsMixin
from mixins.splash_menu import SplashMenuMixin

log = logging.getLogger("caesar3.window")

# ── UI layout ────────────────────────────────────────────────────────────────
TOP_BAR_H = 36
BOTTOM_BAR_H = 86  # increased to fit category tabs above palette buttons
RIGHT_PANEL_W = 240
TAB_H = 22

MINIMAP_W = 160
MINIMAP_H = 120
MINIMAP_MARGIN = 8

INFO_PANEL_W = 220
INFO_PANEL_H = 280

MENU_W = 280
# v0.19: bumped from 380 to 580 to fit nine buttons (Resume,
# Save, Load, Map editor, Buildings editor, Unit editor,
# Bartering, Help, Quit). Each button is 40px tall + 10px gap.
MENU_H = 580

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SEASON_BY_MONTH = {
    1: "Winter", 2: "Winter", 3: "Spring", 4: "Spring", 5: "Spring",
    6: "Summer", 7: "Summer", 8: "Summer", 9: "Autumn", 10: "Autumn",
    11: "Autumn", 12: "Winter",
}

# v0.6: filter overlay names. Index 0 = no overlay; the rest are tinted
# heatmaps the player can cycle with `O`. Order matters (cycle order).
# Each entry is (label, kind) where kind is read by _draw_overlay to pick
# the right per-tile metric.
OVERLAY_NAMES: list[tuple[str, str]] = [
    ("Off",                "none"),
    ("Water need",         "service:water"),
    ("Food need",          "service:food"),
    ("Religion need",      "service:religion"),
    ("Entertainment need", "service:entertainment"),
    ("Education need",     "service:education"),
    ("Health need",        "service:health"),
    ("Defence",            "service:defence"),
    ("Unhappy areas",      "unhappy"),
    ("Road access",        "road_access"),
    # v0.17: heatmap of buildings short on staff. Red = empty, amber
    # = partially staffed, faint = lightly understaffed. Reads from
    # the same building_status snapshot the J panel uses, so the
    # two views agree on what's understaffed.
    ("Jobless / missing workers", "jobless"),
    # v0.19.x: heatmap of recent walker-carried-goods activity. Reads
    # ``walker_manager.flow_heat()`` — every active delivery walker
    # deposits on its current tile each tick, the manager decays the
    # whole map each tick. Bright tiles = busy delivery routes; dark
    # tiles = no recent traffic. Companion to v0.19's recorder: the
    # recorder gives the data answer ("X bread produced, Y consumed"),
    # this gives the visual one ("the bread is moving from here to
    # there").
    ("Goods flow",         "goods_flow"),
]


class CaesarGameWindow(
    ScenarioLayoutsMixin,
    BuildingsEditorMixin,
    UnitEditorMixin,
    MapEditorMixin,
    ScheduledEventsPanelMixin,
    TradePanelsMixin,
    DiagnosticPanelsMixin,
    SplashMenuMixin,
    arcade.Window,
):
    def __init__(self, width: int, height: int, title: str):
        super().__init__(width, height, title, resizable=False)
        arcade.set_background_color(COLOR_UI_BG)

    # ══════════════════════════════════════════════════════════════════════
    #  SETUP
    # ══════════════════════════════════════════════════════════════════════
    def setup(self) -> None:
        # ── Data layer ────────────────────────────────────────────────────
        self.registry = BuildingRegistry.from_json_file(BUILDINGS_PATH)
        self.balance = BALANCE
        self.hotkeys = build_hotkey_map()

        # ── Texture registry ──────────────────────────────────────────────
        # Loads PNGs from ./assets/textures/ on demand. Anything missing
        # falls back to the original colored-rectangle rendering, so the
        # game still runs end-to-end with zero PNGs on disk.
        self.textures = TextureRegistry()
        self._preload_textures()

        # Categorised palette (preserves declared order from JSON within
        # each category). Empty categories are dropped.
        cat_map = self.registry.by_category()
        self.palette_by_cat: list[tuple[str, str, list[str]]] = [
            (cat_id, cat_label, cat_map[cat_id])
            for cat_id, cat_label in PALETTE_CATEGORIES
            if cat_id in cat_map and cat_map[cat_id]
        ]
        self.active_cat_idx = 0
        # v0.19.x: per-tab horizontal scroll for the bottom palette.
        # Some tabs (industry, agriculture) hold more buildings than
        # fit on screen; the player pages through them with the ◀ ▶
        # buttons drawn at either end of the palette row, or by
        # turning the mouse-wheel while hovering the bar.
        self.palette_scroll: int = 0

        # v0.14: feature → list[building name] reverse index for the
        # hover tooltip. Computed once at startup. A feature appears
        # if any building either *needs* it (extractor gating) or
        # gets a yield bonus from it. The order follows registry
        # declaration order so the tooltip lists buildings in the
        # same order the player sees them in the palette.
        self._feature_consumers: dict[str, list[str]] = {}
        for bid, bd in self.registry.all().items():
            for feat in (bd.needs_feature or []):
                self._feature_consumers.setdefault(feat, []).append(bd.name)
            for feat in (bd.feature_yield_bonus or {}):
                consumers = self._feature_consumers.setdefault(feat, [])
                if bd.name not in consumers:
                    consumers.append(bd.name)

        # ── Systems ───────────────────────────────────────────────────────
        self.game_map = GameMap(self.registry, textures=self.textures)
        self.economy = EconomyManager(self.registry, self.balance)
        self.road_network = RoadNetwork(self.game_map, self.registry)
        self.pathfinder = Pathfinder(self.road_network)
        self.service_map = ServiceMap(self.game_map, self.registry)
        self._wire_service_staffing_gate()
        self.trade_manager = TradeRouteManager(self.economy)
        self._ensure_commercial_roads()
        self.event_manager = EconomicEventManager()
        # v0.36: trigger wiring (Event editor output). Empty list
        # by default; map.json's `triggers` block lands here via
        # mapfile._apply_world_to_game. The callbacks are bound late
        # because fire_cutscene_for_flag / fire_request_for_id /
        # on_flag_set are methods on self.
        from triggers import TriggerManager
        self.trigger_manager = TriggerManager(
            triggers=[],
            cutscene_callback=self.fire_cutscene_for_flag_id,
            request_callback=self.fire_request_for_id,
            flag_setter=self.fire_cutscene_for_flag,
            event_manager=self.event_manager,
            naval_raid_callback=self._dispatch_naval_raid,
        )
        # v0.36: scenario library overrides. Set by mapfile when a
        # scenario JSON declares its own cutscenes/requests/events
        # files; consumed by the lazy loaders below.
        self.scenario_libraries: dict[str, str] = {}
        self.scenario_triggers: list[dict] = []
        # v0.23.x: JSON-driven unit registry. Loaded from data/units.json;
        # falls back to a built-in set when the file is missing so the
        # game launches in fresh checkouts that haven't generated it.
        self.unit_registry = self._ensure_unit_registry()
        self.walker_manager = WalkerManager(
            self.registry, textures=self.textures, pathfinder=self.pathfinder,
            service_map=self.service_map,
            unit_registry=self._ensure_unit_registry(),
        )
        # House evolution depends on the walker_manager (for fresh-delivery
        # signals) so it's constructed last.
        self.house_evolution = HouseEvolution(
            self.game_map, self.registry, self.service_map, self.balance,
            walker_manager=self.walker_manager,
        )
        # ── v0.8: Caesar / diplomacy / rebellion ────────────────────────
        self.diplomacy = DiplomacyTracker(self.balance)
        self.caesar = CaesarRequestManager(self.balance)
        self.rebellion = RebellionTracker(self.balance)
        # Index of the last-processed Caesar history entry, so we don't
        # double-penalise diplomacy on a failed request.
        self._caesar_history_seen: int = 0
        # ── v0.9: per-warehouse stock allocation ───────────────────────
        self.storage = Storage(self.game_map, self.registry)
        # ── v0.9: building decay & maintenance ─────────────────────────
        self.decay = DecayManager(
            self.game_map, self.registry, self.service_map, self.balance,
        )

        # ── Game state ────────────────────────────────────────────────────
        self.selected_building: str = "house"
        self.paused: bool = False
        self.speed_multiplier: int = 1
        self.tick_accumulator: float = 0.0
        self.game_time: int = 0
        self.year: int = 1
        self.month: int = 1
        self.show_help: bool = False
        self.show_minimap: bool = True
        self.show_menu: bool = False
        # Post-v0.28: live frame profiler. Toggled with Ctrl+P. We
        # always *collect* the samples (the contextmanager overhead is
        # ~hundreds of nanoseconds per section) but only render the HUD
        # when the toggle is on, keeping the off-state cost negligible
        # while still letting the player flip it on mid-stutter and see
        # the last ~second of frames immediately.
        from profiler import Profiler  # local import: profiler avoids
                                       # arcade at import time
        self.profiler: Profiler = Profiler()
        self.show_profiler: bool = False
        # v0.15: scenario id selected from the splash. "default" is the
        # original starter city; "gallic_war" puts the houses on the
        # eastern bank with a different chain layout. Read by
        # `_place_starter_city` to dispatch on layout. Persisted to
        # save state in the game_meta block so a loaded game keeps
        # remembering which campaign you started in (no current
        # gameplay difference besides the layout, but the field is
        # there for events.json scenarios to key on).
        self.scenario: str = "default"

        # v0.15: buildings-editor state. `editor_open` toggles the
        # modal panel; the rest is scratch the panel reads. Game
        # logic continues to run when the editor is open *unless*
        # show_menu is also True (it isn't — menu closes when
        # editor opens). We keep the sim ticking so the player
        # can A/B-test edits without remembering to unpause; the
        # ramp is cheap because the panel is read-mostly until
        # the player taps a +/- button. If this turns out to be
        # confusing, flip `paused = True` when entering the
        # editor.
        self.editor_open: bool = False
        self.editor_selected_id: str = "house"
        self.editor_values: dict[str, int] = {}
        self.editor_dirty: bool = False
        # Hit-test rects rebuilt each draw, consumed by on_mouse_press.
        # Format: list of (x1, x2, y1, y2, action) where action is a
        # string the click handler dispatches on.
        self._editor_btn_rects: list[tuple[float, float, float, float, str]] = []
        # Scroll offset for the building list (in rows from the top).
        self.editor_scroll: int = 0
        # v0.30: buildings-editor scrollbar drag state. The proportional
        # thumb is draggable via mouse press → drag → release, and the
        # editor reads these flags from on_mouse_drag / on_mouse_release.
        # `_editor_thumb_drag_offset` is the distance from the mouse
        # y-coord to the thumb's top edge at the moment drag started,
        # so dragging feels anchored to wherever the user clicked.
        self._editor_thumb_dragging: bool = False
        self._editor_thumb_drag_offset: float = 0.0
        # Stash of the last-drawn scrollbar geometry. Set by
        # ``_draw_editor`` (rect = (sx1, sx2, track_b, track_t,
        # thumb_b, thumb_t, max_scroll, track_h, thumb_h)) and read
        # by the press / drag handlers.
        self._editor_scrollbar_rect: tuple | None = None
        # Stash of list-state from the last draw for keyboard nav and
        # mouse wheel. Populated by ``_draw_editor``.
        self._editor_list_rows_visible: int = 0
        self._editor_list_all_ids: list[str] = []
        self._editor_list_max_scroll: int = 0
        # v0.25: unit editor state, mirroring the buildings-editor
        # pattern. `unit_editor_values` snapshots the editable
        # scalars (hp, damage, speed, sight, training_time, cost,
        # upkeep, weapon_cost, patrol_radius, defense). `unit_editor_skills`
        # snapshots the skill list as a set for toggle-style editing.
        # `unit_editor_flags` snapshots boolean fields (ranged, armoured).
        self.unit_editor_selected_id: str = "light_infantry"
        self.unit_editor_values: dict[str, float] = {}
        self.unit_editor_flags: dict[str, bool] = {}
        self.unit_editor_skills: set[str] = set()
        self.unit_editor_dirty: bool = False
        self.unit_editor_scroll: int = 0
        self._unit_editor_btn_rects: list[tuple[float, float, float, float, str]] = []
        # v0.28: trigger-editor state. Splash-menu modal for editing
        # ``data/events.json``. Mirrors the buildings-editor structure:
        # scrollable list of events on the left + per-event scalar
        # editor in the middle + colour picker on the right. Save
        # writes back through the same atomic ``.tmp`` → rename
        # pattern. The editor edits a working list (snapshot of the
        # JSON file) and only commits on Save.
        #
        # We don't snapshot from ``event_manager.events`` because the
        # editor is reachable from splash *before* a game has been
        # started — and the running event manager (when one exists)
        # may have mutated its event list (modders' code might
        # re-bind it). Reading the JSON file directly keeps the
        # editor's source of truth aligned with what Save writes back.
        self.trigger_editor_open: bool = False
        self.trigger_editor_events: list[dict] = []
        self.trigger_editor_selected_idx: int = 0
        self.trigger_editor_scroll: int = 0
        self.trigger_editor_dirty: bool = False
        self._trigger_editor_btn_rects: list[tuple[float, float, float, float, str]] = []
        # Text-input focus for the trigger editor: None, "msg",
        # "name", or "modkey:<key>" (last is the key field of a new
        # modifier row before it's named).
        self.trigger_editor_text_field: str | None = None
        self._trigger_editor_text_buffer: str = ""
        # Resource / modifier picker overlays — None when closed,
        # otherwise "effect" or "modifier" string indicating which
        # pane the picker should add an entry to.
        self.trigger_editor_picker_open: str | None = None
        # v0.31: RPG request editor state. Materialised lazily on
        # first open via ``_splash_open_rpg_request_editor`` so the
        # import of ``rpg_request_editor`` (and its arcade.Text
        # cache) doesn't pay a startup cost for players who never
        # open the editor. Implementation lives in
        # ``rpg_request_editor.py`` rather than this file — at 10k
        # lines we're already past the comfortable size for a single
        # module, and the new editor is self-contained enough to
        # extract cleanly.
        self.rpg_request_editor_state = None  # type: ignore[assignment]
        # v0.37: Triggers editor state (splash menu). Same lazy-init
        # pattern as the RPG request editor — module lives in
        # ``triggers_editor.py``. Edits the ``triggers`` block on
        # ``data/scenarios/<name>/map.json`` so authors can wire
        # event/cutscene/flag effects to game-state conditions
        # without hand-editing JSON. Companion to the v0.28 Event
        # editor (which owns the library of WHAT events do).
        self.triggers_editor_state = None  # type: ignore[assignment]
        # v0.32: Cutscene cinematic editor state (splash menu) +
        # cutscene player state (runtime presenter). Same lazy-init
        # pattern as the RPG request editor — the modules live in
        # their own files so this game_window keeps a clean dispatch
        # surface. The editor reads/writes ``data/cutscenes.json``;
        # the player consumes the same library at runtime when a
        # trigger flag flips (see ``fire_cutscene_for_flag``).
        self.cutscene_editor_state = None  # type: ignore[assignment]
        self.cutscene_player_state = None  # type: ignore[assignment]
        # Library reload tracker — bumped each time the cutscene editor
        # writes to disk so the runtime player can re-read fresh.
        self._cutscenes_library: list[dict] | None = None
        # v0.36: RPG-request library cache + pending dispatch queue.
        # Lazy-loaded by ``fire_request_for_id`` (same pattern as
        # ``_cutscenes_library``); the queue feeds the modal display
        # once the RPG dispatcher consumes it.
        self._rpg_requests_library: list[dict] | None = None
        self._pending_rpg_requests: list[dict] = []
        # v0.38 (audit 7.1): the runtime presenter state + the delayed-
        # effect queue. ``rpg_player_state`` is created lazily the first
        # time a request fires (same pattern as cutscene_player_state).
        # ``_rpg_delayed_effects`` holds decisions whose ``delay_ticks``
        # is non-zero: each entry is ``(fire_at_tick, effects, pop, happy)``
        # and ``_game_tick`` applies them when ``game_time`` reaches
        # ``fire_at_tick``. Persisted by saveload so a "pay next month"
        # promise survives a save/load.
        self.rpg_player_state = None  # type: ignore[assignment]
        self._rpg_delayed_effects: list[tuple[int, dict, int, int]] = []
        self._rpg_was_paused: bool = False
        # v0.28: scheduled-events panel (lives inside the map editor).
        # Mutates ``game.event_manager.scheduled`` in-place; saves
        # round-trip through the existing ``save_map`` plumbing.
        self.show_scheduled_panel: bool = False
        self._scheduled_panel_rects: list[tuple[float, float, float, float, str]] = []
        # When non-None, identifies the dropdown currently open inside
        # the scheduled panel: "event:<idx>" or "kind:<idx>".
        self.scheduled_dropdown_open: str | None = None
        # Text-input focus for the value field of a scheduled row.
        # Format: "val:<idx>" or None.
        self.scheduled_text_field: str | None = None
        self._scheduled_text_buffer: str = ""
        # v0.7: top-level app state.
        #   "splash" — main menu over the marble-statue background, sim frozen
        #   "playing" — normal gameplay
        # The splash is the *first* screen the player sees; "New game" /
        # "Load game" / "Start campaign" transition into "playing". The
        # in-game ESC menu (show_menu) is independent — it pauses the sim
        # but doesn't return to splash.
        self.app_state: str = "splash"
        # Hover index for the splash buttons (0..N-1) or None.
        self.splash_hover: int | None = None
        # Credits panel toggle, drawn only over the splash screen.
        self.show_credits: bool = False
        # v0.52: "Mapping keys keyboards" overlay toggle (splash only) —
        # lists every key binding plus the on-disk save/config folders.
        self.show_keymap: bool = False
        # Splash buttons. Order is the spec from the brief. Actions are
        # bound after _menu_actions exists (a few attributes below).
        # We store (label, callback) once methods are defined.
        # Click-inspected building origin or None.
        self.inspected: tuple[int, int] | None = None
        # v0.11: hit-test rects for the warehouse inspector's accept/reject
        # toggle buttons. Re-populated each draw; consulted by on_mouse_press
        # before falling through to world-click logic. Each entry is
        # (x1, x2, y1, y2, action) where action is either a good name (toggle
        # accepts for that good) or the literal string "__empty__" for the
        # 'Drain' button. Empty when the inspector is closed or not viewing
        # a storage building.
        self._warehouse_btn_rects: list[tuple[int, int, int, int, str]] = []
        # v0.41: shipyard build-queue button rects (add-ship + per-row
        # remove). Same (x1,x2,y1,y2,action) shape as the warehouse rects.
        # Empty unless the inspector is viewing a shipyard.
        self._shipyard_btn_rects: list[tuple[int, int, int, int, str]] = []
        # Hover-over palette button index → tooltip data.
        self.palette_hover: int | None = None
        # Rolling resource history for trend arrows (last 6 ticks).
        self._res_history: dict[str, deque] = {
            r: deque(maxlen=6) for r in ("food", "wood", "iron", "tools", "money")
        }
        # v0.6: longer history for graph plots. 240 samples ≈ 2 minutes
        # at 2 ticks/sec — enough to spot trends without becoming noisy.
        # v0.15: four extra metrics — fed_fraction (predicts unrest
        # before food runs out), food_net (food prod−cons; predicts a
        # crash earlier than the absolute food stockpile), employed_ratio
        # (filled/needed jobs; predicts production drag), and
        # stone_blocks (the v0.13 civic-construction bottleneck —
        # players watch this when planning temples / senate). Total
        # rises to 10 plots in a 2×5 grid.
        self._plot_history: dict[str, deque] = {
            k: deque(maxlen=240) for k in (
                "population", "treasury", "food", "wood", "iron", "happiness",
                "fed_fraction", "food_net", "employed_ratio", "stone_blocks",
                # v0.26: economic stats so 'g' shows the missing
                # money flow. Income / expenses are per-tick deltas
                # the economy already tracks; net is computed at
                # sample time. tax_rate is per-tick (cycled with T).
                "income", "expenses", "net", "tax_pct",
                # v0.54: four leading-indicator metrics, each backed by
                # an existing live value. housing_headroom (capacity −
                # population) predicts a migration stall before pop
                # plateaus; rebel_pressure (×100 / threshold) is the
                # single best "unrest about to erupt" signal; wage_ratio
                # (paid/owed ×100) drops before happiness collapses;
                # jobless (population − employed) is the labour slack the
                # J panel shows as a number but never as a trend.
                "housing_headroom", "rebel_pressure", "wage_ratio", "jobless",
            )
        }
        # v0.6: filter overlay. 0 = none; otherwise an index into
        # OVERLAY_NAMES below. Cycled with `O`.
        self.overlay_idx: int = 0
        # v0.6: graph window toggle.
        self.show_graphs: bool = False
        # v0.8: statistics panel toggle ('S' key).
        self.show_stats: bool = False
        # v0.17: job statistics panel ('J' key). Read-only summary of
        # employment: pool, demand, jobless, per-role distribution,
        # per-building shortfall list. Built from the same
        # building_status snapshot the inspector reads, so it's always
        # one tick fresh.
        self.show_jobs: bool = False

        # v0.19: tick-by-tick diagnostic recorder. Off until the
        # player presses Ctrl+R. Writes JSONL to ./data/recordings/.
        self.recorder = TickRecorder()
        # v0.55: player-action recorder. Toggled by the SAME Ctrl+R so a
        # session captures BOTH the per-tick world state (recorder) and
        # the player's decisions (action_recorder), joinable on `tick`.
        # This is the data imitation-learning needs; see
        # AUDIT_AND_TRAINING_GUIDE.md §3.4.
        self.action_recorder = ActionRecorder()
        # v0.19: bartering menu modal state. ``show_barter`` toggles
        # the modal (open via 'b' key). The two side-of-the-trade
        # selections plus the give-quantity input are scratch the
        # menu reads. Hit-test rects are rebuilt each draw, the same
        # pattern the buildings editor uses.
        self.show_barter: bool = False
        self.barter_give_id: str = "wheat"
        self.barter_recv_id: str = "bread"
        self.barter_give_qty: int = 10
        self._barter_btn_rects: list[tuple[float, float, float, float, str]] = []
        self.barter_history: list[dict] = []
        # v0.35: international gold-trade modal state. ``show_gold_trade``
        # toggles (open via 'c' key). The player picks BUY or SELL, a
        # resource, a quantity; the engine debits/credits treasury and
        # stock based on bartering.STOCK_PRICES. 100 dn flat fee.
        self.show_gold_trade: bool = False
        self.gold_trade_side: str = "buy"
        self.gold_trade_resource: str = "wheat"
        self.gold_trade_qty: int = 10
        self._gold_trade_btn_rects: list[
            tuple[float, float, float, float, str]
        ] = []
        self.gold_trade_history: list[dict] = []
        # v0.50: commercial-roads window state ('R' key). Lists the
        # seven foreign trade cities; the player links a city
        # (establishes a commercial road, one-off treasury cost) and
        # attaches persistent auto-trade routes that sell a stockpile
        # good each tick for gold via the shared TradeRouteManager.
        # ``commercial_roads`` (the manager) is created in
        # ``_ensure_commercial_roads`` right after each trade_manager
        # is built (setup / new-game / map-play / editor).
        self.show_commercial_roads: bool = False
        # Currently expanded city in the window (None = city list view).
        self.commercial_selected_city: str | None = None
        # Good + rate the "add route" sub-row is staged with.
        self.commercial_route_good: str = "wheat"
        self.commercial_route_rate: int = 10
        self._commercial_btn_rects: list[
            tuple[float, float, float, float, str]
        ] = []
        self._commercial_scroll: int = 0
        # v0.35: 'N' opens the nutrient-coverage informational panel.
        # Lists every nutrient with: produced this tick, eaten this
        # tick, stock, and coverage % (eaten/population). Non-modal,
        # same pattern as the happiness debug overlay.
        self.show_nutrients_panel: bool = False
        # v0.51: finance budget panel ('$'). A read-only modal that
        # summarises every income source, every upkeep / expense line,
        # and the resulting net balance per tick + current treasury.
        # Same non-modal overlay pattern as the nutrients panel.
        self.show_finance_panel: bool = False
        # v0.52: commerce-ships panel ('!') — a read-only summary of the
        # city's commercial fleet: how many ships are busy on voyages
        # (at sea, with cargo + destination) vs how many berths are free
        # to dispatch. Same non-modal overlay tier as the finance panel.
        self.show_commerce_ships_panel: bool = False
        # v0.19: load-map picker on the SPLASH screen. Same modal as
        # the editor's load picker, but invokable before any game
        # has started — the brief asks for a "Play custom map"
        # button next to "New game".
        self.show_splash_load_map: bool = False
        self._splash_load_map_rects: list[tuple[float, float, float, float, str]] = []
        # v0.36: "Load scenario" picker — lists subdirectories under
        # ``data/scenarios/`` that contain a ``map.json``. Same modal
        # pattern as the load-map picker but enumerates scripted
        # scenarios rather than bare maps. ``data/scenarios/`` is
        # the v0.36 home for self-contained scenarios (map + libraries
        # + assets); see ``mapfile.list_scenarios``.
        self.show_splash_load_scenario: bool = False
        self._splash_load_scenario_rects: list[tuple[float, float, float, float, str]] = []
        # v0.36: "Event editor" splash modal — informational stub.
        # The full graph-view UI is a follow-up; until it lands this
        # modal documents the wiring schema (the `triggers` block on
        # map.json) and tells authors how to hand-edit it. Distinct
        # from the Trigger editor, which owns the *what events do*
        # library (data/events.json); the Event editor owns the
        # *when things fire, and what happens next* wiring graph.
        self.show_splash_event_editor: bool = False
        # v0.19: read-only unit (walker) editor. Hardcoded walker
        # defs aren't mod-friendly yet; this surfaces what the engine
        # knows so the player can see the four roles + sprite
        # filenames. Closes on Esc / click outside.
        self.show_unit_editor: bool = False
        # v0.21: Z toggles a happiness-equation debug overlay. Reads
        # economy.last_happiness_breakdown — the per-term dict the
        # economy stashes each tick (base, tax_pen, food_bon, etc.).
        # Updates live while open so the player can pause, change a
        # tax rate, watch the equation re-balance.
        self.show_happiness_debug: bool = False
        # v0.44: P toggles the ship route debug overlay — draws each
        # ship's cached BFS/A* sea path as a polyline so you can see
        # where vessels are headed and confirm pathfinding routes around
        # coastlines. Off by default.
        self.show_ship_paths: bool = False
        # v0.48: military-unit command mode (toggle key 'U'). When on,
        # left-click/drag selects player units and right-click issues a
        # move/attack order; build/demolish are suspended so the two
        # interaction models don't fight. The CommandManager owns the
        # selection + control groups (the model lives in commands.py).
        from commands import CommandManager
        self.command_mode: bool = False
        self.command_manager = CommandManager()
        # Drag-box state: screen-space anchor while the left button is
        # held in command mode (None when not dragging).
        self._cmd_drag_start: tuple[float, float] | None = None
        self._cmd_drag_now: tuple[float, float] | None = None
        # v0.22: D toggles the production-diagnostics modal. Reads
        # diagnostics.ProductionDiagnostics — pure-logic engine that
        # walks each tracked chain and reports the *first* broken
        # stage with a fix suggestion. The panel renders one row per
        # broken chain; Esc / click closes.
        self.show_diagnostics: bool = False
        # v0.22: scroll offset for the diagnostics panel — the chain
        # set is bounded (12 chains) but the per-stage rows can
        # overflow on a small window. Reset to 0 each open.
        self.diag_scroll: int = 0
        # v0.23: scroll offsets for the J and S panels. The jobs
        # panel can list every worker-using building (200+ entries
        # late game); the stats panel renders the resource flow,
        # workforce, and per-building rows in a single column. Both
        # consume PgUp / PgDn while open. Reset to 0 each open so
        # the player always lands at the top.
        self.jobs_scroll: int = 0
        self.stats_scroll: int = 0

        # ── Cameras ──────────────────────────────────────────────────────
        self.world_camera = arcade.Camera2D()
        map_cx = GRID_COLS * TILE_SIZE / 2
        map_cy = GRID_ROWS * TILE_SIZE / 2
        self.world_camera.position = (map_cx, map_cy)
        self.gui_camera = arcade.Camera2D()

        # ── Input tracking ───────────────────────────────────────────────
        self.keys_held: set[int] = set()
        self.hover_row: int | None = None
        self.hover_col: int | None = None
        self.mouse_x: int = 0
        self.mouse_y: int = 0

        # ── Notifications ─────────────────────────────────────────────────
        self.notifications: list[tuple] = []

        # v0.21: middle-click extractable bubble. None when not
        # showing; otherwise a tuple (origin_row, origin_col, summary,
        # expires_at_game_time) where ``summary`` is the dict returned
        # by GameMap.extractable_summary. Drawn over the world near the
        # building footprint, expires after ~5 seconds.
        self.extract_bubble: tuple[int, int, dict, float] | None = None

        # ── HUD text pool ─────────────────────────────────────────────────
        self._create_gui_texts()

        # ── Starter city + first trade route ──────────────────────────────
        self._place_starter_city()
        tr = TRADE_ROUTES[0]
        self.trade_manager.add_route(
            TradeRoute(tr["name"], tr["goods"], tr["rate"], tr["profit"])
        )

        log.info("Setup complete. Map %dx%d, tile %dpx", GRID_COLS, GRID_ROWS, TILE_SIZE)

    def reset_transient_state(self) -> None:
        """Called by saveload after a successful load."""
        self.tick_accumulator = 0.0
        self.notifications.clear()
        self.paused = False
        self.show_help = False
        self.show_menu = False
        self.hover_row = None
        self.hover_col = None
        self.inspected = None
        self._warehouse_btn_rects = []
        # Force road & service maps to rebuild from the loaded grid.
        self.road_network.rebuild()
        # v0.23: re-wire the staffing gate after a load. The
        # ServiceMap from the loaded game is the same object, but
        # we need to make sure the staffing closure references the
        # *current* economy.building_status (saveload swapped the
        # economy out from under us — a stale closure would silently
        # gate against an old dict).
        self._wire_service_staffing_gate()
        self.service_map.rebuild()

    def _ensure_unit_registry(self):
        """v0.23.x: return the loaded UnitRegistry, lazy-loading on first
        access. Centralising the lookup means every WalkerManager reset
        path (new game, load game, map editor, scenario switch) gets the
        same registry instance without each path having to remember to
        construct it.

        The test fixtures construct CaesarGameWindow via ``__new__`` +
        manual attribute setting and never call ``__init__``; for those
        callers ``self.unit_registry`` simply doesn't exist until this
        helper is invoked. The first call from any reset path lazily
        builds one from ``data/units.json`` (or the built-in fallback),
        stores it on ``self``, and returns it.
        """
        reg = getattr(self, "unit_registry", None)
        if reg is None:
            reg = UnitRegistry.from_json_file(DATA_DIR / "units.json")
            self.unit_registry = reg
        return reg

    def _wire_service_staffing_gate(self) -> None:
        """v0.23: register the closure ServiceMap consults to decide
        whether a service-providing building is actually staffed.

        Background: until v0.22, an empty engineer post still gave
        full maintenance coverage — decay didn't tick on the
        surrounding city. The fix is one tile-level check inside
        ``ServiceMap.rebuild()`` that asks "does this provider have
        any workers?" and skips it if not. The lookup needs the
        economy's per-building status dict, which doesn't exist at
        ServiceMap construction time, so we wire it in afterwards.

        Returns ``(workers_filled, workers_needed)`` for the
        building at ``(row, col)``, or ``None`` if the economy
        hasn't computed a status row yet (cold start, the very
        first frame). ``None`` falls through to the legacy
        always-active path so the first-frame service map is
        identical to the v0.22 behaviour.
        """
        def lookup(row: int, col: int):
            status_dict = getattr(self.economy, "building_status", None)
            if not status_dict:
                return None
            row_status = status_dict.get((row, col))
            if row_status is None:
                return None
            # building_status entries are dataclasses; mock or test
            # fixtures may pass plain dicts. Handle both shapes.
            if hasattr(row_status, "workers_filled"):
                return (
                    int(getattr(row_status, "workers_filled", 0)),
                    int(getattr(row_status, "workers_needed", 0)),
                )
            return (
                int(row_status.get("workers_filled", 0)),
                int(row_status.get("workers_needed", 0)),
            )
        self.service_map.set_staffing_lookup(lookup)

    # ------------------------------------------------------------------
    def _current_palette(self) -> list[str]:
        if not self.palette_by_cat:
            return []
        return self.palette_by_cat[self.active_cat_idx][2]

    def _create_gui_texts(self) -> None:
        """Pre-create arcade.Text objects for the HUD so we never call draw_text."""
        # Top bar
        self.txt_calendar = arcade.Text("", 10, self.height - 26, COLOR_GOLD, 13, bold=True)
        # v0.21: tick counter — shown to the right of the date so the
        # player can see exactly how many simulation ticks have run.
        # Useful when reading recorder.py JSONL dumps (ticks line up
        # with the on-screen counter) and for tuning balance numbers
        # that talk about "per-tick" rates.
        self.txt_tick = arcade.Text(
            "", 0, self.height - 26, COLOR_GRAY, 11,
        )
        # v0.17: bumped 7 → 9 generic cells. The original 7 were
        # Pop / Treasury / Food / Wood / Iron / Happy / Speed; v0.17
        # split Food into Nutrients + Diversity (two separate readouts
        # because nutrient diversity is the headline gameplay metric).
        self.txt_topbar_items: list[arcade.Text] = [
            arcade.Text("", 0, self.height - 26, COLOR_WHITE, 11) for _ in range(9)
        ]

        # Right panel
        self.txt_rpanel_title = arcade.Text("ECONOMY", 0, 0, COLOR_GOLD, 11, bold=True)
        # v0.17: bumped 20 → 40 to fit the per-nutrient satiety block
        # (10 nutrient lines + section headers + legacy food + goods
        # block) added in this release. Empty slots are cheap (each is
        # an arcade.Text with text="" and a single draw call).
        self.txt_rpanel_lines = [arcade.Text("", 0, 0, COLOR_WHITE, 8) for _ in range(40)]
        self.txt_rpanel_trade_title = arcade.Text("TRADE ROUTES", 0, 0, COLOR_GOLD, 10, bold=True)
        self.txt_rpanel_trade_lines = [arcade.Text("", 0, 0, COLOR_GRAY, 8) for _ in range(6)]
        self.txt_rpanel_event_title = arcade.Text("EVENTS", 0, 0, COLOR_GOLD, 10, bold=True)
        self.txt_rpanel_event_lines = [arcade.Text("", 0, 0, COLOR_GRAY, 8) for _ in range(5)]

        # Tab labels + palette buttons (re-built on every category switch).
        self.txt_tabs = [
            arcade.Text(label, 0, 0, COLOR_WHITE, 9, bold=True)
            for _, label, _ in self.palette_by_cat
        ]
        # Reusable button label pool (3 lines × 24 buttons max).
        # v0.19.x: bumped 14 → 24 — agriculture split + new buildings
        # (fishery, beekeeper, cheese_maker, slaughterhouse, etc.)
        # pushed industry past 14 entries. Buttons that don't fit on
        # screen are reachable via scroll arrows; the pool just needs
        # to be big enough to back the full per-tab inventory.
        self.txt_bbar_labels: list[tuple[arcade.Text, arcade.Text, arcade.Text]] = []
        for _ in range(24):
            name_t = arcade.Text("", 0, 0, COLOR_WHITE, 8, bold=True)
            cost_t = arcade.Text("", 0, 0, COLOR_GOLD, 7)
            key_t = arcade.Text("", 0, 0, COLOR_GRAY, 7)
            self.txt_bbar_labels.append((name_t, cost_t, key_t))

        # Tooltip — palette tooltip + v0.14 hover tooltip share this
        # arcade.Text pool (we never draw both at the same time).
        self.txt_tooltip_lines = [
            arcade.Text("", 0, 0, COLOR_WHITE, 9) for _ in range(8)
        ]

        # Building info popup
        self.txt_info_title = arcade.Text("", 0, 0, COLOR_GOLD, 11, bold=True)
        # v0.23.x: pool sized to 20 (was 16) to fit the new
        # "Lifetime made / used" rows.
        self.txt_info_lines = [
            arcade.Text("", 0, 0, COLOR_WHITE, 9) for _ in range(20)
        ]

        # Notifications
        self.txt_notifications = [
            arcade.Text("", 0, 0, COLOR_WHITE, 12, anchor_x="center", bold=True)
            for _ in range(5)
        ]

        # Pause overlay
        self.txt_pause = arcade.Text(
            "PAUSED", self.width / 2, self.height / 2, COLOR_GOLD, 36,
            anchor_x="center", anchor_y="center", bold=True,
        )
        self.txt_pause_sub = arcade.Text(
            "Press SPACE to resume", self.width / 2, self.height / 2 - 40,
            COLOR_GRAY, 16, anchor_x="center", anchor_y="center",
        )

        # Help overlay
        self.txt_help_title = arcade.Text(
            "HELP", self.width / 2, self.height - 60, COLOR_GOLD, 24,
            anchor_x="center", bold=True,
        )
        self.txt_help_body = arcade.Text(
            HELP_TEXT, self.width / 2 - 200, self.height / 2 + 120,
            COLOR_WHITE, 12, multiline=True, width=400,
        )

        # Menu overlay
        self.txt_menu_title = arcade.Text(
            "MENU", self.width / 2, 0, COLOR_GOLD, 22,
            anchor_x="center", bold=True,
        )
        # Buttons drawn dynamically; the labels come from a list:
        # v0.15: "Buildings editor" added — a simple modal that lets
        # the player tweak cost / workers / housing / storage on
        # each building and save back to data/buildings.json. Sits
        # in the in-game menu (not on the splash) because editing
        # mid-game is the more common need.
        # v0.19: ESC menu now matches the splash menu (parity request)
        # — Map editor and Unit editor were previously splash-only,
        # which left mid-game players with no way to reach them
        # without quitting. Both work the same way as the splash
        # actions: they switch app_state and tear down transient
        # state. The "Resume" / "Save" / "Load" / "Quit" entries
        # remain in-game-only (they have no meaning on the splash).
        self._menu_actions = [
            ("Resume", self._menu_resume),
            ("Quick-Save (F5)", self._menu_save),
            ("Quick-Load (F9)", self._menu_load),
            ("Map editor", self._menu_open_map_editor),
            ("Buildings editor", self._menu_open_editor),
            ("Unit editor", self._menu_open_unit_editor),
            ("Bartering (B)", self._menu_open_barter),
            ("Toggle Help (H)", self._menu_help),
            ("Quit", self._menu_quit),
        ]
        self.txt_menu_buttons = [
            arcade.Text(label, 0, 0, COLOR_WHITE, 12, anchor_x="center", bold=True)
            for label, _ in self._menu_actions
        ]

        # ── Splash / main-menu overlay (v0.7) ────────────────────────────
        # v0.14: stub buttons (Start campaign / Load campaign / Map editor /
        # Units and Buildings editor / MOD creator) used to live here as
        # `_notify("coming soon")` placeholders. They were dead-end UI —
        # clicking them did nothing useful and made the menu look bigger
        # than it really is. Removed until the corresponding features
        # actually ship; until then the splash advertises only what works.
        # v0.15: "New game (Gallic War)" added — a working scenario
        # button. Picks the alternate starter layout (`_gallic_starter_layout`)
        # and otherwise behaves like the default New game.
        # v0.16: "Map editor" added — opens a free-placement scenario
        # authoring mode with Save/Load/Rename buttons in a top
        # toolbar. Maps round-trip through ``./data/maps/*.json``.
        self._splash_actions: list[tuple[str, callable]] = [
            ("New game",                 self._splash_new_game),
            ("New game (Gallic War)",    self._splash_new_game_gallic),
            # v0.30: Skirmish — combat-only sandbox seeded with one of
            # each military building (each pre-staffed with 10 ready-
            # to-fight units) and a scripted barbarian-wave schedule.
            # Reuses the v0.6 raid spawner (force_invasion_wave) for
            # the actual enemy units.
            ("Skirmish",                 self._splash_skirmish),
            ("Play custom map",          self._splash_play_custom_map),
            # v0.36: Load scenario — picker over scripted scenarios
            # in ``data/scenarios/``. Sits next to "Play custom map"
            # because both are "load a world from disk" entry points;
            # the difference is what's bundled with the world. A
            # custom map is just terrain + buildings; a scenario adds
            # cutscenes, RPG requests, scenario-local events, and the
            # ``triggers`` wiring that fires them.
            ("Load scenario",            self._splash_load_scenario),
            ("Load game",                self._splash_load_game),
            ("Map editor",               self._splash_open_map_editor),
            ("Buildings editor",         self._splash_open_buildings_editor),
            ("Unit editor",              self._splash_open_unit_editor),
            # v0.28: Event editor — author-side editor for
            # `data/events.json` (the global event library). Per-map
            # scheduled events live in the map editor's toolbar.
            # NOTE on responsibility split (v0.37): the Event editor
            # owns *what events do* — the effects, banner text, timed
            # modifiers each event applies when fired. It does NOT
            # decide *when* events fire or what cascades from them;
            # that's the Triggers editor (below), which authors the
            # `triggers` block on map.json.
            #
            # Historical: this menu entry was labelled "Trigger editor"
            # through v0.36, with "Event editor" being a (read-only)
            # info splash about the wiring schema. v0.37 swapped the
            # labels to match what each editor actually does — the
            # underlying state names (`trigger_editor_events`,
            # `_trigger_editor_save_to_disk`) are kept as-is so saves
            # and tests still round-trip.
            ("Event editor",             self._splash_open_trigger_editor),
            # v0.36: Triggers editor — wiring graph over the four
            # library editors. Authors the `triggers` block on
            # map.json: the edges that say "when X happens, do Y"
            # (play a cutscene, fire an event, push an RPG request,
            # schedule a future event, set a flag). The runtime
            # consumer (TriggerManager) shipped in v0.36; v0.37 ships
            # the editor UI to author the JSON without hand-editing.
            ("Triggers editor",          self._splash_open_event_editor),
            # v0.31: RPG request editor — author-side editor for
            # `data/rpg_requests.json` (NPC dialogues with multi-
            # choice decisions). Save format integrates with the
            # trigger editor via a planned `fire_request` effect
            # (TBD v0.32) so a scheduled event can spawn an RPG
            # request at runtime.
            ("RPG request editor",       self._splash_open_rpg_request_editor),
            # v0.32: Cut scene cinematic editor — author-side editor
            # for `data/cutscenes.json` (scripted slide decks shown
            # between gameplay beats, advanced by Next/OK). Sits next
            # to the RPG request editor in the splash menu because
            # the two are conceptually neighbours: NPC dialogues vs.
            # passive cinematics.
            ("Cut scene cinematic editor", self._splash_open_cutscene_editor),
            # v0.52: "Mapping keys keyboards" — a read-only overlay
            # listing every key binding (the in-game HELP_TEXT) plus the
            # on-disk folders where the game saves/loads its config,
            # saves, and maps. Lets a player learn the controls and find
            # their files before ever starting a game.
            ("Mapping keys keyboards",   self._splash_keymap),
            ("Credits",                  self._splash_credits),
            ("Exit",                     self._splash_exit),
        ]
        self.txt_splash_title = arcade.Text(
            "CAESAR III CLONE", self.width / 2, self.height - 60,
            COLOR_GOLD, 32, anchor_x="center", bold=True,
        )
        self.txt_splash_subtitle = arcade.Text(
            "City Builder Economy Simulation",
            self.width / 2, self.height - 96,
            COLOR_WHITE, 14, anchor_x="center", italic=True,
        )
        # v0.37: read the displayed version string from the project's
        # single source of truth (``version.py``) instead of hard-coding
        # the literal. Pre-v0.37 the splash showed "v0.19" forever
        # because every release forgot to bump this line; centralising
        # the constant means one edit per release covers every
        # surface (splash overlay, save-file stamp, future About panel).
        from version import VERSION as _PROJECT_VERSION
        self.txt_splash_version = arcade.Text(
            _PROJECT_VERSION, 12, 12, COLOR_GRAY, 10,
        )
        self.txt_splash_buttons = [
            arcade.Text(label, 0, 0, COLOR_WHITE, 13, anchor_x="center", bold=True)
            for label, _ in self._splash_actions
        ]
        # Credits screen text (shown when "Credits" pressed; ESC returns).
        # v0.14: text moved to `credits.py` so contributors can update
        # attributions without touching the rendering code.
        self.txt_credits_body = arcade.Text(
            CREDITS_TEXT,
            self.width / 2, self.height / 2,
            COLOR_WHITE, 13, anchor_x="center", anchor_y="center",
            multiline=True, width=self.width - 200, align="center",
        )

    def _preload_textures(self) -> None:
        """Eagerly load every texture we'll plausibly need.

        Done at setup so the first time a building is placed we don't get
        a one-frame stutter from a synchronous PNG decode. Misses are
        cached just like hits — there's no penalty for listing items that
        the user hasn't shipped a PNG for yet.
        """
        items: list[tuple[str, str]] = [
            ("terrain", "grass"),
            ("terrain", "grass_alt"),
            ("terrain", "water"),
        ]
        # All buildings (including 'empty' is harmless — it just won't be
        # found unless the user adds an empty.png).
        for bid in self.registry.all_ids():
            items.append(("buildings", bid))
        # House tiers 0..4 cover the full evolution range.
        for tier in range(5):
            items.append(("houses", f"tier{tier}"))
        # Walker roles seen in the codebase.
        for role in ("worker", "trader", "citizen"):
            items.append(("walkers", role))
        # v0.19.x: priced-resource icons for delivery-walker carry
        # overlays and warehouse/granary content icons. The full set
        # is generated by tools/generate_resource_textures.py from
        # bartering.STOCK_PRICES.
        from bartering import known_resources
        for rid in known_resources():
            items.append(("resources", rid))

        self.textures.preload(items)
        attempts, hits = self.textures.stats()
        log.info("Texture preload: %d/%d files found", hits, attempts)

    # ══════════════════════════════════════════════════════════════════════
    #  UPDATE
    # ══════════════════════════════════════════════════════════════════════
    def on_update(self, delta_time: float) -> None:
        # While the splash screen is up, freeze everything except notifications
        # decay (there shouldn't be any, but cheap to keep consistent).
        if self.app_state == "splash":
            self.notifications = [n for n in self.notifications if n[2] > self.game_time]
            return

        # ── Camera pan from held keys (works while paused too) ────────────
        dx, dy = 0.0, 0.0
        if arcade.key.LEFT in self.keys_held or arcade.key.A in self.keys_held:
            dx -= CAMERA_PAN_SPEED * delta_time
        if arcade.key.RIGHT in self.keys_held or arcade.key.D in self.keys_held:
            dx += CAMERA_PAN_SPEED * delta_time
        if arcade.key.DOWN in self.keys_held or arcade.key.S in self.keys_held:
            dy -= CAMERA_PAN_SPEED * delta_time
        if arcade.key.UP in self.keys_held or arcade.key.W in self.keys_held:
            dy += CAMERA_PAN_SPEED * delta_time
        if dx or dy:
            cx, cy = self.world_camera.position
            self.world_camera.position = (cx + dx, cy + dy)

        # ── Notifications expire on game_time ──────────────────────────────
        with self.profiler.section("notifications"):
            self.notifications = [n for n in self.notifications if n[2] > self.game_time]
            # v0.21: extract bubble expires the same way.
            if (
                self.extract_bubble is not None
                and self.extract_bubble[3] <= self.game_time
            ):
                self.extract_bubble = None

        # v0.16: editor mode freezes the simulation. The world is
        # being authored — running the economy would burn through
        # nutrient stocks while the player is laying out the map,
        # then they'd save a "fresh" map with stocks already eaten.
        # Camera pan / keys / mouse continue to work above this gate.
        if self.app_state == "editor":
            return

        # v0.38 (audit 7.1): present any queued RPG request. Done
        # before the pause gate so a request fired by a trigger opens
        # even while the player is otherwise paused; presenting it then
        # pauses the sim until a decision is made.
        self._drain_pending_rpg_requests()

        if self.paused or self.show_menu:
            return

        # ── Economy + per-tick simulation ─────────────────────────────────
        self.tick_accumulator += delta_time * self.speed_multiplier
        tick_period = 1.0 / TICKS_PER_SECOND
        while self.tick_accumulator >= tick_period:
            self.tick_accumulator -= tick_period
            with self.profiler.section("game_tick"):
                self._game_tick()
            # v0.56: the walker manager's heavy logic — spawning,
            # dispatch, raids, combat, fire, ships, reaping — is
            # per-TICK work and now runs inside the tick loop, not once
            # per frame. Previously it ran every frame, so its cadence
            # (and CPU cost) silently tracked the display's frame rate
            # and ignored speed_multiplier. Movement is handled
            # separately below for visual smoothness.
            with self.profiler.section("walkers.update"):
                self.walker_manager.update(
                    self.game_map, self.economy, move=False,
                )
            self.house_evolution.record_deliveries(self.game_time)

        # ── Walker movement: per-FRAME for smooth motion ──────────────────
        # v0.56: only positions advance every frame. ``dt_scale`` is the
        # fraction of a game tick elapsed this frame (frame seconds ×
        # game speed ÷ tick period), so a walker crosses a tile in the
        # same number of *ticks* at 30, 60, or 144 fps, and game speed
        # scales motion proportionally. Record deliveries every frame so
        # the supply memory captures all of a walker's pass-bys, not just
        # the ones that land on a tick boundary.
        with self.profiler.section("walkers.movement"):
            dt_scale = (delta_time * self.speed_multiplier) / tick_period
            self.walker_manager.update_movement(self.game_map, dt_scale)

    # ── v0.14: feature depletion tap ──────────────────────────────────────
    def _feature_tap(
        self, building_id: str, row: int, col: int, amount: float,
    ) -> float:
        """Bridge between the economy's `feature_tap` callback and
        `GameMap.tap_feature_in_footprint`. The economy doesn't know
        about footprints; the map does. We resolve the building
        instance from the registry, then ask the map to drain.

        Returns the amount actually extracted (0 if every gated tile
        in the footprint is depleted, partial if mid-tick exhaustion).

        v0.25: routes three classes of extractor:

          * ``needs_feature`` (mines, lumber mill, quarry) — drain
            the gating feature in the footprint. Unchanged.
          * ``feature_yield_bonus`` (farms on fertile_soil) — drain
            ONLY the bonus feature. A farm without any bonus tile
            returns ``amount`` unchanged (plain-grass farming is
            still free).
          * ``needs_terrain='water'`` (fishery) — drain the ``fish``
            feature in the footprint *or* its Chebyshev-1 neighbours
            (the fishery sits on land touching water, so fish are on
            the neighbour side).

        Buildings outside these three classes return ``amount``
        unchanged — the economy treats the requested amount as fully
        produced.
        """
        bd = self.registry.get(building_id)
        if bd is None:
            return amount  # Unknown building → don't gate.
        if bd.needs_feature:
            # Original v0.14 path — drain the gating feature(s).
            return self.game_map.tap_feature_in_footprint(
                row, col, bd, amount,
                wanted_features=set(bd.needs_feature),
            )
        if bd.feature_yield_bonus:
            # Farms etc. — drain ONLY the bonus feature. If no tile
            # under the footprint carries it, the helper returns 0.0;
            # but the farm should still produce its base yield on
            # plain grass, so we substitute `amount` for the no-bonus
            # case.
            wanted = set(bd.feature_yield_bonus.keys())
            # Pre-check: is any bonus tile present? If not, this is
            # plain-grass farming and no tap is needed.
            has_bonus = any(
                self.game_map.feature_at(r, c) in wanted
                for r, c in self.game_map._iter_footprint(row, col, bd)
            )
            if not has_bonus:
                return amount
            return self.game_map.tap_feature_in_footprint(
                row, col, bd, amount,
                wanted_features=wanted,
            )
        if bd.needs_terrain == "water":
            # Fishery — drain `fish` feature in footprint + neighbours.
            # No fish in catchment means no extraction (the building
            # produces nothing this tick).
            return self.game_map.tap_feature_in_footprint(
                row, col, bd, amount,
                wanted_features={"fish"},
                include_neighbours=True,
            )
        return amount

    def _game_tick(self) -> None:
        self.game_time += 1
        if self.game_time % 10 == 0:
            self.month += 1
            if self.month > 12:
                self.month = 1
                self.year += 1

        # v0.38 (audit 7.1): apply any RPG decision effects that were
        # deferred via a decision's delay_ticks ("pay next month").
        self._process_rpg_delayed_effects()

        # v0.25: advance every in-flight construction by one tick.
        # Buildings that finish this tick are reported back so we can
        # notify the player; the simulation below then includes them
        # in `positioned` as fully operational.
        completed = self.game_map.advance_construction()
        for bid, r, c in completed:
            bd = self.registry.get(bid)
            name = bd.name if bd is not None else bid
            self._notify(
                f"{name} built at ({r},{c})", COLOR_GREEN,
            )

        # Build (id, row, col) tuples and pass connectivity + tier lookups.
        # v0.25: under-construction buildings are excluded from every
        # downstream system — they don't produce, don't consume, don't
        # employ workers, don't provide services, don't gate house
        # evolution. This is the canonical "is this building live"
        # filter; every other system reads `positioned` and inherits
        # the exclusion.
        all_positioned = self.game_map.get_building_positions()
        positioned = [
            (bt, r, c) for (bt, r, c) in all_positioned
            if not self.game_map.is_under_construction(r, c)
        ]

        # v0.14: drought is a timed event whose `modifiers` field carries
        # `water_factor: 0.4` (or similar) for its duration. We push that
        # into the ServiceMap so its `coverage()` queries return the
        # dimmed value uniformly — farms see lower water_factor for
        # output gating, houses see lower water service for happiness,
        # the inspector and hover tooltip read the dimmed value too.
        # When the event expires, `current_modifiers()` returns no
        # `water_factor` entry; we set the modifier back to 1.0
        # (which `set_service_modifier` removes from the dict).
        active_modifiers = self.event_manager.current_modifiers()
        self.service_map.set_service_modifier(
            "water", active_modifiers.get("water_factor", 1.0),
        )
        # v0.28: economy-side modifiers. Blizzard's
        # `wood_consumption_factor: 2.0` lands here. We pass the whole
        # dict — _calc_production reads per-resource keys by name.
        # The blizzard also wires `pop_kill_when_resource_zero` as a
        # parallel list of (resource, pop/tick); other events can do
        # the same by adding a `pop_kill_when_resource_zero` entry
        # in their modifiers block (a list of [res, n] pairs).
        self.economy.economic_modifiers = dict(active_modifiers)
        kill_spec = active_modifiers.get("pop_kill_when_resource_zero")
        if kill_spec:
            # Accept both [["wood", 1]] and a single ["wood", 1]
            # for back-compat / convenience.
            if (
                isinstance(kill_spec, (list, tuple))
                and kill_spec
                and not isinstance(kill_spec[0], (list, tuple))
            ):
                kill_spec = [kill_spec]
            self.economy.pop_kill_when_resource_zero = [
                (str(res), int(n)) for res, n in kill_spec
            ]
        else:
            self.economy.pop_kill_when_resource_zero = []
        service_lookup = self.service_map.coverage

        self.economy.update(
            positioned,
            connectivity=self.road_network.is_connected,
            tier_lookup=lambda r, c: get_house_tier(self.game_map, r, c),
            service_lookup=service_lookup,
            feature_lookup=self.game_map.feature_at,
            feature_tap=self._feature_tap,
        )
        # Push into trend history after the tick.
        for r, hist in self._res_history.items():
            if r == "money":
                hist.append(self.economy.treasury)
            else:
                hist.append(self.economy.resources.get(r, 0))
        # v0.6: also push into the longer plot history.
        self._record_plot_sample()

        # v0.9: re-allocate the global pool across warehouses for this
        # tick. Read-only from the economy's POV; the warehouse
        # inspector reads `self.storage.contents(...)` to show real
        # per-warehouse stocks.
        self.storage.reset_overflow()
        self.storage.distribute(self.economy.resources)

        # House evolution (works off the freshly-rebuilt service map).
        changes = self.house_evolution.check_all(self.game_time)
        for orow, ocol, old, new in changes:
            self.game_map._invalidate_label(orow, ocol)  # force redraw
            arrow = "↑" if new > old else "↓"
            self._notify(
                f"House {arrow} tier {new}",
                COLOR_GREEN if new > old else COLOR_RED,
            )

        self.trade_manager.update()
        # v0.51: per-TRIP inter-city commerce. The voyage manager
        # dispatches free commercial ships (loading goods from the
        # commercial-port buffer) and completes returning trips, crediting
        # gold once per round trip. It's gated on ship availability, port
        # stock, destination war state, and sea piracy via the
        # _voyage_world adapter. Completed trips surface as notifications.
        voyage_gold_this_tick = 0.0
        vm = getattr(self, "voyage_manager", None)
        if vm is not None:
            completed = vm.update(self._voyage_world())
            for rec in completed:
                from commercial_roads import city_name
                voyage_gold_this_tick += float(rec.get("gold", 0.0))
                self._notify(
                    f"Trade ship home: {rec['qty']} {rec['good']} → "
                    f"{city_name(rec['city'])} (+{rec['gold']} dn)",
                    COLOR_GREEN,
                )
        # v0.51: fold the two out-of-economy income streams into the
        # finance breakdown so the '$' budget panel can attribute them.
        # Trade routes credit per tick (steady); voyages credit lumpily
        # on arrival (0 on most ticks, a spike on a return). Both also
        # already hit the treasury directly inside their managers — this
        # only records the *attribution*, it does not double-credit.
        fb = getattr(self.economy, "finance_breakdown", None)
        if fb is not None:
            fb["trade_routes"] = float(
                getattr(self.trade_manager, "last_tick_profit", 0.0)
            )
            fb["voyages"] = voyage_gold_this_tick
        # v0.28: pass self so the scheduler can read year / month /
        # game_time / population / treasury for trigger conditions.
        self.event_manager.update(self.economy, game=self)
        # v0.36: trigger wiring — runs AFTER event_manager so any
        # events fired this tick are already queued in
        # trigger_manager._pending_events (via the event_triggered
        # signal) and pickable up by on_event_fired triggers.
        self.trigger_manager.update(self.economy, game=self)
        signals.emit("tick", self.game_time)

        ev = self.event_manager.get_current()
        if ev and self.event_manager.display_timer == self.event_manager.DISPLAY_TICKS - 1:
            self.notifications.append(
                (ev["msg"], ev.get("color", COLOR_WHITE), self.game_time + 40)
            )

        # v0.6: surface combat events. The walker manager records every
        # damaging hit; we both log them and bubble a notification on
        # the first one of the tick so the player is never surprised by
        # quietly losing a soldier.
        combat = self.walker_manager.combat_events()
        if combat:
            log.info("Combat tick: %d hits", len(combat))
            for atk, defn, dmg, r, c in combat:
                log.debug("  %s → %s (%d dmg) at (%d,%d)", atk, defn, dmg, r, c)
            atk0, defn0, dmg0, r0, c0 = combat[0]
            if defn0 == "soldier":
                self._notify(
                    f"Soldier hit at ({r0},{c0}) -{dmg0} HP", COLOR_RED,
                )
            elif defn0 == "citizen":
                # A civilian death is louder than a soldier hit — the
                # player loses a unit of population and there's no HP
                # bar to read, so we lead with the location.
                self._notify(
                    f"Citizen killed at ({r0},{c0})!", COLOR_RED,
                )
            elif defn0 == "building":
                # Per-tick burn ticks would spam the feed; we only
                # surface the first one of the tick (which is what
                # the [0]-indexed pick does naturally) and let the
                # destroyed event below handle the loss.
                self._notify(
                    f"Building under attack at ({r0},{c0})", COLOR_GOLD,
                )
            elif defn0 == "building_destroyed":
                self._notify(
                    f"Building destroyed at ({r0},{c0})!", COLOR_RED,
                )
            else:
                self._notify(
                    f"Enemy hit at ({r0},{c0}) -{dmg0} HP", COLOR_GREEN,
                )
            # Even if the first event was a citizen / soldier hit,
            # scan the rest for a destruction so we never silently
            # lose a building.
            for atk, defn, dmg, r, c in combat[1:]:
                if defn == "building_destroyed":
                    self._notify(
                        f"Building destroyed at ({r},{c})!", COLOR_RED,
                    )
                    break

        # ── v0.8: diplomacy, Caesar, rebellion ───────────────────────────
        # Diplomacy: each placed senate yields a small per-tick contribution.
        senate_count = sum(
            1 for bt, _, _ in self.game_map.get_building_positions() if bt == "senate"
        )
        self.diplomacy.tick(senate_count)

        # Caesar: schedule + fire + deadline-check. A new request returns
        # a dict; show a banner once.
        new_req = self.caesar.update(
            self.month + 12 * (self.year - 1),  # absolute "month" since founding
            self.game_time,
            self.economy,
            self.walker_manager,
        )
        if new_req is not None:
            kind = new_req["kind"]
            amt = new_req["amount"]
            self._notify(
                f"CAESAR DEMANDS: {amt} {kind}!  Press Y to comply",
                COLOR_RED,
            )
        # Apply diplomacy penalty for any newly-failed requests we haven't
        # processed yet. The history grows append-only; we walk only the
        # tail past _caesar_history_seen.
        new_history = self.caesar.history[self._caesar_history_seen:]
        for entry in new_history:
            if not entry["honoured"]:
                self.diplomacy.add(self.balance.diplomacy_per_caesar_failed)
                self._notify("Caesar is displeased.", COLOR_RED)
        self._caesar_history_seen = len(self.caesar.history)

        # Rebellion: pressure ramps with starvation; spawns from house tiles.
        rebel_count = sum(
            1 for w in self.walker_manager.walkers
            if w.role == "enemy"   # Enemy walkers (raids + rebels) share the role
        )
        spawn_n = self.rebellion.update(
            fed_fraction=self.economy.fed_fraction,
            current_rebel_count=rebel_count,
        )
        if spawn_n > 0:
            actually = self.walker_manager.spawn_rebels_from_houses(
                self.game_map, spawn_n,
            )
            if actually > 0:
                self._notify(
                    f"REBELLION! {actually} citizen(s) take up arms",
                    COLOR_RED,
                )

        # ── v0.9: building decay ─────────────────────────────────────────
        # Engineer post coverage prevents decay; uncovered buildings
        # gradually lose condition. Collapse → small refund + notification.
        # We refund money outside the manager (manager is pure-logic, no
        # economy ref); the total cost lookup goes through the registry.
        collapses = self.decay.tick(self.game_time)
        for bid, r, c in collapses:
            bd = self.registry.get(bid)
            if bd is not None:
                refund = int(bd.cost * self.balance.decay_collapse_refund)
                self.economy.treasury += refund
                self._notify(
                    f"{bd.name} collapsed at ({r},{c}) — refund {refund} gold",
                    COLOR_RED,
                )

        # v0.19: tick-by-tick diagnostic record. Recorder is a no-op
        # when off; see recorder.py for the JSONL schema.
        self.recorder.record_tick(self)

        # v0.30: skirmish wave scheduler. Cheap no-op for non-skirmish
        # scenarios — runs last so any side-effects (notifications,
        # enemy spawns) land after the normal simulation pass.
        if getattr(self, "scenario", None) == "skirmish":
            self._skirmish_tick()

    # ══════════════════════════════════════════════════════════════════════
    #  DRAW
    # ══════════════════════════════════════════════════════════════════════
    def _visible_tile_bounds(self) -> tuple[int, int, int, int] | None:
        """v0.56: inclusive ``(min_row, max_row, min_col, max_col)`` tile
        window currently on screen, for culling the map's colour/glyph
        fallback pass. Returns ``None`` (draw everything) if the camera
        geometry can't be read — safer to over-draw than to clip the map.

        The textured SpriteList path ignores this (the GPU batches and
        culls it); only the immediate-mode fallback, used when terrain
        textures are missing, benefits — but that's exactly the case
        where per-tile draw calls hurt, so it's worth computing.
        """
        cam = getattr(self, "world_camera", None)
        if cam is None:
            return None
        try:
            cx, cy = cam.position
            zoom = getattr(cam, "zoom", 1.0) or 1.0
            half_w = (self.width / zoom) / 2.0
            half_h = (self.height / zoom) / 2.0
            pad = 1  # one-tile margin so edge tiles never pop in late
            min_c = int((cx - half_w) // TILE_SIZE) - pad
            max_c = int((cx + half_w) // TILE_SIZE) + pad
            min_r = int((cy - half_h) // TILE_SIZE) - pad
            max_r = int((cy + half_h) // TILE_SIZE) + pad
            min_r = max(0, min_r)
            min_c = max(0, min_c)
            max_r = min(self.game_map.rows - 1, max_r)
            max_c = min(self.game_map.cols - 1, max_c)
            if min_r > max_r or min_c > max_c:
                # Camera entirely off the map — nothing to draw.
                return (0, -1, 0, -1)
            return (min_r, max_r, min_c, max_c)
        except Exception:
            return None

    def on_draw(self) -> None:
        self.clear()

        # Splash screen short-circuits the whole world+HUD render.
        if self.app_state == "splash":
            self.gui_camera.use()
            self._draw_splash()
            # v0.19: Play-custom-map picker overlays on top of splash.
            if self.show_splash_load_map:
                self._draw_splash_load_map()
            # v0.36: Load-scenario picker — same idea, scripted bundles.
            if self.show_splash_load_scenario:
                self._draw_splash_load_scenario()
            return

        # World layer
        self.world_camera.use()
        with self.profiler.section("map.draw"):
            self.game_map.draw(
                self.hover_row, self.hover_col, self.selected_building,
                visible_bounds=self._visible_tile_bounds(),
            )
        with self.profiler.section("walkers.draw"):
            self.walker_manager.draw()
        # v0.44: ship route debug overlay (toggle key 'P'). Drawn in
        # world space so it pans/zooms with the map, right after the
        # walkers so the lines sit under buildings' chrome but over the
        # sea — enough to trace where each ship is headed.
        if getattr(self, "show_ship_paths", False):
            self._draw_ship_paths()
        # v0.48: command-mode selection highlights + drag-box (world space).
        if getattr(self, "command_mode", False):
            self._draw_command_overlay()
        self._draw_road_indicators()
        # v0.19.x: stocked-resource icons on warehouses & granaries.
        # Drawn over buildings (so they sit on top of the texture)
        # but under overlays / influence circles (which are city-wide
        # diagnostic layers, not per-building chrome).
        self._draw_storage_icons()
        # v0.6: tinted overlay layer on top of buildings.
        if self.overlay_idx > 0:
            with self.profiler.section("overlay.draw"):
                self._draw_overlay()
        # v0.8: influence circle around the inspected building (drawn in
        # world space so it pans/zooms with the camera).
        if self.inspected is not None:
            self._draw_influence_circle()
        # v0.21: extractable-resource bubble drawn in world space so
        # it pins to the building footprint as the player pans/zooms.
        if self.extract_bubble is not None:
            self._draw_extract_bubble()

        # GUI layer
        self.gui_camera.use()
        with self.profiler.section("hud.draw"):
            self._draw_top_bar()
            self._draw_right_panel()
            self._draw_bottom_bar()
            self._draw_notifications()
        if self.show_minimap:
            with self.profiler.section("minimap.draw"):
                self._draw_minimap()
        if self.inspected is not None:
            self._draw_info_panel()
        if self.palette_hover is not None:
            self._draw_palette_tooltip()
        else:
            # v0.14: world hover tooltip — only when palette tooltip
            # isn't already eating the player's attention. The method
            # itself short-circuits if there's nothing under the
            # cursor or another panel is open.
            self._draw_hover_tooltip()
        # v0.6: graphs window — drawn last so it sits on top of the HUD.
        if self.show_graphs:
            self._draw_graphs()
        # v0.8: stats panel — same layering as graphs.
        if self.show_stats:
            self._draw_stats()
        # v0.17: jobs panel — same layering as graphs/stats.
        if self.show_jobs:
            self._draw_jobs_panel()
        # v0.6: overlay legend (top-right, just below the top bar) when
        # an overlay is active.
        if self.overlay_idx > 0:
            self._draw_overlay_legend()

        if self.paused and not self.show_menu:
            arcade.draw_lrbt_rectangle_filled(0, self.width, 0, self.height, (0, 0, 0, 120))
            self.txt_pause.draw()
            self.txt_pause_sub.draw()
            # v0.17: redraw the hover tooltip ON TOP of the pause dim
            # so the player can still inspect walkers / buildings /
            # terrain while paused (the spacebar-pause-and-look-around
            # workflow). The first _draw_hover_tooltip() call earlier
            # got covered by the dim rectangle; this second pass lands
            # above it. The tooltip's own backdrop is fully opaque
            # (alpha 230) so it reads cleanly over the dim.
            if self.palette_hover is None:
                self._draw_hover_tooltip()

        if self.show_help:
            arcade.draw_lrbt_rectangle_filled(
                self.width / 2 - 220, self.width / 2 + 220,
                self.height / 2 - 200, self.height / 2 + 200,
                (20, 15, 10, 230),
            )
            arcade.draw_lrbt_rectangle_outline(
                self.width / 2 - 220, self.width / 2 + 220,
                self.height / 2 - 200, self.height / 2 + 200,
                COLOR_GOLD, 2,
            )
            self.txt_help_title.draw()
            self.txt_help_body.draw()

        if self.show_menu:
            self._draw_menu()

        # v0.15: buildings editor — drawn last so it sits over everything.
        if self.editor_open:
            self._draw_editor()

        # v0.28: trigger editor — drawn after buildings editor so it
        # sits on top when both somehow ever opened together (in
        # practice only one is open at a time — `_open_trigger_editor`
        # is splash-entry, not in-game).
        if self.trigger_editor_open:
            self._draw_trigger_editor()

        # v0.31: RPG request editor — drawn after the trigger editor
        # for the same reason. The editor module owns its own draw
        # routines; we just dispatch when the state's `.open` flag
        # is set.
        if self.rpg_request_editor_state is not None \
                and self.rpg_request_editor_state.open:
            import rpg_request_editor
            rpg_request_editor.draw(self, self.rpg_request_editor_state)

        # v0.32: Cutscene editor — same dispatch shape as the RPG
        # editor above. Owns its own draw path; we just check the
        # flag.
        if self.cutscene_editor_state is not None \
                and self.cutscene_editor_state.open:
            import cutscene_editor
            cutscene_editor.draw(self, self.cutscene_editor_state)

        # v0.37: Triggers editor — same dispatch shape as the RPG /
        # cutscene editors above. Originally nested under the
        # ``app_state == "splash"`` branch, but the splash-entry
        # handler switches state to "playing" (so the editor has a
        # backing empty world to mutate, matching its siblings),
        # which meant the draw never ran. Moved here so the editor
        # surfaces regardless of the post-open app_state.
        if self.triggers_editor_state is not None \
                and self.triggers_editor_state.open:
            import triggers_editor
            triggers_editor.draw(self, self.triggers_editor_state)

        # v0.32: Cutscene runtime player — full-screen takeover when
        # a cutscene is active. Drawn after the editors so that even
        # if (somehow) both were active the cutscene wins; in
        # practice they're mutually exclusive (the editor pauses,
        # the runtime fires on flag flips during play).
        if self.cutscene_player_state is not None \
                and self.cutscene_player_state.active:
            import cutscene_player
            cutscene_player.draw(self.cutscene_player_state, self)

        # v0.38 (audit 7.1): RPG request decision panel — full-screen
        # takeover, same precedence tier as the cutscene player. They're
        # mutually exclusive in practice (a request pauses the sim; a
        # cutscene fired by its set_flag queues but won't draw until the
        # request closes).
        if self.rpg_player_state is not None \
                and self.rpg_player_state.active:
            import rpg_player
            rpg_player.draw(self.rpg_player_state, self)

        # v0.19: bartering modal sits over everything except the menu.
        if self.show_barter:
            self._draw_barter()

        # v0.35: gold-trade modal — same precedence as bartering.
        if self.show_gold_trade:
            self._draw_gold_trade()

        # v0.50: commercial-roads window — same precedence tier.
        if getattr(self, "show_commercial_roads", False):
            self._draw_commercial_roads()

        # v0.19: unit editor — read-only viewer.
        if self.show_unit_editor:
            self._draw_unit_editor()

        # v0.21: 'Z' happiness equation debug overlay. Drawn after
        # the unit editor so it sits on top, but doesn't block
        # gameplay (read-only, no input capture other than the Z
        # toggle and Esc close).
        if self.show_happiness_debug:
            self._draw_happiness_debug()

        # v0.35: nutrients informational panel — same non-modal pattern
        # as the happiness debug overlay.
        if getattr(self, "show_nutrients_panel", False):
            self._draw_nutrients_panel()

        # v0.51: finance budget panel ('$') — read-only income / upkeep /
        # balance summary. Same non-modal overlay tier as nutrients.
        if getattr(self, "show_finance_panel", False):
            self._draw_finance_panel()

        # v0.52: commerce-ships panel ('!') — busy (at-sea) commercial
        # voyages vs free commercial-harbour berths. Same overlay tier.
        if getattr(self, "show_commerce_ships_panel", False):
            self._draw_commerce_ships_panel()

        # v0.22: 'D' production-diagnostics modal. Drawn after every
        # other panel so it floats on top — closing pops back to whatever
        # was showing before (typically the world).
        if self.show_diagnostics:
            self._draw_diagnostics_panel()

        # v0.16: map editor toolbar + dialogs. Drawn after the world
        # but before any modal so the toolbar stays visible (it sits
        # at the top of the screen, between the resource bar and the
        # world). The dialogs (load picker, rename) are themselves
        # modals, drawn last so they sit over everything.
        if self.app_state == "editor":
            self._draw_editor_toolbar()
            if self.editor_show_load_picker:
                self._draw_editor_load_picker()
            if self.editor_show_rename_dialog:
                self._draw_editor_rename_dialog()
            # v0.28: scheduled-events side panel. Drawn last so its
            # dropdowns sit over the toolbar's other dialogs.
            if self.show_scheduled_panel:
                self._draw_scheduled_panel()
            # v0.50: starting-budget form. Drawn last of the editor
            # modals so it sits on top of everything else in the editor.
            if getattr(self, "editor_show_initvals", False):
                self._draw_editor_initvals()

        # Post-v0.28: profiler HUD. Drawn dead last so it overlays
        # every panel — the player wanted to see frame cost, not
        # squint past the help text. Anchored to the right edge so
        # it doesn't fight the minimap or the help modal.
        if self.show_profiler:
            self.profiler.draw_hud(self.width - 300, self.height - 60)
        # Always roll the frame's samples into the rolling window,
        # even if the HUD is hidden — that way toggling on shows
        # immediate data instead of a one-second blank.
        self.profiler.end_frame()

    def _draw_command_overlay(self) -> None:
        """v0.48: in command mode, ring each selected unit and (while
        dragging) draw the selection box. World space, so it tracks the
        camera. Also rings a selected unit's active move/attack target
        tile so the player sees where an order is headed."""
        from commands import Verb
        sel = self.command_manager.selection
        for u in sel.units:
            x, y = self.game_map.grid_to_world_center(u.row, u.col)
            arcade.draw_circle_outline(x, y, 9, (90, 230, 120), 2)
            # If the unit has a move/attack order, ring its destination.
            cs = getattr(u, "command", None)
            if cs is not None and cs.order is not None and cs.order.target_tile:
                tr, tc = cs.order.target_tile
                tx, ty = self.game_map.grid_to_world_center(tr, tc)
                col = ((230, 90, 90) if cs.order.verb == Verb.ATTACK
                       else (90, 230, 120))
                arcade.draw_circle_outline(tx, ty, 7, col, 1)
                arcade.draw_line(x, y, tx, ty, (*col, ), 1)
        # Drag-box (world-space rectangle from anchor to current).
        if self._cmd_drag_start is not None and self._cmd_drag_now is not None:
            sx, sy = self._cmd_drag_start
            ex, ey = self._cmd_drag_now
            if abs(ex - sx) + abs(ey - sy) > 4:
                lo_x, hi_x = min(sx, ex), max(sx, ex)
                lo_y, hi_y = min(sy, ey), max(sy, ey)
                arcade.draw_lrbt_rectangle_outline(
                    lo_x, hi_x, lo_y, hi_y, (90, 230, 120), 1.5,
                )

    def _draw_ship_paths(self) -> None:
        """v0.44: debug overlay — draw each ship's cached sea path as a
        world-space polyline, with a dot on the next step and a ring on
        the goal. Reads ``Ship._sea_path`` (populated by the pathfinder);
        ships with no active path (idle/greedy-fallback) are skipped.
        Cheap and read-only; gated on ``show_ship_paths`` (key P)."""
        from walkers import Ship
        for w in self.walker_manager.walkers:
            if not isinstance(w, Ship):
                continue
            path = getattr(w, "_sea_path", None)
            if not path or len(path) < 2:
                continue
            # Path polyline.
            for (r1, c1), (r2, c2) in zip(path, path[1:]):
                x1, y1 = self.game_map.grid_to_world_center(r1, c1)
                x2, y2 = self.game_map.grid_to_world_center(r2, c2)
                arcade.draw_line(x1, y1, x2, y2, (60, 200, 255), 1.5)
            # Goal ring.
            gr, gc = path[-1]
            gx, gy = self.game_map.grid_to_world_center(gr, gc)
            arcade.draw_circle_outline(gx, gy, 6, (60, 200, 255), 2)
            # Next-step dot (where the ship is heading right now).
            try:
                here_idx = path.index((w.row, w.col))
                if here_idx + 1 < len(path):
                    nr, nc = path[here_idx + 1]
                    nx, ny = self.game_map.grid_to_world_center(nr, nc)
                    arcade.draw_circle_filled(nx, ny, 3, (255, 240, 120))
            except ValueError:
                pass

    def _draw_road_indicators(self) -> None:
        """Tiny red 'X' on production buildings that aren't road-connected."""
        for bt, orow, ocol in self.game_map.get_building_positions():
            bd = self.registry.get(bt)
            if bd is None or not bd.requires_road:
                continue
            if self.road_network.is_connected(bt, orow, ocol):
                continue
            x, y = self.game_map.grid_to_world_center(orow, ocol)
            # Small red dot in upper-right of footprint.
            x += (bd.width - 1) * TILE_SIZE / 2 + TILE_SIZE / 3
            y += (bd.height - 1) * TILE_SIZE / 2 + TILE_SIZE / 3
            arcade.draw_circle_filled(x, y, 4, (255, 80, 80))
            arcade.draw_circle_outline(x, y, 4, (40, 0, 0), 1)

    # v0.19.x: per-warehouse / per-granary content icons. The roadmap
    # called this out explicitly: "the inspector lists them but the
    # world view is just a coloured square." We render a row of up to
    # ``STORAGE_ICON_MAX`` resource sprites along the bottom edge of
    # each storage building's footprint, sized to ~⅓ the tile so four
    # icons fit on a 2×1 warehouse and the building texture stays
    # readable above. Only buildings with ``storage > 0`` are
    # considered, so a non-storage building with a dummy entry in the
    # storage allocator (none today, but future-proof) doesn't get
    # icons. The icons show the dominant resources by quantity, so the
    # display reflects what's actually filling the building rather
    # than the categories the storage *could* hold.
    STORAGE_ICON_MAX = 4
    STORAGE_ICON_SIZE = 12

    def _draw_storage_icons(self) -> None:
        for bt, orow, ocol in self.game_map.get_building_positions():
            bd = self.registry.get(bt)
            if bd is None or bd.storage <= 0:
                continue
            stocks = self.storage.contents(orow, ocol)
            if not stocks:
                continue
            # Top-N by quantity, alphabetic tiebreak so the layout is
            # stable across frames (a granary holding equal qty bread
            # & vegetables shouldn't flicker which one shows first).
            top = sorted(
                stocks.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )[: self.STORAGE_ICON_MAX]
            x0, y0 = self.game_map.grid_to_world(orow, ocol)
            w_px = bd.width * TILE_SIZE
            # Lay icons out along the bottom strip of the footprint,
            # centred horizontally. 1px gap between icons.
            n = len(top)
            sz = self.STORAGE_ICON_SIZE
            row_w = n * sz + (n - 1)
            ix0 = x0 + (w_px - row_w) / 2
            iy = y0 + 2  # 2-px inset off the building's bottom edge
            # Translucent backing so icons read against busy textures.
            arcade.draw_lrbt_rectangle_filled(
                ix0 - 2, ix0 + row_w + 2,
                iy - 1, iy + sz + 1,
                (0, 0, 0, 130),
            )
            for i, (good, _qty) in enumerate(top):
                ix = ix0 + i * (sz + 1)
                tex = self.textures.resource(good)
                if tex is not None:
                    arcade.draw_texture_rect(
                        tex, arcade.LBWH(ix, iy, sz, sz),
                    )
                else:
                    # Hash-coloured fallback — same scheme as the
                    # delivery walker cargo overlay so a missing icon
                    # reads consistently across both surfaces.
                    h = abs(hash(good))
                    color = (
                        80 + (h & 0x7F),
                        80 + ((h >> 8) & 0x7F),
                        80 + ((h >> 16) & 0x7F),
                    )
                    arcade.draw_lrbt_rectangle_filled(
                        ix, ix + sz, iy, iy + sz, color,
                    )
                    arcade.draw_lrbt_rectangle_outline(
                        ix, ix + sz, iy, iy + sz, (30, 25, 20), 1,
                    )

    # ── v0.8: influence circle for inspected service-providing buildings ──
    # Per-service tint so the visual telegraphs *what* this circle means.
    # Keys match `Building.provides_service` strings — modders adding new
    # services should also add a colour here (defaults to gold otherwise).
    INFLUENCE_COLORS: dict[str, tuple[int, int, int]] = {
        "water":         (80, 160, 255),    # cool blue
        "food":          (255, 200, 80),    # wheat
        "religion":      (220, 120, 220),   # violet
        "entertainment": (255, 140, 80),    # warm orange
        "education":     (80, 200, 200),    # teal
        "health":        (255, 100, 100),   # medical red
        "safety":        (180, 180, 180),   # neutral grey
        "maintenance":   (160, 130, 90),    # ochre
        "defence":       (200, 80, 80),     # martial red
    }

    def _draw_influence_circle(self) -> None:
        """Draw a coloured ring around the inspected building showing the
        Chebyshev radius of its service. Skipped silently if the inspected
        building doesn't provide a service — clicking a house or a road
        leaves the world clean.

        Drawn from the world camera so it pans / zooms with the map.
        Implementation note: arcade has no Chebyshev-square primitive,
        so we trace the bounding square of the radius. That matches what
        ServiceMap actually computes (Chebyshev distance ≤ radius) — a
        round circle would *lie* about coverage on the corners.
        """
        if self.inspected is None:
            return
        orow, ocol = self.inspected
        cell = self.game_map.grid[orow][ocol]
        if cell is None:
            return
        bid = cell[0]
        bd = self.registry.get(bid)
        if bd is None or bd.provides_service is None or bd.service_radius <= 0:
            return

        color = self.INFLUENCE_COLORS.get(bd.provides_service, COLOR_GOLD)
        # Centre of the building footprint in world coords.
        cx, cy = self.game_map.grid_to_world_center(orow, ocol)
        cx += (bd.width - 1) * TILE_SIZE / 2
        cy += (bd.height - 1) * TILE_SIZE / 2

        # Bounding square covers tiles within Chebyshev distance R from
        # the *origin tile*. Half-side = (R + 0.5*footprint) tiles.
        half_w = (bd.service_radius + bd.width / 2) * TILE_SIZE
        half_h = (bd.service_radius + bd.height / 2) * TILE_SIZE
        l, r = cx - half_w, cx + half_w
        b, t = cy - half_h, cy + half_h

        # Filled translucent fill + crisp outline. The fill is intentionally
        # gentle so it doesn't drown the buildings under it.
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (*color, 35))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, color, 2)

    # ── v0.21: extractable-resource bubble (middle-click) ─────────────────
    def _draw_extract_bubble(self) -> None:
        """Draw the floating bubble for the most-recently middle-clicked
        extractor. Shows the human label (e.g. "Trees", "Iron ore",
        "Fertility", "Fish") and the remaining reserves number — or
        "inexhaustible" for fertile soil / groundwater / fisheries.

        Drawn in world space so it pins to the building footprint as
        the player pans/zooms. The bubble state (and its expiry time)
        is set by the on_mouse_press middle-click branch and cleared
        by on_update once game_time passes the expiry tick.
        """
        if self.extract_bubble is None:
            return
        orow, ocol, summary, _expires = self.extract_bubble
        # v0.23.x: the bubble now serves two cases — a building's
        # extractable summary AND a raw natural-resource tile (no
        # building). For the building case we anchor on the footprint
        # and clear the bubble when the building is demolished; for the
        # raw case we anchor on the single clicked tile and keep the
        # bubble alive even if the tile changes (the feature is the
        # subject, not a building).
        cell = self.game_map.grid[orow][ocol]
        if cell is not None:
            bid = cell[0]
            bd = self.registry.get(bid)
            if bd is None:
                self.extract_bubble = None
                return
            # Anchor: the centre of the building footprint, then offset
            # upward so the bubble doesn't cover the building art.
            x0, y0 = self.game_map.grid_to_world(orow, ocol)
            cx = x0 + bd.width * TILE_SIZE / 2
            top = y0 + bd.height * TILE_SIZE
        else:
            # Raw feature tile — anchor on the single tile itself.
            x0, y0 = self.game_map.grid_to_world(orow, ocol)
            cx = x0 + TILE_SIZE / 2
            top = y0 + TILE_SIZE
        # Build the two-line label: "Resource" / "value".
        label = summary["label"]
        reserves = summary["reserves"]
        # v0.23: discovery-time reserves so the bubble can render
        # "9000 of 85000 left". Older save data may not carry this
        # key; missing → fall back to the v0.21 single-number
        # display rather than crashing.
        initial = summary.get("initial")
        depleted = summary["depleted"]
        tiles = summary["tiles"]
        # v0.26: a "(1 tile)" suffix on the value line is useless
        # noise — the player can already see the building footprint
        # they middle-clicked. Only show the tile count when the
        # extractor straddles more than one feature tile, since
        # that's the case where the number is informative
        # (e.g. "lumber mill catches 3 forest tiles").
        tile_suffix = (
            f" ({tiles} tiles)" if tiles > 1 else ""
        )
        # v0.26: single-line bubble when there's no meaningful
        # numeric reading. Previously an inexhaustible single-tile
        # feature rendered "1 tile" as its value line — which the
        # player rightly called useless. Track this with a flag so
        # the bubble height collapses to fit just the label.
        value_text: str | None
        if depleted:
            value_text = "Depleted"
            value_color = COLOR_RED
        elif reserves is None:
            # Inexhaustible (stone deposit, groundwater).
            if tiles > 1:
                value_text = f"{tiles} tiles"
                value_color = COLOR_GREEN
            else:
                # One inexhaustible tile under the footprint —
                # show only the label, no useless "1 tile" line.
                value_text = None
                value_color = COLOR_GREEN
        else:
            # v0.23: when we know the discovery total, render
            # "9000 of 85000 left" so the player can see how much of
            # the original deposit has been mined out. We round to
            # the nearest integer because reserves are accounted in
            # whole-unit ticks; fractions are noise to the player.
            if initial is not None and initial > 0:
                value_text = (
                    f"{int(round(reserves))} of "
                    f"{int(round(initial))} left{tile_suffix}"
                )
            else:
                value_text = f"{reserves:.0f} left{tile_suffix}"
            if reserves < 100:
                value_color = COLOR_RED
            elif reserves < 300:
                value_color = COLOR_GOLD
            else:
                value_color = COLOR_WHITE

        # v0.26: bubble height is computed from what we're actually
        # going to draw. Three slots: label (always), value text
        # (optional), progress bar (optional). Spacing was tightened
        # so the bar sits clear of the value-text descenders — the
        # v0.23 numbers let them collide on some fonts (the bar.png
        # bug). Numbers are absolute pixel offsets from the top of
        # the bubble inward, so future tweaks only touch this block.
        bar_visible = (
            reserves is not None
            and initial is not None
            and initial > 0
            and not depleted
        )
        # Vertical budget (top→down inside the bubble):
        TOP_PAD = 6
        LABEL_H = 14
        GAP_LABEL_VALUE = 4
        VALUE_H = 14
        GAP_VALUE_BAR = 6
        BAR_H = 7
        BAR_BOTTOM_PAD = 6
        # Compose the height from whichever sections are present.
        bh = TOP_PAD + LABEL_H
        if value_text is not None:
            bh += GAP_LABEL_VALUE + VALUE_H
        if bar_visible:
            bh += GAP_VALUE_BAR + BAR_H + BAR_BOTTOM_PAD
        else:
            bh += BAR_BOTTOM_PAD  # bottom breathing room even without bar
        bw = 180.0  # v0.26: a touch wider so "of" doesn't crowd numerics
        bl = cx - bw / 2
        br = cx + bw / 2
        bb = top + 6
        bt = bb + bh
        # Backdrop + border.
        arcade.draw_lrbt_rectangle_filled(bl, br, bb, bt, (20, 16, 12, 235))
        arcade.draw_lrbt_rectangle_outline(bl, br, bb, bt, COLOR_GOLD, 1)

        # Lazy text pool — two arcade.Text objects shared across all
        # bubble draws.
        if not hasattr(self, "_txt_extract_bubble_label"):
            self._txt_extract_bubble_label = arcade.Text(
                "", 0, 0, COLOR_GOLD, 9, bold=True, anchor_x="center",
            )
            self._txt_extract_bubble_value = arcade.Text(
                "", 0, 0, COLOR_WHITE, 8, anchor_x="center",
            )
        # Label sits TOP_PAD + ~LABEL_H below the bubble top.
        self._txt_extract_bubble_label.text = label
        self._txt_extract_bubble_label.x = cx
        self._txt_extract_bubble_label.y = bt - (TOP_PAD + LABEL_H - 3)
        self._txt_extract_bubble_label.draw()
        if value_text is not None:
            self._txt_extract_bubble_value.text = value_text
            self._txt_extract_bubble_value.color = value_color
            self._txt_extract_bubble_value.x = cx
            self._txt_extract_bubble_value.y = (
                bt - (TOP_PAD + LABEL_H + GAP_LABEL_VALUE + VALUE_H - 3)
            )
            self._txt_extract_bubble_value.draw()

        # v0.23: progress-bar strip showing the fraction left. Bar
        # colour matches the value-text colour so the player gets a
        # consistent severity cue. v0.26: anchored to the bubble
        # bottom (BAR_BOTTOM_PAD up) so it can't overlap text above.
        if bar_visible:
            frac = max(0.0, min(1.0, reserves / initial))
            bar_pad = 8
            bar_l = bl + bar_pad
            bar_r = br - bar_pad
            bar_b = bb + BAR_BOTTOM_PAD
            bar_t = bar_b + BAR_H
            # Empty-track backdrop.
            arcade.draw_lrbt_rectangle_filled(
                bar_l, bar_r, bar_b, bar_t, (40, 30, 20),
            )
            # Filled portion.
            fill_r = bar_l + (bar_r - bar_l) * frac
            arcade.draw_lrbt_rectangle_filled(
                bar_l, fill_r, bar_b, bar_t, value_color,
            )
            arcade.draw_lrbt_rectangle_outline(
                bar_l, bar_r, bar_b, bar_t, (60, 50, 40), 1,
            )

    # ── v0.8: city statistics panel (S key) ──────────────────────────────
    def _draw_jobs_panel(self) -> None:
        """v0.17: Modal overlay listing employment statistics.

        Three sections:

          1. Headline numbers — population pool, total demand, total
             filled, jobless, total shortfall.
          2. Per-role table — worker / trader / soldier / citizen
             with (filled / needed) for each.
          3. Per-building list — every worker-using building, sorted
             with short-staffed ones first; columns: name, role,
             filled/needed, state, reason.

        v0.23: PgUp / PgDn scroll the per-building list. The
        headline strip and the per-role table stay fixed at the top
        — they're cheap context the player wants visible at all
        times. The scroll cursor only walks the building list (the
        only section that can be longer than the panel).

        Click anywhere or press J again to close.
        """
        from jobs import snapshot_jobs, WORKER_ROLES
        snap = snapshot_jobs(
            self.game_map, self.registry,
            self.economy.building_status, self.economy.population,
        )

        # Panel: ~70% × ~85% of the window, centred.
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

        # Lazy text pool.
        if not hasattr(self, "_txt_jobs_title"):
            self._txt_jobs_title = arcade.Text(
                "", 0, 0, COLOR_GOLD, 16, bold=True, anchor_x="center",
            )
            self._txt_jobs_subtitle = arcade.Text(
                "", 0, 0, COLOR_GRAY, 10, anchor_x="center",
            )
            self._txt_jobs_lines: list[arcade.Text] = []
        # Grow the line pool to fit. Each building takes one line; add
        # ~16 for headers and the headline block.
        needed_slots = len(snap.per_building) + 16
        while len(self._txt_jobs_lines) < needed_slots:
            self._txt_jobs_lines.append(
                arcade.Text("", 0, 0, COLOR_WHITE, 11)
            )

        # Title.
        self._txt_jobs_title.text = "JOBS & EMPLOYMENT"
        self._txt_jobs_title.x = cx
        self._txt_jobs_title.y = t - 28
        self._txt_jobs_title.draw()
        self._txt_jobs_subtitle.text = (
            "PgUp / PgDn to scroll  ·  press J or click anywhere to close  ·  "
            "press O for the jobless heatmap overlay"
        )
        self._txt_jobs_subtitle.x = cx
        self._txt_jobs_subtitle.y = t - 46
        self._txt_jobs_subtitle.draw()

        # Headline row 1.
        head_y = t - 80
        block_w = panel_w / 3
        head_lines = [
            (f"Pool: {snap.population}", COLOR_WHITE),
            (f"Demand: {snap.total_demand}", COLOR_WHITE),
            (
                f"Filled: {snap.total_filled}",
                COLOR_GREEN if snap.total_shortfall == 0 else COLOR_GOLD,
            ),
        ]
        for i, (text, color) in enumerate(head_lines):
            tx = self._txt_jobs_lines[i]
            tx.text = text
            tx.color = color
            tx.x = l + 16 + i * block_w
            tx.y = head_y
            tx.draw()

        # Headline row 2.
        head_y2 = head_y - 22
        utilisation = (
            int(100 * snap.total_filled / snap.total_demand)
            if snap.total_demand > 0 else 0
        )
        head_lines2 = [
            (
                f"Jobless: {snap.jobless}",
                COLOR_GOLD if snap.jobless > 0 else COLOR_GREEN,
            ),
            (
                f"Shortfall: {snap.total_shortfall}",
                COLOR_RED if snap.total_shortfall > 0 else COLOR_GREEN,
            ),
            (
                f"Utilisation: {utilisation}%",
                COLOR_GREEN if utilisation >= 90 else
                COLOR_GOLD if utilisation >= 60 else COLOR_RED,
            ),
        ]
        for i, (text, color) in enumerate(head_lines2):
            tx = self._txt_jobs_lines[3 + i]
            tx.text = text
            tx.color = color
            tx.x = l + 16 + i * block_w
            tx.y = head_y2
            tx.draw()

        # Per-role section.
        role_y = head_y2 - 36
        section = self._txt_jobs_lines[6]
        section.text = "── By role ──"
        section.color = COLOR_GOLD
        section.x = l + 16
        section.y = role_y
        section.draw()
        role_y -= 18
        for i, role in enumerate(WORKER_ROLES):
            filled, needed = snap.per_role.get(role, (0, 0))
            color = (
                COLOR_GREEN if filled == needed and needed > 0 else
                COLOR_GRAY if needed == 0 else
                COLOR_RED if filled == 0 else
                COLOR_GOLD
            )
            tx = self._txt_jobs_lines[7 + i]
            tx.text = f"  {role.capitalize()}: {filled} / {needed}"
            tx.color = color
            tx.x = l + 16
            tx.y = role_y - i * 16
            tx.draw()

        # Per-building section.
        build_y = role_y - len(WORKER_ROLES) * 16 - 24
        section2 = self._txt_jobs_lines[7 + len(WORKER_ROLES)]
        # v0.23: header now says "showing N..M of K" so the player
        # knows the panel really *does* have more rows below.
        per_b = snap.per_building
        row_h = 13
        # Reserve a footer line for "PgDn for more" when there's
        # overflow — keep the geometry simple by knocking 18px off
        # the available area.
        footer_h = 22
        max_rows = max(0, int((build_y - (b + 30 + footer_h)) / row_h))
        total = len(per_b)
        # Clamp scroll so PgDn past the end snaps back to the last
        # full window — same UX as the diagnostics panel.
        max_scroll = max(0, total - max_rows)
        self.jobs_scroll = max(0, min(self.jobs_scroll, max_scroll))
        start = self.jobs_scroll
        end = min(total, start + max_rows)
        section2.text = (
            f"── Buildings ({total} with worker slots — "
            f"showing {start + 1 if total else 0}..{end} of {total}) ──"
            if total > 0
            else "── Buildings (none with worker slots) ──"
        )
        section2.color = COLOR_GOLD
        section2.x = l + 16
        section2.y = build_y
        section2.draw()
        build_y -= 18

        rendered = 0
        idx_start = 8 + len(WORKER_ROLES)
        for entry in per_b[start:end]:
            tx = self._txt_jobs_lines[idx_start + rendered]
            wf = entry.workers_filled
            wn = entry.workers_needed
            short = wn - wf
            short_str = f"  ({short} short)" if short > 0 else ""
            line = (
                f"  {entry.name:<22s} "
                f"[{entry.role:<7s}] "
                f"{wf}/{wn}{short_str}"
            )
            if len(line) > 70:
                line = line[:67] + "..."
            tx.text = line
            if short > 0:
                tx.color = COLOR_RED if wf == 0 else COLOR_GOLD
            else:
                tx.color = COLOR_WHITE
            tx.x = l + 16
            tx.y = build_y - rendered * row_h
            tx.draw()
            rendered += 1

        # Footer scroll-hint. Drawn at a fixed bottom offset so the
        # visual position doesn't jiggle as the row count changes.
        if max_scroll > 0:
            tx = self._txt_jobs_lines[idx_start + rendered]
            if start < max_scroll:
                tx.text = (
                    f"   ({total - end} more below — PgDn to scroll, "
                    f"PgUp to go back)"
                )
            else:
                tx.text = "   (end of list — PgUp to scroll back)"
            tx.color = COLOR_GRAY
            tx.x = l + 16
            tx.y = b + 24
            tx.draw()

    def _draw_stats(self) -> None:
        """Modal overlay listing every building, every resource flow, and
        the workforce breakdown. Built on the pure-logic ``stats.py`` so
        it stays testable headlessly.

        The panel is wide and tall (most of the screen) because there's a
        lot to look at. Two columns split the lines roughly in half so
        the eye doesn't track a single 40-line column. Click anywhere or
        press S again to close.

        v0.23: PgUp / PgDn scroll the line buffer. We slice
        ``lines[stats_scroll : stats_scroll + window]`` then split the
        *visible* slice into two columns — that way the player can
        page through a large city's stats without losing the column
        layout.
        """
        snap = build_snapshot(
            self.game_map, self.registry, self.economy,
            walker_manager=self.walker_manager,
            decay_manager=self.decay,
        )
        lines = format_lines(snap, self.registry)

        # Lazy-allocate the Text pool. The line count is data-driven so
        # we grow on demand; arcade.Text construction is cheap relative
        # to a frame so this is fine.
        if not hasattr(self, "_txt_stats_lines") or len(self._txt_stats_lines) < len(lines):
            self._txt_stats_lines = [
                arcade.Text("", 0, 0, COLOR_WHITE, 10) for _ in range(max(80, len(lines)))
            ]
        if not hasattr(self, "_txt_stats_title"):
            self._txt_stats_title = arcade.Text(
                "", 0, 0, COLOR_GOLD, 16, bold=True, anchor_x="center",
            )
        if not hasattr(self, "_txt_stats_footer"):
            self._txt_stats_footer = arcade.Text(
                "", 0, 0, COLOR_GRAY, 10, anchor_x="center",
            )

        # Panel: ~85% × ~88% of the window, centred.
        panel_w = self.width * 0.86
        panel_h = self.height * 0.88
        cx = self.width / 2
        cy = self.height / 2
        l = cx - panel_w / 2
        r = cx + panel_w / 2
        b = cy - panel_h / 2
        t = cy + panel_h / 2
        arcade.draw_lrbt_rectangle_filled(0, self.width, 0, self.height, (0, 0, 0, 160))
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (30, 22, 18, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        self._txt_stats_title.text = "CITY STATISTICS  (S or click to close, PgUp/PgDn to scroll)"
        self._txt_stats_title.x = cx
        self._txt_stats_title.y = t - 22
        self._txt_stats_title.draw()

        # Two columns, top-aligned, with a vertical divider for legibility.
        col_gap = 24
        col_w = (panel_w - 32 - col_gap) / 2
        col1_x = l + 16
        col2_x = l + 16 + col_w + col_gap
        first_y = t - 50
        line_h = 14
        # Reserve a footer line for "showing N..M of K".
        footer_h = 22
        rows_per_col = max(0, int((panel_h - 80 - footer_h) / line_h))
        # Total visible rows in this panel = 2 columns × rows_per_col.
        window = rows_per_col * 2
        # v0.23: clamp the scroll cursor so PgDn past the end snaps
        # back to the last full window. Same UX as the diagnostics
        # and jobs panels.
        max_scroll = max(0, len(lines) - window)
        self.stats_scroll = max(0, min(self.stats_scroll, max_scroll))
        start = self.stats_scroll
        end = min(len(lines), start + window)
        visible = lines[start:end]
        # Split the *visible* slice. Find the empty-line index nearest
        # the midpoint of `visible` so the two columns break cleanly
        # between sections.
        mid = len(visible) // 2
        split = mid
        for off in range(0, max(mid, 1)):
            for cand in (mid - off, mid + off):
                if 0 <= cand < len(visible) and visible[cand] == "":
                    split = cand
                    break
            else:
                continue
            break

        for i, text in enumerate(visible[:split][:rows_per_col]):
            tt = self._txt_stats_lines[i]
            tt.text = text
            tt.color = COLOR_GOLD if (text and text == text.upper() and not text.startswith(" ")) else COLOR_WHITE
            tt.x = col1_x
            tt.y = first_y - i * line_h
            tt.draw()
        for j, text in enumerate(visible[split:][:rows_per_col]):
            tt = self._txt_stats_lines[len(visible[:split]) + j]
            tt.text = text
            tt.color = COLOR_GOLD if (text and text == text.upper() and not text.startswith(" ")) else COLOR_WHITE
            tt.x = col2_x
            tt.y = first_y - j * line_h
            tt.draw()

        # Footer scroll readout.
        if len(lines) > window:
            self._txt_stats_footer.text = (
                f"showing lines {start + 1}..{end} of {len(lines)}  ·  "
                + ("PgDn for more" if start < max_scroll else "end")
                + ("  ·  PgUp to scroll back" if start > 0 else "")
            )
        else:
            self._txt_stats_footer.text = f"{len(lines)} lines"
        self._txt_stats_footer.x = cx
        self._txt_stats_footer.y = b + 16
        self._txt_stats_footer.draw()


    def _trend_arrow(self, resource: str) -> tuple[str, tuple[int, int, int]]:
        hist = self._res_history.get(resource)
        if not hist or len(hist) < 2:
            return "", COLOR_GRAY
        delta = hist[-1] - hist[0]
        if delta > 0.5:
            return "↑", COLOR_GREEN
        if delta < -0.5:
            return "↓", COLOR_RED
        return "→", COLOR_GRAY

    def _draw_top_bar(self) -> None:
        y = self.height - TOP_BAR_H
        arcade.draw_lrbt_rectangle_filled(0, self.width, y, self.height, COLOR_UI_PANEL)
        arcade.draw_line(0, y, self.width, y, COLOR_UI_BORDER, 2)

        season = SEASON_BY_MONTH[self.month]
        self.txt_calendar.text = (
            f"Year {self.year}, {MONTH_NAMES[self.month - 1]} ({season})"
        )
        self.txt_calendar.draw()
        # v0.21: tick counter, sized just behind the date label. We
        # measure the calendar text width via a simple
        # character-count heuristic (arcade.Text.content_width is
        # available but only after first draw on some versions; the
        # heuristic keeps placement deterministic).
        cal_width = len(self.txt_calendar.text) * 8 + 12
        self.txt_tick.text = f"Tick {self.economy.tick_count}"
        self.txt_tick.x = 10 + cal_width
        self.txt_tick.y = self.height - 26
        self.txt_tick.draw()

        # Build the cell list with icon colours and trend arrows.
        # Treasury colour flips red if negative.
        treasury_color = COLOR_RED if self.economy.treasury < 0 else COLOR_GOLD
        # v0.17 fix: the top bar's "Food: 9065" was the legacy single-
        # resource readout that became misleading the moment v0.16 split
        # food into 10 nutrients (the screenshot showed "Food 9065"
        # while the player was actually starving — it was reading the
        # wheat-farm fallback `food` legacy). Now we show two top-bar
        # cells: total nutrient stock (sum of the ten nutrient
        # resources) and the nutrient diversity ratio. The food trend
        # arrow tracks the legacy resource since that's the one with
        # historical samples in `_res_history`.
        from constants import NUTRIENTS
        nutrient_total = sum(
            int(self.economy.resources.get(n, 0)) for n in NUTRIENTS
        )
        diversity = self.economy.nutrient_diversity
        diversity_max = self.economy.balance.nutrient_diversity_full_count
        # Diversity colour: green at full, gold partial, red at zero.
        if diversity >= diversity_max:
            div_color = COLOR_GREEN
        elif diversity >= diversity_max // 2:
            div_color = COLOR_GOLD
        else:
            div_color = COLOR_RED
        items: list[tuple[str, tuple[int, int, int], tuple[int, int, int] | None, str]] = [
            (f"Pop {self.economy.population}", COLOR_WHITE, None, ""),
            (f"{self.economy.treasury:.0f} gold",
             treasury_color, RESOURCE_COLORS["money"], self._trend_arrow("money")[0]),
            (f"Nutrients {nutrient_total}",
             COLOR_WHITE, RESOURCE_COLORS["food"], self._trend_arrow("food")[0]),
            (f"Diversity {diversity}/{diversity_max}",
             div_color, None, ""),
            (f"Wood {self.economy.resources.get('wood', 0):.0f}",
             COLOR_WHITE, RESOURCE_COLORS["wood"], self._trend_arrow("wood")[0]),
            (f"Iron {self.economy.resources.get('iron', 0):.0f}",
             COLOR_WHITE, RESOURCE_COLORS["iron"], self._trend_arrow("iron")[0]),
            (f"Happy {self.economy.happiness:.0f}%",
             COLOR_GREEN if self.economy.happiness > 50 else COLOR_RED,
             None, ""),
            (f"{self.speed_multiplier}x{' ⏸' if self.paused else ''}",
             COLOR_GRAY, None, ""),
        ]
        # v0.17: x-step reduced from 130 → 115 because we now have 8
        # cells (Pop / Treasury / Nutrients / Diversity / Wood / Iron /
        # Happy / Speed) — at 130 px each, the rightmost cell would
        # spill off the 1280-wide default window.
        # v0.21: bumped start from 240 → 310 so the tick counter
        # ("Tick NNNNN") fits between the date and the first cell
        # without overlapping. ~70 px is plenty for 5-digit tick
        # numbers at the 11-pt label size.
        x = 310
        for i, (text, color, icon_color, arrow) in enumerate(items):
            if i >= len(self.txt_topbar_items):
                break
            t = self.txt_topbar_items[i]
            display_text = f"{text}{(' ' + arrow) if arrow else ''}"
            t.text = display_text
            t.color = color
            # Icon swatch.
            if icon_color is not None:
                arcade.draw_lrbt_rectangle_filled(
                    x, x + 10, self.height - 27, self.height - 17, icon_color,
                )
                t.x = x + 14
            else:
                t.x = x
            t.y = self.height - 26
            t.draw()
            x += 115

        # v0.26: hovered tile (row, col) readout in the top bar.
        # Drawn below the date strip so it doesn't crowd the resource
        # cells. Empty when the mouse isn't over a tile (over UI
        # chrome or out-of-bounds), so the strip blanks out instead
        # of showing stale coordinates.
        if not hasattr(self, "_txt_hover_coord"):
            self._txt_hover_coord = arcade.Text(
                "", 0, 0, COLOR_GRAY, 9, bold=False,
            )
        if self.hover_row is not None and self.hover_col is not None:
            self._txt_hover_coord.text = (
                f"Tile ({self.hover_col}, {self.hover_row})"
            )
            self._txt_hover_coord.color = COLOR_GRAY
        else:
            self._txt_hover_coord.text = ""
        # Pin to just under the date (which sits at height-26).
        self._txt_hover_coord.x = 10
        self._txt_hover_coord.y = self.height - TOP_BAR_H + 4
        self._txt_hover_coord.draw()

    def _draw_right_panel(self) -> None:
        px = self.width - RIGHT_PANEL_W
        py = BOTTOM_BAR_H
        ph = self.height - TOP_BAR_H - BOTTOM_BAR_H
        arcade.draw_lrbt_rectangle_filled(px, self.width, py, py + ph, COLOR_UI_PANEL)
        arcade.draw_lrbt_rectangle_outline(px, self.width, py, py + ph, COLOR_UI_BORDER, 2)

        y = py + ph - 18
        self.txt_rpanel_title.x = px + 5
        self.txt_rpanel_title.y = y
        self.txt_rpanel_title.draw()
        y -= 16

        # v0.14: per-line tone-based colouring. The right panel was a
        # uniform white wall of text in the screenshots — at a glance
        # you couldn't tell food was draining or treasury was healthy.
        # The economy now annotates each line with a tone; we map
        # tones to existing palette colours.
        TONE_COLORS = {
            "good":    COLOR_GREEN,
            "bad":     COLOR_RED,
            "warn":    COLOR_GOLD,
            "section": COLOR_GOLD,
            "normal":  COLOR_WHITE,
        }
        for i, (line, tone) in enumerate(self.economy.get_status_lines_with_tones()):
            if i >= len(self.txt_rpanel_lines):
                break
            t = self.txt_rpanel_lines[i]
            t.text = line
            t.color = TONE_COLORS.get(tone, COLOR_WHITE)
            t.x = px + 5
            t.y = y
            t.draw()
            y -= 13

        y -= 8
        self.txt_rpanel_trade_title.x = px + 5
        self.txt_rpanel_trade_title.y = y
        self.txt_rpanel_trade_title.draw()
        y -= 14
        for i, line in enumerate(self.trade_manager.get_summary()):
            if i >= len(self.txt_rpanel_trade_lines):
                break
            t = self.txt_rpanel_trade_lines[i]
            t.text = line
            t.x = px + 5
            t.y = y
            t.draw()
            y -= 12

        # ── v0.8: diplomacy + Caesar block ───────────────────────────────
        # Slim and inline so it doesn't crowd the existing panel layout.
        y -= 8
        if not hasattr(self, "_txt_rpanel_dip"):
            self._txt_rpanel_dip = arcade.Text(
                "", 0, 0, COLOR_GOLD, 9, bold=True,
            )
            self._txt_rpanel_caesar = arcade.Text(
                "", 0, 0, COLOR_RED, 9,
            )
        self._txt_rpanel_dip.text = f"Diplomacy: {self.diplomacy.points:.0f}"
        self._txt_rpanel_dip.x = px + 5
        self._txt_rpanel_dip.y = y
        self._txt_rpanel_dip.draw()
        y -= 12
        if self.caesar.current is not None:
            req = self.caesar.current
            ticks_left = max(0, req["deadline_tick"] - self.game_time)
            self._txt_rpanel_caesar.text = (
                f"CAESAR: {req['amount']} {req['kind']}  ({ticks_left}t, Y)"
            )
            self._txt_rpanel_caesar.x = px + 5
            self._txt_rpanel_caesar.y = y
            self._txt_rpanel_caesar.draw()
            y -= 12

        y -= 8
        self.txt_rpanel_event_title.x = px + 5
        self.txt_rpanel_event_title.y = y
        self.txt_rpanel_event_title.draw()
        y -= 14
        for i, ev in enumerate(reversed(self.event_manager.history[-4:])):
            if i >= len(self.txt_rpanel_event_lines):
                break
            t = self.txt_rpanel_event_lines[i]
            t.text = f"- {ev['name']}"
            t.x = px + 5
            t.y = y
            t.draw()
            y -= 12

    def _draw_bottom_bar(self) -> None:
        arcade.draw_lrbt_rectangle_filled(0, self.width, 0, BOTTOM_BAR_H, COLOR_UI_PANEL)
        arcade.draw_line(0, BOTTOM_BAR_H, self.width, BOTTOM_BAR_H, COLOR_UI_BORDER, 2)

        # ── Tab strip (top of the bar) ───────────────────────────────────
        tab_y0 = BOTTOM_BAR_H - TAB_H
        tab_y1 = BOTTOM_BAR_H
        x = 8
        for i, (cat_id, cat_label, _) in enumerate(self.palette_by_cat):
            tw = max(60, len(cat_label) * 9 + 16)
            sel = (i == self.active_cat_idx)
            fill = (80, 70, 50) if sel else (45, 38, 32)
            arcade.draw_lrbt_rectangle_filled(x, x + tw, tab_y0, tab_y1, fill)
            arcade.draw_lrbt_rectangle_outline(
                x, x + tw, tab_y0, tab_y1,
                COLOR_GOLD if sel else (80, 70, 60), 1,
            )
            t = self.txt_tabs[i]
            t.x = x + tw / 2
            t.y = tab_y0 + 4
            t.anchor_x = "center"
            t.color = COLOR_WHITE if sel else COLOR_GRAY
            t.draw()
            # Stash the rect for hit-testing.
            self.palette_by_cat[i] = (cat_id, cat_label, self.palette_by_cat[i][2])
            x += tw + 4

        # ── Building palette buttons ─────────────────────────────────────
        # v0.19.x: draw ◀ / ▶ scroll arrows on either side of the
        # palette row when the current category overflows. The arrows
        # are 18 px wide and steal that much horizontal space from the
        # button row, but they only appear when there's something to
        # scroll — narrow tabs (housing, water, religion) still get
        # the full bar width.
        btn_w = 78
        btn_h = BOTTOM_BAR_H - TAB_H - 10
        btn_y = 5
        palette = self._current_palette()
        palette_count = len(palette)
        # Horizontal real estate available for buttons.
        bar_left = 8
        bar_right = self.width - 8
        # How many buttons fit at btn_w + 3px gap?
        page_w_full = bar_right - bar_left
        per_page = max(1, page_w_full // (btn_w + 3))
        overflow = palette_count > per_page
        arrow_w = 18 if overflow else 0
        if overflow:
            # Recompute per_page after stealing space for both arrows.
            page_w = page_w_full - 2 * (arrow_w + 4)
            per_page = max(1, page_w // (btn_w + 3))
        # Clamp scroll: never past the last full page.
        max_scroll = max(0, palette_count - per_page)
        if self.palette_scroll > max_scroll:
            self.palette_scroll = max_scroll
        if self.palette_scroll < 0:
            self.palette_scroll = 0
        self._palette_per_page = per_page  # used by hit-test
        # Stash hit rects for the scroll arrows so on_mouse_press
        # can dispatch them. None when arrows are hidden.
        self._palette_arrow_rects: list[tuple[float, float, float, float, str]] = []
        x = bar_left
        if overflow:
            # Left arrow.
            ar_l = x
            ar_r = x + arrow_w
            arcade.draw_lrbt_rectangle_filled(ar_l, ar_r, btn_y, btn_y + btn_h, (50, 42, 35))
            arcade.draw_lrbt_rectangle_outline(ar_l, ar_r, btn_y, btn_y + btn_h, COLOR_UI_BORDER, 1)
            if not hasattr(self, "_txt_palette_arrow_l"):
                self._txt_palette_arrow_l = arcade.Text(
                    "<", 0, 0, COLOR_GOLD, 12, bold=True, anchor_x="center",
                )
                self._txt_palette_arrow_r = arcade.Text(
                    ">", 0, 0, COLOR_GOLD, 12, bold=True, anchor_x="center",
                )
            self._txt_palette_arrow_l.x = (ar_l + ar_r) / 2
            self._txt_palette_arrow_l.y = btn_y + btn_h / 2 - 6
            self._txt_palette_arrow_l.color = COLOR_GOLD if self.palette_scroll > 0 else COLOR_GRAY
            self._txt_palette_arrow_l.draw()
            self._palette_arrow_rects.append((ar_l, ar_r, btn_y, btn_y + btn_h, "left"))
            x = ar_r + 4
        # Visible slice of the palette starting at palette_scroll.
        visible_slice = palette[self.palette_scroll : self.palette_scroll + per_page]
        for vi, bid in enumerate(visible_slice):
            i = self.palette_scroll + vi  # absolute index for hotkey logic
            if vi >= len(self.txt_bbar_labels):
                break
            bd = self.registry[bid]
            sel = (bid == self.selected_building)
            affordable = self.economy.can_afford(bd.cost)
            if sel:
                arcade.draw_lrbt_rectangle_filled(x, x + btn_w, btn_y, btn_y + btn_h, (80, 70, 50))
                arcade.draw_lrbt_rectangle_outline(x, x + btn_w, btn_y, btn_y + btn_h, COLOR_GOLD, 2)
            else:
                border = (80, 70, 60) if affordable else (140, 60, 60)
                arcade.draw_lrbt_rectangle_outline(x, x + btn_w, btn_y, btn_y + btn_h, border, 1)

            arcade.draw_lrbt_rectangle_filled(
                x + 3, x + 13, btn_y + btn_h - 13, btn_y + btn_h - 3, bd.color,
            )

            name_t, cost_t, key_t = self.txt_bbar_labels[vi]
            name_t.text = bd.name[:9]
            name_t.color = COLOR_WHITE if affordable else (160, 100, 100)
            name_t.x = x + 16
            name_t.y = btn_y + btn_h - 14
            name_t.draw()
            cost_t.text = f"{bd.cost} gold"
            cost_t.color = COLOR_GOLD if affordable else (160, 100, 100)
            cost_t.x = x + 16
            cost_t.y = btn_y + btn_h - 26
            cost_t.draw()
            # Hotkey only on first 10 *visible* buttons (1-9, 0).
            hotkey = str(vi + 1) if vi < 9 else ("0" if vi == 9 else "")
            if hotkey:
                key_t.text = hotkey
                key_t.x = x + btn_w - 12
                key_t.y = btn_y + 3
                key_t.draw()

            x += btn_w + 3
        if overflow:
            # Right arrow.
            ar_l = bar_right - arrow_w
            ar_r = bar_right
            arcade.draw_lrbt_rectangle_filled(ar_l, ar_r, btn_y, btn_y + btn_h, (50, 42, 35))
            arcade.draw_lrbt_rectangle_outline(ar_l, ar_r, btn_y, btn_y + btn_h, COLOR_UI_BORDER, 1)
            self._txt_palette_arrow_r.x = (ar_l + ar_r) / 2
            self._txt_palette_arrow_r.y = btn_y + btn_h / 2 - 6
            self._txt_palette_arrow_r.color = COLOR_GOLD if self.palette_scroll < max_scroll else COLOR_GRAY
            self._txt_palette_arrow_r.draw()
            self._palette_arrow_rects.append((ar_l, ar_r, btn_y, btn_y + btn_h, "right"))

    def _palette_button_at(self, x: int, y: int) -> int | None:
        """Return the *absolute* palette index (i.e. index into the
        full per-tab list, not the visible slice) under (x, y), or
        None if the click missed.

        v0.19.x: factors in the horizontal scroll offset so a click
        on the visually-first button after the player has paged right
        returns the correct entry."""
        if y > BOTTOM_BAR_H - TAB_H or y < 0:
            return None
        # Ignore arrow rects — they're handled separately upstream.
        for x1, x2, _y1, _y2, _action in getattr(self, "_palette_arrow_rects", []):
            if x1 <= x <= x2:
                return None
        bar_left = 8
        # If arrows are showing, buttons start after the left arrow.
        if getattr(self, "_palette_arrow_rects", []):
            bar_left = 8 + 18 + 4  # arrow_w + gap
        bx = bar_left
        btn_w = 78
        per_page = getattr(self, "_palette_per_page", len(self._current_palette()))
        for vi in range(per_page):
            if bx <= x <= bx + btn_w:
                idx = self.palette_scroll + vi
                if idx < len(self._current_palette()):
                    return idx
                return None
            bx += btn_w + 3
        return None

    def _palette_arrow_at(self, x: int, y: int) -> str | None:
        """Return 'left' / 'right' if the click hit a palette scroll
        arrow, else None."""
        for x1, x2, y1, y2, action in getattr(self, "_palette_arrow_rects", []):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return action
        return None

    def _tab_at(self, x: int, y: int) -> int | None:
        if not (BOTTOM_BAR_H - TAB_H <= y <= BOTTOM_BAR_H):
            return None
        bx = 8
        for i, (_cat_id, cat_label, _) in enumerate(self.palette_by_cat):
            tw = max(60, len(cat_label) * 9 + 16)
            if bx <= x <= bx + tw:
                return i
            bx += tw + 4
        return None

    def _draw_palette_tooltip(self) -> None:
        idx = self.palette_hover
        if idx is None:
            return
        palette = self._current_palette()
        if idx >= len(palette):
            return
        bid = palette[idx]
        bd = self.registry[bid]

        lines: list[tuple[str, tuple[int, int, int]]] = [
            (bd.name, COLOR_GOLD),
            (bd.description, COLOR_WHITE),
        ]
        if bd.cost > 0:
            afford = self.economy.can_afford(bd.cost)
            lines.append((
                f"Cost: {bd.cost} gold",
                COLOR_GOLD if afford else COLOR_RED,
            ))
        if bd.production:
            prod_str = ", ".join(f"+{a} {r}" for r, a in bd.production.items())
            lines.append((f"Produces: {prod_str}", COLOR_GREEN))
        if bd.consumption:
            cons_str = ", ".join(f"-{a} {r}" for r, a in bd.consumption.items())
            lines.append((f"Consumes: {cons_str}", COLOR_RED))
        if bd.workers > 0:
            lines.append((f"Workers: {bd.workers}", COLOR_WHITE))
        if bd.requires_road:
            lines.append(("Needs road", COLOR_GRAY))

        # Position above mouse, clamped to screen.
        tw = 220
        th = len(lines) * 14 + 8
        tx = max(4, min(self.mouse_x - tw / 2, self.width - tw - 4))
        ty = max(BOTTOM_BAR_H + 4, self.mouse_y + 12)
        arcade.draw_lrbt_rectangle_filled(tx, tx + tw, ty, ty + th, (20, 15, 10, 230))
        arcade.draw_lrbt_rectangle_outline(tx, tx + tw, ty, ty + th, COLOR_GOLD, 1)
        for i, (line, color) in enumerate(lines[:8]):
            t = self.txt_tooltip_lines[i]
            t.text = line
            t.color = color
            t.x = tx + 6
            t.y = ty + th - 14 - i * 14
            t.draw()

    # ── v0.14: world hover tooltip ────────────────────────────────────────
    # When the mouse hovers a tile (no building selected for placement,
    # no inspector open, no palette tooltip in flight) we surface what's
    # under the cursor: a walker if one is on that tile, otherwise the
    # building, otherwise the terrain feature, otherwise nothing. The
    # rationale was the v0.13 review's call: hover_row/hover_col were
    # tracked by on_mouse_motion but no UI ever read them — natural
    # resources, walkers, and worker fill levels were invisible without
    # opening the inspector.
    def _hover_tooltip_lines(
        self, row: int, col: int,
    ) -> list[tuple[str, tuple[int, int, int]]] | None:
        """Build the tooltip line list for the tile at (row, col), or None
        if there's nothing worth surfacing (plain grass with no walker
        and no feature)."""
        # 1. Walker takes priority — walkers move and the player wants
        # to track them. Hit-test against current position (not target —
        # interpolation is visual only). DeliveryWalkers carry a `good`;
        # CombatWalkers carry hp.
        for w in self.walker_manager.walkers:
            if w.row == row and w.col == col and not w.done:
                return self._walker_tooltip_lines(w)

        # 2. Building.
        existing = self.game_map.get_building_at(row, col)
        if existing is not None and existing[0] != "road":
            bid, orow, ocol = existing
            return self._building_tooltip_lines(bid, orow, ocol)

        # 3. Terrain feature.
        feat = self.game_map.feature_at(row, col)
        if feat is not None:
            return self._feature_tooltip_lines(feat)

        return None

    def _walker_tooltip_lines(self, w) -> list[tuple[str, tuple[int, int, int]]]:
        # Title = role with a friendly capitalisation.
        title = w.role.capitalize()
        lines: list[tuple[str, tuple[int, int, int]]] = [
            (title, COLOR_GOLD),
        ]
        # DeliveryWalker: show cargo. The class isn't imported at the
        # top of game_window — duck-type on `good` instead.
        good = getattr(w, "good", None)
        if good:
            lines.append((f"Carrying: {good}", COLOR_WHITE))
        # CombatWalkers (Soldier / Enemy) carry hp/damage.
        hp = getattr(w, "hp", None)
        if hp is not None:
            max_hp = getattr(w, "max_hp", hp)
            color = COLOR_GREEN if hp >= max_hp * 0.66 else (
                COLOR_RED if hp <= max_hp * 0.33 else COLOR_GOLD
            )
            lines.append((f"HP: {hp}/{max_hp}", color))
        lines.append((f"At ({w.row}, {w.col})", COLOR_GRAY))
        return lines

    def _building_tooltip_lines(
        self, bid: str, orow: int, ocol: int,
    ) -> list[tuple[str, tuple[int, int, int]]]:
        bd = self.registry[bid]
        lines: list[tuple[str, tuple[int, int, int]]] = [
            (bd.name, COLOR_GOLD),
        ]
        # Activity status (v0.12 BuildingStatus). The inspector shows
        # full reason; the hover tooltip shows just the headline state
        # so the player can scan many buildings at a glance without
        # clicking each one.
        status = self.economy.building_status.get((orow, ocol))
        if status is not None:
            color = {
                "active":      COLOR_GREEN,
                "partial":     COLOR_GOLD,
                "starved":     COLOR_RED,
                "unstaffed":   COLOR_RED,
                "disconnected": COLOR_RED,
                "idle":        COLOR_GRAY,
            }.get(status.state, COLOR_WHITE)
            lines.append((status.reason, color))
            if bd.workers > 0:
                wf, wn = status.workers_filled, status.workers_needed
                w_color = COLOR_GREEN if wf >= wn else (
                    COLOR_RED if wf == 0 else COLOR_GOLD
                )
                lines.append((f"Workers: {wf}/{wn}", w_color))
        elif bd.workers > 0:
            lines.append((f"Workers: {bd.workers}", COLOR_WHITE))
        if bd.production:
            prod_str = ", ".join(f"+{a} {r}" for r, a in bd.production.items())
            lines.append((f"Produces: {prod_str}", COLOR_GREEN))
        if bd.consumption:
            cons_str = ", ".join(f"-{a} {r}" for r, a in bd.consumption.items())
            lines.append((f"Consumes: {cons_str}", COLOR_RED))
        return lines

    def _feature_tooltip_lines(
        self, feat: str,
    ) -> list[tuple[str, tuple[int, int, int]]]:
        label = FEATURE_LABELS.get(feat, feat.replace("_", " ").capitalize())
        # The colour swatch comes from FEATURE_COLORS so the title in
        # the tooltip matches the dot the player sees on the map.
        title_color = FEATURE_COLORS.get(feat, COLOR_GOLD)
        lines: list[tuple[str, tuple[int, int, int]]] = [
            (label, title_color),
        ]
        consumers = self._feature_consumers.get(feat, [])
        if consumers:
            # Two lines if many consumers, one if few — keeps the
            # tooltip compact for the common case.
            joined = ", ".join(consumers)
            if len(joined) <= 28:
                lines.append((f"Used by: {joined}", COLOR_WHITE))
            else:
                lines.append(("Used by:", COLOR_WHITE))
                lines.append((f"  {joined}", COLOR_WHITE))
        else:
            lines.append(("Decorative — no extractor", COLOR_GRAY))
        # v0.14: depletion. Reserves are stored on the GameMap; we
        # surface them on the hover so the player can see "this
        # forest is almost gone, time to relocate the lumber mill"
        # without opening an inspector. Inexhaustible features
        # (None reserves) get no extra line — saying "infinite"
        # would just be noise.
        reserves = self.game_map.feature_reserves(self.hover_row, self.hover_col)
        if reserves is not None:
            if reserves <= 0:
                lines.append(("Depleted", COLOR_RED))
            elif reserves < 100:
                lines.append((f"Reserves: {reserves:.0f} (low)", COLOR_RED))
            elif reserves < 300:
                lines.append((f"Reserves: {reserves:.0f}", COLOR_GOLD))
            else:
                lines.append((f"Reserves: {reserves:.0f}", COLOR_WHITE))
        return lines

    def _draw_hover_tooltip(self) -> None:
        # Suppress when something else is already taking the player's
        # attention: the inspector panel, palette tooltip, ESC menu,
        # or active placement of a building (the placement preview
        # shows different per-tile feedback already).
        if self.inspected is not None:
            return
        if self.palette_hover is not None:
            return
        if self.show_menu:
            return
        # v0.17: while paused, the player explicitly wants to look
        # around — so we DON'T suppress the tooltip just because a
        # building is selected for placement. The placement preview
        # (the green/red translucent footprint) is part of the world
        # render and stays under the pause dim; the tooltip rendered
        # over the dim is what the player is actively reading.
        if self.selected_building is not None and not self.paused:
            return
        if self.hover_row is None or self.hover_col is None:
            return
        lines = self._hover_tooltip_lines(self.hover_row, self.hover_col)
        if not lines:
            return
        # Same drawing pattern as palette tooltip; offset to the right
        # of the cursor so the cursor doesn't sit on top of the title.
        tw = 220
        th = len(lines) * 14 + 8
        tx = max(4, min(self.mouse_x + 14, self.width - tw - 4))
        ty = max(BOTTOM_BAR_H + 4, self.mouse_y + 12)
        # Clamp top edge against the top bar.
        if ty + th > self.height - TOP_BAR_H - 4:
            ty = self.mouse_y - th - 12
        arcade.draw_lrbt_rectangle_filled(tx, tx + tw, ty, ty + th, (20, 15, 10, 230))
        arcade.draw_lrbt_rectangle_outline(tx, tx + tw, ty, ty + th, COLOR_GOLD, 1)
        for i, (line, color) in enumerate(lines[:8]):
            t = self.txt_tooltip_lines[i]
            t.text = line
            t.color = color
            t.x = tx + 6
            t.y = ty + th - 14 - i * 14
            t.draw()

    def _draw_info_panel(self) -> None:
        if self.inspected is None:
            self._warehouse_btn_rects = []
            self._shipyard_btn_rects = []
            return
        orow, ocol = self.inspected
        cell = self.game_map.grid[orow][ocol]
        if cell is None:
            self.inspected = None
            self._warehouse_btn_rects = []
            self._shipyard_btn_rects = []
            return
        bid, _, _ = cell
        bd = self.registry[bid]

        # v0.11: warehouses get a taller panel because they sprout a
        # button grid for the accept/reject filter. We extend down
        # rather than up so the title bar stays in the same place.
        panel_h = INFO_PANEL_H
        is_storage = bd.storage > 0
        # v0.41: shipyards (buildings that build ships) sprout a build-
        # queue footer — add ship buttons + the pending-hull list.
        is_shipyard = bool(getattr(bd, "ship_kind", ""))
        if is_storage:
            # Two rows of toggle buttons (~7 per row = 14 goods) + a
            # separator + a 'Drain' button. ~110px footer.
            panel_h = INFO_PANEL_H + 110
        elif is_shipyard:
            # Footer holds: an 'add' button + a header + up to
            # SHIPYARD_QUEUE_MAX (8) queue rows at ~19px each. ~210px
            # comfortably fits the cap without the per-row space guard
            # tripping.
            panel_h = INFO_PANEL_H + 210

        # Position: right side of map, just left of the right panel.
        px = self.width - RIGHT_PANEL_W - INFO_PANEL_W - 8
        py = self.height - TOP_BAR_H - panel_h - 8
        arcade.draw_lrbt_rectangle_filled(
            px, px + INFO_PANEL_W, py, py + panel_h, (30, 22, 18, 235),
        )
        arcade.draw_lrbt_rectangle_outline(
            px, px + INFO_PANEL_W, py, py + panel_h, COLOR_GOLD, 2,
        )

        title = bd.name
        if bd.is_evolvable:
            tier = get_house_tier(self.game_map, orow, ocol)
            title = f"{bd.name} (Tier {tier})"
        self.txt_info_title.text = title
        self.txt_info_title.x = px + 8
        self.txt_info_title.y = py + panel_h - 18
        self.txt_info_title.draw()

        # Build info lines.
        lines: list[tuple[str, tuple[int, int, int]]] = []
        lines.append((f"At ({orow},{ocol}), {bd.width}x{bd.height}", COLOR_GRAY))

        # v0.12: per-building activity status, rich version. The economy
        # populates a BuildingStatus per (row, col) each tick — we read
        # whatever it last said. For brand-new buildings or pure passive
        # infrastructure (roads, wells) the dict may not have an entry;
        # treat absence as "idle" without alarming the player.
        status = self.economy.building_status.get((orow, ocol))
        if status is not None:
            color_for_state = {
                "active":       COLOR_GREEN,
                "partial":      COLOR_GOLD,
                "disconnected": COLOR_RED,
                "unstaffed":    COLOR_RED,
                "starved":      COLOR_RED,
                "idle":         COLOR_GRAY,
            }
            lines.append((
                status.reason,
                color_for_state.get(status.state, COLOR_WHITE),
            ))

        if bd.requires_road:
            connected = self.road_network.is_connected(bid, orow, ocol)
            lines.append((
                "✓ On road network" if connected else "✗ NOT on road network",
                COLOR_GREEN if connected else COLOR_RED,
            ))
        if bd.workers > 0:
            # v0.12: workers_filled vs workers_needed. The status snapshot
            # has both — fall back to the static slot count if the building
            # hasn't been ticked yet (inspector opened immediately after
            # placement, before the next tick fires).
            if status is not None:
                wf = status.workers_filled
                wn = status.workers_needed
                color = COLOR_GREEN if wf >= wn else (
                    COLOR_RED if wf == 0 else COLOR_GOLD
                )
                lines.append((f"Workers: {wf}/{wn}", color))
            else:
                lines.append((f"Workers needed: {bd.workers}", COLOR_WHITE))
        if bd.production:
            lines.append(("Produces: " + ", ".join(
                f"{a} {r}" for r, a in bd.production.items()
            ), COLOR_GREEN))
        if bd.consumption:
            lines.append(("Consumes: " + ", ".join(
                f"{a} {r}" for r, a in bd.consumption.items()
            ), COLOR_RED))
        # ── v0.23.x: cumulative produced / consumed lifetime totals ──
        # Per-building numbers maintained by EconomyManager (drained on
        # demolish). Only shown when the building has actually run for
        # at least one productive tick; brand-new placements stay silent
        # so the inspector doesn't flash empty "Lifetime: 0" rows.
        produced_l = self.economy.produced_lifetime.get((orow, ocol))
        if produced_l:
            top = sorted(
                produced_l.items(), key=lambda kv: kv[1], reverse=True,
            )[:3]
            lines.append((
                "Lifetime made: " + ", ".join(
                    f"{int(round(v))} {r}" for r, v in top
                ),
                COLOR_GREEN,
            ))
        consumed_l = self.economy.consumed_lifetime.get((orow, ocol))
        if consumed_l:
            top = sorted(
                consumed_l.items(), key=lambda kv: kv[1], reverse=True,
            )[:3]
            lines.append((
                "Lifetime used: " + ", ".join(
                    f"{int(round(v))} {r}" for r, v in top
                ),
                COLOR_RED,
            ))
        # Show services *received* at this tile (useful for houses).
        if bd.is_evolvable or bd.needs_services:
            services = self.service_map.services_at(orow, ocol)
            if services:
                for s, v in sorted(services.items()):
                    ok = v >= 1.0
                    lines.append((
                        f"  {s}: {v:.1f}",
                        COLOR_GREEN if ok else COLOR_RED,
                    ))
            else:
                lines.append(("  (no services in range)", COLOR_GRAY))
        if bd.provides_service:
            lines.append((
                f"Provides {bd.provides_service} (r={bd.service_radius})",
                COLOR_GOLD,
            ))

        # ── v0.45: harbor berth occupancy ───────────────────────────────
        # A harbor (ship_slots > 0) shows how many of its berths are
        # currently occupied by idle, role-matching ships.
        if getattr(bd, "ship_slots", 0) > 0:
            occ = self.walker_manager.harbor_berth_occupancy(self.game_map)
            slot = occ.get((orow, ocol))
            if slot is not None:
                used, cap = slot["occupied"], slot["capacity"]
                lines.append((
                    f"Berths: {used}/{cap}",
                    COLOR_GREEN if used < cap else COLOR_GOLD,
                ))

        # ── v0.9: decay status — only shown when below the warning
        # threshold. A pristine building is silent on the inspector.
        decay_label = self.decay.status_label(orow, ocol)
        if decay_label is not None:
            text, severity = decay_label
            color = COLOR_RED if severity == "critical" else COLOR_GOLD
            lines.append((text, color))

        # ── v0.9: storage facility stock view (real, not estimated) ───────
        # Warehouses (and any building with `storage > 0`) read directly
        # from the Storage allocator, which holds real per-warehouse
        # stocks rebuilt every tick from the global pool.
        if is_storage:
            lines.append(("", COLOR_WHITE))   # spacer
            stocks = self.storage.contents(orow, ocol)
            used = self.storage.used(orow, ocol)
            free = self.storage.free_slots(orow, ocol)
            cap = bd.storage
            color_used = COLOR_GREEN if used < cap * 0.9 else COLOR_RED
            lines.append((
                f"Stocks: {used}/{cap}", color_used,
            ))
            lines.append((
                f"Free slots: {free}",
                COLOR_GREEN if free > 0 else COLOR_RED,
            ))
            if stocks:
                # Sorted dominant-first so the headline good is on top.
                for good, qty in sorted(stocks.items(), key=lambda x: x[1], reverse=True)[:6]:
                    lines.append((f"  {good}: {qty}", COLOR_WHITE))
            else:
                lines.append(("  (empty)", COLOR_GRAY))

        # Render every line we collected (capped at the pool size so we
        # never index out of bounds). Lines start 36 below the panel
        # top (which is at py + panel_h).
        for i, (text, color) in enumerate(lines[:len(self.txt_info_lines)]):
            t = self.txt_info_lines[i]
            t.text = text
            t.color = color
            t.x = px + 8
            t.y = py + panel_h - 36 - i * 14
            t.draw()

        # ── v0.11: warehouse accept/reject toggle grid ───────────────────
        # Only drawn for storage buildings. Each good gets a small button
        # showing whether the warehouse currently accepts it. Clicking
        # toggles. The Drain button sets accepts=∅ (the warehouse will
        # not take any new goods on the next distribute() — its current
        # contents flow back to other warehouses or overflow).
        if is_storage:
            self._draw_warehouse_toggles(orow, ocol, px, py)
            self._shipyard_btn_rects = []
        elif is_shipyard:
            self._draw_shipyard_queue(orow, ocol, bd, px, py)
            self._warehouse_btn_rects = []
        else:
            self._warehouse_btn_rects = []
            self._shipyard_btn_rects = []

    def _draw_warehouse_toggles(
        self, orow: int, ocol: int, px: int, py: int,
    ) -> None:
        """Render the per-good accept/reject toggle row for a warehouse.

        Lives outside ``_draw_info_panel`` to keep that method shorter
        and to make the click-handling region computation testable in
        isolation (we cache the rects on ``self._warehouse_btn_rects``).

        Layout: a footer band at the bottom of the panel, two rows of
        small buttons. Each button shows the good's first letter and is
        green when accepted, dimmed when rejected. A 'Drain' button at
        the right edge sets accepts to the empty set.
        """
        rects: list[tuple[int, int, int, int, str]] = []
        current = self.storage.accepts(orow, ocol)  # None = accept all

        # Two-row grid. 7 buttons per row × 14 standard goods.
        goods = self.storage.STANDARD_GOODS
        btn_w = 24
        btn_h = 18
        gap = 2
        cols = 7
        # Footer starts 6px above the panel bottom edge and grows up.
        # Leave a 2px gutter between the last info line and the buttons.
        footer_y = py + 8       # bottom row's y1
        row_y = [footer_y, footer_y + btn_h + gap]

        # "Filter:" label above the grid.
        label_y = row_y[1] + btn_h + 4
        # Use a transient text object — we don't draw enough text to
        # justify a pre-allocated pool here; arcade.Text creation is
        # O(once) since we only do this when the inspector is open.
        arcade.Text(
            "Filter (click to toggle, all=on by default):",
            px + 8, label_y, COLOR_GRAY, 8,
        ).draw()

        for i, good in enumerate(goods):
            row = i // cols
            col = i % cols
            x1 = px + 8 + col * (btn_w + gap)
            x2 = x1 + btn_w
            y1 = row_y[1 - row]  # top row first (reads naturally)
            y2 = y1 + btn_h
            accepted = current is None or good in current
            color = (60, 130, 70) if accepted else (60, 50, 45)
            arcade.draw_lrbt_rectangle_filled(x1, x2, y1, y2, color)
            arcade.draw_lrbt_rectangle_outline(
                x1, x2, y1, y2,
                COLOR_GOLD if accepted else COLOR_GRAY, 1,
            )
            # Single-letter glyph: first char of good name. 'p' alone
            # is ambiguous between pottery/planks; use 2 chars when
            # they collide.
            glyph = good[:2] if good in ("planks", "pottery") else good[0]
            arcade.Text(
                glyph, x1 + 4, y1 + 3,
                COLOR_WHITE if accepted else COLOR_GRAY, 9, bold=True,
            ).draw()
            rects.append((x1, x2, y1, y2, good))

        # 'Drain' button on the right side, spans both rows.
        drain_x1 = px + INFO_PANEL_W - 56
        drain_x2 = px + INFO_PANEL_W - 8
        drain_y1 = row_y[0]
        drain_y2 = row_y[1] + btn_h
        is_draining = current is not None and len(current) == 0
        drain_color = (130, 50, 50) if is_draining else (60, 40, 35)
        arcade.draw_lrbt_rectangle_filled(
            drain_x1, drain_x2, drain_y1, drain_y2, drain_color,
        )
        arcade.draw_lrbt_rectangle_outline(
            drain_x1, drain_x2, drain_y1, drain_y2,
            COLOR_GOLD if is_draining else COLOR_GRAY, 1,
        )
        arcade.Text(
            "Drain" if not is_draining else "✓Drain",
            drain_x1 + 6, drain_y1 + (drain_y2 - drain_y1) // 2 - 5,
            COLOR_WHITE, 9, bold=True,
        ).draw()
        rects.append((drain_x1, drain_x2, drain_y1, drain_y2, "__drain__"))

        # 'Reset' button just under the drain — clears the filter back
        # to None (accept all). Only useful when something IS filtered.
        reset_x1 = drain_x1 - 50
        reset_x2 = drain_x1 - 2
        reset_y1 = drain_y1
        reset_y2 = drain_y2
        is_default = current is None
        reset_color = (40, 40, 40) if is_default else (60, 90, 60)
        arcade.draw_lrbt_rectangle_filled(
            reset_x1, reset_x2, reset_y1, reset_y2, reset_color,
        )
        arcade.draw_lrbt_rectangle_outline(
            reset_x1, reset_x2, reset_y1, reset_y2,
            COLOR_GRAY if is_default else COLOR_GOLD, 1,
        )
        arcade.Text(
            "All" if is_default else "Reset",
            reset_x1 + 6, reset_y1 + (reset_y2 - reset_y1) // 2 - 5,
            COLOR_GRAY if is_default else COLOR_WHITE, 9, bold=True,
        ).draw()
        rects.append((reset_x1, reset_x2, reset_y1, reset_y2, "__reset__"))

        self._warehouse_btn_rects = rects

    # ── v0.41: shipyard build-queue footer ───────────────────────────────
    # Friendly labels for the kinds a yard can build. The yard's own
    # ``ship_kind`` selects which "add" button is offered.
    _SHIP_KIND_LABEL = {
        "trade_ship": "Cargo ship",
        "warship": "Warship",
        "transport_ship": "Transport",
    }

    def _draw_shipyard_queue(
        self, orow: int, ocol: int, bd, px: int, py: int,  # noqa: ANN001
    ) -> None:
        """Render the build-queue footer for a shipyard: one '+ <ship>'
        button to enqueue a hull of the yard's kind, followed by the
        list of currently-queued hulls (oldest first). The head entry
        shows a build-progress bar; every entry has a ✕ remove button.

        Caches click regions on ``self._shipyard_btn_rects`` as
        (x1, x2, y1, y2, action) tuples where action is ``"__add__"`` or
        ``"__rm__<index>"``. Mirrors ``_draw_warehouse_toggles`` so the
        click-handling path is the same shape.
        """
        rects: list[tuple[int, int, int, int, str]] = []
        kind = getattr(bd, "ship_kind", "")
        label = self._SHIP_KIND_LABEL.get(kind, kind or "Ship")

        queue = self.walker_manager.shipyard_queue(self.game_map, orow, ocol)
        state = self.game_map.building_state.get((orow, ocol), {})
        build_t = int(state.get("ship_build_t", 0))
        interval = self.walker_manager.SHIPYARD_INTERVAL
        qmax = self.walker_manager.SHIPYARD_QUEUE_MAX

        row_h = 16
        gap = 3
        stride = row_h + gap
        # Footer layout, top-down from a fixed top inside the panel:
        #   [add button] [header] [row 1] ... [row qmax]
        # The panel was grown (INFO_PANEL_H + 210) to fit exactly this.
        # We compute positions from the bottom up so the lowest queue
        # row sits comfortably above the panel's bottom edge (py + 8).
        bottom = py + 8
        # The add button is the topmost element of the footer band.
        add_y1 = bottom + (qmax * stride) + 18  # leave room for header
        add_y2 = add_y1 + row_h + 4

        # ── '+ Add' button (left) ────────────────────────────────────
        full = len(queue) >= qmax
        add_x1 = px + 8
        add_x2 = px + INFO_PANEL_W - 8
        add_color = (60, 50, 45) if full else (60, 110, 70)
        arcade.draw_lrbt_rectangle_filled(
            add_x1, add_x2, add_y1, add_y2, add_color,
        )
        arcade.draw_lrbt_rectangle_outline(
            add_x1, add_x2, add_y1, add_y2,
            COLOR_GRAY if full else COLOR_GOLD, 1,
        )
        add_label = (
            f"Queue full ({qmax})" if full else f"+ Build {label}"
        )
        arcade.Text(
            add_label, add_x1 + 8, add_y1 + 4,
            COLOR_GRAY if full else COLOR_WHITE, 10, bold=True,
        ).draw()
        if not full:
            rects.append((add_x1, add_x2, add_y1, add_y2, "__add__"))

        # ── Queue header ─────────────────────────────────────────────
        hdr_y = add_y1 - 14
        arcade.Text(
            f"Build queue ({len(queue)}/{qmax}):",
            px + 8, hdr_y, COLOR_GRAY, 8,
        ).draw()

        # ── Queued rows (oldest first; head shows build progress) ────
        # Row i top descends from just under the header. With the panel
        # sized for qmax rows, the last row's bottom lands at ~py+8.
        rows_top = hdr_y - 6
        for i, q_kind in enumerate(queue):
            r_y2 = rows_top - i * stride
            r_y1 = r_y2 - row_h
            if r_y1 < py + 6:
                break  # ran out of footer space (shouldn't with qmax=8)
            q_label = self._SHIP_KIND_LABEL.get(q_kind, q_kind)
            # Row background.
            arcade.draw_lrbt_rectangle_filled(
                px + 8, px + INFO_PANEL_W - 8, r_y1, r_y2, (45, 38, 32),
            )
            # Head-of-queue: draw a build-progress fill behind the label.
            if i == 0 and interval > 0:
                frac = max(0.0, min(1.0, build_t / interval))
                if frac > 0:
                    fill_r = px + 8 + int(
                        (INFO_PANEL_W - 16 - 22) * frac
                    )
                    arcade.draw_lrbt_rectangle_filled(
                        px + 8, fill_r, r_y1, r_y2, (70, 95, 55),
                    )
            arcade.draw_lrbt_rectangle_outline(
                px + 8, px + INFO_PANEL_W - 8, r_y1, r_y2, COLOR_GRAY, 1,
            )
            tag = f"{i + 1}. {q_label}"
            if i == 0:
                pct = int(100 * build_t / interval) if interval else 0
                tag += f"  (building {pct}%)"
            arcade.Text(
                tag, px + 12, r_y1 + 3, COLOR_WHITE, 9,
            ).draw()
            # ✕ remove button at the right edge of the row.
            rm_x2 = px + INFO_PANEL_W - 10
            rm_x1 = rm_x2 - 18
            arcade.draw_lrbt_rectangle_filled(
                rm_x1, rm_x2, r_y1 + 1, r_y2 - 1, (120, 50, 45),
            )
            arcade.Text(
                "✕", rm_x1 + 5, r_y1 + 3, COLOR_WHITE, 9, bold=True,
            ).draw()
            rects.append((rm_x1, rm_x2, r_y1, r_y2, f"__rm__{i}"))

        self._shipyard_btn_rects = rects

    def _draw_minimap(self) -> None:
        # Bottom-left corner of the playable area, above the bottom bar.
        mx = MINIMAP_MARGIN
        my = BOTTOM_BAR_H + MINIMAP_MARGIN
        arcade.draw_lrbt_rectangle_filled(
            mx, mx + MINIMAP_W, my, my + MINIMAP_H, (20, 15, 10, 220),
        )
        arcade.draw_lrbt_rectangle_outline(
            mx, mx + MINIMAP_W, my, my + MINIMAP_H, COLOR_GOLD, 1,
        )
        # Tile dimensions in mini-map space.
        # v0.26: read map dimensions off the live GameMap so a
        # resized editor map renders correctly. The minimap fits
        # the full map regardless of size — tile cells just get
        # bigger or smaller per axis.
        gm_rows = self.game_map.rows
        gm_cols = self.game_map.cols
        tw = MINIMAP_W / gm_cols
        th = MINIMAP_H / gm_rows
        # Terrain (very cheap — 1200 cells max).
        # v0.15: hills and mountains rendered distinctly so the player
        # can plan placement from the minimap (mountains read as
        # impassable; hills as "rough but flat enough").
        for r in range(gm_rows):
            for c in range(gm_cols):
                t = self.game_map.terrain[r][c]
                if t == TERRAIN_WATER:
                    color = COLOR_WATER
                elif t == TERRAIN_HILLS:
                    color = COLOR_HILLS
                elif t == TERRAIN_MOUNTAINS:
                    color = COLOR_MOUNTAINS
                else:
                    color = COLOR_GRASS
                arcade.draw_lrbt_rectangle_filled(
                    mx + c * tw, mx + (c + 1) * tw,
                    my + r * th, my + (r + 1) * th, color,
                )
        # Buildings — overlay.
        for bt, orow, ocol in self.game_map.get_building_positions():
            bd = self.registry.get(bt)
            if bd is None:
                continue
            arcade.draw_lrbt_rectangle_filled(
                mx + ocol * tw, mx + (ocol + bd.width) * tw,
                my + orow * th, my + (orow + bd.height) * th,
                bd.color,
            )
        # Camera viewport rect.
        # v0.29: clamp to live map dims (gm_cols/gm_rows), not the
        # static GRID_COLS/GRID_ROWS, so the viewport rectangle stays
        # accurate on resized (e.g. 128x128) maps.
        cam_x, cam_y = self.world_camera.position
        zoom = self.world_camera.zoom
        view_w_world = self.width / zoom
        view_h_world = self.height / zoom
        x0 = (cam_x - view_w_world / 2) / TILE_SIZE
        x1 = (cam_x + view_w_world / 2) / TILE_SIZE
        y0 = (cam_y - view_h_world / 2) / TILE_SIZE
        y1 = (cam_y + view_h_world / 2) / TILE_SIZE
        arcade.draw_lrbt_rectangle_outline(
            mx + max(0, x0) * tw, mx + min(gm_cols, x1) * tw,
            my + max(0, y0) * th, my + min(gm_rows, y1) * th,
            COLOR_GOLD, 1,
        )

    def _minimap_at(self, x: int, y: int) -> tuple[int, int] | None:
        """If (x,y) is over the minimap, return the corresponding (row, col).

        v0.29: read map dimensions off the live ``GameMap`` instead of
        the static ``GRID_ROWS`` / ``GRID_COLS`` constants. Without this,
        clicks on a 128x128 map landed on the wrong tile (the conversion
        used the default 40x30 scale) and tiles past row/col 29/39 were
        unreachable from the minimap altogether. Mirrors the tile-size
        computation in ``_draw_minimap`` so the two stay in lockstep.
        """
        if not self.show_minimap:
            return None
        mx = MINIMAP_MARGIN
        my = BOTTOM_BAR_H + MINIMAP_MARGIN
        if not (mx <= x <= mx + MINIMAP_W and my <= y <= my + MINIMAP_H):
            return None
        gm_rows = self.game_map.rows
        gm_cols = self.game_map.cols
        tw = MINIMAP_W / gm_cols
        th = MINIMAP_H / gm_rows
        col = int((x - mx) / tw)
        row = int((y - my) / th)
        return max(0, min(gm_rows - 1, row)), max(0, min(gm_cols - 1, col))

    # ── v0.6: filter overlays ─────────────────────────────────────────────
    def _overlay_metric(self, kind: str, row: int, col: int) -> float:
        """Return a 0..1 'need / signal' value for one tile.

        For service overlays, the metric is 1.0 - coverage (so red = no
        service, green = full service). For 'unhappy', a global city-
        happiness inversion modulated by housing tiles. For 'road_access',
        1.0 if the tile is a building that needs a road and isn't
        connected, 0.0 otherwise. The caller blends this onto the tile.

        v0.17: 'jobless' reads from the per-frame ``_jobless_grid``
        cache (computed once at the start of ``_draw_overlay`` because
        building-footprint expansion isn't a per-tile operation). The
        cache is keyed by (row, col) → 0..1 shortfall ratio.
        """
        if kind.startswith("service:"):
            service = kind.split(":", 1)[1]
            cov = self.service_map.coverage(service, row, col)
            # Cap at 1.0; need = 1 - cov clamped.
            return max(0.0, min(1.0, 1.0 - cov))
        if kind == "unhappy":
            cell = self.game_map.grid[row][col]
            if cell is None or cell[0] != "house":
                return 0.0
            # Happiness scales city-wide; use it as the need.
            return max(0.0, 1.0 - self.economy.happiness / 100.0)
        if kind == "road_access":
            cell = self.game_map.grid[row][col]
            if cell is None:
                return 0.0
            bid, orow, ocol = cell
            bd = self.registry.get(bid)
            if bd is None or not bd.requires_road:
                return 0.0
            return 0.0 if self.road_network.is_connected(bid, orow, ocol) else 1.0
        if kind == "jobless":
            grid = getattr(self, "_jobless_grid", None)
            if grid is None:
                return 0.0
            try:
                return grid[row][col]
            except (IndexError, TypeError):
                return 0.0
        if kind == "goods_flow":
            # v0.19.x: read the manager's per-tile heatmap, normalised
            # by the per-frame max we cached in _draw_overlay. We use
            # a green→amber→red ramp via the existing colour selector;
            # _draw_overlay knows to flip the palette for goods_flow
            # so high traffic reads as bright (green) rather than red.
            heat = getattr(self, "_flow_heat_snapshot", None)
            if not heat:
                return 0.0
            v = heat.get((row, col), 0.0)
            mx = getattr(self, "_flow_heat_max", 0.0)
            if mx <= 0.0:
                return 0.0
            # Cube-root keeps small intensities visible — a tile with
            # a single recent deposit shouldn't be invisible next to
            # a 10-deposit hotspot. (sqrt brightens too much; cbrt
            # gives a gentle perceptual ramp.)
            return min(1.0, (v / mx) ** (1.0 / 3.0))
        return 0.0

    def _draw_overlay(self) -> None:
        """Tint each tile by its metric — red = high need, green = none."""
        if self.overlay_idx <= 0 or self.overlay_idx >= len(OVERLAY_NAMES):
            return
        _label, kind = OVERLAY_NAMES[self.overlay_idx]
        # v0.26: overlay loops use the live map dimensions, not the
        # module constants — a resized editor map may be smaller
        # than GRID_ROWS/COLS and iterating past it would IndexError
        # on the heatmap arrays.
        gm_rows = self.game_map.rows
        gm_cols = self.game_map.cols
        # v0.17: jobless overlay needs a per-building → per-tile
        # expansion. We compute it once per frame here, stash it on
        # self, and let _overlay_metric read it for each tile.
        if kind == "jobless":
            from jobs import jobless_overlay_grid, snapshot_jobs
            snap = snapshot_jobs(
                self.game_map, self.registry,
                self.economy.building_status, self.economy.population,
            )
            self._jobless_grid = jobless_overlay_grid(
                snap, self.game_map, self.registry,
                rows=gm_rows, cols=gm_cols,
            )
        else:
            # Free the cache when we switch off jobless so memory
            # doesn't hold the last frame's grid forever.
            self._jobless_grid = None
        # v0.19.x: snapshot the goods-flow heatmap so _overlay_metric
        # can normalise by the per-frame max. Snapshot rather than
        # live-read so the palette is stable within one paint pass.
        if kind == "goods_flow":
            heat = self.walker_manager.flow_heat()
            self._flow_heat_snapshot = dict(heat)
            self._flow_heat_max = max(heat.values()) if heat else 0.0
        else:
            self._flow_heat_snapshot = None
            self._flow_heat_max = 0.0
        # Tile loop. 1200 cells max — same scale as the minimap, fine
        # for a per-frame draw pass.
        # v0.19.x: for goods_flow, only iterate the tiles we actually
        # have heat for — the dict is sparse (~50-200 entries) so this
        # is much cheaper than the full 1200-cell sweep, and the
        # palette is also flipped (green = busy, not red).
        if kind == "goods_flow":
            if not self._flow_heat_snapshot:
                return
            for (r, c), _v in self._flow_heat_snapshot.items():
                if not (0 <= r < gm_rows and 0 <= c < gm_cols):
                    continue
                m = self._overlay_metric(kind, r, c)
                if m <= 0.01:
                    continue
                # Bright cyan → green → faint blue. Inverts the need
                # palette: high traffic is "good" (visible movement),
                # low traffic is "quiet". Alpha scales with intensity.
                if m > 0.66:
                    color = (80, 220, 200, 130)   # busy: cyan-green
                elif m > 0.33:
                    color = (90, 200, 120, 100)   # medium: green
                else:
                    color = (120, 180, 220, 70)   # light: pale blue
                x = c * TILE_SIZE
                y = r * TILE_SIZE
                arcade.draw_lrbt_rectangle_filled(
                    x, x + TILE_SIZE, y, y + TILE_SIZE, color,
                )
            return
        for r in range(gm_rows):
            for c in range(gm_cols):
                m = self._overlay_metric(kind, r, c)
                if m <= 0.01:
                    continue
                # Red→amber→green is unintuitive when the metric is "need":
                # red for high need (0.6+), amber mid (0.3-0.6), faint
                # otherwise. Alpha scales with the metric so the buildings
                # underneath stay legible.
                if m > 0.66:
                    color = (220, 60, 60, 130)
                elif m > 0.33:
                    color = (220, 160, 60, 100)
                else:
                    color = (220, 220, 60, 70)
                x = c * TILE_SIZE
                y = r * TILE_SIZE
                arcade.draw_lrbt_rectangle_filled(
                    x, x + TILE_SIZE, y, y + TILE_SIZE, color,
                )

    def _draw_overlay_legend(self) -> None:
        """Tiny top-right legend so the player knows what overlay is on."""
        if self.overlay_idx <= 0:
            return
        label, _ = OVERLAY_NAMES[self.overlay_idx]
        msg = f"OVERLAY: {label}  (O cycles, [ / ] step)"
        x0 = self.width - RIGHT_PANEL_W - 280
        y0 = self.height - TOP_BAR_H - 26
        arcade.draw_lrbt_rectangle_filled(
            x0, x0 + 270, y0, y0 + 22, (20, 15, 10, 220),
        )
        arcade.draw_lrbt_rectangle_outline(
            x0, x0 + 270, y0, y0 + 22, COLOR_GOLD, 1,
        )
        # Reusable text via the overlay text slot we'll preallocate.
        if not hasattr(self, "_txt_overlay"):
            self._txt_overlay = arcade.Text("", x0 + 6, y0 + 5, COLOR_GOLD, 10, bold=True)
        self._txt_overlay.text = msg
        self._txt_overlay.x = x0 + 6
        self._txt_overlay.y = y0 + 5
        self._txt_overlay.draw()

    # ── v0.6: graph plots ────────────────────────────────────────────────
    def _record_plot_sample(self) -> None:
        """Push one sample into each plot history. Called per game tick."""
        self._plot_history["population"].append(self.economy.population)
        self._plot_history["treasury"].append(self.economy.treasury)
        self._plot_history["food"].append(self.economy.resources.get("food", 0))
        self._plot_history["wood"].append(self.economy.resources.get("wood", 0))
        self._plot_history["iron"].append(self.economy.resources.get("iron", 0))
        self._plot_history["happiness"].append(self.economy.happiness)
        # v0.15: predictive / health metrics. fed_fraction × 100 so the
        # plot reads as a percentage like happiness. food_net is the
        # tick's food balance (prod − cons) — negative means the city
        # is bleeding food regardless of what the absolute stockpile
        # says. employed_ratio is filled/needed × 100; near-zero
        # means most workshops are starved of staff. stone_blocks is
        # the v0.13 civic-construction bottleneck the player monitors
        # when planning temples / senate / fountains.
        self._plot_history["fed_fraction"].append(
            getattr(self.economy, "fed_fraction", 1.0) * 100.0
        )
        self._plot_history["food_net"].append(
            getattr(self.economy, "food_balance", 0.0)
        )
        # employed_ratio: filled/needed. We approximate "needed" via
        # the sum of per-building worker requirements seen on this
        # tick — the economy doesn't expose that directly so we
        # mirror the workforce stash from `_calc_production` via
        # the building registry. Cheap walk.
        needed = 0
        for bt, _r, _c in self.game_map.get_building_positions():
            bd = self.registry.get(bt)
            if bd is not None and bd.workers > 0:
                needed += bd.workers
        ratio = (
            min(1.0, self.economy.employed / needed) * 100.0
            if needed > 0 else 100.0
        )
        self._plot_history["employed_ratio"].append(ratio)
        self._plot_history["stone_blocks"].append(
            self.economy.resources.get("stone_blocks", 0)
        )
        # v0.26: economic stats. The graphs window used to plot the
        # consumption side (food, stone_blocks) but never the money
        # flow — players asked for income / expenses / net so they
        # could read whether the economy is profitable at a glance.
        # All three live on EconomyManager already; we just sample.
        # tax_pct is the player's current tax rate (×100) so a single
        # plot can be read alongside the others without divide.
        income = float(getattr(self.economy, "income_per_tick", 0.0))
        expenses = float(getattr(self.economy, "expenses_per_tick", 0.0))
        self._plot_history["income"].append(income)
        self._plot_history["expenses"].append(expenses)
        self._plot_history["net"].append(income - expenses)
        self._plot_history["tax_pct"].append(
            float(getattr(self.economy, "tax_rate", 0.0)) * 100.0
        )
        # v0.54: four leading-indicator metrics. All read from values
        # the simulation already maintains — no new bookkeeping.
        #  • housing_headroom: capacity − population. Positive = room to
        #    grow; ≤0 means migration has nowhere to land. Can go
        #    negative briefly if housing is demolished under a full pop.
        #  • rebel_pressure: RebellionTracker.pressure as a % of the
        #    spawn threshold, so 100 = rebels about to spawn. Clamped at
        #    the top only for display sanity if pressure overshoots.
        #  • wage_ratio: wages paid / owed (×100). <100 = underpaying,
        #    which bleeds happiness a tick or two later.
        #  • jobless: population − employed (count, floored at 0).
        self._plot_history["housing_headroom"].append(
            float(getattr(self.economy, "housing_capacity", 0))
            - float(self.economy.population)
        )
        thr = float(getattr(self.balance, "rebel_pressure_threshold", 1.0)) or 1.0
        self._plot_history["rebel_pressure"].append(
            float(getattr(self.rebellion, "pressure", 0.0)) / thr * 100.0
        )
        self._plot_history["wage_ratio"].append(
            float(getattr(self.economy, "wage_payment_ratio", 1.0)) * 100.0
        )
        self._plot_history["jobless"].append(
            float(max(0, self.economy.population - getattr(self.economy, "employed", 0)))
        )

    def _draw_graphs(self) -> None:
        """Modal overlay panel showing rolling line plots.

        v0.14: panel height bumped from 460 to 520 and inner row spacing
        tightened so the per-plot title (above the box) and the
        `now/min/max` value line (below the box) no longer overlap
        the next row's title. Plot titles render in white with a
        coloured swatch; previously `Wood` in (140,100,60) against a
        dark grid was essentially invisible.

        v0.15: 4 new plots added (fed_fraction, food_net,
        employed_ratio, stone_blocks) — total is now 10 in a 2×5
        grid. Panel height grew accordingly so each row keeps
        the same vertical breathing room as the v0.14 layout.
        Width nudged up from 560 → 620 because two of the new
        labels ("Employed %", "Food net/t") are wider than the
        v0.14 max ("Happiness %") and we need them to fit
        without colliding with the swatch.

        v0.54: 4 more plots (housing_headroom, rebel_pressure,
        wage_ratio, jobless) take the total to 18. Rather than add
        an 8th row to the already-tall panel, the grid goes to
        3 columns × 6 rows: each plot keeps roughly its v0.26 row
        height (the trend-reading axis) and only loses horizontal
        width, which the line plots tolerate well. Panel widens
        620 → 900 to give the three columns usable boxes and
        shrinks 720 → 660 since six rows need less height than
        seven; the existing screen-clamp keeps it inside small
        windows.
        """
        panel_w = 900
        panel_h = 660
        cx = self.width / 2
        cy = self.height / 2
        l = cx - panel_w / 2
        r = cx + panel_w / 2
        b = cy - panel_h / 2
        t = cy + panel_h / 2
        # Clamp to screen — at 800px tall windows the 720px panel needs
        # to stay inside the viewport. Slide upward if we'd otherwise
        # bottom-clip; the title strip stays anchored to the top.
        if b < 20:
            shift = 20 - b
            b += shift
            t += shift
        if t > self.height - 20:
            shift = (self.height - 20) - t
            b += shift
            t += shift
        arcade.draw_lrbt_rectangle_filled(0, self.width, 0, self.height, (0, 0, 0, 140))
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (30, 22, 18, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)
        # Title.
        if not hasattr(self, "_txt_graph_title"):
            self._txt_graph_title = arcade.Text(
                "CITY METRICS", 0, 0, COLOR_GOLD, 14, bold=True, anchor_x="center",
            )
        self._txt_graph_title.text = "CITY METRICS  (G to close)"
        self._txt_graph_title.x = cx
        self._txt_graph_title.y = t - 24
        self._txt_graph_title.draw()
        # 3 columns × 6 rows of mini-plots (was 2×7 in v0.26–v0.53,
        # 2×5 pre-v0.26).
        plots = [
            ("population",     "Population",   COLOR_WHITE),
            ("treasury",       "Treasury",     COLOR_GOLD),
            ("food",           "Food",         (220, 200, 80)),
            ("wood",           "Wood",         (140, 100, 60)),
            ("iron",           "Iron",         (170, 170, 180)),
            ("happiness",      "Happiness %",  COLOR_GREEN),
            # v0.15: the four health metrics.
            ("fed_fraction",   "Fed %",        (240, 180, 60)),
            ("food_net",       "Food net/t",   (220, 100, 60)),
            ("employed_ratio", "Employed %",   (120, 200, 220)),
            ("stone_blocks",   "Stone blocks", (200, 200, 200)),
            # v0.26: the four missing economic stats. Income /
            # expenses / net give the player the money-flow readout
            # the panel was missing; tax_pct sits beside them so
            # the lever the player most often pulls (T cycles tax)
            # is visible in the same view.
            ("income",         "Income/t",     (120, 220, 120)),
            ("expenses",       "Expense/t",    (220, 120, 120)),
            ("net",            "Net/t",        COLOR_GOLD),
            ("tax_pct",        "Tax %",        (200, 170, 80)),
            # v0.54: four leading-indicator metrics. Colours chosen to
            # read as "watch me" warmth (rebel pressure red-orange,
            # jobless amber) vs calm (housing teal, wage blue) against
            # the dark grid.
            ("housing_headroom", "Housing room", (120, 200, 160)),
            ("rebel_pressure",   "Unrest %",     (230, 90, 70)),
            ("wage_ratio",       "Wages %",      (110, 160, 220)),
            ("jobless",          "Jobless",      (230, 190, 90)),
        ]
        # Row layout: top of grid sits below the title; allocate 28px
        # per row for chrome (title strip above + value strip below)
        # so the inner plot box fits comfortably between.
        grid_top = t - 50
        # v0.54: with 18 plots the grid moves from 2×7 to 3×6. Three
        # columns keep each plot's *height* (the trend-readability axis)
        # at the old /6-ish band instead of shrinking it further, at the
        # cost of narrower boxes — acceptable since the line plots read
        # vertically. col_w divides the inner width by 3; row_h by 6.
        grid_h = panel_h - 70
        N_COLS = 3
        N_ROWS = 6
        col_w = (panel_w - 20 - (N_COLS - 1) * 10) / N_COLS
        row_h = grid_h / N_ROWS
        TITLE_STRIP = 16     # space above plot box for the title row
        VALUE_STRIP = 16     # space below plot box for the value row
        # v0.15: pool grew from 6 → 10. The hasattr() guard preserves
        # the slot pool across redraws but skips re-creating it once
        # populated; the earlier code allocated len(plots) eagerly,
        # which now matches the new total without any per-frame
        # rework.
        if not hasattr(self, "_txt_graph_labels") or len(self._txt_graph_labels) != len(plots):
            self._txt_graph_labels = [
                arcade.Text("", 0, 0, COLOR_WHITE, 9, bold=True) for _ in plots
            ]
            self._txt_graph_values = [
                arcade.Text("", 0, 0, COLOR_GRAY, 8) for _ in plots
            ]
        for i, (key, label, color) in enumerate(plots):
            row = i // N_COLS
            col = i % N_COLS
            cell_l = l + 10 + col * (col_w + 10)
            cell_r = cell_l + col_w
            # Row's vertical band runs from `band_top` down by `row_h`.
            band_top = grid_top - row * row_h
            band_bot = band_top - row_h
            # Plot box sits inside the band, leaving room for the
            # title strip on top and the value strip on bottom.
            cell_t = band_top - TITLE_STRIP
            cell_b = band_bot + VALUE_STRIP
            # Plot box.
            arcade.draw_lrbt_rectangle_filled(
                cell_l, cell_r, cell_b, cell_t, (20, 15, 10, 220),
            )
            arcade.draw_lrbt_rectangle_outline(
                cell_l, cell_r, cell_b, cell_t, (90, 75, 55), 1,
            )
            # Title + colour swatch. Swatch sits to the right of the
            # title; the title itself stays white so it's legible
            # against the dark grid (the screenshot's `Wood` rendered
            # in (140,100,60) was almost invisible).
            lbl = self._txt_graph_labels[i]
            lbl.text = label
            lbl.color = COLOR_WHITE
            lbl.x = cell_l + 4
            lbl.y = cell_t + 3
            lbl.draw()
            # Small swatch — same colour as the line plot below.
            sw_size = 8
            sw_x = cell_l + 4 + len(label) * 6 + 4
            arcade.draw_lrbt_rectangle_filled(
                sw_x, sw_x + sw_size, cell_t + 4, cell_t + 4 + sw_size, color,
            )
            hist = list(self._plot_history.get(key, []))
            if hist:
                vmax = max(hist) or 1.0
                vmin = min(hist)
                last = hist[-1]
                vt = self._txt_graph_values[i]
                vt.text = f"now {last:.0f} | min {vmin:.0f} | max {vmax:.0f}"
                vt.x = cell_l + 4
                # Value strip sits *below* the plot box, comfortably
                # above the next row's title strip.
                vt.y = cell_b - 12
                vt.draw()
                # Line plot — `n` samples mapped across the plot width.
                n = len(hist)
                if n >= 2:
                    span = vmax - vmin if vmax > vmin else 1.0
                    inner_w = (cell_r - cell_l) - 8
                    inner_h = (cell_t - cell_b) - 8
                    px = cell_l + 4
                    py = cell_b + 4
                    pts: list[tuple[float, float]] = []
                    for j, v in enumerate(hist):
                        gx = px + (j / (n - 1)) * inner_w
                        gy = py + ((v - vmin) / span) * inner_h
                        pts.append((gx, gy))
                    # Draw connected line segments. Arcade's draw_line is
                    # cheap enough for ≤240 segments per plot.
                    for j in range(1, len(pts)):
                        x1, y1 = pts[j - 1]
                        x2, y2 = pts[j]
                        arcade.draw_line(x1, y1, x2, y2, color, 1.5)

    def _draw_notifications(self) -> None:
        y = self.height - TOP_BAR_H - 20
        for idx, (msg, color, _) in enumerate(self.notifications[:5]):
            if idx >= len(self.txt_notifications):
                break
            t = self.txt_notifications[idx]
            t.text = msg
            t.color = color
            t.x = self.width / 2
            t.y = y
            tw = len(msg) * 7 + 20
            cx = self.width / 2
            arcade.draw_lrbt_rectangle_filled(
                cx - tw / 2, cx + tw / 2, y - 8, y + 16, (20, 15, 10, 200),
            )
            arcade.draw_lrbt_rectangle_outline(
                cx - tw / 2, cx + tw / 2, y - 8, y + 16, color, 2,
            )
            t.draw()
            y -= 30

    def _draw_menu(self) -> None:
        arcade.draw_lrbt_rectangle_filled(0, self.width, 0, self.height, (0, 0, 0, 160))
        cx = self.width / 2
        cy = self.height / 2
        l, r = cx - MENU_W / 2, cx + MENU_W / 2
        b, top = cy - MENU_H / 2, cy + MENU_H / 2
        arcade.draw_lrbt_rectangle_filled(l, r, b, top, (40, 32, 26, 240))
        arcade.draw_lrbt_rectangle_outline(l, r, b, top, COLOR_GOLD, 2)
        self.txt_menu_title.x = cx
        self.txt_menu_title.y = top - 36
        self.txt_menu_title.draw()
        # Buttons.
        button_h = 40
        for i, (label, _action) in enumerate(self._menu_actions):
            by_top = top - 80 - i * (button_h + 10)
            by_bot = by_top - button_h
            arcade.draw_lrbt_rectangle_filled(l + 24, r - 24, by_bot, by_top, (60, 50, 40))
            arcade.draw_lrbt_rectangle_outline(l + 24, r - 24, by_bot, by_top, COLOR_GOLD, 1)
            t = self.txt_menu_buttons[i]
            t.x = cx
            t.y = by_bot + button_h / 2 - 6
            t.draw()

    def _menu_button_at(self, x: int, y: int) -> int | None:
        cx = self.width / 2
        cy = self.height / 2
        l, r = cx - MENU_W / 2, cx + MENU_W / 2
        top = cy + MENU_H / 2
        button_h = 40
        for i in range(len(self._menu_actions)):
            by_top = top - 80 - i * (button_h + 10)
            by_bot = by_top - button_h
            if l + 24 <= x <= r - 24 and by_bot <= y <= by_top:
                return i
        return None

    # ── Menu actions ─────────────────────────────────────────────────────
    def _ensure_commercial_roads(self) -> None:
        """v0.50: (re)create the CommercialRoadManager bound to the
        current ``self.trade_manager``. Called after every place that
        rebuilds the trade manager (setup / new-game / map-play /
        editor) so the manager and its routes always share the live
        TradeRouteManager that ``on_update`` ticks.

        v0.51: also (re)create the VoyageManager — the per-TRIP
        inter-city commerce layer — bound to the same economy and the
        freshly-built road manager. It rides the same lifecycle as the
        road manager (a voyage can only run to a *linked* city), so
        rebuilding one rebuilds the other and neither outlives a stale
        economy reference.
        """
        from commercial_roads import CommercialRoadManager
        from voyages import VoyageManager
        self.commercial_roads = CommercialRoadManager(self.trade_manager)
        self.voyage_manager = VoyageManager(self.economy, self.commercial_roads)
        # v0.52: id(voyage) -> the visible TradeShip mirroring it, so the
        # completion hook can turn that ship home / retire it. Rebuilt
        # with the manager; in-flight ships from a previous manager are
        # orphaned (harmless — they just sail their route and berth).
        self._voyage_ships = {}

    def _voyage_world(self):
        """v0.51: build the per-tick ``VoyageWorld`` adapter the
        VoyageManager consumes. The manager is pure logic and never
        touches the map or walkers directly; this thin closure answers
        its four condition questions from live game state:

          * ``ships_available()`` — total trade-ship berths across the
            city's *commercial* harbours (buildings with ship_slots and
            port_role 'commercial') minus the voyages already at sea.
            A harbour shelters idle trade ships; each in-flight voyage
            ties one up, so the free count is capacity − active.
          * ``port_stock(good)`` / ``take_from_port`` — the commercial
            port buffers city goods for export. We model that buffer as
            the global economy pool (the same pool storage.distribute
            allocates), so a voyage exports real stock and the rest of
            the sim sees it leave. Only goods the city *has* can ship.
          * ``no_war_at_destination(city_id)`` — a destination is unsafe
            when an active land/sea invasion is under way. We have no
            per-foreign-city war state, so we use the presence of any
            hostile *enemy* walker as the war proxy: while raiders are
            on the board, foreign markets shut their gates.
          * ``no_piracy_on_route()`` — the sea lane is closed while any
            hostile EnemyShip is afloat (raiders prey on merchantmen —
            see walkers.py sea-raid logic).
        """
        game = self
        from walkers import EnemyShip, Enemy, TradeShip

        class _VoyageWorld:
            def ships_available(self) -> int:
                gm = getattr(game, "game_map", None)
                reg = getattr(game, "registry", None)
                if gm is None or reg is None:
                    return 0
                capacity = 0
                for bid, _r, _c in gm.get_building_positions():
                    bd = reg.get(bid)
                    if bd is None:
                        continue
                    if getattr(bd, "ship_slots", 0) > 0 and \
                            getattr(bd, "port_role", "") == "commercial":
                        capacity += int(bd.ship_slots)
                at_sea = game.voyage_manager.active_count()
                return max(0, capacity - at_sea)

            def port_stock(self, good: str) -> int:
                return int(game.economy.resources.get(good, 0))

            def take_from_port(self, good: str, qty: int) -> int:
                have = int(game.economy.resources.get(good, 0))
                take = max(0, min(int(qty), have))
                if take > 0:
                    game.economy.resources[good] = have - take
                return take

            def no_war_at_destination(self, city_id: str) -> bool:
                wm = getattr(game, "walker_manager", None)
                if wm is None:
                    return True
                for w in wm.walkers:
                    if isinstance(w, Enemy) and not getattr(w, "done", False):
                        return False
                return True

            def no_piracy_on_route(self) -> bool:
                wm = getattr(game, "walker_manager", None)
                if wm is None:
                    return True
                for w in wm.walkers:
                    if isinstance(w, EnemyShip) and not getattr(w, "done", False):
                        return False
                return True

            # ── v0.52: visible-ship hooks ─────────────────────────────
            # Each abstract voyage is mirrored by a real TradeShip walker
            # so the player sees commerce moving on the water. The hooks
            # are best-effort cosmetics: if no harbour water / edge water
            # can be found, the voyage still runs (gold still lands), it
            # just isn't drawn. We map id(voyage) -> ship so the matching
            # ship can be turned home / retired when the trip completes.
            def _commercial_harbour_water(self):
                """A water tile adjacent to a *commercial* harbour — the
                launch/return berth for a voyage ship. None if the city
                has no commercial harbour touching water."""
                gm = getattr(game, "game_map", None)
                reg = getattr(game, "registry", None)
                wm = getattr(game, "walker_manager", None)
                if gm is None or reg is None or wm is None:
                    return None
                for bid, orow, ocol in gm.get_building_positions():
                    bd = reg.get(bid)
                    if bd is None:
                        continue
                    if getattr(bd, "ship_slots", 0) > 0 and \
                            getattr(bd, "port_role", "") == "commercial":
                        tile = wm._adjacent_water_tile(
                            gm, orow, ocol, bd.width, bd.height,
                        )
                        if tile is not None:
                            return tile
                return None

            def _edge_water_exit(self, near):
                """A map-edge water tile to sail the cargo out to — the
                'foreign city is off-map' abstraction. Picks the border
                water tile nearest ``near`` so the route reads sensibly."""
                gm = getattr(game, "game_map", None)
                if gm is None:
                    return None
                from walkers import _impassable_sea
                rows = getattr(gm, "rows", 0)
                cols = getattr(gm, "cols", 0)
                edges = []
                for c in range(cols):
                    if not _impassable_sea(gm, 0, c):
                        edges.append((0, c))
                    if not _impassable_sea(gm, rows - 1, c):
                        edges.append((rows - 1, c))
                for r in range(rows):
                    if not _impassable_sea(gm, r, 0):
                        edges.append((r, 0))
                    if not _impassable_sea(gm, r, cols - 1):
                        edges.append((r, cols - 1))
                if not edges:
                    return None
                nr, nc = near
                return min(edges, key=lambda e: abs(e[0] - nr) + abs(e[1] - nc))

            def on_voyage_launched(self, voyage) -> None:
                wm = getattr(game, "walker_manager", None)
                if wm is None:
                    return
                home = self._commercial_harbour_water()
                if home is None:
                    return  # no berth → run the voyage invisibly
                exit_tile = self._edge_water_exit(home)
                if exit_tile is None:
                    return
                ship = wm.spawn_trade_ship(home, exit_tile, good=voyage.good)
                # Tag the ship so the per-tick ship-arrival logic doesn't
                # try to credit export income (that's the voyage layer's
                # job now) and so we can retire it on completion.
                ship.cargo_qty = int(voyage.qty)
                ship._voyage_ship = True
                game._voyage_ships[id(voyage)] = ship

            def on_voyage_completed(self, record, voyage) -> None:
                ship = game._voyage_ships.pop(id(voyage), None)
                if ship is None:
                    return
                # The trip is settled in the ledger; send the visible
                # ship home and let it berth (or just mark it done if it's
                # already drifting). flip_route turns it back toward home.
                try:
                    ship.cargo_qty = 0
                    if not ship.outbound:
                        ship.done = True  # already heading home → retire
                    else:
                        ship.flip_route()
                except Exception:  # noqa: BLE001
                    ship.done = True

        return _VoyageWorld()

    def _close_other_modal_panels(self, keep: str | None = None) -> None:
        """v0.23: enforce single-panel-at-a-time for the floating
        info panels (jobs / stats / graphs / diagnostics / happiness
        debug / unit editor / barter). When the player taps J the
        jobs panel opens *and* any other panel that was up gets
        closed — the screen always shows at most one of these at
        once.

        ``keep`` is the panel that's about to be (or just was)
        toggled on; pass its short name (``"jobs"``, ``"stats"``,
        etc.) so this helper doesn't close it. Pass ``None`` (or any
        unrecognised value) to close *every* panel — useful from
        Esc-handling paths.

        Why a helper rather than inlining: each panel has its own
        scroll offset, and we want to *reset* the scroll on the
        kept panel each time it opens. Centralising the rule keeps
        every entry-point honest.
        """
        flags = {
            "jobs":            "show_jobs",
            "stats":           "show_stats",
            "graphs":          "show_graphs",
            "diagnostics":     "show_diagnostics",
            "happiness_debug": "show_happiness_debug",
            "unit_editor":     "show_unit_editor",
            "barter":          "show_barter",
            # v0.35: nutrient panel ('N') and gold-trade modal ('C').
            "nutrients":       "show_nutrients_panel",
            "gold_trade":      "show_gold_trade",
            # v0.50: commercial-roads window ('R').
            "commercial":      "show_commercial_roads",
            # v0.51: finance budget panel ('$').
            "finance":         "show_finance_panel",
            # v0.52: commerce-ships panel ('!').
            "commerce_ships":  "show_commerce_ships_panel",
        }
        for panel, attr in flags.items():
            if panel == keep:
                continue
            if getattr(self, attr, False):
                setattr(self, attr, False)

    def _menu_resume(self) -> None:
        self.show_menu = False

    def _menu_save(self) -> None:
        save_game(self)
        self._notify("Game saved!", COLOR_GREEN)
        self.show_menu = False

    def _menu_load(self) -> None:
        # v0.17 fix: previously this notified BEFORE calling load_game,
        # which meant the notification's expiry time used the *current*
        # game_time — but load_game then overwrote game_time with the
        # saved value, which could be many ticks later, instantly
        # expiring the notification. The fix: notify AFTER the load
        # so the expiry uses the loaded game_time.
        #
        # We also surface the loaded population/year so the player gets
        # explicit feedback that a load happened and which save it
        # came from. Just "Game loaded!" without context is too easy
        # to miss.
        ok = load_game(self)
        if ok:
            # load_game called reset_transient_state which cleared
            # notifications; queue ours after that so it survives.
            self._notify(
                f"Game loaded — Year {self.year} M{self.month}, "
                f"pop {self.economy.population}",
                COLOR_GREEN,
                duration=80,
            )
        else:
            # No save file at the default slot. Tell the player what
            # path the engine looked at so they can verify (a common
            # source of confusion: "I clicked save earlier!" — but
            # the cwd had changed, so the file went elsewhere).
            from constants import SAVES_DIR
            self._notify(
                f"No save at {SAVES_DIR}/quicksave.json — press F5 to save",
                COLOR_RED,
                duration=80,
            )
        self.show_menu = False

    def _menu_help(self) -> None:
        self.show_menu = False
        self.show_help = True

    def _menu_quit(self) -> None:
        self.close()


    # ── v0.32: Cutscene runtime trigger ──────────────────────────────
    def fire_cutscene_for_flag(self, flag: str) -> bool:
        """If any cutscene's ``trigger_flag`` matches ``flag``, start
        playing it. Called by the runtime (RPG request dispatcher,
        scheduled events, etc.) when a flag flips.

        Returns ``True`` if a cutscene was fired (the caller may want
        to pause the simulation), ``False`` if no cutscene matched.

        The library is lazily loaded on first call and reloaded any
        time the editor saves (the editor itself clears
        ``_cutscenes_library`` on save by calling
        ``invalidate_cutscenes_cache``). We don't reload every frame
        because a cutscene's library could grow to hundreds of
        entries.
        """
        if not flag:
            return False
        # v0.36: forward to the trigger manager so on_flag triggers
        # fire next tick. We do this BEFORE the cutscene lookup so
        # that even a flag with no matching cutscene still wires the
        # trigger graph.
        if getattr(self, "trigger_manager", None) is not None:
            self.trigger_manager.on_flag_set(flag)
        from cutscenes import find_cutscene_for_flag
        cs = find_cutscene_for_flag(self._effective_cutscenes(), flag)
        if cs is None:
            return False
        import cutscene_player
        if self.cutscene_player_state is None:
            self.cutscene_player_state = cutscene_player.CutscenePlayerState()
        cutscene_player.start(self.cutscene_player_state, cs)
        # Pause the simulation while the cutscene plays — same UX as
        # the RPG request modal: the world freezes while the player
        # reads. The player advancing past the last slide resumes via
        # the click handler below.
        self._cutscene_was_paused = self.paused
        self.paused = True
        log.info("Cutscene fired: %s (trigger flag %s)",
                 cs.get("id"), flag)
        return True

    def _effective_cutscenes(self) -> list[dict]:
        """The cutscene library used for all runtime lookups: the on-disk
        ``data/cutscenes.json`` library merged with the cutscenes the
        currently-loaded scenario embeds in its ``scenario_libraries``
        block.

        Before v0.40 the runtime only consulted the on-disk library, so a
        scenario that shipped its cinematics inside its own ``map.json``
        (``scenario_libraries.cutscenes``) — as ``mare_nostrum`` does for
        its whole intro chain — would have every ``play_cutscene`` trigger
        silently miss ("cutscene not in library"). Merging the scenario's
        embedded library here makes those triggers resolve.

        Disk entries win on an id collision (a saved/edited cutscene
        overrides a scenario default of the same id). The on-disk half is
        still cached in ``_cutscenes_library`` and invalidated by the
        editor via ``invalidate_cutscenes_cache``; the scenario half is
        read fresh each call (it's tiny and changes only on map load).
        """
        if self._cutscenes_library is None:
            from cutscenes import load_cutscenes
            try:
                self._cutscenes_library = load_cutscenes()
            except Exception:  # noqa: BLE001
                log.exception("_effective_cutscenes: load_cutscenes failed")
                self._cutscenes_library = []
        merged: list[dict] = list(self._cutscenes_library)
        seen = {c.get("id") for c in merged if c.get("id")}
        scenario_cs = (self.scenario_libraries or {}).get("cutscenes") or []
        if isinstance(scenario_cs, list):
            for cs in scenario_cs:
                if isinstance(cs, dict) and cs.get("id") not in seen:
                    merged.append(cs)
                    seen.add(cs.get("id"))
        return merged

    def invalidate_cutscenes_cache(self) -> None:
        """Drop the cached cutscene library so the next fire reloads
        from disk. Called by the cutscene editor after saving."""
        self._cutscenes_library = None

    # ── v0.36: by-id callbacks for the Event editor's triggers ────────
    # The TriggerManager calls these for `play_cutscene` / `fire_request`
    # do-effects. Distinct from fire_cutscene_for_flag (which looks up
    # by trigger_flag) because trigger entries reference cutscenes /
    # requests directly by id — the wiring layer should not depend on
    # the cutscene's own auto-fire flag.

    def _dispatch_naval_raid(self, count: int, hp: int, damage: int) -> None:
        """Trigger callback (v0.46): spawn a wave of enemy ships. Routes
        to the walker manager, which finds map-edge water and a coastal
        objective. Safe no-op if there's no map/manager yet."""
        if getattr(self, "walker_manager", None) is None:
            return
        try:
            self.walker_manager.spawn_naval_raid(
                self.game_map, count=count, hp=hp, damage=damage,
            )
        except Exception:  # noqa: BLE001
            log.exception("naval raid dispatch failed")

    def _cmd_commit_selection(self, modifiers: int) -> None:
        """v0.48: turn the just-finished left drag/click into a selection.

        A near-zero drag = a single click → select the one player unit on
        the clicked tile (if any). A real drag → box-select every player
        unit whose tile falls in the rectangle. Shift held → add/toggle
        into the existing selection instead of replacing it."""
        if self._cmd_drag_start is None:
            return
        sx, sy = self._cmd_drag_start
        ex, ey = self._cmd_drag_now if self._cmd_drag_now else (sx, sy)
        shift = bool(modifiers & arcade.key.MOD_SHIFT)
        is_box = (abs(ex - sx) + abs(ey - sy)) > 12.0  # world-px threshold
        gm = self.game_map
        if is_box:
            r0, c0 = gm.world_to_grid_instance(sx, sy)
            r1, c1 = gm.world_to_grid_instance(ex, ey)
            if None in (r0, c0, r1, c1):
                return
            units = self.walker_manager.player_units_in_rect(r0, c0, r1, c1)
            if shift:
                for u in units:
                    self.command_manager.add_to_selection(u)
            else:
                self.command_manager.select_box(units)
            log.info("Command select-box → %d unit(s)", len(units))
        else:
            r, c = gm.world_to_grid_instance(sx, sy)
            if r is None or c is None:
                return
            hits = self.walker_manager.player_units_in_rect(r, c, r, c)
            if shift and hits:
                self.command_manager.toggle_in_selection(hits[0])
            elif hits:
                self.command_manager.select_one(hits[0])
            elif not shift:
                self.command_manager.clear_selection()
            log.info("Command select-click at (%d,%d) → %d unit(s)",
                     r, c, self.command_manager.selection.count())

    def _cmd_issue_order_at(self, row: int, col: int) -> None:
        """v0.48: right-click in command mode issues an order to the
        selection at (row, col). Enemy on the tile → ATTACK; else → MOVE.
        Reports partial obedience via a notification."""
        from commands import Verb
        if self.command_manager.selection.is_empty():
            return
        from walkers import Enemy
        target = None
        for w in self.walker_manager.walkers:
            if getattr(w, "done", False):
                continue
            if isinstance(w, Enemy) and (w.row, w.col) == (row, col):
                target = w
                break
        if target is not None:
            accepted = self.command_manager.issue_verb(
                Verb.ATTACK, tile=(row, col), target_id=id(target),
            )
            verb_name = "attack"
        else:
            accepted = self.command_manager.issue_verb(Verb.MOVE, tile=(row, col))
            verb_name = "move"
        total = self.command_manager.selection.count()
        log.info("Command %s at (%d,%d): %d/%d obeyed",
                 verb_name, row, col, len(accepted), total)
        if len(accepted) < total:
            self._notify(
                f"{len(accepted)}/{total} units can {verb_name} here.",
                COLOR_GOLD,
            )

    def fire_cutscene_for_flag_id(self, cid: str) -> bool:
        """Play the cutscene with the given ``id`` (not trigger_flag).
        Used by the TriggerManager's ``play_cutscene`` do-effect.

        Returns True if the cutscene was found and started, False
        otherwise. Lazy-loads the library on first call same as
        fire_cutscene_for_flag.
        """
        if not cid:
            return False
        library = self._effective_cutscenes()
        cs = next(
            (c for c in library if c.get("id") == cid),
            None,
        )
        if cs is None:
            log.warning(
                "fire_cutscene_for_flag_id: cutscene %r not in library", cid,
            )
            return False
        import cutscene_player
        if self.cutscene_player_state is None:
            self.cutscene_player_state = cutscene_player.CutscenePlayerState()
        cutscene_player.start(self.cutscene_player_state, cs)
        self._cutscene_was_paused = self.paused
        self.paused = True
        log.info("Cutscene fired (by id): %s", cid)
        return True

    def fire_request_for_id(self, rid: str) -> bool:
        """Push the RPG request with the given ``id`` onto the queue.
        Used by the TriggerManager's ``fire_request`` do-effect.

        Returns True if the request was found and queued, False
        otherwise. Stub-compatible — when the RPG request dispatcher
        ships (planned alongside this), it consumes
        ``self._pending_rpg_requests`` and pops them off in order.
        """
        if not rid:
            return False
        # Lazy-load the request library the same way the cutscene
        # path does. The attribute is seeded to None in __init__.
        if self._rpg_requests_library is None:
            from rpg_requests import load_requests
            try:
                self._rpg_requests_library = load_requests()
            except Exception:  # noqa: BLE001
                log.exception("fire_request_for_id: load_requests failed")
                self._rpg_requests_library = []
        req = next(
            (r for r in self._rpg_requests_library if r.get("id") == rid),
            None,
        )
        if req is None:
            log.warning(
                "fire_request_for_id: request %r not in library", rid,
            )
            return False
        self._pending_rpg_requests.append(req)
        log.info("RPG request queued (by id): %s", rid)
        return True

    # ── v0.38 (audit 7.1): RPG request runtime presenter ─────────────
    def _drain_pending_rpg_requests(self) -> None:
        """If no request panel is up and one is queued, pop and present
        it. Called once per frame from ``on_update`` (before the pause
        gate, so a freshly-fired request opens even if the sim is
        otherwise idle). Pausing mirrors the cutscene player: the world
        freezes while the player decides, and the pre-panel paused state
        is restored when a decision closes it.
        """
        if not self._pending_rpg_requests:
            return
        # Don't stack panels — one request at a time.
        if self.rpg_player_state is not None and self.rpg_player_state.active:
            return
        import rpg_player
        if self.rpg_player_state is None:
            self.rpg_player_state = rpg_player.RpgPlayerState()
        req = self._pending_rpg_requests.pop(0)
        if rpg_player.start(self.rpg_player_state, req):
            self._rpg_was_paused = self.paused
            self.paused = True
            log.info("RPG request presented: %s", req.get("id"))

    def apply_rpg_decision(self, decision: dict) -> None:
        """Apply a chosen decision's outcome to the simulation.

        Immediate effects (``delay_ticks`` == 0) go straight to the
        economy via ``apply_event_effects`` (which also moves population
        / happiness). A non-zero ``delay_ticks`` parks the resource
        effects on ``_rpg_delayed_effects`` to fire later (the "pay next
        month" case) — happiness/diplomacy still apply now, since those
        are the player's immediate reaction, not the deferred payment.
        ``set_flag`` routes through ``fire_cutscene_for_flag`` (which
        also notifies the trigger graph), and ``fire_event`` through the
        event manager — reusing the exact paths the cutscene/trigger
        systems already use.
        """
        if not decision:
            return
        effects = dict(decision.get("effects") or {})
        delay = int(decision.get("delay_ticks") or 0)
        happy = int(decision.get("happy") or 0)
        dip = int(decision.get("diplomacy") or 0)

        if delay > 0:
            # Defer the resource bundle; apply the social reaction now.
            self._rpg_delayed_effects.append(
                (self.game_time + delay, effects, 0, 0),
            )
            if happy:
                self.economy.apply_event_effects({}, 0, happy)
            log.info("RPG decision: deferred %r by %d ticks", effects, delay)
        else:
            self.economy.apply_event_effects(effects, 0, happy)

        if dip:
            self.diplomacy.add(float(dip))

        flag = str(decision.get("set_flag") or "").strip()
        if flag:
            # Routes through the trigger graph + any matching cutscene.
            self.fire_cutscene_for_flag(flag)

        ev_name = decision.get("fire_event")
        if ev_name and getattr(self, "event_manager", None) is not None:
            ev = next(
                (e for e in getattr(self.event_manager, "events", [])
                 if e.get("name") == ev_name),
                None,
            )
            if ev is not None:
                self.event_manager._trigger(self.economy, event=ev)
            else:
                log.warning("RPG decision: fire_event %r not in registry",
                            ev_name)

    def _process_rpg_delayed_effects(self) -> None:
        """Apply any delayed RPG effects whose fire-tick has arrived.
        Called once per ``_game_tick``. Keeps the queue small by
        partitioning into due / not-yet each tick."""
        if not self._rpg_delayed_effects:
            return
        still: list[tuple[int, dict, int, int]] = []
        for fire_at, effects, pop, happy in self._rpg_delayed_effects:
            if self.game_time >= fire_at:
                self.economy.apply_event_effects(effects, pop, happy)
                log.info("RPG delayed effect applied at tick %d: %r",
                         self.game_time, effects)
            else:
                still.append((fire_at, effects, pop, happy))
        self._rpg_delayed_effects = still

    # ── v0.28: Trigger editor — layout constants ─────────────────────
    TRIGGER_EDITOR_PANEL_W = 920
    TRIGGER_EDITOR_PANEL_H = 720
    TRIGGER_EDITOR_LIST_W = 200
    TRIGGER_EDITOR_ROW_H = 22
    TRIGGER_EDITOR_MSG_MAX = 80
    TRIGGER_EDITOR_NAME_MAX = 32
    TRIGGER_EDITOR_STEP_MINOR = 10
    TRIGGER_EDITOR_STEP_MAJOR = 50

    # Resource id menu for the effect-picker overlay. Limited to the
    # economy's known resource set plus "money" (which the apply
    # path handles specially). New resources added later can show up
    # here by extending the list — unknown effects are silently
    # ignored at apply time, so this list is purely UX.
    TRIGGER_EDITOR_EFFECT_KEYS: list[str] = [
        "money", "food", "wood", "stone", "iron", "iron_ore",
        "weapons", "tools", "wheat", "flour", "planks",
        "stone_blocks", "bread",
    ]
    # Modifier keys the engine currently understands. v0.28 ships
    # water_factor (drought), wood_consumption_factor (blizzard), and
    # pop_kill_when_resource_zero (blizzard). The "factor" suffix is
    # numeric; pop_kill is a list. The picker writes the right shape
    # depending on which key the author picks.
    TRIGGER_EDITOR_MODIFIER_KEYS: list[str] = [
        "water_factor", "wood_consumption_factor",
        "pop_kill_when_resource_zero",
    ]

    def _draw_trigger_editor(self) -> None:
        """Render the trigger editor modal — v0.28.

        Layout:
          * Left column:   scrollable list of events.
          * Middle column: per-event scalar rows + msg text input.
          * Right column:  colour picker (R/G/B sliders) + effects
                           pane + modifiers pane.
          * Footer:        Add event · Remove event · Cancel · Save.

        Hit rects are rebuilt each frame into
        ``_trigger_editor_btn_rects`` and consumed by
        ``_trigger_editor_handle_click`` on the next click.
        """
        self._trigger_editor_btn_rects = []
        # Dim background.
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 170),
        )
        cx = self.width / 2
        cy = self.height / 2
        pw = self.TRIGGER_EDITOR_PANEL_W
        ph = self.TRIGGER_EDITOR_PANEL_H
        l = cx - pw / 2
        r = cx + pw / 2
        b = cy - ph / 2
        t = cy + ph / 2
        # Clamp to screen (same pattern as buildings editor).
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

        # Lazy text-pool.
        if not hasattr(self, "_txt_trigger_editor"):
            self._txt_trigger_editor: dict[str, arcade.Text] = {}

        def te_text(key: str, x: float, y: float, size: int = 12,
                    color=COLOR_WHITE, bold: bool = False,
                    anchor_x: str = "left") -> arcade.Text:
            t_obj = self._txt_trigger_editor.get(key)
            if t_obj is None:
                t_obj = arcade.Text("", x, y, color, size,
                                    bold=bold, anchor_x=anchor_x)
                self._txt_trigger_editor[key] = t_obj
            t_obj.x = x
            t_obj.y = y
            t_obj.color = color
            return t_obj

        title = te_text("title", cx, t - 26, 14, COLOR_GOLD, True,
                        anchor_x="center")
        title.text = "EVENT EDITOR"
        title.draw()
        hint = te_text("hint", cx, t - 44, 10, COLOR_GRAY, anchor_x="center")
        hint.text = (
            "Library of events: name, banner colour, effects, timed "
            "modifiers. Triggers editor decides WHEN they fire."
        )
        hint.draw()

        # ── Left column: event list ───────────────────────────────────
        list_l = l + 14
        list_r = list_l + self.TRIGGER_EDITOR_LIST_W
        list_t = t - 64
        list_b = b + 60
        arcade.draw_lrbt_rectangle_filled(
            list_l, list_r, list_b, list_t, (22, 18, 14, 230),
        )
        arcade.draw_lrbt_rectangle_outline(
            list_l, list_r, list_b, list_t, COLOR_UI_BORDER, 1,
        )
        rows_visible = int((list_t - list_b) / self.TRIGGER_EDITOR_ROW_H)
        events = self.trigger_editor_events
        max_scroll = max(0, len(events) - rows_visible)
        self.trigger_editor_scroll = max(
            0, min(self.trigger_editor_scroll, max_scroll)
        )
        # Scroll arrows (right of the list).
        scroll_btn_w = 20
        scroll_x1 = list_r + 2
        scroll_x2 = scroll_x1 + scroll_btn_w
        for action, ay_b, ay_t, lbl_txt in (
            ("scroll_up", list_t - 22, list_t, "^"),
            ("scroll_down", list_b, list_b + 22, "v"),
        ):
            arcade.draw_lrbt_rectangle_filled(
                scroll_x1, scroll_x2, ay_b, ay_t, (60, 50, 40),
            )
            arcade.draw_lrbt_rectangle_outline(
                scroll_x1, scroll_x2, ay_b, ay_t, COLOR_UI_BORDER, 1,
            )
            tt = te_text(
                f"scroll_{action}", (scroll_x1 + scroll_x2) / 2,
                ay_b + 4, 12, COLOR_WHITE, True, anchor_x="center",
            )
            tt.text = lbl_txt
            tt.draw()
            self._trigger_editor_btn_rects.append(
                (scroll_x1, scroll_x2, ay_b, ay_t, action)
            )
        # Row rendering.
        for vi in range(rows_visible):
            idx = self.trigger_editor_scroll + vi
            if idx >= len(events):
                break
            ev = events[idx]
            row_top = list_t - vi * self.TRIGGER_EDITOR_ROW_H
            row_bot = row_top - self.TRIGGER_EDITOR_ROW_H
            selected = (idx == self.trigger_editor_selected_idx)
            if selected:
                arcade.draw_lrbt_rectangle_filled(
                    list_l, list_r, row_bot, row_top, (90, 70, 50, 220),
                )
            # Small colour swatch on the left of the row label.
            col = ev.get("color") or [200, 200, 200]
            r_c, g_c, b_c = int(col[0]), int(col[1]), int(col[2])
            arcade.draw_lrbt_rectangle_filled(
                list_l + 4, list_l + 14, row_bot + 5, row_bot + 15,
                (r_c, g_c, b_c),
            )
            arcade.draw_lrbt_rectangle_outline(
                list_l + 4, list_l + 14, row_bot + 5, row_bot + 15,
                COLOR_UI_BORDER, 1,
            )
            row_txt = te_text(
                f"row_{vi}", list_l + 20, row_bot + 5, 11,
                COLOR_GOLD if selected else COLOR_WHITE,
            )
            name = str(ev.get("name", "(unnamed)"))
            row_txt.text = name if len(name) <= 22 else name[:21] + "…"
            row_txt.draw()
            self._trigger_editor_btn_rects.append(
                (list_l, list_r, row_bot, row_top, f"select:{idx}")
            )

        # Footer for the list: Add / Remove.
        add_l = list_l
        add_r = list_l + (self.TRIGGER_EDITOR_LIST_W // 2) - 4
        rem_l = add_r + 8
        rem_r = list_r
        ay_t = list_b - 4
        ay_b = ay_t - 24
        for x1, x2, action, lbl_txt, fill in (
            (add_l, add_r, "add_event",    "+ Add",    (50, 80, 50)),
            (rem_l, rem_r, "remove_event", "— Remove", (80, 50, 40)),
        ):
            arcade.draw_lrbt_rectangle_filled(x1, x2, ay_b, ay_t, fill)
            arcade.draw_lrbt_rectangle_outline(
                x1, x2, ay_b, ay_t, COLOR_UI_BORDER, 1,
            )
            tt = te_text(
                f"foot_{action}", (x1 + x2) / 2, ay_b + 6, 11,
                COLOR_WHITE, True, anchor_x="center",
            )
            tt.text = lbl_txt
            tt.draw()
            self._trigger_editor_btn_rects.append(
                (x1, x2, ay_b, ay_t, action)
            )

        # ── Right pane: form ──────────────────────────────────────────
        if (
            self.trigger_editor_selected_idx < 0
            or self.trigger_editor_selected_idx >= len(events)
        ):
            self._draw_trigger_editor_footer(l, r, b, te_text)
            return
        ev = events[self.trigger_editor_selected_idx]
        form_l = list_r + 30
        form_r = r - 14
        form_t = list_t
        form_b = list_b

        # Event name editable text box.
        name_lbl = te_text("name_lbl", form_l, form_t - 24, 12, COLOR_GOLD, True)
        name_lbl.text = "Event name:"
        name_lbl.draw()
        editing_name = self.trigger_editor_text_field == "name"
        nb_l = form_l + 100
        nb_r = nb_l + 280
        nb_t = form_t - 14
        nb_b = nb_t - 22
        arcade.draw_lrbt_rectangle_filled(nb_l, nb_r, nb_b, nb_t, (22, 18, 14))
        arcade.draw_lrbt_rectangle_outline(
            nb_l, nb_r, nb_b, nb_t,
            COLOR_GOLD if editing_name else COLOR_UI_BORDER,
            2 if editing_name else 1,
        )
        nb_txt = te_text(
            "name_val", nb_l + 6, nb_b + 5, 11,
            COLOR_GOLD if editing_name else COLOR_WHITE,
        )
        if editing_name:
            nb_txt.text = (self._trigger_editor_text_buffer or "") + "_"
        else:
            nb_txt.text = str(ev.get("name", ""))
        nb_txt.draw()
        self._trigger_editor_btn_rects.append(
            (nb_l, nb_r, nb_b, nb_t, "edit_name")
        )

        # Scalar rows: pop, happy, duration_ticks.
        scalar_fields = [
            ("pop", "Population delta"),
            ("happy", "Happiness delta"),
            ("duration_ticks", "Duration (ticks)"),
        ]
        major = self.TRIGGER_EDITOR_STEP_MAJOR
        minor = self.TRIGGER_EDITOR_STEP_MINOR
        field_y = form_t - 60
        row_stride = 30
        for fi, (field, label) in enumerate(scalar_fields):
            row_y = field_y - fi * row_stride
            value = int(ev.get(field, 0))
            mb_b = row_y - 22
            mb_t = mb_b + 20
            lbl = te_text(f"lbl_{field}", form_l, mb_b + 5, 11,
                          COLOR_WHITE, True)
            lbl.text = label
            lbl.draw()
            cursor = form_l + 160
            # For pop and happy, allow negative values (the engine
            # accepts them — Famine has pop=-8). For duration_ticks,
            # clamp at 0 (negative duration is meaningless).
            allow_neg = field in ("pop", "happy")
            for width, btn_lbl, action_kind in (
                (36, f"-{major}", "minus_major"),
                (30, f"-{minor}", "minus_minor"),
                (60, str(value),  "value"),
                (30, f"+{minor}", "plus_minor"),
                (36, f"+{major}", "plus_major"),
            ):
                left = cursor
                right = left + width
                if action_kind == "value":
                    fill = (22, 18, 14)
                    text_color = COLOR_GOLD
                else:
                    fill = (60, 45, 35)
                    text_color = COLOR_WHITE
                arcade.draw_lrbt_rectangle_filled(left, right, mb_b, mb_t, fill)
                arcade.draw_lrbt_rectangle_outline(
                    left, right, mb_b, mb_t, COLOR_UI_BORDER, 1,
                )
                bt = te_text(
                    f"{action_kind}_{field}", (left + right) / 2, mb_b + 3,
                    12, text_color, True, anchor_x="center",
                )
                bt.text = btn_lbl
                bt.draw()
                if action_kind != "value":
                    # action format: "{action_kind}:{field}:{allow_neg}"
                    self._trigger_editor_btn_rects.append(
                        (left, right, mb_b, mb_t,
                         f"{action_kind}:{field}:{int(allow_neg)}")
                    )
                cursor = right + 4

        # msg text input — sits below the scalar rows.
        msg_y = field_y - len(scalar_fields) * row_stride - 20
        msg_lbl = te_text("msg_lbl", form_l, msg_y, 11, COLOR_WHITE, True)
        msg_lbl.text = "Banner message:"
        msg_lbl.draw()
        editing_msg = self.trigger_editor_text_field == "msg"
        mb_l = form_l
        mb_r = form_r - 10
        mb_t = msg_y - 8
        mb_b = mb_t - 22
        arcade.draw_lrbt_rectangle_filled(mb_l, mb_r, mb_b, mb_t, (22, 18, 14))
        arcade.draw_lrbt_rectangle_outline(
            mb_l, mb_r, mb_b, mb_t,
            COLOR_GOLD if editing_msg else COLOR_UI_BORDER,
            2 if editing_msg else 1,
        )
        mb_txt = te_text(
            "msg_val", mb_l + 6, mb_b + 5, 11,
            COLOR_GOLD if editing_msg else COLOR_WHITE,
        )
        if editing_msg:
            mb_txt.text = (self._trigger_editor_text_buffer or "") + "_"
        else:
            full = str(ev.get("msg", ""))
            mb_txt.text = full if len(full) <= 90 else full[:89] + "…"
        mb_txt.draw()
        self._trigger_editor_btn_rects.append(
            (mb_l, mb_r, mb_b, mb_t, "edit_msg")
        )

        # ── Colour picker (R / G / B sliders) ────────────────────────
        # Three rows of -10 / -1 / val / +1 / +10 buttons.
        color_y = mb_b - 24
        color_lbl = te_text("color_lbl", form_l, color_y, 11, COLOR_WHITE, True)
        color_lbl.text = "Banner colour (R / G / B 0..255):"
        color_lbl.draw()
        col = ev.get("color") or [200, 200, 200]
        # Make sure it's a 3-list of ints; user-added events seed
        # this with a default in _open_trigger_editor.
        if len(col) < 3:
            col = list(col) + [0] * (3 - len(col))
            ev["color"] = col
        # Swatch.
        sw_l = form_r - 64
        sw_r = sw_l + 50
        sw_t = color_y + 4
        sw_b = sw_t - 26
        arcade.draw_lrbt_rectangle_filled(
            sw_l, sw_r, sw_b, sw_t,
            (int(col[0]), int(col[1]), int(col[2])),
        )
        arcade.draw_lrbt_rectangle_outline(
            sw_l, sw_r, sw_b, sw_t, COLOR_UI_BORDER, 1,
        )
        for ci, (ch_lbl, _idx) in enumerate(
            (("R", 0), ("G", 1), ("B", 2)),
        ):
            r_y = color_y - 22 - ci * 26
            rb_b = r_y - 18
            rb_t = rb_b + 20
            c_lbl = te_text(f"color_ch_{ci}", form_l, rb_b + 4, 11, COLOR_GRAY)
            c_lbl.text = ch_lbl
            c_lbl.draw()
            cur_val = int(col[_idx])
            cursor = form_l + 30
            for width, btn_lbl, action_kind in (
                (32, "-10", "color_minus_major"),
                (28, "-1",  "color_minus_minor"),
                (44, str(cur_val), "color_value"),
                (28, "+1",  "color_plus_minor"),
                (32, "+10", "color_plus_major"),
            ):
                left = cursor
                right = left + width
                if action_kind == "color_value":
                    fill = (22, 18, 14)
                    text_color = COLOR_GOLD
                else:
                    fill = (60, 45, 35)
                    text_color = COLOR_WHITE
                arcade.draw_lrbt_rectangle_filled(left, right, rb_b, rb_t, fill)
                arcade.draw_lrbt_rectangle_outline(
                    left, right, rb_b, rb_t, COLOR_UI_BORDER, 1,
                )
                bt = te_text(
                    f"{action_kind}_{_idx}", (left + right) / 2, rb_b + 3,
                    11, text_color, True, anchor_x="center",
                )
                bt.text = btn_lbl
                bt.draw()
                if action_kind != "color_value":
                    self._trigger_editor_btn_rects.append(
                        (left, right, rb_b, rb_t, f"{action_kind}:{_idx}")
                    )
                cursor = right + 3

        # ── Effects + Modifiers panes (side by side) ──────────────────
        panes_top = color_y - 22 - 3 * 26 - 14
        pane_w = (form_r - form_l - 16) // 2
        pane_h = panes_top - form_b - 4
        # Cap pane height for readability.
        pane_h = max(80, min(180, pane_h))
        eff_l = form_l
        eff_r = eff_l + pane_w
        mod_l = eff_r + 16
        mod_r = mod_l + pane_w
        pane_bot = panes_top - pane_h
        self._trigger_editor_draw_kv_pane(
            "Effects (resource deltas)", ev["effects"],
            eff_l, eff_r, panes_top, pane_bot, "effect",
            COLOR_GREEN, te_text, allow_negative=True,
        )
        self._trigger_editor_draw_kv_pane(
            "Modifiers (timed)", ev["modifiers"],
            mod_l, mod_r, panes_top, pane_bot, "modifier",
            (180, 140, 80), te_text, allow_negative=False, is_modifier=True,
        )

        # Footer.
        self._draw_trigger_editor_footer(l, r, b, te_text)

        # Picker overlay (drawn last so it sits on top).
        if self.trigger_editor_picker_open:
            self._trigger_editor_draw_picker(te_text)

    def _draw_trigger_editor_footer(self, l, r, b, te_text) -> None:
        """Draw the Cancel / Save footer on the trigger editor."""
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
        cancel_txt = te_text(
            "btn_cancel", (cancel_x_l + cancel_x_r) / 2, btn_y_bot + 8,
            12, COLOR_WHITE, True, anchor_x="center",
        )
        cancel_txt.text = "Cancel"
        cancel_txt.draw()
        self._trigger_editor_btn_rects.append(
            (cancel_x_l, cancel_x_r, btn_y_bot, btn_y_top, "cancel")
        )
        save_fill = (90, 70, 30) if self.trigger_editor_dirty else (50, 40, 35)
        arcade.draw_lrbt_rectangle_filled(
            save_x_l, save_x_r, btn_y_bot, btn_y_top, save_fill,
        )
        arcade.draw_lrbt_rectangle_outline(
            save_x_l, save_x_r, btn_y_bot, btn_y_top,
            COLOR_GOLD if self.trigger_editor_dirty else COLOR_UI_BORDER,
            2 if self.trigger_editor_dirty else 1,
        )
        save_txt = te_text(
            "btn_save", (save_x_l + save_x_r) / 2, btn_y_bot + 8,
            12, COLOR_GOLD if self.trigger_editor_dirty else COLOR_WHITE,
            True, anchor_x="center",
        )
        save_txt.text = "Save" + (" *" if self.trigger_editor_dirty else "")
        save_txt.draw()
        self._trigger_editor_btn_rects.append(
            (save_x_l, save_x_r, btn_y_bot, btn_y_top, "save")
        )

    def _trigger_editor_draw_kv_pane(
        self, title: str, items: dict, l: float, r: float,
        top_y: float, bot_y: float, action_prefix: str,
        accent_color, te_text,
        allow_negative: bool = False, is_modifier: bool = False,
    ) -> None:
        """Draw an Effects or Modifiers pane: header + one row per
        key/value + an Add button.

        Each row has -10 / -1 / value / +1 / +10 / × on a single line.
        Modifiers may carry non-numeric values (e.g. the blizzard
        ``pop_kill_when_resource_zero`` list) — those rows render the
        value as a short stringified preview with no +/- controls,
        just a remove button.
        """
        arcade.draw_lrbt_rectangle_filled(l, r, bot_y, top_y, (28, 22, 18))
        arcade.draw_lrbt_rectangle_outline(l, r, bot_y, top_y, COLOR_UI_BORDER, 1)
        hdr = te_text(
            f"{action_prefix}_hdr", (l + r) / 2, top_y - 18,
            11, accent_color, True, anchor_x="center",
        )
        hdr.text = title
        hdr.draw()
        row_y = top_y - 38
        row_h = 22
        keys = sorted(items.keys())
        for rid in keys:
            qty = items[rid]
            if row_y - row_h < bot_y + 28:
                break
            row_b = row_y - 18
            row_t = row_y
            lbl_text = rid if len(rid) <= 14 else rid[:13] + "…"
            lbl = te_text(
                f"{action_prefix}_lbl_{rid}", l + 6, row_b + 3,
                10, COLOR_WHITE,
            )
            lbl.text = lbl_text
            lbl.draw()
            cursor = l + 110
            # Non-numeric (list/dict/str) values: show repr, only allow
            # remove. Numeric values: full -10/-1/val/+1/+10 + ×.
            if isinstance(qty, (int, float)) and not isinstance(qty, bool):
                num = qty
                for width, btn_lbl, action_kind, fill, txt_col in (
                    (24, "-10", "minus_major", (60, 45, 35), COLOR_WHITE),
                    (22, "-1",  "minus_minor", (60, 45, 35), COLOR_WHITE),
                    (38, str(int(num)) if num == int(num) else f"{num:g}",
                     "val", (22, 18, 14), COLOR_GOLD),
                    (22, "+1",  "plus_minor", (60, 45, 35), COLOR_WHITE),
                    (24, "+10", "plus_major", (60, 45, 35), COLOR_WHITE),
                    (20, "x",   "remove",     (90, 40, 35), COLOR_WHITE),
                ):
                    left = cursor
                    right = left + width
                    arcade.draw_lrbt_rectangle_filled(
                        left, right, row_b, row_t, fill,
                    )
                    arcade.draw_lrbt_rectangle_outline(
                        left, right, row_b, row_t, COLOR_UI_BORDER, 1,
                    )
                    bt = te_text(
                        f"{action_prefix}_{action_kind}_{rid}",
                        (left + right) / 2, row_b + 3,
                        10, txt_col, True, anchor_x="center",
                    )
                    bt.text = btn_lbl
                    bt.draw()
                    if action_kind != "val":
                        self._trigger_editor_btn_rects.append(
                            (left, right, row_b, row_t,
                             f"{action_prefix}_{action_kind}:{rid}")
                        )
                    cursor = right + 3
            else:
                # Non-numeric modifier — preview + remove only.
                preview = repr(qty)
                if len(preview) > 24:
                    preview = preview[:23] + "…"
                pv = te_text(
                    f"{action_prefix}_preview_{rid}", cursor, row_b + 3,
                    10, COLOR_GRAY,
                )
                pv.text = preview
                pv.draw()
                # Remove button on the right.
                rm_r = r - 6
                rm_l = rm_r - 20
                arcade.draw_lrbt_rectangle_filled(
                    rm_l, rm_r, row_b, row_t, (90, 40, 35),
                )
                arcade.draw_lrbt_rectangle_outline(
                    rm_l, rm_r, row_b, row_t, COLOR_UI_BORDER, 1,
                )
                bt = te_text(
                    f"{action_prefix}_rm_nonnum_{rid}",
                    (rm_l + rm_r) / 2, row_b + 3,
                    10, COLOR_WHITE, True, anchor_x="center",
                )
                bt.text = "x"
                bt.draw()
                self._trigger_editor_btn_rects.append(
                    (rm_l, rm_r, row_b, row_t,
                     f"{action_prefix}_remove:{rid}")
                )
            row_y -= row_h
        # Add button at the bottom of the pane.
        ab_l = l + 8
        ab_r = r - 8
        ab_t = bot_y + 26
        ab_b = bot_y + 6
        arcade.draw_lrbt_rectangle_filled(ab_l, ab_r, ab_b, ab_t, (50, 70, 50))
        arcade.draw_lrbt_rectangle_outline(
            ab_l, ab_r, ab_b, ab_t, COLOR_UI_BORDER, 1,
        )
        bt = te_text(
            f"{action_prefix}_add_btn", (ab_l + ab_r) / 2, ab_b + 6,
            11, COLOR_WHITE, True, anchor_x="center",
        )
        bt.text = "+ Add…"
        bt.draw()
        self._trigger_editor_btn_rects.append(
            (ab_l, ab_r, ab_b, ab_t, f"{action_prefix}_add")
        )

    def _trigger_editor_draw_picker(self, te_text) -> None:
        """Modal overlay for adding an effect or modifier key. The
        picker is a centered list of valid keys; click a key → add
        it to the currently-selected event with a sensible default
        value; click outside → dismiss.
        """
        kind = self.trigger_editor_picker_open
        if kind == "effect":
            keys = self.TRIGGER_EDITOR_EFFECT_KEYS
            title = "Pick an effect resource"
        else:
            keys = self.TRIGGER_EDITOR_MODIFIER_KEYS
            title = "Pick a modifier key"
        pw = 320
        ph = 40 + 24 * len(keys) + 40
        cx = self.width / 2
        cy = self.height / 2
        l = cx - pw / 2
        r = cx + pw / 2
        b = cy - ph / 2
        t = cy + ph / 2
        # Dim background and draw modal.
        arcade.draw_lrbt_rectangle_filled(0, self.width, 0, self.height, (0, 0, 0, 150))
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 250))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)
        ht = te_text("picker_title", cx, t - 26, 13, COLOR_GOLD, True,
                     anchor_x="center")
        ht.text = title
        ht.draw()
        row_top = t - 50
        for i, key in enumerate(keys):
            ry_t = row_top - i * 24
            ry_b = ry_t - 22
            arcade.draw_lrbt_rectangle_filled(
                l + 8, r - 8, ry_b, ry_t, (50, 40, 32),
            )
            arcade.draw_lrbt_rectangle_outline(
                l + 8, r - 8, ry_b, ry_t, COLOR_UI_BORDER, 1,
            )
            rt = te_text(
                f"picker_row_{i}", l + 16, ry_b + 4, 11, COLOR_WHITE,
            )
            rt.text = key
            rt.draw()
            self._trigger_editor_btn_rects.append(
                (l + 8, r - 8, ry_b, ry_t, f"picker_pick:{key}")
            )
        # Cancel
        cb_l = l + 8
        cb_r = r - 8
        cb_t = b + 30
        cb_b = b + 8
        arcade.draw_lrbt_rectangle_filled(cb_l, cb_r, cb_b, cb_t, (80, 50, 40))
        arcade.draw_lrbt_rectangle_outline(
            cb_l, cb_r, cb_b, cb_t, COLOR_UI_BORDER, 1,
        )
        ct = te_text("picker_cancel", (cb_l + cb_r) / 2, cb_b + 6,
                     11, COLOR_WHITE, True, anchor_x="center")
        ct.text = "Cancel"
        ct.draw()
        self._trigger_editor_btn_rects.append(
            (cb_l, cb_r, cb_b, cb_t, "picker_cancel")
        )

    def _trigger_editor_handle_click(self, x: int, y: int) -> bool:
        """Hit-test the trigger editor's button rects. Returns True
        if the click was handled. Eats every click while the modal
        is open."""
        for x1, x2, y1, y2, action in self._trigger_editor_btn_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                # Clicking somewhere else drops any active text input.
                if (
                    self.trigger_editor_text_field is not None
                    and action not in ("edit_name", "edit_msg")
                ):
                    self.trigger_editor_text_field = None
                    self._trigger_editor_text_buffer = ""
                self._trigger_editor_dispatch(action)
                return True
        # Click outside any button — drop text-input focus too.
        if self.trigger_editor_text_field is not None:
            self.trigger_editor_text_field = None
            self._trigger_editor_text_buffer = ""
        return True

    def _trigger_editor_dispatch(self, action: str) -> None:
        ev = None
        if (
            0 <= self.trigger_editor_selected_idx < len(self.trigger_editor_events)
        ):
            ev = self.trigger_editor_events[self.trigger_editor_selected_idx]
        if action == "cancel":
            self._trigger_editor_close(commit=False)
            return
        if action == "save":
            self._trigger_editor_close(commit=True)
            return
        if action == "scroll_up":
            self.trigger_editor_scroll = max(0, self.trigger_editor_scroll - 1)
            return
        if action == "scroll_down":
            self.trigger_editor_scroll += 1  # _draw clamps next frame
            return
        if action.startswith("select:"):
            new_idx = int(action.split(":", 1)[1])
            if 0 <= new_idx < len(self.trigger_editor_events):
                self.trigger_editor_selected_idx = new_idx
            return
        if action == "add_event":
            new_ev = {
                "name": "New event",
                "effects": {},
                "modifiers": {},
                "pop": 0,
                "happy": 0,
                "duration_ticks": 0,
                "msg": "A new event occurs.",
                "color": [200, 200, 200],
            }
            self.trigger_editor_events.append(new_ev)
            self.trigger_editor_selected_idx = len(self.trigger_editor_events) - 1
            self.trigger_editor_dirty = True
            return
        if action == "remove_event":
            if ev is None:
                return
            idx = self.trigger_editor_selected_idx
            del self.trigger_editor_events[idx]
            self.trigger_editor_dirty = True
            if not self.trigger_editor_events:
                self.trigger_editor_selected_idx = -1
            else:
                self.trigger_editor_selected_idx = min(
                    idx, len(self.trigger_editor_events) - 1
                )
            return
        if ev is None:
            return
        # Scalar +/- buttons: minus_major:field:allow_neg etc.
        if action.startswith(
            ("minus_major:", "minus_minor:", "plus_minor:", "plus_major:")
        ):
            kind, field, allow_neg_s = action.split(":", 2)
            allow_neg = bool(int(allow_neg_s))
            if kind == "minus_major":
                delta = -self.TRIGGER_EDITOR_STEP_MAJOR
            elif kind == "minus_minor":
                delta = -self.TRIGGER_EDITOR_STEP_MINOR
            elif kind == "plus_minor":
                delta = self.TRIGGER_EDITOR_STEP_MINOR
            else:
                delta = self.TRIGGER_EDITOR_STEP_MAJOR
            cur = int(ev.get(field, 0))
            new_val = cur + delta
            if not allow_neg:
                new_val = max(0, new_val)
            if new_val != cur:
                ev[field] = new_val
                self.trigger_editor_dirty = True
            return
        # Colour +/-.
        if action.startswith(
            ("color_minus_major:", "color_minus_minor:",
             "color_plus_minor:",  "color_plus_major:")
        ):
            kind, idx_s = action.split(":", 1)
            idx = int(idx_s)
            if kind == "color_minus_major":
                delta = -10
            elif kind == "color_minus_minor":
                delta = -1
            elif kind == "color_plus_minor":
                delta = 1
            else:
                delta = 10
            col = ev.setdefault("color", [200, 200, 200])
            cur = int(col[idx])
            new_val = max(0, min(255, cur + delta))
            if new_val != cur:
                col[idx] = new_val
                self.trigger_editor_dirty = True
            return
        # Text inputs.
        if action == "edit_name":
            self.trigger_editor_text_field = "name"
            self._trigger_editor_text_buffer = str(ev.get("name", ""))
            return
        if action == "edit_msg":
            self.trigger_editor_text_field = "msg"
            self._trigger_editor_text_buffer = str(ev.get("msg", ""))
            return
        # Effects / modifiers panes.
        for pane_prefix, pane_key in (("effect", "effects"),
                                       ("modifier", "modifiers")):
            pane_dict = ev.setdefault(pane_key, {})
            for kind, delta in (
                (f"{pane_prefix}_minus_major:", -10),
                (f"{pane_prefix}_minus_minor:", -1),
                (f"{pane_prefix}_plus_minor:",   1),
                (f"{pane_prefix}_plus_major:",  10),
            ):
                if action.startswith(kind):
                    rid = action[len(kind):]
                    cur = pane_dict.get(rid, 0)
                    if not isinstance(cur, (int, float)) or isinstance(cur, bool):
                        return  # non-numeric — can't step
                    new_val = cur + delta
                    # Effects allow negative (e.g. Famine food -150);
                    # modifier "factor" keys clamp at 0 (a negative
                    # factor would invert the engine's water/wood math).
                    if pane_prefix == "modifier":
                        new_val = max(0, new_val)
                    if new_val == 0 and pane_prefix == "modifier":
                        pane_dict.pop(rid, None)
                    else:
                        pane_dict[rid] = int(new_val) if isinstance(cur, int) else new_val
                    if new_val != cur:
                        self.trigger_editor_dirty = True
                    return
            if action.startswith(f"{pane_prefix}_remove:"):
                rid = action.split(":", 1)[1]
                if rid in pane_dict:
                    del pane_dict[rid]
                    self.trigger_editor_dirty = True
                return
            if action == f"{pane_prefix}_add":
                self.trigger_editor_picker_open = pane_prefix
                return
        # Picker pick/cancel.
        if action == "picker_cancel":
            self.trigger_editor_picker_open = None
            return
        if action.startswith("picker_pick:"):
            key = action.split(":", 1)[1]
            side = self.trigger_editor_picker_open
            if side == "effect":
                ev.setdefault("effects", {})[key] = (
                    ev["effects"].get(key) or 0
                )
                # If the entry was missing, seed with 0; otherwise
                # leave the existing value.
                if ev["effects"][key] == 0:
                    ev["effects"][key] = 10  # sensible non-zero default
            elif side == "modifier":
                # The non-numeric modifier `pop_kill_when_resource_zero`
                # gets a sensible default list shape; numeric "*_factor"
                # keys get 1.0 (multiplicative identity, then the
                # author dials it from there).
                if key == "pop_kill_when_resource_zero":
                    ev.setdefault("modifiers", {})[key] = [["wood", 1]]
                else:
                    ev.setdefault("modifiers", {})[key] = 1.0
            self.trigger_editor_picker_open = None
            self.trigger_editor_dirty = True
            return
        log.warning("Trigger editor: unknown action %r", action)

    def _trigger_editor_close(self, commit: bool) -> None:
        """Close the trigger editor. If ``commit`` and the editor is
        dirty, write events.json and reload the running event manager."""
        if commit and self.trigger_editor_dirty:
            try:
                self._trigger_editor_save_to_disk()
                self._notify("Events saved.", COLOR_GREEN)
            except Exception as e:  # noqa: BLE001 — surface any I/O fault
                log.exception("Trigger editor save failed")
                self._notify(f"Save failed: {e}", COLOR_RED)
                return  # leave editor open so player can retry
        self.trigger_editor_open = False
        self.trigger_editor_dirty = False
        self.trigger_editor_picker_open = None
        self.trigger_editor_text_field = None
        self._trigger_editor_text_buffer = ""

    def _trigger_editor_save_to_disk(self) -> None:
        """Apply the editor's working list to ``data/events.json`` and
        rebuild the running event manager. Atomic via tmp+rename,
        matching the buildings editor's save semantics.
        """
        import json
        import os
        from events import load_events as _reload_events
        out: list[dict] = []
        for ev in self.trigger_editor_events:
            # Strip empty effects/modifiers so the JSON stays tight
            # (matches the original events.json — Plague has no
            # `effects` key, not an empty one).
            entry: dict = {
                "name": str(ev.get("name", "")),
                "effects": {k: int(v) if isinstance(v, (int, float))
                            and not isinstance(v, bool) and v == int(v)
                            else v
                            for k, v in (ev.get("effects") or {}).items()},
                "pop": int(ev.get("pop", 0)),
                "happy": int(ev.get("happy", 0)),
                "msg": str(ev.get("msg", "")),
                "color": [int(c) for c in (ev.get("color") or [200, 200, 200])],
            }
            # duration_ticks + modifiers are optional — only persist
            # them when set.
            dur = int(ev.get("duration_ticks", 0))
            if dur > 0:
                entry["duration_ticks"] = dur
            mods = ev.get("modifiers") or {}
            if mods:
                entry["modifiers"] = dict(mods)
            out.append(entry)
        path = EVENTS_PATH
        tmp = str(path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f, indent=2)
        os.replace(tmp, path)
        log.info("Trigger editor: wrote %s (%d events)", path, len(out))
        # Hot-reload the running event manager. Preserve the
        # scheduled list and disable_random_events flag — those are
        # per-map state, not part of events.json.
        em = getattr(self, "event_manager", None)
        if em is not None:
            try:
                # Pass the path explicitly so tests that monkeypatch
                # EVENTS_PATH see the updated location (the
                # load_events default arg is bound at import time).
                em.events = _reload_events(path)
            except Exception as e:  # noqa: BLE001
                log.warning("Trigger editor: reload failed: %s", e)

    EDITOR_TEXT_INPUT_MAX = 80

    def _trigger_editor_text_handle_key(
        self, symbol: int, modifiers: int,
    ) -> bool:
        """Buffer mutation for the trigger editor's name / msg text
        fields. Returns True if the key was consumed. Enter commits;
        Esc cancels; Backspace deletes; printable chars append.
        """
        field = self.trigger_editor_text_field
        if field not in ("name", "msg"):
            return False
        if symbol in (arcade.key.ENTER, arcade.key.RETURN, arcade.key.NUM_ENTER):
            ev = self.trigger_editor_events[self.trigger_editor_selected_idx]
            buf = (self._trigger_editor_text_buffer or "").strip() \
                if field == "name" else (self._trigger_editor_text_buffer or "")
            limit = (
                self.TRIGGER_EDITOR_NAME_MAX if field == "name"
                else self.TRIGGER_EDITOR_MSG_MAX
            )
            buf = buf[:limit]
            if field == "name" and not buf:
                self._notify("Name can't be empty", COLOR_RED)
                return True
            if ev.get(field) != buf:
                ev[field] = buf
                self.trigger_editor_dirty = True
            self.trigger_editor_text_field = None
            self._trigger_editor_text_buffer = ""
            return True
        if symbol == arcade.key.ESCAPE:
            self.trigger_editor_text_field = None
            self._trigger_editor_text_buffer = ""
            return True
        if symbol == arcade.key.BACKSPACE:
            self._trigger_editor_text_buffer = (
                self._trigger_editor_text_buffer or ""
            )[:-1]
            return True
        ch = self._char_for_symbol(symbol, modifiers)
        if ch is not None:
            limit = (
                self.TRIGGER_EDITOR_NAME_MAX if field == "name"
                else self.TRIGGER_EDITOR_MSG_MAX
            )
            if len(self._trigger_editor_text_buffer or "") < limit:
                self._trigger_editor_text_buffer = (
                    self._trigger_editor_text_buffer or ""
                ) + ch
            return True
        return False

    def _char_for_symbol(self, symbol: int, modifiers: int) -> str | None:
        """Map an arcade key code to a printable character for text-
        input fields. Returns None if the key isn't a printable we
        want to accept. Shared by the trigger editor and the
        scheduled-events panel — both want the same letter/digit/
        punctuation set, so we don't re-implement it.
        """
        shift = bool(modifiers & arcade.key.MOD_SHIFT)
        if arcade.key.A <= symbol <= arcade.key.Z:
            base = symbol - arcade.key.A
            ch = chr(ord("a") + base)
            return ch.upper() if shift else ch
        if arcade.key.KEY_0 <= symbol <= arcade.key.KEY_9:
            return chr(ord("0") + symbol - arcade.key.KEY_0)
        if symbol == arcade.key.SPACE:
            return " "
        if symbol == arcade.key.MINUS:
            return "_" if shift else "-"
        if symbol == arcade.key.UNDERSCORE:
            return "_"
        if symbol == arcade.key.PERIOD:
            return "."
        if symbol == arcade.key.COMMA:
            return ","
        if symbol == arcade.key.SEMICOLON:
            return ":" if shift else ";"
        if symbol == arcade.key.APOSTROPHE:
            return "'"
        if symbol == arcade.key.EXCLAMATION:
            return "!"
        if symbol == arcade.key.QUESTION:
            return "?"
        return None






    # ══════════════════════════════════════════════════════════════════════
    #  INPUT
    # ══════════════════════════════════════════════════════════════════════
    def on_mouse_motion(self, x: int, y: int, dx: int, dy: int) -> None:
        self.mouse_x = x
        self.mouse_y = y
        # Splash: only thing tracked is which menu button (if any) the
        # mouse is over so we can highlight it.
        if self.app_state == "splash":
            self.splash_hover = self._splash_button_at(x, y)
            return
        # World hover (only if mouse is over the world area).
        if y < self.height - TOP_BAR_H and x < self.width - RIGHT_PANEL_W and y >= BOTTOM_BAR_H:
            wx, wy = self._screen_to_world(x, y)
            # v0.30: must use the instance variant, NOT the @staticmethod
            # world_to_grid. The static one bounds against module-level
            # GRID_ROWS/GRID_COLS (the *default* 30×40 grid), so on any
            # editor-resized map (v0.26 lets the user pick anything up
            # to 64×64), hover past row 29 / col 39 silently returned
            # (None, None) — creating a dead zone on the right/bottom
            # strip that looked like the editor was "auto-shrinking"
            # the map back to the default size.
            self.hover_row, self.hover_col = self.game_map.world_to_grid_instance(wx, wy)
        else:
            self.hover_row, self.hover_col = None, None
        # Palette tooltip.
        self.palette_hover = self._palette_button_at(x, y) if y < BOTTOM_BAR_H else None

    def on_mouse_press(self, x: int, y: int, button: int, modifiers: int) -> None:
        btn_name = {
            arcade.MOUSE_BUTTON_LEFT: "LEFT",
            arcade.MOUSE_BUTTON_RIGHT: "RIGHT",
            arcade.MOUSE_BUTTON_MIDDLE: "MIDDLE",
        }.get(button, str(button))
        # v0.6: every click logs once, with (x,y,button) and the resolved
        # UI region. Cheap, debug-level so production runs are quiet by
        # default; flip the root logger to DEBUG to see them.
        log.debug("CLICK %s at (%d,%d) modifiers=%d", btn_name, x, y, modifiers)

        # Splash screen — eats every click. Left clicks fire button actions;
        # other clicks do nothing. If the credits panel is open, *any* click
        # dismisses it (matches the on-screen prompt) without firing a button.
        if self.app_state == "splash":
            if self.show_credits:
                self.show_credits = False
                log.info("CLICK closes credits panel")
                return
            # v0.52: the "Mapping keys keyboards" overlay also dismisses
            # on any click (matches its on-screen prompt) before the
            # splash buttons can fire.
            if getattr(self, "show_keymap", False):
                self.show_keymap = False
                log.info("CLICK closes keymap overlay")
                return
            # v0.19: load-map picker on splash eats clicks before
            # the splash buttons. Hit-test rows; click outside
            # dismisses.
            if self.show_splash_load_map:
                if button == arcade.MOUSE_BUTTON_LEFT:
                    handled = False
                    for x1, x2, y1, y2, action in self._splash_load_map_rects:
                        if x1 <= x <= x2 and y1 <= y <= y2:
                            if action.startswith("pick:"):
                                fname = action.split(":", 1)[1]
                                self._splash_play_map_file(fname)
                            handled = True
                            break
                    if not handled:
                        self.show_splash_load_map = False
                return
            # v0.36: load-scenario picker — same pattern as the map
            # picker, but the action payload carries a data-dir-relative
            # path rather than a bare MAPS_DIR filename.
            if self.show_splash_load_scenario:
                if button == arcade.MOUSE_BUTTON_LEFT:
                    handled = False
                    for x1, x2, y1, y2, action in self._splash_load_scenario_rects:
                        if x1 <= x <= x2 and y1 <= y <= y2:
                            if action.startswith("pick:"):
                                rel = action.split(":", 1)[1]
                                self._splash_play_scenario_path(rel)
                            handled = True
                            break
                    if not handled:
                        self.show_splash_load_scenario = False
                return
            if button != arcade.MOUSE_BUTTON_LEFT:
                return
            idx = self._splash_button_at(x, y)
            if idx is None:
                log.debug("CLICK on splash: no button hit")
                return
            label, action = self._splash_actions[idx]
            log.info("CLICK splash button: %s", label)
            action()
            return

        # Menu first — eats all clicks.
        if self.show_menu:
            idx = self._menu_button_at(x, y)
            if idx is not None and button == arcade.MOUSE_BUTTON_LEFT:
                label, action = self._menu_actions[idx]
                log.info("CLICK menu button: %s", label)
                action()
            else:
                log.debug("CLICK in menu: no button hit")
            return

        # v0.19: bartering modal eats every click before world.
        if self.show_barter:
            if button == arcade.MOUSE_BUTTON_LEFT:
                self._barter_handle_click(x, y)
            return

        # v0.35: gold-trade modal — same precedence as bartering.
        if getattr(self, "show_gold_trade", False):
            if button == arcade.MOUSE_BUTTON_LEFT:
                self._gold_trade_handle_click(x, y)
            return

        # v0.50: commercial-roads window — same precedence as gold-trade.
        if getattr(self, "show_commercial_roads", False):
            if button == arcade.MOUSE_BUTTON_LEFT:
                self._commercial_roads_handle_click(x, y)
            return

        # v0.25: unit editor (definition editor — rewritten in v0.25).
        # Eats every click; buttons inside dispatch via
        # `_unit_editor_handle_click`. Esc / Cancel close it; clicks
        # outside the modal hit rects are absorbed but do nothing
        # (parallels the buildings editor).
        if self.show_unit_editor:
            if button == arcade.MOUSE_BUTTON_LEFT:
                self._unit_editor_handle_click(x, y)
            return

        # v0.22: diagnostics panel. Any click closes (matching jobs / stats).
        if self.show_diagnostics:
            self.show_diagnostics = False
            return

        # v0.15: buildings editor — eats every click. Buttons inside
        # are dispatched via _editor_handle_click; clicks elsewhere
        # in the modal are absorbed but do nothing (the player uses
        # Cancel / Save / Esc to leave).
        if self.editor_open:
            if button == arcade.MOUSE_BUTTON_LEFT:
                # v0.30: scrollbar thumb drag start. If the click lands
                # on the thumb, set a drag flag and stash a y-offset so
                # the thumb tracks the mouse smoothly. If it lands on
                # the track *outside* the thumb, page-jump in that
                # direction.
                rect = getattr(self, "_editor_scrollbar_rect", None)
                if rect is not None:
                    sx1, sx2, track_b, track_t, thumb_b, thumb_t, \
                        max_scroll, track_h, thumb_h = rect
                    if sx1 <= x <= sx2 and track_b <= y <= track_t:
                        if thumb_b <= y <= thumb_t:
                            # Thumb hit — start drag.
                            self._editor_thumb_dragging = True
                            self._editor_thumb_drag_offset = thumb_t - y
                            return
                        else:
                            # Page-jump on track click. Page size =
                            # one screen of rows.
                            rows_visible = getattr(
                                self, "_editor_list_rows_visible", 5,
                            )
                            if y > thumb_t:
                                self.editor_scroll = max(
                                    0, self.editor_scroll - rows_visible,
                                )
                            else:
                                self.editor_scroll = min(
                                    max_scroll,
                                    self.editor_scroll + rows_visible,
                                )
                            return
                self._editor_handle_click(x, y)
            return

        # v0.28: trigger editor — same pattern as the buildings
        # editor. Eats every click while open; buttons inside route
        # through `_trigger_editor_handle_click`.
        if self.trigger_editor_open:
            if button == arcade.MOUSE_BUTTON_LEFT:
                self._trigger_editor_handle_click(x, y)
            return

        # v0.31: RPG request editor — same eat-all-clicks pattern.
        if self.rpg_request_editor_state is not None \
                and self.rpg_request_editor_state.open:
            if button == arcade.MOUSE_BUTTON_LEFT:
                import rpg_request_editor
                rpg_request_editor.handle_click(
                    self, self.rpg_request_editor_state, x, y,
                )
            return

        # v0.32: Cutscene editor — same eat-all-clicks pattern.
        if self.cutscene_editor_state is not None \
                and self.cutscene_editor_state.open:
            if button == arcade.MOUSE_BUTTON_LEFT:
                import cutscene_editor
                cutscene_editor.handle_click(
                    self, self.cutscene_editor_state, x, y,
                )
            return

        # v0.37: Triggers editor — same eat-all-clicks pattern as the
        # RPG / cutscene editors above. See the matching draw branch
        # for why this lives outside the splash app_state.
        if self.triggers_editor_state is not None \
                and self.triggers_editor_state.open:
            if button == arcade.MOUSE_BUTTON_LEFT:
                import triggers_editor
                triggers_editor.handle_click(
                    self, self.triggers_editor_state, x, y,
                )
            return

        # v0.38 (audit 7.1): RPG request panel eats every click while
        # up; a click on a decision button resolves the request and
        # restores the pre-panel paused state.
        if self.rpg_player_state is not None \
                and self.rpg_player_state.active:
            if button == arcade.MOUSE_BUTTON_LEFT:
                import rpg_player
                chosen = rpg_player.handle_click(
                    self.rpg_player_state, self, x, y,
                )
                if chosen is not None:
                    self.apply_rpg_decision(chosen)
                    self.paused = getattr(self, "_rpg_was_paused", False)
            return

        # v0.32: Cutscene runtime player — full-screen, eats every
        # click. Closing the cutscene restores the pre-cutscene paused
        # state (so a cutscene that fired during play resumes play;
        # one that fired while paused stays paused).
        if self.cutscene_player_state is not None \
                and self.cutscene_player_state.active:
            if button == arcade.MOUSE_BUTTON_LEFT:
                import cutscene_player
                cutscene_player.handle_click(
                    self.cutscene_player_state, self, x, y,
                )
                # If the click closed the cutscene, restore paused
                # state to what it was before the cutscene fired.
                if not self.cutscene_player_state.active:
                    self.paused = getattr(self, "_cutscene_was_paused", False)
            return

        # v0.16: map editor — toolbar / picker / rename dialog get
        # first dibs on every click. Anything not consumed there
        # falls through to the normal world / palette handlers (so
        # the player can still place buildings, scroll the palette,
        # etc.).
        # v0.50: the init-values modal is fully modal — it swallows
        # every mouse button (including right-click demolish) so the
        # world underneath is untouchable while the form is up.
        if self.app_state == "editor" and getattr(self, "editor_show_initvals", False):
            if button == arcade.MOUSE_BUTTON_LEFT:
                self._editor_initvals_handle_click(x, y)
            return
        if self.app_state == "editor" and button == arcade.MOUSE_BUTTON_LEFT:
            if self._editor_handle_toolbar_click(x, y):
                return

        # Graph window: eats clicks too (any click closes it).
        if self.show_graphs:
            log.info("CLICK closes graph window")
            self.show_graphs = False
            return
        # v0.8: stats window — same behaviour.
        if self.show_stats:
            log.info("CLICK closes stats panel")
            self.show_stats = False
            return
        # v0.17: jobs panel — same behaviour as stats.
        if getattr(self, "show_jobs", False):
            log.info("CLICK closes jobs panel")
            self.show_jobs = False
            return

        # Mini-map teleport.
        mm = self._minimap_at(x, y)
        if mm is not None and button == arcade.MOUSE_BUTTON_LEFT:
            row, col = mm
            self.world_camera.position = (
                col * TILE_SIZE + TILE_SIZE / 2,
                row * TILE_SIZE + TILE_SIZE / 2,
            )
            log.info("CLICK minimap → teleport to (%d,%d)", row, col)
            return

        # Top bar: clicks dismiss the info panel.
        if y > self.height - TOP_BAR_H:
            log.debug("CLICK in top bar — dismiss inspector")
            self.inspected = None
            self._warehouse_btn_rects = []
            return

        # Right panel: clicks dismiss the info panel.
        # v0.26 fix: the right panel only spans the *world view* — i.e.
        # `y >= BOTTOM_BAR_H` — but the previous check also ate clicks
        # in the bottom bar at the right edge. With 15 category tabs
        # the last two (Civic, Mil) extend past x=1040 on the default
        # 1280-wide window, so middle-of-bottom-bar clicks on those
        # tabs got dismissed here instead of reaching `_tab_at` below.
        # Scoping this branch to the world-view band restores the
        # palette tabs at the right edge.
        if x > self.width - RIGHT_PANEL_W and y >= BOTTOM_BAR_H:
            log.debug("CLICK in right panel — dismiss inspector")
            self.inspected = None
            self._warehouse_btn_rects = []
            return

        # ── v0.11: warehouse toggle buttons ─────────────────────────────
        # The inspector panel for a warehouse has a footer of accept/
        # reject toggles + a Drain + a Reset button. Hit-test those
        # before falling through to world-click. Only LEFT clicks count;
        # right-clicks on the inspector dismiss it (consistent with
        # right-click anywhere else acting as cancel/demolish).
        if (
            button == arcade.MOUSE_BUTTON_LEFT
            and self.inspected is not None
            and self._warehouse_btn_rects
        ):
            wb_orow, wb_ocol = self.inspected
            for x1, x2, y1, y2, action in self._warehouse_btn_rects:
                if x1 <= x <= x2 and y1 <= y <= y2:
                    if action == "__drain__":
                        # Drain mode: empty set = accept nothing. Goods
                        # currently in this warehouse get redistributed
                        # on the next tick (or land in overflow if no
                        # other warehouse has room).
                        self.storage.set_accepts(
                            wb_orow, wb_ocol, set(),
                        )
                        log.info(
                            "CLICK warehouse toggle: drain (%d,%d)",
                            wb_orow, wb_ocol,
                        )
                    elif action == "__reset__":
                        self.storage.set_accepts(
                            wb_orow, wb_ocol, None,
                        )
                        log.info(
                            "CLICK warehouse toggle: reset (%d,%d) accept-all",
                            wb_orow, wb_ocol,
                        )
                    else:
                        # Single-good toggle.
                        new_state = self.storage.toggle_accept(
                            wb_orow, wb_ocol, action,
                        )
                        log.info(
                            "CLICK warehouse toggle (%d,%d): %s → %s",
                            wb_orow, wb_ocol, action,
                            "accept" if new_state else "reject",
                        )
                    return

        # ── v0.41: shipyard build-queue buttons ─────────────────────────
        # The inspector panel for a shipyard has a footer with an 'add
        # ship' button and a per-hull remove (✕) button. Same hit-test
        # shape as the warehouse toggles; LEFT clicks only.
        if (
            button == arcade.MOUSE_BUTTON_LEFT
            and self.inspected is not None
            and self._shipyard_btn_rects
        ):
            sy_orow, sy_ocol = self.inspected
            cell = self.game_map.grid[sy_orow][sy_ocol]
            yard_kind = ""
            if cell is not None:
                ybd = self.registry.get(cell[0])
                yard_kind = getattr(ybd, "ship_kind", "") if ybd else ""
            for x1, x2, y1, y2, action in self._shipyard_btn_rects:
                if x1 <= x <= x2 and y1 <= y <= y2:
                    if action == "__add__":
                        added = self.walker_manager.shipyard_enqueue(
                            self.game_map, sy_orow, sy_ocol, yard_kind,
                        )
                        log.info(
                            "CLICK shipyard add (%d,%d): %s → %s",
                            sy_orow, sy_ocol, yard_kind,
                            "queued" if added else "queue full",
                        )
                    elif action.startswith("__rm__"):
                        try:
                            idx = int(action[len("__rm__"):])
                        except ValueError:
                            idx = -1
                        self.walker_manager.shipyard_dequeue(
                            self.game_map, sy_orow, sy_ocol, idx,
                        )
                        log.info(
                            "CLICK shipyard remove (%d,%d): index %d",
                            sy_orow, sy_ocol, idx,
                        )
                    return

        # Bottom bar: tabs first, then palette buttons.
        if y < BOTTOM_BAR_H:
            tab = self._tab_at(x, y)
            if tab is not None and button == arcade.MOUSE_BUTTON_LEFT:
                old = self.active_cat_idx
                self.active_cat_idx = tab
                # v0.19.x: reset horizontal scroll on tab switch so
                # the new tab opens at its first button rather than
                # mid-page from the previous tab's offset.
                self.palette_scroll = 0
                cat_id, cat_label, _ = self.palette_by_cat[tab]
                log.info("CLICK tab %d → %s (%s)", tab, cat_id, cat_label)
                # Auto-select the first building in the new tab.
                pal = self._current_palette()
                if pal:
                    self.selected_building = pal[0]
                    log.info("Selected → %s", self.selected_building)
                if old != tab:
                    log.debug("Tab %d → %d", old, tab)
                return
            # v0.19.x: scroll arrows for tabs that overflow.
            arrow = self._palette_arrow_at(x, y)
            if arrow is not None and button == arcade.MOUSE_BUTTON_LEFT:
                if arrow == "left":
                    self.palette_scroll = max(0, self.palette_scroll - 1)
                else:
                    self.palette_scroll += 1  # _draw clamps next frame
                log.info("CLICK palette arrow %s → scroll %d", arrow, self.palette_scroll)
                return
            idx = self._palette_button_at(x, y)
            if idx is not None and button == arcade.MOUSE_BUTTON_LEFT:
                pal = self._current_palette()
                if idx < len(pal):
                    self.selected_building = pal[idx]
                    log.info("CLICK palette button %d → %s", idx, self.selected_building)
            else:
                log.debug("CLICK in bottom bar: no hit")
            return

        # World click.
        wx, wy = self._screen_to_world(x, y)
        # v0.30: use the instance variant (see on_mouse_motion above
        # for the full rationale). Same bug, same fix — clicks past
        # the default-grid bounds were being dropped on resized maps.
        row, col = self.game_map.world_to_grid_instance(wx, wy)
        if row is None or col is None:
            log.debug("CLICK out of world bounds at world (%.1f,%.1f)", wx, wy)
            return

        # v0.48: command mode takes over world clicks. Left = select
        # (press starts a possible drag-box, release commits); Right =
        # issue an order to the current selection at the clicked tile.
        if getattr(self, "command_mode", False):
            if button == arcade.MOUSE_BUTTON_LEFT:
                # Begin a (possibly zero-size) drag-box; the actual
                # selection commit happens on release in on_mouse_release.
                self._cmd_drag_start = (wx, wy)
                self._cmd_drag_now = (wx, wy)
                return
            if button == arcade.MOUSE_BUTTON_RIGHT:
                self._cmd_issue_order_at(row, col)
                return
            if button == arcade.MOUSE_BUTTON_MIDDLE:
                return  # middle-click inert in command mode
            # fall through for any other button

        if button == arcade.MOUSE_BUTTON_LEFT:
            # v0.19.x: in map editor, if a paint tool is active,
            # paint terrain/feature instead of placing a building.
            if (
                getattr(self, "app_state", "playing") == "editor"
                and getattr(self, "editor_paint_tool", None)
            ):
                if self._editor_paint_at(row, col):
                    return
            # If clicking on an existing building and it's not currently
            # selected for placement, open the info popup instead of placing.
            existing = self.game_map.get_building_at(row, col)
            if existing is not None and existing[0] != "road":
                _bid, orow, ocol = existing
                self.inspected = (orow, ocol)
                log.info(
                    "CLICK world (%d,%d) → inspect %s at (%d,%d)",
                    row, col, _bid, orow, ocol,
                )
                return
            # Otherwise place.
            self.inspected = None
            log.info(
                "CLICK world (%d,%d) → place %s",
                row, col, self.selected_building,
            )
            self._try_place(row, col)
        elif button == arcade.MOUSE_BUTTON_MIDDLE:
            # v0.21: middle-click on an extracting building shows a
            # short-lived bubble summarising how much of the
            # extractable resource is left under the footprint —
            # iron in a mine, trees under a lumber mill, fertility
            # under a farm, fish near a fishery. Buildings that
            # aren't extractors get a generic "no extraction"
            # notification (cheap to surface, helps the player
            # discover which buildings the bubble applies to).
            #
            # v0.23.x: the bubble now also responds to clicks on a
            # RAW natural-resource tile (no building placed yet) —
            # the player can scout the map and see "how much iron
            # is in this vein before I commit to building a mine
            # here". The same summary dict is built directly from
            # the feature_state at the clicked tile.
            existing = self.game_map.get_building_at(row, col)
            if existing is None:
                # v0.23.x: empty cell — but it might still carry a
                # natural-resource feature worth inspecting.
                summary = self._raw_feature_summary(row, col)
                if summary is None:
                    log.debug("MIDDLE-CLICK on empty cell (%d,%d)", row, col)
                    return
                # Anchor the bubble at the clicked tile (single-tile
                # footprint). Expiry timing matches the building case.
                self.extract_bubble = (
                    row, col, summary, self.game_time + 30,
                )
                log.info(
                    "MIDDLE-CLICK raw feature %s at (%d,%d) → reserves=%s",
                    summary["kind"], row, col, summary["reserves"],
                )
                return
            bid, orow, ocol = existing
            if bid == "road":
                return
            bd = self.registry[bid]
            summary = self.game_map.extractable_summary(orow, ocol, bd)
            if summary is None:
                self._notify(
                    f"{bd.name}: not an extractor.",
                    COLOR_GRAY,
                )
                return
            # Stash the bubble; expires ~30 ticks (~15s) later.
            # game_time is a tick counter, not seconds — see on_update.
            self.extract_bubble = (
                orow, ocol, summary, self.game_time + 30,
            )
            log.info(
                "MIDDLE-CLICK %s at (%d,%d) → %s reserves=%s tiles=%d",
                bid, orow, ocol,
                summary["label"], summary["reserves"], summary["tiles"],
            )
        elif button == arcade.MOUSE_BUTTON_RIGHT:
            self.inspected = None
            log.info("CLICK world (%d,%d) RIGHT → demolish attempt", row, col)
            removed = self.game_map.remove_building(row, col)
            # v0.16: in editor mode, demolition is free (placement was
            # too) — no refund, no notification. The author wants to
            # iterate quickly.
            if getattr(self, "app_state", "playing") == "editor":
                if removed:
                    log.info("Editor: removed %s at (%d,%d)", removed, row, col)
                return
            if removed and removed != "road":
                refund = self.registry[removed].cost // 2
                self.economy.refund(refund)
                self._notify(
                    f"Demolished {self.registry[removed].name} (+{refund} gold)",
                    COLOR_GRAY,
                )
                log.info("Demolished %s at (%d,%d), refund %d", removed, row, col, refund)
                self._record_action("demolish", row=row, col=col,
                    removed=removed, refund=int(refund),
                )
            elif removed == "road":
                log.info("Demolished road at (%d,%d)", row, col)
                self._record_action("demolish", row=row, col=col, removed="road", refund=0,
                )
            else:
                log.debug("Right-click on empty cell (%d,%d) — nothing to demolish", row, col)

    def on_mouse_drag(
        self, x: int, y: int, dx: int, dy: int, buttons: int, modifiers: int,
    ) -> None:
        """v0.30: scrollbar-thumb drag handler.

        When the buildings editor's thumb is being dragged (flag set
        in on_mouse_press), each drag event re-positions the thumb
        and recomputes ``editor_scroll`` from the new thumb position.
        Other drag events (e.g. camera pan) aren't intercepted — we
        only act when the editor's thumb-drag flag is set, then early
        return so no other handler runs.
        """
        if self._editor_thumb_dragging and self.editor_open:
            rect = self._editor_scrollbar_rect
            if rect is None:
                return
            sx1, sx2, track_b, track_t, _tb, _tt, max_scroll, track_h, thumb_h = rect
            if max_scroll <= 0 or track_h <= thumb_h:
                return
            # New thumb top edge = mouse y + drag offset, clamped to
            # [track_b + thumb_h, track_t].
            new_thumb_t = y + self._editor_thumb_drag_offset
            new_thumb_t = max(track_b + thumb_h, min(track_t, new_thumb_t))
            # Convert thumb position back to scroll index. The thumb's
            # *offset from the top* (0 → max) maps linearly to
            # editor_scroll (0 → max_scroll).
            thumb_offset = track_t - new_thumb_t
            ratio = thumb_offset / max(1, (track_h - thumb_h))
            self.editor_scroll = int(round(max_scroll * ratio))
            self.editor_scroll = max(0, min(max_scroll, self.editor_scroll))
            return
        # v0.48: in command mode, dragging the left button stretches the
        # selection box (tracked in world space).
        if (
            getattr(self, "command_mode", False)
            and self._cmd_drag_start is not None
            and (buttons & arcade.MOUSE_BUTTON_LEFT)
        ):
            self._cmd_drag_now = self._screen_to_world(x, y)

    def on_mouse_release(
        self, x: int, y: int, button: int, modifiers: int,
    ) -> None:
        """v0.30: clear the editor scrollbar drag flag on any mouse
        release. Cheap and harmless to clear unconditionally — the
        flag is only set during an active thumb drag."""
        if self._editor_thumb_dragging:
            self._editor_thumb_dragging = False
        # v0.48: command-mode left-release commits the selection — a
        # click (tiny drag) selects the single unit under the cursor; a
        # real drag selects everything in the box. Shift adds/toggles.
        if (
            getattr(self, "command_mode", False)
            and button == arcade.MOUSE_BUTTON_LEFT
            and self._cmd_drag_start is not None
        ):
            self._cmd_commit_selection(modifiers)
            self._cmd_drag_start = None
            self._cmd_drag_now = None

    def on_mouse_scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        # v0.50: init-values modal scrolls its goods list instead of
        # zooming the world. Checked first since it's a modal over the
        # editor.
        if self.app_state == "editor" and getattr(self, "editor_show_initvals", False):
            self._editor_initvals_handle_scroll(scroll_y)
            return
        # v0.50: commercial-roads window scrolls its good-picker list.
        if getattr(self, "show_commercial_roads", False):
            self._commercial_roads_handle_scroll(scroll_y)
            return
        # v0.30: when the buildings editor is open, the mouse wheel
        # scrolls the list (3 lines per notch) instead of zooming the
        # world camera. Without this, the wheel did nothing useful
        # inside the modal (the world isn't visible).
        if self.editor_open:
            step = 3
            max_scroll = getattr(self, "_editor_list_max_scroll", 0)
            # scroll_y > 0 = wheel up = move up the list = decrease scroll.
            self.editor_scroll = max(
                0,
                min(
                    max_scroll,
                    self.editor_scroll - int(scroll_y) * step,
                ),
            )
            return
        old_zoom = self.world_camera.zoom
        new_zoom = old_zoom + scroll_y * CAMERA_ZOOM_STEP
        new_zoom = max(CAMERA_ZOOM_MIN, min(CAMERA_ZOOM_MAX, new_zoom))
        self.world_camera.zoom = new_zoom
        if abs(new_zoom - old_zoom) > 0.001:
            log.debug("Zoom: %.2f", new_zoom)

    def on_key_press(self, symbol: int, modifiers: int) -> None:
        self.keys_held.add(symbol)

        # Splash: keyboard is intentionally minimal. ESC closes the credits
        # panel if open, otherwise quits — matches what an OS-level "main
        # menu" usually does. ENTER triggers "New game" as a one-key start.
        # Up/Down arrows move the hover cursor through the button list.
        if self.app_state == "splash":
            if symbol == arcade.key.ESCAPE:
                if self.show_splash_load_map:
                    self.show_splash_load_map = False
                    return
                # v0.36: dismiss new splash modals before the credits
                # / close fallback. Same precedence rule the load-map
                # picker already follows.
                if self.show_splash_load_scenario:
                    self.show_splash_load_scenario = False
                    return
                # v0.37: the old show_splash_event_editor info modal
                # was replaced by the real Triggers editor, which
                # owns its own ESC dispatch via the key-press chain
                # earlier in this method — but if a sub-state is
                # somehow lingering, fall through to the close()
                # path below rather than swallow the key here.
                if self.show_credits:
                    self.show_credits = False
                elif getattr(self, "show_keymap", False):
                    # v0.52: Esc closes the keymap overlay before quitting.
                    self.show_keymap = False
                else:
                    self.close()
                return
            if symbol in (arcade.key.ENTER, arcade.key.RETURN, arcade.key.NUM_ENTER):
                if self.splash_hover is not None:
                    label, action = self._splash_actions[self.splash_hover]
                    log.info("ENTER splash button: %s", label)
                    action()
                else:
                    log.info("ENTER on splash → New game")
                    self._splash_new_game()
                return
            if symbol in (arcade.key.DOWN, arcade.key.S):
                cur = self.splash_hover if self.splash_hover is not None else -1
                self.splash_hover = (cur + 1) % len(self._splash_actions)
                return
            if symbol in (arcade.key.UP, arcade.key.W):
                cur = self.splash_hover if self.splash_hover is not None else 0
                self.splash_hover = (cur - 1) % len(self._splash_actions)
                return
            return

        # v0.50: map editor's init-values form captures every keypress
        # while open (digit entry into the focused field, Enter commits,
        # Esc cancels). Sits ahead of the rename-dialog and global ESC
        # handlers so Esc dismisses the form rather than popping the menu.
        if (
            self.app_state == "editor"
            and getattr(self, "editor_show_initvals", False)
        ):
            if self._editor_initvals_handle_key(symbol, modifiers):
                return

        # v0.16: map editor's rename dialog captures every keypress
        # while open — letters/digits go into the buffer, Enter
        # commits, Esc cancels. Done before the global ESC handler
        # below so Esc dismisses the dialog instead of opening the
        # game menu.
        if (
            self.app_state == "editor"
            and getattr(self, "editor_show_rename_dialog", False)
        ):
            if self._editor_rename_handle_key(symbol, modifiers):
                return
        # v0.16: editor's load picker — Esc dismisses.
        if (
            self.app_state == "editor"
            and getattr(self, "editor_show_load_picker", False)
            and symbol == arcade.key.ESCAPE
        ):
            self.editor_show_load_picker = False
            return

        # v0.19.x: buildings-editor sprite-filename text input.
        # When the player has clicked into the filename field, every
        # keypress goes to the buffer until Enter (commit), Esc
        # (cancel), or a click elsewhere. Lives ahead of the global
        # ESC handler so Esc cancels the input instead of closing
        # the editor entirely.
        if (
            getattr(self, "editor_open", False)
            and getattr(self, "editor_text_input_field", None) == "filename"
        ):
            if self._editor_filename_handle_key(symbol, modifiers):
                return

        # v0.30: buildings-editor keyboard navigation.
        # Up / Down step one row, PgUp / PgDn step a page, Home / End
        # jump to first / last. Each move both reselects the building
        # (so the right-hand form snaps to it) and adjusts scroll so
        # the selection stays visible. Sits AFTER the filename text
        # input above — that way typing into the filename field still
        # wins on Up/Down keystrokes within the field.
        if (
            getattr(self, "editor_open", False)
            and getattr(self, "editor_text_input_field", None) != "filename"
        ):
            if self._editor_handle_nav_key(symbol):
                return

        # v0.28: scheduled-events panel value-field text input. Same
        # pattern as the buildings-editor filename input. Sits ahead
        # of the global ESC handler so Esc cancels the input rather
        # than closing the panel.
        if (
            getattr(self, "show_scheduled_panel", False)
            and getattr(self, "scheduled_text_field", None) is not None
        ):
            if self._scheduled_text_handle_key(symbol, modifiers):
                return

        # v0.28: trigger-editor text input (event name / banner msg).
        # Same precedence as the buildings-editor filename input — sits
        # ahead of the global ESC handler so Esc cancels the input
        # rather than closing the editor.
        if (
            getattr(self, "trigger_editor_open", False)
            and getattr(self, "trigger_editor_text_field", None) is not None
        ):
            if self._trigger_editor_text_handle_key(symbol, modifiers):
                return

        # v0.31: RPG request editor — same precedence. While the
        # editor is open, every keystroke first gets a chance to be
        # consumed by the editor (text input + the editor-level Esc
        # shortcut that closes the modal).
        if (
            self.rpg_request_editor_state is not None
            and self.rpg_request_editor_state.open
        ):
            import rpg_request_editor
            if rpg_request_editor.handle_key(
                self, self.rpg_request_editor_state, symbol, modifiers,
            ):
                return

        # v0.37: Triggers editor key dispatch — same shape as the
        # RPG request editor above. Text-input focus, Esc-cancels-input,
        # Esc-with-no-input closes the editor.
        if (
            self.triggers_editor_state is not None
            and self.triggers_editor_state.open
        ):
            import triggers_editor
            if triggers_editor.handle_key(
                self, self.triggers_editor_state, symbol, modifiers,
            ):
                return

        # v0.32: Cutscene editor key dispatch — same shape as the
        # RPG request editor above. Lets the editor consume keys
        # for its text fields before any global shortcut fires.
        if (
            self.cutscene_editor_state is not None
            and self.cutscene_editor_state.open
        ):
            import cutscene_editor
            if cutscene_editor.handle_key(
                self, self.cutscene_editor_state, symbol, modifiers,
            ):
                return

        # v0.38 (audit 7.1): RPG request panel keyboard handling. Number
        # keys 1..N pick the matching decision; Esc is intentionally NOT
        # a free dismiss (a request must be answered) — it picks the
        # first decision as a sensible default so the player is never
        # soft-locked by a panel they can't mouse.
        if (
            self.rpg_player_state is not None
            and self.rpg_player_state.active
        ):
            import rpg_player
            decs = rpg_player.decisions(self.rpg_player_state)
            num_keys = {
                arcade.key.KEY_1: 0, arcade.key.KEY_2: 1,
                arcade.key.KEY_3: 2, arcade.key.KEY_4: 3,
                arcade.key.KEY_5: 4,
            }
            pick = None
            if symbol in num_keys and num_keys[symbol] < len(decs):
                pick = num_keys[symbol]
            elif symbol == arcade.key.ESCAPE and decs:
                pick = 0
            if pick is not None:
                chosen = rpg_player.choose(self.rpg_player_state, pick)
                if chosen is not None:
                    self.apply_rpg_decision(chosen)
                    self.paused = getattr(self, "_rpg_was_paused", False)
            return

        # v0.32: Cutscene runtime player — Esc closes the cutscene
        # (same UX as the editor modals). Enter / Space / Right
        # arrow advance to next slide / OK so keyboard players don't
        # need to mouse-click through long cutscenes.
        if (
            self.cutscene_player_state is not None
            and self.cutscene_player_state.active
        ):
            import cutscene_player
            if symbol == arcade.key.ESCAPE:
                cutscene_player.close(self.cutscene_player_state)
                self.paused = getattr(self, "_cutscene_was_paused", False)
                return
            if symbol in (
                arcade.key.ENTER, arcade.key.RETURN, arcade.key.NUM_ENTER,
                arcade.key.SPACE, arcade.key.RIGHT,
            ):
                cutscene_player.advance(self.cutscene_player_state)
                if not self.cutscene_player_state.active:
                    self.paused = getattr(self, "_cutscene_was_paused", False)
                return
            # Any other key while a cutscene plays is ignored.
            return

        if symbol == arcade.key.ESCAPE:
            # v0.19: dismiss new modals first (barter, unit editor,
            # splash load-map picker) before the buildings editor or
            # ESC menu logic. Order matters: Esc inside a modal should
            # close that modal, not pop the menu.
            if self.show_barter:
                self.show_barter = False
                return
            # v0.35: Esc closes the gold-trade modal.
            if getattr(self, "show_gold_trade", False):
                self.show_gold_trade = False
                return
            # v0.50: Esc closes the commercial-roads window.
            if getattr(self, "show_commercial_roads", False):
                self.show_commercial_roads = False
                return
            # v0.35: Esc closes the nutrients informational panel.
            if getattr(self, "show_nutrients_panel", False):
                self.show_nutrients_panel = False
                return
            # v0.51: Esc closes the finance budget panel.
            if getattr(self, "show_finance_panel", False):
                self.show_finance_panel = False
                return
            # v0.52: Esc closes the commerce-ships panel.
            if getattr(self, "show_commerce_ships_panel", False):
                self.show_commerce_ships_panel = False
                return
            if self.show_unit_editor:
                self.show_unit_editor = False
                return
            # v0.22: Esc closes the diagnostics panel before popping
            # the menu. Same pattern as the other floating panels.
            if self.show_diagnostics:
                self.show_diagnostics = False
                return
            # v0.21: Esc closes the happiness debug overlay before
            # popping the menu. Same pattern as the other floating
            # panels above.
            if self.show_happiness_debug:
                self.show_happiness_debug = False
                return
            if self.show_splash_load_map:
                self.show_splash_load_map = False
                return
            # v0.15: ESC closes the editor first (cancel without save).
            # If the editor's not open, fall through to the existing
            # menu-toggle behaviour.
            if self.editor_open:
                self._editor_close(commit=False)
                return
            # v0.28: same for the trigger editor.
            if self.trigger_editor_open:
                self._trigger_editor_close(commit=False)
                return
            # v0.28: scheduled panel — Esc closes it.
            if self.show_scheduled_panel:
                if self.scheduled_dropdown_open is not None:
                    self.scheduled_dropdown_open = None
                else:
                    self.show_scheduled_panel = False
                return
            # ESC opens the menu instead of quitting (audit/REVIEW item).
            self.show_menu = not self.show_menu
            self.inspected = None
        elif symbol == arcade.key.SPACE:
            self.paused = not self.paused
            log.info("Paused: %s", self.paused)
        elif symbol == arcade.key.H:
            self.show_help = not self.show_help
        elif symbol == arcade.key.P and (modifiers & arcade.key.MOD_CTRL):
            # Post-v0.28: Ctrl+P toggles the frame profiler HUD.
            # Modifier-gated so a plain P stays free for other binds.
            self.show_profiler = not self.show_profiler
            log.info("Profiler %s", "shown" if self.show_profiler else "hidden")
            self._notify(
                f"Profiler: {'on' if self.show_profiler else 'off'}",
                COLOR_GOLD,
            )
        elif symbol == arcade.key.M:
            self.show_minimap = not self.show_minimap
            log.info("Mini-map %s", "shown" if self.show_minimap else "hidden")
        elif symbol == arcade.key.O:
            # Cycle overlays. Off → first → ... → last → Off.
            self.overlay_idx = (self.overlay_idx + 1) % len(OVERLAY_NAMES)
            label = OVERLAY_NAMES[self.overlay_idx][0]
            log.info("Overlay → %s", label)
            self._notify(f"Overlay: {label}", COLOR_GOLD)
        elif symbol == arcade.key.BRACKETLEFT:
            self.overlay_idx = (self.overlay_idx - 1) % len(OVERLAY_NAMES)
            label = OVERLAY_NAMES[self.overlay_idx][0]
            log.info("Overlay ← %s", label)
            self._notify(f"Overlay: {label}", COLOR_GOLD)
        elif symbol == arcade.key.BRACKETRIGHT:
            self.overlay_idx = (self.overlay_idx + 1) % len(OVERLAY_NAMES)
            label = OVERLAY_NAMES[self.overlay_idx][0]
            log.info("Overlay → %s", label)
            self._notify(f"Overlay: {label}", COLOR_GOLD)
        elif symbol == arcade.key.G:
            # v0.6: 'G' opens the long-form graphs window. v0.23
            # enforces single-panel-at-a-time — opening graphs
            # closes any other floating info panel that was up.
            opening = not self.show_graphs
            self.show_graphs = opening
            if opening:
                self._close_other_modal_panels(keep="graphs")
            log.info("Graphs %s", "open" if self.show_graphs else "closed")
        elif symbol == arcade.key.S:
            # v0.8: 'S' opens the statistics panel. Read-only synthesis
            # of registry + economy + walker_manager. Note: WASD pan uses
            # 'S' too, but only while held — toggling is a quick tap, so
            # the conflict is invisible in practice. Any click closes it.
            #
            # v0.23: PgUp / PgDn scroll the panel while it's open;
            # opening also closes any other floating panel so the
            # screen never shows two stacked readouts at once.
            opening = not self.show_stats
            self.show_stats = opening
            if opening:
                self.stats_scroll = 0
                self._close_other_modal_panels(keep="stats")
            log.info("Stats %s", "open" if self.show_stats else "closed")
        elif symbol == arcade.key.J:
            # v0.17: 'J' opens the jobs / employment statistics panel.
            # Distinct from 'S' (general stats) because employment is
            # a focused-enough question to warrant its own dedicated
            # readout — the player asks "where are my workers?" often
            # enough that going through the multi-section stats panel
            # is wrong UX.
            #
            # v0.23: same mutex + PgUp / PgDn treatment as the stats
            # panel. Opening jobs closes whatever else was up.
            opening = not self.show_jobs
            self.show_jobs = opening
            if opening:
                self.jobs_scroll = 0
                self._close_other_modal_panels(keep="jobs")
            log.info("Jobs panel %s", "open" if self.show_jobs else "closed")
        elif symbol == arcade.key.Z:
            # v0.21: 'Z' toggles the happiness equation debug overlay.
            # Surfaces every additive term (base, tax_pen, food_bon,
            # house_bon, emp_bon, water_pen, diversity_bon, h_prod)
            # plus the inputs each was computed from, so the player
            # can see *why* the smoothed happiness is moving where
            # it is. Reads economy.last_happiness_breakdown — empty
            # before the first tick, in which case the panel says so
            # rather than showing zeros that look real.
            opening = not self.show_happiness_debug
            self.show_happiness_debug = opening
            if opening:
                self._close_other_modal_panels(keep="happiness_debug")
            log.info(
                "Happiness debug %s",
                "open" if self.show_happiness_debug else "closed",
            )
        elif symbol == arcade.key.P:
            # v0.44: 'P' toggles the ship route debug overlay (world-space
            # polyline of each ship's cached sea path). Non-modal — it
            # just flips a draw flag, no input capture.
            self.show_ship_paths = not self.show_ship_paths
            log.info(
                "Ship path overlay %s",
                "on" if self.show_ship_paths else "off",
            )
        elif symbol == arcade.key.U:
            # v0.48: 'U' toggles military-unit command mode. While on,
            # left-click/drag selects player units and right-click issues
            # move/attack orders (build/demolish are suspended). Toggling
            # off clears any in-progress drag-box but keeps the selection.
            self.command_mode = not self.command_mode
            self._cmd_drag_start = None
            self._cmd_drag_now = None
            log.info("Command mode %s", "ON" if self.command_mode else "off")
            self._notify(
                "Command mode ON — click/drag to select, right-click to order."
                if self.command_mode else "Command mode off.",
                COLOR_GOLD if self.command_mode else COLOR_GRAY,
            )
        elif (
            getattr(self, "command_mode", False)
            and not self.command_manager.selection.is_empty()
            and symbol in (arcade.key.H, arcade.key.Y, arcade.key.J,
                           arcade.key.K)
        ):
            # v0.49: stance hotkeys, active only in command mode with a
            # selection (so they return before the global bindings see
            # them). Mnemonics chosen from keys free of conflict in this
            # context: H=hold/defend, Y=rest, J=patrol(to a waypoint set
            # by the next right-click is future; here patrols around the
            # current spot), K=retreat-home.
            from commands import Verb
            stance_map = {
                arcade.key.H: (Verb.DEFEND, "defend (hold)"),
                arcade.key.Y: (Verb.REST, "rest"),
                arcade.key.J: (Verb.PATROL, "patrol"),
                arcade.key.K: (Verb.RETREAT, "retreat"),
            }
            verb, label = stance_map[symbol]
            # Clear any stale stance anchors so DEFEND/PATROL re-anchor at
            # the unit's current tile.
            for u in self.command_manager.selection.units:
                for attr in ("_defend_anchor", "_patrol_anchor", "_patrol_leg"):
                    if hasattr(u, attr):
                        delattr(u, attr)
            # PATROL needs a waypoint; default to a few tiles east of each
            # unit if none given (a visible back-and-forth).
            tile = None
            if verb == Verb.PATROL:
                u0 = self.command_manager.selection.units[0]
                tile = (u0.row, u0.col + 4)
            accepted = self.command_manager.issue_verb(verb, tile=tile)
            total = self.command_manager.selection.count()
            log.info("Stance %s: %d/%d units", label, len(accepted), total)
            self._notify(
                f"{label}: {len(accepted)}/{total} units.",
                COLOR_GOLD,
            )
        elif (
            getattr(self, "command_mode", False)
            and arcade.key.KEY_0 <= symbol <= arcade.key.KEY_9
        ):
            # v0.48: 0-9 control groups. Ctrl+digit assigns the current
            # selection to that group; plain digit recalls it.
            digit = symbol - arcade.key.KEY_0
            if modifiers & arcade.key.MOD_CTRL:
                self.command_manager.assign_group(digit)
                log.info("Assigned selection to control group %d", digit)
                self._notify(f"Control group {digit} set "
                             f"({self.command_manager.selection.count()} units).",
                             COLOR_GREEN)
            else:
                if self.command_manager.recall_group(digit):
                    log.info("Recalled control group %d", digit)
                else:
                    log.debug("Control group %d empty", digit)
        elif symbol == arcade.key.D:
            # v0.22: 'D' toggles the production-diagnostics modal.
            # Reads diagnostics.ProductionDiagnostics(...).broken_chains()
            # and renders one row per broken chain — the engine surfaces
            # root causes first (a weapons failure caused by an
            # unstaffed mine reports the *mine*, not the smith). Esc /
            # click closes. Movement keys (WASD) pan the world while
            # open since the panel is informational, not modal —
            # closing it isn't needed to keep playing.
            opening = not self.show_diagnostics
            self.show_diagnostics = opening
            self.diag_scroll = 0
            if opening:
                self._close_other_modal_panels(keep="diagnostics")
            log.info(
                "Diagnostics panel %s",
                "open" if self.show_diagnostics else "closed",
            )
        elif symbol == arcade.key.PAGEDOWN:
            # v0.22 + v0.23: PgUp / PgDn scrolls whichever info panel
            # is currently open. Order matters only for the "no two
            # are open at once" invariant the new mutex enforces;
            # we still check each in case a future feature reopens
            # multiple panels.
            if self.show_diagnostics:
                self.diag_scroll += 8
            elif self.show_jobs:
                self.jobs_scroll += 8
            elif self.show_stats:
                self.stats_scroll += 8
        elif symbol == arcade.key.PAGEUP:
            if self.show_diagnostics:
                self.diag_scroll = max(0, self.diag_scroll - 8)
            elif self.show_jobs:
                self.jobs_scroll = max(0, self.jobs_scroll - 8)
            elif self.show_stats:
                self.stats_scroll = max(0, self.stats_scroll - 8)
        elif symbol == arcade.key.TAB:
            if self.palette_by_cat:
                self.active_cat_idx = (self.active_cat_idx + 1) % len(self.palette_by_cat)
                self.palette_scroll = 0  # v0.19.x: reset on tab cycle
                pal = self._current_palette()
                if pal:
                    self.selected_building = pal[0]
        elif symbol == arcade.key.T:
            self.economy.cycle_tax_rate()
            self._notify(f"Tax rate → {self.economy.tax_rate*100:.0f}%", COLOR_GOLD)
            self._record_action("set_tax", rate=round(float(self.economy.tax_rate), 4),
            )
        elif symbol == arcade.key.R:
            # v0.50: 'R' opens the Commercial roads window (link foreign
            # cities, run persistent auto-trade routes). The tick
            # recorder that used to live on plain 'R' moved to Ctrl+R —
            # it's a diagnostic tool used far less often than commerce,
            # and the brief asks for 'R' = commercial roads.
            if modifiers & arcade.key.MOD_CTRL:
                if self.recorder.is_active:
                    path = self.recorder.stop()
                    self.action_recorder.stop()
                    self._notify(
                        f"Recording stopped → {path.name if path else '?'}",
                        COLOR_GREEN,
                    )
                else:
                    path = self.recorder.start()
                    # v0.55: start the action recorder with the SAME
                    # timestamp the state file used, so the two files
                    # pair up (session_<ts>.jsonl + .actions.jsonl).
                    ts = (
                        path.name[len("session_"):-len(".jsonl")]
                        if path else None
                    )
                    self.action_recorder.start(ts=ts)
                    self._notify(
                        f"Recording → {path.name}",
                        COLOR_GOLD,
                    )
                return
            opening = not getattr(self, "show_commercial_roads", False)
            self.show_commercial_roads = opening
            if opening:
                self._close_other_modal_panels(keep="commercial")
                # Reset to the city-list view each time it opens.
                self.commercial_selected_city = None
                self._commercial_scroll = 0
            log.info(
                "Commercial roads window %s",
                "open" if self.show_commercial_roads else "closed",
            )
        elif symbol == arcade.key.B:
            # v0.19: 'B' opens the international bartering menu.
            # Read STOCK_PRICES from bartering.py and let the player
            # exchange resources at fixed ratios + 100 gold fee.
            opening = not self.show_barter
            self.show_barter = opening
            if opening:
                self._close_other_modal_panels(keep="barter")
            log.info("Barter menu %s", "open" if self.show_barter else "closed")
        elif symbol == arcade.key.C:
            # v0.35: 'C' opens the international goods<->gold trade modal.
            # Sibling of bartering — same price table, but one side of
            # the trade is always gold rather than a second resource.
            opening = not self.show_gold_trade
            self.show_gold_trade = opening
            if opening:
                self._close_other_modal_panels(keep="gold_trade")
            log.info(
                "Gold trade menu %s",
                "open" if self.show_gold_trade else "closed",
            )
        elif symbol == arcade.key.N:
            # v0.35: 'N' opens the nutrients coverage panel — provided
            # vs needed per nutrient with delivery percentage.
            opening = not getattr(self, "show_nutrients_panel", False)
            self.show_nutrients_panel = opening
            if opening:
                self._close_other_modal_panels(keep="nutrients")
            log.info(
                "Nutrients panel %s",
                "open" if self.show_nutrients_panel else "closed",
            )
        elif symbol == arcade.key.DOLLAR or (
            symbol == arcade.key.KEY_4 and (modifiers & arcade.key.MOD_SHIFT)
        ):
            # v0.51: '$' opens the finance budget panel. On most layouts
            # '$' is Shift+4, which some platforms report as DOLLAR and
            # others as KEY_4 + Shift — accept both. (Bare '4' stays the
            # building-palette hotkey; only the shifted form is finance.)
            opening = not getattr(self, "show_finance_panel", False)
            self.show_finance_panel = opening
            if opening:
                self._close_other_modal_panels(keep="finance")
            log.info(
                "Finance panel %s",
                "open" if self.show_finance_panel else "closed",
            )
        elif symbol == arcade.key.EXCLAMATION or (
            symbol == arcade.key.KEY_1 and (modifiers & arcade.key.MOD_SHIFT)
        ):
            # v0.52: '!' opens the commerce-ships panel. Like '$', '!' is
            # Shift+1, reported as EXCLAMATION on some platforms and as
            # KEY_1 + Shift on others — accept both. (Bare '1' stays the
            # building-palette hotkey; only the shifted form is this panel.)
            opening = not getattr(self, "show_commerce_ships_panel", False)
            self.show_commerce_ships_panel = opening
            if opening:
                self._close_other_modal_panels(keep="commerce_ships")
            log.info(
                "Commerce-ships panel %s",
                "open" if self.show_commerce_ships_panel else "closed",
            )
        elif symbol == arcade.key.Y:
            # v0.8: Yield to Caesar — pay the in-flight tribute.
            if self.caesar.current is None:
                self._notify("No request from Caesar.", COLOR_GRAY)
            else:
                ok = self.caesar.try_comply(self.economy, self.diplomacy)
                if ok:
                    self._notify("Caesar is pleased.", COLOR_GREEN)
                    self._record_action("yield_to_caesar", ok=True)
                else:
                    req = self.caesar.current
                    self._notify(
                        f"Cannot afford Caesar's demand: {req['amount']} {req['kind']}",
                        COLOR_RED,
                    )
        elif symbol in (arcade.key.EQUAL, arcade.key.PLUS, arcade.key.NUM_ADD):
            self.speed_multiplier = min(5, self.speed_multiplier + 1)
            log.info("Speed → %dx", self.speed_multiplier)
            self._notify(f"Speed: {self.speed_multiplier}x", COLOR_GRAY)
        elif symbol in (arcade.key.MINUS, arcade.key.NUM_SUBTRACT):
            self.speed_multiplier = max(1, self.speed_multiplier - 1)
            log.info("Speed → %dx", self.speed_multiplier)
            self._notify(f"Speed: {self.speed_multiplier}x", COLOR_GRAY)
        elif symbol == arcade.key.F5:
            save_game(self)
            self._notify("Game saved!", COLOR_GREEN)
        elif symbol == arcade.key.F9:
            if load_game(self):
                self._notify("Game loaded!", COLOR_GREEN)
            else:
                self._notify("No save file found!", COLOR_RED)
        else:
            # Hotkeys 1-9, 0 select the i-th building of the current category.
            num_keys = [
                arcade.key.KEY_1, arcade.key.KEY_2, arcade.key.KEY_3,
                arcade.key.KEY_4, arcade.key.KEY_5, arcade.key.KEY_6,
                arcade.key.KEY_7, arcade.key.KEY_8, arcade.key.KEY_9,
                arcade.key.KEY_0,
            ]
            if symbol in num_keys:
                idx = num_keys.index(symbol)
                pal = self._current_palette()
                if idx < len(pal):
                    self.selected_building = pal[idx]

    def on_key_release(self, symbol: int, modifiers: int) -> None:
        self.keys_held.discard(symbol)

    # ── Helpers ───────────────────────────────────────────────────────────
    def _screen_to_world(self, sx: float, sy: float) -> tuple[float, float]:
        cam_x, cam_y = self.world_camera.position
        zoom = self.world_camera.zoom
        half_w = self.width / 2 / zoom
        half_h = self.height / 2 / zoom
        return cam_x - half_w + sx / zoom, cam_y - half_h + sy / zoom

    def _raw_feature_summary(self, row: int, col: int) -> dict | None:
        """v0.23.x: thin window-level wrapper around
        :py:meth:`GameMap.raw_feature_summary`. Exists so future input
        paths (hover tooltip, scout-overlay) can call one method to get
        the same summary shape the middle-click bubble already renders.
        """
        return self.game_map.raw_feature_summary(row, col)

    def _record_action(self, action: str, **fields) -> None:
        """v0.55: forward a player action to the action recorder, if one
        is wired. Guarded with getattr so gameplay never depends on the
        recorder existing — test stubs and headless harnesses that don't
        construct an ActionRecorder simply skip recording.
        """
        rec = getattr(self, "action_recorder", None)
        if rec is not None:
            rec.note(self, action, **fields)

    def _try_place(self, row: int, col: int) -> None:
        bid = self.selected_building
        bd = self.registry[bid]
        # v0.27: placement brushes. A brush isn't a real grid building;
        # it's a stamp of N copies of another building (typically
        # `road`) in a row or column. Resolve here and delegate to
        # the per-tile placement loop. Brushes work the same way in
        # play mode (cost is gold, gracefully fails on collision)
        # and editor mode (free, gracefully skips occupied tiles).
        if bd.placement_brush:
            self._try_place_brush(row, col, bd)
            return
        # v0.16: in map-editor mode, every placement is free — no
        # treasury cost, no material cost. The `can_place` geometry /
        # feature / water gates still apply (a port still has to
        # touch water, a lumber mill still has to sit on a forest)
        # because those are the *map's* rules, not the budget. The
        # editor is a tool for laying out a scenario, not a cheat
        # mode — letting authors place a port on grass would create
        # malformed maps that crash on load.
        if getattr(self, "app_state", "playing") == "editor":
            if self.game_map.place_building(
                row, col, bid, economy=None, bypass_validation=False,
            ):
                log.info(
                    "Editor: placed %s at (%d,%d) for free", bid, row, col,
                )
            else:
                reason = self._diagnose_placement_failure(row, col, bid)
                self._notify(reason, COLOR_RED)
            return

        if not self.economy.can_afford(bd.cost):
            self._notify(f"Need {bd.cost} gold!", COLOR_RED)
            return
        # v0.13: material cost gate. Civic buildings need stone_blocks,
        # housing needs planks. We surface a specific notification per
        # missing good rather than a generic "Cannot build here!" so the
        # player understands what's missing.
        if bd.material_cost:
            missing = [
                (good, qty - self.economy.resources.get(good, 0))
                for good, qty in bd.material_cost.items()
                if self.economy.resources.get(good, 0) < qty
            ]
            if missing:
                short = ", ".join(f"{int(short_qty)} {g}" for g, short_qty in missing)
                self._notify(f"Need {short}!", COLOR_RED)
                return
        # Pass the economy into place_building so material_cost is
        # validated AND deducted atomically. If can_place fails for any
        # reason (no road, no feature, water, etc.), no materials are
        # deducted because place_building bails before the deduction.
        if self.game_map.place_building(row, col, bid, economy=self.economy):
            self.economy.spend(bd.cost)
            log.info("Built %s at (%d,%d) for %d dn", bid, row, col, bd.cost)
            self._record_action("place", building=bid, row=row, col=col,
                cost=int(bd.cost), ok=True,
            )
        else:
            # v0.13: distinguish failure reasons. The most common ones a
            # player will hit are feature gating (lumber mill not on
            # forest, mine not on a vein) and water-adjacency (reservoir
            # not next to a lake). The check is cheap and runs after
            # can_place already returned False.
            reason = self._diagnose_placement_failure(row, col, bid)
            self._notify(reason, COLOR_RED)

    def _try_place_brush(self, row: int, col: int, bd) -> None:
        """v0.27: stamp N copies of a brush's `stamp_id` building in a
        row or column starting at (row, col).

        Behaviour:
          * Cost is the brush's ``bd.cost`` field (typically
            length × stamp_cost). Player path checks treasury once
            up-front — partial placement does NOT refund unused
            tiles, mirroring how the player would expect a
            "5 roads in one click" button to behave: you committed
            to a strip.
          * Stops at the first collision / out-of-bounds tile.
            Already-placed tiles stay placed. This matches the
            Caesar 3 "drag a road" interaction the player asked
            for: forgiving rather than all-or-nothing.
          * In editor mode (``app_state == 'editor'``) every tile
            is free; we still stop at collision but don't deduct.
          * Direction comes from the brush spec:
              - ``axis: 'h'`` walks +col (left → right)
              - ``axis: 'v'`` walks +row (top → bottom)
            The arrow glyph in the building's display name (`→` / `↓`)
            tells the player which way the stamp grows.
        """
        spec = bd.placement_brush
        stamp_id = spec.get("stamp_id", "road")
        axis = spec.get("axis", "h")
        length = int(spec.get("length", 1))
        if stamp_id not in self.registry:
            self._notify(f"Brush stamp '{stamp_id}' not found", COLOR_RED)
            return
        # Treasury / cost gate is identical to the regular path —
        # but applied once for the whole brush.
        in_editor = getattr(self, "app_state", "playing") == "editor"
        if not in_editor:
            if not self.economy.can_afford(bd.cost):
                self._notify(f"Need {bd.cost} gold!", COLOR_RED)
                return
        # Walk N tiles in the chosen axis. Stop on the first failure
        # but keep what we placed.
        placed = 0
        for i in range(length):
            if axis == "v":
                rr, cc = row + i, col
            else:
                rr, cc = row, col + i
            economy_arg = None if in_editor else self.economy
            ok = self.game_map.place_building(rr, cc, stamp_id, economy=economy_arg)
            if not ok:
                break
            placed += 1
        if placed == 0:
            # Surface a useful failure for the first-tile rejection
            # (no partial cost was committed at this point either —
            # the treasury deduction below is gated on placed > 0).
            reason = self._diagnose_placement_failure(row, col, stamp_id)
            self._notify(reason, COLOR_RED)
            return
        if not in_editor:
            # Charge the full brush cost up-front. Rationale: the
            # player paid for a strip; partial placement still costs
            # the planning effort. If we wanted to be strictly fair
            # we'd charge `placed * stamp.cost`; we instead respect
            # the brush's own published `bd.cost`. This also keeps
            # the cost a single deterministic number the HUD can
            # show in the palette button.
            self.economy.spend(bd.cost)
        log.info(
            "Brush %s @ (%d,%d) axis=%s length=%d → placed %d of %d",
            bd.id if hasattr(bd, "id") else "?", row, col, axis, length, placed, length,
        )
        if placed < length:
            self._notify(
                f"Placed {placed}/{length} {stamp_id} tiles (blocked).",
                COLOR_GOLD,
            )

    def _diagnose_placement_failure(
        self, row: int, col: int, bid: str,
    ) -> str:
        """v0.13: produce a short human-readable reason for why placement
        was refused. Mirrors the checks in GameMap.can_place but reports
        the *first* failed condition rather than yes/no.

        v0.27: bounds come from the live game_map (rows/cols) instead
        of hardcoded 30/40. Also reports the new bridge rule ("Must
        sit fully on water") and detects placement-brush misuse.
        """
        bd = self.registry.get(bid)
        if bd is None:
            return "Unknown building"
        # v0.27: placement brushes can't be placed directly. This is
        # a misuse — the caller should have routed to _try_place_brush.
        if bd.placement_brush:
            return "Brush: use _try_place_brush"
        gm = self.game_map
        rows = getattr(gm, "rows", 30)
        cols = getattr(gm, "cols", 40)
        # Out of bounds or overlap.
        for rr, cc in gm._iter_footprint(row, col, bd):
            if not (0 <= rr < rows and 0 <= cc < cols):
                return "Out of bounds"
            if gm.grid[rr][cc] is not None:
                return "Cell occupied"
        # v0.27: bridges need every footprint tile on water.
        if bd.bridges_water:
            for rr, cc in gm._iter_footprint(row, col, bd):
                if not (0 <= rr < rows and 0 <= cc < cols):
                    return "Out of bounds"
                if not gm.is_water(rr, cc):
                    return "Bridge must sit fully on water"
        # Feature gating.
        if bd.needs_feature:
            ok = any(
                gm.has_feature_in_footprint(row, col, bd, f)
                for f in bd.needs_feature
            )
            if not ok:
                names = " or ".join(
                    f.replace("_", " ") for f in bd.needs_feature
                )
                return f"Need {names} here"
        if bd.needs_water_adjacent:
            if not gm.has_water_neighbour(row, col, bd):
                return "Must touch water"
        if bd.needs_terrain == "water":
            return "Must touch water tile"
        return "Cannot build here"

    def _add_next_trade_route(self) -> None:
        existing = {r.name for r in self.trade_manager.routes}
        # v0.8: trade routes cost money + diplomacy. Check both before
        # picking which route to add — otherwise we'd silently fail past
        # the player. The two costs are read from balance.py so a modder
        # can move them.
        cost_m = self.balance.trade_route_cost_money
        cost_d = self.balance.trade_route_cost_diplomacy
        if self.economy.treasury < cost_m:
            self._notify(
                f"Need {cost_m} dn to open a route (have {self.economy.treasury:.0f})",
                COLOR_RED,
            )
            return
        if not self.diplomacy.can_afford(cost_d):
            self._notify(
                f"Need {cost_d} diplomacy points (have {self.diplomacy.points:.0f})",
                COLOR_RED,
            )
            return
        for tr in TRADE_ROUTES:
            if tr["name"] not in existing:
                # Spend resources only after we know there's a route to add.
                self.economy.treasury -= cost_m
                self.diplomacy.spend(cost_d)
                self.trade_manager.add_route(
                    TradeRoute(tr["name"], tr["goods"], tr["rate"], tr["profit"])
                )
                self._notify(
                    f"Trade route: {tr['name']}!  (-{cost_m} gold, -{cost_d} dip)",
                    COLOR_GREEN,
                )
                return
        self._notify("All routes established.", COLOR_GRAY)

    def _notify(self, msg: str, color: tuple, duration: int = 50) -> None:
        # v0.17: bumped the default expiry from 25 → 50 ticks (about
        # 25 seconds at 2 ticks/sec) so notifications stay visible
        # long enough for the player to actually read them. Important
        # for confirmations like "Game loaded!" where the player
        # specifically clicked an action and wants to see it land.
        self.notifications.append((msg, color, self.game_time + duration))
