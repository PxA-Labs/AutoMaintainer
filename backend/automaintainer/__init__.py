"""AutoMaintainer engine package (epic #157).

Importing it has no side effects. For now it only holds the engine
configuration; the runtime does not use it yet.
"""

from .config import AutoMaintainerConfig

__all__ = ["AutoMaintainerConfig"]
