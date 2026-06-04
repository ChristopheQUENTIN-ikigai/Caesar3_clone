"""RPG request editor — splash-menu modal for ``data/rpg_requests.json``.

v0.31. Mirrors the trigger editor (``game_window._draw_trigger_editor``
et al) but operates on RPG-request dicts rather than economic events.
The structure is:

    +---------------------------------------------------------------+
    | RPG REQUEST EDITOR                                            |
    | edit data/rpg_requests.json — NPC dialogues with choices      |
    |                                                               |
    | +-----------+  +-------------------------------------------+  |
    | | request   |  | id:           [_____]                     |  |
    | | list      |  | npc name:     [_____]                     |  |
    | | (scrolly) |  | npc portrait: [_____]                     |  |
    | |           |  | scene:        [_____]                     |  |
    | | * caesar  |  | text: (multiline)                         |  |
    | | * priest  |  |       [__________________________]        |  |
    | | * general |  |                                           |  |
    | |           |  | resources preview:  money: -1000          |  |
    | |           |  | trigger flag: [_____]                     |  |
    | |           |  | trigger event:[_____]                     |  |
    | |           |  |                                           |  |
    | |           |  | DECISIONS:                                |  |
    | |           |  |   1) [Yes immediately ] delay=0  ...     |  |
    | |           |  |   2) [Yes next month  ] delay=600 ...    |  |
    | |           |  |   ...                                     |  |
    | |           |  | [+ Add decision] [- Remove decision]      |  |
    | +-----------+  +-------------------------------------------+  |
    |                                                               |
    | [+ Add] [- Remove]              [Cancel]  [Save]              |
    +---------------------------------------------------------------+

The editor is intentionally implemented as a *standalone module* —
``game_window.py`` is already 10k lines and the trigger editor alone
adds 1.2k lines to it. We hold this one in its own file and patch
``game_window.py`` with just the dispatch/state-init plumbing.

Save round-trips through ``rpg_requests.save_requests``, which uses
the same atomic ``.tmp → rename`` pattern as the other editors.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import arcade

from constants import (
    COLOR_GOLD, COLOR_GRAY, COLOR_RED, COLOR_UI_BORDER, COLOR_WHITE,
)
from rpg_requests import (
    load_requests, make_blank_decision, make_blank_request, save_requests,
)

log = logging.getLogger(__name__)


# ── Layout constants ─────────────────────────────────────────────────
# Tuned to feel right at the default 1280×800 window. The editor
# clamps to screen if the window is smaller (same pattern as the
# trigger editor in game_window.py).
PANEL_W = 1100
PANEL_H = 740
LIST_W = 220
ROW_H = 22
# v0.32: bumped from 26 → 46 so each decision row can host a second
# visual line. Line 1 keeps the v0.31 layout (label/delay/happy/flag/
# event/effects-summary); line 2 hosts the new ``diplomacy`` ± stepper
# (split out from happy so authors can move citizen happiness and
# Roman-favour standing independently). Two visual lines per decision
# trades off vertical density for clarity — at typical 3-5 decisions
# per request the box still fits comfortably without scrolling.
DEC_ROW_H = 46
TEXT_MAX = 240
ID_MAX = 48
NPC_NAME_MAX = 32
LABEL_MAX = 32

# Resource keys offered in the resources preview / decision-effects
# pickers. Same set the trigger editor uses, kept in sync manually —
# both editors deliberately don't try to import the economy module
# (which pulls in the world).
RESOURCE_KEYS: list[str] = [
    "money", "food", "wood", "stone", "iron", "iron_ore",
    "weapons", "tools", "wheat", "flour", "planks",
    "stone_blocks", "bread",
]


# v0.32: file-browser dialog for NPC portraits and background scenes.
# The two folders live under ./assets/textures/ next to the rest of
# the texture library. Authors drop PNG/JPG files in the folder and
# the RPG editor's "Browse…" button lists whatever is there.
#
# Resolved off the project root rather than the cwd so the editor
# works regardless of where ``python main.py`` was invoked from
# (mirrors the constants.py ROOT_DIR pattern).
from pathlib import Path as _Path  # noqa: E402 (intentional alias)

_ASSETS_ROOT = _Path(__file__).resolve().parent / "assets" / "textures"
PORTRAIT_DIR = _ASSETS_ROOT / "portraits"
SCENE_DIR = _ASSETS_ROOT / "scene"

# Image extensions the browser will list. We accept both PNG and JPG
# (and the JPEG long form) — the underlying ``textures.py`` loader
# already picks whichever exists at lookup time, so we don't enforce a
# single extension at edit time. Lower-case comparison only; the
# browser is case-insensitive on extensions.
_IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg")


def _list_browser_files(directory: _Path) -> list[str]:
    """List image file *basenames* (without extension) in ``directory``.

    Returns an empty list when the directory is missing — the browser
    renders a "(empty)" placeholder rather than throwing. Files are
    sorted alphabetically so the order is stable across runs and
    independent of filesystem listing order.

    We deliberately strip the extension because that's what gets
    persisted into ``npc_portrait`` / ``background_scene`` — the
    runtime texture lookup re-adds whichever extension is on disk. If
    a folder contains both ``messenger.png`` and ``messenger.jpg`` we
    de-dup so the user sees one entry; the loader picks the file by
    extension preference.
    """
    try:
        names = []
        seen: set[str] = set()
        for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in _IMAGE_EXTS:
                continue
            stem = entry.stem
            if stem in seen:
                continue
            seen.add(stem)
            names.append(stem)
        return names
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []


class RpgRequestEditorState:
    """Holds editor state for the splash-menu RPG request editor.

    A single instance lives on the game window as
    ``window.rpg_request_editor_state`` and is created lazily the
    first time the editor is opened. The state is *not* tied to the
    world (the editor runs over a frozen empty world; same pattern
    as the trigger editor).

    Fields
    ------
    open : bool
        Whether the editor modal is active. Drives the on_draw /
        on_mouse_press dispatch in game_window.
    requests : list[dict]
        Working copy of the request library. On Save this is written
        back to disk via ``rpg_requests.save_requests`` and the
        editor reloads from disk so the in-memory copy matches the
        canonical file.
    selected_idx : int
        Index into ``requests`` of the currently selected entry. -1
        when ``requests`` is empty.
    scroll : int
        Vertical scroll offset for the left-column request list.
    dec_scroll : int
        Vertical scroll offset for the decisions list (right pane
        bottom half). Decisions can be more numerous than the visible
        rows; this keeps them scrollable.
    text_field : str | None
        Which text input is currently focused for typing. Values:
        - None — no text field focused (clicks go through normal
          buttons).
        - "id" / "npc_name" / "npc_portrait" / "background_scene" /
          "request_text" / "trigger_flag" / "trigger_event" —
          request-level text fields.
        - "dec:<i>:label" / "dec:<i>:set_flag" / "dec:<i>:fire_event"
          — per-decision text fields, where ``<i>`` is the index of
          the decision being edited.
        - "res:<key>" — typing into the resources preview for the
          given resource key. We need the picker to know which row.
        - "decres:<i>:<key>" — typing into decision i's effects for
          resource key. Same as above but for the per-decision pane.
    text_buffer : str
        The string currently being typed. On Enter/click-elsewhere we
        commit it into the relevant field.
    dirty : bool
        Has the user made unsaved changes? Used to (a) highlight the
        Save button when there's something to save and (b) prompt
        on Cancel (TBD — for v0.31 we just discard, matching the
        trigger editor's behaviour).
    btn_rects : list[tuple]
        Hit-test rectangles rebuilt each frame in draw() and consumed
        on the next click. Tuple shape:
        ``(left, right, bottom, top, action_string)``.
    """

    def __init__(self) -> None:
        self.open: bool = False
        self.requests: list[dict[str, Any]] = []
        self.selected_idx: int = -1
        self.scroll: int = 0
        self.dec_scroll: int = 0
        self.text_field: str | None = None
        self.text_buffer: str = ""
        self.dirty: bool = False
        self.btn_rects: list[tuple[float, float, float, float, str]] = []
        # Lazy text-pool — same trick the trigger editor uses to
        # avoid allocating arcade.Text() per frame. Keys are stable
        # ids (e.g. "title", "row_3", "dec_label_0"); values are the
        # cached arcade.Text instances.
        self._txt_pool: dict[str, arcade.Text] = {}
        # v0.32: file-browser overlay. ``browser_target`` is the field
        # the browser is currently picking *for* — either None (no
        # browser open), "npc_portrait", or "background_scene". The
        # overlay is modal: while it's open the rest of the editor's
        # button rects are ignored. ``browser_scroll`` is the vertical
        # scroll offset for the (potentially many) image files in the
        # target folder.
        self.browser_target: str | None = None
        self.browser_scroll: int = 0
        self.browser_btn_rects: list[
            tuple[float, float, float, float, str]
        ] = []


# ── Open / close ──────────────────────────────────────────────────────

def open_editor(window: Any, state: RpgRequestEditorState) -> None:
    """Load ``data/rpg_requests.json`` into the editor's working list
    and mark the editor as open.

    Mirrors ``_open_trigger_editor`` in game_window.py — the caller
    (a splash button) is responsible for resetting the world to a
    safe empty state and freezing the simulation first. This function
    only handles editor state.
    """
    try:
        reqs = load_requests()
    except Exception as e:  # noqa: BLE001 — surface anything to the user
        log.exception("RPG request editor: load failed")
        if hasattr(window, "_notify"):
            window._notify(f"Load failed: {e}", COLOR_RED)
        return
    state.requests = reqs
    state.selected_idx = 0 if reqs else -1
    state.scroll = 0
    state.dec_scroll = 0
    state.text_field = None
    state.text_buffer = ""
    state.dirty = False
    state.open = True
    log.info("RPG request editor: opened with %d requests", len(reqs))


def close_editor(state: RpgRequestEditorState) -> None:
    """Close without saving. The trigger editor follows the same
    discard-on-Cancel pattern; future work can add an "unsaved
    changes?" confirm dialog if playtesters ask for it."""
    state.open = False
    state.text_field = None
    state.text_buffer = ""
    log.info("RPG request editor: closed (dirty=%s)", state.dirty)


def save_and_close(window: Any, state: RpgRequestEditorState) -> None:
    """Write the working list to disk and close the editor.

    Atomic via ``rpg_requests.save_requests``. On failure we leave
    the editor open so the user can retry (the trigger editor does
    the same).
    """
    try:
        save_requests(state.requests)
    except OSError as e:
        log.exception("RPG request editor: save failed")
        if hasattr(window, "_notify"):
            window._notify(f"Save failed: {e}", COLOR_RED)
        return
    state.dirty = False
    if hasattr(window, "_notify"):
        window._notify(
            f"Saved {len(state.requests)} RPG requests", COLOR_GOLD,
        )
    state.open = False


# ── Drawing helpers ──────────────────────────────────────────────────

def _txt(state: RpgRequestEditorState, key: str, x: float, y: float,
         size: int = 12, color: tuple = COLOR_WHITE, bold: bool = False,
         anchor_x: str = "left") -> arcade.Text:
    """Lazy cache for arcade.Text instances — same pattern as the
    trigger editor's ``te_text`` closure."""
    t = state._txt_pool.get(key)
    if t is None:
        t = arcade.Text("", x, y, color, size,
                        bold=bold, anchor_x=anchor_x)
        state._txt_pool[key] = t
    t.x = x
    t.y = y
    t.color = color
    return t


def _draw_text_field(state: RpgRequestEditorState, key: str,
                     l: float, r: float, b: float, t: float,
                     value: str, action: str) -> None:
    """Render a single-line text input box. ``action`` is the string
    pushed onto ``btn_rects`` that the click handler uses to focus
    this field when the user clicks it.

    If the field is currently focused (state.text_field == action's
    field name), draw the buffer + a trailing cursor caret so the
    player can see what they're typing.
    """
    # Strip the "focus:" / "click:" prefix if any to figure out the
    # focus state. The action string passed in is the bare field
    # name (e.g. "id", "npc_name", "dec:2:label").
    focused = (state.text_field == action)
    fill = (40, 32, 24) if not focused else (60, 50, 36)
    border = COLOR_GOLD if focused else COLOR_UI_BORDER
    arcade.draw_lrbt_rectangle_filled(l, r, b, t, fill)
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, border, 1)
    display = state.text_buffer if focused else value
    # Truncate so we never overflow the box visually; the buffer
    # itself isn't truncated (we just clip the render).
    max_chars = max(4, int((r - l) / 7))
    if len(display) > max_chars:
        display = "…" + display[-(max_chars - 1):]
    if focused:
        display = display + "|"
    txt = _txt(state, f"txt_{action}", l + 6, b + 4, 11, COLOR_WHITE)
    txt.text = display
    txt.draw()
    state.btn_rects.append((l, r, b, t, f"focus:{action}"))


