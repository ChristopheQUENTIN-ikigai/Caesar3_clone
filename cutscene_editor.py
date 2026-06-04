"""Cutscene cinematic editor — splash-menu modal for ``data/cutscenes.json``.

v0.32. Mirrors the RPG request editor's structure (left-column list
of cutscenes, right-pane form for the selected entry) but the per-
entry payload is a slide deck rather than a decision tree.

    +---------------------------------------------------------------+
    | CUT SCENE CINEMATIC EDITOR                                    |
    | edit data/cutscenes.json — scripted slide decks               |
    |                                                               |
    | +-----------+  +-------------------------------------------+  |
    | | cutscene  |  | id:           [_____]                     |  |
    | | list      |  | title:        [_____]                     |  |
    | | (scrolly) |  | trigger flag: [_____]                     |  |
    | |           |  |                                           |  |
    | | * intro   |  | SLIDES:                                   |  |
    | | * victory |  |   1) image[__] [Browse…]                  |  |
    | | * defeat  |  |      caption[___________]                 |  |
    | |           |  |      body[___________________________]    |  |
    | |           |  |                                           |  |
    | |           |  |   2) image[__] [Browse…] …                |  |
    | |           |  |   ...                                     |  |
    | |           |  | [+ Add slide] [- Remove slide]            |  |
    | +-----------+  +-------------------------------------------+  |
    |                                                               |
    | [+ Add] [- Remove]              [Cancel]  [Save]              |
    +---------------------------------------------------------------+

Standalone module for the same reason the RPG request editor is —
keeps ``game_window.py`` from growing another 1k lines. We re-use the
scene-folder file browser from ``rpg_request_editor`` so the author
sees the same dialog when picking a slide image.
"""
from __future__ import annotations

import logging
from typing import Any

import arcade

from constants import (
    COLOR_GOLD, COLOR_GRAY, COLOR_RED, COLOR_UI_BORDER, COLOR_WHITE,
)
from cutscenes import (
    load_cutscenes, make_blank_cutscene, make_blank_slide, save_cutscenes,
)
# Re-use the RPG editor's scene-folder + browser helpers. v0.32 ships
# one file browser shared across both editors so authors don't have
# to learn two slightly different UIs.
from rpg_request_editor import (
    SCENE_DIR, _list_browser_files,
)

log = logging.getLogger(__name__)


# ── Layout constants ─────────────────────────────────────────────────
PANEL_W = 1100
PANEL_H = 740
LIST_W = 220
ROW_H = 22
# Slides are visually richer than decisions (image + caption + body)
# so each row is taller. Three sub-lines × ~22px + padding = 78px.
SLIDE_ROW_H = 78
TITLE_MAX = 80
ID_MAX = 48
CAPTION_MAX = 64
BODY_MAX = 320


class CutsceneEditorState:
    """Holds editor state for the cutscene editor. One instance per
    game window, created lazily the first time the editor opens.

    Fields mirror RpgRequestEditorState's shape so the patterns the
    splash menu uses for one work for the other.
    """

    def __init__(self) -> None:
        self.open: bool = False
        self.cutscenes: list[dict[str, Any]] = []
        self.selected_idx: int = -1
        self.scroll: int = 0
        self.slide_scroll: int = 0
        # Focused text field. Values:
        #   None — no field focused
        #   "id" / "title" / "trigger_flag" — top-level cutscene fields
        #   "slide:<i>:caption" / "slide:<i>:body" / "slide:<i>:image"
        #     — per-slide text fields
        self.text_field: str | None = None
        self.text_buffer: str = ""
        self.dirty: bool = False
        self.btn_rects: list[tuple[float, float, float, float, str]] = []
        self._txt_pool: dict[str, arcade.Text] = {}
        # File browser — picks scene images for slides. ``browser_target``
        # is the field-name string the picked image goes into
        # (e.g. "slide:2:image"). None means the browser is closed.
        self.browser_target: str | None = None
        self.browser_scroll: int = 0
        self.browser_btn_rects: list[
            tuple[float, float, float, float, str]
        ] = []


# ── Open / close ──────────────────────────────────────────────────────

def open_editor(window: Any, state: CutsceneEditorState) -> None:
    """Load ``data/cutscenes.json`` into the editor's working list and
    mark the editor as open. Mirrors ``rpg_request_editor.open_editor``."""
    try:
        cs = load_cutscenes()
    except Exception as e:  # noqa: BLE001
        log.exception("Cutscene editor: load failed")
        if hasattr(window, "_notify"):
            window._notify(f"Load failed: {e}", COLOR_RED)
        return
    state.cutscenes = cs
    state.selected_idx = 0 if cs else -1
    state.scroll = 0
    state.slide_scroll = 0
    state.text_field = None
    state.text_buffer = ""
    state.dirty = False
    state.browser_target = None
    state.browser_scroll = 0
    state.open = True
    log.info("Cutscene editor: opened with %d cutscenes", len(cs))


def close_editor(state: CutsceneEditorState) -> None:
    """Close without saving (discard changes — same pattern as the
    trigger editor)."""
    state.open = False
    state.text_field = None
    state.text_buffer = ""
    state.browser_target = None
    log.info("Cutscene editor: closed (dirty=%s)", state.dirty)


def save_and_close(window: Any, state: CutsceneEditorState) -> None:
    """Write the working list to disk and close the editor."""
    try:
        save_cutscenes(state.cutscenes)
    except OSError as e:
        log.exception("Cutscene editor: save failed")
        if hasattr(window, "_notify"):
            window._notify(f"Save failed: {e}", COLOR_RED)
        return
    state.dirty = False
    # Invalidate the window's cached cutscene library so the runtime
    # picks up the fresh save without a process restart.
    if hasattr(window, "invalidate_cutscenes_cache"):
        window.invalidate_cutscenes_cache()
    if hasattr(window, "_notify"):
        window._notify(
            f"Saved {len(state.cutscenes)} cutscenes", COLOR_GOLD,
        )
    state.open = False


# ── Drawing helpers ──────────────────────────────────────────────────

def _txt(state: CutsceneEditorState, key: str, x: float, y: float,
         size: int = 12, color: tuple = COLOR_WHITE, bold: bool = False,
         anchor_x: str = "left") -> arcade.Text:
    """Lazy cache for arcade.Text instances."""
    t = state._txt_pool.get(key)
    if t is None:
        t = arcade.Text("", x, y, color, size,
                        bold=bold, anchor_x=anchor_x)
        state._txt_pool[key] = t
    t.x = x
    t.y = y
    t.color = color
    return t


def _draw_text_field(state: CutsceneEditorState, key: str,
                     l: float, r: float, b: float, t: float,
                     value: str, action: str) -> None:
    """Single-line text input box."""
    focused = (state.text_field == action)
    fill = (40, 32, 24) if not focused else (60, 50, 36)
    border = COLOR_GOLD if focused else COLOR_UI_BORDER
    arcade.draw_lrbt_rectangle_filled(l, r, b, t, fill)
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, border, 1)
    display = state.text_buffer if focused else value
    max_chars = max(4, int((r - l) / 7))
    if len(display) > max_chars:
        display = "…" + display[-(max_chars - 1):]
    if focused:
        display = display + "|"
    txt = _txt(state, f"txt_{action}", l + 6, b + 4, 11, COLOR_WHITE)
    txt.text = display
    txt.draw()
    state.btn_rects.append((l, r, b, t, f"focus:{action}"))


def _draw_browse_button(state: CutsceneEditorState,
                        l: float, r: float, b: float, t: float,
                        action: str) -> None:
    """Tiny gold-bordered Browse button. Same look as the one in the
    RPG editor."""
    arcade.draw_lrbt_rectangle_filled(l, r, b, t, (70, 60, 40))
    arcade.draw_lrbt_rectangle_outline(l, r, b, t, COLOR_GOLD, 1)
    txt = _txt(state, f"btn_{action}", (l + r) / 2, b + 3, 9,
               COLOR_GOLD, True, anchor_x="center")
    txt.text = "Browse…"
    txt.draw()
    state.btn_rects.append((l, r, b, t, action))


# ── Main draw entry point ────────────────────────────────────────────

