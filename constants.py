"""Game constants: grid, camera, colors, UI text, hotkeys.

Buildings are defined in `data/buildings.json` and loaded via BuildingRegistry.
Events are defined in `data/events.json` and loaded via EconomicEventManager.

Avoid adding gameplay numbers here — those live in `balance.py`.
"""
from pathlib import Path

import arcade

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
BUILDINGS_PATH = DATA_DIR / "buildings.json"
EVENTS_PATH = DATA_DIR / "events.json"
# v0.31: RPG request library — author-facing NPC dialogues that
# present the player with a multi-choice decision (Caesar messenger
# demands tribute, high priest asks for an Oracle, etc.). Edited via
# the splash menu's "RPG request editor"; consumed at runtime by the
# (forthcoming) request dispatcher which the trigger editor can wire
# in via a `fire_request` effect.
RPG_REQUESTS_PATH = DATA_DIR / "rpg_requests.json"
# v0.32: cinematic cutscene library — author-facing scripted slide
# decks ("Caesar conquers Gaul", "the barbarians arrive at the gates",
# etc.). Edited via the splash menu's "Cut scene cinematic editor";
# triggered at runtime by a trigger/request setting
# ``window.active_cutscene_id``. Each cutscene is a list of slides
# with an image + caption + body text; the player advances with
# "Next" and the last slide swaps to "OK" which closes the cutscene.
CUTSCENES_PATH = DATA_DIR / "cutscenes.json"
SAVES_DIR = ROOT_DIR / "saves"
# v0.16: scenario maps live in ./data/maps. The map editor writes
# JSON files here; the splash "Load map" button reads from here.
# Created on demand so a fresh checkout doesn't need an empty
# directory committed.
MAPS_DIR = DATA_DIR / "maps"

# v0.36: scripted-scenario bundles. Each subdirectory contains a
# self-contained scenario: ``map.json`` (the world + trigger wiring)
# plus the optional scenario-local libraries pointed at by the map's
# ``scenario_libraries`` block (``cutscenes.json`` /
# ``rpg_requests.json`` / ``events.json``). The splash "Load
# scenario" picker enumerates immediate child directories that
# contain a ``map.json``; see ``mapfile.list_scenarios``.
SCENARIOS_DIR = DATA_DIR / "scenarios"

# ── v0.16: Nutrients ──────────────────────────────────────────────────────────
# The ten goods the population eats / drinks. Every other resource
# (planks, stone, iron, tools, weapons, …) is *non-nutrient* and lives
# in warehouses; nutrients live in granaries. The split is enforced by
# `storage.distribute()`: granaries default-accept only this set, and
# warehouses default-reject it.
#
# Order matters — it's the order the HUD renders nutrient dots in the
# top bar and the order the granary inspector lists them. "bread" is
# first because it's the staple every house gets at tier 1; the rest
# climb in luxury (vegetables/fruit at tier 2, cheese/oil at tier 3,
# fish/meat/wine at tier 4, etc.). Spice and honey are condiments —
# small in volume, gating high-tier evolution.
#
# Naming note: the user-facing nutrient list spelled this "olive oil",
# but the internal id is "oil" (the existing olive_press output that
# domus already needs at tier 3). We keep the "oil" id for backwards
# save compatibility — every test, save file, and house-tier rule
# already says "oil". The HUD renders it as "Olive oil" via
# NUTRIENT_LABELS so the player sees the spec wording.
NUTRIENTS: tuple[str, ...] = (
    "bread", "vegetables", "fruits", "meat", "fish",
    "cheese", "oil", "honey", "spice", "wine",
)

# Reverse-friendly: a quick membership check that's cheaper than a
# tuple scan in tight loops (storage.distribute runs every tick).
NUTRIENTS_SET: frozenset[str] = frozenset(NUTRIENTS)

# v0.16: human-readable display labels for the nutrients. Used by the
# top-bar HUD and the granary inspector; the raw ids stay snake_case
# everywhere else (saveload, building.json, tests).
NUTRIENT_LABELS: dict[str, str] = {
    "bread":      "Bread",
    "vegetables": "Vegetables",
    "fruits":     "Fruits",
    "meat":       "Meat",
    "fish":       "Fish",
    "cheese":     "Cheese",
    "oil":        "Olive oil",
    "honey":      "Honey",
    "spice":      "Spice",
    "wine":       "Wine",
}

