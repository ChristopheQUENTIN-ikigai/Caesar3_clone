"""RPG requests — author-facing NPC dialogue library.

An *RPG request* is a single NPC interaction that pauses the game,
shows a background scene + character portrait + a paragraph of text,
and presents the player with a list of decision buttons. Each
decision can:

  * Move a resource bundle (`effects`: ``{resource_id: int_delta}``,
    same shape the trigger editor uses).
  * Delay the actual application by N ticks (so "Yes for next month"
    deducts the cost later, not now). 0 means immediate.
  * Set a string flag (`set_flag`) the rest of the simulation can
    key on — "caesar_tribute_paid", "oracle_approved", etc.
  * Fire a named event (`fire_event`) — refers to an event by name
    in ``events.json``. Wires this module into the existing v0.28
    event manager so a Refused tribute can trigger a "caesar_
    displeased" debuff.

The on-disk schema (``data/rpg_requests.json``) is a list of dicts.
A single request looks like::

    {
      "id": "caesar_tribute_1k",
      "npc_name": "MESSENGER",
      "npc_portrait": "messenger",
      "background_scene": "throne_room",
      "request_text": "Caesar demands a tribute of 1000 golden coins.",
      "resources": {"money": -1000},   // preview, shown to player
      "trigger_flag": "caesar_tribute_paid",  // default flag, can be
                                              // overridden per decision
      "trigger_event": "caesar_displeased",   // default event, ditto
      "decisions": [
        {"label": "Yes immediately",
         "delay_ticks": 0,
         "effects": {"money": -1000},
         "set_flag": "caesar_tribute_paid",
         "fire_event": null,
         "happy": 0},
        ...
      ]
    }

The editor (``rpg_request_editor.py``) reads + writes this file with
the same ``.tmp → rename`` atomic pattern as the buildings + trigger
editors. The runtime dispatcher (TBD; the trigger editor's
``fire_request`` effect will be wired in v0.32) reads it at world
init and matches by ``id``.

This module deliberately stays loader-only. The dispatcher / runtime
display will live in a sibling module so that the editor can be
opened from the splash menu without dragging the whole game world
into the import graph (mirror of ``events.load_events`` vs
``EconomicEventManager``).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from constants import RPG_REQUESTS_PATH

log = logging.getLogger(__name__)


# ── Schema defaults ──────────────────────────────────────────────────
# Used by the editor when it materialises a fresh blank request via
# the "+ Add" button, and by ``load_requests`` to fill in missing
# fields so the editor never has to dict-vs-None check.
DEFAULT_REQUEST: dict[str, Any] = {
    "id": "new_request",
    "npc_name": "MESSENGER",
    "npc_portrait": "messenger",
    "background_scene": "throne_room",
    "request_text": "",
    "resources": {},
    "trigger_flag": "",
    "trigger_event": None,
    "decisions": [],
}

DEFAULT_DECISION: dict[str, Any] = {
    "label": "Yes",
    "delay_ticks": 0,
    "effects": {},
    "set_flag": "",
    "fire_event": None,
    "happy": 0,
    # v0.32: per-decision diplomacy delta. Integer points, positive
    # rewards (improved standing with Rome), negative penalises. Wired
    # into the runtime dispatcher (when v0.32 ships) via
    # DiplomacyTracker.add(). Stored separately from `happy` because
    # the two stats answer different questions — happy = "do my
    # citizens like me", diplomacy = "does Caesar trust me" — and
    # different decisions move them by different amounts (refusing
    # Caesar's tribute spikes citizen happiness but tanks diplomacy).
    "diplomacy": 0,
}


def load_requests(path: str | Path = RPG_REQUESTS_PATH) -> list[dict[str, Any]]:
    """Load the RPG-request library from disk.

    Missing file → empty list (so a fresh checkout can still open the
    editor and start authoring). Malformed JSON → empty list + log;
    we don't want to crash the splash menu over a typo'd comma. The
    editor sees the empty list and the user can rebuild from scratch.

    Every returned dict is normalised so the editor never sees
    ``None`` where it expects a list/dict (mirrors the same defensive
    setdefault pass the trigger editor does for events.json).
    """
    p = Path(path)
    if not p.is_file():
        log.info("rpg_requests: %s does not exist — returning empty list", p)
        return []
    try:
        with open(p, "r") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        log.error("rpg_requests: failed to parse %s: %s", p, e)
        return []
    except OSError as e:
        log.error("rpg_requests: failed to read %s: %s", p, e)
        return []
    if not isinstance(raw, list):
        log.error("rpg_requests: %s is not a list — got %s", p, type(raw))
        return []
    out: list[dict[str, Any]] = []
    for i, req in enumerate(raw):
        if not isinstance(req, dict):
            log.warning("rpg_requests: entry %d is not a dict — skipping", i)
            continue
        req = dict(req)
        for k, v in DEFAULT_REQUEST.items():
            req.setdefault(k, v if not isinstance(v, (dict, list)) else type(v)())
        # Normalise nested containers so the editor can mutate them.
        req["resources"] = dict(req.get("resources") or {})
        decs = []
        for d in (req.get("decisions") or []):
            if not isinstance(d, dict):
                continue
            d = dict(d)
            for k, v in DEFAULT_DECISION.items():
                d.setdefault(k, v if not isinstance(v, (dict, list)) else type(v)())
            d["effects"] = dict(d.get("effects") or {})
            decs.append(d)
        req["decisions"] = decs
        out.append(req)
    log.info("rpg_requests: loaded %d requests from %s", len(out), p)
    return out


def save_requests(
    requests: list[dict[str, Any]],
    path: str | Path = RPG_REQUESTS_PATH,
) -> None:
    """Write the request library to disk atomically.

    Uses the same ``.tmp → os.replace`` pattern as the buildings and
    trigger editors so a crash mid-write never corrupts the user's
    in-progress data file.

    The output is cleaned (empty resources / flags / events dropped)
    so the on-disk JSON stays tight, matching the trigger editor's
    handling of optional ``duration_ticks`` / ``modifiers``.
    """
    import os
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cleaned: list[dict[str, Any]] = []
    for req in requests:
        entry: dict[str, Any] = {
            "id": str(req.get("id", "")).strip() or "request",
            "npc_name": str(req.get("npc_name", "")),
            "npc_portrait": str(req.get("npc_portrait", "")),
            "background_scene": str(req.get("background_scene", "")),
            "request_text": str(req.get("request_text", "")),
        }
        res = {k: int(v) for k, v in (req.get("resources") or {}).items()
               if isinstance(v, (int, float))}
        if res:
            entry["resources"] = res
        flag = str(req.get("trigger_flag") or "").strip()
        if flag:
            entry["trigger_flag"] = flag
        ev = req.get("trigger_event")
        if ev:
            entry["trigger_event"] = str(ev)
        decs: list[dict[str, Any]] = []
        for d in (req.get("decisions") or []):
            dec: dict[str, Any] = {
                "label": str(d.get("label", "")),
                "delay_ticks": int(d.get("delay_ticks", 0) or 0),
                "effects": {k: int(v) for k, v in (d.get("effects") or {}).items()
                            if isinstance(v, (int, float))},
                "happy": int(d.get("happy", 0) or 0),
                # v0.32: per-decision diplomacy delta. Mirrors the happy
                # field — always persisted (even when zero) so the
                # author can see at a glance that the decision is
                # diplomatically neutral rather than missing data.
                "diplomacy": int(d.get("diplomacy", 0) or 0),
            }
            sf = str(d.get("set_flag") or "").strip()
            if sf:
                dec["set_flag"] = sf
            fe = d.get("fire_event")
            if fe:
                dec["fire_event"] = str(fe)
            decs.append(dec)
        entry["decisions"] = decs
        cleaned.append(entry)
    tmp = str(p) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cleaned, f, indent=2)
    os.replace(tmp, p)
    log.info("rpg_requests: wrote %s (%d requests)", p, len(cleaned))


def make_blank_request(existing_ids: list[str] | None = None) -> dict[str, Any]:
    """Materialise a fresh request dict for the editor's "+ Add" button.

    Deep-copies the defaults so the editor's later in-place mutations
    don't poison module state. If an ``existing_ids`` list is passed,
    appends a numeric suffix so the new request has a unique id from
    the moment it appears in the list (the editor doesn't validate
    uniqueness until Save — but a unique seed avoids the surprise of
    typing into "new_request" and finding two of them).
    """
    import copy
    req = copy.deepcopy(DEFAULT_REQUEST)
    if existing_ids:
        base = req["id"]
        suffix = 1
        while f"{base}_{suffix}" in existing_ids:
            suffix += 1
        req["id"] = f"{base}_{suffix}"
    return req


def make_blank_decision() -> dict[str, Any]:
    """Materialise a fresh decision dict for the editor's per-request
    "+ Add decision" button."""
    import copy
    return copy.deepcopy(DEFAULT_DECISION)