def _draw_step_button(state: RpgRequestEditorState,
                      l: float, r: float, b: float, t: float,
                      label: str, action: str,
                      fill: tuple = (60, 50, 40)) -> None:
    """Tiny clickable button for ± steppers next to numeric fields."""
    arcade.draw_lrbt_rectangle_filled(l, r, b, t, fill)
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_UI_BORDER, 1)
    txt = _txt(state, f"btn_{action}", (l + r) / 2, b + 3, 10,
               COLOR_WHITE, True, anchor_x="center")
    txt.text = label
    txt.draw()
    state.btn_rects.append((l, r, b, t, action))


def _draw_browse_button(state: RpgRequestEditorState,
                        l: float, r: float, b: float, t: float,
                        label: str, action: str) -> None:
    """v0.32: small gold-tinted button that pops the file-browser
    overlay. Visually distinct from the ± steppers (slightly larger
    fill, gold border) so the author scans the form and immediately
    spots the affordance — typing a name still works, but the button
    is the discoverable path for new authors who don't know what
    portraits ship with the game."""
    arcade.draw_lrbt_rectangle_filled(l, r, b, t, (70, 60, 40))
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 1)
    txt = _txt(state, f"btn_{action}", (l + r) / 2, b + 3, 9,
               COLOR_GOLD, True, anchor_x="center")
    txt.text = label
    txt.draw()
    state.btn_rects.append((l, r, b, t, action))