# ── Grid ──────────────────────────────────────────────────────────────────────
# GRID_COLS / GRID_ROWS are the *default* (new-game / non-editor) map
# size — the engine still works at this size without any editor input.
# The map editor can resize the live GameMap up to EDITOR_GRID_MAX per
# axis (v0.27: 128). Services / walkers / pathfinding allocate against
# the *live* GameMap.rows / GameMap.cols, so a 40×30 default game uses
# no more memory than before; only resized maps pay for the bigger
# grid.
GRID_COLS = 40
GRID_ROWS = 30
TILE_SIZE = 32

# v0.29: maximum per-axis size the editor will resize a map to. v0.27
# raised the ceiling to 128 (after refactoring services.py / walkers.py
# / pathfinding.py off of the module constants and onto game_map.rows
# / cols); v0.29 drops it back to 64 per playtester feedback — 128×128
# maps were technically supported but pathological for the A* walker
# routing budget on integrated GPUs, and 64×64 covers every authored
# scenario shipping with the game with comfortable headroom. The
# refactor that made larger-than-default safe stays — only the
# enforcement constant moved. The minimum (EDITOR_GRID_MIN, in
# game_window) stays at 8.
EDITOR_GRID_MAX = 64

# Terrain ids
TERRAIN_GRASS = 0
TERRAIN_GRASS_ALT = 1
TERRAIN_WATER = 2
# v0.15: hills and mountains are non-buildable terrain types added
# for visual variety and gameplay nuance. Hills are passable
# (walkers cross them) but no building can be placed on them
# except, conceptually, an extractor — for now, they're decorative
# and unbuildable, the same as water for non-port buildings.
# Mountains are fully impassable (walkers route around them) and
# completely unbuildable. Both render distinctly so the player
# can read elevation at a glance.
TERRAIN_HILLS = 3
TERRAIN_MOUNTAINS = 4
# v0.18: desert is non-buildable (like mountains) but passable for
# walkers (like hills). Visually rendered as a sandy yellow-brown
# tile. Added so map authors can carve out arid regions in the
# editor without falling back to "water" for every non-buildable
# patch — water blocks walker traversal whereas desert allows it.
TERRAIN_DESERT = 5

# v0.13: terrain *features* are a separate layer on top of the base
# terrain. None for an empty cell; a string id for a natural resource
# spot. Buildings may require a specific feature in their footprint —
# a lumber mill must sit on/adjacent to forest, a quarry on a stone
# deposit, etc. Features are visible to the player as small overlays
# rendered above the grass tile, and the editor (v0.14) will let
# scenario authors paint them.
FEATURE_FOREST = "forest"
FEATURE_GOLD_VEIN = "gold_vein"
FEATURE_COPPER_VEIN = "copper_vein"
# v0.18: iron_vein added — a separate feature for iron extraction.
# Until v0.17 mines tapped gold/copper veins generically; the
# player asked for explicit iron deposits the editor can paint,
# matching the goods set the economy already tracks (iron is a
# first-class resource consumed by the factory and weapons-smith).
FEATURE_IRON_VEIN = "iron_vein"
FEATURE_STONE_DEPOSIT = "stone_deposit"
FEATURE_FERTILE_SOIL = "fertile_soil"
FEATURE_GROUNDWATER = "groundwater"
# v0.25: fish are a depletable water feature. Without a fish feature
# on the tile, water is just water — a fishery placed there produces
# nothing. Each fishy tile carries a finite stock (35000 fish in
# balance defaults); when the stock hits zero the feature is cleared
# and the texture removed so the tile reverts to plain water. Same
# model as forest/gold_vein but on water instead of grass.
FEATURE_FISH = "fish"
# v0.26: clay is now a natural-resource feature. The Clay Pit
# requires a clay_deposit tile in its footprint, mirroring the
# iron/copper/gold mine and quarry pattern. Previously clay was
# produced without a map prerequisite; v0.26 makes it consistent
# with the other extractive industries (mining tab).
FEATURE_CLAY_DEPOSIT = "clay_deposit"

