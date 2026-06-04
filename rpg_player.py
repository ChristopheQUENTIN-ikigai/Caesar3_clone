"""RPG-request player — runtime presenter for ``data/rpg_requests.json``.

v0.38 — closes audit finding 7.1. The RPG-request *editor*, *loader*,
*trigger effect* (``fire_request``) and the queue
(``game_window._pending_rpg_requests``) all shipped across v0.31–v0.36,
but the **consumer** — the full-screen scene + portrait + decision-button
panel — was never built, so a queued request displayed nothing and a
decision's ``effects`` / ``delay_ticks`` / ``set_flag`` / ``fire_event``
never executed.

This module is the consumer. It is a deliberate sibling of
``cutscene_player.py`` — same shape (a lightweight state object plus
free functions), same lazy-arcade trick so the logic is headless-
testable, same hit-rect-rebuilt-each-frame click model. The difference
is that a cutscene is a linear deck the player clicks *through*, whereas
a request presents a set of *decisions* and the player picks exactly
one; picking applies that decision's outcome and closes the panel.

Trigger flow (the half that already existed, now completed):

  1. A trigger's ``fire_request`` effect calls
     ``game_window.fire_request_for_id(rid)``, which appends the request
     dict to ``window._pending_rpg_requests``.
  2. ``game_window`` (v0.38) drains that queue: if no request is active
     and the queue is non-empty, it pops one and calls ``start``.
  3. ``draw`` renders the scene + portrait + request text + one button
     per decision; ``handle_click`` hit-tests the buttons.
  4. Clicking a decision calls ``choose``, which returns the chosen
     decision dict. ``game_window.apply_rpg_decision`` then applies it:
     immediate ``effects`` go straight to the economy; a non-zero
     ``delay_ticks`` parks them on a delayed-effect queue processed in
     ``_game_tick``; ``set_flag`` / ``fire_event`` / ``happy`` /
     ``diplomacy`` fire through the existing systems.

Decision schema (from ``rpg_requests.DEFAULT_DECISION``)::

    {"label": str, "delay_ticks": int, "effects": {res: int},
     "set_flag": str, "fire_event": str|None, "happy": int,
     "diplomacy": int}

Like the cutscene player, drawing imports arcade lazily; ``start`` /
``choose`` / ``close`` / state inspection are pure and unit-tested in
``tests/test_rpg_player.py``.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class RpgPlayerState:
    """Runtime state for the active RPG request. Lives on the game
    window as ``window.rpg_player_state``, created lazily the first
    time a request fires.

    Fields
    ------
    active : bool
        Whether a request panel is currently up. Drives the
        ``game_window`` draw / input / pause dispatch.
    request : dict | None
        The currently-displayed request dict (from
        ``rpg_requests.load_requests``), or ``None`` when inactive.
    btn_rects : list[tuple]
        Hit-test rectangles for the decision buttons, rebuilt each
        frame in ``draw`` and consumed on the next click. Each entry
        is ``(left, right, bottom, top, decision_index)``.
    chosen_idx : int | None
        Index of the decision the player picked, or ``None`` until
        a choice is made. Set by ``choose``; read by callers that
        want to know what happened after the panel closes.
    """

    def __init__(self) -> None:
        self.active: bool = False
        self.request: dict[str, Any] | None = None
        self.btn_rects: list[tuple[float, float, float, float, int]] = []
        self.chosen_idx: int | None = None
        # Lazy text-pool, same trick as the cutscene player / editors.
        self._txt_pool: dict[str, Any] = {}


def start(state: RpgPlayerState, request: dict[str, Any]) -> bool:
    """Begin presenting a request. A request with no decisions is
    rejected (logged and ignored) — a panel the player can't dismiss
    by choosing would soft-lock the game. Returns True if the panel
    opened.
    """
    decisions = request.get("decisions") or []
    if not decisions:
        log.info("rpg_player: refusing request %r with no decisions",
                 request.get("id"))
        return False
    state.request = request
    state.active = True
    state.chosen_idx = None
    state.btn_rects = []
    log.info("rpg_player: started %r (%d decisions)",
             request.get("id"), len(decisions))
    return True


def close(state: RpgPlayerState) -> None:
    """Force-close the panel (decision picked, or Esc teardown)."""
    state.active = False
    state.request = None
    state.btn_rects = []
    # chosen_idx is intentionally NOT cleared here — a caller that
    # closes via `choose` wants to read which decision won. The next
    # `start` resets it.


def decisions(state: RpgPlayerState) -> list[dict[str, Any]]:
    """The active request's decision list, or [] when inactive."""
    if not state.active or state.request is None:
        return []
    return state.request.get("decisions") or []


