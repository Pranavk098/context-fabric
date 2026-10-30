"""Default-subcommand selection."""

from __future__ import annotations

DEFAULT_COMMAND_ATTR = "default_command"


def pick_default_command(
    configured: str | None, protected_args: list[str]
) -> str | None:
    """Return the configured default only when no subcommand was supplied."""
    if configured is None or protected_args:
        return None
    return configured
