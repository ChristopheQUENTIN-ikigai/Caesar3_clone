#!/usr/bin/env python3
"""
Caesar III Clone — City Builder Economy Simulation
Built with Python 3.11+ and Arcade 3.x

Run:  python main.py
"""

import arcade
import logging
import pyglet
from game_window import CaesarGameWindow

# Default windowed size — used when the detected screen is large enough.
# On smaller displays we shrink to fit so the game is fully visible without
# the player having to fight a window manager.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800
SCREEN_TITLE = "Caesar III Clone — City Builder Economy Simulation"
# Leave some slack around the window for the OS chrome (taskbar, title bar)
# so we don't accidentally span the full screen and look like a broken
# fullscreen toggle.
SCREEN_MARGIN = 80

# ── Terminal logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("caesar3")


def detect_screen_size() -> tuple[int, int]:
    """Return ``(screen_width, screen_height)`` for the default monitor.

    Uses pyglet (already a transitive dependency of arcade) so this
    works headless-ishly: if no display is available (CI, SSH-no-X) we
    fall back to the default size and log a warning rather than crashing.
    """
    try:
        display = pyglet.display.get_display()
        screen = display.get_default_screen()
        return int(screen.width), int(screen.height)
    except Exception as e:  # noqa: BLE001 — any display error → fallback
        log.warning("Could not detect screen size (%s); using defaults", e)
        return DEFAULT_WIDTH, DEFAULT_HEIGHT


def pick_window_size(screen_w: int, screen_h: int) -> tuple[int, int]:
    """Pick the largest sensible windowed size that fits this screen.

    Strategy: cap at the default 1280×800; shrink only if the screen is
    too small for it (rare laptops, virtual desktops). We never grow
    larger than the default because the HUD layout was designed at that
    resolution — going bigger leaves dead space in the bottom bar.
    """
    target_w = min(DEFAULT_WIDTH, max(800, screen_w - SCREEN_MARGIN))
    target_h = min(DEFAULT_HEIGHT, max(600, screen_h - SCREEN_MARGIN))
    return target_w, target_h


def main():
    screen_w, screen_h = detect_screen_size()
    log.info("Detected screen size: %dx%d", screen_w, screen_h)
    width, height = pick_window_size(screen_w, screen_h)
    if (width, height) != (DEFAULT_WIDTH, DEFAULT_HEIGHT):
        log.info("Window adapted to fit screen: %dx%d", width, height)
    log.info("Starting %s  (%dx%d)", SCREEN_TITLE, width, height)
    log.info("Arcade version: %s", arcade.VERSION)
    window = CaesarGameWindow(width, height, SCREEN_TITLE)
    window.setup()
    arcade.run()
    log.info("Game closed.")


if __name__ == "__main__":
    main()
