"""Re-export shim: the negative-knowledge scorers moved to ``sidecar.negscore`` (2026-10-18).

v1/v2/v3 rules are identical to the historical ``evals/negscore.py``; only the home changed
so product code (``sidecar.demo``) can use them without importing ``evals``. Kept so
``evals.negscore`` imports (``evals/negscore_compare.py``, docs, external scripts) keep
working.
"""

from __future__ import annotations

from sidecar.negscore import *  # noqa: F401,F403
from sidecar.negscore import __all__ as __all__
