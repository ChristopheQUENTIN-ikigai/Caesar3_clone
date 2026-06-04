"""Map-level trigger wiring — companion to ``events.py``.

A *trigger* is a single edge in the scenario flag graph. The author
writes one in the Event editor (a graph view sitting over the four
library editors); on disk it lands inside the map JSON's top-level
``triggers`` array. Schema::

    {
      "id":   "branch_refused",
      "when": {"kind": "on_flag", "flag": "tribute_refused"},
      "do": [
        {"kind": "play_cutscene", "id": "cutscene_caesar_wrath"},
        {"kind": "fire_event",    "name": "Caesar Displeased"},
        {"kind": "schedule_event","event": "Barbarian Raid",
         "trigger": {"kind": "at_tick", "value": 3600}}
      ],
      "once": true,
      "notes": "free-text author comment, ignored at runtime"
    }

Supported ``when`` kinds — the first six mirror v0.28's
``scheduled_events`` so the same author intent is expressible in
either field; the last four are net-new (the bridge between the flag
graph and the timeline, plus the v0.37 seasonal/ratio gates):

    at_tick              value=int       game_time >= value
    at_year              value=int       game.year  >= value
    at_month             value=int       game.month == value
    on_population_above  value=int       economy.population > value
    on_treasury_below    value=int       economy.treasury  < value
    on_event_fired       event=str       named event fired this tick
    on_flag              flag=str        flag was set this tick
                         state="unset"   flag was cleared this tick
    in_season_random     season=str      season (Winter|Spring|Summer|
                         prob=float      Autumn) AND a per-tick Bernoulli
                                         roll passes (e.g. "Blizzard
                                         only fires in winter, 1% per
                                         tick"). Author pairs with
                                         once=False for recurring weather.
    on_building_ratio_below
                         building=str    (count of buildings of kind /
                         per=str         per-1000 of `per` metric) < value.
                         value=float     `per` is "population" (default)
                                         or "inhabitants" — same number,
                                         alias for readability. Useful
                                         for "spread plague when
                                         doctors-per-1000-citizens <
                                         1.0", i.e. < 0.1%.

Supported ``do`` kinds:

    play_cutscene    id=str
    fire_request     id=str
    fire_event       name=str
    set_flag         flag=str
    schedule_event   event=str, trigger={kind, value}

The manager is deliberately a thin dispatcher. It does NOT own the
event registry, the cutscene library, or the request library — those
each live behind their own editor / loader. Instead the dispatcher
holds *callbacks* injected by the game window at construction time
(``cutscene_callback``, ``request_callback``, ``event_manager``) and
calls into them by id/name. Same separation-of-concerns reason
cutscenes is a sibling module of events rather than a subclass.

Why a separate manager from ``EconomicEventManager``?

  * Events define what Famine, Plague, etc. DO (library).
  * Triggers define WHEN those events fire — and also when cutscenes
    play, requests pop up, flags get set.
  * One editor owns each file; merging the managers would force the
    editors to grow tendrils into each other's domains.

Save / load: the manager round-trips through ``to_dict`` / ``load_dict``
the same way ``EconomicEventManager`` does. The ``fired`` flag on each
trigger persists across saves (otherwise a save mid-scenario would
replay every one-shot trigger on reload).
"""
from __future__ import annotations

import logging
import random
from typing import Any, Callable

log = logging.getLogger(__name__)


# Type aliases — keep the constructor signature readable.
CutsceneCallback = Callable[[str], bool]   # cutscene id → did it play
RequestCallback  = Callable[[str], bool]   # request  id → did it fire
FlagSetter       = Callable[[str], None]   # called for `do` set_flag


# Roll chance for random_after_tick — kept in lock-step with
# events.EconomicEventManager._check_scheduled (3 % / tick) so an
# author moving a wiring between `triggers` and `scheduled_events`
# gets the same statistical behaviour.
RANDOM_AFTER_TICK_PROB = 0.03


