"""Action recorder — append-only JSONL log of *player decisions*. (v0.55)

The companion to `recorder.py`. Where `TickRecorder` snapshots the
**world state** every tick (population, treasury, buildings, …), this
records **what the player did** and on which tick they did it:

    {"tick": 412, "action": "place", "building": "house",
     "row": 10, "col": 8, "cost": 30, "ok": true}
    {"tick": 418, "action": "demolish", "row": 12, "col": 5,
     "removed": "farm", "refund": 40}
    {"tick": 440, "action": "set_tax", "rate": 0.08}
    {"tick": 455, "action": "barter", "give": "wheat", "give_qty": 10,
     "recv": "bread", "recv_qty": 5, "fee": 100}
    {"tick": 470, "action": "yield_to_caesar"}

Why a second file rather than folding actions into the state record?

  * **Cadence.** State is one record per *tick*; actions are sparse and
    event-driven (zero or many per tick). Keeping them separate avoids
    bloating every tick row with an almost-always-empty "actions" list.
  * **Join key.** Both logs carry ``tick`` from the same ``game_time``
    clock, so ``pandas.merge(state, actions, on="tick")`` reconstructs
    the full ``(observation, action)`` stream imitation learning needs.
  * **Same session.** Both recorders start/stop on the same Ctrl+R
    toggle and stamp the same wall-clock timestamp, so a session's
    state file and action file sort next to each other in ``ls -lt``.

The schema is deliberately open: ``note(action, **fields)`` writes
whatever keyword fields the call site passes, with ``tick`` and
``wall_time`` injected automatically. New action kinds need no schema
change here — just a new ``note(...)`` call at the UI site.

Public API
----------

  ``ActionRecorder`` — ``start()`` / ``stop()`` mirror ``TickRecorder``.
  ``note(game, action, **fields)`` appends one record, tagged with the
  game's current ``game_time``. No-op while inactive, so call sites
  never have to branch on "is recording on?".
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("caesar3.action_recorder")

# Reuse the state recorder's directory so a session's two files live
# side by side. Imported lazily-safe: recorder.py has no heavy deps.
try:
    from recorder import RECORDINGS_DIR
except Exception:  # pragma: no cover - defensive; recorder always imports
    RECORDINGS_DIR = Path(__file__).resolve().parent / "data" / "recordings"

FLUSH_EVERY = 5  # actions between flush() calls — they're sparse, flush often.


class ActionRecorder:
    """Append-only JSONL recorder of player actions.

    State machine matches ``TickRecorder``: ``inactive`` -> ``recording``
    (on ``start``) -> ``inactive`` (on ``stop``). ``note`` no-ops when
    inactive.
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self._fp = None
        self._since_flush = 0

    @property
    def is_active(self) -> bool:
        return self._fp is not None

    # ── Lifecycle ────────────────────────────────────────────────────────
    def start(self, ts: str | None = None, label: str | None = None) -> Path:
        """Open a new action log. ``ts`` lets the caller pass the SAME
        timestamp the state recorder used, so the two files pair up;
        if omitted we generate our own.
        """
        if self.is_active:
            self.stop()
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = ts or _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = f"_{label}" if label else ""
        self.path = RECORDINGS_DIR / f"session_{ts}{slug}.actions.jsonl"
        self._fp = self.path.open("a", buffering=1)
        self._since_flush = 0
        log.info("Action recorder started -> %s", self.path)
        return self.path

    def stop(self) -> Path | None:
        path = self.path
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:
                log.exception("Error closing action recorder file")
            self._fp = None
            log.info("Action recorder stopped (%s)", path)
        self.path = None
        self._since_flush = 0
        return path

    # ── Recording ────────────────────────────────────────────────────────
    def note(self, game, action: str, **fields: Any) -> None:  # noqa: ANN001
        """Append one action record.

        ``game`` supplies ``game_time`` (and year/month for context).
        ``action`` is a short kind string ("place", "demolish", …).
        Any extra ``**fields`` are merged into the record verbatim.
        No-op while inactive.
        """
        if not self.is_active or self._fp is None:
            return
        try:
            rec: dict[str, Any] = {
                "tick": int(getattr(game, "game_time", 0)),
                "year": int(getattr(game, "year", 0)),
                "month": int(getattr(game, "month", 0)),
                "wall_time": _dt.datetime.now().isoformat(timespec="seconds"),
                "action": action,
            }
            rec.update(fields)
            self._fp.write(json.dumps(rec, separators=(",", ":")) + "\n")
            self._since_flush += 1
            if self._since_flush >= FLUSH_EVERY:
                self._fp.flush()
                self._since_flush = 0
        except Exception:
            log.exception("Action recorder failed to write %r; continuing",
                          action)
