"""Generate flows.pdf — visual documentation of the v0.13 economy.

Four pages:
  1. Food chain
  2. Lumber + heavy industry chain
  3. Luxury chains (oil, wine, pottery)
  4. Building activity status states

Each page is a matplotlib figure with hand-positioned boxes and arrows
to keep the topology readable. We don't auto-layout because the chain
has known structure and a layout algorithm produces messier results
than placing nodes on a deliberate grid.
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch, Rectangle
from matplotlib.backends.backend_pdf import PdfPages

OUTPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("flows.pdf")

# Color palette — matches the v0.12 visualizer diagrams.
C_BUILDING_EXTRACT = "#1D9E75"   # teal
C_BUILDING_REFINE  = "#7F77DD"   # purple
C_BUILDING_CONSUME = "#D85A30"   # coral
C_RESOURCE         = "#B4B2A9"   # gray
C_TEXT             = "#2C2C2A"
C_ARROW            = "#5F5E5A"
C_TEXT_ON_FILL_DARK = "#F1EFE8"


def _draw_box(ax, x, y, w, h, label, sublabel="", color=C_BUILDING_EXTRACT, dark_text=True):
    """Draw a labelled box at (x, y) with width w, height h."""
    rect = Rectangle((x, y), w, h, facecolor=color, edgecolor="black",
                     linewidth=0.7, joinstyle="round")
    ax.add_patch(rect)
    text_color = C_TEXT if dark_text else C_TEXT_ON_FILL_DARK
    ax.text(x + w / 2, y + h / 2 + (0.07 if sublabel else 0),
            label, ha="center", va="center",
            fontsize=10, fontweight="bold", color=text_color)
    if sublabel:
        ax.text(x + w / 2, y + h / 2 - 0.13, sublabel,
                ha="center", va="center", fontsize=8, color=text_color)


def _draw_resource(ax, x, y, w, h, label):
    """Draw a smaller, gray resource box."""
    rect = Rectangle((x, y), w, h, facecolor=C_RESOURCE, edgecolor="black",
                     linewidth=0.5)
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h / 2, label, ha="center", va="center",
            fontsize=9, color=C_TEXT)


def _arrow(ax, x1, y1, x2, y2, label="", style="-|>", dashed=False):
    """Draw an arrow from (x1, y1) to (x2, y2) with an optional label."""
    linestyle = "--" if dashed else "-"
    ar = FancyArrowPatch(
        (x1, y1), (x2, y2),
        arrowstyle=style, color=C_ARROW, linewidth=1.0,
        mutation_scale=10, linestyle=linestyle,
    )
    ax.add_patch(ar)
    if label:
        ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 0.05, label,
                ha="center", va="bottom", fontsize=8, color=C_ARROW,
                bbox=dict(facecolor="white", edgecolor="none", pad=0.5))


def page_food_chain(pdf):
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 9)
    ax.set_aspect("equal")
    ax.axis("off")

    fig.suptitle("Food chain — extraction → refining → consumption",
                 fontsize=14, fontweight="bold", y=0.96)

    # Tier labels (small, in the margin)
    ax.text(0.5, 7.5, "Extract", fontsize=10, fontweight="bold", color=C_TEXT)
    ax.text(4.5, 7.5, "Refine", fontsize=10, fontweight="bold", color=C_TEXT)
    ax.text(8.5, 7.5, "Consume", fontsize=10, fontweight="bold", color=C_TEXT)

    # Farm
    _draw_box(ax, 0.3, 5.8, 2.5, 1.2, "Farm",
              "6 workers · fertile soil +50%", C_BUILDING_EXTRACT)

    # Resources from farm
    _draw_resource(ax, 0.3, 4.2, 1.1, 0.7, "wheat")
    _draw_resource(ax, 1.7, 4.2, 1.1, 0.7, "food")
    _arrow(ax, 1.0, 5.8, 0.85, 4.9, "+12")
    _arrow(ax, 2.0, 5.8, 2.25, 4.9, "+8")

    # Windmill
    _draw_box(ax, 4.0, 5.8, 2.5, 1.2, "Windmill",
              "3 workers · wheat → flour", C_BUILDING_REFINE, dark_text=False)
    _arrow(ax, 1.4, 4.55, 4.0, 6.2, "−4")

    # Flour resource
    _draw_resource(ax, 4.5, 4.2, 1.5, 0.7, "flour")
    _arrow(ax, 5.25, 5.8, 5.25, 4.9, "+7")

    # Bakery
    _draw_box(ax, 4.0, 2.5, 2.5, 1.2, "Bakery",
              "3 workers · flour + wood → bread", C_BUILDING_REFINE,
              dark_text=False)
    _arrow(ax, 5.25, 4.2, 5.25, 3.7, "−2")

    # Wood input from elsewhere
    _draw_resource(ax, 7.0, 5.0, 1.4, 0.7, "wood")
    ax.text(7.7, 4.85, "(oven fuel)", ha="center", fontsize=7,
            color=C_TEXT, style="italic")
    _arrow(ax, 7.0, 5.3, 6.5, 3.4, "−1")

    # Food (output of bakery)
    _draw_resource(ax, 4.5, 0.8, 2.0, 0.7, "food (bread)")
    _arrow(ax, 5.25, 2.5, 5.25, 1.5, "+10")

    # Consumers
    _draw_box(ax, 8.5, 5.5, 2.5, 1.0, "House",
              "−2 / 10 residents", C_BUILDING_CONSUME)
    _draw_box(ax, 8.5, 3.7, 2.5, 1.0, "Market",
              "−3 / tick", C_BUILDING_CONSUME)
    _draw_box(ax, 8.5, 1.9, 2.5, 1.0, "Tavern",
              "−2 / tick", C_BUILDING_CONSUME)

    _arrow(ax, 6.5, 1.15, 8.4, 5.5)
    _arrow(ax, 6.5, 1.15, 8.4, 4.0)
    _arrow(ax, 6.5, 1.15, 8.4, 2.2)

    # Footer note
    ax.text(0.3, 0.3,
            "Numbers on arrows = units / tick at full activity. "
            "1 farm + 1 windmill + 1 bakery feeds ~90 citizens "
            "(8 from farm + 10 from bakery, ÷ 0.2 / citizen).",
            fontsize=9, color=C_TEXT, wrap=True)

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def page_lumber_chain(pdf):
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 9)
    ax.set_aspect("equal")
    ax.axis("off")

    fig.suptitle("Lumber + heavy industry — extractors, refiners, end uses",
                 fontsize=14, fontweight="bold", y=0.96)

    ax.text(0.5, 8.0, "Extract", fontsize=10, fontweight="bold", color=C_TEXT)
    ax.text(5.5, 8.0, "Refine / industry", fontsize=10, fontweight="bold", color=C_TEXT)
    ax.text(9.5, 8.0, "Goods", fontsize=10, fontweight="bold", color=C_TEXT)

    # Lumber mill (forest required)
    _draw_box(ax, 0.3, 6.7, 2.4, 0.9, "Lumber mill",
              "4 workers · forest", C_BUILDING_EXTRACT)
    # Mine
    _draw_box(ax, 0.3, 5.2, 2.4, 0.9, "Iron mine",
              "8 workers · gold/copper", C_BUILDING_EXTRACT)
    # Quarry
    _draw_box(ax, 0.3, 3.7, 2.4, 0.9, "Quarry",
              "6 workers · stone deposit", C_BUILDING_EXTRACT)

    # Resources column
    _draw_resource(ax, 3.2, 6.85, 1.4, 0.6, "wood")
    _draw_resource(ax, 3.2, 5.35, 1.4, 0.6, "iron")
    _draw_resource(ax, 3.2, 3.85, 1.4, 0.6, "stone")

    _arrow(ax, 2.7, 7.15, 3.2, 7.15, "+8")
    _arrow(ax, 2.7, 5.65, 3.2, 5.65, "+6")
    _arrow(ax, 2.7, 4.15, 3.2, 4.15, "+5")

    # mine consumes wood (dashed, internal feedback)
    _arrow(ax, 3.5, 6.85, 1.5, 6.1, "−2", dashed=True)

    # Refiners
    _draw_box(ax, 5.3, 6.7, 2.4, 0.9, "Sawmill",
              "5 workers · wood → planks", C_BUILDING_REFINE,
              dark_text=False)
    _draw_box(ax, 5.3, 5.2, 2.4, 0.9, "Factory",
              "10 workers · iron+wood→tools", C_BUILDING_REFINE,
              dark_text=False)
    _draw_box(ax, 5.3, 3.7, 2.4, 0.9, "Stonemason",
              "5 workers · stone → blocks", C_BUILDING_REFINE,
              dark_text=False)
    _draw_box(ax, 5.3, 2.2, 2.4, 0.9, "Weapon smith",
              "6 workers · iron+wood→arms", C_BUILDING_REFINE,
              dark_text=False)

    _arrow(ax, 4.6, 7.15, 5.3, 7.15, "−3")
    _arrow(ax, 4.6, 5.65, 5.3, 5.65, "−3")
    _arrow(ax, 4.6, 5.5, 5.4, 2.3, "−3")
    _arrow(ax, 4.6, 4.15, 5.3, 4.15, "−2")
    _arrow(ax, 4.6, 7.0, 5.4, 5.5, "−2")
    _arrow(ax, 4.6, 7.0, 5.4, 2.4, "−1")

    # Output goods
    _draw_resource(ax, 8.2, 6.85, 1.4, 0.6, "planks")
    _draw_resource(ax, 8.2, 5.35, 1.4, 0.6, "tools")
    _draw_resource(ax, 8.2, 3.85, 1.4, 0.6, "stone_blocks")
    _draw_resource(ax, 8.2, 2.35, 1.4, 0.6, "weapons")

    _arrow(ax, 7.7, 7.15, 8.2, 7.15, "+5")
    _arrow(ax, 7.7, 5.65, 8.2, 5.65, "+5")
    _arrow(ax, 7.7, 4.15, 8.2, 4.15, "+4")
    _arrow(ax, 7.7, 2.65, 8.2, 2.65, "+4")

    # Consumers
    ax.text(0.5, 1.6, "Construction & end uses", fontsize=10,
            fontweight="bold", color=C_TEXT)
    _draw_box(ax, 0.3, 0.5, 2.0, 0.9, "Tier 4 villa", "needs planks", C_BUILDING_CONSUME)
    _draw_box(ax, 2.5, 0.5, 2.0, 0.9, "Senate / temple", "needs blocks+planks", C_BUILDING_CONSUME)
    _draw_box(ax, 4.7, 0.5, 2.0, 0.9, "Bakery", "−1 wood (fuel)", C_BUILDING_CONSUME)
    _draw_box(ax, 6.9, 0.5, 1.8, 0.9, "Port", "−3 wood / tick", C_BUILDING_CONSUME)
    _draw_box(ax, 8.9, 0.5, 1.8, 0.9, "Barracks", "−1 weapons", C_BUILDING_CONSUME)

    _arrow(ax, 8.9, 7.0, 1.3, 1.4)
    _arrow(ax, 9.5, 4.0, 3.5, 1.4)
    _arrow(ax, 8.7, 7.0, 3.5, 1.4)
    _arrow(ax, 3.9, 7.0, 5.7, 1.4, dashed=True)
    _arrow(ax, 3.9, 7.0, 7.8, 1.4, dashed=True)
    _arrow(ax, 9.6, 2.5, 9.8, 1.4)

    ax.text(0.3, 0.05,
            "Solid arrow = chain output flow. Dashed = legacy raw-wood "
            "consumer (factory/weapon-smith/bakery still draw wood directly; "
            "sawmill is the new preferred consumer).",
            fontsize=8, color=C_TEXT)

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def page_luxury_chains(pdf):
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 9)
    ax.set_aspect("equal")
    ax.axis("off")

    fig.suptitle("Luxury chains — olives, wine, pottery",
                 fontsize=14, fontweight="bold", y=0.96)

    rows = [("Olive farm", "olive_farm", "olives", "12", "Olive press",
             "olives → oil", "4", "oil", "8", "Tier 3 domus"),
            ("Vineyard", "vineyard", "grapes", "12", "Wine press",
             "grapes → wine", "4", "wine", "8", "Tavern −1 / tick"),
            ("Clay pit", "clay_pit", "clay", "10", "Pottery shop",
             "clay → pottery", "3", "pottery", "6", "Tier 4 villa")]

    y_top = 6.5
    row_h = 1.6
    for i, (extract_name, _ex_id, raw_name, raw_amt, refine_name,
            refine_sub, refine_consume, good_name, good_amt, gate) in enumerate(rows):
        y = y_top - i * row_h

        _draw_box(ax, 0.3, y, 2.0, 0.9, extract_name,
                  "+" + raw_amt + " / tick", C_BUILDING_EXTRACT)
        _draw_resource(ax, 2.7, y + 0.15, 1.2, 0.6, raw_name)
        _arrow(ax, 2.3, y + 0.45, 2.7, y + 0.45, "+" + raw_amt)

        _draw_box(ax, 4.3, y, 2.4, 0.9, refine_name,
                  refine_sub, C_BUILDING_REFINE, dark_text=False)
        _arrow(ax, 3.9, y + 0.45, 4.3, y + 0.45, "−" + refine_consume)

        _draw_resource(ax, 7.1, y + 0.15, 1.2, 0.6, good_name)
        _arrow(ax, 6.7, y + 0.45, 7.1, y + 0.45, "+" + good_amt)

        _draw_box(ax, 8.7, y, 2.5, 0.9, gate, "", C_BUILDING_CONSUME)
        _arrow(ax, 8.3, y + 0.45, 8.7, y + 0.45)

    # Cross-link annotation: villa also needs oil
    ax.text(7.5, 1.5, "(villa also needs oil)", fontsize=8,
            color=C_TEXT, style="italic")

    ax.text(0.3, 0.7,
            "House tier gating: tier 3 domus needs oil; tier 4 villa needs "
            "oil + pottery + planks. Wine is non-gating but the tavern stops "
            "producing happiness without it (v0.12 starvation rule).",
            fontsize=9, color=C_TEXT, wrap=True)
    ax.text(0.3, 0.15,
            "Tier 3 ratio: 1 olive farm → 3 olive presses (12 ÷ 4); "
            "1 olive press serves enough oil for ~8 villa-blocks via one "
            "market walker.",
            fontsize=9, color=C_TEXT, wrap=True)

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def page_status_states(pdf):
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 9)
    ax.set_aspect("equal")
    ax.axis("off")

    fig.suptitle("Per-building activity status (v0.12 + v0.13)",
                 fontsize=14, fontweight="bold", y=0.96)
    ax.text(0.3, 7.9,
            "Each building is classified once per tick. Throttle multiplies "
            "production AND consumption.",
            fontsize=10, color=C_TEXT)

    # Central decision node
    _draw_box(ax, 4.5, 6.5, 3.0, 0.9, "classify()", "one per tick", "#888780")

    # 6 outcome boxes laid out in two rows
    outcomes = [
        ("disconnected", "no road link", "#A32D2D", 0.5, 4.5),
        ("unstaffed", "0 / N workers", "#A32D2D", 4.5, 4.5),
        ("starved", "an input is 0", "#A32D2D", 8.5, 4.5),
        ("partial", "workers or input < 100%", "#BA7517", 0.5, 2.5),
        ("active", "all gates pass", "#3B6D11", 4.5, 2.5),
        ("idle", "no inputs/outputs", "#5F5E5A", 8.5, 2.5),
    ]
    for state, sub, color, x, y in outcomes:
        _draw_box(ax, x, y, 3.0, 1.4, state, sub, color, dark_text=False)
        _arrow(ax, 6.0, 6.5, x + 1.5, y + 1.4)

    # Footer formula
    ax.text(0.3, 1.5,
            "throttle = min(workers_filled / workers_needed, "
            "min over inputs of supply_ratio)",
            fontsize=10, fontweight="bold", color=C_TEXT)
    ax.text(0.3, 1.1,
            "v0.12 added input-starvation gating: a tavern with no wine "
            "produces zero happiness AND drinks no food. v0.13 adds wage "
            "gating on top:",
            fontsize=9, color=C_TEXT, wrap=True)
    ax.text(0.3, 0.65,
            "if last tick's wage bill couldn't be paid, the global worker "
            "pool is reduced proportionally — buildings cascade through "
            "partial → unstaffed",
            fontsize=9, color=C_TEXT, wrap=True)
    ax.text(0.3, 0.25,
            "until the player raises taxes or demolishes idle staff. "
            "Wage rate: 0.2 dn / worker / tick (balance.wage_per_worker).",
            fontsize=9, color=C_TEXT, wrap=True)

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def page_summary(pdf):
    """Front page summarizing what's in the rest of the document."""
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 9)
    ax.axis("off")

    fig.suptitle("Caesar III clone — Economy flows",
                 fontsize=18, fontweight="bold", y=0.94)
    ax.text(6.0, 7.9, "v0.13 — supply chains, terrain features, wages",
            ha="center", fontsize=12, color=C_TEXT, style="italic")

    sections = [
        ("Page 2 — Food chain",
         "farm → wheat → windmill → flour → bakery → food. Plus subsistence "
         "food directly from the farm. House / market / tavern consume."),
        ("Page 3 — Lumber + heavy industry",
         "lumber mill → wood → sawmill → planks (for villas + civic). "
         "Quarry → stone → stonemason → stone_blocks. Mine → iron → factory / "
         "weapon smith. v0.13: extractors gate on terrain features."),
        ("Page 4 — Luxury chains",
         "Olive farm → olive press → oil (tier 3 gate). Vineyard → wine "
         "press → wine (tavern input). Clay pit → pottery shop → pottery "
         "(tier 4 gate)."),
        ("Page 5 — Activity status states",
         "Each building is classified each tick: active, partial, "
         "disconnected, unstaffed, starved, or idle. Status drives the "
         "inspector display and gates production / consumption."),
    ]
    y = 6.5
    for title, body in sections:
        ax.text(0.5, y, title, fontsize=12, fontweight="bold", color=C_TEXT)
        ax.text(0.5, y - 0.4, body, fontsize=10, color=C_TEXT, wrap=True)
        y -= 1.3

    ax.text(0.5, 0.6,
            "Numbers on arrows are units/tick at full activity. ",
            fontsize=9, color=C_TEXT)
    ax.text(0.5, 0.3,
            "Building colors: teal = extractor, purple = refiner, "
            "coral = consumer, gray = resource pool.",
            fontsize=9, color=C_TEXT)

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(OUTPUT) as pdf:
        page_summary(pdf)
        page_food_chain(pdf)
        page_lumber_chain(pdf)
        page_luxury_chains(pdf)
        page_status_states(pdf)
        d = pdf.infodict()
        d["Title"] = "Caesar III clone — Economy flows"
        d["Author"] = "Caesar III clone v0.13 documentation"
        d["Subject"] = "Visual documentation of supply chains and activity states"
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
