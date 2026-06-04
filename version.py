"""Project version — single source of truth.

The string lives in one place so the splash screen, log lines, save-file
metadata, and any future "About" panel all agree. Bump this on every
release; pre-release work-in-progress reads the same value as the last
shipped tag.

Imported by:
    * mixins/splash_menu.py — bottom-left version overlay
    * game_window.py        — fallback version label, save-file stamp

Format: "vMAJOR.MINOR" where MINOR increments per shipped milestone
(matches the CHANGELOG_v0.* file naming). We deliberately keep the
``v`` prefix in the string so callers can render it directly without
prepending it themselves.
"""
from __future__ import annotations

# Bump on every release. Anything that wants to display the version
# imports VERSION from here rather than hard-coding the literal.
VERSION: str = "v0.56"
