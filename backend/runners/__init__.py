"""Alias: ``runners`` is ``automaintainer.runners`` (#187).

The runner package moved into the automaintainer package so the wheel
installs no generic top-level names. The old package name and each of its
submodules are made the same module objects (not copies), so ``import
runners.local``, ``mock.patch("runners.local.<name>")`` and the documented
``SANDBOX_BACKEND=runners.docker_sandbox:create`` reach the code that actually
runs. New code imports ``automaintainer.runners``.
"""

import sys

import automaintainer.runners as _package
from automaintainer.runners import base, docker_sandbox, local, sandbox

# Every submodule of automaintainer.runners; test_sdk_package checks that this
# list is complete, since an unaliased one would load a second copy.
for _module in (base, docker_sandbox, local, sandbox):
    sys.modules[f"{__name__}.{_module.__name__.rsplit('.', 1)[1]}"] = _module

sys.modules[__name__] = _package
