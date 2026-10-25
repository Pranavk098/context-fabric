"""Console output safety: never crash on a name the terminal's code page cannot encode.

Windows consoles often use a legacy code page (cp1252), where printing a CJK skill name or a
symbol such as an arrow raises ``UnicodeEncodeError``. Switch the streams to UTF-8 and replace
anything that still cannot be written.
"""

from __future__ import annotations

import sys


def ensure_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
