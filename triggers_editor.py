"""Triggers editor — splash-menu modal for the ``triggers`` block on
``data/scenarios/<scenario>/map.json``.

v0.37. Companion editor to the v0.28 Event editor: the Event editor
owns the library of *what events do* (``data/events.json`` — name,
effects, banner colour, modifiers, duration); this one owns the
*when those events fire and what cascades from them* wiring. Authors
no longer hand-edit the JSON to wire a cutscene/event/request to a
flag flip — this editor surfaces the same fields the runtime
``TriggerManager`` consumes.

Schema (mirror of ``triggers.py`` docstring)::

    {
      "id":   "branch_refused",
      "when": {"kind": "on_flag", "flag": "tribute_refused"},
      "do": [
        {"kind": "play_cutscene", "id": "cutscene_caesar_wrath"},
        {"kind": "fire_event",    "name": "Caesar Displeased"},
        {"kind": "schedule_event","event": "Barbarian Raid",
         "trigger": {"kind": "at_tick", "value": 3600}}
      ],
      "once": true,
      "notes": "free-text author comment, ignored at runtime"
    }

Editor layout (1100×740 panel, clamped to screen)::

    +---------------------------------------------------------------+
    | TRIGGERS EDITOR                                               |
    | edit map.json `triggers` — wire library editors to game flow  |
    | scenario: tribute_crisis                            [Browse…] |
    |                                                               |
    | +-----------+  +-------------------------------------------+  |
    | | trigger   |  | id:        [_______]                      |  |
    | | list      |  | once:      [x]                            |  |
    | | (scrolly) |  |                                           |  |
    | |           |  | WHEN                                      |  |
    | |* intro    |  |   kind:     [on_flag       ] (cycle)      |  |
    | |* refused  |  |   value:    [_____] (int kinds only)      |  |
    | |* boom     |  |   flag:     [_____] (on_flag only)        |  |
    | |  …        |  |   state:    [set | unset] (on_flag only)  |  |
    | |           |  |   event:    [_____] (on_event_fired only) |  |
    | |           |  |                                           |  |
    | |           |  | DO (effects, run in order)                |  |
    | |           |  |  1) play_cutscene  id=[__]            x  |  |
    | |           |  |  2) fire_event     name=[__]          x  |  |
    | |           |  |  …                                        |  |
    | |           |  | [+ Add effect]                            |  |
    | +-----------+  +-------------------------------------------+  |
    |                                                               |
    | [+ Add] [- Remove]              [Cancel]  [Save]              |
    +---------------------------------------------------------------+

Implementation choices that matter (read once, then forget):

* Same module-shaped pattern as ``rpg_request_editor.py`` and
  ``cutscene_editor.py``: a ``TriggersEditorState`` dataclass on the
  window plus free functions ``open_editor`` / ``draw`` /
  ``handle_click`` / ``handle_key``. We don't subclass arcade.View —
  the editor renders into the splash overlay surface, same as its
  siblings.

* The editor edits ONE scenario's map.json at a time. v0.37 supports
  the bundled ``tribute_crisis`` scenario; the picker lists any
  scenario folder under ``data/scenarios/``, so adding more works
  with no editor changes.

* "Cycle" buttons drive the ``when.kind`` and per-effect ``do.kind``
  fields. Free-text typing is reserved for ``id``, ``flag``,
  ``event`` name, ``do.id``, etc. — anything where typos are
  meaningful. Numeric ``value`` uses -/+ stepper buttons; this
  matches the Event editor's per-field stepper UX rather than going
  through the text-input field.

* Save is atomic (tmp + os.replace) so a half-written file can't
  leave the scenario in a corrupted state. We round-trip *only* the
  ``triggers`` field of map.json, preserving every other key (terrain,
  buildings, stocks, the works) byte-for-byte.

* This editor knows nothing about the runtime ``TriggerManager``.
  The trigger manager re-reads the scenario JSON on the next world
  load; an in-game live-reload would need the trigger manager to
  ``load_dict`` from the updated block. v0.37 keeps it simple:
  save → reload the scenario in-game to see the new wiring.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import arcade

from constants import (
    COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_RED,
    COLOR_UI_BORDER, COLOR_WHITE, DATA_DIR,
)

log = logging.getLogger(__name__)


# ── Schema reference — kept in sync with triggers.py runtime ─────────
# The order matters: cycling the "kind" button rotates through this
# list. We put the most commonly authored kinds first so authors
# don't have to cycle far.
WHEN_KINDS: list[str] = [
    "at_tick",
    "at_year",
    "at_month",
    "on_population_above",
    "on_treasury_below",
    "random_after_tick",
    "on_flag",
    "on_event_fired",
    # v0.37: seasonal probabilistic + civic-service ratio gates.
    # Listed last so existing tests / scenarios that index by the
    # canonical order (none currently do, but be conservative) are
    # unaffected; new authors cycling through still reach them.
    "in_season_random",
    "on_building_ratio_below",
]

# v0.37: season names usable in ``in_season_random``. Order matters —
# cycling the season button rotates through this list. Mirrors
# ``game_window.SEASON_BY_MONTH``'s value set; if a season is added
# there, add it here too.
SEASONS: list[str] = ["Winter", "Spring", "Summer", "Autumn"]

# v0.37: denominator names for ``on_building_ratio_below.per``. The
# runtime treats these as synonymous (both resolve to
# ``economy.population``), but exposing both lets authors pick the
# wording that reads best in their scenario notes.
PER_KINDS: list[str] = ["population", "inhabitants"]

# v0.37: float-stepper increments for ``in_season_random.prob`` and
# ``on_building_ratio_below.value``. Probabilities live in [0, 1] and
# care about the second decimal place; building ratios per-1000 are
# usually authored at the integer / tenths level (1.0, 0.5). One
# pair of buttons covers both because we clamp to a hint-specific
# range at commit time.
STEP_FLOAT_MINOR = 0.01
STEP_FLOAT_MAJOR = 0.1
PROB_MIN = 0.0
PROB_MAX = 1.0
RATIO_MIN = 0.0
RATIO_MAX = 1000.0

DO_KINDS: list[str] = [
    "play_cutscene",
    "fire_event",
    "fire_request",
    "set_flag",
    "schedule_event",
]

# Per-kind arg-field shape — drives the per-row inline editor in the
# do-list. Each tuple is (kind, list of (arg_key, label, kind_hint))
# where kind_hint is "text" or "int". The renderer reads this so we
# don't repeat the case dispatch in three places.
DO_ARG_SHAPE: dict[str, list[tuple[str, str, str]]] = {
    "play_cutscene":  [("id",    "cutscene id",     "text")],
    "fire_event":     [("name",  "event name",      "text")],
    "fire_request":   [("id",    "request id",      "text")],
    "set_flag":       [("flag",  "flag name",       "text")],
    # schedule_event: one text + a nested trigger.kind cycle and value.
    "schedule_event": [
        ("event", "event name",       "text"),
        ("@trig_kind", "trigger.kind", "cycle_when"),
        ("@trig_val",  "trigger.value", "int"),
    ],
}

# Subset of WHEN_KINDS that can be nested inside schedule_event.trigger.
# All numeric-value kinds qualify; on_flag / on_event_fired don't —
# they live at the top-level when.
SCHEDULE_WHEN_KINDS: list[str] = [
    "at_tick", "at_year", "at_month",
    "on_population_above", "on_treasury_below",
    "random_after_tick",
]


# ── Layout constants ────────────────────────────────────────────────
PANEL_W = 1100
PANEL_H = 740
LIST_W = 220
ROW_H = 22
DO_ROW_H = 28
ID_MAX = 48
FLAG_MAX = 48
NAME_MAX = 48
VALUE_MAX_INT = 100000
VALUE_MIN_INT = -100000
STEP_MAJOR = 100
STEP_MINOR = 10


# ── State ──────────────────────────────────────────────────────────
class TriggersEditorState:
    """Holds editor state for the splash-menu Triggers editor.

    A single instance lives on the game window as
    ``window.triggers_editor_state`` and is created lazily the first
    time the editor is opened. State is not tied to the world — the
    editor runs over a frozen empty world, same pattern as the Event
    editor and RPG request editor.

    Fields
    ------
    open : bool
        Whether the editor modal is active. Drives the on_draw /
        on_mouse_press dispatch in game_window.
    scenario_rel : str
        Path under ``DATA_DIR`` to the scenario's map.json being
        edited. Defaults to ``scenarios/tribute_crisis/map.json``.
    map_blob : dict
        The full parsed map.json. We keep the whole thing so Save can
        write it back without dropping unrelated fields. Edits only
        touch ``map_blob["triggers"]``.
    triggers : list[dict]
        Convenience alias for ``map_blob["triggers"]``. Kept as a
        first-class field so ``draw`` doesn't repeat the dict access.
    selected_idx : int
        Index into ``triggers`` of the selected entry, or -1 if
        ``triggers`` is empty.
    scroll : int
        Vertical scroll offset for the left-column trigger list.
    do_scroll : int
        Vertical scroll offset for the do-effects list — separate so
        a trigger with many effects can scroll independently of the
        trigger list.
    text_field : str | None
        Which text input is focused. Values: None, "id", "flag",
        "event", "do:<i>:<arg_key>" (where <i> is the effect index
        and <arg_key> matches DO_ARG_SHAPE).
    text_buffer : str
        The string being typed. Committed to the field on Enter.
    dirty : bool
        Set on every mutation. The Save button is highlighted while
        true; Cancel discards regardless.
    btn_rects : list
        Hit-test rectangles rebuilt each frame in draw() and consumed
        on the next click. Tuple shape:
        ``(left, right, bottom, top, action_string)``.
    """

    def __init__(self) -> None:
        self.open: bool = False
        self.scenario_rel: str = "scenarios/tribute_crisis/map.json"
        self.map_blob: dict[str, Any] = {}
        self.triggers: list[dict[str, Any]] = []
        self.selected_idx: int = -1
        self.scroll: int = 0
        self.do_scroll: int = 0
        self.text_field: str | None = None
        self.text_buffer: str = ""
        self.dirty: bool = False
        self.btn_rects: list[tuple[float, float, float, float, str]] = []
        # Lazy text-pool — same trick the other editors use to skip
        # allocating arcade.Text() per frame.
        self._txt_pool: dict[str, arcade.Text] = {}
        # Scenario-picker overlay: when not None, draw() renders an
        # overlay listing every scenario directory and the next click
        # picks one. Mirrors the rpg_request_editor file browser.
        self.picker_open: bool = False


# ── File I/O ───────────────────────────────────────────────────────
def _resolve_scenario_path(rel: str) -> Path:
    """Resolve a scenario-relative path under DATA_DIR. We keep the
    relative form on the state because ``DATA_DIR`` is a Path; storing
    only the string keeps the state JSON-serialisable for tests."""
    return Path(DATA_DIR) / rel


def list_scenarios() -> list[str]:
    """Walk ``data/scenarios/*/map.json`` and return the relative
    paths (suitable for ``state.scenario_rel``). Sorted for stable UI
    ordering. Missing folder → empty list (the picker shows a
    "no scenarios" line in that case)."""
    base = Path(DATA_DIR) / "scenarios"
    try:
        out: list[str] = []
        for child in sorted(base.iterdir()):
            mj = child / "map.json"
            if mj.is_file():
                out.append(f"scenarios/{child.name}/map.json")
        return out
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []


def _load_map(rel: str) -> dict[str, Any]:
    """Read and JSON-parse a scenario map.json. Raises on malformed
    JSON so the caller can surface the error to the user — better
    than silently opening an empty editor."""
    p = _resolve_scenario_path(rel)
    with p.open("r") as f:
        blob = json.load(f)
    if not isinstance(blob, dict):
        raise ValueError(f"{p}: top-level JSON is not an object")
    return blob


def _save_map(rel: str, blob: dict[str, Any]) -> None:
    """Write blob back to disk atomically. The ``triggers`` field is
    expected to already be normalised by the caller (no editor-only
    fields, no None values)."""
    p = _resolve_scenario_path(rel)
    tmp = str(p) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(blob, f, indent=2)
    os.replace(tmp, p)


# ── Helpers for building new entries ──────────────────────────────
def _blank_trigger() -> dict[str, Any]:
    """Return a fresh trigger seed for the "+ Add" button. The shape
    matches what the runtime accepts; the author cycles ``when.kind``
    and adds ``do`` effects from there."""
    return {
        "id": "new_trigger",
        "when": {"kind": "at_tick", "value": 0},
        "do": [],
        "once": True,
    }


def _blank_effect(kind: str = "set_flag") -> dict[str, Any]:
    """Return a fresh effect seed for the "+ Add effect" button. The
    seed picks ``set_flag`` because it's the most common cascading
    primitive — author then cycles to whatever they actually want.
    """
    if kind == "schedule_event":
        return {
            "kind": "schedule_event",
            "event": "",
            "trigger": {"kind": "at_tick", "value": 0},
        }
    # All other kinds: kind + one string arg. We seed the string
    # field with empty so the editor's text input shows the placeholder.
    shape = DO_ARG_SHAPE.get(kind, [])
    seed: dict[str, Any] = {"kind": kind}
    for arg_key, _label, hint in shape:
        if arg_key.startswith("@"):
            continue  # nested fields are handled per-kind above
        seed[arg_key] = "" if hint == "text" else 0
    return seed


# ── Open / close / save ───────────────────────────────────────────
def open_editor(window: Any, state: TriggersEditorState) -> None:
    """Load the scenario's ``map.json`` into the editor's working
    blob and mark the editor as open. The caller (a splash button)
    is responsible for resetting the world to a safe empty state
    and freezing the simulation first."""
    # Fall back to the first available scenario if the saved one is
    # gone (e.g. the player deleted the folder between sessions).
    try:
        blob = _load_map(state.scenario_rel)
    except (FileNotFoundError, ValueError) as e:
        log.warning("Triggers editor: %s missing or malformed (%s); "
                    "trying first scenario", state.scenario_rel, e)
        cands = list_scenarios()
        if not cands:
            if hasattr(window, "_notify"):
                window._notify("No scenarios found.", COLOR_RED)
            return
        state.scenario_rel = cands[0]
        try:
            blob = _load_map(state.scenario_rel)
        except Exception as e2:  # noqa: BLE001
            log.exception("Triggers editor: fallback load also failed")
            if hasattr(window, "_notify"):
                window._notify(f"Load failed: {e2}", COLOR_RED)
            return
    except Exception as e:  # noqa: BLE001 — surface any other I/O fault
        log.exception("Triggers editor: load failed")
        if hasattr(window, "_notify"):
            window._notify(f"Load failed: {e}", COLOR_RED)
        return

    state.map_blob = blob
    # Normalise: ensure every trigger has the editor's expected fields
    # so the renderer doesn't have to dict-vs-None guard everywhere.
    raw_triggers = blob.get("triggers") or []
    state.triggers = []
    for t in raw_triggers:
        t = dict(t)
        t.setdefault("id", "trigger")
        t.setdefault("when", {"kind": "at_tick", "value": 0})
        t.setdefault("do", [])
        t.setdefault("once", True)
        # Make sure ``when`` is a dict-shaped thing — pre-v0.36 saves
        # may have stored a plain int (shouldn't happen, but be
        # defensive).
        if not isinstance(t["when"], dict):
            t["when"] = {"kind": "at_tick", "value": int(t["when"] or 0)}
        if not isinstance(t["do"], list):
            t["do"] = []
        state.triggers.append(t)
    state.selected_idx = 0 if state.triggers else -1
    state.scroll = 0
    state.do_scroll = 0
    state.text_field = None
    state.text_buffer = ""
    state.dirty = False
    state.picker_open = False
    state.open = True
    log.info(
        "Triggers editor: opened %s with %d triggers",
        state.scenario_rel, len(state.triggers),
    )


def close_editor(state: TriggersEditorState) -> None:
    """Close without saving. Same discard-on-Cancel UX as the other
    editors."""
    state.open = False
    state.text_field = None
    state.text_buffer = ""
    state.picker_open = False
    log.info("Triggers editor: closed (dirty=%s)", state.dirty)


def save_and_close(window: Any, state: TriggersEditorState) -> None:
    """Write the working triggers list back into map.json's ``triggers``
    field and close the editor. Atomic; on failure we leave the editor
    open so the user can retry."""
    # Strip editor-only state (the in-runtime "fired" flag, if a save
    # round-tripped through here). On load the runtime sets it back to
    # False from the JSON's absence.
    out_triggers: list[dict[str, Any]] = []
    for t in state.triggers:
        entry: dict[str, Any] = {
            "id": str(t.get("id", "trigger")),
            "when": dict(t.get("when") or {"kind": "at_tick", "value": 0}),
            "do": [dict(e) for e in (t.get("do") or [])],
            "once": bool(t.get("once", True)),
        }
        # Preserve notes (free-text author comments) when present.
        if t.get("notes"):
            entry["notes"] = str(t["notes"])
        out_triggers.append(entry)

    blob = dict(state.map_blob)
    blob["triggers"] = out_triggers
    try:
        _save_map(state.scenario_rel, blob)
    except OSError as e:
        log.exception("Triggers editor: save failed")
        if hasattr(window, "_notify"):
            window._notify(f"Save failed: {e}", COLOR_RED)
        return
    # Persist the saved blob locally so subsequent edits round-trip
    # against the same on-disk state.
    state.map_blob = blob
    state.dirty = False
    if hasattr(window, "_notify"):
        window._notify(
            f"Saved {len(out_triggers)} triggers to {state.scenario_rel}",
            COLOR_GREEN,
        )
    state.open = False
    log.info(
        "Triggers editor: wrote %d triggers to %s",
        len(out_triggers), state.scenario_rel,
    )


# ── Drawing ─────────────────────────────────────────────────────
def draw(window: Any, state: TriggersEditorState) -> None:
    """Render the modal. Called from the splash overlay branch in
    game_window's on_draw. Builds hit-test rects into
    ``state.btn_rects`` as it goes, consumed by ``handle_click`` on
    the next click."""
    state.btn_rects = []
    cx = window.width / 2
    cy = window.height / 2

    # Dim background.
    arcade.draw_lrbt_rectangle_filled(
        0, window.width, 0, window.height, (0, 0, 0, 170),
    )

    # Panel rect (clamped to screen if window is tiny).
    pw = PANEL_W
    ph = PANEL_H
    l = cx - pw / 2
    r = cx + pw / 2
    b = cy - ph / 2
    t = cy + ph / 2
    if t > window.height - 20:
        shift = (window.height - 20) - t
        t += shift; b += shift
    if b < 20:
        shift = 20 - b
        t += shift; b += shift
    arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

    # ── Header ──────────────────────────────────────────────────
    _text(state, "title", "TRIGGERS EDITOR",
          cx, t - 26, 14, COLOR_GOLD, True, anchor_x="center")
    _text(state, "subtitle",
          "edit map.json `triggers` — wire library editors to game flow",
          cx, t - 44, 10, COLOR_GRAY, anchor_x="center")
    # Scenario picker line: "scenario: foo  [Browse…]"
    _text(state, "scen_lbl",
          f"scenario: {state.scenario_rel}",
          l + 18, t - 62, 11, COLOR_WHITE)
    br_l = r - 110
    br_r = r - 16
    br_t = t - 56
    br_b = br_t - 22
    _button(state, br_l, br_r, br_b, br_t, "Browse…", "scenario_browse")

    # ── Left column: trigger list ───────────────────────────────
    list_l = l + 14
    list_r = list_l + LIST_W
    list_t = t - 80
    list_b = b + 60
    arcade.draw_lrbt_rectangle_filled(
        list_l, list_r, list_b, list_t, (22, 18, 14, 230),
    )
    arcade.draw_lrbt_rectangle_outline(
        list_l, list_r, list_b, list_t, COLOR_UI_BORDER, 1,
    )
    rows_visible = max(1, int((list_t - list_b) / ROW_H))
    max_scroll = max(0, len(state.triggers) - rows_visible)
    state.scroll = max(0, min(state.scroll, max_scroll))
    # Scroll buttons (right of the list).
    sx1 = list_r + 2
    sx2 = sx1 + 20
    _button(state, sx1, sx2, list_t - 22, list_t, "^", "scroll_up")
    _button(state, sx1, sx2, list_b, list_b + 22, "v", "scroll_down")
    # Rows.
    for vi in range(rows_visible):
        idx = state.scroll + vi
        if idx >= len(state.triggers):
            break
        tr = state.triggers[idx]
        row_t = list_t - vi * ROW_H
        row_b = row_t - ROW_H
        selected = (idx == state.selected_idx)
        if selected:
            arcade.draw_lrbt_rectangle_filled(
                list_l, list_r, row_b, row_t, (90, 70, 50, 220),
            )
        tid = str(tr.get("id", "?"))
        label = tid if len(tid) <= 24 else tid[:23] + "…"
        # Small icon-ish marker: ▶ if fires-once already done, • else.
        # (The editor doesn't see runtime state, so we just use • for
        # consistency.)
        _text(state, f"row_{vi}", f"• {label}",
              list_l + 6, row_b + 5, 11,
              COLOR_GOLD if selected else COLOR_WHITE)
        state.btn_rects.append(
            (list_l, list_r, row_b, row_t, f"select:{idx}")
        )
    # Footer for the list: Add / Remove.
    add_l = list_l
    add_r = list_l + (LIST_W // 2) - 4
    rem_l = add_r + 8
    rem_r = list_r
    ay_t = list_b - 4
    ay_b = ay_t - 24
    _button(state, add_l, add_r, ay_b, ay_t, "+ Add", "add_trigger",
            fill=(50, 80, 50))
    _button(state, rem_l, rem_r, ay_b, ay_t, "— Remove", "remove_trigger",
            fill=(80, 50, 40))

    # ── Right pane: per-trigger form ────────────────────────────
    form_l = list_r + 30
    form_r = r - 14
    if 0 <= state.selected_idx < len(state.triggers):
        _draw_form(state, state.triggers[state.selected_idx],
                   form_l, form_r, list_t, list_b)
    else:
        _text(state, "empty",
              "(no triggers yet — click + Add to create one)",
              (form_l + form_r) / 2, (list_t + list_b) / 2,
              12, COLOR_GRAY, anchor_x="center")

    # ── Footer: Cancel / Save ──────────────────────────────────
    _draw_footer(state, l, r, b)

    # ── Scenario picker overlay (if open) ──────────────────────
    if state.picker_open:
        _draw_scenario_picker(state, window)


def _draw_form(state: TriggersEditorState, tr: dict[str, Any],
               form_l: float, form_r: float,
               form_t: float, form_b: float) -> None:
    """Render the per-trigger edit pane. Reads the selected trigger
    and produces buttons / text inputs / steppers for every editable
    field. Hit rects are appended to ``state.btn_rects`` as we go."""
    # id row.
    y = form_t - 14
    _text(state, "id_lbl", "id:", form_l, y, 11, COLOR_GOLD, True)
    nb_l = form_l + 60
    nb_r = nb_l + 280
    nb_t = y + 4
    nb_b = nb_t - 22
    editing = state.text_field == "id"
    _text_input(state, nb_l, nb_r, nb_b, nb_t,
                state.text_buffer if editing else str(tr.get("id", "")),
                editing, "edit_id")

    # once toggle.
    onc_l = nb_r + 24
    onc_r = onc_l + 80
    once_val = bool(tr.get("once", True))
    _button(
        state, onc_l, onc_r, nb_b, nb_t,
        f"once: {'on' if once_val else 'off'}",
        "toggle_once",
        fill=(60, 80, 50) if once_val else (80, 60, 40),
    )

    # WHEN section header.
    y = nb_b - 24
    _text(state, "when_hdr", "WHEN",
          form_l, y, 12, COLOR_GOLD, True)
    y -= 22
    when = tr.setdefault("when", {"kind": "at_tick", "value": 0})
    if not isinstance(when, dict):
        when = {"kind": "at_tick", "value": 0}
        tr["when"] = when
    kind = str(when.get("kind", "at_tick"))
    # kind cycle button.
    _text(state, "when_kind_lbl", "kind:", form_l, y + 4, 11, COLOR_WHITE)
    kb_l = form_l + 70
    kb_r = kb_l + 200
    kb_t = y + 8
    kb_b = kb_t - 22
    _button(state, kb_l, kb_r, kb_b, kb_t, kind, "cycle_when_kind",
            fill=(50, 50, 70))
    # Per-kind arg fields. Same dispatch table as the runtime so the
    # editor is the single source of truth for which args each kind
    # consumes.
    y = kb_b - 28
    for arg_key, arg_label, arg_hint in _when_arg_shape(kind):
        _text(state, f"when_arg_{arg_key}_lbl", f"{arg_label}:",
              form_l, y + 4, 11, COLOR_WHITE)
        if arg_hint == "int":
            _draw_int_stepper(
                state, form_l + 110, y, when, arg_key,
                f"when:{arg_key}",
            )
        elif arg_hint == "state":
            cur_state = str(when.get("state", "set"))
            sb_l = form_l + 110
            sb_r = sb_l + 100
            sb_t = y + 8
            sb_b = sb_t - 22
            _button(state, sb_l, sb_r, sb_b, sb_t,
                    f"state: {cur_state}",
                    "cycle_when_state",
                    fill=(60, 80, 50) if cur_state == "set" else (80, 60, 40))
        elif arg_hint == "season":
            # v0.37: season cycle button. Same visual idiom as the
            # state toggle — one button, click to advance.
            cur_season = str(when.get("season", SEASONS[0]))
            if cur_season not in SEASONS:
                cur_season = SEASONS[0]
            sb_l = form_l + 110
            sb_r = sb_l + 140
            sb_t = y + 8
            sb_b = sb_t - 22
            # Tint hints at the season — keeps the button instantly
            # readable when authoring a winter-only Blizzard wiring.
            season_fill = {
                "Winter": (60, 80, 110),
                "Spring": (60, 100, 70),
                "Summer": (110, 95, 50),
                "Autumn": (110, 70, 50),
            }.get(cur_season, (60, 60, 80))
            _button(state, sb_l, sb_r, sb_b, sb_t,
                    cur_season, "cycle_when_season",
                    fill=season_fill)
        elif arg_hint == "per":
            cur_per = str(when.get("per", PER_KINDS[0]))
            if cur_per not in PER_KINDS:
                cur_per = PER_KINDS[0]
            pb_l = form_l + 110
            pb_r = pb_l + 140
            pb_t = y + 8
            pb_b = pb_t - 22
            _button(state, pb_l, pb_r, pb_b, pb_t,
                    cur_per, "cycle_when_per",
                    fill=(50, 60, 70))
        elif arg_hint in ("prob", "ratio"):
            # v0.37: float stepper. The bounds clamp on click, but
            # we pass them through so a future "show out-of-range
            # warning" pass can read them off the same source.
            lo, hi = ((PROB_MIN, PROB_MAX) if arg_hint == "prob"
                      else (RATIO_MIN, RATIO_MAX))
            _draw_float_stepper(
                state, form_l + 110, y, when, arg_key,
                f"when:{arg_key}",
                lo=lo, hi=hi,
            )
        else:
            # text
            nb_l2 = form_l + 110
            nb_r2 = nb_l2 + 260
            nb_t2 = y + 8
            nb_b2 = nb_t2 - 22
            field_id = f"when_{arg_key}"
            editing = state.text_field == field_id
            cur_val = str(when.get(arg_key, ""))
            _text_input(
                state, nb_l2, nb_r2, nb_b2, nb_t2,
                state.text_buffer if editing else cur_val,
                editing, f"edit_when:{arg_key}",
            )
        y -= 28

    # DO section header.
    y -= 6
    _text(state, "do_hdr", "DO (effects, run in order)",
          form_l, y, 12, COLOR_GOLD, True)
    y -= 4
    do_top = y
    do_bot = form_b + 6
    # Scroll buttons for the do-list.
    dsx1 = form_r - 30
    dsx2 = form_r - 10
    _button(state, dsx1, dsx2, do_top - 18, do_top, "^", "do_scroll_up")
    _button(state, dsx1, dsx2, do_bot, do_bot + 18, "v", "do_scroll_down")
    # Effects panel — a scrollable list of effect rows.
    do_list = tr.setdefault("do", [])
    if not isinstance(do_list, list):
        do_list = []
        tr["do"] = do_list
    rows_visible = max(1, int((do_top - do_bot - 30) / DO_ROW_H))
    max_scroll = max(0, len(do_list) - rows_visible)
    state.do_scroll = max(0, min(state.do_scroll, max_scroll))
    cur_y = do_top - 6
    for vi in range(rows_visible):
        idx = state.do_scroll + vi
        if idx >= len(do_list):
            break
        eff = do_list[idx]
        if not isinstance(eff, dict):
            eff = {"kind": "set_flag"}
            do_list[idx] = eff
        row_t = cur_y
        row_b = row_t - DO_ROW_H
        if row_b < do_bot + 28:
            break
        # Background row.
        arcade.draw_lrbt_rectangle_filled(
            form_l, form_r - 36, row_b, row_t, (28, 22, 18),
        )
        arcade.draw_lrbt_rectangle_outline(
            form_l, form_r - 36, row_b, row_t, COLOR_UI_BORDER, 1,
        )
        # Effect number.
        _text(state, f"do_num_{vi}", f"{idx + 1})",
              form_l + 6, row_b + 7, 11, COLOR_GRAY)
        # Kind cycle.
        ek_l = form_l + 32
        ek_r = ek_l + 130
        ek_t = row_t - 4
        ek_b = ek_t - 20
        ek_kind = str(eff.get("kind", "set_flag"))
        _button(state, ek_l, ek_r, ek_b, ek_t,
                ek_kind, f"cycle_do_kind:{idx}",
                fill=(50, 50, 70))
        # Arg field(s) — only the first one is rendered inline so the
        # row stays at one visual line. schedule_event's nested
        # trigger.kind / trigger.value get inline editors below the
        # main arg row.
        shape = DO_ARG_SHAPE.get(ek_kind, [])
        x_cursor = ek_r + 8
        if shape:
            arg_key, arg_label, arg_hint = shape[0]
            if arg_hint == "text":
                ti_l = x_cursor
                ti_r = ti_l + 200
                ti_t = ek_t
                ti_b = ek_b
                fid = f"do:{idx}:{arg_key}"
                editing = state.text_field == fid
                cur_val = str(eff.get(arg_key, ""))
                _text_input(
                    state, ti_l, ti_r, ti_b, ti_t,
                    state.text_buffer if editing else cur_val,
                    editing, f"edit_do:{idx}:{arg_key}",
                )
                x_cursor = ti_r + 8
        # Remove button at the row's right.
        rm_l = form_r - 32
        rm_r = form_r - 12
        rm_t = ek_t
        rm_b = ek_b
        _button(state, rm_l, rm_r, rm_b, rm_t, "x",
                f"remove_do:{idx}", fill=(90, 40, 35))
        # schedule_event: second visual line for trigger.kind / value.
        if ek_kind == "schedule_event":
            nested = eff.setdefault(
                "trigger", {"kind": "at_tick", "value": 0},
            )
            if not isinstance(nested, dict):
                nested = {"kind": "at_tick", "value": 0}
                eff["trigger"] = nested
            sl_t = ek_b - 2
            sl_b = sl_t - 16
            _text(state, f"do_st_lbl_{vi}", "trigger:",
                  form_l + 32, sl_b + 1, 10, COLOR_GRAY)
            sk_l = form_l + 90
            sk_r = sk_l + 110
            _button(state, sk_l, sk_r, sl_b, sl_t,
                    str(nested.get("kind", "at_tick")),
                    f"cycle_do_sched_kind:{idx}",
                    fill=(50, 60, 50))
            # +/- stepper for the nested value.
            sv = int(nested.get("value", 0))
            sx = sk_r + 8
            for w, lbl, act in (
                (28, "-10", f"sched_minus_minor:{idx}"),
                (28, "-1", f"sched_minus_one:{idx}"),
                (50, str(sv), None),
                (28, "+1", f"sched_plus_one:{idx}"),
                (28, "+10", f"sched_plus_minor:{idx}"),
            ):
                xl = sx
                xr = sx + w
                if act is None:
                    arcade.draw_lrbt_rectangle_filled(
                        xl, xr, sl_b, sl_t, (22, 18, 14),
                    )
                    arcade.draw_lrbt_rectangle_outline(
                        xl, xr, sl_b, sl_t, COLOR_UI_BORDER, 1,
                    )
                    _text(state, f"do_sv_{vi}", lbl,
                          (xl + xr) / 2, sl_b + 1, 10, COLOR_GOLD,
                          anchor_x="center")
                else:
                    _button(state, xl, xr, sl_b, sl_t, lbl, act,
                            fill=(60, 45, 35), text_size=10)
                sx = xr + 2
            cur_y -= 18  # extra space for the second line
        cur_y -= DO_ROW_H
    # + Add effect button at the bottom of the panel.
    ab_l = form_l
    ab_r = form_r - 36
    ab_t = do_bot + 24
    ab_b = do_bot + 4
    _button(state, ab_l, ab_r, ab_b, ab_t, "+ Add effect",
            "add_do_effect", fill=(50, 80, 50))


def _draw_footer(state: TriggersEditorState,
                 l: float, r: float, b: float) -> None:
    """Cancel / Save buttons. Same layout as the Event editor's
    footer (and the runtime makes the same atomic-save guarantee)."""
    btn_w = 110
    btn_h = 30
    btn_y_t = b + 40
    btn_y_b = btn_y_t - btn_h
    save_r = r - 24
    save_l = save_r - btn_w
    cancel_r = save_l - 16
    cancel_l = cancel_r - btn_w
    _button(state, cancel_l, cancel_r, btn_y_b, btn_y_t,
            "Cancel", "cancel", fill=(50, 40, 35))
    save_fill = (90, 70, 30) if state.dirty else (50, 40, 35)
    _button(state, save_l, save_r, btn_y_b, btn_y_t,
            "Save" + (" *" if state.dirty else ""),
            "save", fill=save_fill,
            text_color=COLOR_GOLD if state.dirty else COLOR_WHITE)


def _draw_scenario_picker(state: TriggersEditorState, window: Any) -> None:
    """Modal overlay: list every scenario and let the author pick
    one to edit. Mirrors the file browser in rpg_request_editor."""
    cands = list_scenarios()
    cx = window.width / 2
    cy = window.height / 2
    pw = 480
    rows = max(1, len(cands)) + 2
    ph = 40 + 24 * rows + 32
    l = cx - pw / 2
    r = cx + pw / 2
    b = cy - ph / 2
    t = cy + ph / 2
    arcade.draw_lrbt_rectangle_filled(
        0, window.width, 0, window.height, (0, 0, 0, 150),
    )
    arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 250))
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)
    _text(state, "pick_title", "Pick a scenario",
          cx, t - 26, 13, COLOR_GOLD, True, anchor_x="center")
    row_top = t - 50
    if not cands:
        _text(state, "pick_empty",
              "No scenarios found in data/scenarios/.",
              cx, row_top, 11, COLOR_GRAY, anchor_x="center")
    for i, rel in enumerate(cands):
        ry_t = row_top - i * 24
        ry_b = ry_t - 22
        # Strip the leading "scenarios/" + trailing "/map.json" for
        # the display label; it's noisy and identical for every row.
        label = rel
        if rel.startswith("scenarios/") and rel.endswith("/map.json"):
            label = rel[len("scenarios/"):-len("/map.json")]
        _button(state, l + 16, r - 16, ry_b, ry_t,
                label, f"pick_scenario:{rel}",
                fill=(50, 40, 32), text_color=COLOR_WHITE)
    # Cancel.
    cb_l = l + 16
    cb_r = r - 16
    cb_t = b + 30
    cb_b = b + 8
    _button(state, cb_l, cb_r, cb_b, cb_t, "Cancel",
            "pick_cancel", fill=(80, 50, 40))


# ── Tiny drawing helpers ───────────────────────────────────────
def _text(state: TriggersEditorState, key: str, txt: str,
          x: float, y: float, size: int = 11,
          color=COLOR_WHITE, bold: bool = False,
          anchor_x: str = "left") -> None:
    """Cached arcade.Text — same lazy pool the other editors use to
    skip allocating a new Text object per frame."""
    obj = state._txt_pool.get(key)
    if obj is None:
        obj = arcade.Text(txt, x, y, color, size, bold=bold, anchor_x=anchor_x)
        state._txt_pool[key] = obj
    obj.text = txt
    obj.x = x
    obj.y = y
    obj.color = color
    obj.draw()


def _button(state: TriggersEditorState,
            l: float, r: float, b: float, t: float,
            label: str, action: str,
            fill=(60, 45, 35), text_color=COLOR_WHITE,
            text_size: int = 11) -> None:
    """Draw a rectangular button, register its hit rect, and label it."""
    arcade.draw_lrbt_rectangle_filled(l, r, b, t, fill)
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_UI_BORDER, 1)
    _text(state, f"btn_{action}", label,
          (l + r) / 2, b + 5, text_size, text_color, True,
          anchor_x="center")
    state.btn_rects.append((l, r, b, t, action))


def _text_input(state: TriggersEditorState,
                l: float, r: float, b: float, t: float,
                display: str, editing: bool, action: str) -> None:
    """Render a text input box: gold border when focused, gray when not.
    Appends a cursor to the displayed text while editing."""
    arcade.draw_lrbt_rectangle_filled(l, r, b, t, (22, 18, 14))
    arcade.draw_lrbt_rectangle_outline(
        l, r, b, t,
        COLOR_GOLD if editing else COLOR_UI_BORDER,
        2 if editing else 1,
    )
    shown = (display + "_") if editing else display
    if len(shown) > 40:
        shown = "…" + shown[-39:]
    _text(state, f"ti_{action}", shown, l + 6, b + 5, 11,
          COLOR_GOLD if editing else COLOR_WHITE)
    state.btn_rects.append((l, r, b, t, action))


def _draw_int_stepper(state: TriggersEditorState,
                      x: float, y: float,
                      target: dict, key: str,
                      action_prefix: str) -> None:
    """5-button -10 / -1 / value / +1 / +10 stepper, written into
    ``target[key]``. Stable layout: total width fits in 260px, which
    leaves room next to the kind cycle in the form layout."""
    cur = int(target.get(key, 0))
    cursor = x
    for w, lbl, act in (
        (28, f"-{STEP_MAJOR}", "minus_major"),
        (28, f"-{STEP_MINOR}", "minus_minor"),
        (70, str(cur), None),
        (28, f"+{STEP_MINOR}", "plus_minor"),
        (28, f"+{STEP_MAJOR}", "plus_major"),
    ):
        xl = cursor
        xr = cursor + w
        yb = y - 14
        yt = yb + 22
        if act is None:
            arcade.draw_lrbt_rectangle_filled(xl, xr, yb, yt, (22, 18, 14))
            arcade.draw_lrbt_rectangle_outline(
                xl, xr, yb, yt, COLOR_UI_BORDER, 1,
            )
            _text(state, f"intst_{action_prefix}_val", lbl,
                  (xl + xr) / 2, yb + 5, 11, COLOR_GOLD,
                  bold=True, anchor_x="center")
        else:
            _button(state, xl, xr, yb, yt, lbl,
                    f"{action_prefix}_{act}", fill=(60, 45, 35))
        cursor = xr + 2


def _draw_float_stepper(state: TriggersEditorState,
                        x: float, y: float,
                        target: dict, key: str,
                        action_prefix: str,
                        *, lo: float, hi: float,
                        decimals: int = 2) -> None:
    """v0.37: float counterpart to ``_draw_int_stepper``. Shows
    ``-MAJOR / -MINOR / value / +MINOR / +MAJOR`` where MAJOR / MINOR
    are ``STEP_FLOAT_MAJOR`` / ``STEP_FLOAT_MINOR``. The displayed
    value is rounded to ``decimals`` so floating-point noise from
    repeated +0.01 clicks (the classic 0.30000000000000004) doesn't
    leak into the on-disk JSON.

    The ``lo`` / ``hi`` bounds are enforced in the dispatch handler
    on click; here they only widen the display column if needed."""
    cur = float(target.get(key, 0.0) or 0.0)
    # Pre-format so the value-cell width fits both 0.01 and 1000.00.
    val_text = f"{cur:.{decimals}f}"
    cursor = x
    for w, lbl, act in (
        (32, f"-{STEP_FLOAT_MAJOR:.2f}", "fminus_major"),
        (32, f"-{STEP_FLOAT_MINOR:.2f}", "fminus_minor"),
        (70, val_text, None),
        (32, f"+{STEP_FLOAT_MINOR:.2f}", "fplus_minor"),
        (32, f"+{STEP_FLOAT_MAJOR:.2f}", "fplus_major"),
    ):
        xl = cursor
        xr = cursor + w
        yb = y - 14
        yt = yb + 22
        if act is None:
            arcade.draw_lrbt_rectangle_filled(xl, xr, yb, yt, (22, 18, 14))
            arcade.draw_lrbt_rectangle_outline(
                xl, xr, yb, yt, COLOR_UI_BORDER, 1,
            )
            _text(state, f"fst_{action_prefix}_val", lbl,
                  (xl + xr) / 2, yb + 5, 11, COLOR_GOLD,
                  bold=True, anchor_x="center")
        else:
            _button(state, xl, xr, yb, yt, lbl,
                    f"{action_prefix}_{act}", fill=(60, 45, 35))
        cursor = xr + 2



def _when_arg_shape(kind: str) -> list[tuple[str, str, str]]:
    """Args expected by each ``when.kind``. Drives both the form
    renderer and the field dispatch in handle_click.

    Hint values consumed by ``_draw_form``:
        "int"          → 5-button -100/-10/value/+10/+100 stepper
        "text"         → text input
        "state"        → set/unset toggle (only used by on_flag)
        "season"       → cycle button (Winter→Spring→Summer→Autumn)
        "per"          → cycle button (population↔inhabitants)
        "prob"         → 4-button -0.10/-0.01/value/+0.01/+0.10 stepper
                         clamped to [0.0, 1.0]
        "ratio"        → same stepper as "prob", clamped to [0.0, 1000.0]
    """
    if kind in ("at_tick", "at_year", "at_month",
                "on_population_above", "on_treasury_below",
                "random_after_tick"):
        return [("value", "value", "int")]
    if kind == "on_flag":
        return [("flag", "flag", "text"),
                ("@state", "state", "state")]
    if kind == "on_event_fired":
        return [("event", "event", "text")]
    # v0.37: seasonal probabilistic. Two fields — the season cycle
    # and the per-tick Bernoulli probability.
    if kind == "in_season_random":
        return [("season", "season", "season"),
                ("prob",   "prob/tick", "prob")]
    # v0.37: building-ratio shortage gate. Three fields — which
    # building kind to count, what to divide by, and the
    # per-1000 threshold.
    if kind == "on_building_ratio_below":
        return [("building", "building id", "text"),
                ("per",      "per",         "per"),
                ("value",    "per 1000",    "ratio")]
    return []


# ── Click handling ────────────────────────────────────────────
def handle_click(window: Any, state: TriggersEditorState,
                 x: float, y: float) -> bool:
    """Hit-test the rebuilt btn_rects from the last draw and dispatch.
    Returns True if anything was consumed — the editor eats every
    click while open (matches the Event editor)."""
    # Picker overlay swallows everything when open.
    if state.picker_open:
        for x1, x2, y1, y2, action in state.btn_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                _dispatch(window, state, action)
                return True
        # Click outside any picker button — close the picker.
        state.picker_open = False
        return True
    for x1, x2, y1, y2, action in state.btn_rects:
        if x1 <= x <= x2 and y1 <= y <= y2:
            # Drop text-input focus on any click that isn't into a
            # text-input action.
            if (state.text_field is not None
                    and not action.startswith(("edit_", "cycle_"))):
                _commit_text_buffer(state)
            _dispatch(window, state, action)
            return True
    # Click outside any button → drop focus.
    if state.text_field is not None:
        _commit_text_buffer(state)
    return True


def _dispatch(window: Any, state: TriggersEditorState,
              action: str) -> None:
    """Route one action string to the appropriate mutator. Action
    strings have one of these shapes:

        cancel | save
        scroll_up | scroll_down
        do_scroll_up | do_scroll_down
        add_trigger | remove_trigger
        select:<idx>
        toggle_once
        cycle_when_kind | cycle_when_state
        cycle_when_season | cycle_when_per       (v0.37)
        edit_id | edit_when:<arg_key> | edit_do:<i>:<arg_key>
        when:<arg_key>_<minus_major|minus_minor|plus_minor|plus_major>
        when:<arg_key>_<fminus_major|fminus_minor|fplus_minor|fplus_major>
                                                 (v0.37 float stepper)
        cycle_do_kind:<i> | remove_do:<i> | add_do_effect
        cycle_do_sched_kind:<i>
        sched_minus_minor:<i> | sched_minus_one:<i>
        sched_plus_one:<i> | sched_plus_minor:<i>
        scenario_browse | pick_scenario:<rel> | pick_cancel
    """
    if action == "cancel":
        close_editor(state)
        return
    if action == "save":
        save_and_close(window, state)
        return
    if action == "scroll_up":
        state.scroll = max(0, state.scroll - 1)
        return
    if action == "scroll_down":
        state.scroll += 1  # clamped next draw
        return
    if action == "do_scroll_up":
        state.do_scroll = max(0, state.do_scroll - 1)
        return
    if action == "do_scroll_down":
        state.do_scroll += 1
        return
    if action == "add_trigger":
        state.triggers.append(_blank_trigger())
        state.selected_idx = len(state.triggers) - 1
        state.do_scroll = 0
        state.dirty = True
        return
    if action == "remove_trigger":
        if 0 <= state.selected_idx < len(state.triggers):
            del state.triggers[state.selected_idx]
            if not state.triggers:
                state.selected_idx = -1
            else:
                state.selected_idx = min(
                    state.selected_idx, len(state.triggers) - 1,
                )
            state.dirty = True
        return
    if action.startswith("select:"):
        new_idx = int(action.split(":", 1)[1])
        if 0 <= new_idx < len(state.triggers):
            state.selected_idx = new_idx
            state.do_scroll = 0
        return
    if action == "scenario_browse":
        state.picker_open = True
        return
    if action == "pick_cancel":
        state.picker_open = False
        return
    if action.startswith("pick_scenario:"):
        rel = action.split(":", 1)[1]
        state.picker_open = False
        if state.dirty:
            # Don't silently drop unsaved edits. We just notify and
            # bail; the author has to Cancel/Save before switching
            # scenarios. Same friendly-strict pattern as the buildings
            # editor.
            if hasattr(window, "_notify"):
                window._notify(
                    "Unsaved triggers — Save or Cancel before switching scenarios.",
                    COLOR_RED,
                )
            return
        state.scenario_rel = rel
        open_editor(window, state)
        return

    # Everything below assumes a selected trigger.
    if not (0 <= state.selected_idx < len(state.triggers)):
        return
    tr = state.triggers[state.selected_idx]

    if action == "toggle_once":
        tr["once"] = not bool(tr.get("once", True))
        state.dirty = True
        return
    if action == "cycle_when_kind":
        when = tr.setdefault("when", {"kind": "at_tick", "value": 0})
        cur = when.get("kind", "at_tick")
        try:
            i = WHEN_KINDS.index(cur)
        except ValueError:
            i = -1
        new_kind = WHEN_KINDS[(i + 1) % len(WHEN_KINDS)]
        when.clear()
        when["kind"] = new_kind
        # Seed sensible defaults per kind so a freshly-cycled when is
        # immediately valid (the runtime's _when_matches skips unknown
        # shapes anyway, but a half-typed when shouldn't read as
        # "no-op").
        if new_kind in ("at_tick", "at_year", "at_month",
                        "on_population_above", "on_treasury_below",
                        "random_after_tick"):
            when["value"] = 0
        elif new_kind == "on_flag":
            when["flag"] = ""
            when["state"] = "set"
        elif new_kind == "on_event_fired":
            when["event"] = ""
        elif new_kind == "in_season_random":
            # Winter @ 1% per tick: the canonical "blizzard in winter"
            # example from the v0.37 spec. Authors who want a
            # different season just click the season cycle.
            when["season"] = "Winter"
            when["prob"] = 0.01
        elif new_kind == "on_building_ratio_below":
            # Doctors-per-1000 < 1.0 (≈ 0.1%): the canonical
            # "spread plague when medical coverage drops" example.
            # Author retypes the building id if they want a different
            # service.
            when["building"] = "clinic"
            when["per"] = "population"
            when["value"] = 1.0
        state.dirty = True
        return
    if action == "cycle_when_state":
        when = tr.setdefault("when", {"kind": "on_flag", "flag": ""})
        when["state"] = "unset" if when.get("state", "set") == "set" else "set"
        state.dirty = True
        return
    if action == "cycle_when_season":
        when = tr.setdefault("when",
                             {"kind": "in_season_random",
                              "season": SEASONS[0], "prob": 0.01})
        cur = str(when.get("season", SEASONS[0]))
        try:
            i = SEASONS.index(cur)
        except ValueError:
            i = -1
        when["season"] = SEASONS[(i + 1) % len(SEASONS)]
        state.dirty = True
        return
    if action == "cycle_when_per":
        when = tr.setdefault("when",
                             {"kind": "on_building_ratio_below",
                              "building": "", "per": PER_KINDS[0],
                              "value": 1.0})
        cur = str(when.get("per", PER_KINDS[0]))
        try:
            i = PER_KINDS.index(cur)
        except ValueError:
            i = -1
        when["per"] = PER_KINDS[(i + 1) % len(PER_KINDS)]
        state.dirty = True
        return
    if action == "edit_id":
        state.text_field = "id"
        state.text_buffer = str(tr.get("id", ""))
        return
    if action.startswith("edit_when:"):
        arg_key = action.split(":", 1)[1]
        state.text_field = f"when_{arg_key}"
        state.text_buffer = str(tr.get("when", {}).get(arg_key, ""))
        return
    if action.startswith("when:"):
        # when:<arg_key>_<minus_major|minus_minor|plus_minor|plus_major>
        # (int stepper) or
        # when:<arg_key>_<fminus_major|fminus_minor|fplus_minor|fplus_major>
        # (float stepper).
        rest = action.split(":", 1)[1]
        # We split on the last underscore-prefix kind, since arg_key
        # may itself contain underscores (it doesn't today, but
        # forward-proofing).
        # v0.37: try float suffixes first — they're a longer match
        # ("_fminus_major" vs "_minus_major") so the int loop would
        # mistakenly trim the leading 'f' off the arg_key otherwise.
        for suffix, delta in (
            ("_fminus_major", -STEP_FLOAT_MAJOR),
            ("_fminus_minor", -STEP_FLOAT_MINOR),
            ("_fplus_minor",   STEP_FLOAT_MINOR),
            ("_fplus_major",   STEP_FLOAT_MAJOR),
        ):
            if rest.endswith(suffix):
                arg_key = rest[: -len(suffix)]
                when = tr.setdefault("when", {})
                try:
                    cur = float(when.get(arg_key, 0.0) or 0.0)
                except (TypeError, ValueError):
                    cur = 0.0
                new_val = cur + delta
                # Pick the clamp range from the arg's hint. Same
                # branch as the renderer: prob → [0, 1],
                # ratio → [0, 1000]. Anything unrecognised stays
                # un-clamped (lets us add new float fields without
                # editing this site).
                hint = None
                for ak, _lbl, h in _when_arg_shape(
                    str(when.get("kind", ""))
                ):
                    if ak == arg_key:
                        hint = h
                        break
                if hint == "prob":
                    new_val = max(PROB_MIN, min(PROB_MAX, new_val))
                elif hint == "ratio":
                    new_val = max(RATIO_MIN, min(RATIO_MAX, new_val))
                # Round to 2 decimals to keep the on-disk JSON tidy
                # — repeated +0.01 clicks otherwise drift into
                # 0.30000000000000004 territory.
                when[arg_key] = round(new_val, 2)
                state.dirty = True
                return
        for kind, delta in (
            ("_minus_major", -STEP_MAJOR),
            ("_minus_minor", -STEP_MINOR),
            ("_plus_minor", STEP_MINOR),
            ("_plus_major", STEP_MAJOR),
        ):
            if rest.endswith(kind):
                arg_key = rest[: -len(kind)]
                when = tr.setdefault("when", {})
                cur = int(when.get(arg_key, 0) or 0)
                new_val = cur + delta
                # Clamp month to [1, 12] inclusive for at_month so the
                # editor doesn't author runtime-invalid wirings.
                if when.get("kind") == "at_month":
                    new_val = max(1, min(12, new_val))
                else:
                    new_val = max(VALUE_MIN_INT, min(VALUE_MAX_INT, new_val))
                when[arg_key] = new_val
                state.dirty = True
                return
        return

    # ── do-effects ────────────────────────────────────────────
    if action == "add_do_effect":
        do_list = tr.setdefault("do", [])
        do_list.append(_blank_effect())
        state.do_scroll = max(0, len(do_list) - 1)  # bring it into view
        state.dirty = True
        return
    if action.startswith("remove_do:"):
        idx = int(action.split(":", 1)[1])
        do_list = tr.setdefault("do", [])
        if 0 <= idx < len(do_list):
            del do_list[idx]
            state.dirty = True
        return
    if action.startswith("cycle_do_kind:"):
        idx = int(action.split(":", 1)[1])
        do_list = tr.setdefault("do", [])
        if 0 <= idx < len(do_list):
            eff = do_list[idx]
            cur = eff.get("kind", "set_flag")
            try:
                i = DO_KINDS.index(cur)
            except ValueError:
                i = -1
            new_kind = DO_KINDS[(i + 1) % len(DO_KINDS)]
            # Re-seed the effect with the new shape — leave nothing
            # of the previous kind's args around so the file stays
            # tight on Save.
            do_list[idx] = _blank_effect(new_kind)
            state.dirty = True
        return
    if action.startswith("edit_do:"):
        # edit_do:<i>:<arg_key>
        _, rest = action.split(":", 1)
        i_s, arg_key = rest.split(":", 1)
        i = int(i_s)
        state.text_field = f"do:{i}:{arg_key}"
        do_list = tr.setdefault("do", [])
        if 0 <= i < len(do_list):
            state.text_buffer = str(do_list[i].get(arg_key, ""))
        else:
            state.text_buffer = ""
        return
    if action.startswith("cycle_do_sched_kind:"):
        idx = int(action.split(":", 1)[1])
        do_list = tr.setdefault("do", [])
        if 0 <= idx < len(do_list):
            eff = do_list[idx]
            nested = eff.setdefault(
                "trigger", {"kind": "at_tick", "value": 0},
            )
            cur = nested.get("kind", "at_tick")
            try:
                i = SCHEDULE_WHEN_KINDS.index(cur)
            except ValueError:
                i = -1
            nested["kind"] = SCHEDULE_WHEN_KINDS[
                (i + 1) % len(SCHEDULE_WHEN_KINDS)
            ]
            # Clamp the existing value to the new kind's range
            # (at_month wants [1, 12]).
            v = int(nested.get("value", 0) or 0)
            if nested["kind"] == "at_month":
                nested["value"] = max(1, min(12, v))
            state.dirty = True
        return
    for prefix, delta in (
        ("sched_minus_minor:", -STEP_MINOR),
        ("sched_minus_one:",   -1),
        ("sched_plus_one:",     1),
        ("sched_plus_minor:",  STEP_MINOR),
    ):
        if action.startswith(prefix):
            idx = int(action[len(prefix):])
            do_list = tr.setdefault("do", [])
            if 0 <= idx < len(do_list):
                eff = do_list[idx]
                nested = eff.setdefault(
                    "trigger", {"kind": "at_tick", "value": 0},
                )
                cur = int(nested.get("value", 0) or 0)
                new_val = cur + delta
                if nested.get("kind") == "at_month":
                    new_val = max(1, min(12, new_val))
                else:
                    new_val = max(0, new_val)  # delays are non-negative
                nested["value"] = new_val
                state.dirty = True
            return


# ── Key handling for text inputs ───────────────────────────────
def handle_key(window: Any, state: TriggersEditorState,
               symbol: int, modifiers: int) -> bool:
    """Buffer mutation for any active text input. Returns True if
    the key was consumed.

    Enter commits, Esc cancels (back to the unedited field value),
    Backspace deletes, printable chars append. Same UX as the Event
    editor — we delegate the character mapping to
    ``window._char_for_symbol`` so we share punctuation / shift rules.
    """
    if state.text_field is None:
        # Esc with no active text input closes the editor as a whole.
        if symbol == arcade.key.ESCAPE:
            close_editor(state)
            return True
        return False
    if symbol in (arcade.key.ENTER, arcade.key.RETURN, arcade.key.NUM_ENTER):
        _commit_text_buffer(state)
        return True
    if symbol == arcade.key.ESCAPE:
        # Discard the in-flight edit.
        state.text_field = None
        state.text_buffer = ""
        return True
    if symbol == arcade.key.BACKSPACE:
        state.text_buffer = (state.text_buffer or "")[:-1]
        return True
    ch = window._char_for_symbol(symbol, modifiers) \
        if hasattr(window, "_char_for_symbol") else None
    if ch is None:
        return False
    limit = _field_limit(state.text_field)
    if len(state.text_buffer or "") < limit:
        state.text_buffer = (state.text_buffer or "") + ch
    return True


def _field_limit(field: str) -> int:
    """Character cap per text field. We keep these short — the
    triggers JSON is meant to be human-readable, and a 200-char
    flag name is a mistake we'd rather make hard to commit."""
    if field == "id":
        return ID_MAX
    if field.startswith("when_flag") or field.endswith(":flag"):
        return FLAG_MAX
    return NAME_MAX


def _commit_text_buffer(state: TriggersEditorState) -> None:
    """Apply the in-flight text buffer to the field it belongs to."""
    if state.text_field is None:
        return
    if not (0 <= state.selected_idx < len(state.triggers)):
        state.text_field = None
        state.text_buffer = ""
        return
    tr = state.triggers[state.selected_idx]
    field = state.text_field
    buf = (state.text_buffer or "").strip()
    if field == "id":
        # Empty id is allowed (the runtime tolerates it — it shows
        # up in log lines as "<unnamed>"), but we prefer to keep the
        # previous id rather than silently blank it.
        if buf:
            if tr.get("id") != buf:
                tr["id"] = buf
                state.dirty = True
    elif field.startswith("when_"):
        arg_key = field[len("when_"):]
        when = tr.setdefault("when", {})
        if when.get(arg_key) != buf:
            when[arg_key] = buf
            state.dirty = True
    elif field.startswith("do:"):
        # do:<i>:<arg_key>
        _, i_s, arg_key = field.split(":", 2)
        i = int(i_s)
        do_list = tr.setdefault("do", [])
        if 0 <= i < len(do_list):
            if do_list[i].get(arg_key) != buf:
                do_list[i][arg_key] = buf
                state.dirty = True
    state.text_field = None
    state.text_buffer = ""
