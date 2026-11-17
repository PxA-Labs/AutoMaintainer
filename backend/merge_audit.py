"""Alias: ``merge_audit`` is ``automaintainer._runtime.merge_audit`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import merge_audit`` and ``mock.patch("merge_audit.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import merge_audit as _module

sys.modules[__name__] = _module
