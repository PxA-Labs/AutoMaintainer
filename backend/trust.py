"""Alias: ``trust`` is ``automaintainer._runtime.trust`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import trust`` and ``mock.patch("trust.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import trust as _module

sys.modules[__name__] = _module
