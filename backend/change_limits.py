"""Alias: ``change_limits`` is ``automaintainer._runtime.change_limits`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import change_limits`` and ``mock.patch("change_limits.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import change_limits as _module

sys.modules[__name__] = _module
