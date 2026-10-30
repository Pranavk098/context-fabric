"""Path expansion helpers."""

from __future__ import annotations

import os

DEFAULT_EXPAND_VARS = False


def expand_path_vars(
    value: str | os.PathLike[str], enabled: bool
) -> str | os.PathLike[str]:
    """Expand environment variables in a path when ``enabled`` is true."""
    if not isinstance(enabled, bool):
        raise TypeError("expand_vars must be a bool")

    if not enabled:
        return value

    return os.path.expandvars(value)
