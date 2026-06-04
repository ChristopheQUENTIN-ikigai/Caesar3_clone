"""Cutscene player — runtime presenter for ``data/cutscenes.json`` entries.

v0.32. Renders one cutscene slide at a time on top of a darkened
world, with a "Next" button that advances through the deck. The last
slide swaps "Next" to "OK"; clicking it closes the cutscene and
returns the player to gameplay.

This module is intentionally tiny — the editor + schema (in
``cutscenes.py``) does the heavy lifting. The player only needs:

  * State (``CutscenePlayerState``) — current cutscene + slide index.
  * ``start(state, cutscene_dict)`` — open a cutscene.
  * ``draw(state, window)`` — render the current slide.
  * ``advance(state)`` — Next/OK button action.
  * ``handle_click(state, window, x, y)`` — hit-test the Next/OK button.

Trigger flow (wired by ``game_window`` in v0.32):

  1. A flag flips at runtime — e.g. the player honours Caesar's
     tribute, setting ``caesar_tribute_paid``.
  2. ``game_window.fire_cutscene_for_flag`` looks up
     ``cutscenes.find_cutscene_for_flag(library, flag)``.
  3. If a match is found, ``start(state, cs)`` is called and the game
     pauses while the cutscene plays.
  4. When the player clicks OK on the last slide, ``advance`` sets
     ``state.active = False`` and the game resumes.

The runtime player imports the texture registry lazily so the
test suite can exercise advance / state transitions without touching
arcade.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class CutscenePlayerState:
    """Lightweight runtime state. Lives on the game window as
    ``window.cutscene_player_state`` and is created lazily the first
    time a cutscene fires.

    Fields
    ------
    active : bool
        Whether a cutscene is currently playing. Drives the
        ``game_window.on_draw`` / ``on_mouse_press`` dispatch.
    cutscene : dict | None
        The currently-playing cutscene (a dict from
        ``cutscenes.load_cutscenes()``), or ``None`` when inactive.
    slide_idx : int
        Index into ``cutscene["slides"]`` of the currently-visible
        slide. Always 0 right after ``start``; advances by 1 per
        Next click; ``OK`` on the last slide closes.
    btn_rect : tuple | None
        Hit-test rectangle for the Next/OK button, rebuilt each
        frame in ``draw`` and consumed on the next click. Tuple
        shape ``(left, right, bottom, top)`` or ``None`` if the
        cutscene has no slides (defensive).
    """

    def __init__(self) -> None:
        self.active: bool = False
        self.cutscene: dict[str, Any] | None = None
        self.slide_idx: int = 0
        self.btn_rect: tuple[float, float, float, float] | None = None
        # Lazy text-pool, same trick as the editors.
        self._txt_pool: dict[str, Any] = {}


def _normalize_cutscene(cutscene: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``cutscene`` whose slides use the player's slide
    schema (``image`` / ``caption`` / ``body``), accepting the
    scenario / editor authoring schema (``scene`` / ``portrait`` /
    ``text``) as aliases.

    Why this exists: the runtime player was written against the
    ``data/cutscenes.json`` slide shape (``image`` / ``caption`` /
    ``body``), but scenario-authored cutscenes — e.g. every mare_nostrum
    cinematic in its ``scenario_libraries`` block — use the editor shape
    (``scene`` / ``portrait`` / ``text``). Without this alias a scenario
    intro would render the "(no image)" placeholder and an empty body
    even though the slide is fully authored. The mapping is additive: an
    explicit ``image`` / ``caption`` / ``body`` always wins, so existing
    on-disk cutscenes are untouched.

    The copy is shallow-per-slide (we build new slide dicts) so the
    shared library dict the lookup returned is never mutated.
    """
    out = dict(cutscene)
    norm_slides = []
    for slide in (cutscene.get("slides") or []):
        if not isinstance(slide, dict):
            continue
        s = dict(slide)
        # scene → image (the scene/background art basename).
        if not s.get("image") and s.get("scene"):
            s["image"] = s["scene"]
        # text → body (the narration paragraph).
        if not s.get("body") and s.get("text"):
            s["body"] = s["text"]
        # portrait is preserved as-is for the portrait draw below; no
        # alias needed (the authoring and runtime keys already agree).
        norm_slides.append(s)
    out["slides"] = norm_slides
    return out


def start(state: CutscenePlayerState, cutscene: dict[str, Any]) -> None:
    """Begin playing a cutscene. A cutscene with no slides is
    rejected (logged and ignored) — we don't open the modal just to
    immediately close it.
    """
    cutscene = _normalize_cutscene(cutscene)
    slides = cutscene.get("slides") or []
    if not slides:
        log.info("cutscene_player: refusing empty cutscene %r",
                 cutscene.get("id"))
        return
    state.cutscene = cutscene
    state.slide_idx = 0
    state.active = True
    log.info("cutscene_player: started %r (%d slides)",
             cutscene.get("id"), len(slides))