# ── Main draw entry point ────────────────────────────────────────────

def draw(window: Any, state: RpgRequestEditorState) -> None:
    """Render the editor modal. Called from game_window.on_draw when
    ``state.open`` is True.

    Rebuilds ``state.btn_rects`` each frame; the click handler
    consumes that list on the next click.
    """
    state.btn_rects = []

    # Dim the world behind the modal.
    arcade.draw_lrbt_rectangle_filled(
        0, window.width, 0, window.height, (0, 0, 0, 170),
    )
    cx = window.width / 2
    cy = window.height / 2
    pw = min(PANEL_W, window.width - 40)
    ph = min(PANEL_H, window.height - 40)
    l = cx - pw / 2
    r = cx + pw / 2
    b = cy - ph / 2
    t = cy + ph / 2

    arcade.draw_lrbt_rectangle_filled(l, r, b, t, (35, 28, 22, 245))
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

    # Header.
    title = _txt(state, "title", cx, t - 26, 14, COLOR_GOLD, True,
                 anchor_x="center")
    title.text = "RPG REQUEST EDITOR"
    title.draw()
    hint = _txt(state, "hint", cx, t - 44, 10, COLOR_GRAY,
                anchor_x="center")
    hint.text = (
        "Edit data/rpg_requests.json — NPC dialogues with multi-choice"
        " decisions for storytelling, strategy, and immersion"
    )
    hint.draw()

    # Left column: request list.
    list_l = l + 14
    list_r = list_l + LIST_W
    list_t = t - 64
    list_b = b + 56
    _draw_request_list(state, list_l, list_r, list_b, list_t)

    # Right pane: form (or "no selection" placeholder).
    form_l = list_r + 30
    form_r = r - 14
    form_t = list_t
    form_b = list_b
    if 0 <= state.selected_idx < len(state.requests):
        _draw_request_form(state, form_l, form_r, form_b, form_t)
    else:
        _draw_empty_placeholder(state, form_l, form_r, form_b, form_t)

    # Footer.
    _draw_footer(state, l, r, b)

    # v0.32: File-browser overlay. Drawn last (on top of everything
    # else inside the modal) when the author has clicked one of the
    # "Browse…" buttons. While the overlay is open the underlying
    # buttons are still drawn but their hit-rects are ignored by the
    # click handler — see ``handle_click``.
    if state.browser_target is not None:
        _draw_file_browser(state, window)


def _draw_request_list(state: RpgRequestEditorState,
                       list_l: float, list_r: float,
                       list_b: float, list_t: float) -> None:
    """Left column: scrollable list of requests + per-list add/remove."""
    arcade.draw_lrbt_rectangle_filled(
        list_l, list_r, list_b, list_t, (22, 18, 14, 230),
    )
    arcade.draw_lrbt_rectangle_outline(
        list_l, list_r, list_b, list_t, COLOR_UI_BORDER, 1,
    )
    rows_visible = max(1, int((list_t - list_b) / ROW_H))
    reqs = state.requests
    max_scroll = max(0, len(reqs) - rows_visible)
    state.scroll = max(0, min(state.scroll, max_scroll))

    # Scroll arrows on the right edge of the list.
    sbtn_w = 20
    sx1 = list_r + 2
    sx2 = sx1 + sbtn_w
    for action, ay_b, ay_t, lbl in (
        ("list_scroll_up", list_t - 22, list_t, "^"),
        ("list_scroll_down", list_b, list_b + 22, "v"),
    ):
        arcade.draw_lrbt_rectangle_filled(sx1, sx2, ay_b, ay_t, (60, 50, 40))
        arcade.draw_lrbt_rectangle_outline(
            sx1, sx2, ay_b, ay_t, COLOR_UI_BORDER, 1,
        )
        tt = _txt(state, f"sb_{action}", (sx1 + sx2) / 2, ay_b + 4,
                  12, COLOR_WHITE, True, anchor_x="center")
        tt.text = lbl
        tt.draw()
        state.btn_rects.append((sx1, sx2, ay_b, ay_t, action))

    # Rows.
    for vi in range(rows_visible):
        idx = state.scroll + vi
        if idx >= len(reqs):
            break
        req = reqs[idx]
        row_top = list_t - vi * ROW_H
        row_bot = row_top - ROW_H
        selected = (idx == state.selected_idx)
        if selected:
            arcade.draw_lrbt_rectangle_filled(
                list_l, list_r, row_bot, row_top, (90, 70, 50, 220),
            )
        # Row content: NPC label colour swatch left, id text right.
        # (We could load and thumbnail the portrait image here; not
        # in v0.31, the swatch is enough for navigation.)
        arcade.draw_lrbt_rectangle_filled(
            list_l + 4, list_l + 14, row_bot + 5, row_bot + 15,
            (160, 130, 70),
        )
        arcade.draw_lrbt_rectangle_outline(
            list_l + 4, list_l + 14, row_bot + 5, row_bot + 15,
            COLOR_UI_BORDER, 1,
        )
        rt = _txt(state, f"row_{vi}", list_l + 20, row_bot + 5, 11,
                  COLOR_GOLD if selected else COLOR_WHITE)
        name = str(req.get("id") or req.get("npc_name") or "(unnamed)")
        rt.text = name if len(name) <= 22 else name[:21] + "…"
        rt.draw()
        state.btn_rects.append(
            (list_l, list_r, row_bot, row_top, f"select:{idx}")
        )

    # Footer: + Add / - Remove for the list.
    add_l = list_l
    add_r = list_l + LIST_W // 2 - 4
    rem_l = add_r + 8
    rem_r = list_r
    ay_t = list_b - 4
    ay_b = ay_t - 24
    for x1, x2, action, lbl, fill in (
        (add_l, add_r, "add_request", "+ Add", (50, 80, 50)),
        (rem_l, rem_r, "remove_request", "− Remove", (80, 50, 40)),
    ):
        arcade.draw_lrbt_rectangle_filled(x1, x2, ay_b, ay_t, fill)
        arcade.draw_lrbt_rectangle_outline(
            x1, x2, ay_b, ay_t, COLOR_UI_BORDER, 1,
        )
        tt = _txt(state, f"foot_{action}", (x1 + x2) / 2, ay_b + 6,
                  11, COLOR_WHITE, True, anchor_x="center")
        tt.text = lbl
        tt.draw()
        state.btn_rects.append((x1, x2, ay_b, ay_t, action))


def _draw_empty_placeholder(state: RpgRequestEditorState,
                            l: float, r: float, b: float, t: float) -> None:
    """When no request is selected (empty list) tell the user how to
    get started."""
    msg = _txt(state, "empty", (l + r) / 2, (b + t) / 2, 13,
               COLOR_GRAY, anchor_x="center")
    msg.text = "No requests yet. Click \"+ Add\" to create one."
    msg.draw()


