"""Command alias helpers."""

from __future__ import annotations

import collections.abc as cabc
import typing as t

ALIAS_HELP_SEPARATOR = " / "

if t.TYPE_CHECKING:
    from .core import Command


def find_alias(commands: cabc.Mapping[str, "Command"], name: str) -> "Command | None":
    """Return the command whose ``aliases`` contain ``name``, if any."""
    for command in commands.values():
        if name in getattr(command, "aliases", ()):
            return command
    return None
