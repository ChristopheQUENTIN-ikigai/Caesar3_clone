"""Unit command & selection model (v0.47 — campaign foundation).

This is the pure-logic core of the military-unit management interface.
It deliberately holds **no** arcade imports — selection rectangles and
click handling live in the UI layer; this module is the testable model
underneath: what's selected, what control groups exist, what order each
unit is under, and which verbs are even legal for a given unit.

The full command vocabulary (12 verbs) is declared here from the start
so later drops only have to add *resolution* behaviour, not re-plumb the
model. In v0.47 only MOVE and STOP resolve into actual movement (wired
in walkers); the rest are accepted, validated, and stored as the unit's
current order/stance so the UI can already issue them and show state.

Design:
  * ``Verb`` — the 12 commands + STOP, as a str-valued enum so orders
    serialise trivially and tests read naturally.
  * ``Order`` — a verb plus an optional target (a tile (row,col) or a
    target unit id). Immutable-ish value object.
  * ``CommandState`` — per-unit: current order + persistent stance.
    Attached to a unit as ``unit.command``.
  * ``Selection`` — the set of currently-selected units, with add/
    toggle/clear and control-group (0-9) save/recall.
  * ``CommandManager`` — owns a Selection, validates verbs against unit
    kind (land vs naval), and stamps orders onto selected units.

Land vs naval: a unified system per the design decision. A selection may
hold both; ``CommandManager.issue`` applies an order only to the units
it's *legal* for (e.g. EMBARK only to land soldiers, PATROL not to a
transport) and reports which units accepted it, so the UI can show
"6 of 8 units obeyed".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verb(str, Enum):
    """The command vocabulary. STOP is the implicit 'cancel current
    order' verb; the other twelve are the player-facing commands."""
    STOP = "stop"
    MOVE = "move"          # navpoint: go to a tile
    GROUP = "group"        # form the selection into a named group
    SPLIT = "split"        # break a group apart
    JOIN = "join"          # merge into another group / formation
    EMBARK = "embark"      # board a transport (land soldier → ship)
    DISEMBARK = "disembark"  # leave a transport onto shore
    ATTACK = "attack"      # move-to-and-engage a target
    DEFEND = "defend"      # hold position, engage what comes
    PATROL = "patrol"      # loop between waypoints
    RETREAT = "retreat"    # disengage and fall back
    SIEGE = "siege"        # attack a building/fortification
    REST = "rest"          # recover (morale/hp) in place
    SCOUT = "scout"        # reveal/advance cautiously

    def __str__(self) -> str:  # so f-strings show "move", not "Verb.MOVE"
        return self.value


# Verbs that only make sense for land units, only for naval units, or
# for either. EMBARK is a land-soldier action (board a ship); DISEMBARK
# is issued to the transport (or its embarked troops). Naval units don't
# patrol/siege/rest/scout in this model (those are land-soldier stances);
# they do move, stop, attack, defend, retreat, group/split/join.
_LAND_ONLY = frozenset({Verb.EMBARK, Verb.SIEGE, Verb.SCOUT, Verb.REST,
                        Verb.PATROL})
_NAVAL_ONLY = frozenset({Verb.DISEMBARK})
# Everything else applies to both kinds.
_UNIVERSAL = frozenset({Verb.STOP, Verb.MOVE, Verb.GROUP, Verb.SPLIT,
                        Verb.JOIN, Verb.ATTACK, Verb.DEFEND, Verb.RETREAT})


def verb_applies_to(verb: Verb, *, naval: bool) -> bool:
    """Is ``verb`` a legal command for a unit of this kind?"""
    if verb in _UNIVERSAL:
        return True
    if verb in _NAVAL_ONLY:
        return naval
    if verb in _LAND_ONLY:
        return not naval
    return False


@dataclass
class Order:
    """A single command: a verb plus an optional target. ``target_tile``
    is a (row, col) for positional verbs (MOVE, PATROL waypoint, SIEGE a
    building tile); ``target_id`` is the id() of another unit for
    unit-targeting verbs (ATTACK, JOIN). Both optional — STOP/DEFEND/REST
    need neither."""
    verb: Verb
    target_tile: tuple[int, int] | None = None
    target_id: int | None = None

    def __post_init__(self) -> None:
        # Coerce a bare string verb (e.g. from a UI button id or a save)
        # into the enum so callers can pass either.
        if not isinstance(self.verb, Verb):
            self.verb = Verb(str(self.verb))


# Stances that persist until changed (vs one-shot orders like MOVE which
# complete). Used by later drops to decide whether a unit re-acquires the
# behaviour each tick.
PERSISTENT_STANCES = frozenset({Verb.DEFEND, Verb.PATROL, Verb.SIEGE,
                                Verb.REST, Verb.SCOUT})