# Display colours for terrain features (rendered as small dots/marks
# above the grass texture). The base palette stays grass-coloured;
# features are an additive overlay so the player can read both layers.
FEATURE_COLORS: dict[str, tuple[int, int, int]] = {
    FEATURE_FOREST:        (40, 100, 50),     # dark green
    FEATURE_GOLD_VEIN:     (220, 180, 60),    # gold
    FEATURE_COPPER_VEIN:   (180, 100, 60),    # copper
    FEATURE_IRON_VEIN:     (110, 110, 130),   # iron grey-blue
    FEATURE_STONE_DEPOSIT: (140, 140, 140),   # gray
    FEATURE_FERTILE_SOIL:  (140, 100, 60),    # rich brown
    FEATURE_GROUNDWATER:   (100, 150, 200),   # pale blue
    FEATURE_FISH:          (200, 220, 240),   # pale blue-white (silvery)
    FEATURE_CLAY_DEPOSIT:  (175, 120, 90),    # clay-orange-brown
}

# v0.14: human-readable labels for terrain features. Used by the hover
# tooltip — the raw feature ids are snake_case strings ("gold_vein"),
# the labels are what the player should see ("Gold vein").
FEATURE_LABELS: dict[str, str] = {
    FEATURE_FOREST:        "Forest",
    FEATURE_GOLD_VEIN:     "Gold vein",
    FEATURE_COPPER_VEIN:   "Copper vein",
    FEATURE_IRON_VEIN:     "Iron vein",
    FEATURE_STONE_DEPOSIT: "Stone deposit",
    FEATURE_FERTILE_SOIL:  "Fertile soil",
    FEATURE_GROUNDWATER:   "Groundwater",
    FEATURE_FISH:          "Fish",
    FEATURE_CLAY_DEPOSIT:  "Clay deposit",
}

# ── Simulation ────────────────────────────────────────────────────────────────
TICKS_PER_SECOND = 2
EVENT_MIN_INTERVAL = 30
EVENT_MAX_INTERVAL = 80

# ── Camera ────────────────────────────────────────────────────────────────────
CAMERA_PAN_SPEED = 400          # pixels / second
CAMERA_ZOOM_MIN = 0.25
CAMERA_ZOOM_MAX = 3.0
CAMERA_ZOOM_STEP = 0.1

# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_GRASS      = (86, 125, 70)
COLOR_GRASS_ALT  = (78, 118, 64)
COLOR_WATER      = (64, 100, 170)
# v0.15: hills are mid-green-brown — they read as "rolling" not flat.
# Mountains are darker grey-brown with a steeper feel; they're
# unbuildable and walkers route around them.
COLOR_HILLS      = (110, 120, 75)
COLOR_MOUNTAINS  = (95, 85, 75)
# v0.18: desert is sandy yellow-brown — buildings are rejected, but
# walkers cross it normally (unlike water/mountains).
COLOR_DESERT     = (200, 175, 110)
COLOR_ROAD       = (160, 140, 110)
COLOR_GRID_LINE  = (60, 90, 50, 80)
COLOR_UI_BG      = (30, 25, 20)
COLOR_UI_PANEL   = (50, 42, 35)
COLOR_UI_BORDER  = (120, 100, 70)
COLOR_GOLD       = (220, 190, 80)
COLOR_RED        = (200, 60, 60)
COLOR_GREEN      = (60, 180, 80)
COLOR_WHITE      = (230, 230, 220)
COLOR_GRAY       = (160, 155, 145)
COLOR_HIGHLIGHT  = (255, 255, 100, 80)
COLOR_INVALID    = (255, 50, 50, 80)

# ── Building palette / hotkeys ────────────────────────────────────────────────
# Categories displayed in the tabbed bottom bar. Order matters (tab order).
#
# v0.26: three new tabs.
#   • Mining — iron/copper/gold mines, their dedicated smelters, and
#     clay_pit. Pulled out of industry so all map-feature-extractive
#     industries live together. Industry now hosts only the
#     secondary processors with no map prerequisite.
#   • Politic — Senate (moved from religion) plus Forum, Consulate,
#     Embassy, Caesar Hostel. Religion is now just temples.
#   • Bank — Local / Business / Private banks. Pure money producers,
#     distinct from commerce (which is goods-distribution).
PALETTE_CATEGORIES: list[tuple[str, str]] = [
    ("infra", "Infra"),
    ("housing", "Housing"),
    ("water", "Water"),
    # v0.19.x: agriculture is its own tab — wheat farms, vineyards,
    # olive farms, vegetable farms, orchards, pasturage, spice
    # fields, beekeepers — split out of the industry tab so the
    # player can see all primary food/raw-material producers in
    # one place. Industry now hosts the secondary processors
    # (windmill, bakery, sawmill, presses, mines, etc.) only.
    ("agriculture", "Agri"),
    ("mining", "Mining"),
    ("industry", "Industry"),
    ("commerce", "Commerce"),
    ("bank", "Bank"),
    ("entertainment", "Fun"),
    ("religion", "Religion"),
    ("politic", "Politic"),
    ("education", "Edu"),
    ("health", "Health"),
    ("security", "Civic"),
    ("military", "Mil"),
]