def choose(state: RpgPlayerState, idx: int) -> dict[str, Any] | None:
    """Pick the decision at ``idx``. Returns the decision dict (so the
    caller can apply its effects) and closes the panel, or ``None`` if
    the index is out of range / the panel isn't active.

    Defensive against double-clicks: once a choice is made the panel
    is closed, so a second ``choose`` on the now-inactive state returns
    ``None`` rather than applying a decision twice.
    """
    decs = decisions(state)
    if not (0 <= idx < len(decs)):
        return None
    state.chosen_idx = idx
    chosen = decs[idx]
    close(state)
    log.info("rpg_player: chose decision %d (%r)",
             idx, chosen.get("label"))
    return chosen


# ── Draw + click handling (arcade-using; imported lazily) ────────────

def draw(state: RpgPlayerState, window: Any) -> None:
    """Render the active request panel. Imports arcade lazily so
    headless tests drive ``start`` / ``choose`` / ``close`` without the
    graphics stack. Layout mirrors the cutscene player: a darkened
    full-screen scrim, a scene image up top, the NPC portrait + name,
    the request text, then a vertical stack of decision buttons.
    """
    import arcade
    from constants import (
        COLOR_GOLD, COLOR_GRAY, COLOR_UI_BORDER, COLOR_WHITE,
    )

    if not state.active or state.request is None:
        return
    req = state.request
    state.btn_rects = []

    # Full-screen scrim — same intent as the cutscene player: replace
    # the world view while the player reads and decides.
    arcade.draw_lrbt_rectangle_filled(
        0, window.width, 0, window.height, (10, 8, 6, 255),
    )

    # ── Scene image (top band) ─────────────────────────────────
    img_b = window.height * 0.46
    img_t = window.height * 0.92
    img_h = img_t - img_b
    img_w = min(window.width - 80, img_h * 16 / 9)
    img_l = (window.width - img_w) / 2
    img_r = img_l + img_w
    scene = req.get("background_scene") or ""
    texture = _try_load_scene_texture(window, scene)
    if texture is not None:
        arcade.draw_texture_rect(texture, arcade.LBWH(img_l, img_b, img_w, img_h))
        arcade.draw_lrbt_rectangle_outline(img_l, img_r, img_b, img_t, COLOR_GOLD, 2)
    else:
        arcade.draw_lrbt_rectangle_filled(img_l, img_r, img_b, img_t, (30, 25, 20))
        arcade.draw_lrbt_rectangle_outline(img_l, img_r, img_b, img_t, COLOR_UI_BORDER, 2)
        ph = _txt(state, "ph", (img_l + img_r) / 2, (img_b + img_t) / 2 - 6,
                  14, COLOR_GRAY, anchor_x="center")
        ph.text = f"[ scene: {scene or '(none)'} ]"
        ph.draw()

    # ── Portrait + NPC name (overlaid bottom-left of the scene) ──
    portrait = req.get("npc_portrait") or ""
    ptex = _try_load_portrait_texture(window, portrait)
    p_sz = 96
    p_l = img_l + 14
    p_b = img_b + 14
    if ptex is not None:
        arcade.draw_texture_rect(ptex, arcade.LBWH(p_l, p_b, p_sz, p_sz))
        arcade.draw_lrbt_rectangle_outline(
            p_l, p_l + p_sz, p_b, p_b + p_sz, COLOR_GOLD, 2,
        )
    name = str(req.get("npc_name") or "")
    if name:
        nm = _txt(state, "npc", p_l, p_b - 18, 14, COLOR_GOLD,
                  bold=True, anchor_x="left")
        nm.text = name
        nm.draw()

    # ── Request text (wrapped, below the scene) ─────────────────
    body = str(req.get("request_text") or "")
    text_y = img_b - 30
    if body:
        for li, line in enumerate(_wrap_text(body, max_chars=88)[:5]):
            bt = _txt(state, f"body_{li}", window.width / 2, text_y - li * 22,
                      13, COLOR_WHITE, anchor_x="center")
            bt.text = line
            bt.draw()
        text_y -= min(5, len(_wrap_text(body, max_chars=88))) * 22

    # ── Decision buttons (vertical stack, centred) ──────────────
    decs = decisions(state)
    btn_w = 420
    btn_h = 40
    gap = 10
    btn_l = window.width / 2 - btn_w / 2
    btn_r = btn_l + btn_w
    # Stack upward from a baseline so a long request text doesn't
    # collide with the buttons; clamp the baseline so they stay
    # on-screen even with several decisions.
    base_y = max(40, text_y - 30 - len(decs) * (btn_h + gap))
    for i, dec in enumerate(decs):
        b = base_y + (len(decs) - 1 - i) * (btn_h + gap)
        t = b + btn_h
        arcade.draw_lrbt_rectangle_filled(btn_l, btn_r, b, t, (70, 60, 40))
        arcade.draw_lrbt_rectangle_outline(btn_l, btn_r, b, t, COLOR_GOLD, 2)
        label = str(dec.get("label") or f"Option {i + 1}")
        # Append a terse outcome hint so the choice isn't blind.
        hint = _outcome_hint(dec)
        lt = _txt(state, f"btn_{i}", (btn_l + btn_r) / 2, b + 12, 14,
                  COLOR_WHITE, bold=True, anchor_x="center")
        lt.text = f"{label}   {hint}" if hint else label
        lt.draw()
        state.btn_rects.append((btn_l, btn_r, b, t, i))


