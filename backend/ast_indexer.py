"""Alias: ``ast_indexer`` is ``automaintainer._runtime.ast_indexer`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import ast_indexer`` and ``mock.patch("ast_indexer.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import ast_indexer as _module

sys.modules[__name__] = _module