def draw(window: Any, state: CutsceneEditorState) -> None:
    """Render the cutscene editor modal."""
    state.btn_rects = []

    # Dim background.
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
    title.text = "CUT SCENE CINEMATIC EDITOR"
    title.draw()
    hint = _txt(state, "hint", cx, t - 44, 10, COLOR_GRAY,
                anchor_x="center")
    hint.text = (
        "Edit data/cutscenes.json — scripted slide decks shown between"
        " gameplay beats. Triggered by a flag flipping at runtime."
    )
    hint.draw()

    # Left: cutscene list.
    list_l = l + 14
    list_r = list_l + LIST_W
    list_t = t - 64
    list_b = b + 56
    _draw_cutscene_list(state, list_l, list_r, list_b, list_t)

    # Right: form (or empty placeholder).
    form_l = list_r + 30
    form_r = r - 14
    form_t = list_t
    form_b = list_b
    if 0 <= state.selected_idx < len(state.cutscenes):
        _draw_cutscene_form(state, form_l, form_r, form_b, form_t)
    else:
        msg = _txt(state, "empty", (form_l + form_r) / 2,
                   (form_b + form_t) / 2, 13, COLOR_GRAY, anchor_x="center")
        msg.text = "No cutscenes yet. Click \"+ Add\" to create one."
        msg.draw()

    # Footer.
    _draw_footer(state, l, r, b)

    # File browser overlay.
    if state.browser_target is not None:
        _draw_file_browser(state, window)