# Legacy flat palette (kept for save-format/back-compat with any tests).
BUILDING_PALETTE: list[str] = [
    "road", "house", "farm", "lumber_mill", "mine",
    "bakery", "market", "factory", "tavern", "theatre",
    "temple", "warehouse", "port",
]


def build_hotkey_map() -> dict[int, str]:
    """Return the keyboard hotkey → building id map.

    Built lazily so this module can be imported without arcade being
    initialised (e.g. for headless tests).

    Hotkeys 1-9 + 0 select the first 10 buildings of the *currently
    visible category tab*; this map is the legacy flat-palette mapping
    used as a fallback before tabs are activated.
    """
    return {
        arcade.key.KEY_1: "road",
        arcade.key.KEY_2: "house",
        arcade.key.KEY_3: "farm",
        arcade.key.KEY_4: "lumber_mill",
        arcade.key.KEY_5: "mine",
        arcade.key.KEY_6: "bakery",
        arcade.key.KEY_7: "market",
        arcade.key.KEY_8: "factory",
        arcade.key.KEY_9: "tavern",
        arcade.key.KEY_0: "theatre",
    }


# ── Resource icon colours (top bar) ──────────────────────────────────────────
RESOURCE_COLORS: dict[str, tuple[int, int, int]] = {
    "food":  (220, 200, 80),
    "wood":  (140, 100, 60),
    "iron":  (170, 170, 180),
    "tools": (200, 150, 100),
    "money": (220, 190, 80),
}


# ── Tax ───────────────────────────────────────────────────────────────────────
TAX_RATES = [0.05, 0.10, 0.15, 0.20, 0.25]

# ── Trade routes ──────────────────────────────────────────────────────────────
TRADE_ROUTES = [
    {"name": "Rome → Greece",   "goods": "food",  "rate": 50, "profit": 8},
    {"name": "Rome → Egypt",    "goods": "iron",  "rate": 30, "profit": 12},
    {"name": "Rome → Gaul",     "goods": "wood",  "rate": 40, "profit": 6},
    {"name": "Rome → Carthage", "goods": "tools", "rate": 20, "profit": 15},
]

# ── v0.50: Commercial roads / trade cities ─────────────────────────────────────
# The seven foreign cities the player can link to via the in-game
# "Commercial roads" window (hotkey 'R'). Linking a city establishes a
# commercial road; the player can then attach persistent auto-trade
# routes (sell a stockpile good each tick for gold) to a linked city.
# Each route is a TradeRoute (see trade.py) running every tick through
# the existing TradeRouteManager.
#
# The per-city ``profit_mult`` scales the base per-unit profit so the
# cities feel distinct: nearer / safer markets (Roma, Massilia) pay a
# touch less; the far/exotic ones (Alexandria, Istanbul) pay more for
# the longer haul. ``link_cost`` is the one-off treasury cost to open
# the commercial road. Spelling follows the brief exactly.
# v0.51: each city also carries a ``distance`` (abstract sea-leagues).
# It scales the per-TRIP round-trip interval of the voyage layer
# (voyages.py): a near city like Roma turns a ship around fast, a far
# one like Alexandria ties a ship up for many ticks. Distance and
# ``profit_mult`` move together (farther → richer but slower) but are
# kept as separate knobs so a modder can make a city far-but-poor or
# near-but-rich without coupling the two.
TRADE_CITIES: list[dict] = [
    {"id": "massilia",   "name": "Massilia",   "link_cost": 200,  "profit_mult": 1.0,  "distance": 4},
    {"id": "roma",       "name": "Roma",       "link_cost": 150,  "profit_mult": 0.9,  "distance": 2},
    {"id": "carthage",   "name": "Carthage",   "link_cost": 300,  "profit_mult": 1.1,  "distance": 5},
    {"id": "athina",     "name": "Athina",     "link_cost": 350,  "profit_mult": 1.15, "distance": 6},
    {"id": "istanbul",   "name": "Istanbul",   "link_cost": 450,  "profit_mult": 1.25, "distance": 8},
    {"id": "tunis",      "name": "Tunis",      "link_cost": 300,  "profit_mult": 1.1,  "distance": 5},
    {"id": "alexandria", "name": "Alexandria", "link_cost": 500,  "profit_mult": 1.3,  "distance": 9},
]