def handle_click(state: RpgPlayerState, window: Any,
                 x: int, y: int) -> dict[str, Any] | None:
    """Hit-test the decision buttons. Returns the chosen decision dict
    if a button was clicked (so ``game_window`` can apply it), else
    ``None``. Like the cutscene player, the panel eats *every* click
    while up (the player can't place buildings through the scrim) — but
    only a button click resolves the request.
    """
    if not state.active:
        return None
    for (l, r, b, t, idx) in state.btn_rects:
        if l <= x <= r and b <= y <= t:
            return choose(state, idx)
    return None  # mis-click inside the scrim — eaten by the caller


# ── Helpers ──────────────────────────────────────────────────────────

def _outcome_hint(dec: dict[str, Any]) -> str:
    """A terse parenthetical so the player isn't choosing blind, e.g.
    "(−500 gold, +5 mood, delayed)". Pure string formatting."""
    bits: list[str] = []
    effects = dec.get("effects") or {}
    money = effects.get("money")
    if money:
        bits.append(f"{money:+d} gold")
    # Surface up to one non-money resource so the hint stays short.
    for k, v in effects.items():
        if k != "money" and v:
            bits.append(f"{v:+d} {k}")
            break
    happy = dec.get("happy") or 0
    if happy:
        bits.append(f"{happy:+d} mood")
    dip = dec.get("diplomacy") or 0
    if dip:
        bits.append(f"{dip:+d} dipl.")
    if dec.get("delay_ticks"):
        bits.append("delayed")
    return f"({', '.join(bits)})" if bits else ""


def _txt(state: RpgPlayerState, key: str, x: float, y: float,
         size: int = 12, color: tuple = (230, 230, 220),
         bold: bool = False, anchor_x: str = "left") -> Any:
    """Lazy text-pool, identical trick to cutscene_player._txt."""
    import arcade
    t = state._txt_pool.get(key)
    if t is None:
        t = arcade.Text("", x, y, color, size, bold=bold, anchor_x=anchor_x)
        state._txt_pool[key] = t
    t.x = x
    t.y = y
    t.color = color
    return t


def _load_scene_or_portrait(window: Any, subdir: str, name: str) -> Any:
    """Shared loader for scene/ and portraits/ textures. Returns the
    arcade.Texture or None (missing file → caller draws a placeholder).
    Caches on the window so repeated frames don't re-decode."""
    if not name:
        return None
    try:
        import arcade
        from constants import ROOT_DIR
        from pathlib import Path
        base = Path(ROOT_DIR) / "assets" / "textures" / subdir
        cache = getattr(window, "_rpg_texture_cache", None)
        if cache is None:
            cache = {}
            setattr(window, "_rpg_texture_cache", cache)
        for ext in (".png", ".jpg", ".jpeg"):
            candidate = base / f"{name}{ext}"
            if candidate.is_file():
                key = str(candidate)
                if key in cache:
                    return cache[key]
                tex = arcade.load_texture(key)
                cache[key] = tex
                return tex
    except Exception:  # noqa: BLE001
        log.exception("rpg_player: failed to load %s/%s", subdir, name)
        return None
    return None


def _try_load_scene_texture(window: Any, name: str) -> Any:
    return _load_scene_or_portrait(window, "scene", name)


def _try_load_portrait_texture(window: Any, name: str) -> Any:
    return _load_scene_or_portrait(window, "portraits", name)


def _wrap_text(text: str, max_chars: int = 88) -> list[str]:
    """Naive word-wrap, identical to cutscene_player._wrap_text."""
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for w in text.split():
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines
