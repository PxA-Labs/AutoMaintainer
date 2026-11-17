"""AutoMaintainer engine package (epic #157).

Importing it has no side effects. ``AutoMaintainerConfig`` is imported
eagerly; the engine (which imports the agent runtime) only on first access
of ``AutoMaintainerEngine`` or ``RunResult``, so importing the config alone
never loads the runtime modules.
"""

from .config import AutoMaintainerConfig

__all__ = ["AutoMaintainerConfig", "AutoMaintainerEngine", "RunResult"]


def __getattr__(name: str):
    if name in ("AutoMaintainerEngine", "RunResult"):
        from . import engine

        return getattr(engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
