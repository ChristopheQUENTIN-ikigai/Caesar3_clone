"""Generate flat-color placeholder PNGs for the Tribute Crisis scenario.

Style matches ``tools/generate_placeholder_textures.py`` — ugly on
purpose so the author sees at a glance which art is still a stand-in.
Two kinds:

  * scenes:    320 x 180, deep background colour + a centered glyph
  * portraits:  96 x 96,  flat bust silhouette + a single-letter badge

Filenames match the basenames referenced in
``data/scenarios/tribute_crisis/cutscenes.json`` and
``data/scenarios/tribute_crisis/rpg_requests.json`` so the runtime
loader picks them up directly.

By default the script writes into the scenario's own ``assets/`` tree
*and* the project-level runtime texture trees, mirroring the manual
install step in ``data/scenarios/tribute_crisis/README.md``. Pass
``--scenario-only`` to write only the scenario tree (useful when
copying a scenario between projects).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


# ── Asset tables ─────────────────────────────────────────────────────
# Background colour sets the mood; the glyph in the centre is a single
# letter inside a darker rect so the eye finds it. Both signals are
# obvious-placeholder cues. v0.36 added ``caesar_uneasy`` (partial-
# tribute branch) and ``abandoned_village`` (raid-abandoned branch).
SCENES: dict[str, tuple[tuple[int, int, int], str]] = {
    "frontier_dawn":     ((110, 130, 140), "FD"),  # cool dawn, intro
    "burning_village":   ((140,  60,  40), "BV"),  # raid aftermath
    "caesar_pleased":    ((180, 150,  60), "CP"),  # gold throne
    "caesar_wrath":      ((110,  30,  30), "CW"),  # red throne
    "caesar_uneasy":     ((150,  90,  50), "CR"),  # partial-tribute amber
    "victory_parade":    ((180, 170, 100), "VP"),  # warm gold
    "ruined_forum":      (( 70,  60,  50), "RF"),  # defeat
    "abandoned_village": (( 90,  70,  60), "DV"),  # raid abandoned
}

PORTRAITS: dict[str, tuple[tuple[int, int, int], str]] = {
    "village_elder":   ((150, 130, 100), "E"),
    "raid_survivor":   ((130,  90,  70), "R"),
    "tribune":         (( 90, 110, 150), "T"),
}


# Tiny 3x5 bitmap font — only the glyphs we use. Each char is a list of
# 5 rows of 3 chars ("#"=on, " "=off). Drawn as filled rectangles of
# size ``scale``, so a glyph at scale=4 takes 12x20 px.
FONT_3x5: dict[str, list[str]] = {
    "B": ["## ", "# #", "## ", "# #", "## "],
    "C": [" ##", "#  ", "#  ", "#  ", " ##"],
    "D": ["## ", "# #", "# #", "# #", "## "],
    "E": ["###", "#  ", "## ", "#  ", "###"],
    "F": ["###", "#  ", "## ", "#  ", "#  "],
    "P": ["## ", "# #", "## ", "#  ", "#  "],
    "R": ["## ", "# #", "## ", "# #", "# #"],
    "T": ["###", " # ", " # ", " # ", " # "],
    "V": ["# #", "# #", "# #", "# #", " # "],
    "W": ["# #", "# #", "# #", "###", "# #"],
}


def _stamp_glyph(
    d: ImageDraw.ImageDraw, text: str, x: int, y: int, scale: int,
    color: tuple[int, int, int, int],
) -> None:
    """Stamp short uppercase text using the 3x5 bitmap font."""
    cursor_x = x
    for ch in text:
        glyph = FONT_3x5.get(ch)
        if glyph is None:
            cursor_x += 4 * scale
            continue
        for ry, row in enumerate(glyph):
            for rx, cell in enumerate(row):
                if cell != " ":
                    px = cursor_x + rx * scale
                    py = y + ry * scale
                    d.rectangle(
                        (px, py, px + scale - 1, py + scale - 1),
                        fill=color,
                    )
        cursor_x += 4 * scale  # 3 cols + 1 gap


def _draw_scene(path: Path, color: tuple[int, int, int], glyph: str) -> None:
    """320x180 background + dark centered placard with the glyph."""
    im = Image.new("RGBA", (320, 180), color + (255,))
    d = ImageDraw.Draw(im)
    # Horizon line (top 2/3 sky, bottom 1/3 ground) — a tiny gradient
    # cue so the scenes don't look identical at thumbnail size.
    ground = tuple(max(0, c - 40) for c in color) + (255,)
    d.rectangle((0, 120, 320, 180), fill=ground)
    # Placard rect with glyph (no font dependency — we draw the
    # letters as filled rectangles).
    d.rectangle((130, 70, 190, 110), fill=(30, 30, 30, 255))
    d.rectangle((130, 70, 190, 110), outline=(220, 220, 220, 255), width=2)
    _stamp_glyph(d, glyph, x=140, y=78, scale=4, color=(220, 220, 220, 255))
    im.save(path)


def _draw_portrait(
    path: Path, color: tuple[int, int, int], badge: str,
) -> None:
    """96x96 oval bust + a single-letter badge bottom-right."""
    im = Image.new("RGBA", (96, 96), (40, 40, 50, 255))
    d = ImageDraw.Draw(im)
    # Shoulders (trapezoid approximated with a rect + ellipse on top).
    d.rectangle((18, 60, 78, 96), fill=color + (255,))
    # Head — circle.
    d.ellipse((30, 18, 66, 54), fill=color + (255,))
    # Outline around the bust.
    d.rectangle((0, 0, 95, 95), outline=(20, 20, 25, 255), width=2)
    # Badge: dark square + glyph in the corner.
    d.rectangle((72, 72, 92, 92), fill=(20, 20, 25, 255))
    _stamp_glyph(d, badge, x=76, y=76, scale=2, color=(230, 220, 180, 255))
    im.save(path)


def _write_set(
    scene_dir: Path, portrait_dir: Path,
) -> tuple[int, int]:
    scene_dir.mkdir(parents=True, exist_ok=True)
    portrait_dir.mkdir(parents=True, exist_ok=True)
    for name, (color, glyph) in SCENES.items():
        _draw_scene(scene_dir / f"{name}.png", color, glyph)
    for name, (color, badge) in PORTRAITS.items():
        _draw_portrait(portrait_dir / f"{name}.png", color, badge)
    return len(SCENES), len(PORTRAITS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parent.parent),
        help=(
            "Project root (default: parent of tools/). The script "
            "writes to data/scenarios/tribute_crisis/assets/ and to "
            "assets/textures/{scene,portraits}/ relative to this."
        ),
    )
    parser.add_argument(
        "--scenario-only", action="store_true",
        help=(
            "Write only into the scenario's own assets/ tree; skip "
            "the runtime texture trees."
        ),
    )
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    scenario = root / "data" / "scenarios" / "tribute_crisis"
    n_sc, n_po = _write_set(
        scenario / "assets" / "scene",
        scenario / "assets" / "portraits",
    )
    print(f"wrote {n_sc} scenes + {n_po} portraits → {scenario}/assets/")
    if args.scenario_only:
        return
    runtime_scene = root / "assets" / "textures" / "scene"
    runtime_port = root / "assets" / "textures" / "portraits"
    _write_set(runtime_scene, runtime_port)
    print(f"also wrote → {runtime_scene} and {runtime_port}")


if __name__ == "__main__":
    main()
