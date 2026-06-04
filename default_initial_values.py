"""Default initial values for the map editor's "Init values" form. (v0.50)

When a scenario author opens the map editor's *Init values* form, the
fields are seeded from the template here: starting **gold** (treasury),
starting **population**, and an opening quantity for every **good** the
economy tracks — the ten nutrients the population eats/drinks plus the
warehouse-side raw/intermediate stocks.

Why a separate file (rather than reading ``balance.start_*`` directly)?
Same modding philosophy as ``bartering.STOCK_PRICES`` and
``constants.TRADE_ROUTES``: an author rebalancing the *starting budget*
a player is dropped into should be able to edit one small, plainly
named table without touching engine code (``economy.py``) or the
campaign-wide tuning numbers in ``balance.py``. The form reads this
table on open; saving the map serialises the resulting live economy
values via ``mapfile.collect_map_state`` exactly as before.

The numbers below mirror ``balance.py``'s ``start_*`` defaults at the
time of writing so a fresh map "feels like" a default new game until
the author edits it. Goods the engine seeds at zero (most luxury
nutrients) are listed explicitly at ``0`` so the form shows every good
as an editable row rather than hiding the ones that happen to start
empty — an author setting up a fishing village wants to see the
``fish`` row even though the default is 0.

Public API
----------

  DEFAULT_GOLD (int)
      Starting treasury in denarii.

  DEFAULT_POPULATION (int)
      Starting population (citizens).

  DEFAULT_NUTRIENTS (dict[str, int])
      ``{nutrient_id: opening_stock}`` for the ten nutrients in
      ``constants.NUTRIENTS`` order. Granary-side goods.

  DEFAULT_STOCKS (dict[str, int])
      ``{good_id: opening_stock}`` for the warehouse-side raw /
      intermediate goods in ``mapfile.WAREHOUSE_STOCKS`` order.

  build_default_initial_values() -> dict
      Convenience: returns a single dict the form consumes —
      ``{"gold": int, "population": int, "goods": {good_id: int, ...}}``
      with nutrients and stocks merged into one ordered ``goods`` map
      (nutrients first, then stocks), matching the order the form
      renders its rows. Built fresh on each call so the caller can
      mutate the result without disturbing the module tables.

Authoring notes
---------------

  * Editing a value here changes what the form *prefills*; it does not
    retroactively change maps already saved — those carry their own
    snapshot in the map JSON.
  * Unknown good ids are harmless: the form only renders rows for goods
    the economy knows (the union of NUTRIENTS and WAREHOUSE_STOCKS), so
    a stray key here is simply ignored. A *missing* key falls back to 0.
  * Keep values non-negative. The form clamps at 0 on edit, but seeding
    a negative here would just be clamped away on first interaction.
"""
from __future__ import annotations

from constants import NUTRIENTS
from mapfile import WAREHOUSE_STOCKS

# ── Treasury / population ─────────────────────────────────────────────────────
# Mirrors balance.start_treasury / balance.start_population so a fresh
# map matches a default new game until the author edits it.
DEFAULT_GOLD: int = 1000
DEFAULT_POPULATION: int = 100

# ── Nutrients (granary-side) ──────────────────────────────────────────────────
# Order follows constants.NUTRIENTS. Bread is the staple the default
# game seeds (balance.start_bread = 30); the rest start empty so the
# author opts into a luxury-rich start deliberately.
DEFAULT_NUTRIENTS: dict[str, int] = {
    "bread":      30,
    "vegetables": 0,
    "fruits":     0,
    "meat":       0,
    "fish":       0,
    "cheese":     0,
    "oil":        0,
    "honey":      0,
    "spice":      0,
    "wine":       0,
}

# ── Warehouse-side raw / intermediate goods ───────────────────────────────────
# Order follows mapfile.WAREHOUSE_STOCKS. Construction stock (stone,
# stone_blocks, wood, planks) and the early-chain buffers (wheat, flour,
# iron, tools) mirror balance.start_* so the player can build a few
# things before their own supply chain comes online.
DEFAULT_STOCKS: dict[str, int] = {
    "stone":        100,
    "stone_blocks": 50,
    "iron":         150,
    "wood":         300,
    "tools":        50,
    "planks":       20,
    "wheat":        50,
    "flour":        20,
    "weapons":      3,
    "olives":       0,
    "grapes":       0,
    "clay":         0,
    "pottery":      0,
    "livestock":    0,
    "horses":       0,
}


def build_default_initial_values() -> dict:
    """Return a fresh ``{"gold", "population", "goods"}`` dict for the
    form to seed from.

    ``goods`` merges nutrients (first, in NUTRIENTS order) then
    warehouse stocks (in WAREHOUSE_STOCKS order). Any good in those
    canonical lists that's missing from the tables above defaults to 0,
    so the form always has a complete, ordered set of rows even if a
    future engine version adds a good before this template catches up.

    Built fresh on every call — the caller owns the returned dict and
    can mutate it (the form does, as the author edits) without touching
    the module-level tables.
    """
    goods: dict[str, int] = {}
    for n in NUTRIENTS:
        goods[n] = int(DEFAULT_NUTRIENTS.get(n, 0))
    for g in WAREHOUSE_STOCKS:
        # Don't clobber a nutrient that (hypothetically) also appears in
        # the stocks list — nutrients win, matching the form's row order.
        if g not in goods:
            goods[g] = int(DEFAULT_STOCKS.get(g, 0))
    return {
        "gold":       int(DEFAULT_GOLD),
        "population": int(DEFAULT_POPULATION),
        "goods":      goods,
    }
