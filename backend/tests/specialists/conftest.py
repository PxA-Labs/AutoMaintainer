"""Shared setup of the specialist-agent suite (#525).

The backend directory goes on ``sys.path`` (as in tests/concurrency) so the
suite can import the root modules and the fixtures of other suites.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
for path in (BACKEND, BACKEND / "tests" / "redteam"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
