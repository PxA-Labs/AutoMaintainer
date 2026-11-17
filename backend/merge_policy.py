"""Alias: ``merge_policy`` is ``automaintainer._runtime.merge_policy`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import merge_policy`` and ``mock.patch("merge_policy.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import merge_policy as _module

sys.modules[__name__] = _module
