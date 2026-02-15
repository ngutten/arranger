"""Signal graph editor package.

Public surface:
  GraphModel              – data model (nodes + connections)
  GraphNode, GraphConnection, PortDef, PortType  – model primitives
  GraphEditorWindow       – the popup editor window
  NodeGraphCanvas         – the canvas widget (for embedding if needed)
"""

from .graph_model import (
    GraphModel, GraphNode, GraphConnection,
    PortDef, PortType,
    set_plugin_descriptors, get_plugin_descriptor, plugin_id_for_type,
)
from .node_canvas import NodeGraphCanvas
from .graph_editor_window import GraphEditorWindow

__all__ = [
    "GraphModel", "GraphNode", "GraphConnection", "PortDef", "PortType",
    "NodeGraphCanvas", "GraphEditorWindow",
    "set_plugin_descriptors", "get_plugin_descriptor", "plugin_id_for_type",
]
