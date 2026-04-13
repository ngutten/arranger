"""UI layer for the song-plugins system.

Public entry point: :class:`PluginHost` plus :class:`PluginsDock`.
"""

from .broadcast_band import BroadcastBand
from .host import PluginHost
from .plugins_dock import PluginsDock

__all__ = ["PluginHost", "PluginsDock", "BroadcastBand"]
