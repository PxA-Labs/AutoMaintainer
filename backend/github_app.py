"""Alias: ``github_app`` is ``automaintainer._runtime.github_app`` (#187).

The module moved into the automaintainer package so the wheel installs no
generic top-level names. This file makes the old name the same module object
(not a copy), so ``import github_app`` and ``mock.patch("github_app.<name>")`` reach the
code that actually runs. New code imports the package path.
"""

import sys

from automaintainer._runtime import github_app as _module

sys.modules[__name__] = _module
