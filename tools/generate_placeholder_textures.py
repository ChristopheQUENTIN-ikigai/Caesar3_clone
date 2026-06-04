"""Generate placeholder PNG textures into ./assets/textures.

Default behaviour::

    python tools/generate_placeholder_textures.py

Creates a starter sprite set so the texture pipeline can be exercised
end-to-end with no external art dependency. **Existing files are left
alone** — once you've replaced a placeholder with real art, running the
generator again won't clobber it.

Useful CLI flags::

    --force            Overwrite every PNG, including ones you've already
                       customised. Use after a major rework when you want
                       fresh placeholders across the board.

    --only buildings   Restrict generation to one section. Valid sections:
                       terrain, buildings, houses, walkers. Combine with
                       --force if you want to wipe just that section.

    --force-missing    Default behaviour, kept as an explicit flag for
                       scripts: only write files that don't exist yet.

    --dry-run          Print what would be written / skipped, do nothing.

The PNGs are deliberately ugly — flat colors, simple icons, no shading —
so the player knows at a glance which sprites are still placeholders
versus replaced with real art. See ``textures.py`` for the loader and
the on-disk layout.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from PIL import Image, ImageDraw

# Make the project root importable regardless of cwd, so `constants` works.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from constants import TILE_SIZE  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("placeholders")


ASSETS_DIR = ROOT / "assets" / "textures"
BUILDINGS_JSON = ROOT / "data" / "buildings.json"

# ── Terrain ──────────────────────────────────────────────────────────────────
TERRAINS: dict[str, tuple[int, int, int]] = {
    "grass":     (86, 125, 70),
    "grass_alt": (78, 118, 64),
    "water":     (64, 100, 170),
    # v0.25: hills/mountains/desert ship a placeholder too — until now
    # the loader fell back to flat-colour rectangles + a tiny triangle
    # overlay, which read as "uninitialised". Writing PNGs gives the
    # tile some texture (speckles for hills, peak shapes for
    # mountains, sand grains for desert) consistent with the grass /
    # water motifs.
    "hills":     (110, 120, 75),
    "mountains": (95, 85, 75),
    "desert":    (200, 175, 110),
}

# ── House tiers (shack → villa) ──────────────────────────────────────────────
HOUSE_TIERS: dict[int, tuple[int, int, int]] = {
    0: (130, 100, 70),
    1: (180, 140, 100),
    2: (210, 175, 130),
    3: (230, 200, 160),
    4: (245, 230, 200),
}

# ── Walkers ──────────────────────────────────────────────────────────────────
WALKERS: dict[str, tuple[int, int, int]] = {
    "worker":  (220, 180, 60),
    "trader":  (60, 160, 220),
    "citizen": (200, 200, 200),
    # v0.6
    "soldier": (200, 60, 60),
    "enemy":   (60, 30, 30),
    # v0.25 — ballista is a siege engine, drawn brown-grey to read as
    # "wooden machine" rather than "infantry". Heavier shape than the
    # other soldiers (single-pixel highlight isn't enough to read it
    # as a vehicle, but the colour difference is the first cue).
    "ballista": (110, 90, 70),
    # v0.33 — per-unit-type sprites so a bowman doesn't look identical
    # to a melee legionary, and a cavalry trooper reads as mounted.
    # Each is paired with a custom silhouette in ``_write_walker``
    # (bow line for archers, horse hindquarters for cavalry, paler
    # red for scouts, darker steel for heavy infantry, leather-tan
    # for barbarians). Colours stay deliberately desaturated — these
    # are placeholders, not finished art.
    "bowman":           (180, 130, 90),
    "heavy_cavalry":    (160, 50, 50),
    "scout":            (220, 130, 110),
    "heavy_infantry":   (140, 60, 70),
    "light_infantry":   (200, 60, 60),
    "barbarian_infantry": (110, 70, 40),
}

# Sprites for buildings get a small glyph in the centre so they're not all
# identical squares. Glyph is 1-3 ASCII chars sized to fit a tile.
BUILDING_GLYPHS: dict[str, str] = {
    "empty": "",
    "road": "═",
    "house": "H",
    "well": "○",
    "fountain": "♒",
    "aqueduct": "≡",
    "farm": "F",
    "workshop": "W",
    "mine": "M",
    "bakery": "B",
    "market": "$",
    "factory": "⚙",
    "tavern": "T",
    "theatre": "Θ",
    "temple": "+",
    "warehouse": "□",
    "port": "⚓",
    # ── v0.5 industry & supply chains ───────────────────────────────────
    "olive_farm": "♣",
    "olive_press": "Ø",
    "vineyard": "♠",
    "wine_press": "%",
    "clay_pit": "▽",
    "pottery_workshop": "U",
    "weapon_smith": "†",
    "granary": "G",
    "senate": "S",
    # ── v0.6 civic / military / water ────────────────────────────────
    "reservoir": "≈",
    "school": "A",
    "library": "L",
    "clinic": "+",
    "hospital": "✚",
    "prefecture": "P",
    "engineer_post": "E",
    "barracks": "X",
    "fort": "F",
    "tower": "T",
    # v0.25 — military manufacture (ballista workshop).
    "military_manufacture": "⚒",
}


def _darker(rgb: tuple[int, int, int], k: float = 0.7) -> tuple[int, int, int]:
    return tuple(max(0, int(c * k)) for c in rgb)  # type: ignore[return-value]


def _ensure_dirs() -> None:
    # v0.23.x: 'natural/' subfolder for primary-resource feature
    # placeholders (forest, ore veins, fertile soil, groundwater).
    # The texture loader checks terrain/ → natural/ → features/ in
    # that order, so dropping replacement art straight into natural/
    # picks up automatically. JPGs would lose alpha so these are
    # PNG-with-alpha — feature overlays let the grass tile show
    # through gaps in the motif.
    for sub in ("terrain", "buildings", "houses", "walkers", "natural"):
        (ASSETS_DIR / sub).mkdir(parents=True, exist_ok=True)


# ── Write gatekeeping ────────────────────────────────────────────────────────
# Every writer below calls `_should_write` first and returns immediately if
# the answer is False. This means we don't build the image in memory when
# we're going to skip it, AND we have a single source of truth for the
# skip/force/dry-run policy — a future writer can't forget the check and
# silently clobber user art.
class _Stats:
    """Outcome counts for the run summary."""
    def __init__(self) -> None:
        self.written: int = 0
        self.skipped: int = 0
        self.would_write: int = 0  # dry-run only


# Module-level config because the writers are module-level functions and
# threading this through every signature would clutter them. The CLI sets
# these up before any writer fires.
_stats = _Stats()
_force: bool = False
_dry_run: bool = False


def _should_write(path: Path, label: str) -> bool:
    """Return True if the writer should proceed with building & saving.

    Side effect: when returning False (skip), the skip counter is bumped
    and a SKIP line is logged. Writers don't need to log anything on
    skip themselves.
    """
    if path.exists() and not _force:
        _stats.skipped += 1
        log.info("  SKIP    %s  (already exists)", label)
        return False
    return True


def _save(img: Image.Image, path: Path, label: str) -> None:
    """Save *img* to *path*, honouring the dry-run flag and bumping the
    write counter. Callers must have already cleared `_should_write`."""
    existed = path.exists()
    if _dry_run:
        _stats.would_write += 1
        verb = "OVERWR*" if existed else "WRITE *"
        log.info("  %s %s  (dry run)", verb, label)
        return
    img.save(path)
    _stats.written += 1
    verb = "OVERWR " if existed else "WRITE  "
    log.info("  %s %s", verb, label)


def _write_terrain(name: str, color: tuple[int, int, int]) -> None:
    out = ASSETS_DIR / "terrain" / f"{name}.png"
    label = f"terrain/{name}.png"
    if not _should_write(out, label):
        return
    img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), color + (255,))
    draw = ImageDraw.Draw(img)
    # Faint dotted texture so adjacent tiles don't visually merge.
    if name == "water":
        # Wave hatching.
        for y in range(2, TILE_SIZE, 6):
            draw.line([(2, y), (TILE_SIZE - 3, y)], fill=_darker(color, 0.85), width=1)
    elif name == "hills":
        # v0.25: rolling-mound motif. Two soft arcs over the base
        # green-brown so a hill tile reads as "elevation, but still
        # walkable" — distinct from the flat grass texture and from
        # the angular mountain silhouette.
        dark = _darker(color, 0.65)
        # Two small arcs ('hills') near the centre.
        for cx in (10, 22):
            draw.arc(
                [cx - 5, TILE_SIZE // 2 - 2, cx + 5, TILE_SIZE // 2 + 4],
                start=180, end=360, fill=dark + (255,), width=2,
            )
        # Speckle the rest of the tile lightly so it doesn't read flat.
        for x in range(0, TILE_SIZE, 5):
            for y in range(0, TILE_SIZE, 5):
                if (x * 7 + y * 13) % 7 == 0:
                    draw.point((x, y), fill=_darker(color, 0.75))
    elif name == "mountains":
        # v0.25: two overlapping triangular peaks with a snow-cap
        # highlight. Reads as "unbuildable, impassable" at a glance
        # without relying on the on-the-fly silhouette the renderer
        # used to overlay when no texture was present.
        dark = _darker(color, 0.6)
        light = (220, 215, 210)
        # Big peak.
        draw.polygon(
            [(6, TILE_SIZE - 4), (16, 6), (24, TILE_SIZE - 4)],
            fill=dark + (255,),
        )
        # Snow cap on the big peak.
        draw.polygon(
            [(13, 11), (16, 6), (19, 11)],
            fill=light + (255,),
        )
        # Smaller foreground peak overlapping on the right.
        draw.polygon(
            [(18, TILE_SIZE - 4), (24, 12), (29, TILE_SIZE - 4)],
            fill=_darker(color, 0.5) + (255,),
        )
    elif name == "desert":
        # v0.25: sand-grain stippling on a pale base. A handful of
        # darker pixels suggests dune ripples; intentionally sparse so
        # the tile reads as flat-but-arid (still walkable).
        dark = _darker(color, 0.8)
        light = (255, 235, 180)
        for x in range(2, TILE_SIZE, 3):
            for y in range(2, TILE_SIZE, 3):
                k = (x * 5 + y * 11) % 11
                if k == 0:
                    draw.point((x, y), fill=dark)
                elif k == 3:
                    draw.point((x, y), fill=light)
        # A subtle horizontal "dune line" near the bottom.
        draw.line(
            [(4, TILE_SIZE - 8), (TILE_SIZE - 5, TILE_SIZE - 8)],
            fill=dark, width=1,
        )
    else:
        # Grass speckles — deterministic from coordinates so output is stable.
        for x in range(0, TILE_SIZE, 4):
            for y in range(0, TILE_SIZE, 4):
                if (x * 7 + y * 13) % 5 == 0:
                    px = _darker(color, 0.85)
                    draw.point((x, y), fill=px)
    _save(img, out, label)


def _write_building(bid: str, color: tuple[int, int, int], size: tuple[int, int]) -> None:
    cols, rows = size
    w, h = cols * TILE_SIZE, rows * TILE_SIZE
    out = ASSETS_DIR / "buildings" / f"{bid}.png"
    label = f"buildings/{bid}.png  ({w}x{h})"
    if not _should_write(out, label):
        return
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 2-pixel margin so adjacent buildings have a visible seam.
    draw.rectangle([2, 2, w - 3, h - 3], fill=color + (255,))
    # Inner darker rim — gives a tiny sense of depth versus a flat fill.
    draw.rectangle([2, 2, w - 3, h - 3], outline=_darker(color, 0.6), width=2)
    # Glyph (uses the default PIL font; sized to be roughly tile-centred).
    glyph = BUILDING_GLYPHS.get(bid, bid[:1].upper())
    if glyph:
        # Estimate the text bbox to centre it. The default font is small,
        # but readable enough as a placeholder.
        try:
            bbox = draw.textbbox((0, 0), glyph)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = 6, 8
        draw.text(((w - tw) / 2, (h - th) / 2 - 2), glyph, fill=(255, 255, 255, 230))
    _save(img, out, label)


def _write_house_tier(tier: int, color: tuple[int, int, int]) -> None:
    out = ASSETS_DIR / "houses" / f"tier{tier}.png"
    label = f"houses/tier{tier}.png"
    if not _should_write(out, label):
        return
    img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, TILE_SIZE - 3, TILE_SIZE - 3], fill=color + (255,))
    draw.rectangle([2, 2, TILE_SIZE - 3, TILE_SIZE - 3], outline=_darker(color, 0.55), width=2)
    # Tier dots in the corner — visible at a glance even on similar colours.
    for i in range(tier + 1):
        cx = 4 + i * 4
        draw.ellipse([cx, 4, cx + 2, 6], fill=(40, 30, 20, 255))
    _save(img, out, label)


def _write_walker(role: str, color: tuple[int, int, int]) -> None:
    out = ASSETS_DIR / "walkers" / f"{role}.png"
    label = f"walkers/{role}.png"
    if not _should_write(out, label):
        return
    size = 16
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size // 2
    outline = (30, 25, 20, 255)
    body = color + (255,)

    # v0.33: per-role silhouette dispatch. Civilian / generic combat
    # roles keep the legacy round-blob shape (back-compat). Specific
    # unit types get a distinguishing shape so the player can tell a
    # bowman from a cavalryman at a glance, even at 1× zoom. The
    # shapes are minimal — silhouette + 1-2 accent strokes — because
    # these are placeholders, not finished art.
    if role == "bowman":
        # Body: vertical oval (archer in profile). Bow: vertical arc
        # to the right side. Bright pixel for the head, slightly
        # offset so it reads as facing.
        draw.ellipse([cx - 3, cy - 5, cx + 3, cy + 5], fill=body)
        draw.ellipse([cx - 3, cy - 5, cx + 3, cy + 5], outline=outline, width=1)
        # Bow: arc from top-right to bottom-right of the body.
        bow_color = (60, 40, 20, 255)
        draw.arc(
            [cx + 1, cy - 6, cx + 7, cy + 6],
            start=300, end=60, fill=bow_color, width=1,
        )
        # Bowstring (vertical line just inside the arc).
        draw.line([(cx + 4, cy - 4), (cx + 4, cy + 4)], fill=(200, 180, 140, 255))
        # Head.
        draw.point((cx, cy - 3), fill=(255, 240, 200, 255))
    elif role == "heavy_cavalry":
        # Body: rider torso + horse body underneath. Horse is a
        # horizontal oval (cy + 2..cy + 6); rider is a small circle
        # on top. Together they read as "mounted" — taller, wider
        # than infantry.
        horse_color = _darker(color, 0.55) + (255,)
        # Horse body (horizontal oval, bigger than a typical walker).
        draw.ellipse([cx - 6, cy + 1, cx + 6, cy + 6], fill=horse_color)
        draw.ellipse([cx - 6, cy + 1, cx + 6, cy + 6], outline=outline, width=1)
        # Legs: 4 dark pixels under the body.
        for lx in (cx - 4, cx - 1, cx + 2, cx + 5):
            draw.point((lx, cy + 7), fill=outline)
        # Rider (circle on top).
        draw.ellipse([cx - 3, cy - 5, cx + 3, cy + 1], fill=body)
        draw.ellipse([cx - 3, cy - 5, cx + 3, cy + 1], outline=outline, width=1)
        # Head highlight.
        draw.point((cx, cy - 3), fill=(255, 240, 200, 255))
        # Lance pixel pointing up-right — extra "this is cavalry" cue.
        draw.line([(cx + 3, cy - 4), (cx + 6, cy - 7)], fill=(180, 180, 180, 255))
    elif role == "scout":
        # Smaller, lighter body — emphasises "fast / lightly equipped".
        # Same round blob but 4-px radius instead of 5, and a feather
        # pixel above the head so it doesn't look like a wounded
        # light_infantry.
        draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=body)
        draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], outline=outline, width=1)
        draw.point((cx, cy - 2), fill=(255, 240, 200, 255))
        # Feather / plume.
        draw.point((cx + 1, cy - 5), fill=(255, 255, 255, 255))
        draw.point((cx + 2, cy - 6), fill=(255, 255, 255, 255))
    elif role == "heavy_infantry":
        # Square shoulders + helmet crest. Wider body than the
        # default circle so it reads as "armoured".
        draw.rectangle([cx - 5, cy - 4, cx + 4, cy + 5], fill=body)
        draw.rectangle([cx - 5, cy - 4, cx + 4, cy + 5], outline=outline, width=1)
        # Helmet (dark cap on top).
        helmet = _darker(color, 0.5) + (255,)
        draw.rectangle([cx - 3, cy - 6, cx + 2, cy - 3], fill=helmet)
        # Crest (bright red ridge across the helmet top).
        draw.line([(cx - 3, cy - 7), (cx + 2, cy - 7)], fill=(220, 80, 60, 255))
    elif role == "barbarian_infantry":
        # Asymmetric, slightly hunched silhouette: oval body offset
        # left of centre, club / axe stroke on the right. Reads as
        # "raider" rather than disciplined legionary.
        draw.ellipse([cx - 5, cy - 4, cx + 3, cy + 5], fill=body)
        draw.ellipse([cx - 5, cy - 4, cx + 3, cy + 5], outline=outline, width=1)
        # Head (slightly to the left, with a horned-cap pixel above).
        draw.point((cx - 1, cy - 2), fill=(255, 230, 200, 255))
        draw.point((cx - 2, cy - 5), fill=outline)   # horn-tip pixel
        draw.point((cx + 1, cy - 5), fill=outline)   # horn-tip pixel
        # Crude weapon: short diagonal stroke top-right.
        draw.line([(cx + 3, cy - 3), (cx + 6, cy - 6)], fill=(90, 60, 30, 255))
    elif role == "light_infantry":
        # Same circle as the generic 'soldier' but with a small
        # rectangular shield outline on the left — reads as
        # 'standard legionary' rather than a militia citizen.
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=body)
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=outline, width=1)
        draw.point((cx, cy - 3), fill=(255, 240, 200, 255))
        # Shield (small rect on the left edge).
        shield = _darker(color, 0.5) + (255,)
        draw.rectangle([cx - 7, cy - 2, cx - 5, cy + 3], fill=shield)
        draw.rectangle([cx - 7, cy - 2, cx - 5, cy + 3], outline=outline, width=1)
    else:
        # Legacy default — civilian / soldier / enemy / ballista all
        # use this. Plain circle + head pixel, identical to v0.32.
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=body)
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=outline, width=1)
        draw.point((cx, cy - 3), fill=(255, 255, 255, 255))
    _save(img, out, label)


# ── v0.23.x: natural primary resources (forest, veins, deposits) ─────────────
# Each feature draws a small motif on a transparent background — the grass
# underneath shows through the gaps. Output goes to assets/textures/natural/
# as PNG-with-alpha. Artwork is *deliberately* simple (flat colour + 1px
# darker outline + minimal silhouette) so the player can tell at a glance
# this is a placeholder; they're meant to be replaced with real art later.

# Per-feature colour palette. Mirrors constants.FEATURE_COLORS so the
# placeholder rim matches the in-game minimap / overlay tint. Hard-coded
# here rather than imported because the generator is run as a CLI tool
# with the project root on sys.path; pulling constants.* would force an
# arcade import we don't need for a script that just writes PNGs.
NATURAL_COLORS: dict[str, tuple[int, int, int]] = {
    "forest":         (40, 100, 50),
    "iron_vein":      (110, 110, 130),
    "gold_vein":      (220, 180, 60),
    "copper_vein":    (180, 100, 60),
    "stone_deposit":  (140, 140, 140),
    "fertile_soil":   (140, 100, 60),
    "groundwater":    (100, 150, 200),
    # v0.25: fish — silvery on a water-blue base. Drawn over the
    # water terrain tile (which the renderer paints first); the
    # `fish.png` overlay layers on top with transparency so the
    # water still reads as water underneath.
    "fish":           (200, 220, 240),
    # v0.26: clay deposit — clay-orange-brown trough/lumps. The
    # Clay Pit needs a clay_deposit tile in its footprint (see
    # FEATURE_CLAY_DEPOSIT in constants.py and the v0.26 changelog).
    # The motif is a small irregular clay-pit silhouette with a few
    # clumps so it reads distinctly from the stone-deposit boulders
    # (boxy grey rectangles) and the ore-vein flecks (rounded
    # ellipses). Mirrors constants.FEATURE_COLORS[FEATURE_CLAY_DEPOSIT].
    "clay_deposit":   (175, 120, 90),
}


def _draw_forest(draw: ImageDraw.ImageDraw, color: tuple[int, int, int]) -> None:
    """Three small tree silhouettes, transparent gaps between them.

    Trees are isoceles triangles with a 1-px trunk pixel below — at
    32×32 each tree is ~10px tall. Positions chosen so adjacent
    forest tiles tile together visually (no tree clipping the edge)."""
    dark = _darker(color, 0.6)
    trunk = (60, 40, 25, 255)
    # (cx, base_y, half_w, height)
    trees = [
        (8,  22,  5, 10),
        (20, 18,  5, 10),
        (14, 28,  4,  8),
    ]
    for cx, by, hw, ht in trees:
        # Triangle canopy.
        draw.polygon(
            [(cx, by - ht), (cx - hw, by), (cx + hw, by)],
            fill=color + (255,), outline=dark + (255,),
        )
        # 1×2 trunk pixel.
        draw.rectangle([cx, by, cx + 1, by + 2], fill=trunk)


def _draw_vein(draw: ImageDraw.ImageDraw, color: tuple[int, int, int]) -> None:
    """Cluster of ore flecks. Used by iron / gold / copper veins —
    only the colour changes between them.

    A handful of small filled ellipses scattered in a rough cluster,
    each with a darker rim so they read as 3D nuggets rather than a
    paint splash. The cluster sits in the centre of the tile leaving
    transparent margins so the grass underneath is still visible."""
    dark = _darker(color, 0.55)
    # (cx, cy, rx, ry) — 5 nuggets in a vague diamond pattern.
    nuggets = [
        (16, 10, 3, 2),
        (10, 16, 3, 3),
        (22, 16, 3, 3),
        (16, 22, 4, 3),
        (16, 16, 2, 2),  # central highlight
    ]
    for cx, cy, rx, ry in nuggets:
        draw.ellipse(
            [cx - rx, cy - ry, cx + rx, cy + ry],
            fill=color + (255,), outline=dark + (255,),
        )


def _draw_stone_deposit(draw: ImageDraw.ImageDraw, color: tuple[int, int, int]) -> None:
    """Three chunky boulders. Same idea as veins but bigger and
    blocky rather than rounded — stone reads as 'pile of rocks',
    veins as 'flecks in the rock'."""
    dark = _darker(color, 0.6)
    boulders = [
        # (l, t, r, b)
        (4, 10, 14, 22),
        (14, 6, 22, 16),
        (16, 18, 26, 28),
    ]
    for box in boulders:
        draw.rectangle(box, fill=color + (255,), outline=dark + (255,))


def _draw_fertile_soil(draw: ImageDraw.ImageDraw, color: tuple[int, int, int]) -> None:
    """Horizontal furrow lines — looks like a tilled field. Faintly
    transparent so the grass tile underneath still tints through."""
    dark = _darker(color, 0.5)
    alpha = 200  # not fully opaque — soil tints, doesn't replace
    for y in range(4, TILE_SIZE - 2, 5):
        draw.line(
            [(3, y), (TILE_SIZE - 4, y)],
            fill=color + (alpha,), width=2,
        )
        draw.line(
            [(3, y + 2), (TILE_SIZE - 4, y + 2)],
            fill=dark + (alpha,), width=1,
        )


def _draw_groundwater(draw: ImageDraw.ImageDraw, color: tuple[int, int, int]) -> None:
    """Wave-ripple motif, semi-transparent. Distinct from the
    'water' terrain tile — that's solid blue; groundwater is a
    *feature* on top of grass meaning "you can sink a well here"."""
    dark = _darker(color, 0.6)
    alpha = 180
    # Three nested arcs evoking a ripple. PIL's arc takes a bbox.
    for r, w in [(10, 2), (6, 2), (3, 1)]:
        draw.arc(
            [TILE_SIZE // 2 - r, TILE_SIZE // 2 - r,
             TILE_SIZE // 2 + r, TILE_SIZE // 2 + r],
            start=200, end=340,
            fill=color + (alpha,), width=w,
        )
    # A single dark pixel at the centre — the 'spring source'.
    draw.ellipse(
        [TILE_SIZE // 2 - 2, TILE_SIZE // 2 - 2,
         TILE_SIZE // 2 + 2, TILE_SIZE // 2 + 2],
        fill=dark + (255,),
    )


def _draw_fish(draw: ImageDraw.ImageDraw, color: tuple[int, int, int]) -> None:
    """v0.25: two small fish silhouettes on a water tile.

    The base layer is the water terrain texture (painted by the
    renderer below us); this overlay adds a couple of silvery fish
    shapes so the player can see "this water has fish here". A small
    ripple under each fish keeps the motif legible at 32×32.

    Fish geometry: a teardrop body + a triangular tail. Body in the
    `color` parameter (pale silvery blue-white), tail darker so the
    fish reads as a single creature rather than two blobs.
    """
    dark = _darker(color, 0.55)
    eye = (30, 40, 60)
    ripple = (180, 210, 230)

    # Fish 1 — upper-left, pointing right.
    # Body: an ellipse.
    draw.ellipse([4, 9, 16, 15], fill=color + (255,), outline=dark + (255,))
    # Tail: a triangle on the left.
    draw.polygon(
        [(4, 12), (1, 8), (1, 16)],
        fill=dark + (255,),
    )
    # Eye: a single dark pixel near the nose.
    draw.point((14, 11), fill=eye + (255,))
    # Ripple under fish 1.
    draw.line([(3, 18), (12, 18)], fill=ripple + (180,), width=1)

    # Fish 2 — lower-right, pointing left (mirror).
    draw.ellipse([16, 20, 28, 26], fill=color + (255,), outline=dark + (255,))
    # Tail: a triangle on the right.
    draw.polygon(
        [(28, 23), (31, 19), (31, 27)],
        fill=dark + (255,),
    )
    # Eye near the nose (left side).
    draw.point((18, 22), fill=eye + (255,))
    # Ripple under fish 2.
    draw.line([(18, 29), (28, 29)], fill=ripple + (180,), width=1)


def _draw_clay_deposit(draw: ImageDraw.ImageDraw, color: tuple[int, int, int]) -> None:
    """v0.26: clay deposit motif — a shallow pit silhouette with two
    or three lumps of raw clay piled at the edge.

    Visually distinct from stone deposit (boxy grey rectangles) and
    the ore veins (rounded flecks): clay reads as a soft earth-tone
    excavation. The pit is a darker oval at centre-bottom; clay
    chunks are smaller, asymmetric, lighter on top to fake a
    slight 3D feel. Transparent margins let the grass tint through.
    """
    dark = _darker(color, 0.55)
    # Inline a "lighter" tint — _darker(k=1.15) would also work,
    # but the project only ships `_darker`; computing it explicitly
    # keeps the dependency surface zero and the channel arithmetic
    # obvious.
    light = tuple(min(255, int(c * 1.18)) for c in color)
    # The pit — a flattened darker ellipse at the bottom of the tile.
    draw.ellipse([5, 18, 27, 28], fill=dark + (255,), outline=_darker(color, 0.4) + (255,))
    # Three clay lumps. Sizes/positions chosen so they don't tile
    # awkwardly with neighbours (no pixel-bleed at the edges).
    lumps = [
        # (l, t, r, b)
        (6, 6, 14, 14),    # left lump
        (16, 4, 24, 13),   # back-right lump (slightly taller)
        (10, 11, 19, 19),  # central foreground lump
    ]
    for box in lumps:
        draw.ellipse(box, fill=color + (255,), outline=dark + (255,))
    # A small lighter highlight on the back-right lump so it reads
    # as 3D rather than a flat blob.
    draw.ellipse([18, 6, 21, 9], fill=light + (255,))


# Dispatch table — keep parallel to NATURAL_COLORS. Adding a new
# feature is a one-line entry here + a new _draw_xxx function.
NATURAL_DRAWERS: dict[str, "callable"] = {
    "forest":         _draw_forest,
    "iron_vein":      _draw_vein,
    "gold_vein":      _draw_vein,
    "copper_vein":    _draw_vein,
    "stone_deposit":  _draw_stone_deposit,
    "fertile_soil":   _draw_fertile_soil,
    "groundwater":    _draw_groundwater,
    "fish":           _draw_fish,
    "clay_deposit":   _draw_clay_deposit,
}


def _write_natural(feat_id: str, color: tuple[int, int, int]) -> None:
    """Write one natural-feature placeholder PNG to assets/textures/natural/.

    Output is a TILE_SIZE × TILE_SIZE PNG with alpha — the motif is
    drawn opaque-ish and the rest of the canvas is transparent so the
    grass terrain tile shows through underneath when the feature
    overlay is composited.
    """
    out = ASSETS_DIR / "natural" / f"{feat_id}.png"
    label = f"natural/{feat_id}.png"
    if not _should_write(out, label):
        return
    img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    drawer = NATURAL_DRAWERS.get(feat_id)
    if drawer is None:
        # Unknown feature id — fall back to a small filled square so
        # the loader still finds *something* and the artist can iterate.
        # Keeping the warning quiet (this isn't a bug, just a new id
        # the dispatch table hasn't been taught about yet).
        log.info(
            "  natural/%s: no specialised drawer, writing flat fallback",
            feat_id,
        )
        dark = _darker(color, 0.6)
        draw.rectangle(
            [4, 4, TILE_SIZE - 5, TILE_SIZE - 5],
            fill=color + (200,), outline=dark + (255,), width=2,
        )
    else:
        drawer(draw, color)
    _save(img, out, label)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate placeholder textures into ./assets/textures.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help=("Overwrite every PNG, including ones you've already customised. "
              "Default behaviour is to skip files that already exist."),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be written / skipped, do nothing.",
    )
    parser.add_argument(
        "--only", choices=("terrain", "buildings", "houses", "walkers", "natural"),
        action="append", default=None,
        help=("Limit generation to one or more sections. Repeat to include "
              "several. Default: all five sections."),
    )
    args = parser.parse_args()

    global _force, _dry_run
    _force = bool(args.force)
    _dry_run = bool(args.dry_run)

    sections = set(args.only) if args.only else {
        "terrain", "buildings", "houses", "walkers", "natural",
    }

    log.info(
        "Generating placeholder textures into %s%s%s",
        ASSETS_DIR,
        "  [force=on]" if _force else "  [force=off — existing files preserved]",
        "  [dry-run]" if _dry_run else "",
    )
    _ensure_dirs()

    if "terrain" in sections:
        log.info("Terrain:")
        for name, color in TERRAINS.items():
            _write_terrain(name, color)

    if "buildings" in sections:
        log.info("Buildings:")
        with BUILDINGS_JSON.open() as f:
            bdefs = json.load(f)
        for bid, data in bdefs.items():
            color = tuple(data["color"])
            size = tuple(data["size"])
            _write_building(bid, color, size)  # type: ignore[arg-type]

    if "houses" in sections:
        log.info("House tiers:")
        for tier, color in HOUSE_TIERS.items():
            _write_house_tier(tier, color)

    if "walkers" in sections:
        log.info("Walkers:")
        for role, color in WALKERS.items():
            _write_walker(role, color)

    if "natural" in sections:
        log.info("Natural primary resources:")
        for feat_id, color in NATURAL_COLORS.items():
            _write_natural(feat_id, color)

    # ── Summary ──────────────────────────────────────────────────────────
    log.info("")
    if _dry_run:
        log.info(
            "Dry run: would write %d, skipped %d existing.",
            _stats.would_write, _stats.skipped,
        )
    else:
        log.info(
            "Wrote %d, skipped %d existing.", _stats.written, _stats.skipped,
        )
    if _stats.skipped > 0 and not _force:
        log.info(
            "Use --force to overwrite existing files (e.g. after a balance pass).",
        )


if __name__ == "__main__":
    main()
