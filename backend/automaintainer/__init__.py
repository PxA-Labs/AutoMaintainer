"""AutoMaintainer engine package and Python SDK (epic #157, #187).

    from automaintainer import AutoMaintainerConfig, AutoMaintainerEngine

    engine = AutoMaintainerEngine(AutoMaintainerConfig(...))
    result = await engine.run("owner/repo", target_issue=42)

Importing it has no side effects and loads none of the engine runtime
(``automaintainer._runtime``: agents, GitHub, Supabase, LangGraph) or the
runners: every public name below is resolved on first access through the
module ``__getattr__``. The runtime modules are private; the stable surface
is this module and ``automaintainer.runners``.
"""

from __future__ import annotations

import importlib

# Single source of the version: hatchling reads this line when building the
# wheel ([tool.hatch.version] in pyproject.toml).
__version__ = "0.1.0"

# Public name -> submodule that defines it.
_EXPORTS = {
    "AutoMaintainerConfig": "config",
    "AutoMaintainerEngine": "engine",
    "RunResult": "engine",
    "RunAlreadyActive": "engine",
    "cancel_active_run": "engine",
    "BaseRunner": "runners",
    "LocalRunner": "runners",
    "SandboxRunner": "runners",
    "SandboxBackend": "runners",
    "RunnerError": "runners",
    "PathTraversalError": "runners",
    "CommandTimeoutError": "runners",
    "WorkspaceError": "runners",
}

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str):
    submodule = _EXPORTS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{submodule}", __name__), name)
    # Cache it so the next access skips __getattr__.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
