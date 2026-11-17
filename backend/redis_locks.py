"""Alias: ``redis_locks`` is ``automaintainer._runtime.redis_locks`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import redis_locks`` and ``mock.patch("redis_locks.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import redis_locks as _module

sys.modules[__name__] = _module
