"""Auto-generate resource icon textures. (v0.19)

The 'resources visible when extracted/transported/stored' brief asks
for one PNG per resource at ``./assets/textures/resources/<id>.png``.
Each icon is a small, color-coded glyph drawn with PIL. The same
art-direction the placeholder building generator uses (flat shape +
1px outline + tiny inner mark) so resources read at 16×16 inside
walker tooltips and warehouse icons.

Why generate rather than ship art?

  * One source of truth for the colour and the on-disk file means
    the HUD tint and the sprite never drift apart.
  * Modders dropping in real artwork keeps working — the registry
    only generates a placeholder when the file is missing. (See
    ``tools/generate_placeholder_textures.py`` for the same
    pattern on buildings.)

Run ``python tools/generate_resource_textures.py`` to (re)generate.
The game itself does NOT call this on launch — it would slow boot for
no benefit since the registry's missing-texture fallback is colored
rectangles. We document the script in CHANGELOG so a player who wants
the icons runs it once.

Output: ``./assets/textures/resources/<id>.png`` at 32×32 px (one
tile-size). Walkers / warehouses scale-down to their intended footprint
at draw time.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Make the parent directory importable so we can read the bartering
# table for the canonical resource list.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from bartering import STOCK_PRICES  # noqa: E402

log = logging.getLogger("caesar3.gen_resources")

# Per-resource colour palette. The values double as the inner-glyph
# colour; the outer rim is always a dark brown for readability over
# the grass / road tiles.
RESOURCE_PALETTE: dict[str, tuple[int, int, int]] = {
    # subsistence
    "bread":        (210, 170, 110),
    "vegetables":   (110, 170,  80),
    "fruits":       (220, 100,  90),
    "meat":         (180,  60,  70),
    "fish":         (110, 160, 200),
    "cheese":       (240, 220, 140),
    "oil":          (210, 200,  80),
    "honey":        (230, 190,  60),
    "spice":        (190,  90, 110),
    "wine":         (130,  40,  90),
    # raw / intermediate
    "wheat":        (220, 200,  90),
    "flour":        (235, 225, 195),
    "olives":       (110, 130,  70),
    "grapes":       (130,  70, 130),
    "clay":         (180, 110,  80),
    "pottery":      (200, 110,  80),
    "livestock":    (170, 130,  90),
    "horses":       (140, 100,  60),
    # industry / civic
    "wood":         (140, 100,  60),
    "planks":       (170, 130,  80),
    "stone":        (140, 140, 140),
    "stone_blocks": (170, 170, 170),
    "iron":         (150, 150, 170),
    "tools":        (200, 150, 100),
    "weapons":      (180, 130,  90),
    # v0.26 — mining tab metals.
    "copper_ore":   (150,  90,  55),
    "gold_ore":     (200, 170,  70),
    "copper":       (190, 110,  60),
    "gold":         (230, 195,  80),
}

OUT_DIR = _HERE.parent / "assets" / "textures" / "resources"
SIZE = 32


def _draw_resource(resource_id: str, color: tuple[int, int, int]) -> "Image.Image":
    """Draw a single resource icon."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Outer dark frame (trapezoid suggesting a sack / crate).
    rim = (40, 30, 22)
    d.rectangle([2, 6, SIZE - 3, SIZE - 3], fill=rim)
    # Inner block.
    d.rectangle([4, 8, SIZE - 5, SIZE - 5], fill=color)
    # Highlight strip.
    hi = tuple(min(255, int(c * 1.25)) for c in color)
    d.rectangle([4, 8, SIZE - 5, 12], fill=hi)
    # Small letter — first two letters of the id, uppercase. Helps the
    # player tell "wheat" from "flour" without zoom.
    label = resource_id[:2].upper()
    try:
        # Default PIL bitmap font; cheap, no font file needed.
        d.text((SIZE // 2 - 6, SIZE // 2 - 3), label,
               fill=(20, 15, 8))
    except Exception:
        pass
    return img


def generate_all() -> int:
    """Generate every resource icon. Returns the count written."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    # Iterate the bartering table so we cover every priced resource.
    # Anything with no palette entry uses a neutral grey.
    default_color = (160, 150, 140)
    for rid in STOCK_PRICES:
        color = RESOURCE_PALETTE.get(rid, default_color)
        img = _draw_resource(rid, color)
        out = OUT_DIR / f"{rid}.png"
        img.save(out)
        written += 1
    log.info("Wrote %d resource icons to %s", written, OUT_DIR)
    return written


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n = generate_all()
    print(f"Wrote {n} resource textures to {OUT_DIR}")