def _draw_cutscene_list(state: CutsceneEditorState,
                        list_l: float, list_r: float,
                        list_b: float, list_t: float) -> None:
    """Left column."""
    arcade.draw_lrbt_rectangle_filled(
        list_l, list_r, list_b, list_t, (22, 18, 14, 230),
    )
    arcade.draw_lrbt_rectangle_outline(
        list_l, list_r, list_b, list_t, COLOR_UI_BORDER, 1,
    )
    rows_visible = max(1, int((list_t - list_b) / ROW_H))
    items = state.cutscenes
    max_scroll = max(0, len(items) - rows_visible)
    state.scroll = max(0, min(state.scroll, max_scroll))

    # Scroll arrows.
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
        if idx >= len(items):
            break
        cs = items[idx]
        row_top = list_t - vi * ROW_H
        row_bot = row_top - ROW_H
        selected = (idx == state.selected_idx)
        if selected:
            arcade.draw_lrbt_rectangle_filled(
                list_l, list_r, row_bot, row_top, (90, 70, 50, 220),
            )
        rt = _txt(state, f"row_{vi}", list_l + 8, row_bot + 5, 11,
                  COLOR_GOLD if selected else COLOR_WHITE)
        # Display title if present, else id.
        name = str(cs.get("title") or cs.get("id") or "(unnamed)")
        rt.text = name if len(name) <= 22 else name[:21] + "…"
        rt.draw()
        state.btn_rects.append(
            (list_l, list_r, row_bot, row_top, f"select:{idx}")
        )

    # Footer: + Add / - Remove.
    add_l = list_l
    add_r = list_l + LIST_W // 2 - 4
    rem_l = add_r + 8
    rem_r = list_r
    ay_t = list_b - 4
    ay_b = ay_t - 24
    for x1, x2, action, lbl, fill in (
        (add_l, add_r, "add_cutscene", "+ Add", (50, 80, 50)),
        (rem_l, rem_r, "remove_cutscene", "− Remove", (80, 50, 40)),
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


def _draw_cutscene_form(state: CutsceneEditorState,
                        l: float, r: float, b: float, t: float) -> None:
    """Right pane."""
    cs = state.cutscenes[state.selected_idx]

    # Row 1: id + title.
    y = t - 24
    lbl = _txt(state, "lbl_id", l, y, 11, COLOR_GOLD, True)
    lbl.text = "ID:"
    lbl.draw()
    _draw_text_field(state, "id", l + 30, l + 220, y - 18, y - 2,
                     str(cs.get("id", "")), "id")
    lbl2 = _txt(state, "lbl_title", l + 240, y, 11, COLOR_GOLD, True)
    lbl2.text = "Title:"
    lbl2.draw()
    _draw_text_field(state, "ttl", l + 290, l + 600, y - 18, y - 2,
                     str(cs.get("title", "")), "title")

    # Row 2: trigger flag.
    y -= 32
    lbl3 = _txt(state, "lbl_flag", l, y, 11, COLOR_GOLD, True)
    lbl3.text = "Trigger flag:"
    lbl3.draw()
    _draw_text_field(state, "tflag", l + 95, l + 360, y - 18, y - 2,
                     str(cs.get("trigger_flag") or ""), "trigger_flag")
    hint = _txt(state, "flag_hint", l + 380, y - 14, 9, COLOR_GRAY)
    hint.text = "(cutscene fires when this flag flips at runtime)"
    hint.draw()

    # Slides header + scrollable box.
    y -= 36
    hdr = _txt(state, "slides_hdr", l, y, 12, COLOR_GOLD, True)
    hdr.text = "Slides (one screen each, player advances with Next/OK):"
    hdr.draw()

    box_l = l
    box_r = r - 14
    box_t = y - 12
    box_b = b + 38
    _draw_slides_box(state, cs, box_l, box_r, box_b, box_t)


def _draw_slides_box(state: CutsceneEditorState, cs: dict[str, Any],
                     box_l: float, box_r: float,
                     box_b: float, box_t: float) -> None:
    """Scrollable list of slides for the selected cutscene."""
    arcade.draw_lrbt_rectangle_filled(
        box_l, box_r, box_b, box_t, (22, 18, 14, 230),
    )
    arcade.draw_lrbt_rectangle_outline(
        box_l, box_r, box_b, box_t, COLOR_UI_BORDER, 1,
    )
    slides = cs.get("slides") or []
    rows_visible = max(1, int((box_t - box_b) / SLIDE_ROW_H))
    max_scroll = max(0, len(slides) - rows_visible)
    state.slide_scroll = max(0, min(state.slide_scroll, max_scroll))

    # Scroll arrows.
    sx1 = box_r + 2
    sx2 = sx1 + 18
    for action, ay_b, ay_t, lbl in (
        ("slide_scroll_up", box_t - 20, box_t, "^"),
        ("slide_scroll_down", box_b, box_b + 20, "v"),
    ):
        arcade.draw_lrbt_rectangle_filled(sx1, sx2, ay_b, ay_t, (60, 50, 40))
        arcade.draw_lrbt_rectangle_outline(
            sx1, sx2, ay_b, ay_t, COLOR_UI_BORDER, 1,
        )
        tt = _txt(state, f"ssb_{action}", (sx1 + sx2) / 2, ay_b + 3,
                  11, COLOR_WHITE, True, anchor_x="center")
        tt.text = lbl
        tt.draw()
        state.btn_rects.append((sx1, sx2, ay_b, ay_t, action))

    for vi in range(rows_visible):
        idx = state.slide_scroll + vi
        if idx >= len(slides):
            break
        slide = slides[idx]
        row_top = box_t - vi * SLIDE_ROW_H
        row_bot = row_top - SLIDE_ROW_H

        # Separator between rows.
        if vi > 0:
            arcade.draw_lrbt_rectangle_filled(
                box_l + 4, box_r - 4, row_top - 1, row_top,
                (60, 50, 40, 180),
            )

        # Row number — left margin, vertically centred.
        num = _txt(state, f"sn_{vi}", box_l + 6, row_top - 38, 11,
                   COLOR_GOLD, True)
        num.text = f"{idx + 1})"
        num.draw()

        # Line 1: image field + Browse button.
        line1_b = row_top - 22
        line1_t = row_top - 6
        il = _txt(state, f"sil_{vi}", box_l + 28, line1_b + 4, 10,
                  COLOR_GRAY)
        il.text = "image:"
        il.draw()
        _draw_text_field(state, f"si_{vi}",
                         box_l + 68, box_l + 230, line1_b, line1_t,
                         str(slide.get("image", "")),
                         f"slide:{idx}:image")
        _draw_browse_button(state, box_l + 236, box_l + 296,
                            line1_b, line1_t,
                            f"slide_browse:{idx}")

        # Caption on the same line, right of the image controls.
        cl = _txt(state, f"scl_{vi}", box_l + 310, line1_b + 4, 10,
                  COLOR_GRAY)
        cl.text = "caption:"
        cl.draw()
        _draw_text_field(state, f"sc_{vi}",
                         box_l + 360, box_r - 6, line1_b, line1_t,
                         str(slide.get("caption", "")),
                         f"slide:{idx}:caption")

        # Line 2 & 3: body (taller — two lines worth of vertical space).
        line2_b = row_top - 70
        line2_t = row_top - 28
        bl = _txt(state, f"sbl_{vi}", box_l + 28, line2_t - 12, 10,
                  COLOR_GRAY)
        bl.text = "body:"
        bl.draw()
        _draw_text_field(state, f"sb_{vi}",
                         box_l + 68, box_r - 6, line2_b, line2_t,
                         str(slide.get("body", "")),
                         f"slide:{idx}:body")

    # Add/Remove slide buttons.
    foot_t = box_b - 4
    foot_b = foot_t - 24
    add_l = box_l
    add_r = box_l + 130
    rem_l = add_r + 8
    rem_r = rem_l + 150
    for x1, x2, action, lbl, fill in (
        (add_l, add_r, "add_slide", "+ Add slide", (50, 80, 50)),
        (rem_l, rem_r, "remove_slide", "− Remove slide", (80, 50, 40)),
    ):
        arcade.draw_lrbt_rectangle_filled(x1, x2, foot_b, foot_t, fill)
        arcade.draw_lrbt_rectangle_outline(
            x1, x2, foot_b, foot_t, COLOR_UI_BORDER, 1,
        )
        tt = _txt(state, f"slide_foot_{action}", (x1 + x2) / 2,
                  foot_b + 6, 11, COLOR_WHITE, True, anchor_x="center")
        tt.text = lbl
        tt.draw()
        state.btn_rects.append((x1, x2, foot_b, foot_t, action))


def _draw_footer(state: CutsceneEditorState,
                 l: float, r: float, b: float) -> None:
    """Bottom-of-modal Cancel/Save."""
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
    if state.dirty:
        d = _txt(state, "dirty", l + 14, b + 18, 10, COLOR_GOLD)
        d.text = "● unsaved changes"
        d.draw()


# ── File browser overlay (re-uses scene/ folder) ──────────────────────

_BROWSER_ROW_H = 22
_BROWSER_PANEL_W = 420
_BROWSER_PANEL_H = 460


def _draw_file_browser(state: CutsceneEditorState, window: Any) -> None:
    """Modal scene-image picker for slide.image fields."""
    state.browser_btn_rects = []
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

    title = _txt(state, "br_title", cx, t - 26, 13, COLOR_GOLD, True,
                 anchor_x="center")
    title.text = "BROWSE SCENES"
    title.draw()

    folder_str = str(SCENE_DIR)
    if len(folder_str) > 56:
        folder_str = "…" + folder_str[-55:]
    pth = _txt(state, "br_path", cx, t - 44, 9, COLOR_GRAY,
               anchor_x="center")
    pth.text = folder_str
    pth.draw()

    files = _list_browser_files(SCENE_DIR)
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
        msg.text = "(no scenes in ./assets/textures/scene/)"
        msg.draw()
    else:
        for vi in range(rows_visible):
            idx = state.browser_scroll + vi
            if idx >= len(files):
                break
            name = files[idx]
            row_top = list_t - vi * _BROWSER_ROW_H
            row_bot = row_top - _BROWSER_ROW_H
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

    if max_scroll > 0:
        sx1 = list_r - 22
        sx2 = list_r
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

    # Cancel.
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

def handle_click(window: Any, state: CutsceneEditorState,
                 x: int, y: int) -> None:
    """Process a left-click while the editor is open."""
    # Browser overlay eats clicks.
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

    hit_action: str | None = None
    for (l, r, b, t, action) in state.btn_rects:
        if l <= x <= r and b <= y <= t:
            hit_action = action
            break

    new_focus: str | None = None
    if hit_action and hit_action.startswith("focus:"):
        new_focus = hit_action.split(":", 1)[1]

    if state.text_field is not None and state.text_field != new_focus:
        _commit_text(state)

    if hit_action is None:
        state.text_field = None
        state.text_buffer = ""
        return

    if hit_action.startswith("focus:"):
        field = hit_action.split(":", 1)[1]
        state.text_field = field
        state.text_buffer = _read_field(state, field)
        return

    _dispatch_action(window, state, hit_action)


def _dispatch_browser_action(state: CutsceneEditorState,
                             action: str) -> None:
    """Handle a click inside the scene-image browser."""
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
        target = state.browser_target  # e.g. "slide:2:image"
        if target and target.startswith("slide:") and \
                0 <= state.selected_idx < len(state.cutscenes):
            _, idx_str, sub = target.split(":", 2)
            idx = int(idx_str)
            slides = state.cutscenes[state.selected_idx].get("slides") or []
            if 0 <= idx < len(slides) and sub == "image":
                slides[idx]["image"] = name
                state.dirty = True
        state.browser_target = None
        return


def _read_field(state: CutsceneEditorState, field: str) -> str:
    if state.selected_idx < 0 or state.selected_idx >= len(state.cutscenes):
        return ""
    cs = state.cutscenes[state.selected_idx]
    if field in ("id", "title", "trigger_flag"):
        return str(cs.get(field, ""))
    if field.startswith("slide:"):
        _, idx_str, sub = field.split(":", 2)
        idx = int(idx_str)
        slides = cs.get("slides") or []
        if 0 <= idx < len(slides):
            return str(slides[idx].get(sub, ""))
    return ""


def _commit_text(state: CutsceneEditorState) -> None:
    field = state.text_field
    if field is None:
        return
    if state.selected_idx < 0 or state.selected_idx >= len(state.cutscenes):
        state.text_field = None
        state.text_buffer = ""
        return
    cs = state.cutscenes[state.selected_idx]
    val = state.text_buffer
    if field in ("id", "title", "trigger_flag"):
        if field == "id":
            val = val[:ID_MAX]
        elif field == "title":
            val = val[:TITLE_MAX]
        else:
            val = val[:80]
        cs[field] = val
        state.dirty = True
    elif field.startswith("slide:"):
        _, idx_str, sub = field.split(":", 2)
        idx = int(idx_str)
        slides = cs.get("slides") or []
        if 0 <= idx < len(slides):
            if sub == "caption":
                val = val[:CAPTION_MAX]
            elif sub == "body":
                val = val[:BODY_MAX]
            slides[idx][sub] = val
            state.dirty = True
    state.text_field = None
    state.text_buffer = ""


def _dispatch_action(window: Any, state: CutsceneEditorState,
                     action: str) -> None:
    """Handle non-text clicks."""
    if action == "cancel":
        close_editor(state)
        return
    if action == "save":
        save_and_close(window, state)
        return
    if action == "list_scroll_up":
        state.scroll = max(0, state.scroll - 1)
        return
    if action == "list_scroll_down":
        state.scroll = state.scroll + 1
        return
    if action == "slide_scroll_up":
        state.slide_scroll = max(0, state.slide_scroll - 1)
        return
    if action == "slide_scroll_down":
        state.slide_scroll = state.slide_scroll + 1
        return
    if action == "add_cutscene":
        existing = [str(c.get("id", "")) for c in state.cutscenes]
        state.cutscenes.append(make_blank_cutscene(existing))
        state.selected_idx = len(state.cutscenes) - 1
        state.dirty = True
        return
    if action == "remove_cutscene":
        if 0 <= state.selected_idx < len(state.cutscenes):
            del state.cutscenes[state.selected_idx]
            state.selected_idx = min(state.selected_idx,
                                     len(state.cutscenes) - 1)
            state.dirty = True
        return
    if action.startswith("select:"):
        idx = int(action.split(":", 1)[1])
        if 0 <= idx < len(state.cutscenes):
            state.selected_idx = idx
            state.slide_scroll = 0
        return
    if action == "add_slide":
        if 0 <= state.selected_idx < len(state.cutscenes):
            cs = state.cutscenes[state.selected_idx]
            cs.setdefault("slides", []).append(make_blank_slide())
            state.dirty = True
        return
    if action == "remove_slide":
        if 0 <= state.selected_idx < len(state.cutscenes):
            slides = state.cutscenes[state.selected_idx].get("slides") or []
            if slides:
                slides.pop()
                state.dirty = True
        return
    if action.startswith("slide_browse:"):
        idx = int(action.split(":", 1)[1])
        if 0 <= state.selected_idx < len(state.cutscenes):
            if state.text_field is not None:
                _commit_text(state)
            state.browser_target = f"slide:{idx}:image"
            state.browser_scroll = 0
        return


def handle_key(window: Any, state: CutsceneEditorState,
               symbol: int, modifiers: int) -> bool:
    """Buffer mutation for text fields. Same semantics as the RPG
    request editor."""
    if state.text_field is None:
        if symbol == arcade.key.ESCAPE and state.browser_target is not None:
            state.browser_target = None
            return True
        if symbol == arcade.key.ESCAPE:
            close_editor(state)
            return True
        return False
    if symbol in (arcade.key.ENTER, arcade.key.RETURN, arcade.key.NUM_ENTER):
        _commit_text(state)
        return True
    if symbol == arcade.key.ESCAPE:
        state.text_field = None
        state.text_buffer = ""
        return True
    if symbol == arcade.key.BACKSPACE:
        state.text_buffer = state.text_buffer[:-1]
        return True
    char_fn = getattr(window, "_char_for_symbol", None)
    if char_fn is None:
        return False
    ch = char_fn(symbol, modifiers)
    if ch is not None:
        if len(state.text_buffer) < 512:
            state.text_buffer += ch
        return True
    return False
