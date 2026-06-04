# Textures

Drop image files in here to skin the game. Anything missing falls back
to the colored-rectangle rendering — you can ship one PNG at a time.

## Folder layout

```
assets/textures/
├── buildings/   # one image per building (farm, bakery, vegetable_farm, …)
├── terrain/     # grass, grass_alt, water, hills, mountains, AND feature
│                # overlays (forest, gold_vein, copper_vein, iron_vein,
│                # stone_deposit, fertile_soil, groundwater) — v0.21
│                # consolidated these here since they all paint the ground
├── features/    # legacy location for feature overlays — kept as a
│                # fallback so old art still loads. New art goes in terrain/
├── walkers/     # worker, trader, citizen, soldier, cavalry, rebel, enemy, delivery
├── houses/      # tier0..tier4 (shack → villa) — overrides the building image
├── resources/   # one image per priced resource (wheat, bread, oil, …) — drives v0.19.x
│                # delivery-walker carry overlays and warehouse/granary content icons
└── ui/          # splash, panel art, logo, …
```

## Supported formats

`.png`, `.jpg`, `.jpeg`, `.webp`. PNG is preferred for tile sprites
(real alpha channel for irregular footprints); JPEG is fine for
backgrounds where transparency isn't needed.

## The manifest — `data/textures.json`

The manifest is the **single source of truth for "which file goes
where"**. Each entry maps a key to a filename:

```json
{
  "buildings": {
    "farm":         "farm.jpg",
    "wheat farm":   "wheat_farm.jpg",
    "bakery":       "bakery.png",
    "vegetable_farm": "veg_grower.jpg"
  },
  "terrain": {
    "grass":    "grass.jpg",
    "water":    "water.jpg"
  },
  "walkers": {
    "worker":   "worker.png"
  }
}
```

Both the **internal id** (`farm`) and the **human-readable name**
(`wheat farm`) are accepted as keys. The lookup is case-insensitive.

If a key has no manifest entry, the registry falls back to looking for
`<id>.<ext>` for each supported extension. So you can either:

1. **List every file in the manifest** for a clean inventory, or
2. **Skip the manifest entirely** and just name your file
   `<building_id>.png` — the registry will find it.

The two approaches mix freely: list custom names where you want to,
let the rest auto-resolve.

## Sprite sizing

- 1×1 buildings: 32×32 px (matches `TILE_SIZE`).
- 2×2 buildings (farm, granary): 64×64 px.
- 3×3 buildings (senate, fort): 96×96 px.

The renderer scales the source to the building's footprint anyway, so
exact dimensions aren't critical — but matching tile size avoids
filtering blur.

## Adding a new building texture

1. Drop the image file (e.g. `vegetable_farm.jpg`) into
   `assets/textures/buildings/`.
2. Either:
   - Name the file `<building_id>.<ext>` (auto-resolved), **or**
   - Add an entry to `data/textures.json` under `"buildings"`.
3. Restart the game. Textures are loaded lazily on first use.

## Walker textures

The walker layer also renders coloured circles by default. Drop in
`worker.png`, `trader.png`, `citizen.png`, `soldier.png`, `cavalry.png`,
`rebel.png`, `enemy.png`, `delivery.png` to skin them. Each should be
16–24 px roughly, with transparent background.