def _draw_request_form(state: RpgRequestEditorState,
                       l: float, r: float, b: float, t: float) -> None:
    """Right pane: editor for the selected request."""
    req = state.requests[state.selected_idx]

    # Row 1: id + npc name.
    y = t - 24
    lbl = _txt(state, "lbl_id", l, y, 11, COLOR_GOLD, True)
    lbl.text = "ID:"
    lbl.draw()
    _draw_text_field(state, "id", l + 30, l + 220, y - 18, y - 2,
                     str(req.get("id", "")), "id")
    lbl2 = _txt(state, "lbl_npc", l + 240, y, 11, COLOR_GOLD, True)
    lbl2.text = "NPC name:"
    lbl2.draw()
    _draw_text_field(state, "npc_name", l + 320, l + 510, y - 18, y - 2,
                     str(req.get("npc_name", "")), "npc_name")

    # Row 2: portrait + scene. v0.32 — each field gets a tiny
    # "Browse…" button next to it that pops the file-browser overlay,
    # listing PNGs/JPGs from the corresponding ``./assets/textures/``
    # subfolder. The text field still accepts free typing for authors
    # who'd rather type a name (or for portraits not yet on disk).
    y -= 32
    lbl3 = _txt(state, "lbl_portrait", l, y, 11, COLOR_GOLD, True)
    lbl3.text = "Portrait:"
    lbl3.draw()
    _draw_text_field(state, "portrait", l + 70, l + 230, y - 18, y - 2,
                     str(req.get("npc_portrait", "")), "npc_portrait")
    _draw_browse_button(state, l + 234, l + 294, y - 18, y - 2,
                        "Browse…", "browse:npc_portrait")
    lbl4 = _txt(state, "lbl_scene", l + 310, y, 11, COLOR_GOLD, True)
    lbl4.text = "Scene:"
    lbl4.draw()
    _draw_text_field(state, "scene", l + 360, l + 540, y - 18, y - 2,
                     str(req.get("background_scene", "")), "background_scene")
    _draw_browse_button(state, l + 544, l + 604, y - 18, y - 2,
                        "Browse…", "browse:background_scene")

    # Row 3: request text (wider, single line — multiline editing is
    # out-of-scope for v0.31; future revision will swap this for a
    # word-wrap input).
    y -= 32
    lbl5 = _txt(state, "lbl_text", l, y, 11, COLOR_GOLD, True)
    lbl5.text = "Request text:"
    lbl5.draw()
    _draw_text_field(state, "text", l, l + (r - l) - 8, y - 38, y - 18,
                     str(req.get("request_text", "")), "request_text")

    # Row 4: trigger flag + event.
    y -= 56
    lbl6 = _txt(state, "lbl_flag", l, y, 11, COLOR_GOLD, True)
    lbl6.text = "Trigger flag:"
    lbl6.draw()
    _draw_text_field(state, "tflag", l + 95, l + 290, y - 18, y - 2,
                     str(req.get("trigger_flag") or ""), "trigger_flag")
    lbl7 = _txt(state, "lbl_event", l + 300, y, 11, COLOR_GOLD, True)
    lbl7.text = "Trigger event:"
    lbl7.draw()
    _draw_text_field(state, "tevent", l + 400, l + 595, y - 18, y - 2,
                     str(req.get("trigger_event") or ""), "trigger_event")

    # Row 5: resources preview header + per-resource lines.
    y -= 36
    lbl8 = _txt(state, "lbl_res", l, y, 12, COLOR_GOLD, True)
    lbl8.text = "Resources (preview shown to player):"
    lbl8.draw()
    y -= 22
    res = req.get("resources") or {}
    # Render up to 6 resource rows in two columns.
    for i, key in enumerate(RESOURCE_KEYS[:8]):
        col = i // 4
        row = i % 4
        rx = l + col * 250
        ry = y - row * 22
        val = res.get(key)
        # Resource label.
        kk = _txt(state, f"res_lbl_{key}", rx, ry, 10,
                  COLOR_WHITE if val else COLOR_GRAY)
        kk.text = key + ":"
        kk.draw()
        # ± buttons + value display. Click the value to edit.
        bx1, bx2 = rx + 80, rx + 100
        by1, by2 = ry - 3, ry + 13
        _draw_step_button(state, bx1, bx2, by1, by2, "−",
                          f"res_dec:{key}")
        bx1, bx2 = rx + 105, rx + 175
        arcade.draw_lrbt_rectangle_filled(bx1, bx2, by1, by2, (40, 32, 24))
        arcade.draw_lrbt_rectangle_outline(
            bx1, bx2, by1, by2, COLOR_UI_BORDER, 1,
        )
        vt = _txt(state, f"res_val_{key}", (bx1 + bx2) / 2, by1 + 2, 10,
                  COLOR_WHITE if val else COLOR_GRAY,
                  anchor_x="center")
        vt.text = str(int(val)) if val else "—"
        vt.draw()
        bx1, bx2 = rx + 180, rx + 200
        _draw_step_button(state, bx1, bx2, by1, by2, "+",
                          f"res_inc:{key}")

    # Decisions list (lower half of the form).
    dec_top = y - 4 * 22 - 12
    _draw_decisions(state, req, l, r, b, dec_top)


