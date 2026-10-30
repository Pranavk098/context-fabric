"""Choice alias and visibility helpers."""

from __future__ import annotations

import collections.abc as cabc
import typing as t

ALIAS_MAP_NAME = "aliases"


def build_alias_map(
    choices: cabc.Sequence[t.Any], aliases: cabc.Mapping[str, t.Any]
) -> dict[str, t.Any]:
    """Validate aliases and return them as a plain mapping."""
    mapping: dict[str, t.Any] = {}
    choice_set = set(choices)

    for alias, target in aliases.items():
        if not alias:
            raise ValueError("alias must be a non-empty string")
        if alias in choice_set:
            raise ValueError(f"alias {alias!r} conflicts with a choice")
        if target not in choice_set:
            raise ValueError(f"alias {alias!r} targets an unknown choice")
        mapping[alias] = target

    return mapping


def visible_choices(
    choices: cabc.Sequence[t.Any], hidden: cabc.Iterable[t.Any]
) -> tuple[t.Any, ...]:
    """Return choices that may appear in help and completion output."""
    hidden_set = set(hidden)
    return tuple(choice for choice in choices if choice not in hidden_set)
