"""Texture registry — loads PNG / JPEG / JPG / WEBP from
``./assets/textures`` and serves them up with a graceful fallback when a
file is missing.

Layout on disk (relative to the project root)::

    assets/textures/
        terrain/
            grass.png
            grass_alt.png
            water.png
        buildings/
            road.png
            house.png        # generic house (used if no per-tier sprite)
            farm.png
            workshop.png
            ...one image per building id in data/buildings.json
        houses/              # optional per-tier house sprites
            tier0.png        # shack
            tier1.png        # insula
            tier2.png        # simple domus
            tier3.png        # domus
            tier4.png        # villa
        walkers/
            worker.png
            trader.png
            citizen.png
        resources/              # v0.19.x — carry-icon + warehouse-icon set
            wheat.png
            bread.png
            ...one image per priced resource in bartering.STOCK_PRICES
        ui/
            splash.jpg       # main-menu background

For a given ``(category, name)`` lookup the registry first consults the
**manifest** at ``./data/textures.json``: if the manifest names a
specific file for that key, the registry tries that file first. If the
manifest doesn't name one, the registry falls back to trying
``<name>.<ext>`` for each supported extension. The first hit wins.

The manifest accepts **both the internal id (e.g. ``farm``) and the
human-readable name (e.g. ``wheat farm``) as keys** — modders can use
whichever is convenient. Lookups normalise to lower-case so case-
sensitivity isn't a footgun.

PNG is preferred for tile sprites because it has a real alpha channel
— but JPEG / JPG is fine for backgrounds (splash screen, large
illustrations) where transparency isn't needed and file size matters.

Anything missing is fine — callers fall back to the colored rectangle they
were drawing before. This means **modders can drop in PNGs / JPGs
incrementally** without breaking the game.

Sprites should be square, sized to one tile (TILE_SIZE×TILE_SIZE px) for
1×1 buildings; multi-tile buildings can ship a single sprite that the
renderer scales to (width*TILE_SIZE) × (height*TILE_SIZE). RGBA recommended
so transparency works around irregular building footprints.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import arcade

log = logging.getLogger("caesar3.textures")


ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "textures"
# v0.17: optional texture manifest. Lets the player pick a custom
# filename for any given key (building id, terrain id, walker role,
# etc.) without renaming the file or hacking the engine. Loaded
# lazily on first use so a project that doesn't have one runs fine.
MANIFEST_PATH = Path(__file__).resolve().parent / "data" / "textures.json"

# Extensions tried in order. PNG first because tile sprites benefit from
# its alpha channel; JPEG variants follow for backgrounds and screenshots
# the player drops in. WEBP is included because some artwork pipelines
# default to it. Case-insensitive on disk: we try lowercase only — most
# filesystems are case-sensitive (Linux, macOS APFS in some configs) so
# we expect modders to use lowercase, which the placeholder generator
# already does.
SUPPORTED_EXTENSIONS: tuple[str, ...] = ("png", "jpg", "jpeg", "webp")


def _load_manifest(path: Path = MANIFEST_PATH) -> dict[str, dict[str, str]]:
    """Return the texture manifest dict, or an empty dict if absent.

    The manifest's top-level keys are categories (``buildings``,
    ``terrain``, ``walkers``, ``houses``, ``features``, ``ui``); each
    is a dict from key → filename. Keys are normalised to lower-case
    so the manifest is case-insensitive.
    """
    if not path.is_file():
        return {}
    try:
        with path.open() as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read texture manifest %s: %s", path, e)
        return {}
    out: dict[str, dict[str, str]] = {}
    for cat, entries in raw.items():
        if cat.startswith("_"):  # skip _comment etc.
            continue
        if not isinstance(entries, dict):
            continue
        out[cat] = {str(k).lower(): str(v) for k, v in entries.items()}
    return out


class TextureRegistry:
    """Lazy on-demand loader.

    First call to ``get(category, name)`` consults the manifest for a
    specific filename; if missing, falls back to trying
    ``assets/textures/<category>/<name>.<ext>`` for each extension in
    ``SUPPORTED_EXTENSIONS``. The result — an ``arcade.Texture`` or
    ``None`` — is cached so missing files don't get retried each frame.
    """

    def __init__(self, root: Path = ASSETS_DIR, manifest_path: Path = MANIFEST_PATH):
        self.root = root
        self._cache: dict[tuple[str, str], arcade.Texture | None] = {}
        self._load_attempts = 0
        self._load_hits = 0
        self._manifest = _load_manifest(manifest_path)

    # ── Public API ────────────────────────────────────────────────────────
    def get(self, category: str, name: str) -> arcade.Texture | None:
        """Return the texture for ``category/name`` or None if not on disk.

        The manifest is consulted first: if a category/name pair has
        an entry, we try that exact filename. If not (or if the
        manifest-named file is missing on disk), we fall back to the
        legacy ``<name>.<ext>`` lookup across the supported
        extensions.
        """
        key = (category, name)
        if key in self._cache:
            return self._cache[key]
        self._load_attempts += 1
        cat_dir = self.root / category
        tex: arcade.Texture | None = None

        # 1) Manifest hit — try the named file first. We accept the
        #    name verbatim and also lower-case (modders typing "Wheat
        #    Farm" should match our internal lower-cased manifest).
        manifest_cat = self._manifest.get(category, {})
        candidates: list[str] = []
        manifest_filename = manifest_cat.get(name.lower())
        if manifest_filename:
            candidates.append(manifest_filename)

        # 2) Legacy fallback — <name>.<ext> for each supported ext.
        for ext in SUPPORTED_EXTENSIONS:
            candidates.append(f"{name}.{ext}")

        for fname in candidates:
            path = cat_dir / fname
            if not path.is_file():
                continue
            try:
                tex = arcade.load_texture(str(path))
                self._load_hits += 1
                log.debug("Loaded texture %s", path)
                break
            except Exception as e:  # noqa: BLE001 — arcade can raise many things
                log.warning("Failed to load %s: %s — trying next candidate", path, e)
                tex = None
        self._cache[key] = tex
        return tex

    def terrain(self, name: str) -> arcade.Texture | None:
        return self.get("terrain", name)

    def building(self, building_id: str) -> arcade.Texture | None:
        # v0.17: also try the building's display name as a manifest
        # key, so an entry like {"wheat farm": "wheat_farm.jpg"}
        # works alongside the id-keyed entry. We can't see the name
        # at this layer (we only have the id) — but the manifest
        # loader stores both id-keyed and name-keyed entries on the
        # same flat dict, so a name-keyed lookup will simply miss
        # quietly when the call site uses the id. To make a name
        # lookup work, the caller passes the name through `get`
        # directly.
        return self.get("buildings", building_id)

    def building_by_name(self, building_name: str) -> arcade.Texture | None:
        """Look up a building texture using the human-readable name
        rather than the id. Useful when the manifest entry is keyed
        by display name (e.g. ``{"wheat farm": "wheat_farm.jpg"}``).
        """
        return self.get("buildings", building_name)

    def feature(self, feature_id: str) -> arcade.Texture | None:
        """v0.17: terrain features (forest, ore vein, etc.) live in
        their own subdirectory. Used by the renderer when drawing
        the feature overlay above the grass tile.

        v0.21: the canonical location for these textures moved from
        ``assets/textures/features/`` to ``assets/textures/terrain/``
        — they're part of the terrain layer (forest, mineral
        deposits, fertile soil all paint the ground), so the folder
        rename matches what they actually are. We try ``terrain/``
        first; ``features/`` is consulted as a fallback so any art
        already shipped at the old path keeps working until the
        modder moves it.

        v0.23.x: a third location, ``assets/textures/natural/``, was
        added for *primary-resource* placeholders specifically
        (forest, ore veins, fertile soil, groundwater) — distinct
        from the broader ``terrain/`` slot which also holds grass,
        water, hills, mountains. The loader now tries
        ``terrain/`` → ``natural/`` → ``features/`` in that order;
        the first hit wins. ``terrain/`` first means a modder who
        has explicitly customised a feature by dropping it into
        the canonical terrain folder still gets their art used;
        ``natural/`` second is where the placeholder generator
        writes (and where the player drops in their own primary-
        resource art); ``features/`` is the legacy back-compat.
        """
        tex = self.get("terrain", feature_id)
        if tex is not None:
            return tex
        tex = self.get("natural", feature_id)
        if tex is not None:
            return tex
        return self.get("features", feature_id)

    def house_tier(self, tier: int) -> arcade.Texture | None:
        return self.get("houses", f"tier{tier}")

    def walker(self, role: str) -> arcade.Texture | None:
        return self.get("walkers", role)

    def resource(self, resource_id: str) -> arcade.Texture | None:
        """v0.19.x: priced-resource icons.

        Lives under ``assets/textures/resources/<resource_id>.png``. Used
        by delivery-walker carry overlays and by the
        warehouse/granary content icons. The full set is generated by
        ``tools/generate_resource_textures.py`` from the
        ``bartering.STOCK_PRICES`` table; missing files fall through to
        the registry's standard miss-cache so an unprepared install
        just hides the overlay rather than crashing.
        """
        return self.get("resources", resource_id)

    def ui(self, name: str) -> arcade.Texture | None:
        """Load a UI asset (splash background, panel art, logo, …).

        Lives under ``assets/textures/ui/<name>.<ext>``. Falls through the
        same extension list as everything else — JPEG-friendly so big
        photographs don't bloat the repo with PNG."""
        return self.get("ui", name)

    # ── Diagnostics ───────────────────────────────────────────────────────
    def stats(self) -> tuple[int, int]:
        """``(attempts, hits)`` since startup. Useful for a one-line
        info-log telling the player how many sprites were found."""
        return self._load_attempts, self._load_hits

    def preload(self, items: list[tuple[str, str]]) -> None:
        """Eagerly load a batch — avoids the first-frame stutter when a
        previously-unseen building is first placed.

        ``items`` is a list of ``(category, name)`` pairs.
        """
        for cat, name in items:
            self.get(cat, name)