def _draw_decisions(state: RpgRequestEditorState, req: dict[str, Any],
                    l: float, r: float, b: float, top: float) -> None:
    """Bottom half of the form: scrollable decisions list."""
    hdr = _txt(state, "dec_hdr", l, top, 12, COLOR_GOLD, True)
    hdr.text = "Decisions (player's choice panel):"
    hdr.draw()

    # Decision list box.
    box_l = l
    box_r = r - 14
    box_t = top - 16
    box_b = b + 38   # leave room for the per-list +/- footer
    arcade.draw_lrbt_rectangle_filled(
        box_l, box_r, box_b, box_t, (22, 18, 14, 230),
    )
    arcade.draw_lrbt_rectangle_outline(
        box_l, box_r, box_b, box_t, COLOR_UI_BORDER, 1,
    )
    decs = req.get("decisions") or []
    rows_visible = max(1, int((box_t - box_b) / DEC_ROW_H))
    max_scroll = max(0, len(decs) - rows_visible)
    state.dec_scroll = max(0, min(state.dec_scroll, max_scroll))

    # Decision scroll arrows.
    sbtn_w = 18
    sx1 = box_r + 2
    sx2 = sx1 + sbtn_w
    for action, ay_b, ay_t, lbl in (
        ("dec_scroll_up", box_t - 20, box_t, "^"),
        ("dec_scroll_down", box_b, box_b + 20, "v"),
    ):
        arcade.draw_lrbt_rectangle_filled(sx1, sx2, ay_b, ay_t, (60, 50, 40))
        arcade.draw_lrbt_rectangle_outline(
            sx1, sx2, ay_b, ay_t, COLOR_UI_BORDER, 1,
        )
        tt = _txt(state, f"dsb_{action}", (sx1 + sx2) / 2, ay_b + 3,
                  11, COLOR_WHITE, True, anchor_x="center")
        tt.text = lbl
        tt.draw()
        state.btn_rects.append((sx1, sx2, ay_b, ay_t, action))

    for vi in range(rows_visible):
        idx = state.dec_scroll + vi
        if idx >= len(decs):
            break
        dec = decs[idx]
        row_top = box_t - vi * DEC_ROW_H
        row_bot = row_top - DEC_ROW_H

        # v0.32: each decision is now rendered on TWO visual lines so
        # we can fit the new `diplomacy` ± stepper without re-flowing
        # the v0.31 layout. We anchor everything off ``row_top`` rather
        # than ``row_bot`` so adding a third line later wouldn't shift
        # the upper fields.
        # Line 1 sits in the upper ~22px of the row; line 2 below it.
        line1_b = row_top - 21
        line1_t = row_top - 3
        line2_b = row_top - 43
        line2_t = row_top - 25

        # Faint horizontal separator between adjacent decisions so the
        # two-line groupings read as units. Skip on the first visible
        # row (the box border already separates it from the header).
        if vi > 0:
            arcade.draw_lrbt_rectangle_filled(
                box_l + 4, box_r - 4, row_top - 1, row_top,
                (60, 50, 40, 180),
            )

        # Row number — vertically centred across both lines.
        num = _txt(state, f"dec_num_{vi}", box_l + 6,
                   (line1_b + line2_t) / 2 - 6, 11,
                   COLOR_GOLD, True)
        num.text = f"{idx + 1})"
        num.draw()

        # ── Line 1 ───────────────────────────────────────────────
        # Label (clickable text field).
        _draw_text_field(state, f"dec_label_{vi}",
                         box_l + 30, box_l + 210, line1_b, line1_t,
                         str(dec.get("label", "")), f"dec:{idx}:label")

        # Delay ticks — ± steppers + value.
        dl = _txt(state, f"dec_dly_{vi}", box_l + 220, line1_b + 5, 10,
                  COLOR_GRAY)
        dl.text = "delay:"
        dl.draw()
        _draw_step_button(state, box_l + 258, box_l + 274,
                          line1_b + 2, line1_t - 2, "−",
                          f"dec_delay_dec:{idx}")
        dv = _txt(state, f"dec_dlyv_{vi}", box_l + 300, line1_b + 4, 10,
                  COLOR_WHITE, anchor_x="center")
        dv.text = str(int(dec.get("delay_ticks", 0)))
        dv.draw()
        _draw_step_button(state, box_l + 320, box_l + 336,
                          line1_b + 2, line1_t - 2, "+",
                          f"dec_delay_inc:{idx}")

        # Happy delta — ± steppers + value.
        hl = _txt(state, f"dec_hl_{vi}", box_l + 348, line1_b + 5, 10,
                  COLOR_GRAY)
        hl.text = "happy:"
        hl.draw()
        _draw_step_button(state, box_l + 388, box_l + 404,
                          line1_b + 2, line1_t - 2, "−",
                          f"dec_happy_dec:{idx}")
        hv = _txt(state, f"dec_hv_{vi}", box_l + 425, line1_b + 4, 10,
                  COLOR_WHITE, anchor_x="center")
        hv.text = str(int(dec.get("happy", 0)))
        hv.draw()
        _draw_step_button(state, box_l + 445, box_l + 461,
                          line1_b + 2, line1_t - 2, "+",
                          f"dec_happy_inc:{idx}")

        # Set flag + fire event (compact, both on line 1).
        _draw_text_field(state, f"dec_flag_{vi}",
                         box_l + 475, box_l + 620, line1_b, line1_t,
                         str(dec.get("set_flag") or ""),
                         f"dec:{idx}:set_flag")
        _draw_text_field(state, f"dec_ev_{vi}",
                         box_l + 628, box_l + 780, line1_b, line1_t,
                         str(dec.get("fire_event") or ""),
                         f"dec:{idx}:fire_event")

        # ── Line 2 ───────────────────────────────────────────────
        # v0.32: Diplomacy ± stepper. Same shape as the happy stepper
        # above but on the second line. Step is ±1 — diplomacy is on
        # the same coarse 0..100 scale as the existing senate
        # accrual, so single-point bumps are the natural granularity.
        dpl = _txt(state, f"dec_dpl_{vi}", box_l + 30, line2_b + 5, 10,
                   COLOR_GOLD, True)
        dpl.text = "diplomacy:"
        dpl.draw()
        _draw_step_button(state, box_l + 100, box_l + 116,
                          line2_b + 2, line2_t - 2, "−",
                          f"dec_diplo_dec:{idx}")
        dpv = _txt(state, f"dec_dplv_{vi}", box_l + 140, line2_b + 4, 10,
                   COLOR_WHITE, anchor_x="center")
        dp_val = int(dec.get("diplomacy", 0))
        dpv.text = f"{dp_val:+d}" if dp_val != 0 else "0"
        # Tint the value green for bonus, red for malus, white for zero
        # so the author can scan a list of decisions and immediately
        # see which ones reward/penalise Roman standing.
        if dp_val > 0:
            dpv.color = (90, 200, 110)
        elif dp_val < 0:
            dpv.color = (220, 90, 90)
        else:
            dpv.color = COLOR_GRAY
        dpv.draw()
        _draw_step_button(state, box_l + 160, box_l + 176,
                          line2_b + 2, line2_t - 2, "+",
                          f"dec_diplo_inc:{idx}")

        # Effect summary — moved to line 2 alongside diplomacy. v0.31
        # kept this at the right edge of line 1; v0.32 moves it down
        # so line 1 can keep the set_flag/fire_event fields readable.
        eff = dec.get("effects") or {}
        es = _txt(state, f"dec_es_{vi}", box_l + 200, line2_b + 4, 10,
                  COLOR_GRAY if not eff else COLOR_WHITE)
        es.text = f"effects: {len(eff)}  (click to sync from preview)"
        es.draw()
        # Hit rect must match the *visual* location of the effects-
        # summary text on line 2 — without this fix the v0.31 click
        # zone would point at the (now empty) right edge of line 1
        # and the player couldn't actually sync from the preview.
        state.btn_rects.append(
            (box_l + 195, box_l + 500, line2_b, line2_t,
             f"dec_sync_effects:{idx}")
        )

    # Add/Remove decision buttons.
    foot_t = box_b - 4
    foot_b = foot_t - 24
    add_l = box_l
    add_r = box_l + 130
    rem_l = add_r + 8
    rem_r = rem_l + 130
    for x1, x2, action, lbl, fill in (
        (add_l, add_r, "add_decision",
         "+ Add decision", (50, 80, 50)),
        (rem_l, rem_r, "remove_decision",
         "− Remove decision", (80, 50, 40)),
    ):
        arcade.draw_lrbt_rectangle_filled(x1, x2, foot_b, foot_t, fill)
        arcade.draw_lrbt_rectangle_outline(
            x1, x2, foot_b, foot_t, COLOR_UI_BORDER, 1,
        )
        tt = _txt(state, f"dec_foot_{action}", (x1 + x2) / 2,
                  foot_b + 6, 11, COLOR_WHITE, True, anchor_x="center")
        tt.text = lbl
        tt.draw()
        state.btn_rects.append((x1, x2, foot_b, foot_t, action))