# Default per-tick quantity a new city trade route ships, and the base
# profit-per-unit before the city multiplier and good price are applied.
# A route's profit is computed as
#   max(1, round(STOCK_PRICES[good] * TRADE_CITY_PROFIT_FRACTION
#                * city.profit_mult))
# so expensive goods earn more per unit and exotic cities pay a premium.
TRADE_CITY_DEFAULT_RATE: int = 10
TRADE_CITY_PROFIT_FRACTION: float = 0.6

# ── v0.51: per-trip voyage layer (voyages.py) ──────────────────────────────
# The voyage layer models inter-city commerce as discrete TRIPS rather
# than the per-tick drip of TRADE_CITY_DEFAULT_RATE. A free commercial
# ship loads goods at the commercial port, sails to a linked city, sells
# the cargo for gold, and sails back — the exchange lands once per
# completed round trip, not every tick. These knobs tune that layer.
#
# A trip only departs when ALL of these hold (see voyages.py):
#   * a free commercial ship is available (harbor berths - ships at sea)
#   * the good is in stock at the commercial port (>= 1 unit)
#   * the destination city is not at war (no_war_at_destination)
#   * the sea road is clear of pirates (no_piracy_on_route)
#
# VOYAGE_SHIP_CAPACITY — the per-trip cargo cap of one hull: a ship
# carries at most this many total units of goods per trip (the brief's
# "usually 1000 total unit of goods").
VOYAGE_SHIP_CAPACITY: int = 1000

# The round-trip interval (in ticks) for a city is
#   VOYAGE_INTERVAL_BASE + VOYAGE_INTERVAL_PER_DISTANCE * city.distance
# so the nearest city (Roma, distance 2) turns a ship in ~18 ticks and
# the farthest (Alexandria, distance 9) in ~46 ticks. The interval is
# the *cooldown* between a ship leaving and the same berth being free to
# dispatch again — i.e. the modelled there-and-back sailing time.
VOYAGE_INTERVAL_BASE: int = 10
VOYAGE_INTERVAL_PER_DISTANCE: int = 4

# Per-unit gold the foreign market pays for a voyage's cargo. Reuses the
# same STOCK_PRICES table the barter / gold-trade / route panels read,
# scaled by this fraction and the city's profit_mult — so an expensive
# good shipped to a far city earns the most. Distinct constant from
# TRADE_CITY_PROFIT_FRACTION so the per-trip economy can be tuned
# independently of the legacy per-tick routes.
VOYAGE_PROFIT_FRACTION: float = 0.9


# ── Help text ─────────────────────────────────────────────────────────────────
HELP_TEXT = """CONTROLS
─────────────────────────────
Left Click      Place / inspect building
Middle Click    Show extractable reserves bubble (mine/farm/lumber/fishery)
                Also works on raw natural-resource tiles (scout)
Right Click     Demolish (50% refund)
1-9, 0          Select building (hotkey)
TAB             Cycle palette category
Arrows / WASD   Pan camera
Scroll Wheel    Zoom in / out
+  /  -         Speed up / slow down
SPACE           Pause / Resume
T               Cycle tax rate
R               Open commercial roads window (Ctrl+R: tick recorder)
B               Open international bartering menu (good for good)
C               Open international trade menu (good for gold)
N               Open nutrients coverage panel
$               Open finance budget panel (income / upkeep / balance)
!               Open commerce-ships panel (busy at sea / free to dispatch)
Y               Yield to Caesar's demand
S               Toggle city statistics panel
J               Toggle jobs / employment panel
Z               Toggle happiness equation debug overlay
F5              Quick-save
F9              Quick-load
M               Toggle mini-map
O               Cycle overlay (water/food/...)
G               Toggle graphs window (Pop / Treasury / Food / Wood / Iron /
                Happiness / Fed % / Food net / Employed % / Stone /
                Income / Expense / Net / Tax %)
H               Toggle this help
ESC             Open menu (save/quit/editors)"""
