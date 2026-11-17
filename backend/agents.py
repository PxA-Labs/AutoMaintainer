"""Alias: ``agents`` is ``automaintainer._runtime.agents`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import agents`` and ``mock.patch("agents.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import agents as _module

sys.modules[__name__] = _module
