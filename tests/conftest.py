"""Repository-wide pytest config.

Adds the project root to ``sys.path`` so stand-alone packages
living outside the main distribution (``event_engine``, for
example) can be imported during the test collection phase.

Also tightens a few warnings for clarity in test output.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Diagnostic so we can prove conftest loaded on slow CI.
import os  # noqa: E402

if os.environ.get("EVENT_ENGINE_DEBUG_CONFTEST") == "1":
    print(f"[conftest] inserted {_ROOT} into sys.path[0]")
