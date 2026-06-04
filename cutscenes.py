"""Cutscenes — author-facing cinematic slide-deck library.

A *cutscene* is a sequence of full-screen informational slides shown
between gameplay beats. The player sees one slide at a time with a
"Next" button (or "OK" on the final slide) and reads through the
story at their own pace. Use cases: opening narration, mission
briefing, post-victory celebration, doom-and-gloom warning when a
trigger flag flips.

The on-disk schema (``data/cutscenes.json``) is a list of dicts. A
single cutscene looks like::

    {
      "id": "intro_gallic_war",
      "title": "The Gallic War Begins",
      "trigger_flag": "gallic_war_started",    // optional — fired
                                               // when this flag flips
      "slides": [
        {
          "image": "throne_room",   // basename in assets/textures/scene/
          "caption": "Year 58 BC",
          "body": "Caesar stands before the senate, sword in hand.",
        },
        {
          "image": "battlefield",
          "caption": "Across the Rhone",
          "body": "The legions march north…",
        },
      ]
    }

Why a separate file from ``rpg_requests.json``? RPG requests are
interactive (the player picks one of N decisions, each with resource
effects) — cutscenes are passive (the player reads + clicks Next).
Mixing the two in one file would force every entry to carry the union
of fields, and the editors would have to disambiguate by shape. Two
files, two schemas, two editors — clean.

This module is loader-only. The runtime player lives in
``cutscene_player.py`` and the splash editor in ``cutscene_editor.py``;
both round-trip through the functions below.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from constants import CUTSCENES_PATH

log = logging.getLogger(__name__)


# ── Schema defaults ──────────────────────────────────────────────────
DEFAULT_CUTSCENE: dict[str, Any] = {
    "id": "new_cutscene",
    "title": "Untitled Cutscene",
    "trigger_flag": "",
    "slides": [],
}

DEFAULT_SLIDE: dict[str, Any] = {
    "image": "",       # basename under assets/textures/scene/
    "caption": "",     # short header above the body text
    "body": "",        # main paragraph; rendered with word-wrap
}


def load_cutscenes(path: str | Path = CUTSCENES_PATH) -> list[dict[str, Any]]:
    """Load the cutscene library from disk.

    Missing file → empty list (so a fresh checkout can open the editor
    and start authoring). Malformed JSON → empty list + log. Every
    returned dict is normalised so the editor never has to dict-vs-None
    check nested containers.
    """
    p = Path(path)
    if not p.is_file():
        log.info("cutscenes: %s does not exist — returning empty list", p)
        return []
    try:
        with open(p, "r") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        log.error("cutscenes: failed to parse %s: %s", p, e)
        return []
    except OSError as e:
        log.error("cutscenes: failed to read %s: %s", p, e)
        return []
    if not isinstance(raw, list):
        log.error("cutscenes: %s is not a list — got %s", p, type(raw))
        return []
    out: list[dict[str, Any]] = []
    for i, cs in enumerate(raw):
        if not isinstance(cs, dict):
            log.warning("cutscenes: entry %d is not a dict — skipping", i)
            continue
        cs = dict(cs)
        for k, v in DEFAULT_CUTSCENE.items():
            cs.setdefault(k, v if not isinstance(v, list) else list(v))
        slides: list[dict[str, Any]] = []
        for s in (cs.get("slides") or []):
            if not isinstance(s, dict):
                continue
            s = dict(s)
            for k, v in DEFAULT_SLIDE.items():
                s.setdefault(k, v)
            slides.append(s)
        cs["slides"] = slides
        out.append(cs)
    log.info("cutscenes: loaded %d cutscenes from %s", len(out), p)
    return out


def save_cutscenes(
    cutscenes: list[dict[str, Any]],
    path: str | Path = CUTSCENES_PATH,
) -> None:
    """Write the cutscene library to disk atomically.

    Uses the same ``.tmp → os.replace`` pattern as the other editors
    so a crash mid-write never corrupts the user's in-progress data.
    The output is cleaned: empty trigger flags dropped, slides with
    no image AND no body text dropped (so the on-disk JSON stays tight
    and `+ Add slide` clicks don't leave stub rows behind).
    """
    import os
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cleaned: list[dict[str, Any]] = []
    for cs in cutscenes:
        entry: dict[str, Any] = {
            "id": str(cs.get("id", "")).strip() or "cutscene",
            "title": str(cs.get("title", "")),
        }
        flag = str(cs.get("trigger_flag") or "").strip()
        if flag:
            entry["trigger_flag"] = flag
        slides: list[dict[str, Any]] = []
        for s in (cs.get("slides") or []):
            slide: dict[str, Any] = {
                "image": str(s.get("image", "")),
                "caption": str(s.get("caption", "")),
                "body": str(s.get("body", "")),
            }
            # Drop completely-blank slides — they're scaffolding the
            # author never filled in. A slide with only an image (no
            # text) is fine — that's a silent transition.
            if not slide["image"] and not slide["body"] and not slide["caption"]:
                continue
            slides.append(slide)
        entry["slides"] = slides
        cleaned.append(entry)
    tmp = str(p) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cleaned, f, indent=2)
    os.replace(tmp, p)
    log.info("cutscenes: wrote %s (%d cutscenes)", p, len(cleaned))


def make_blank_cutscene(existing_ids: list[str] | None = None) -> dict[str, Any]:
    """Materialise a fresh cutscene dict for the editor's "+ Add"
    button. Same uniquify-by-suffix trick as ``make_blank_request``."""
    import copy
    cs = copy.deepcopy(DEFAULT_CUTSCENE)
    if existing_ids:
        base = cs["id"]
        suffix = 1
        while f"{base}_{suffix}" in existing_ids:
            suffix += 1
        cs["id"] = f"{base}_{suffix}"
    return cs


def make_blank_slide() -> dict[str, Any]:
    """Materialise a fresh slide dict for the editor's per-cutscene
    "+ Add slide" button."""
    import copy
    return copy.deepcopy(DEFAULT_SLIDE)


def find_cutscene_for_flag(
    cutscenes: list[dict[str, Any]], flag: str,
) -> dict[str, Any] | None:
    """Return the first cutscene whose ``trigger_flag`` matches ``flag``,
    or ``None`` if no cutscene triggers on that flag. Used by the runtime
    dispatcher to look up "which cutscene should fire when the player
    sets the gallic_war_started flag?" — multiple cutscenes can share a
    flag in principle, but the runtime fires the first match (define
    order) and ignores the rest."""
    if not flag:
        return None
    for cs in cutscenes:
        if cs.get("trigger_flag") == flag:
            return cs
    return None
