"""Alias: ``retrieval`` is ``automaintainer._runtime.retrieval`` (#526).

Every runtime module is importable under its top-level name from backend/
(see ast_indexer.py), so ``import retrieval`` and
``mock.patch("retrieval.<name>")`` reach the code that actually runs. New
code imports the package path.
"""

import sys

from automaintainer._runtime import retrieval as _module

sys.modules[__name__] = _module
