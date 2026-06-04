"""Minimal pub/sub. Lets features react to game events without engine edits.

Usage:
    from signals import signals

    def on_built(building_id, row, col):
        print(f"Built {building_id} at {row},{col}")

    signals.connect("building_placed", on_built)
    signals.emit("building_placed", "house", 12, 8)

Standard signal names emitted by the engine:
    building_placed   (building_id, row, col)
    building_removed  (building_id, row, col)
    event_triggered   (event_dict)
    tick              (tick_count)
    game_loaded       ()
    game_saved        (path)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

log = logging.getLogger("caesar3.signals")


class SignalBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def connect(self, name: str, handler: Callable) -> None:
        self._handlers[name].append(handler)

    def disconnect(self, name: str, handler: Callable) -> None:
        if handler in self._handlers.get(name, []):
            self._handlers[name].remove(handler)

    def emit(self, name: str, *args, **kwargs) -> None:
        for h in self._handlers.get(name, []):
            try:
                h(*args, **kwargs)
            except Exception:
                # A misbehaving listener must never break the engine.
                log.exception("Signal handler failed for %s", name)

    def clear(self, name: str | None = None) -> None:
        """Remove all handlers (used by tests). If name=None, clear everything."""
        if name is None:
            self._handlers.clear()
        else:
            self._handlers.pop(name, None)


# Global bus. Tests can call signals.clear() between cases.
signals = SignalBus()
