"""Random economic events.

Event definitions come from `data/events.json`. Modders add an event by
appending to that file — no edits here. Each event is a dict with:

    name:    str
    effects: dict[str, int]   resource deltas (food/wood/iron/tools/money)
    pop:     int              population delta
    happy:   int              happiness delta
    msg:     str              UI banner text
    color:   [r, g, b]        UI banner color
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Any

from constants import EVENT_MAX_INTERVAL, EVENT_MIN_INTERVAL, EVENTS_PATH
from signals import signals

log = logging.getLogger("caesar3.events")


def load_events(path: str | Path = EVENTS_PATH) -> list[dict[str, Any]]:
    """Load event definitions from a JSON file. Tuple-ifies color fields."""
    with Path(path).open() as f:
        raw = json.load(f)
    events = []
    for ev in raw:
        ev = dict(ev)
        if "color" in ev and isinstance(ev["color"], list):
            ev["color"] = tuple(ev["color"])
        events.append(ev)
    return events


class EconomicEventManager:
    DISPLAY_TICKS = 30

    # v0.28: trigger kinds for per-map scheduled events. The map file
    # declares each schedule entry as
    #     {"event": "Drought", "trigger": {"kind": "at_year", "value": 3}}
    # and we resolve them per-tick in update(). The full list:
    #
    #   at_year         value=int      fire once when game.year == value
    #   at_month        value=int      fire once when game.month == value
    #                                  (across all years — author can use
    #                                  this to set up "always in winter").
    #   at_tick         value=int      fire once when game_time == value
    #   random_after_tick value=int    after game_time>=value, roll a
    #                                  chance each tick (3%) and fire when
    #                                  it hits — gives a "looming threat
    #                                  arrives sometime after t=N" feel
    #   on_population_above  value=int fire once when economy.population
    #                                  first exceeds value
    #   on_treasury_below    value=int fire once when economy.treasury
    #                                  first drops below value
    #
    # Each entry tracks a `fired: bool` flag — most triggers are
    # one-shot. The exception is `random_after_tick`, which can re-arm
    # (but the rolled-and-not-yet-fired state lives in `fired=False`
    # plus the per-tick re-roll). For v0.28 we keep it simple: every
    # scheduled trigger fires AT MOST ONCE per game. Authors who want
    # a recurring blizzard create two entries.

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        scheduled: list[dict[str, Any]] | None = None,
        disable_random_events: bool = False,
    ):
        self.events = events if events is not None else load_events()
        self.cooldown = random.randint(EVENT_MIN_INTERVAL, EVENT_MAX_INTERVAL)
        self.current_event: dict[str, Any] | None = None
        self.display_timer: int = 0
        self.history: list[dict[str, str]] = []
        # v0.14: timed effects. An event with `duration_ticks > 0` and
        # a `modifiers` dict (e.g. `{"water_factor": 0.4}`) becomes an
        # active effect for that many ticks. While active, the modifiers
        # are applied via `current_modifiers()` — callers wrap their
        # ServiceMap / economy lookups with these multipliers.
        # Only one effect of each `name` runs at a time; re-triggering
        # the same event refreshes the timer rather than stacking.
        self.active_effects: list[dict[str, Any]] = []
        # v0.28: per-map scheduled events. Each entry:
        #   {"event": "Drought",
        #    "trigger": {"kind": "at_year", "value": 3},
        #    "fired": False}
        # Loaded from the map JSON's `scheduled_events` array. The
        # `fired` flag is set to True the moment the scheduler fires
        # the event, so even on a multi-tick condition (population
        # crossing a threshold) it only triggers once.
        self.scheduled: list[dict[str, Any]] = list(scheduled or [])
        for entry in self.scheduled:
            entry.setdefault("fired", False)
        # v0.28: when True the random cooldown never fires its dice
        # roll; only scheduled events trigger. Per-map flag — authors
        # building a pure-scripted scenario flip this on; the default
        # map / freeform game leaves it False so random events still
        # happen.
        self.disable_random_events: bool = bool(disable_random_events)

    def update(self, economy, game=None) -> None:  # noqa: ANN001
        if self.display_timer > 0:
            self.display_timer -= 1
        # v0.14: tick down active timed effects. Expire them before the
        # banner is drawn so the player sees one last frame of "drought
        # ending" if they happen to be looking at the banner.
        if self.active_effects:
            for fx in self.active_effects:
                fx["remaining_ticks"] -= 1
            self.active_effects = [
                fx for fx in self.active_effects if fx["remaining_ticks"] > 0
            ]
        # v0.28: check scheduled triggers before the random cooldown.
        # A scheduled event firing this tick still gets its banner
        # and history entry — the player can't distinguish "scripted"
        # from "random" events in the HUD, on purpose.
        if game is not None and self.scheduled:
            self._check_scheduled(economy, game)
        # Random cooldown — skipped if the map disables random events.
        if not self.disable_random_events:
            self.cooldown -= 1
            if self.cooldown <= 0:
                self._trigger(economy)
                self.cooldown = random.randint(EVENT_MIN_INTERVAL, EVENT_MAX_INTERVAL)

    def _check_scheduled(self, economy, game) -> None:  # noqa: ANN001
        """v0.28: walk the scheduled-event list and fire any whose
        trigger condition is met right now. Each entry fires at most
        once (the `fired` flag is set on dispatch).
        """
        game_year = int(getattr(game, "year", 1))
        game_month = int(getattr(game, "month", 1))
        game_time = int(getattr(game, "game_time", 0))
        for entry in self.scheduled:
            if entry["fired"]:
                continue
            trig = entry.get("trigger", {}) or {}
            kind = trig.get("kind")
            val = trig.get("value")
            fire = False
            if kind == "at_year":
                fire = val is not None and game_year >= int(val)
            elif kind == "at_month":
                fire = val is not None and game_month == int(val)
            elif kind == "at_tick":
                fire = val is not None and game_time >= int(val)
            elif kind == "random_after_tick":
                # Once we're past the floor, a 3%/tick chance fires it.
                # The constant is intentionally small — the player asked
                # for "looming threat", not "instant", and a 3% roll
                # per tick means the event lands somewhere in the next
                # ~33 ticks (~17s at 2 TPS) once eligibility opens.
                if val is not None and game_time >= int(val):
                    fire = random.random() < 0.03
            elif kind == "on_population_above":
                fire = val is not None and economy.population > int(val)
            elif kind == "on_treasury_below":
                fire = val is not None and economy.treasury < int(val)
            else:
                continue  # unknown trigger kind — skip silently
            if not fire:
                continue
            # Find the event definition by name.
            name = entry.get("event")
            ev = next((e for e in self.events if e["name"] == name), None)
            if ev is None:
                log.warning(
                    "Scheduled event '%s' not found in event registry; skipping",
                    name,
                )
                entry["fired"] = True  # don't keep retrying a missing event
                continue
            self._trigger(economy, event=ev)
            entry["fired"] = True

    def _trigger(self, economy, event: dict[str, Any] | None = None) -> None:  # noqa: ANN001
        """Fire one event. If `event` is supplied (scheduler path), use
        that; otherwise pick randomly from the registry (cooldown path).
        """
        if not self.events:
            return
        ev = event if event is not None else random.choice(self.events)
        self.current_event = ev
        self.display_timer = self.DISPLAY_TICKS
        economy.apply_event_effects(
            ev.get("effects", {}), ev.get("pop", 0), ev.get("happy", 0),
        )
        # v0.14: timed-effect events. If the event declares a non-zero
        # duration_ticks and a modifiers dict, start (or refresh) an
        # active effect entry. Modifiers are arbitrary string→float;
        # consumed today are "water_factor" (drought), the v0.28
        # "wood_consumption_factor" + "pop_kill_when_resource_zero"
        # (blizzard), but the structure supports adding "wage_factor",
        # "happiness_factor", etc. without further plumbing.
        duration = int(ev.get("duration_ticks", 0))
        modifiers = ev.get("modifiers") or {}
        if duration > 0 and modifiers:
            # Refresh existing same-named effect rather than stacking.
            for fx in self.active_effects:
                if fx["name"] == ev["name"]:
                    fx["remaining_ticks"] = duration
                    fx["modifiers"] = dict(modifiers)
                    break
            else:
                self.active_effects.append({
                    "name": ev["name"],
                    "remaining_ticks": duration,
                    "modifiers": dict(modifiers),
                })
        self.history.append({"name": ev["name"], "msg": ev["msg"]})
        if len(self.history) > 20:
            self.history = self.history[-20:]
        signals.emit("event_triggered", ev)
        log.info("EVENT: %s — %s", ev["name"], ev["msg"])

    def get_current(self) -> dict[str, Any] | None:
        if self.display_timer > 0 and self.current_event:
            return self.current_event
        return None

    # v0.14: aggregated modifiers from all active effects. Each modifier
    # is multiplied so two simultaneous droughts (unlikely, but
    # composable) cumulatively dim the water service. Returns an empty
    # dict when nothing is active — callers can check truthiness.
    #
    # v0.28: non-numeric modifier values are passed through unchanged
    # (last-event-wins on conflict). The blizzard event's
    # ``pop_kill_when_resource_zero: [["wood", 1]]`` is a list, not a
    # scalar — multiplying it would crash. Numeric values still
    # multiply, matching the v0.14 drought / event-stacking semantics.
    def current_modifiers(self) -> dict:
        out: dict = {}
        for fx in self.active_effects:
            for k, v in fx["modifiers"].items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k] = out.get(k, 1.0) * v
                else:
                    # Non-numeric modifier (list, dict, str, …):
                    # last-event-wins. We don't try to merge lists —
                    # the use case is rare enough to not warrant the
                    # complexity, and modders can split a multi-
                    # mechanic event into two if they need stacking.
                    out[k] = v
        return out

    # ── Persistence (1.6: persist in-flight banner) ───────────────────────
    def to_dict(self) -> dict:
        return {
            "cooldown": self.cooldown,
            "history": self.history[-10:],
            "current_event_name": (
                self.current_event["name"] if self.current_event else None
            ),
            "display_timer": self.display_timer,
            # v0.14: persist active timed effects so a save mid-drought
            # resumes mid-drought.
            "active_effects": list(self.active_effects),
            # v0.28: persist the per-map scheduled list AND each
            # entry's fired flag, so a save mid-scenario doesn't
            # re-fire events the player has already seen.
            "scheduled": [dict(s) for s in self.scheduled],
            "disable_random_events": self.disable_random_events,
        }

    def from_dict(self, d: dict) -> None:
        self.cooldown = d.get("cooldown", EVENT_MIN_INTERVAL)
        self.history = list(d.get("history", []))
        self.display_timer = d.get("display_timer", 0)
        # v0.14: pre-v0.14 saves don't have active_effects; default to
        # empty (no active drought etc. at load time).
        self.active_effects = [
            dict(fx) for fx in d.get("active_effects", [])
        ]
        # v0.28: scheduled-event list. Pre-v0.28 saves have no entry;
        # default to whatever the constructor seeded (typically [],
        # unless the map declared scheduled_events at load time).
        if "scheduled" in d:
            self.scheduled = [dict(s) for s in d["scheduled"]]
            for entry in self.scheduled:
                entry.setdefault("fired", False)
        if "disable_random_events" in d:
            self.disable_random_events = bool(d["disable_random_events"])
        name = d.get("current_event_name")
        if name and self.display_timer > 0:
            for ev in self.events:
                if ev["name"] == name:
                    self.current_event = ev
                    break
        else:
            self.current_event = None