def close(state: CutscenePlayerState) -> None:
    """Force-close the cutscene (Esc handler / scene teardown)."""
    state.active = False
    state.cutscene = None
    state.slide_idx = 0


def advance(state: CutscenePlayerState) -> bool:
    """Move to the next slide. Returns ``True`` if the cutscene is
    still playing afterwards, ``False`` if the OK click just closed
    it (so the caller can resume gameplay).

    Pre-condition: ``state.active`` is True and ``state.cutscene`` is
    set. Defensive against being called on a closed cutscene.
    """
    if not state.active or state.cutscene is None:
        return False
    slides = state.cutscene.get("slides") or []
    if state.slide_idx >= len(slides) - 1:
        # Last slide — OK closes.
        close(state)
        return False
    state.slide_idx += 1
    return True


def current_slide(state: CutscenePlayerState) -> dict[str, Any] | None:
    """Convenience accessor for the currently-visible slide dict."""
    if not state.active or state.cutscene is None:
        return None
    slides = state.cutscene.get("slides") or []
    if 0 <= state.slide_idx < len(slides):
        return slides[state.slide_idx]
    return None


def is_last_slide(state: CutscenePlayerState) -> bool:
    """True if the currently-visible slide is the last one (i.e. the
    Next button shows "OK")."""
    if not state.active or state.cutscene is None:
        return False
    slides = state.cutscene.get("slides") or []
    return state.slide_idx >= len(slides) - 1


# ── Draw + click handling (arcade-using; imported lazily) ────────────

def draw(state: CutscenePlayerState, window: Any) -> None:
    """Render the current slide. Imports arcade lazily so headless
    tests can drive ``advance`` / ``start`` / ``close`` without
    pulling in the graphics stack."""
    import arcade
    from constants import (
        COLOR_GOLD, COLOR_GRAY, COLOR_UI_BORDER, COLOR_WHITE,
    )

    if not state.active or state.cutscene is None:
        return
    slide = current_slide(state)
    if slide is None:
        return

    # Full-screen background — darker than the editor modals because
    # this is meant to *replace* the world view for the player, not
    # float over it.
    arcade.draw_lrbt_rectangle_filled(
        0, window.width, 0, window.height, (10, 8, 6, 255),
    )

    # ── Scene image ─────────────────────────────────────────────
    # Centred letterbox at the top 60% of the screen. We try to load
    # the slide's image from assets/textures/scene/<name>.png|.jpg via
    # the window's texture registry; if that's not available, we draw
    # a gold-bordered placeholder rectangle so the player still sees
    # *something* and the slide layout doesn't collapse.
    img_b = window.height * 0.32
    img_t = window.height * 0.92
    img_h = img_t - img_b
    img_w = min(window.width - 80, img_h * 16 / 9)
    img_l = (window.width - img_w) / 2
    img_r = img_l + img_w

    texture = _try_load_scene_texture(window, slide.get("image", ""))
    if texture is not None:
        arcade.draw_texture_rect(
            texture,
            arcade.LBWH(img_l, img_b, img_w, img_h),
        )
        arcade.draw_lrbt_rectangle_outline(
            img_l, img_r, img_b, img_t, COLOR_GOLD, 2,
        )
    else:
        # Placeholder: dark inner panel + tiny "missing image" hint.
        arcade.draw_lrbt_rectangle_filled(
            img_l, img_r, img_b, img_t, (30, 25, 20),
        )
        arcade.draw_lrbt_rectangle_outline(
            img_l, img_r, img_b, img_t, COLOR_UI_BORDER, 2,
        )
        ph = _txt(state, "ph_text", (img_l + img_r) / 2,
                  (img_b + img_t) / 2 - 6, 14, COLOR_GRAY,
                  anchor_x="center")
        img_name = slide.get("image") or "(no image)"
        ph.text = f"[ scene: {img_name} ]"
        ph.draw()

    # ── Caption + body ─────────────────────────────────────────
    caption = str(slide.get("caption") or "")
    body = str(slide.get("body") or "")
    text_y = img_b - 20
    if caption:
        cap = _txt(state, "caption", window.width / 2, text_y, 18,
                   COLOR_GOLD, bold=True, anchor_x="center")
        cap.text = caption
        cap.draw()
        text_y -= 30

    # Body — naive word-wrap into ~80-char lines. Each line is its own
    # cached Text. Cap at six lines so a runaway body doesn't push the
    # Next button off-screen; the slide author can split into two
    # slides if they need more.
    if body:
        lines = _wrap_text(body, max_chars=80)[:6]
        for li, line in enumerate(lines):
            bt = _txt(state, f"body_{li}", window.width / 2,
                      text_y - li * 22, 12, COLOR_WHITE,
                      anchor_x="center")
            bt.text = line
            bt.draw()

    # ── Next / OK button ───────────────────────────────────────
    is_last = is_last_slide(state)
    btn_label = "OK" if is_last else "Next ▶"
    btn_w = 140
    btn_h = 38
    btn_r = window.width / 2 + btn_w / 2
    btn_l = window.width / 2 - btn_w / 2
    btn_b = 30
    btn_t = btn_b + btn_h
    # Highlight OK (closes the cutscene) more strongly than Next.
    fill = (90, 130, 70) if is_last else (70, 60, 40)
    arcade.draw_lrbt_rectangle_filled(btn_l, btn_r, btn_b, btn_t, fill)
    arcade.draw_lrbt_rectangle_outline(
        btn_l, btn_r, btn_b, btn_t, COLOR_GOLD, 2,
    )
    bt = _txt(state, "btn", (btn_l + btn_r) / 2, btn_b + 11, 14,
              COLOR_WHITE, bold=True, anchor_x="center")
    bt.text = btn_label
    bt.draw()
    state.btn_rect = (btn_l, btn_r, btn_b, btn_t)

    # Slide counter (small, bottom-right) so the player has a sense
    # of how long the cutscene is.
    slides = state.cutscene.get("slides") or []
    if slides:
        cnt = _txt(state, "counter", window.width - 24, 18, 10,
                   COLOR_GRAY, anchor_x="right")
        cnt.text = f"{state.slide_idx + 1} / {len(slides)}"
        cnt.draw()


