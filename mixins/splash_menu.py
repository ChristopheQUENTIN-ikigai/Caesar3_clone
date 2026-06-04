"""Splash menu and new-game bootstrap — every `_splash_*` action plus
the shared `_reset_to_empty_world` / `_start_new_game` helpers.
Also covers the splash sub-screens (Load map, Load scenario, Event
editor info) and the related `_menu_open_*` entry points that share
state with the splash. Extracted from game_window.py for clarity
(~800 lines)."""
from __future__ import annotations

import logging

import arcade

from balance import BALANCE
from caesar import CaesarRequestManager
from constants import (
    COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_RED,
    COLOR_UI_BORDER, COLOR_WHITE,
    DATA_DIR,
    # v0.52: shown on the "Mapping keys keyboards" overlay so the player
    # can see exactly where saves / maps / config live on disk, and the
    # key bindings, without launching a game first.
    HELP_TEXT, MAPS_DIR, SAVES_DIR,
    TRADE_ROUTES,
)
from decay import DecayManager
from diplomacy import DiplomacyTracker
from economy import EconomyManager
from game_map import GameMap
from house_evolution import HouseEvolution
from pathfinding import Pathfinder
from rebellion import RebellionTracker
from road_network import RoadNetwork
from saveload import load_game
from services import ServiceMap
from storage import Storage
from trade import TradeRoute, TradeRouteManager
from walkers import WalkerManager

log = logging.getLogger("caesar3.window")

class SplashMenuMixin:
    """SplashMenuMixin — see module docstring."""

    # ══════════════════════════════════════════════════════════════════════
    #  SPLASH SCREEN  (v0.7)
    # ══════════════════════════════════════════════════════════════════════
    # Layout constants — tuned to feel right at 1280x800 but computed from
    # self.width/height so smaller screens get a proportional menu.
    SPLASH_BTN_W = 280
    SPLASH_BTN_H = 36
    SPLASH_BTN_GAP = 8
    SPLASH_PANEL_PAD = 24

    def _splash_button_rects(self) -> list[tuple[float, float, float, float]]:
        """Return ``[(left, right, bottom, top), ...]`` for each splash button.

        Centred horizontally; stacked vertically below the title; the
        whole stack is anchored to the vertical centre so tall screens
        don't push it offscreen on either edge.
        """
        n = len(self._splash_actions)
        total_h = n * self.SPLASH_BTN_H + (n - 1) * self.SPLASH_BTN_GAP
        cx = self.width / 2
        # Stack starts a touch above centre so the title has breathing room.
        top0 = self.height / 2 + total_h / 2
        rects: list[tuple[float, float, float, float]] = []
        for i in range(n):
            t = top0 - i * (self.SPLASH_BTN_H + self.SPLASH_BTN_GAP)
            b = t - self.SPLASH_BTN_H
            l = cx - self.SPLASH_BTN_W / 2
            r = cx + self.SPLASH_BTN_W / 2
            rects.append((l, r, b, t))
        return rects

    def _splash_button_at(self, x: int, y: int) -> int | None:
        for i, (l, r, b, t) in enumerate(self._splash_button_rects()):
            if l <= x <= r and b <= y <= t:
                return i
        return None

    def _draw_splash(self) -> None:
        # Background: tile the splash JPG to fill the window. We scale-to-fit
        # by drawing a single full-window textured rectangle. If the asset
        # is missing we fall back to the dark UI background that the rest
        # of the chrome uses, so the menu still renders.
        bg = self.textures.ui("splash")
        if bg is not None:
            arcade.draw_texture_rect(
                bg,
                arcade.LBWH(0, 0, self.width, self.height),
            )
            # Slightly darken the right side so light-grey buttons stay
            # legible against the bright marble. Cheap full-screen scrim.
            arcade.draw_lrbt_rectangle_filled(
                0, self.width, 0, self.height, (0, 0, 0, 90),
            )
        else:
            arcade.draw_lrbt_rectangle_filled(
                0, self.width, 0, self.height, (30, 25, 20),
            )

        # Title block.
        self.txt_splash_title.x = self.width / 2
        self.txt_splash_title.y = self.height - 60
        self.txt_splash_title.draw()
        self.txt_splash_subtitle.x = self.width / 2
        self.txt_splash_subtitle.y = self.height - 96
        self.txt_splash_subtitle.draw()
        self.txt_splash_version.draw()

        # Buttons.
        for i, ((l, r, b, t), label_text) in enumerate(
            zip(self._splash_button_rects(), self.txt_splash_buttons)
        ):
            hovered = (self.splash_hover == i)
            fill = (90, 70, 50, 230) if hovered else (50, 42, 35, 220)
            border = COLOR_GOLD if hovered else COLOR_UI_BORDER
            arcade.draw_lrbt_rectangle_filled(l, r, b, t, fill)
            arcade.draw_lrbt_rectangle_outline(l, r, b, t, border, 2 if hovered else 1)
            label_text.x = (l + r) / 2
            label_text.y = b + self.SPLASH_BTN_H / 2 - 7
            label_text.color = COLOR_GOLD if hovered else COLOR_WHITE
            label_text.draw()

        # Credits panel sits over everything when toggled.
        if self.show_credits:
            arcade.draw_lrbt_rectangle_filled(
                100, self.width - 100, 100, self.height - 100,
                (20, 15, 10, 240),
            )
            arcade.draw_lrbt_rectangle_outline(
                100, self.width - 100, 100, self.height - 100,
                COLOR_GOLD, 2,
            )
            self.txt_credits_body.x = self.width / 2
            self.txt_credits_body.y = self.height / 2
            self.txt_credits_body.draw()

        # v0.52: "Mapping keys keyboards" overlay — key bindings + the
        # on-disk folders the game saves to / loads from. Sits over
        # everything (same tier as Credits). Toggled by the splash button
        # / Esc. We build the two Text objects lazily and cache them so a
        # repeated open doesn't re-allocate every frame.
        if self.show_keymap:
            arcade.draw_lrbt_rectangle_filled(
                60, self.width - 60, 60, self.height - 60,
                (18, 14, 10, 244),
            )
            arcade.draw_lrbt_rectangle_outline(
                60, self.width - 60, 60, self.height - 60,
                COLOR_GOLD, 2,
            )
            # Title.
            if not hasattr(self, "_txt_keymap_title"):
                self._txt_keymap_title = arcade.Text(
                    "KEY BINDINGS & FILE LOCATIONS",
                    self.width / 2, self.height - 92,
                    COLOR_GOLD, 18, anchor_x="center", bold=True,
                )
            self._txt_keymap_title.x = self.width / 2
            self._txt_keymap_title.y = self.height - 92
            self._txt_keymap_title.draw()

            # Key bindings (left column) — reuse the shared HELP_TEXT so
            # the splash overlay and the in-game 'H' help never drift.
            if not hasattr(self, "_txt_keymap_body"):
                self._txt_keymap_body = arcade.Text(
                    HELP_TEXT, 0, 0, COLOR_WHITE, 11,
                    anchor_x="left", anchor_y="top", multiline=True,
                    width=self.width / 2 - 100,
                )
            self._txt_keymap_body.x = 100
            self._txt_keymap_body.y = self.height - 130
            self._txt_keymap_body.draw()

            # File locations (right column) — resolve the *absolute*
            # folders so the player can find them in their file manager.
            # str(Path) is platform-correct; we show the dirs the game
            # actually reads/writes (saves, maps) plus the data root.
            paths_text = (
                "FILE LOCATIONS\n"
                "─────────────────────────────\n"
                "These are the folders the game\n"
                "saves to and loads from:\n\n"
                f"Saved games:\n{SAVES_DIR}\n\n"
                f"Custom maps:\n{MAPS_DIR}\n\n"
                f"Data / config root:\n{DATA_DIR}\n\n"
                "(Paths are created on first use.)"
            )
            if not hasattr(self, "_txt_keymap_paths"):
                self._txt_keymap_paths = arcade.Text(
                    paths_text, 0, 0, COLOR_WHITE, 11,
                    anchor_x="left", anchor_y="top", multiline=True,
                    width=self.width / 2 - 100,
                )
            # Refresh the text every draw — the resolved path string is
            # cheap and this keeps it correct if the working dir changed.
            self._txt_keymap_paths.text = paths_text
            self._txt_keymap_paths.x = self.width / 2 + 40
            self._txt_keymap_paths.y = self.height - 130
            self._txt_keymap_paths.draw()

            # Footer hint.
            if not hasattr(self, "_txt_keymap_hint"):
                self._txt_keymap_hint = arcade.Text(
                    "Press Esc or click anywhere to close",
                    self.width / 2, 80,
                    COLOR_GRAY, 11, anchor_x="center", italic=True,
                )
            self._txt_keymap_hint.x = self.width / 2
            self._txt_keymap_hint.y = 80
            self._txt_keymap_hint.draw()

    def _splash_new_game(self) -> None:
        """Start a fresh game on the default scenario. Wired to the
        "New game" splash button. Delegates to ``_start_new_game``,
        which tears down any existing world (including a Gallic/skirmish
        starter the player may have loaded earlier) and re-seeds the
        default starter layout + economy."""
        log.info("Splash → New game")
        self._start_new_game(scenario="default")

    def _splash_new_game_gallic(self) -> None:
        """v0.15: New game in the Gallic War scenario. Wipes the
        default starter (placed by setup()) and lays the Gallic
        starter layout instead. The economy is also reset so a player
        who clicks "New game" then comes back to splash and clicks
        "New game (Gallic War)" doesn't carry leftover state."""
        log.info("Splash → New game (Gallic War)")
        self._start_new_game(scenario="gallic_war")

    def _splash_skirmish(self) -> None:
        """v0.30: Skirmish — combat-only sandbox.

        Boots a fresh game on the skirmish starter layout (one of each
        military building plus supporting infrastructure), pre-staffs
        every garrison to ``SKIRMISH_UNITS_PER_BUILDING`` units, seeds
        the player with enough treasury and materials to repair /
        rebuild during a long defence, and primes a per-tick wave
        schedule so barbarian invasions arrive at predictable
        intervals. The actual wave dispatch lives in
        ``_skirmish_tick`` and is called from ``_game_tick`` while
        ``self.scenario == "skirmish"``.
        """
        log.info("Splash → Skirmish")
        self._start_new_game(scenario="skirmish")

    def _start_new_game(self, scenario: str) -> None:
        """v0.15: factor the "begin a fresh game" sequence so both the
        default and the Gallic War splash buttons can use it. Setup()
        lays the default starter unconditionally at boot, but the
        player might press the Gallic War button after that — so we
        wipe and re-seed when needed.

        Doing nothing on `scenario == "default"` would be the cheap
        path, but that conflicts with the gallic-then-default flow
        (player picks Gallic, doesn't like it, comes back to splash,
        picks default — we'd still be on the gallic map). Resetting
        every time is the simpler, correct rule.
        """
        from balance import BALANCE
        from economy import EconomyManager
        self.scenario = scenario
        # Tear down the existing world. Building grid + state, terrain
        # features (depletion is per-game), economy resources/treasury,
        # walkers, road & service maps. Caesar/diplomacy/rebellion/decay
        # are reset to their fresh-game defaults.
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
        # Re-seed.
        self._place_starter_city()
        tr = TRADE_ROUTES[0]
        self.trade_manager.add_route(
            TradeRoute(tr["name"], tr["goods"], tr["rate"], tr["profit"])
        )
        # Reset transient game state.
        self.tick_accumulator = 0.0
        self.game_time = 0
        self.year = 1
        self.month = 1
        self.notifications.clear()
        self.paused = False
        self.show_help = False
        self.show_menu = False
        self.inspected = None
        self.app_state = "playing"
        self.show_credits = False
        self._notify(f"New game: {scenario.replace('_', ' ').title()}", COLOR_GOLD)
        # v0.30: skirmish post-setup. Must run AFTER _place_starter_city
        # so the garrison buildings exist on the map, AFTER the economy
        # is reset (so seeded materials persist), and AFTER event_manager
        # exists so we can disable ambient events.
        if scenario == "skirmish":
            self._skirmish_init()

    def _splash_load_game(self) -> None:
        log.info("Splash → Load game")
        if load_game(self):
            self.app_state = "playing"
            self.show_credits = False
            self._notify("Game loaded!", COLOR_GREEN)
        else:
            # Stay on splash so the player can try a different option.
            self._notify("No save file found!", COLOR_RED)

    def _splash_credits(self) -> None:
        log.info("Splash → Credits")
        self.show_credits = not self.show_credits

    def _splash_keymap(self) -> None:
        """v0.52: Splash → "Mapping keys keyboards". Toggles a read-only
        overlay over the splash screen showing every key binding (the
        shared in-game HELP_TEXT) and — per the brief — the on-disk
        folders the game saves to / loads from (saves, maps, data root).
        Same toggle pattern as Credits; Esc or another click closes it."""
        log.info("Splash → Mapping keys keyboards")
        self.show_keymap = not self.show_keymap

    def _splash_exit(self) -> None:
        log.info("Splash → Exit")
        self.close()

    # ── v0.19: new splash/menu entrypoints ───────────────────────────────
    def _splash_play_custom_map(self) -> None:
        """Splash → Play custom map. Opens a list of files in
        ``./data/maps`` and starts a new game from whichever the
        player picks. Reuses the editor's load picker layout — same
        modal, same hit-testing — but the click action is "load
        and switch to playing" rather than "load into the editor".
        """
        log.info("Splash → Play custom map")
        self.show_splash_load_map = True

    # ── v0.36: Load scenario + Event editor splash entry points ──────
    def _splash_load_scenario(self) -> None:
        """Splash → Load scenario. Opens a picker listing every
        subdirectory of ``data/scenarios/`` that ships a ``map.json``.
        Click a row → tear down to a clean world, load that scenario's
        map (which pulls in its libraries via ``scenario_libraries``).

        Sibling of ``_splash_play_custom_map``; the difference is
        scope: a custom map is bare terrain + buildings, a scenario
        bundles cutscenes / RPG requests / events / trigger wiring
        alongside the map.
        """
        log.info("Splash → Load scenario")
        self.show_splash_load_scenario = True

    def _splash_open_event_editor(self) -> None:
        """Splash → Triggers editor.

        Wires the v0.37 ``triggers_editor`` module into the splash
        menu. The function name (`_splash_open_event_editor`) is
        retained for backwards-compatibility with the splash button
        list and existing tests; the UI label and modal are the
        Triggers editor — see the responsibility-split note on the
        splash menu entry.

        Authors editing the ``triggers`` block on a scenario's
        ``map.json``: the editor lets you cycle ``when.kind``, edit
        flag/event/value args inline, add/remove/reorder ``do``
        effects, and save atomically (tmp + os.replace) without
        touching the rest of map.json. The runtime
        ``TriggerManager`` re-reads on the next scenario load —
        save here, then "Play scenario" to see the new wiring.
        """
        log.info("Splash → Triggers editor")
        # Boot a clean state so the editor's open-modal flow has a
        # backing window without inheriting any half-set-up world
        # state from earlier in the session. Same pattern as the
        # RPG request editor and the cutscene editor.
        self._reset_to_empty_world(switch_state="playing")
        self.paused = True
        self.show_menu = False
        import triggers_editor
        if self.triggers_editor_state is None:
            self.triggers_editor_state = triggers_editor.TriggersEditorState()
        triggers_editor.open_editor(self, self.triggers_editor_state)

    def _splash_play_scenario_path(self, rel_path: str) -> None:
        """Pick of a scenario from the splash load-scenario picker.

        ``rel_path`` is data-dir-relative (e.g.
        ``scenarios/tribute_crisis/map.json``) — same shape
        ``mapfile.list_scenarios`` returns. We resolve it under
        ``DATA_DIR``, tear down to a clean world, and load via the
        v0.36 ``load_map_path`` entry point (the legacy ``load_map``
        only handles ``MAPS_DIR``-rooted filenames).
        """
        from constants import DATA_DIR
        from mapfile import load_map_path
        full = DATA_DIR / rel_path
        log.info("Splash → Play scenario %s", full)
        self._reset_to_empty_world(switch_state="playing")
        ok = load_map_path(self, full)
        if ok:
            self._notify(f"Loaded scenario: {rel_path}", COLOR_GREEN)
            self.show_splash_load_scenario = False
            self.paused = False
        else:
            self._notify(f"Failed to load {rel_path}", COLOR_RED)

    def _splash_open_buildings_editor(self) -> None:
        """Splash → Buildings editor. Boots into the same modal the
        in-game ESC menu shows. We need a backing game state for
        the registry hot-reload to mutate, so we tear down to an
        empty world (same as Map editor) and pop the editor over it.
        """
        log.info("Splash → Buildings editor")
        # Bootstrap a clean state so the editor's save-and-reload
        # has somewhere to land.
        self._reset_to_empty_world(switch_state="playing")
        self.paused = True
        self._menu_open_editor()

    def _splash_open_unit_editor(self) -> None:
        """Splash → Unit editor.

        v0.22 ships this as a live army roster (see
        ``_draw_unit_editor``). v0.23 plans to fold it into a
        balance-tuning panel mirroring the buildings editor — but
        that work is queued for a follow-up drop. For now the panel
        keeps its v0.22 behaviour.
        """
        log.info("Splash → Unit editor")
        self._reset_to_empty_world(switch_state="playing")
        self.paused = True
        self.show_menu = False
        self._open_unit_editor()

    def _splash_open_trigger_editor(self) -> None:
        """Splash → Trigger editor.

        v0.28. Loads ``data/events.json`` into a working list and
        opens the editor modal. The editor mirrors the buildings-
        editor structure (scrollable left-column list + per-entry
        editor pane + footer Cancel/Save) but operates on event dicts
        rather than building entries. Save round-trips back to disk
        via the same atomic tmp+rename pattern.

        Bootstraps a clean empty world so the running event manager
        has somewhere to reload the registry into when the user
        clicks Save (same pattern as the buildings editor).
        """
        log.info("Splash → Trigger editor")
        self._reset_to_empty_world(switch_state="playing")
        self.paused = True
        self.show_menu = False
        self._open_trigger_editor()

    def _open_trigger_editor(self) -> None:
        """Open the trigger editor, snapshotting events.json into the
        editor's working list. Can be called from the splash button
        or from any future in-game entry point — same setup either
        way.
        """
        from events import load_events
        try:
            evs = load_events()
        except Exception as e:  # noqa: BLE001
            log.exception("Trigger editor: load_events failed")
            self._notify(f"Load failed: {e}", COLOR_RED)
            return
        # load_events tuple-ifies color fields; the editor keeps them
        # as lists so the per-component (R/G/B) sliders can mutate
        # them in place. We re-tuple on save.
        self.trigger_editor_events = []
        for ev in evs:
            ev = dict(ev)
            if isinstance(ev.get("color"), tuple):
                ev["color"] = list(ev["color"])
            # Normalise: every entry has effects + modifiers dicts so
            # the editor can poke at them without dict-vs-None checks.
            ev.setdefault("effects", {})
            ev.setdefault("modifiers", {})
            ev["effects"] = dict(ev.get("effects") or {})
            ev["modifiers"] = dict(ev.get("modifiers") or {})
            ev.setdefault("pop", 0)
            ev.setdefault("happy", 0)
            ev.setdefault("duration_ticks", 0)
            ev.setdefault("msg", "")
            ev.setdefault("color", [200, 200, 200])
            self.trigger_editor_events.append(ev)
        self.trigger_editor_selected_idx = 0 if self.trigger_editor_events else -1
        self.trigger_editor_scroll = 0
        self.trigger_editor_dirty = False
        self.trigger_editor_text_field = None
        self._trigger_editor_text_buffer = ""
        self.trigger_editor_picker_open = None
        self.trigger_editor_open = True
        log.info(
            "Trigger editor: opened with %d events", len(self.trigger_editor_events),
        )

    # ── v0.31: RPG request editor — splash entry ─────────────────────
    def _splash_open_rpg_request_editor(self) -> None:
        """Splash → RPG request editor.

        v0.31. Loads ``data/rpg_requests.json`` into a working list
        and opens the editor modal. Mirrors the trigger editor's
        splash entry path: reset the world to a safe empty state,
        freeze the simulation, then hand off to the editor module.

        The editor itself lives in ``rpg_request_editor.py`` — see
        the comment on ``self.rpg_request_editor_state`` in __init__
        for why we extracted it rather than inlining like the other
        editors.
        """
        log.info("Splash → RPG request editor")
        self._reset_to_empty_world(switch_state="playing")
        self.paused = True
        self.show_menu = False
        import rpg_request_editor
        if self.rpg_request_editor_state is None:
            self.rpg_request_editor_state = rpg_request_editor.RpgRequestEditorState()
        rpg_request_editor.open_editor(self, self.rpg_request_editor_state)

    # ── v0.32: Cutscene cinematic editor — splash entry ──────────────
    def _splash_open_cutscene_editor(self) -> None:
        """Splash → Cutscene cinematic editor.

        v0.32. Loads ``data/cutscenes.json`` into the editor's working
        list and opens the modal. Mirrors the RPG request editor's
        splash entry path: reset the world to a safe empty state,
        freeze the simulation, then hand off to the editor module.
        Implementation in ``cutscene_editor.py``.
        """
        log.info("Splash → Cutscene cinematic editor")
        self._reset_to_empty_world(switch_state="playing")
        self.paused = True
        self.show_menu = False
        import cutscene_editor
        if self.cutscene_editor_state is None:
            self.cutscene_editor_state = cutscene_editor.CutsceneEditorState()
        cutscene_editor.open_editor(self, self.cutscene_editor_state)

    def _reset_to_empty_world(self, switch_state: str = "playing") -> None:
        """Tear down the world without seeding a starter city.

        Used by splash entry points that need a clean game state but
        skip ``_place_starter_city``. Mirrors the front half of
        ``_start_new_game`` — keep these in sync if either grows
        new managers.
        """
        from economy import EconomyManager
        self.scenario = "blank"
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
        self.tick_accumulator = 0.0
        self.game_time = 0
        self.year = 1
        self.month = 1
        self.notifications.clear()
        self.app_state = switch_state
        self.show_credits = False
        # v0.36: dismiss splash overlays — a state change to "playing"
        # makes them inappropriate, and re-entering splash later should
        # start with all modals closed.
        self.show_splash_load_map = False
        self.show_splash_load_scenario = False
        self.show_splash_event_editor = False
        self.show_menu = False
        self.inspected = None

    def _menu_open_map_editor(self) -> None:
        """ESC menu → Map editor. Same teardown as the splash button
        but invokable mid-game. Players who want to author a map
        from a layout they've built can now do so without quitting."""
        log.info("Menu → Map editor")
        self.show_menu = False
        self._splash_open_map_editor()

    def _menu_open_unit_editor(self) -> None:
        log.info("Menu → Unit editor")
        self.show_menu = False
        self._close_other_modal_panels(keep="unit_editor")
        self._open_unit_editor()

    def _menu_open_barter(self) -> None:
        log.info("Menu → Bartering")
        self.show_menu = False
        self._close_other_modal_panels(keep="barter")
        self.show_barter = True


    # ── v0.19: Splash → Play custom map ───────────────────────────
    def _draw_splash_load_map(self) -> None:
        """Modal list of available maps over the splash screen.

        Same modal pattern the map editor uses, but exposed before
        any game has started (the brief asks for "Play custom map"
        next to "New game"). Click a row to play that map; click
        outside or press Esc to dismiss.
        """
        self._splash_load_map_rects = []
        from mapfile import list_maps
        files = list_maps()
        cx, cy = self.width / 2, self.height / 2
        pw = 520
        ph = max(220, 80 + 28 * max(1, min(len(files), 14)))
        l, r = cx - pw / 2, cx + pw / 2
        b, t = cy - ph / 2, cy + ph / 2
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 200),
        )
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_splash_picker"):
            self._txt_splash_picker: dict[str, arcade.Text] = {}

        def line(key: str, text: str, x: float, y: float,
                 size: int = 11, color=COLOR_WHITE,
                 bold: bool = False, anchor_x: str = "left") -> None:
            obj = self._txt_splash_picker.get(key)
            if obj is None:
                obj = arcade.Text(
                    text, x, y, color, size, bold=bold, anchor_x=anchor_x,
                )
                self._txt_splash_picker[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            obj.draw()

        line("title", "PLAY CUSTOM MAP", cx, t - 28,
             size=14, color=COLOR_GOLD, bold=True, anchor_x="center")
        line("hint",
             "Click a map to play it. Esc to cancel.",
             cx, t - 48, size=10, color=COLOR_GRAY, anchor_x="center")

        if not files:
            line("empty",
                 "(no maps in ./data/maps yet — open Map editor and Save one first)",
                 cx, cy, size=11, color=COLOR_GRAY, anchor_x="center")
            return

        row_h = 24
        for i, fname in enumerate(files):
            row_t = t - 70 - i * row_h
            row_b = row_t - row_h
            if row_b < b + 12:
                break
            arcade.draw_lrbt_rectangle_filled(
                l + 8, r - 8, row_b, row_t, (45, 38, 30),
            )
            arcade.draw_lrbt_rectangle_outline(
                l + 8, r - 8, row_b, row_t, COLOR_UI_BORDER, 1,
            )
            line(f"row_{i}", f"  {fname}",
                 l + 12, row_b + 6, size=11, color=COLOR_WHITE)
            self._splash_load_map_rects.append(
                (l + 8, r - 8, row_b, row_t, f"pick:{fname}")
            )

    def _splash_play_map_file(self, filename: str) -> None:
        """Pick of a map from the splash load-map picker. Tear down
        to a clean game state, load the map, and switch to "playing".
        Mirrors ``_start_new_game`` but seeds from disk."""
        log.info("Splash → Play map %s", filename)
        from mapfile import load_map
        self._reset_to_empty_world(switch_state="playing")
        ok = load_map(self, filename)
        if ok:
            self._notify(f"Loaded: {filename}", COLOR_GREEN)
            self.show_splash_load_map = False
            self.paused = False
        else:
            self._notify(f"Failed to load {filename}", COLOR_RED)

    # ── v0.36: Splash → Load scenario picker ───────────────────────
    def _draw_splash_load_scenario(self) -> None:
        """Modal list of available scenarios over the splash screen.

        Same layout as ``_draw_splash_load_map`` but enumerates
        ``data/scenarios/<id>/map.json`` bundles via
        ``mapfile.list_scenarios``. Each row shows the friendly name
        (from the map JSON's ``name`` field) above the scenario id
        in small text. Click a row to play; click outside or Esc
        dismisses.
        """
        self._splash_load_scenario_rects = []
        from mapfile import list_scenarios
        scenarios = list_scenarios()
        cx, cy = self.width / 2, self.height / 2
        pw = 560
        ph = max(220, 80 + 40 * max(1, min(len(scenarios), 10)))
        l, r = cx - pw / 2, cx + pw / 2
        b, t = cy - ph / 2, cy + ph / 2
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 200),
        )
        arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
        arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

        if not hasattr(self, "_txt_splash_scenario_picker"):
            self._txt_splash_scenario_picker: dict[str, arcade.Text] = {}

        def line(key: str, text: str, x: float, y: float,
                 size: int = 11, color=COLOR_WHITE,
                 bold: bool = False, anchor_x: str = "left") -> None:
            obj = self._txt_splash_scenario_picker.get(key)
            if obj is None:
                obj = arcade.Text(
                    text, x, y, color, size, bold=bold, anchor_x=anchor_x,
                )
                self._txt_splash_scenario_picker[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            obj.draw()

        line("title", "LOAD SCENARIO", cx, t - 28,
             size=14, color=COLOR_GOLD, bold=True, anchor_x="center")
        line("hint",
             "Click a scenario to play it. Esc to cancel.",
             cx, t - 48, size=10, color=COLOR_GRAY, anchor_x="center")

        if not scenarios:
            line("empty",
                 "(no scenarios in ./data/scenarios yet — see "
                 "data/scenarios/<id>/map.json convention)",
                 cx, cy, size=11, color=COLOR_GRAY, anchor_x="center")
            return

        # Taller rows than the map picker so each entry can show the
        # friendly name on top and the scenario id underneath.
        row_h = 40
        for i, sc in enumerate(scenarios):
            row_t = t - 70 - i * row_h
            row_b = row_t - row_h
            if row_b < b + 12:
                break
            arcade.draw_lrbt_rectangle_filled(
                l + 8, r - 8, row_b, row_t, (45, 38, 30),
            )
            arcade.draw_lrbt_rectangle_outline(
                l + 8, r - 8, row_b, row_t, COLOR_UI_BORDER, 1,
            )
            line(f"name_{i}", f"  {sc['name']}",
                 l + 12, row_b + 22, size=12, color=COLOR_WHITE, bold=True)
            line(f"id_{i}", f"  {sc['id']}/",
                 l + 12, row_b + 6, size=9, color=COLOR_GRAY)
            self._splash_load_scenario_rects.append(
                (l + 8, r - 8, row_b, row_t, f"pick:{sc['path']}")
            )

    # ── v0.36: Splash → Event editor (informational modal) ─────────
    def _draw_splash_event_editor(self) -> None:
        """Informational modal that documents the v0.36 wiring schema.

        Until the graph-view UI ships, authors edit the ``triggers``
        block in their scenario's ``map.json`` by hand. This modal
        gives them the schema reference at the splash menu so they
        don't have to dig through the changelog or scenario README
        to remember what kinds are available.

        The text is intentionally terse — full prose docs live in
        ``data/scenarios/tribute_crisis/README.md`` and
        ``CHANGELOG_v0.36.md``. This modal is a quick reference.
        """
        # Backdrop + centred panel — same colour palette as Credits.
        l_pad = 100
        cx = self.width / 2
        arcade.draw_lrbt_rectangle_filled(
            0, self.width, 0, self.height, (0, 0, 0, 200),
        )
        arcade.draw_lrbt_rectangle_filled(
            l_pad, self.width - l_pad, 80, self.height - 80,
            (20, 15, 10, 245),
        )
        arcade.draw_lrbt_rectangle_outline(
            l_pad, self.width - l_pad, 80, self.height - 80,
            COLOR_GOLD, 2,
        )

        if not hasattr(self, "_txt_splash_event_editor"):
            self._txt_splash_event_editor: dict[str, arcade.Text] = {}

        def line(key: str, text: str, x: float, y: float,
                 size: int = 11, color=COLOR_WHITE,
                 bold: bool = False, anchor_x: str = "left") -> None:
            obj = self._txt_splash_event_editor.get(key)
            if obj is None:
                obj = arcade.Text(
                    text, x, y, color, size, bold=bold, anchor_x=anchor_x,
                )
                self._txt_splash_event_editor[key] = obj
            obj.text = text
            obj.x = x
            obj.y = y
            obj.color = color
            obj.draw()

        # Layout — header + two columns of reference (when / do
        # kinds), plus a footer pointing at the JSON file the author
        # actually edits today.
        top = self.height - 110
        col_l = l_pad + 32
        col_r = cx + 32

        line("title", "EVENT EDITOR", cx, top,
             size=18, color=COLOR_GOLD, bold=True, anchor_x="center")
        line("subtitle",
             "Wire the four library editors together. (Graph-view UI is a v0.37 follow-up.)",
             cx, top - 26,
             size=11, color=COLOR_GRAY, anchor_x="center", bold=False)

        # Responsibility split — the bit the user asked to make
        # explicit. The Trigger editor (existing splash button) owns
        # the WHAT; this Event editor owns the WHEN + HOW.
        y = top - 70
        line("what_hdr", "What this editor is for",
             col_l, y, size=13, color=COLOR_GOLD, bold=True)
        y -= 24
        for txt in (
            "• WHEN things fire — at_tick, at_year, on_population_above,",
            "  on_treasury_below, on_flag, on_event_fired, …",
            "• WHAT happens next — play a cutscene, fire an event,",
            "  push an RPG request, set a flag, schedule a future event.",
            "",
            "Not the same as the Trigger editor — that one is the",
            "library of WHAT EVENTS DO (their effects + banner text).",
            "This one is the wiring graph that says when those events",
            "fire and what cascades from them.",
        ):
            line(f"what_{y}", txt, col_l, y, size=11)
            y -= 18

        # When kinds — quick reference.
        y2 = top - 70
        line("when_hdr", "when.kind", col_r, y2,
             size=13, color=COLOR_GOLD, bold=True)
        y2 -= 24
        for txt in (
            "at_tick              value: int",
            "at_year              value: int",
            "at_month             value: int",
            "on_population_above  value: int",
            "on_treasury_below    value: int",
            "random_after_tick    value: int",
            "on_flag              flag: str",
            "                     state: \"set\" | \"unset\"",
            "on_event_fired       event: str",
        ):
            line(f"when_{y2}", txt, col_r, y2, size=11)
            y2 -= 16

        # Do kinds — below the when ref.
        y2 -= 12
        line("do_hdr", "do.kind", col_r, y2,
             size=13, color=COLOR_GOLD, bold=True)
        y2 -= 24
        for txt in (
            "play_cutscene    id: str",
            "fire_request     id: str",
            "fire_event       name: str",
            "set_flag         flag: str",
            "schedule_event   event: str,",
            "                 trigger: {kind, value}",
        ):
            line(f"do_{y2}", txt, col_r, y2, size=11)
            y2 -= 16

        # Footer — how to author today.
        line("how", "How to author today:",
             col_l, 168, size=12, color=COLOR_GOLD, bold=True)
        line("how1",
             "1. Edit the ``triggers`` block at the top level of",
             col_l, 148, size=11)
        line("how2",
             "   ``data/scenarios/<your_scenario>/map.json``.",
             col_l, 132, size=11)
        line("how3",
             "2. Set ``scenario_libraries`` to point at scenario-local",
             col_l, 116, size=11)
        line("how4",
             "   cutscenes / requests / events JSON (optional).",
             col_l, 100, size=11)
        line("ref",
             "See data/scenarios/tribute_crisis/ for a worked example.   "
             "Esc to close.",
             cx, 86, size=10, color=COLOR_GRAY, anchor_x="center")