def _draw_footer(state: RpgRequestEditorState,
                 l: float, r: float, b: float) -> None:
    """Bottom-of-modal Cancel/Save buttons."""
    by_t = b + 36
    by_b = b + 12
    cancel_l = r - 220
    cancel_r = r - 120
    save_l = r - 110
    save_r = r - 14
    for x1, x2, action, lbl, fill in (
        (cancel_l, cancel_r, "cancel", "Cancel", (60, 50, 40)),
        (save_l, save_r, "save", "Save",
         (60, 110, 60) if state.dirty else (70, 90, 70)),
    ):
        arcade.draw_lrbt_rectangle_filled(x1, x2, by_b, by_t, fill)
        arcade.draw_lrbt_rectangle_outline(
            x1, x2, by_b, by_t, COLOR_UI_BORDER, 1,
        )
        tt = _txt(state, f"main_foot_{action}", (x1 + x2) / 2,
                  by_b + 8, 12, COLOR_WHITE, True, anchor_x="center")
        tt.text = lbl
        tt.draw()
        state.btn_rects.append((x1, x2, by_b, by_t, action))
    # Dirty marker.
    if state.dirty:
        d = _txt(state, "dirty", l + 14, b + 18, 10, COLOR_GOLD)
        d.text = "● unsaved changes"
        d.draw()


# v0.32: file-browser overlay rows are 22px each. Constants here so the
# click handler and the draw function agree on row geometry without
# passing them around.
_BROWSER_ROW_H = 22
_BROWSER_PANEL_W = 420
_BROWSER_PANEL_H = 460


def _draw_file_browser(state: RpgRequestEditorState, window: Any) -> None:
    """Render the file-browser modal overlay.

    Lists every image basename in the target folder (``portraits/``
    for ``npc_portrait``, ``scene/`` for ``background_scene``). Each
    row is clickable: a click writes the basename into the underlying
    text field, clears the buffer, and closes the overlay. A "Cancel"
    button and clicks-outside dismiss without picking anything.

    The overlay is purely informational — it doesn't try to render
    image thumbnails (that would require texture decoding off the
    rendering thread, and the existing editor doesn't do that either).
    A future revision can swap in proper thumbnails by re-using the
    ``textures.TextureRegistry`` cache; for v0.32 the file *name* is
    the affordance.
    """
    state.browser_btn_rects = []
    # Full-screen scrim so the overlay reads as modal.
    arcade.draw_lrbt_rectangle_filled(
        0, window.width, 0, window.height, (0, 0, 0, 180),
    )

    cx = window.width / 2
    cy = window.height / 2
    pw = min(_BROWSER_PANEL_W, window.width - 40)
    ph = min(_BROWSER_PANEL_H, window.height - 40)
    l = cx - pw / 2
    r = cx + pw / 2
    b = cy - ph / 2
    t = cy + ph / 2

    arcade.draw_lrbt_rectangle_filled(l, r, b, t, (40, 32, 26, 250))
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 2)

    # Title (which folder we're browsing).
    target = state.browser_target or ""
    if target == "npc_portrait":
        folder = PORTRAIT_DIR
        title_text = "BROWSE PORTRAITS"
    elif target == "background_scene":
        folder = SCENE_DIR
        title_text = "BROWSE SCENES"
    else:
        # Defensive — should never hit; we fall back to portraits.
        folder = PORTRAIT_DIR
        title_text = "BROWSE FILES"

    title = _txt(state, "br_title", cx, t - 26, 13, COLOR_GOLD, True,
                 anchor_x="center")
    title.text = title_text
    title.draw()

    # Show the folder path so the author knows where to drop new
    # images. Truncated from the *left* (with "…") if the path is
    # long, since the meaningful part is the trailing subfolder name.
    folder_str = str(folder)
    if len(folder_str) > 56:
        folder_str = "…" + folder_str[-55:]
    pth = _txt(state, "br_path", cx, t - 44, 9, COLOR_GRAY,
               anchor_x="center")
    pth.text = folder_str
    pth.draw()

    # ── List rows ─────────────────────────────────────────────────
    files = _list_browser_files(folder)
    list_l = l + 14
    list_r = r - 14
    list_t = t - 64
    list_b = b + 50
    arcade.draw_lrbt_rectangle_filled(
        list_l, list_r, list_b, list_t, (22, 18, 14, 230),
    )
    arcade.draw_lrbt_rectangle_outline(
        list_l, list_r, list_b, list_t, COLOR_UI_BORDER, 1,
    )

    rows_visible = max(1, int((list_t - list_b) / _BROWSER_ROW_H))
    max_scroll = max(0, len(files) - rows_visible)
    state.browser_scroll = max(0, min(state.browser_scroll, max_scroll))

    if not files:
        msg = _txt(state, "br_empty", cx, (list_b + list_t) / 2, 11,
                   COLOR_GRAY, anchor_x="center")
        msg.text = "(no images found — drop PNG/JPG files in the folder)"
        msg.draw()
    else:
        for vi in range(rows_visible):
            idx = state.browser_scroll + vi
            if idx >= len(files):
                break
            name = files[idx]
            row_top = list_t - vi * _BROWSER_ROW_H
            row_bot = row_top - _BROWSER_ROW_H
            # Alternating row fill for readability — every other row
            # gets a slightly lighter tint so the eye can scan a long
            # list without losing its place.
            if vi % 2 == 1:
                arcade.draw_lrbt_rectangle_filled(
                    list_l + 1, list_r - 1, row_bot, row_top,
                    (32, 26, 20, 230),
                )
            rt = _txt(state, f"br_row_{vi}", list_l + 10, row_bot + 5, 11,
                      COLOR_WHITE)
            rt.text = name
            rt.draw()
            state.browser_btn_rects.append(
                (list_l, list_r, row_bot, row_top, f"pick:{name}")
            )

    # Scroll arrows (right edge of list).
    sx1 = list_r - 22
    sx2 = list_r
    # Only show arrows when there's something to scroll. Keeps the
    # visual quiet for short folders.
    if max_scroll > 0:
        for action, ay_b, ay_t, lbl in (
            ("br_scroll_up", list_t - 22, list_t, "^"),
            ("br_scroll_down", list_b, list_b + 22, "v"),
        ):
            arcade.draw_lrbt_rectangle_filled(
                sx1, sx2, ay_b, ay_t, (60, 50, 40, 230),
            )
            arcade.draw_lrbt_rectangle_outline(
                sx1, sx2, ay_b, ay_t, COLOR_UI_BORDER, 1,
            )
            tt = _txt(state, f"br_sb_{action}", (sx1 + sx2) / 2, ay_b + 4,
                      12, COLOR_WHITE, True, anchor_x="center")
            tt.text = lbl
            tt.draw()
            state.browser_btn_rects.append((sx1, sx2, ay_b, ay_t, action))

    # Cancel button.
    cancel_l = r - 110
    cancel_r = r - 14
    cancel_t = b + 38
    cancel_b = b + 14
    arcade.draw_lrbt_rectangle_filled(
        cancel_l, cancel_r, cancel_b, cancel_t, (60, 50, 40),
    )
    arcade.draw_lrbt_rectangle_outline(
        cancel_l, cancel_r, cancel_b, cancel_t, COLOR_UI_BORDER, 1,
    )
    ct = _txt(state, "br_cancel", (cancel_l + cancel_r) / 2,
              cancel_b + 6, 11, COLOR_WHITE, True, anchor_x="center")
    ct.text = "Cancel"
    ct.draw()
    state.browser_btn_rects.append(
        (cancel_l, cancel_r, cancel_b, cancel_t, "br_cancel")
    )


