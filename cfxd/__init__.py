"""Context Fabric daemon (``cfxd``): a resident, fail-open hook endpoint.

Claude Code points its http hooks at the daemon; command-hook clients
(OpenCode/Codex) forward through ``cfxd/shim.py``.
"""

from __future__ import annotations

__version__ = "0.1.0a1"