@dataclass
class CommandState:
    """Per-unit command state. Lives on ``unit.command``. Holds the
    current (possibly one-shot) order and the persistent stance the unit
    falls back to when an order completes. ``group`` is the control-group
    number the unit belongs to (or None)."""
    order: Order | None = None
    stance: Verb = Verb.DEFEND  # sensible default: hold and engage
    group: int | None = None

    def set_order(self, order: Order) -> None:
        self.order = order
        # Issuing a persistent stance also updates the fallback stance.
        if order.verb in PERSISTENT_STANCES:
            self.stance = order.verb

    def clear_order(self) -> None:
        self.order = None


def ensure_command(unit: Any) -> CommandState:
    """Return the unit's CommandState, creating it on first access. Lets
    us attach command state lazily to existing walker objects without
    touching their constructors."""
    cs = getattr(unit, "command", None)
    if not isinstance(cs, CommandState):
        cs = CommandState()
        unit.command = cs
    return cs


def is_naval(unit: Any) -> bool:
    """A unit is naval if its category says so (ships set
    ``category = "naval"``)."""
    return getattr(unit, "category", "") == "naval"


class Selection:
    """The set of currently-selected units, plus 0-9 control groups.

    Units are held by identity (the live object). ``control_groups`` maps
    a digit to a list of units; recalling a group replaces the selection
    with that group's still-living members.
    """

    def __init__(self) -> None:
        self._units: list[Any] = []
        self.control_groups: dict[int, list[Any]] = {}

    # ── current selection ──────────────────────────────────────────
    @property
    def units(self) -> list[Any]:
        """Live selected units (drops any that have since died)."""
        self._units = [u for u in self._units if not getattr(u, "done", False)]
        return list(self._units)

    def count(self) -> int:
        return len(self.units)

    def is_empty(self) -> bool:
        return self.count() == 0

    def clear(self) -> None:
        self._units = []

    def select(self, units: list[Any]) -> None:
        """Replace the selection with ``units`` (dedup by identity)."""
        seen: set[int] = set()
        out: list[Any] = []
        for u in units:
            if id(u) not in seen:
                seen.add(id(u))
                out.append(u)
        self._units = out

    def add(self, unit: Any) -> None:
        if all(u is not unit for u in self._units):
            self._units.append(unit)

    def toggle(self, unit: Any) -> None:
        """Shift-click semantics: add if absent, remove if present."""
        for i, u in enumerate(self._units):
            if u is unit:
                del self._units[i]
                return
        self._units.append(unit)

    def contains(self, unit: Any) -> bool:
        return any(u is unit for u in self._units)

    # ── control groups (0-9) ───────────────────────────────────────
    def assign_group(self, digit: int) -> None:
        """Ctrl+digit: save the current selection as control group N."""
        self.control_groups[int(digit)] = self.units

    def recall_group(self, digit: int) -> bool:
        """Digit: replace the selection with control group N's living
        members. Returns True if the group existed and was non-empty."""
        members = [u for u in self.control_groups.get(int(digit), [])
                   if not getattr(u, "done", False)]
        if not members:
            return False
        self.select(members)
        return True


class CommandManager:
    """Owns the selection and turns player intent into orders on units.

    The UI calls ``select_*`` to build a selection, then ``issue`` to
    stamp an order onto every selected unit the order is legal for.
    Resolution (actually moving/fighting) is the units' job in later
    drops; this manager only validates and records intent.
    """

    def __init__(self) -> None:
        self.selection = Selection()

    # ── selection entry points (UI wires clicks/drag to these) ──────
    def select_one(self, unit: Any) -> None:
        self.selection.select([unit] if unit is not None else [])

    def select_box(self, units: list[Any]) -> None:
        """Drag-box select: replace selection with everything in the box.
        Caller pre-filters to player-controllable units."""
        self.selection.select(list(units))

    def add_to_selection(self, unit: Any) -> None:
        self.selection.add(unit)

    def toggle_in_selection(self, unit: Any) -> None:
        self.selection.toggle(unit)

    def clear_selection(self) -> None:
        self.selection.clear()

    # ── issuing orders ──────────────────────────────────────────────
    def issue(self, order: Order) -> list[Any]:
        """Stamp ``order`` onto every selected unit it's legal for.
        Returns the list of units that accepted it (so the UI can report
        partial obedience). Units for which the verb is illegal are left
        with their existing order untouched."""
        accepted: list[Any] = []
        for u in self.selection.units:
            if verb_applies_to(order.verb, naval=is_naval(u)):
                ensure_command(u).set_order(order)
                accepted.append(u)
        return accepted

    def issue_verb(self, verb: Verb, *, tile=None, target_id=None) -> list[Any]:
        """Convenience: build an Order and issue it."""
        return self.issue(Order(verb, target_tile=tile, target_id=target_id))

    # ── control-group passthrough ───────────────────────────────────
    def assign_group(self, digit: int) -> None:
        self.selection.assign_group(digit)
        # Stamp the group number onto each member's command state.
        for u in self.selection.units:
            ensure_command(u).group = int(digit)

    def recall_group(self, digit: int) -> bool:
        return self.selection.recall_group(digit)