# ── Click handling ───────────────────────────────────────────────────

def handle_click(window: Any, state: RpgRequestEditorState,
                 x: int, y: int) -> None:
    """Process a left-click while the editor is open.

    Walks ``state.btn_rects`` (populated by ``draw``) and dispatches
    to the right action. Anything that *doesn't* hit a button is a
    click-outside-text-field, which we treat as "commit + unfocus".
    """
    # v0.32: when the file-browser overlay is open it eats every
    # click — the underlying form's btn_rects are stale this frame
    # but we don't want them firing through the modal scrim. Walk
    # the browser's own button list instead. A click outside any
    # browser button dismisses the overlay (same convention as the
    # splash load-map picker uses for the rest of the game).
    if state.browser_target is not None:
        hit = None
        for (l, r, b, t, action) in state.browser_btn_rects:
            if l <= x <= r and b <= y <= t:
                hit = action
                break
        if hit is None:
            state.browser_target = None
            return
        _dispatch_browser_action(state, hit)
        return

    # Commit any in-progress text edit before changing focus, unless
    # we're clicking the same field again.
    hit_action: str | None = None
    for (l, r, b, t, action) in state.btn_rects:
        if l <= x <= r and b <= y <= t:
            hit_action = action
            break

    # Determine new focus target (if hit_action is a focus:... action).
    new_focus: str | None = None
    if hit_action and hit_action.startswith("focus:"):
        new_focus = hit_action.split(":", 1)[1]

    # Commit current text buffer if the user is moving focus to a
    # different field (or clicking a non-text-field action).
    if state.text_field is not None and state.text_field != new_focus:
        _commit_text(state)

    if hit_action is None:
        # Click in empty space — unfocus.
        state.text_field = None
        state.text_buffer = ""
        return

    if hit_action.startswith("focus:"):
        field = hit_action.split(":", 1)[1]
        state.text_field = field
        state.text_buffer = _read_field(state, field)
        return

    # Non-text actions.
    _dispatch_action(window, state, hit_action)


def _dispatch_browser_action(state: RpgRequestEditorState,
                             action: str) -> None:
    """Handle a click inside the file-browser overlay.

    Actions:
      * ``br_cancel`` — close without picking.
      * ``br_scroll_up`` / ``br_scroll_down`` — scroll the list.
      * ``pick:<basename>`` — write the chosen basename into the
        underlying RPG-request field (``npc_portrait`` or
        ``background_scene``) and close the overlay.
    """
    if action == "br_cancel":
        state.browser_target = None
        return
    if action == "br_scroll_up":
        state.browser_scroll = max(0, state.browser_scroll - 1)
        return
    if action == "br_scroll_down":
        state.browser_scroll = state.browser_scroll + 1
        return
    if action.startswith("pick:"):
        name = action.split(":", 1)[1]
        target = state.browser_target
        if target and 0 <= state.selected_idx < len(state.requests):
            req = state.requests[state.selected_idx]
            req[target] = name
            state.dirty = True
        state.browser_target = None
        return


def _read_field(state: RpgRequestEditorState, field: str) -> str:
    """Return the current string value for the given focusable field
    so the text buffer can pre-populate with it when focus enters."""
    if state.selected_idx < 0 or state.selected_idx >= len(state.requests):
        return ""
    req = state.requests[state.selected_idx]
    if field in ("id", "npc_name", "npc_portrait",
                 "background_scene", "request_text"):
        return str(req.get(field, ""))
    if field == "trigger_flag":
        return str(req.get("trigger_flag") or "")
    if field == "trigger_event":
        return str(req.get("trigger_event") or "")
    if field.startswith("dec:"):
        _, idx_str, sub = field.split(":", 2)
        idx = int(idx_str)
        decs = req.get("decisions") or []
        if 0 <= idx < len(decs):
            v = decs[idx].get(sub)
            return str(v) if v is not None else ""
    return ""


def _commit_text(state: RpgRequestEditorState) -> None:
    """Write ``state.text_buffer`` back into the field named by
    ``state.text_field``."""
    field = state.text_field
    if field is None:
        return
    if state.selected_idx < 0 or state.selected_idx >= len(state.requests):
        state.text_field = None
        state.text_buffer = ""
        return
    req = state.requests[state.selected_idx]
    val = state.text_buffer
    if field in ("id", "npc_name", "npc_portrait",
                 "background_scene", "request_text",
                 "trigger_flag", "trigger_event"):
        # Bound the length so a runaway paste doesn't blow up the JSON.
        if field == "request_text":
            val = val[:TEXT_MAX]
        elif field in ("id",):
            val = val[:ID_MAX]
        else:
            val = val[:NPC_NAME_MAX]
        # Empty string for optional fields → store None so save_requests
        # can drop them.
        if field in ("trigger_event",) and not val.strip():
            req[field] = None
        else:
            req[field] = val
        state.dirty = True
    elif field.startswith("dec:"):
        _, idx_str, sub = field.split(":", 2)
        idx = int(idx_str)
        decs = req.get("decisions") or []
        if 0 <= idx < len(decs):
            if sub == "label":
                val = val[:LABEL_MAX]
            elif sub in ("fire_event",) and not val.strip():
                decs[idx][sub] = None
                state.dirty = True
                state.text_field = None
                state.text_buffer = ""
                return
            decs[idx][sub] = val
            state.dirty = True
    state.text_field = None
    state.text_buffer = ""


def _dispatch_action(window: Any, state: RpgRequestEditorState,
                     action: str) -> None:
    """Handle non-text clicks: scrolling, add/remove, +/- steppers,
    selection, footer Save/Cancel."""
    # Footer.
    if action == "cancel":
        close_editor(state)
        return
    if action == "save":
        save_and_close(window, state)
        return

    # v0.32: open the file-browser overlay for the named target field.
    # ``action`` is "browse:npc_portrait" or "browse:background_scene".
    # We unfocus any text field first so the buffer-vs-stored-value
    # disambiguation doesn't get awkward — the browser writes
    # directly into the request dict, not into the text buffer.
    if action.startswith("browse:"):
        target = action.split(":", 1)[1]
        if target in ("npc_portrait", "background_scene"):
            if state.text_field is not None:
                _commit_text(state)
            state.browser_target = target
            state.browser_scroll = 0
        return

    # List scroll.
    if action == "list_scroll_up":
        state.scroll = max(0, state.scroll - 1)
        return
    if action == "list_scroll_down":
        state.scroll = state.scroll + 1
        return
    if action == "dec_scroll_up":
        state.dec_scroll = max(0, state.dec_scroll - 1)
        return
    if action == "dec_scroll_down":
        state.dec_scroll = state.dec_scroll + 1
        return

    # List add/remove + select.
    if action == "add_request":
        existing = [str(r.get("id", "")) for r in state.requests]
        state.requests.append(make_blank_request(existing))
        state.selected_idx = len(state.requests) - 1
        state.dirty = True
        return
    if action == "remove_request":
        if 0 <= state.selected_idx < len(state.requests):
            del state.requests[state.selected_idx]
            state.selected_idx = min(state.selected_idx,
                                     len(state.requests) - 1)
            state.dirty = True
        return
    if action.startswith("select:"):
        idx = int(action.split(":", 1)[1])
        if 0 <= idx < len(state.requests):
            state.selected_idx = idx
            state.dec_scroll = 0
        return

    # Decisions add/remove.
    if action == "add_decision":
        if 0 <= state.selected_idx < len(state.requests):
            req = state.requests[state.selected_idx]
            req.setdefault("decisions", [])
            new_dec = make_blank_decision()
            # Pre-seed the new decision's effects with the request's
            # resources preview so the common case (every decision
            # has the same cost) is one click instead of N. Authors
            # who want different per-decision effects can drop the
            # synced effects later via the "effects" cell.
            new_dec["effects"] = dict(req.get("resources") or {})
            req["decisions"].append(new_dec)
            state.dirty = True
        return
    if action == "remove_decision":
        if 0 <= state.selected_idx < len(state.requests):
            req = state.requests[state.selected_idx]
            decs = req.get("decisions") or []
            if decs:
                # Remove the last decision (no per-row selection in
                # v0.31 — the focused decision concept is text-field
                # based, not row-based, and "remove the focused row"
                # gets weird if the user is mid-edit).
                decs.pop()
                state.dirty = True
        return

    # Resource +/- on the preview row.
    if action.startswith("res_inc:") or action.startswith("res_dec:"):
        if 0 <= state.selected_idx < len(state.requests):
            key = action.split(":", 1)[1]
            req = state.requests[state.selected_idx]
            res = req.setdefault("resources", {})
            cur = int(res.get(key, 0))
            step = 100 if key == "money" else 10
            if action.startswith("res_inc:"):
                res[key] = cur + step
            else:
                res[key] = cur - step
            # Drop zero entries so the JSON stays tight.
            if res.get(key) == 0:
                del res[key]
            state.dirty = True
        return

    # Decision +/- steppers.
    if action.startswith("dec_delay_inc:") or action.startswith("dec_delay_dec:"):
        idx = int(action.split(":", 1)[1])
        if 0 <= state.selected_idx < len(state.requests):
            decs = state.requests[state.selected_idx].get("decisions") or []
            if 0 <= idx < len(decs):
                cur = int(decs[idx].get("delay_ticks", 0))
                step = 100  # 100 ticks ≈ a short in-game beat
                if action.startswith("dec_delay_inc:"):
                    decs[idx]["delay_ticks"] = cur + step
                else:
                    decs[idx]["delay_ticks"] = max(0, cur - step)
                state.dirty = True
        return
    if action.startswith("dec_happy_inc:") or action.startswith("dec_happy_dec:"):
        idx = int(action.split(":", 1)[1])
        if 0 <= state.selected_idx < len(state.requests):
            decs = state.requests[state.selected_idx].get("decisions") or []
            if 0 <= idx < len(decs):
                cur = int(decs[idx].get("happy", 0))
                step = 1
                if action.startswith("dec_happy_inc:"):
                    decs[idx]["happy"] = cur + step
                else:
                    decs[idx]["happy"] = cur - step
                state.dirty = True
        return
    # v0.32: per-decision diplomacy ± stepper. Mirrors the happy
    # handler — step ±1, no floor (diplomacy can go negative in the
    # stored decision even though DiplomacyTracker.points clamps at 0
    # when the delta is applied at runtime; the editor stores the
    # author's intent verbatim).
    if action.startswith("dec_diplo_inc:") or action.startswith("dec_diplo_dec:"):
        idx = int(action.split(":", 1)[1])
        if 0 <= state.selected_idx < len(state.requests):
            decs = state.requests[state.selected_idx].get("decisions") or []
            if 0 <= idx < len(decs):
                cur = int(decs[idx].get("diplomacy", 0))
                step = 1
                if action.startswith("dec_diplo_inc:"):
                    decs[idx]["diplomacy"] = cur + step
                else:
                    decs[idx]["diplomacy"] = cur - step
                state.dirty = True
        return

    # Sync per-decision effects with the request's resources preview.
    if action.startswith("dec_sync_effects:"):
        idx = int(action.split(":", 1)[1])
        if 0 <= state.selected_idx < len(state.requests):
            req = state.requests[state.selected_idx]
            decs = req.get("decisions") or []
            if 0 <= idx < len(decs):
                decs[idx]["effects"] = dict(req.get("resources") or {})
                state.dirty = True
        return


def handle_key(window: Any, state: RpgRequestEditorState,
               symbol: int, modifiers: int) -> bool:
    """Buffer mutation for text fields. Returns True if the key was
    consumed (so the caller can stop processing it).

    Matches the trigger editor's text input semantics: Enter commits,
    Esc cancels, Backspace deletes one char, printable chars (via
    the window's ``_char_for_symbol`` helper) append. Non-text keys
    (arrows, tab) fall through so future versions can wire them up
    to row navigation without rewriting this.

    ``window`` is passed in so we can reuse its ``_char_for_symbol``
    helper — same keymap as the trigger editor uses, so the two
    editors feel identical to type into.
    """
    if state.text_field is None:
        # v0.32: Esc closes the file-browser overlay (if open) BEFORE
        # falling through to closing the editor. Matches the trigger
        # editor's nested-modal pattern — outer modal stays open.
        if symbol == arcade.key.ESCAPE and state.browser_target is not None:
            state.browser_target = None
            return True
        # Top-level: Esc closes the editor (matches trigger editor).
        if symbol == arcade.key.ESCAPE:
            close_editor(state)
            return True
        return False
    if symbol in (arcade.key.ENTER, arcade.key.RETURN, arcade.key.NUM_ENTER):
        _commit_text(state)
        return True
    if symbol == arcade.key.ESCAPE:
        # Cancel: discard buffer, unfocus, leave field unchanged.
        state.text_field = None
        state.text_buffer = ""
        return True
    if symbol == arcade.key.BACKSPACE:
        state.text_buffer = state.text_buffer[:-1]
        return True
    # Printable character → append.
    char_fn = getattr(window, "_char_for_symbol", None)
    if char_fn is None:
        return False
    ch = char_fn(symbol, modifiers)
    if ch is not None:
        # Hard cap so a stuck key doesn't grow the buffer without
        # bound; _commit_text re-truncates per field type, but the
        # per-frame draw stays cheap.
        if len(state.text_buffer) < 512:
            state.text_buffer += ch
        return True
    return False