class TriggerManager:
    """Per-map trigger dispatcher.

    Construct with the raw ``triggers`` list lifted out of the map
    JSON, plus the four hooks below. Call ``update(economy, game)``
    once per tick; call ``on_flag_set(flag)`` from the runtime any
    time a flag flips (RPG decision, cutscene auto-fire,
    ``do.set_flag`` effect, etc); call ``on_event_fired(name)`` from
    inside the event manager dispatch path.

    Triggers with ``once=True`` (the default) are marked ``fired=True``
    after dispatch and skipped thereafter. Triggers with ``once=False``
    re-arm — useful for recurring beats (yearly tax-collection prompt,
    monthly market festival, etc).
    """

    def __init__(
        self,
        triggers: list[dict[str, Any]] | None = None,
        *,
        cutscene_callback: CutsceneCallback | None = None,
        request_callback: RequestCallback | None = None,
        flag_setter: FlagSetter | None = None,
        event_manager: Any = None,
        naval_raid_callback: Any = None,
    ) -> None:
        self.triggers: list[dict[str, Any]] = []
        for t in (triggers or []):
            t = dict(t)
            t.setdefault("fired", False)
            t.setdefault("once", True)
            self.triggers.append(t)

        self._cutscene_cb = cutscene_callback
        self._request_cb = request_callback
        self._flag_setter = flag_setter
        self._event_manager = event_manager
        # v0.46: dispatches a naval raid (spawns EnemyShips). Signature
        # ``cb(count: int, hp: int, damage: int)``. None in headless
        # contexts → the effect logs and no-ops.
        self._naval_raid_cb = naval_raid_callback

        # Flags that flipped since the last update() pass — drained
        # in update() to fire any on_flag triggers. We accumulate
        # rather than fire synchronously so the dispatch order stays
        # deterministic (game tick → drain flags → drain events).
        self._pending_flags: set[str] = set()
        self._pending_cleared_flags: set[str] = set()

        # Events that fired since the last update() pass — same
        # accumulator pattern for on_event_fired.
        self._pending_events: set[str] = set()

        # Flags currently "set" in the scenario flag graph. Kept here
        # rather than on the game object so save/load round-trips with
        # the manager. The runtime calls ``on_flag_set`` whenever any
        # subsystem flips a flag; the set lives here authoritatively.
        self.flags: set[str] = set()

        # Auto-connect to the engine's event_triggered signal so
        # on_event_fired triggers work without the caller having to
        # remember to forward firings. The signal payload is the
        # event dict; we extract its ``name`` and queue it.
        try:
            from signals import signals
            signals.connect("event_triggered", self._on_event_signal)
        except Exception:  # noqa: BLE001
            # Tests / headless contexts may stub signals — non-fatal.
            log.debug("trigger: could not auto-connect to signals", exc_info=True)

    def _on_event_signal(self, event: dict[str, Any]) -> None:
        """Adapter: SignalBus → on_event_fired."""
        name = event.get("name") if isinstance(event, dict) else None
        if name:
            self.on_event_fired(name)

    # ── Runtime entry points ──────────────────────────────────────

    def on_flag_set(self, flag: str) -> None:
        """Mark a flag as set. Called by the runtime whenever any
        subsystem (RPG dispatcher, cutscene auto-fire,
        ``do.set_flag`` effect) flips a flag. Idempotent — re-setting
        an already-set flag does NOT re-fire on_flag triggers (the
        flag has to genuinely transition from unset → set).
        """
        if not flag:
            return
        if flag in self.flags:
            return
        self.flags.add(flag)
        self._pending_flags.add(flag)
        log.debug("trigger flag set: %s", flag)

    def on_flag_unset(self, flag: str) -> None:
        """Inverse of on_flag_set — clears a flag and queues any
        on_flag triggers with ``state="unset"``."""
        if not flag or flag not in self.flags:
            return
        self.flags.discard(flag)
        self._pending_cleared_flags.add(flag)
        log.debug("trigger flag cleared: %s", flag)

    def on_event_fired(self, event_name: str) -> None:
        """Called from inside ``EconomicEventManager._trigger`` when
        any event lands. Queues the name so on_event_fired triggers
        pick it up next ``update()``.
        """
        if not event_name:
            return
        self._pending_events.add(event_name)
        log.debug("trigger event observed: %s", event_name)

    def update(self, economy, game) -> None:  # noqa: ANN001
        """Per-tick pump. Resolves every un-fired trigger's ``when``
        condition against the current game state and dispatches its
        ``do`` list. Mirrors
        ``EconomicEventManager._check_scheduled`` for the time/
        condition kinds and adds the flag / event kinds on top.

        Pending flags + pending events are drained at end-of-pass so
        a flag set this tick fires its triggers this tick.
        """
        game_year = int(getattr(game, "year", 1))
        game_month = int(getattr(game, "month", 1))
        game_time = int(getattr(game, "game_time", 0))
        # Snapshot the pending sets so a trigger's `do` (which may
        # call set_flag → on_flag_set → _pending_flags.add) doesn't
        # mutate the set we're iterating. New flags from this pass
        # will be picked up next tick — same semantics as the
        # scheduled_events scanner.
        pending_flags = set(self._pending_flags)
        pending_cleared = set(self._pending_cleared_flags)
        pending_events = set(self._pending_events)
        self._pending_flags.clear()
        self._pending_cleared_flags.clear()
        self._pending_events.clear()

        for entry in self.triggers:
            if entry.get("fired") and entry.get("once", True):
                continue
            if not self._when_matches(
                entry.get("when") or {},
                economy=economy, game=game,
                game_year=game_year, game_month=game_month,
                game_time=game_time,
                pending_flags=pending_flags,
                pending_cleared=pending_cleared,
                pending_events=pending_events,
            ):
                continue
            self._dispatch(entry, economy, game)
            entry["fired"] = True
            # Reset for re-arming (recurring) triggers — the contract
            # is "fires at most once per tick"; subsequent ticks
            # re-evaluate from scratch.
            if not entry.get("once", True):
                entry["fired"] = False

    # ── Condition evaluation ──────────────────────────────────────

    def _count_buildings(self, game, building_kind: str) -> int:  # noqa: ANN001
        """Count placed buildings of the given registry id on the
        live game map. Used by ``on_building_ratio_below``.

        Goes through ``game.game_map.get_building_positions()`` —
        the public iterator already deduplicates multi-tile buildings
        (one entry per origin, not per footprint cell). Returns 0 if
        the game has no map yet (e.g. trigger evaluates during the
        editor's frozen empty-world view).

        Construction-in-progress buildings count too: the author is
        gating on civic-service capacity, and a half-built clinic
        still represents an investment that should defer the
        shortage-trigger. If a future tuning pass wants to exclude
        unfinished buildings, filter by ``game.game_map.construction_progress``.
        """
        gm = getattr(game, "game_map", None)
        if gm is None:
            return 0
        try:
            positions = gm.get_building_positions()
        except Exception:  # noqa: BLE001 — be defensive in trigger path
            log.debug("trigger: _count_buildings probe failed", exc_info=True)
            return 0
        return sum(1 for (bid, _r, _c) in positions if bid == building_kind)

    def _when_matches(
        self, when: dict[str, Any], *,
        economy, game,           # noqa: ANN001
        game_year: int, game_month: int, game_time: int,
        pending_flags: set[str],
        pending_cleared: set[str],
        pending_events: set[str],
    ) -> bool:
        kind = when.get("kind")
        if kind == "at_tick":
            val = when.get("value")
            return val is not None and game_time >= int(val)
        if kind == "at_year":
            val = when.get("value")
            return val is not None and game_year >= int(val)
        if kind == "at_month":
            val = when.get("value")
            return val is not None and game_month == int(val)
        if kind == "on_population_above":
            val = when.get("value")
            return val is not None and economy.population > int(val)
        if kind == "on_treasury_below":
            val = when.get("value")
            return val is not None and economy.treasury < int(val)
        if kind == "random_after_tick":
            val = when.get("value")
            if val is None or game_time < int(val):
                return False
            return random.random() < RANDOM_AFTER_TICK_PROB
        if kind == "on_flag":
            flag = when.get("flag", "")
            state = when.get("state", "set")
            if state == "unset":
                return flag in pending_cleared
            return flag in pending_flags
        if kind == "on_event_fired":
            name = when.get("event", "")
            return name in pending_events
        if kind == "in_season_random":
            # v0.37: seasonal probabilistic gate. The author writes
            #   {"kind": "in_season_random", "season": "Winter", "prob": 0.01}
            # and the trigger rolls a Bernoulli each tick the game's
            # month maps to that season. Pairs naturally with
            # ``once=False`` for recurring weather effects: a Blizzard
            # fire_event in winter with prob 0.01 averages ~6 hits
            # across the ~600 winter ticks of a 7200-tick year
            # (1% × 200 ticks × 3 winter months / 12 months). Authors
            # who want exactly one blizzard per scenario keep
            # ``once=True``.
            season = str(when.get("season", "")).strip()
            prob = when.get("prob")
            if not season or prob is None:
                return False
            try:
                prob_f = float(prob)
            except (TypeError, ValueError):
                return False
            # Look up the current season; the mapping lives in
            # ``game_window.SEASON_BY_MONTH`` so this is a soft import
            # to keep ``triggers.py`` headless-importable (tests stub
            # ``game`` without pulling in arcade).
            try:
                from game_window import SEASON_BY_MONTH
                current = SEASON_BY_MONTH.get(int(game_month), "")
            except Exception:  # noqa: BLE001
                # Fallback table — mirrors game_window.SEASON_BY_MONTH so
                # headless tests don't need arcade just to evaluate a
                # season check. Keep in sync if the canonical table
                # there ever shifts.
                _FALLBACK_SEASON = {
                    1: "Winter", 2: "Winter", 3: "Spring",
                    4: "Spring", 5: "Spring", 6: "Summer",
                    7: "Summer", 8: "Summer", 9: "Autumn",
                    10: "Autumn", 11: "Autumn", 12: "Winter",
                }
                current = _FALLBACK_SEASON.get(int(game_month), "")
            if current.lower() != season.lower():
                return False
            return random.random() < prob_f
        if kind == "on_building_ratio_below":
            # v0.37: civic-service shortage gate. Author writes
            #   {"kind": "on_building_ratio_below",
            #    "building": "clinic",
            #    "per": "population",
            #    "value": 1.0}
            # The ratio is (count of buildings whose registry id ==
            # ``building``) divided by (population / 1000) — i.e.
            # buildings per 1000 inhabitants. value=1.0 means "fewer
            # than one clinic per 1000 citizens" (≈ < 0.1%, the
            # threshold called out in the v0.37 spec for the plague
            # spread example). We measure per-1000 rather than as a
            # raw fraction because the raw fraction would always be
            # absurdly small (1 clinic / 200 inhabitants = 0.005,
            # author has to think in scientific notation), whereas
            # 1.0 per 1000 is a number authors can eyeball.
            #
            # Pop=0 short-circuits to "true" — a city with no
            # inhabitants trivially fails any ratio test. Pop is
            # taken from the economy so the manager doesn't have
            # to know how the game tracks people internally.
            kind_id = str(when.get("building", "")).strip()
            value = when.get("value")
            if not kind_id or value is None:
                return False
            try:
                threshold = float(value)
            except (TypeError, ValueError):
                return False
            per = str(when.get("per", "population")).strip().lower()
            if per not in ("population", "inhabitants"):
                # Unknown denominator — log once and skip rather than
                # silently treating it as population (the author may
                # have meant something else, e.g. "houses", which
                # isn't supported yet).
                log.warning(
                    "trigger: on_building_ratio_below per=%r not "
                    "supported — skipping", per,
                )
                return False
            pop = int(getattr(economy, "population", 0) or 0)
            if pop <= 0:
                # No inhabitants → ratio is undefined; treat as
                # "below any positive threshold" so authors get the
                # expected behaviour (e.g. plague-on-empty-map is
                # still possible if they want it).
                return threshold > 0.0
            count = self._count_buildings(game, kind_id)
            ratio_per_1000 = (count * 1000.0) / pop
            return ratio_per_1000 < threshold
        # Unknown kind — log once per trigger to help authors debug
        # typos, then never fire it. We don't raise: a bad trigger
        # shouldn't tank an entire scenario.
        log.warning("trigger: unknown when.kind %r — skipping", kind)
        return False

    # ── Effect dispatch ───────────────────────────────────────────

    def _dispatch(self, entry: dict[str, Any], economy, game) -> None:  # noqa: ANN001
        trig_id = entry.get("id", "<unnamed>")
        for effect in entry.get("do", []) or []:
            kind = effect.get("kind")
            try:
                if kind == "play_cutscene":
                    self._do_play_cutscene(effect.get("id", ""), trig_id)
                elif kind == "fire_request":
                    self._do_fire_request(effect.get("id", ""), trig_id)
                elif kind == "fire_event":
                    self._do_fire_event(effect.get("name", ""), economy, trig_id)
                elif kind == "set_flag":
                    self._do_set_flag(effect.get("flag", ""), trig_id)
                elif kind == "schedule_event":
                    self._do_schedule_event(
                        effect.get("event", ""),
                        effect.get("trigger", {}) or {},
                        trig_id,
                    )
                elif kind == "force_naval_raid":
                    self._do_force_naval_raid(effect, trig_id)
                else:
                    log.warning(
                        "trigger %s: unknown do.kind %r — skipping",
                        trig_id, kind,
                    )
            except Exception:  # noqa: BLE001
                # A broken effect should not tank the rest of the
                # trigger's do-list, nor the scenario. Log and move
                # on; the runtime will surface this in the dev log.
                log.exception("trigger %s: do %r raised", trig_id, kind)
        log.info("trigger fired: %s", trig_id)

    def _do_force_naval_raid(self, effect: dict[str, Any], trig_id: str) -> None:
        """Dispatch a naval raid via the wired callback. Effect fields:
        ``count`` (raiders, default 2), ``hp``/``damage`` (per raider).
        No-ops with a log line in headless contexts (no callback)."""
        if self._naval_raid_cb is None:
            log.warning(
                "trigger %s: force_naval_raid but no naval-raid callback wired",
                trig_id,
            )
            return
        count = int(effect.get("count", 2))
        hp = int(effect.get("hp", 80))
        damage = int(effect.get("damage", 10))
        self._naval_raid_cb(count, hp, damage)
        log.info("trigger %s: launched naval raid (count=%d)", trig_id, count)

    def _do_play_cutscene(self, cid: str, trig_id: str) -> None:
        if not cid:
            log.warning("trigger %s: play_cutscene with empty id", trig_id)
            return
        if self._cutscene_cb is None:
            log.warning(
                "trigger %s: play_cutscene %s requested but no cutscene "
                "callback wired", trig_id, cid,
            )
            return
        self._cutscene_cb(cid)

    def _do_fire_request(self, rid: str, trig_id: str) -> None:
        if not rid:
            log.warning("trigger %s: fire_request with empty id", trig_id)
            return
        if self._request_cb is None:
            log.warning(
                "trigger %s: fire_request %s requested but no request "
                "callback wired", trig_id, rid,
            )
            return
        self._request_cb(rid)

    def _do_fire_event(self, name: str, economy, trig_id: str) -> None:  # noqa: ANN001
        if not name:
            log.warning("trigger %s: fire_event with empty name", trig_id)
            return
        em = self._event_manager
        if em is None:
            log.warning(
                "trigger %s: fire_event %s requested but no event "
                "manager wired", trig_id, name,
            )
            return
        ev = next((e for e in getattr(em, "events", []) if e.get("name") == name), None)
        if ev is None:
            log.warning(
                "trigger %s: event %r not in event registry — skipping",
                trig_id, name,
            )
            return
        # Re-use the existing _trigger path so banner + history +
        # active_effects all wire up exactly like a random event.
        # The on_event_fired hook gets driven via the signal bus
        # (events.py already emits "event_triggered" from _trigger)
        # so we don't have to forward it manually here.
        em._trigger(economy, event=ev)

    def _do_set_flag(self, flag: str, trig_id: str) -> None:
        if not flag:
            log.warning("trigger %s: set_flag with empty flag", trig_id)
            return
        # Flip on ourself so cascading on_flag triggers see it next
        # tick. If a flag_setter is wired (so the cutscene player /
        # cutscene auto-fire path also sees it), call it too.
        self.on_flag_set(flag)
        if self._flag_setter is not None:
            self._flag_setter(flag)

    def _do_schedule_event(
        self, event_name: str, trigger: dict[str, Any], trig_id: str,
    ) -> None:
        if not event_name:
            log.warning("trigger %s: schedule_event with empty name", trig_id)
            return
        em = self._event_manager
        if em is None:
            log.warning(
                "trigger %s: schedule_event %s requested but no event "
                "manager wired", trig_id, event_name,
            )
            return
        em.scheduled.append({
            "event": event_name,
            "trigger": dict(trigger),
            "fired": False,
        })

    # ── Save / load ───────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Snapshot for the save file. The ``fired`` flag on each
        trigger is persisted so a one-shot trigger that already
        fired stays fired across save/load. The active flag set is
        also persisted so on_flag triggers don't refire.
        """
        return {
            "triggers": [dict(t) for t in self.triggers],
            "flags": sorted(self.flags),
        }

    def load_dict(self, d: dict[str, Any]) -> None:
        """Restore from the save file. Pre-v0.36 saves have no entry;
        leave the in-memory state alone in that case (loaded from
        the map JSON at world init).
        """
        if "triggers" in d:
            self.triggers = [dict(t) for t in d["triggers"]]
            for t in self.triggers:
                t.setdefault("fired", False)
                t.setdefault("once", True)
        if "flags" in d:
            self.flags = set(d["flags"])
        # Pending sets always reset on load — anything mid-flight
        # at save time is considered handled by the save itself.
        self._pending_flags.clear()
        self._pending_cleared_flags.clear()
        self._pending_events.clear()