def handle_click(state: CutscenePlayerState, window: Any,
                 x: int, y: int) -> bool:
    """Hit-test the Next/OK button. Returns True if the click was
    consumed (so ``game_window.on_mouse_press`` can stop processing
    it). Anywhere outside the button is ignored — the player must
    explicitly click Next/OK to advance."""
    if not state.active:
        return False
    rect = state.btn_rect
    if rect is None:
        return True  # cutscene's drawing — still eat the click
    l, r, b, t = rect
    if l <= x <= r and b <= y <= t:
        advance(state)
    # Always return True — while the cutscene is up it eats every
    # click, even mis-clicks outside the button. Otherwise the
    # player could place a building "through" the cutscene scrim.
    return True


# ── Helpers ──────────────────────────────────────────────────────────

def _txt(state: CutscenePlayerState, key: str, x: float, y: float,
         size: int = 12, color: tuple = (230, 230, 220),
         bold: bool = False, anchor_x: str = "left") -> Any:
    """Lazy text-pool, same trick as the editors."""
    import arcade
    t = state._txt_pool.get(key)
    if t is None:
        t = arcade.Text("", x, y, color, size,
                        bold=bold, anchor_x=anchor_x)
        state._txt_pool[key] = t
    t.x = x
    t.y = y
    t.color = color
    return t


def _try_load_scene_texture(window: Any, name: str) -> Any:
    """Try to load a scene texture for ``name`` (basename, no
    extension). Returns the arcade.Texture or None.

    Goes through the window's TextureRegistry if available (so the
    cache is shared with the rest of the game); falls back to a direct
    ``arcade.load_texture`` if not. Either way, missing files return
    None — the draw routine falls back to the placeholder rectangle.
    """
    if not name:
        return None
    # First try the window's texture registry. v0.32 doesn't add a
    # dedicated ``scene()`` accessor (the textures module loads from
    # the manifest and ``scene/`` isn't manifest-driven yet), so we
    # use ``ui()`` as a generic "any png" loader if it exists; else
    # direct load.
    try:
        import arcade
        from constants import ROOT_DIR
        from pathlib import Path
        scene_dir = Path(ROOT_DIR) / "assets" / "textures" / "scene"
        for ext in (".png", ".jpg", ".jpeg"):
            candidate = scene_dir / f"{name}{ext}"
            if candidate.is_file():
                # Cache in window's texture registry if it has a
                # generic dict slot we can borrow; otherwise just
                # re-load each frame (the cutscene is short).
                cache = getattr(window, "_cutscene_texture_cache", None)
                if cache is None:
                    cache = {}
                    setattr(window, "_cutscene_texture_cache", cache)
                if str(candidate) in cache:
                    return cache[str(candidate)]
                tex = arcade.load_texture(str(candidate))
                cache[str(candidate)] = tex
                return tex
    except Exception:  # noqa: BLE001
        log.exception("cutscene_player: failed to load scene %r", name)
        return None
    return None


def _wrap_text(text: str, max_chars: int = 80) -> list[str]:
    """Naive word-wrap. Splits ``text`` into lines no longer than
    ``max_chars`` characters, breaking at spaces where possible.
    Words longer than the limit aren't split — they just exceed
    (rare in body prose, and avoids ugly mid-word breaks)."""
    if not text:
        return []
    lines: list[str] = []
    words = text.split()
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur = cur + " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines
