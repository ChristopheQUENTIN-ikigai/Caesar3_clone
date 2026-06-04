"""Tick recorder — append-only JSONL diagnostic log. (v0.19)

Press ``Ctrl+R`` in-game to toggle recording (plain ``r`` opens the
commercial-roads window since v0.50). Every simulation tick that
follows appends a single JSON line to ``./data/recordings/<session>.jsonl``
capturing buildings, resources, walkers, employment and per-citizen
demand vs supply. The file is line-delimited JSON (JSONL) — every line
is a self-contained record — so partial writes never corrupt prior
ticks and analysis tools can stream it.

Why JSONL and not CSV?

  * Records are nested (per-building rows, per-role aggregates,
    per-resource net flow). CSV requires either flattening to one
    column per (building × field) — fragile when buildings are
    placed/demolished — or serialising a JSON blob into a CSV cell.
    JSONL is the same payload without the wrapping awkwardness.
  * Append-only: the recorder opens the file in append mode and
    flushes every ``FLUSH_EVERY`` records, so a crash or Ctrl-C
    keeps every committed line readable.
  * Streaming: pandas ``read_json(path, lines=True)`` parses it
    directly into a DataFrame; jq, grep, head all work.

Performance budget: at 2 ticks/sec on a 50-building city the per-tick
record is ~3 KB, so a 10-minute session writes ~3.6 MB. Disk write
cost is negligible — we explicitly do NOT slow the tick rate while
recording. Buffer flush every 10 records caps the lost-data window
at ~5 seconds on crash.

Why a *session* file rather than overwriting one path?

  * Re-pressing ``r`` to toggle off-then-on shouldn't clobber the
    previous run's data. Each on-press starts a new file with the
    current wall-clock timestamp, so a player can compare runs
    by `ls -lt data/recordings/`.

Schema (one record per tick):

::

    {
      "tick": 412,                      # game_time
      "year": 2, "month": 5,
      "season": "Spring",
      "wall_time": "2026-05-05T17:23:14",  # ISO local
      "population": 106,
      "treasury": 13505,
      "happiness": 63,
      "fed_fraction": 1.0,
      "nutrient_diversity": 1,
      "resources": {"bread": 65, "wheat": 50, ...},
      "production": {"bread": 10, "flour": 7, ...},     # this tick gross
      "consumption": {"bread": 3, "flour": 4, ...},     # this tick gross
      "income_per_tick": 25,
      "expenses_per_tick": 11,
      "jobs": {                                          # employment
        "filled": 27, "needed": 30, "jobless": 79,
        "by_role": {"worker": [22, 24], "trader": [3, 4], ...}
      },
      "buildings": [
        {"id": "farm", "row": 15, "col": 8, "state": "active",
         "workers_filled": 6, "workers_needed": 6,
         "throttle": 1.0, "reason": ""},
        ...
      ],
      "walkers": {                                       # by role
        "worker": 5, "trader": 2, "citizen": 18, ...,
        "delivery": 3, "soldier": 5, "enemy": 0
      },
      "per_citizen": {
        "food_demand_per_tick": 1.0,
        "food_supplied_this_tick": 106,
        "food_satiety": 1.0
      },
      "military": {                                      # v0.22
        "soldiers": 6, "armed": 5, "unarmed": 1,
        "retreating": 0, "morale_avg": 0.83,
        "garrisons": 2, "weapons_stock": 4,
        "iron_ore_stock": 8, "iron_stock": 12,
        "enemies_active": 0, "combat_events": []
      },
      "diagnostics": [                                   # v0.22
        {"resource": "weapons", "stage": "mine",
         "cause": "no_workers", "severity": 3},
        ...
      ],
      "rebellion": {"pressure": 0.0, "ratio": 0.0,       # v0.22
                    "active_rebels": 0},
      "caesar": {"favor": 50, "request_pending": false,
                 "request": null},
      "barter": [...]    # zero or more barters since the last record
    }

Public API
----------

  ``TickRecorder`` — opens/closes a file, appends one record per
  tick. Construct with ``TickRecorder.start()`` (creates file with
  current timestamp) or ``TickRecorder()`` (off until ``.start()``).

The game window owns one instance and toggles it on the 'r' keypress.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any

log = logging.getLogger("caesar3.recorder")

RECORDINGS_DIR = Path(__file__).resolve().parent / "data" / "recordings"
FLUSH_EVERY = 10            # ticks between flush() calls
SESSION_FNAME_FMT = "session_%Y%m%d_%H%M%S.jsonl"


class TickRecorder:
    """Append-only JSONL diagnostic recorder.

    State machine: ``inactive`` → ``recording`` (on ``start``) →
    ``inactive`` (on ``stop``). ``record_tick`` no-ops when inactive
    so the call site doesn't need to branch.

    Pending barters and notable events accumulated between ticks are
    drained into the next record and reset.
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self._fp = None
        self._records_since_flush = 0
        # Scratch: barters / events that happened since the last
        # record_tick. Drained on every record_tick call.
        self._pending_barters: list[dict] = []
        self._pending_events: list[str] = []

    @property
    def is_active(self) -> bool:
        return self._fp is not None

    # ── Lifecycle ────────────────────────────────────────────────────────
    def start(self, label: str | None = None) -> Path:
        """Open a new recording file. Returns the path. Idempotent: a
        second call while active rotates to a new file.
        """
        if self.is_active:
            self.stop()
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = f"_{label}" if label else ""
        self.path = RECORDINGS_DIR / f"session_{ts}{slug}.jsonl"
        self._fp = self.path.open("a", buffering=1)  # line-buffered
        self._records_since_flush = 0
        log.info("Recorder started → %s", self.path)
        return self.path

    def stop(self) -> Path | None:
        """Close the file. Returns the path that was being written, or
        None if the recorder was already inactive.
        """
        path = self.path
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:
                log.exception("Error closing recorder file")
            self._fp = None
            log.info("Recorder stopped (%s)", path)
        self.path = None
        self._records_since_flush = 0
        return path

    # ── Per-tick events from the game loop ───────────────────────────────
    def note_barter(self, record: dict) -> None:
        """Stash a barter record produced by ``bartering.execute_barter``
        so it lands in the *next* tick's record.
        """
        if self.is_active:
            self._pending_barters.append(record)

    def note_event(self, msg: str) -> None:
        """Free-form one-line event string (notification, raid, ...)."""
        if self.is_active:
            self._pending_events.append(msg)

    def record_tick(self, game) -> None:  # noqa: ANN001 — game = CaesarGameWindow
        """Snapshot the game state and append one JSONL record."""
        if not self.is_active or self._fp is None:
            return
        try:
            payload = self._build_payload(game)
            self._fp.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self._records_since_flush += 1
            if self._records_since_flush >= FLUSH_EVERY:
                self._fp.flush()
                self._records_since_flush = 0
        except Exception:
            log.exception("Recorder failed to write tick %s; continuing",
                          getattr(game, "game_time", "?"))

    # ── Internals ────────────────────────────────────────────────────────
    def _build_payload(self, game) -> dict[str, Any]:  # noqa: ANN001
        eco = game.economy
        wm = game.walker_manager

        # Per-role walker counts. ``role`` is set on every walker.
        walker_counts: Counter = Counter()
        for w in wm.walkers:
            walker_counts[getattr(w, "role", "?")] += 1
        # Delivery walkers are a subclass — count them separately.
        from walkers import DeliveryWalker, Soldier, Enemy
        walker_counts["delivery"] = sum(
            1 for w in wm.walkers if isinstance(w, DeliveryWalker)
        )
        walker_counts["soldier"] = sum(
            1 for w in wm.walkers if isinstance(w, Soldier)
        )
        walker_counts["enemy"] = sum(
            1 for w in wm.walkers if isinstance(w, Enemy)
        )

        # Per-building rows. Pulls the same building_status snapshot
        # the inspector / J panel use.
        buildings_rows: list[dict] = []
        bs = getattr(eco, "building_status", {}) or {}
        for bt, r, c in game.game_map.get_building_positions():
            st = bs.get((r, c))
            if st is None:
                continue
            buildings_rows.append({
                "id": bt, "row": r, "col": c,
                "state": st.state,
                "workers_filled": int(st.workers_filled),
                "workers_needed": int(st.workers_needed),
                "throttle": round(float(st.throttle), 3),
                "reason": st.reason or "",
            })

        # Jobs aggregate.
        from jobs import snapshot_jobs
        jsnap = snapshot_jobs(game.game_map, game.registry, bs, eco.population)
        jobs_block = {
            "filled": int(jsnap.total_filled),
            "needed": int(jsnap.total_demand),
            "shortfall": int(jsnap.total_shortfall),
            "jobless": int(jsnap.jobless),
            "by_role": {role: list(pair) for role, pair in jsnap.per_role.items()},
        }

        # Production / consumption: re-derive from gross_production +
        # building defs. _calc_production already stashed gross_production
        # on the economy so we just clone it.
        gross_prod: dict[str, float] = dict(getattr(eco, "gross_production", {}) or {})
        # Re-aggregate consumption from building_status × registry.
        gross_cons: dict[str, float] = {}
        for row in buildings_rows:
            bd = game.registry.get(row["id"])
            if bd is None:
                continue
            throttle = row["throttle"]
            for r_id, amt in bd.consumption.items():
                gross_cons[r_id] = gross_cons.get(r_id, 0) + amt * throttle

        # Resources snapshot (round numerics for compactness).
        res_snap = {k: int(v) for k, v in eco.resources.items()}

        # Per-citizen view: every citizen needs 1 food/tick by design.
        food_eaten = (
            res_snap.get("bread", 0)  # current pool; not the eaten count
            # The eaten count this tick isn't directly stored; we
            # approximate with min(pool, population) which is the
            # post-eating residual + this-tick demand. fed_fraction
            # is the authoritative number.
        )
        per_citizen = {
            "food_demand_per_tick": 1.0,
            "fed_fraction": round(float(eco.fed_fraction), 3),
        }

        # v0.22: military readiness block. Surfaces the soldier
        # equipment / morale state and the active raid pressure so a
        # JSONL recording can be analysed for "did the chain break
        # before the raid?" questions. Keys:
        #   armed / unarmed: per-soldier counts (sums to soldier count).
        #   morale_avg: arithmetic mean over all friendly soldiers
        #               (None when no soldiers — the recorder is happy
        #               with nulls; pandas handles them).
        #   retreating: count of soldiers currently below morale or HP
        #               retreat thresholds.
        #   garrisons: number of barracks/fort buildings on the map.
        #   weapons_stock: short-cut readout (also in `resources`, but
        #                  pulled out so a quick `jq .military.weapons_stock`
        #                  works without unpacking the resource dict).
        #   combat_events: per-tick combat hits drained from the
        #                  walker manager since the last record. Each
        #                  is (attacker_role, defender_role, damage,
        #                  row, col) — same shape the HUD reads.
        from walkers import Soldier as _Soldier
        soldiers = [w for w in wm.walkers if isinstance(w, _Soldier)]
        n_armed = sum(1 for s in soldiers if getattr(s, "armed", True))
        n_unarmed = len(soldiers) - n_armed
        morale_vals = [
            getattr(s, "morale", None) for s in soldiers
            if getattr(s, "morale", None) is not None
        ]
        morale_avg: float | None = (
            round(sum(morale_vals) / len(morale_vals), 3)
            if morale_vals else None
        )
        n_retreating = sum(
            1 for s in soldiers
            if hasattr(s, "is_retreating") and s.is_retreating()
        )
        n_garrisons = sum(
            1 for bt, _r, _c in game.game_map.get_building_positions()
            if bt in ("barracks", "fort")
        )
        # combat events: peek at the manager's per-tick scratch. We
        # serialize tuples as lists so the JSON survives round-trip.
        try:
            ce = wm.combat_events()
        except Exception:
            ce = []
        military_block = {
            "soldiers":      len(soldiers),
            "armed":         n_armed,
            "unarmed":       n_unarmed,
            "retreating":    n_retreating,
            "morale_avg":    morale_avg,
            "garrisons":     n_garrisons,
            "weapons_stock": int(eco.resources.get("weapons", 0)),
            "iron_ore_stock": int(eco.resources.get("iron_ore", 0)),
            "iron_stock":    int(eco.resources.get("iron", 0)),
            "enemies_active": int(walker_counts.get("enemy", 0)),
            "combat_events": [list(t) for t in ce],
        }

        # v0.22: diagnostics block — every broken chain plus its first
        # root-cause entry. The diagnose_all() walker is the same one
        # the 'D' panel uses; recording it lets a session replay tell
        # you exactly when the weapons chain broke (and why).
        diagnostics_block: list[dict] = []
        try:
            from diagnostics import ProductionDiagnostics
            diag = ProductionDiagnostics(
                game.game_map, game.registry, eco,
                walker_manager=wm,
                service_map=getattr(game, "service_map", None),
                storage=getattr(game, "storage", None),
                rebellion=getattr(game, "rebellion", None),
                decay=getattr(game, "decay", None),
            )
            for report in diag.diagnose_all():
                if not report.is_broken or not report.entries:
                    continue
                root = report.entries[0]
                diagnostics_block.append({
                    "resource": report.resource,
                    "stage":    root.stage,
                    "cause":    root.cause,
                    "severity": int(root.severity),
                })
        except Exception:
            # The diagnostics module is pure-logic but tests mock parts
            # of the world; keep recording running even if it raises.
            log.exception(
                "Recorder: diagnostics snapshot failed at tick %s",
                getattr(game, "game_time", "?"),
            )

        # v0.22: rebellion / caesar / diplomacy snapshots — useful
        # context for understanding *why* a chain broke. Each is
        # gracefully optional; fields default to None when the
        # subsystem isn't wired in (test stubs).
        try:
            reb = game.rebellion
            rebellion_block = {
                "pressure":      round(float(getattr(reb, "pressure", 0.0)), 3),
                "ratio":         round(float(reb.ratio() if hasattr(reb, "ratio") else 0.0), 3),
                "active_rebels": sum(
                    1 for w in wm.walkers
                    if getattr(w, "role", "") == "rebel"
                ),
            }
        except Exception:
            rebellion_block = None
        try:
            caes = game.caesar
            caesar_block = {
                "favor":  int(getattr(caes, "favor", 0)),
                "request_pending": caes.current is not None,
                "request": caes.current if caes.current else None,
            }
        except Exception:
            caesar_block = None

        # Build & drain the pending blocks.
        barters = list(self._pending_barters)
        events = list(self._pending_events)
        self._pending_barters.clear()
        self._pending_events.clear()

        # Calendar.
        from game_window import MONTH_NAMES, SEASON_BY_MONTH
        season = SEASON_BY_MONTH.get(game.month, "")
        return {
            "tick": int(game.game_time),
            "year": int(game.year),
            "month": int(game.month),
            "season": season,
            "wall_time": _dt.datetime.now().isoformat(timespec="seconds"),
            "population": int(eco.population),
            "treasury": int(eco.treasury),
            "happiness": round(float(eco.happiness), 2),
            "fed_fraction": round(float(eco.fed_fraction), 3),
            "nutrient_diversity": int(eco.nutrient_diversity),
            "income_per_tick": round(float(eco.income_per_tick), 2),
            "expenses_per_tick": round(float(eco.expenses_per_tick), 2),
            "resources": res_snap,
            "production": {k: round(float(v), 2) for k, v in gross_prod.items()},
            "consumption": {k: round(float(v), 2) for k, v in gross_cons.items()},
            "jobs": jobs_block,
            "buildings": buildings_rows,
            "walkers": dict(walker_counts),
            "per_citizen": per_citizen,
            # v0.22: military readiness, broken-chain diagnostics, and
            # rebellion/caesar snapshots. Each is None or [] when the
            # corresponding subsystem isn't active, so analysis tools
            # can `pandas.json_normalize` without surprises.
            "military":    military_block,
            "diagnostics": diagnostics_block,
            "rebellion":   rebellion_block,
            "caesar":      caesar_block,
            "barters_since_last": barters,
            "events_since_last": events,
        }
