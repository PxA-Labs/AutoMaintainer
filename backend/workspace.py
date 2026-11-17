"""Alias: ``workspace`` is ``automaintainer._runtime.workspace`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import workspace`` and ``mock.patch("workspace.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import workspace as _module

sys.modules[__name__] = _module
