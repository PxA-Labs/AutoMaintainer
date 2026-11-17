"""Alias: ``rate_limiter`` is ``automaintainer._runtime.rate_limiter`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import rate_limiter`` and ``mock.patch("rate_limiter.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import rate_limiter as _module

sys.modules[__name__] = _module
