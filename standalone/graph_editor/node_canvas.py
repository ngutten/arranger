"""Node graph canvas widget.

A QWidget that renders and interacts with a GraphModel.  Handles:
  - Pan (middle-mouse or click-drag on empty space)
  - Zoom (mouse wheel)
  - Node drag
  - Marquee selection
  - Click-drag port-to-port connection
  - Right-click on connection to remove
  - Delete key to remove selected nodes
  - Node body click to select; shift-click to add/remove
  - Node minimize/maximize toggle button
  - Per-node settings widgets embedded inline
  - Context menu on node body (reserved; currently "Set as default synth" for synths)

Coordinate spaces:
  scene  – logical coordinates stored in GraphNode.x / .y
  view   – screen pixels; scene_to_view / view_to_scene convert between them

Each node occupies a scene rectangle computed by _node_rect().
Port circles sit on the left (inputs) and right (outputs) edges of that rect.
"""

from __future__ import annotations

import math
from typing import Optional, Callable

from PySide6.QtWidgets import (
    QWidget, QSizePolicy, QMenu,
)
from PySide6.QtCore import Qt, QPointF, QRectF, QPoint, Signal, QTimer
from PySide6.QtGui import (
    QPainter, QPen, QBrush, QColor, QPainterPath, QFont,
    QFontMetrics, QMouseEvent, QWheelEvent, QKeyEvent,
    QCursor, QAction,
)

from .graph_model import GraphModel, GraphNode, GraphConnection, PortDef, PortType


# ---------------------------------------------------------------------------
# Visual constants
# ---------------------------------------------------------------------------

NODE_W          = 180     # base node width (scene units)
NODE_HEADER_H   = 28      # title bar height
PORT_ROW_H      = 20      # height per port row
PORT_R          = 7       # port circle radius
SETTINGS_PAD    = 6       # padding inside settings area
MIN_BUTTON_W    = 18      # minimize toggle button width
MIN_BUTTON_H    = 14

# Colours
C_BG            = QColor("#0d1117")
C_GRID          = QColor("#1c2333")
C_NODE_BG       = QColor("#1a2236")
C_NODE_BORDER   = QColor("#2a3a5c")
C_NODE_SEL      = QColor("#3a7bd5")
C_NODE_HEADER   = {
    "track_source":   QColor("#1a3a5c"),
    "control_source": QColor("#2a3a1c"),
    "fluidsynth":     QColor("#3a1a4a"),
    "sine":           QColor("#3a2a1a"),
    "sampler":        QColor("#1a3a3a"),
    "lv2":            QColor("#2a1a3a"),
    "mixer":          QColor("#1a2a3a"),
    "output":         QColor("#0d2a1a"),
    "split_stereo":   QColor("#1a2a2a"),
    "merge_stereo":   QColor("#1a2a2a"),
    "note_gate":      QColor("#2a1a3a"),
}
C_NODE_HEADER_DEFAULT = QColor("#1a2a3a")

C_PORT = {
    PortType.MIDI:       QColor("#f9ca24"),
    PortType.AUDIO:      QColor("#6bcb77"),
    PortType.AUDIO_MONO: QColor("#a8e6a3"),   # lighter green — single channel
    PortType.CONTROL:    QColor("#4d96ff"),
}
C_PORT_HOVER    = QColor("#ffffff")
C_WIRE          = QColor("#4d96ff")
C_WIRE_AUDIO    = QColor("#6bcb77")
C_WIRE_MIDI     = QColor("#f9ca24")
C_WIRE_PREVIEW  = QColor("#aaaaaa")
C_MARQUEE_FILL  = QColor(61, 122, 213, 40)
C_MARQUEE_LINE  = QColor("#3a7bd5")
C_TEXT          = QColor("#e6e6e6")
C_TEXT_DIM      = QColor("#888888")
C_DEFAULT_BADGE = QColor("#f9ca24")


# ---------------------------------------------------------------------------
# Hit-test result
# ---------------------------------------------------------------------------

class _Hit:
    NONE        = "none"
    NODE_BODY   = "node_body"
    NODE_HEADER = "node_header"   # for drag
    PORT        = "port"
    WIRE        = "wire"
    MIN_BUTTON  = "min_button"

    def __init__(self, kind=NONE, node: GraphNode = None,
                 port: PortDef = None, conn: GraphConnection = None):
        self.kind = kind
        self.node = node
        self.port = port          # PortDef
        self.conn = conn          # GraphConnection


# ---------------------------------------------------------------------------
# Node graph canvas
# ---------------------------------------------------------------------------

class NodeGraphCanvas(QWidget):
    """Interactive node graph editor canvas.

    Signals:
      graph_changed()       – emitted whenever the model is mutated
      node_settings_needed(GraphNode)  – reserved for future settings dialogs
    """

    graph_changed = Signal()
    node_right_clicked = Signal(object, QPoint)   # (GraphNode, global_pos)
    param_changed = Signal(str, str, float)        # (node_id, param_id, value) — low-latency path

    def __init__(self, model: GraphModel, parent=None,
                 settings_factory: Callable = None):
        """
        settings_factory(node, parent) -> QWidget | None
            If provided, called to create an inline settings widget for a node.
            The widget is embedded below the port rows when the node is not
            minimised.  Pass None to use the built-in default widgets.
        """
        super().__init__(parent)
        self.model = model
        self._settings_factory = settings_factory

        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(400, 300)

        # Viewport transform
        self._origin = QPointF(0.0, 0.0)  # scene point at canvas (0,0)
        self._scale  = 1.0

        # Interaction state
        self._pan_start: Optional[QPointF] = None
        self._pan_origin_start: Optional[QPointF] = None

        self._drag_node: Optional[GraphNode] = None
        self._drag_offset: QPointF = QPointF()

        self._connect_src_node: Optional[GraphNode] = None
        self._connect_src_port: Optional[PortDef]   = None
        self._connect_cursor:   QPointF              = QPointF()

        self._marquee_start: Optional[QPointF] = None
        self._marquee_end:   Optional[QPointF] = None

        self._hover_port_node: Optional[GraphNode] = None
        self._hover_port:      Optional[PortDef]   = None
        self._hover_conn:      Optional[GraphConnection] = None

        self.selected_nodes: set = set()   # node_ids

        # Inline settings widgets: node_id → QWidget
        self._settings_widgets: dict = {}
        self._rebuild_settings_widgets()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def set_model(self, model: GraphModel) -> None:
        """Replace the graph model (e.g. after load)."""
        # Remove old settings widgets
        for w in self._settings_widgets.values():
            w.setParent(None)
            w.deleteLater()
        self._settings_widgets.clear()

        self.model = model
        self.selected_nodes.clear()
        self._rebuild_settings_widgets()
        self.update()

    def frame_all(self) -> None:
        """Zoom/pan to fit all nodes in view."""
        if not self.model.nodes:
            return
        xs = [n.x for n in self.model.nodes]
        ys = [n.y for n in self.model.nodes]
        max_ws = [n.x + NODE_W for n in self.model.nodes]
        max_hs = [n.y + self._node_height(n) for n in self.model.nodes]

        margin = 60
        sx = min(xs) - margin
        sy = min(ys) - margin
        ex = max(max_ws) + margin
        ey = max(max_hs) + margin

        sw, sh = ex - sx, ey - sy
        if sw < 1 or sh < 1:
            return

        scale_x = self.width()  / sw
        scale_y = self.height() / sh
        self._scale = min(scale_x, scale_y, 2.0)
        self._origin = QPointF(sx, sy)
        self.update()

    # -----------------------------------------------------------------------
    # Coordinate helpers
    # -----------------------------------------------------------------------

    def scene_to_view(self, p: QPointF) -> QPointF:
        return QPointF(
            (p.x() - self._origin.x()) * self._scale,
            (p.y() - self._origin.y()) * self._scale,
        )

    def view_to_scene(self, p: QPointF) -> QPointF:
        return QPointF(
            p.x() / self._scale + self._origin.x(),
            p.y() / self._scale + self._origin.y(),
        )

    # -----------------------------------------------------------------------
    # Node geometry (scene units)
    # -----------------------------------------------------------------------

    def _node_height(self, node: GraphNode) -> float:
        if node.minimised:
            return NODE_HEADER_H
        ports = node.ports()
        n_ports = max(len([p for p in ports if not p.is_output]),
                      len([p for p in ports if p.is_output]))
        port_h = max(n_ports, 1) * PORT_ROW_H + SETTINGS_PAD * 2
        settings_h = self._settings_height(node)
        return NODE_HEADER_H + port_h + settings_h

    def _settings_height(self, node: GraphNode) -> float:
        w = self._settings_widgets.get(node.node_id)
        if w is None:
            return 0
        return w.sizeHint().height() + SETTINGS_PAD

    def _node_rect(self, node: GraphNode) -> QRectF:
        return QRectF(node.x, node.y, NODE_W, self._node_height(node))

    def _port_scene_pos(self, node: GraphNode, port: PortDef) -> QPointF:
        """Centre of a port circle in scene coordinates."""
        rect = self._node_rect(node)
        ins  = [p for p in node.ports() if not p.is_output]
        outs = [p for p in node.ports() if p.is_output]
        port_area_top = rect.top() + NODE_HEADER_H + SETTINGS_PAD

        if port.is_output:
            idx = outs.index(port)
            y = port_area_top + idx * PORT_ROW_H + PORT_ROW_H / 2
            return QPointF(rect.right(), y)
        else:
            idx = ins.index(port)
            y = port_area_top + idx * PORT_ROW_H + PORT_ROW_H / 2
            return QPointF(rect.left(), y)

    # -----------------------------------------------------------------------
    # Hit testing
    # -----------------------------------------------------------------------

    def _hit_test(self, scene_pos: QPointF) -> _Hit:
        # Test ports first (priority over body)
        for node in reversed(self.model.nodes):
            if node.minimised:
                # Still test minimize button
                r = self._node_rect(node)
                mb = self._min_button_rect(r)
                if mb.contains(scene_pos):
                    return _Hit(_Hit.MIN_BUTTON, node)
                # Header drag only
                header = QRectF(r.left(), r.top(), r.width(), NODE_HEADER_H)
                if header.contains(scene_pos):
                    return _Hit(_Hit.NODE_HEADER, node)
                continue

            for port in node.ports():
                pp = self._port_scene_pos(node, port)
                if (scene_pos - pp).manhattanLength() <= PORT_R * 1.8:
                    return _Hit(_Hit.PORT, node, port)

        for node in reversed(self.model.nodes):
            r = self._node_rect(node)
            mb = self._min_button_rect(r)
            if mb.contains(scene_pos):
                return _Hit(_Hit.MIN_BUTTON, node)
            if r.contains(scene_pos):
                return _Hit(_Hit.NODE_BODY, node)

        # Test wires
        for conn in self.model.connections:
            if self._wire_hit(conn, scene_pos):
                return _Hit(_Hit.WIRE, conn=conn)

        return _Hit()

    def _min_button_rect(self, node_rect: QRectF) -> QRectF:
        return QRectF(
            node_rect.right() - MIN_BUTTON_W - 4,
            node_rect.top() + (NODE_HEADER_H - MIN_BUTTON_H) / 2,
            MIN_BUTTON_W, MIN_BUTTON_H,
        )

    def _wire_hit(self, conn: GraphConnection, pos: QPointF, thresh: float = 6.0) -> bool:
        src_node = self.model.get_node(conn.from_node)
        dst_node = self.model.get_node(conn.to_node)
        if not src_node or not dst_node:
            return False
        sp = self._find_port(src_node, conn.from_port)
        dp = self._find_port(dst_node, conn.to_port)
        if not sp or not dp:
            return False
        p0 = self._port_scene_pos(src_node, sp)
        p1 = self._port_scene_pos(dst_node, dp)
        return _point_to_bezier_dist(pos, p0, p1) < thresh

    def _find_port(self, node: GraphNode, port_id: str) -> Optional[PortDef]:
        return next((p for p in node.ports() if p.port_id == port_id), None)

    # -----------------------------------------------------------------------
    # Settings widgets
    # -----------------------------------------------------------------------

    def _rebuild_settings_widgets(self) -> None:
        """Create inline settings widgets for all nodes."""
        # Remove any whose node no longer exists
        live_ids = {n.node_id for n in self.model.nodes}
        for nid in list(self._settings_widgets.keys()):
            if nid not in live_ids:
                w = self._settings_widgets.pop(nid)
                w.setParent(None)
                w.deleteLater()

        for node in self.model.nodes:
            if node.node_id not in self._settings_widgets:
                self._create_settings_widget(node)

    def _create_settings_widget(self, node: GraphNode) -> None:
        """Create (or recreate) the inline settings widget for a node."""
        # Remove existing if present
        if node.node_id in self._settings_widgets:
            old = self._settings_widgets.pop(node.node_id)
            old.setParent(None)
            old.deleteLater()

        if self._settings_factory:
            w = self._settings_factory(node, self)
        else:
            w = _make_default_settings_widget(node, self, self._on_node_param_changed)

        if w:
            w.setParent(self)
            w.hide()
            self._settings_widgets[node.node_id] = w

    def _on_node_param_changed(self, node_id: str, key: str, value) -> None:
        """Called by inline settings widgets when a param changes."""
        node = self.model.get_node(node_id)
        if node:
            node.params[key] = value
            # Regenerate mixer ports if channel_count changed
            if key == "channel_count":
                self._create_settings_widget(node)
                self.graph_changed.emit()
            else:
                # Emit low-latency set_param for numeric values so the audio
                # thread sees the change immediately (the debounced graph push
                # still happens for consistency).
                if isinstance(value, (int, float)):
                    self.param_changed.emit(node_id, key, float(value))
                self.graph_changed.emit()
            self.update()

    def _place_settings_widgets(self) -> None:
        """Position settings widgets over their nodes (view space)."""
        for node in self.model.nodes:
            w = self._settings_widgets.get(node.node_id)
            if w is None:
                continue
            if node.minimised:
                w.hide()
                continue
            r = self._node_rect(node)
            ports = node.ports()
            n_ports = max(len([p for p in ports if not p.is_output]),
                          len([p for p in ports if p.is_output]))
            port_bottom = (r.top() + NODE_HEADER_H + SETTINGS_PAD +
                           max(n_ports, 1) * PORT_ROW_H)

            # Convert scene rect to view rect
            tl = self.scene_to_view(QPointF(r.left() + SETTINGS_PAD, port_bottom))
            w_width  = int((NODE_W - SETTINGS_PAD * 2) * self._scale)
            w_height = w.sizeHint().height()

            w.setGeometry(int(tl.x()), int(tl.y()), w_width, w_height)
            w.show()

            # For LV2 nodes: refresh which control ports are driven by wires
            refresh = getattr(w, "refresh_wired_ports", None)
            if refresh is not None:
                wired = {
                    c.to_port
                    for c in self.model.connections
                    if c.to_node == node.node_id
                    and self.model._port_type_for(node.node_id, c.to_port)
                       is not None
                }
                refresh(wired)

    # -----------------------------------------------------------------------
    # Paint
    # -----------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Background
        painter.fillRect(self.rect(), C_BG)
        self._draw_grid(painter)

        # Apply transform
        painter.save()
        painter.translate(-self._origin.x() * self._scale,
                          -self._origin.y() * self._scale)
        painter.scale(self._scale, self._scale)

        self._draw_connections(painter)
        if self._connect_src_port is not None:
            self._draw_preview_wire(painter)
        self._draw_nodes(painter)
        self._draw_marquee(painter)

        painter.restore()

        self._place_settings_widgets()

    def _draw_grid(self, painter: QPainter) -> None:
        pen = QPen(C_GRID)
        pen.setWidth(1)
        painter.setPen(pen)
        step = 40 * self._scale
        ox = (-self._origin.x() * self._scale) % step
        oy = (-self._origin.y() * self._scale) % step
        x = ox
        while x < self.width():
            painter.drawLine(int(x), 0, int(x), self.height())
            x += step
        y = oy
        while y < self.height():
            painter.drawLine(0, int(y), self.width(), int(y))
            y += step

    def _draw_connections(self, painter: QPainter) -> None:
        for conn in self.model.connections:
            src = self.model.get_node(conn.from_node)
            dst = self.model.get_node(conn.to_node)
            if not src or not dst:
                continue
            sp = self._find_port(src, conn.from_port)
            dp = self._find_port(dst, conn.to_port)
            if not sp or not dp:
                continue
            p0 = self._port_scene_pos(src, sp)
            p1 = self._port_scene_pos(dst, dp)
            is_hover = (conn is self._hover_conn)
            col = _wire_color(sp.ptype)
            if is_hover:
                col = col.lighter(160)
            pen = QPen(col, 2.0 if not is_hover else 3.0)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(_bezier_path(p0, p1))

    def _draw_preview_wire(self, painter: QPainter) -> None:
        if not self._connect_src_node:
            return
        p0 = self._port_scene_pos(self._connect_src_node, self._connect_src_port)
        pen = QPen(C_WIRE_PREVIEW, 1.5, Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(_bezier_path(p0, self._connect_cursor))

    def _draw_nodes(self, painter: QPainter) -> None:
        for node in self.model.nodes:
            self._draw_node(painter, node)

    def _draw_node(self, painter: QPainter, node: GraphNode) -> None:
        r = self._node_rect(node)
        is_sel = node.node_id in self.selected_nodes

        # Shadow
        shadow = QPainterPath()
        shadow.addRoundedRect(r.adjusted(3, 3, 3, 3), 6, 6)
        painter.fillPath(shadow, QColor(0, 0, 0, 60))

        # Body
        body_path = QPainterPath()
        body_path.addRoundedRect(r, 6, 6)
        painter.fillPath(body_path, C_NODE_BG)

        # Header
        header_rect = QRectF(r.left(), r.top(), r.width(), NODE_HEADER_H)
        header_path = QPainterPath()
        header_path.addRoundedRect(header_rect, 6, 6)
        # Cover bottom corners of header with square corners
        header_path.addRect(QRectF(r.left(), r.top() + NODE_HEADER_H / 2,
                                   r.width(), NODE_HEADER_H / 2))
        hcol = C_NODE_HEADER.get(node.node_type, C_NODE_HEADER_DEFAULT)
        painter.fillPath(header_path, hcol)

        # Border
        border_pen = QPen(C_NODE_SEL if is_sel else C_NODE_BORDER,
                          2.5 if is_sel else 1.0)
        painter.setPen(border_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(r, 6, 6)

        # Title
        font = QFont("Segoe UI", 8)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(C_TEXT))
        text_rect = QRectF(r.left() + 8, r.top(), r.width() - MIN_BUTTON_W - 16, NODE_HEADER_H)
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                         node.display_name or node.node_type)

        # Default-synth badge
        if node.is_default_synth:
            badge_r = QRectF(r.left() + 4, r.top() + 4, 6, 6)
            painter.setBrush(QBrush(C_DEFAULT_BADGE))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(badge_r)

        # Minimize button
        mb = self._min_button_rect(r)
        painter.setBrush(QBrush(QColor("#2a3a5c")))
        painter.setPen(QPen(C_NODE_BORDER))
        painter.drawRoundedRect(mb, 3, 3)
        painter.setPen(QPen(C_TEXT))
        painter.setFont(QFont("Segoe UI", 7))
        painter.drawText(mb, Qt.AlignCenter, "−" if not node.minimised else "+")

        if node.minimised:
            return

        # Ports
        self._draw_ports(painter, node)

    def _draw_ports(self, painter: QPainter, node: GraphNode) -> None:
        r = self._node_rect(node)
        ins  = [p for p in node.ports() if not p.is_output]
        outs = [p for p in node.ports() if p.is_output]
        port_area_top = r.top() + NODE_HEADER_H + SETTINGS_PAD

        font = QFont("Segoe UI", 7)
        painter.setFont(font)
        fm = QFontMetrics(font)

        for i, port in enumerate(ins):
            y = port_area_top + i * PORT_ROW_H + PORT_ROW_H / 2
            cx = r.left()
            is_hover = (self._hover_port_node is node and self._hover_port is port)
            col = C_PORT_HOVER if is_hover else C_PORT[port.ptype]
            painter.setBrush(QBrush(col))
            painter.setPen(QPen(col.darker(120), 1))
            painter.drawEllipse(QPointF(cx, y), PORT_R, PORT_R)
            painter.setPen(QPen(C_TEXT_DIM))
            painter.drawText(QRectF(cx + PORT_R + 4, y - PORT_ROW_H / 2,
                                    NODE_W / 2 - PORT_R - 8, PORT_ROW_H),
                             Qt.AlignVCenter | Qt.AlignLeft, port.name)

        for i, port in enumerate(outs):
            y = port_area_top + i * PORT_ROW_H + PORT_ROW_H / 2
            cx = r.right()
            is_hover = (self._hover_port_node is node and self._hover_port is port)
            col = C_PORT_HOVER if is_hover else C_PORT[port.ptype]
            painter.setBrush(QBrush(col))
            painter.setPen(QPen(col.darker(120), 1))
            painter.drawEllipse(QPointF(cx, y), PORT_R, PORT_R)
            lbl = port.name
            lbl_w = NODE_W / 2 - PORT_R - 8
            painter.setPen(QPen(C_TEXT_DIM))
            painter.drawText(QRectF(cx - lbl_w - PORT_R - 4, y - PORT_ROW_H / 2,
                                    lbl_w, PORT_ROW_H),
                             Qt.AlignVCenter | Qt.AlignRight, lbl)

    def _draw_marquee(self, painter: QPainter) -> None:
        if self._marquee_start is None or self._marquee_end is None:
            return
        r = QRectF(self._marquee_start, self._marquee_end).normalized()
        painter.fillRect(r, C_MARQUEE_FILL)
        painter.setPen(QPen(C_MARQUEE_LINE, 1, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(r)

    # -----------------------------------------------------------------------
    # Mouse events
    # -----------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        scene_pos = self.view_to_scene(QPointF(event.position()))
        hit = self._hit_test(scene_pos)

        if event.button() == Qt.MiddleButton:
            self._pan_start = QPointF(event.position())
            self._pan_origin_start = QPointF(self._origin)
            self.setCursor(QCursor(Qt.ClosedHandCursor))
            return

        if event.button() == Qt.RightButton:
            if hit.kind == _Hit.WIRE:
                self.model.remove_connection(hit.conn.id)
                self.graph_changed.emit()
                self.update()
            elif hit.kind in (_Hit.NODE_BODY, _Hit.NODE_HEADER):
                # Emit signal; window can show context menu
                self.node_right_clicked.emit(hit.node, event.globalPosition().toPoint())
            return

        if event.button() == Qt.LeftButton:
            if hit.kind == _Hit.MIN_BUTTON:
                hit.node.minimised = not hit.node.minimised
                self._place_settings_widgets()
                self.update()
                return

            if hit.kind == _Hit.PORT:
                # Start connection drag from output ports only
                if hit.port.is_output:
                    self._connect_src_node = hit.node
                    self._connect_src_port = hit.port
                    self._connect_cursor   = scene_pos
                elif hit.port.is_output is False:
                    # Allow dragging from input to start a "reverse" connect
                    # by just picking up existing connection if any
                    existing = next(
                        (c for c in self.model.connections
                         if c.to_node == hit.node.node_id and c.to_port == hit.port.port_id),
                        None
                    )
                    if existing:
                        src = self.model.get_node(existing.from_node)
                        src_port = self._find_port(src, existing.from_port)
                        self.model.remove_connection(existing.id)
                        self._connect_src_node = src
                        self._connect_src_port = src_port
                        self._connect_cursor   = scene_pos
                        self.graph_changed.emit()
                return

            if hit.kind in (_Hit.NODE_BODY, _Hit.NODE_HEADER):
                node = hit.node
                if event.modifiers() & Qt.ShiftModifier:
                    if node.node_id in self.selected_nodes:
                        self.selected_nodes.discard(node.node_id)
                    else:
                        self.selected_nodes.add(node.node_id)
                else:
                    if node.node_id not in self.selected_nodes:
                        self.selected_nodes = {node.node_id}
                self._drag_node = node
                self._drag_offset = QPointF(scene_pos.x() - node.x,
                                            scene_pos.y() - node.y)
                self.update()
                return

            # Empty space: start pan or marquee
            if event.modifiers() & Qt.ShiftModifier:
                self._marquee_start = scene_pos
                self._marquee_end   = scene_pos
            else:
                self.selected_nodes.clear()
                self._pan_start = QPointF(event.position())
                self._pan_origin_start = QPointF(self._origin)
                self.setCursor(QCursor(Qt.ClosedHandCursor))
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        scene_pos = self.view_to_scene(QPointF(event.position()))

        # Pan
        if self._pan_start is not None:
            delta = event.position() - self._pan_start
            self._origin = QPointF(
                self._pan_origin_start.x() - delta.x() / self._scale,
                self._pan_origin_start.y() - delta.y() / self._scale,
            )
            self.update()
            return

        # Connection drag
        if self._connect_src_port is not None:
            self._connect_cursor = scene_pos
            # Hover highlight
            hit = self._hit_test(scene_pos)
            if hit.kind == _Hit.PORT and not hit.port.is_output:
                self._hover_port_node = hit.node
                self._hover_port      = hit.port
            else:
                self._hover_port_node = None
                self._hover_port      = None
            self.update()
            return

        # Node drag
        if self._drag_node is not None:
            for nid in self.selected_nodes:
                n = self.model.get_node(nid)
                if n:
                    dx = scene_pos.x() - self._drag_offset.x() - self._drag_node.x
                    dy = scene_pos.y() - self._drag_offset.y() - self._drag_node.y
                    n.x += dx
                    n.y += dy
            self._drag_node.x = scene_pos.x() - self._drag_offset.x()
            self._drag_node.y = scene_pos.y() - self._drag_offset.y()
            self.update()
            return

        # Marquee
        if self._marquee_start is not None:
            self._marquee_end = scene_pos
            self.update()
            return

        # Hover
        hit = self._hit_test(scene_pos)
        new_hp_node = hit.node if hit.kind == _Hit.PORT else None
        new_hp_port = hit.port if hit.kind == _Hit.PORT else None
        new_hc      = hit.conn if hit.kind == _Hit.WIRE else None
        if (new_hp_node is not self._hover_port_node or
                new_hp_port is not self._hover_port or
                new_hc is not self._hover_conn):
            self._hover_port_node = new_hp_node
            self._hover_port      = new_hp_port
            self._hover_conn      = new_hc
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        scene_pos = self.view_to_scene(QPointF(event.position()))

        if event.button() == Qt.MiddleButton or (
                event.button() == Qt.LeftButton and self._pan_start is not None):
            self._pan_start = None
            self.setCursor(QCursor(Qt.ArrowCursor))
            return

        if event.button() == Qt.LeftButton:
            # Finish connection
            if self._connect_src_port is not None:
                hit = self._hit_test(scene_pos)
                if hit.kind == _Hit.PORT and not hit.port.is_output:
                    src_port = self._connect_src_port
                    dst_port = hit.port
                    # Type check: MIDI→MIDI, AUDIO→AUDIO, CONTROL→CONTROL
                    if src_port.ptype == dst_port.ptype:
                        from .graph_model import GraphConnection
                        conn = GraphConnection(
                            from_node=self._connect_src_node.node_id,
                            from_port=src_port.port_id,
                            to_node=hit.node.node_id,
                            to_port=dst_port.port_id,
                        )
                        if self.model.add_connection(conn):
                            self.graph_changed.emit()
                self._connect_src_node = None
                self._connect_src_port = None
                self._hover_port_node  = None
                self._hover_port       = None
                self.update()
                return

            # Finish node drag
            if self._drag_node is not None:
                self._drag_node = None
                self.graph_changed.emit()
                return

            # Finish marquee
            if self._marquee_start is not None:
                mrect = QRectF(self._marquee_start, self._marquee_end).normalized()
                add_mode = bool(event.modifiers() & Qt.ShiftModifier)
                if not add_mode:
                    self.selected_nodes.clear()
                for node in self.model.nodes:
                    if self._node_rect(node).intersects(mrect):
                        self.selected_nodes.add(node.node_id)
                self._marquee_start = None
                self._marquee_end   = None
                self.update()
                return

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        factor = 1.12 if delta > 0 else 1 / 1.12
        mouse_scene = self.view_to_scene(QPointF(event.position()))
        self._scale = max(0.15, min(4.0, self._scale * factor))
        # Keep mouse point fixed
        self._origin = QPointF(
            mouse_scene.x() - event.position().x() / self._scale,
            mouse_scene.y() - event.position().y() / self._scale,
        )
        self.update()

    # -----------------------------------------------------------------------
    # Keyboard
    # -----------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Delete and self.selected_nodes:
            # Don't allow deleting track_source nodes (managed by app)
            to_del = [nid for nid in self.selected_nodes
                      if (n := self.model.get_node(nid)) and
                         n.node_type not in ("track_source",)]
            for nid in to_del:
                self.model.remove_node(nid)
                # Remove the inline settings widget so it doesn't linger on screen
                w = self._settings_widgets.pop(nid, None)
                if w:
                    w.setParent(None)
                    w.deleteLater()
            self.selected_nodes -= set(to_del)
            if to_del:
                self.graph_changed.emit()
            self.update()
        elif event.key() == Qt.Key_F:
            self.frame_all()
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Default inline settings widgets
# ---------------------------------------------------------------------------

def _make_default_settings_widget(node: GraphNode, parent, on_change: Callable):
    """Build a compact inline settings panel for a node type.

    Returns None if this node type has no settings.
    """
    from PySide6.QtWidgets import (
        QWidget, QFormLayout, QLabel, QDoubleSpinBox, QSpinBox,
        QLineEdit, QPushButton, QHBoxLayout, QSlider,
    )
    from PySide6.QtCore import Qt as _Qt

    t = node.node_type

    if t == "note_gate":
        from PySide6.QtWidgets import QComboBox
        from .graph_model import midi_note_name, midi_pitch_from_name, NOTE_GATE_MODES

        w = QWidget(parent)
        w.setStyleSheet("background: transparent; color: #ccc;")
        lay = QFormLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(3)

        # Mode dropdown
        mode_combo = QComboBox()
        mode_combo.addItems(NOTE_GATE_MODES)
        mode_combo.setCurrentIndex(node.params.get("gate_mode", 0))
        mode_combo.setStyleSheet(
            "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;")
        mode_combo.currentIndexChanged.connect(
            lambda i: on_change(node.node_id, "gate_mode", i))
        lay.addRow(QLabel("Mode:"), mode_combo)

        # Build note-name list for the combo boxes (C-1 to G9, all 128 MIDI notes)
        note_names = [midi_note_name(p) for p in range(128)]

        # Low note
        lo_combo = QComboBox()
        lo_combo.addItems(note_names)
        lo_combo.setCurrentIndex(node.params.get("pitch_lo", 0))
        lo_combo.setStyleSheet(
            "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;")
        lo_combo.currentIndexChanged.connect(
            lambda i: on_change(node.node_id, "pitch_lo", i))
        lay.addRow(QLabel("Lo note:"), lo_combo)

        # High note
        hi_combo = QComboBox()
        hi_combo.addItems(note_names)
        hi_combo.setCurrentIndex(node.params.get("pitch_hi", 127))
        hi_combo.setStyleSheet(
            "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;")
        hi_combo.currentIndexChanged.connect(
            lambda i: on_change(node.node_id, "pitch_hi", i))
        lay.addRow(QLabel("Hi note:"), hi_combo)

        return w

    if t == "fluidsynth":
        w = QWidget(parent)
        w.setStyleSheet("background: transparent; color: #ccc;")
        lay = QFormLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(3)

        sf2_row = QWidget()
        sf2_lay = QHBoxLayout(sf2_row)
        sf2_lay.setContentsMargins(0, 0, 0, 0)
        sf2_lay.setSpacing(3)
        sf2_edit = QLineEdit(node.params.get("sf2_path", ""))
        sf2_edit.setPlaceholderText("SF2 path…")
        sf2_edit.setReadOnly(True)
        sf2_edit.setStyleSheet("color: #aaa; background: #0d1117; border: 1px solid #2a3a5c;")
        sf2_lay.addWidget(sf2_edit)
        browse_btn = QPushButton("…")
        browse_btn.setMaximumWidth(24)
        browse_btn.setStyleSheet("background: #1a2236; color: #ccc;")

        def _browse():
            from PySide6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                w, "Select SF2", "", "SoundFont (*.sf2)")
            if path:
                sf2_edit.setText(path)
                on_change(node.node_id, "sf2_path", path)
        browse_btn.clicked.connect(_browse)
        sf2_lay.addWidget(browse_btn)
        lay.addRow(QLabel("SF2:"), sf2_row)

        def _on_sf2_text_changed(text):
            on_change(node.node_id, "sf2_path", text)
        sf2_edit.textChanged.connect(_on_sf2_text_changed)
        return w

    if t == "sampler":
        w = QWidget(parent)
        w.setStyleSheet("background: transparent; color: #ccc;")
        lay = QFormLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(3)

        # Sample file picker
        smp_row = QWidget()
        smp_lay = QHBoxLayout(smp_row)
        smp_lay.setContentsMargins(0, 0, 0, 0)
        smp_lay.setSpacing(3)
        smp_edit = QLineEdit(node.params.get("sample_path", ""))
        smp_edit.setPlaceholderText("Sample file…")
        smp_edit.setReadOnly(True)
        smp_edit.setStyleSheet("color: #aaa; background: #0d1117; border: 1px solid #2a3a5c;")
        smp_lay.addWidget(smp_edit)
        browse_btn = QPushButton("…")
        browse_btn.setMaximumWidth(24)
        browse_btn.setStyleSheet("background: #1a2236; color: #ccc;")

        def _browse_smp():
            from PySide6.QtWidgets import QFileDialog
            import os
            samples_dir = str(
                __import__('pathlib').Path(__file__).parent.parent / 'samples'
            )
            path, _ = QFileDialog.getOpenFileName(
                w, "Select Sample", samples_dir,
                "Audio (*.wav *.ogg *.flac *.aif *.aiff)")
            if path:
                smp_edit.setText(path)
                on_change(node.node_id, "sample_path", path)
        browse_btn.clicked.connect(_browse_smp)
        smp_lay.addWidget(browse_btn)
        lay.addRow(QLabel("File:"), smp_row)

        # ADSR
        adsr = [("attack", 0.01, 0.0, 4.0),
                ("decay",  0.1,  0.0, 4.0),
                ("sustain",0.8,  0.0, 1.0),
                ("release",0.2,  0.0, 4.0)]
        for pname, default, lo, hi in adsr:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(0.01)
            spin.setDecimals(3)
            spin.setValue(node.params.get(pname, default))
            spin.setStyleSheet(
                "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;")
            spin.setMaximumWidth(80)
            pname_capture = pname  # capture for lambda
            spin.valueChanged.connect(
                lambda v, k=pname_capture: on_change(node.node_id, k, v))
            lay.addRow(QLabel(pname.capitalize() + ":"), spin)
        return w

    if t == "lv2":
        # LV2 node: show URI (small, dimmed) plus appropriate widgets for every
        # control input port.  Widget type is chosen from port metadata hints:
        #   is_toggle     → QCheckBox
        #   is_enumeration + scale_points → QComboBox
        #   is_integer    → QSpinBox (integer steps)
        #   otherwise     → QDoubleSpinBox (continuous)
        # Widgets are disabled (greyed) whenever a wire drives the port.
        from PySide6.QtWidgets import (
            QDoubleSpinBox as _DSB, QScrollArea, QCheckBox, QComboBox,
            QSpinBox as _ISB,
        )

        raw_ports = node.params.get("_ports", [])
        ctrl_inputs = [p for p in raw_ports
                       if p.get("type") == "control" and p.get("direction") == "input"]

        w = QWidget(parent)
        w.setStyleSheet("background: transparent; color: #ccc;")
        lay = QFormLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)

        # URI row (always shown, read-only, small)
        uri_lbl = QLabel(node.params.get("lv2_uri", ""))
        uri_lbl.setStyleSheet("color: #555; font-size: 8px;")
        uri_lbl.setWordWrap(True)
        lay.addRow(QLabel("URI:"), uri_lbl)

        # Dual-mono badge — shown when the plugin is mono and will be run ×2
        if "_dual_mono" not in node.params:
            node.ports()
        if node.params.get("_dual_mono"):
            badge = QLabel("⇄ dual mono ×2")
            badge.setStyleSheet(
                "color: #6bcb77; font-size: 8px; background: #0d1a0d;"
                " border: 1px solid #2a4a2a; border-radius: 3px; padding: 1px 4px;")
            lay.addRow(badge)

        # dict: symbol → widget (QCheckBox / QComboBox / QSpinBox / QDoubleSpinBox)
        _ctrl_widgets: dict = {}

        STYLE_ACTIVE   = "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;"
        STYLE_DISABLED = "background: #111; color: #444; border: 1px solid #1a1a1a;"

        for p in ctrl_inputs:
            sym     = p.get("symbol", "")
            lbl_txt = p.get("name", sym) or sym
            p_min   = float(p.get("min",     0.0))
            p_max   = float(p.get("max",     1.0))
            p_def   = float(p.get("default", 0.0))
            p_def   = max(p_min, min(p_max, p_def))
            stored  = node.params.get(sym, p_def)

            is_toggle = p.get("is_toggle", False)
            is_integer = p.get("is_integer", False)
            is_enum = p.get("is_enumeration", False)
            scale_pts = p.get("scale_points", [])

            sym_capture = sym  # capture for lambdas

            if is_toggle:
                # Boolean on/off → checkbox
                cb = QCheckBox()
                cb.setChecked(float(stored) > 0.5)
                cb.setStyleSheet("color: #ccc;")
                cb.toggled.connect(
                    lambda checked, k=sym_capture: on_change(
                        node.node_id, k, 1.0 if checked else 0.0))
                row_lbl = QLabel(lbl_txt + ":")
                row_lbl.setStyleSheet("color: #aaa; font-size: 8px;")
                lay.addRow(row_lbl, cb)
                _ctrl_widgets[sym] = cb

            elif is_enum and scale_pts:
                # Enumeration with named choices → combo box
                combo = QComboBox()
                combo.setStyleSheet(STYLE_ACTIVE)
                combo.setMaximumWidth(140)
                # Sort scale points by value for consistent ordering
                pts = sorted(scale_pts, key=lambda sp: float(sp.get("value", 0)))
                current_idx = 0
                for idx, sp in enumerate(pts):
                    val = float(sp.get("value", 0))
                    label = sp.get("label", str(val))
                    combo.addItem(label, val)
                    if abs(val - float(stored)) < 0.001:
                        current_idx = idx
                combo.setCurrentIndex(current_idx)
                combo.currentIndexChanged.connect(
                    lambda idx, k=sym_capture, c=combo: on_change(
                        node.node_id, k, c.itemData(idx)))
                row_lbl = QLabel(lbl_txt + ":")
                row_lbl.setStyleSheet("color: #aaa; font-size: 8px;")
                lay.addRow(row_lbl, combo)
                _ctrl_widgets[sym] = combo

            elif is_integer:
                # Integer-valued continuous → QSpinBox
                ispin = _ISB()
                ispin.setRange(int(p_min), int(p_max))
                ispin.setValue(int(round(float(stored))))
                ispin.setStyleSheet(STYLE_ACTIVE)
                ispin.setMaximumWidth(90)
                ispin.valueChanged.connect(
                    lambda v, k=sym_capture: on_change(node.node_id, k, float(v)))
                row_lbl = QLabel(lbl_txt + ":")
                row_lbl.setStyleSheet("color: #aaa; font-size: 8px;")
                lay.addRow(row_lbl, ispin)
                _ctrl_widgets[sym] = ispin

            else:
                # Continuous float → QDoubleSpinBox
                span = p_max - p_min if p_max != p_min else 1.0
                if span <= 0.1:
                    step, dec = 0.001, 4
                elif span <= 2.0:
                    step, dec = 0.01, 3
                elif span <= 20.0:
                    step, dec = 0.1, 2
                elif span <= 200.0:
                    step, dec = 1.0, 1
                else:
                    step, dec = 10.0, 0

                spin = _DSB()
                spin.setRange(p_min, p_max)
                spin.setSingleStep(step)
                spin.setDecimals(dec)
                spin.setValue(float(stored))
                spin.setStyleSheet(STYLE_ACTIVE)
                spin.setMaximumWidth(90)
                spin.valueChanged.connect(
                    lambda v, k=sym_capture: on_change(node.node_id, k, v))
                row_lbl = QLabel(lbl_txt + ":")
                row_lbl.setStyleSheet("color: #aaa; font-size: 8px;")
                lay.addRow(row_lbl, spin)
                _ctrl_widgets[sym] = spin

        def refresh_wired_ports(wired: set):
            """Called by canvas to grey out ports driven by a wire."""
            for sym, widget in _ctrl_widgets.items():
                driven = sym in wired
                widget.setEnabled(not driven)
                if hasattr(widget, 'setStyleSheet') and not isinstance(widget, QCheckBox):
                    widget.setStyleSheet(STYLE_DISABLED if driven else STYLE_ACTIVE)

        w.refresh_wired_ports = refresh_wired_ports
        return w

    if t in ("mixer", "output"):
        w = QWidget(parent)
        w.setStyleSheet("background: transparent; color: #ccc;")
        lay = QFormLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(3)
        spin = QSpinBox()
        spin.setRange(1, 16)
        spin.setValue(node.params.get("channel_count", 2))
        spin.setStyleSheet("background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;")
        spin.setMaximumWidth(60)
        spin.valueChanged.connect(
            lambda v: on_change(node.node_id, "channel_count", v))
        lay.addRow(QLabel("Inputs:"), spin)
        # Per-channel gain sliders
        ch_count = node.params.get("channel_count", 2)
        for i in range(ch_count):
            sld = QSlider(_Qt.Horizontal)
            sld.setRange(0, 100)
            sld.setValue(int(node.params.get(f"gain_{i}", 1.0) * 100))
            sld.setStyleSheet("color: #6bcb77;")
            sld.valueChanged.connect(
                lambda v, idx=i: on_change(node.node_id, f"gain_{idx}", v / 100.0))
            lay.addRow(QLabel(f"Ch {i}:"), sld)
        return w

    if t == "sine":
        w = QWidget(parent)
        w.setStyleSheet("background: transparent; color: #ccc;")
        lay = QFormLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1.0)
        spin.setSingleStep(0.01)
        spin.setDecimals(2)
        spin.setValue(node.params.get("gain", 0.15))
        spin.setStyleSheet("background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;")
        spin.setMaximumWidth(70)
        spin.valueChanged.connect(
            lambda v: on_change(node.node_id, "gain", v))
        lay.addRow(QLabel("Gain:"), spin)
        return w

    # track_source, control_source, unknown: no settings
    # Plugin-backed nodes: auto-generate from descriptor
    from .graph_model import get_plugin_descriptor
    desc = get_plugin_descriptor(t)
    if desc:
        return _make_plugin_settings_widget(node, desc, parent, on_change)
    return None


def _make_plugin_settings_widget(node: GraphNode, desc: dict, parent, on_change: Callable):
    """Auto-generate a settings panel from a plugin descriptor.

    Generates widgets for:
      - Control input ports (based on ControlHint)
      - ConfigParams (file pickers, dropdowns, spinboxes, etc.)
    """
    from PySide6.QtWidgets import (
        QWidget, QFormLayout, QLabel, QDoubleSpinBox, QSpinBox,
        QLineEdit, QPushButton, QHBoxLayout, QCheckBox, QComboBox,
    )
    from PySide6.QtCore import Qt as _Qt

    ports = desc.get("ports", [])
    config_params = desc.get("config_params", [])

    # Filter to control input ports only
    ctrl_inputs = [p for p in ports
                   if p.get("type") == "control" and p.get("role") == "input"]

    if not ctrl_inputs and not config_params:
        return None

    w = QWidget(parent)
    w.setStyleSheet("background: transparent; color: #ccc;")
    lay = QFormLayout(w)
    lay.setContentsMargins(4, 2, 4, 2)
    lay.setSpacing(3)

    STYLE_ACTIVE = "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;"
    STYLE_DISABLED = "background: #111; color: #444; border: 1px solid #1a1a1a;"

    _ctrl_widgets: dict = {}

    # --- Config params first (file pickers, etc.) ---
    for cp in config_params:
        cp_id = cp.get("id", "")
        cp_display = cp.get("display_name", cp_id)
        cp_type = cp.get("type", "string")
        cp_default = cp.get("default", "")
        stored = node.params.get(cp_id, cp_default)

        if cp_type == "filepath":
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(3)
            edit = QLineEdit(str(stored))
            edit.setPlaceholderText(cp.get("doc", ""))
            edit.setReadOnly(True)
            edit.setStyleSheet("color: #aaa; background: #0d1117; border: 1px solid #2a3a5c;")
            row_lay.addWidget(edit)
            browse_btn = QPushButton("…")
            browse_btn.setMaximumWidth(24)
            browse_btn.setStyleSheet("background: #1a2236; color: #ccc;")

            file_filter = cp.get("file_filter", "All Files (*)")
            def _browse(checked=False, e=edit, cid=cp_id, ff=file_filter):
                from PySide6.QtWidgets import QFileDialog
                path, _ = QFileDialog.getOpenFileName(w, f"Select {cp_display}", "", ff)
                if path:
                    e.setText(path)
                    on_change(node.node_id, cid, path)
            browse_btn.clicked.connect(_browse)
            row_lay.addWidget(browse_btn)
            lay.addRow(QLabel(cp_display + ":"), row)

        elif cp_type == "categorical":
            combo = QComboBox()
            combo.setStyleSheet(STYLE_ACTIVE)
            choices = cp.get("choices", [])
            combo.addItems(choices)
            try:
                idx = choices.index(str(stored))
            except ValueError:
                idx = 0
            combo.setCurrentIndex(idx)
            combo.currentTextChanged.connect(
                lambda text, cid=cp_id: on_change(node.node_id, cid, text))
            lay.addRow(QLabel(cp_display + ":"), combo)

        elif cp_type == "integer":
            spin = QSpinBox()
            spin.setRange(0, 999999)
            spin.setValue(int(stored) if stored else 0)
            spin.setStyleSheet(STYLE_ACTIVE)
            spin.setMaximumWidth(80)
            spin.valueChanged.connect(
                lambda v, cid=cp_id: on_change(node.node_id, cid, v))
            lay.addRow(QLabel(cp_display + ":"), spin)

        elif cp_type == "bool":
            cb = QCheckBox()
            cb.setChecked(str(stored).lower() in ("1", "true", "yes"))
            cb.toggled.connect(
                lambda checked, cid=cp_id: on_change(node.node_id, cid, 1 if checked else 0))
            lay.addRow(QLabel(cp_display + ":"), cb)

        elif cp_type == "float":
            spin = QDoubleSpinBox()
            spin.setRange(-1e6, 1e6)
            spin.setValue(float(stored) if stored else 0.0)
            spin.setStyleSheet(STYLE_ACTIVE)
            spin.setMaximumWidth(80)
            spin.valueChanged.connect(
                lambda v, cid=cp_id: on_change(node.node_id, cid, v))
            lay.addRow(QLabel(cp_display + ":"), spin)

        else:  # string
            edit = QLineEdit(str(stored))
            edit.setStyleSheet(STYLE_ACTIVE)
            edit.textChanged.connect(
                lambda text, cid=cp_id: on_change(node.node_id, cid, text))
            lay.addRow(QLabel(cp_display + ":"), edit)

    # --- Control input ports ---
    for p in ctrl_inputs:
        pid = p.get("id", "")
        display = p.get("display_name", pid)
        hint = p.get("hint", "continuous")
        p_min = float(p.get("min", 0.0))
        p_max = float(p.get("max", 1.0))
        p_def = float(p.get("default", 0.0))
        p_step = float(p.get("step", 0.0))
        choices = p.get("choices", [])
        stored = node.params.get(pid, p_def)

        pid_capture = pid

        if hint == "toggle":
            cb = QCheckBox()
            cb.setChecked(float(stored) > 0.5)
            cb.setStyleSheet("color: #ccc;")
            cb.toggled.connect(
                lambda checked, k=pid_capture: on_change(
                    node.node_id, k, 1.0 if checked else 0.0))
            lbl = QLabel(display + ":")
            lbl.setStyleSheet("color: #aaa; font-size: 8px;")
            lay.addRow(lbl, cb)
            _ctrl_widgets[pid] = cb

        elif hint in ("categorical", "radio") and choices:
            combo = QComboBox()
            combo.setStyleSheet(STYLE_ACTIVE)
            combo.setMaximumWidth(140)
            for i, ch in enumerate(choices):
                combo.addItem(ch, float(i))
            current_idx = max(0, min(len(choices) - 1, int(round(float(stored)))))
            combo.setCurrentIndex(current_idx)
            combo.currentIndexChanged.connect(
                lambda idx, k=pid_capture: on_change(node.node_id, k, float(idx)))
            lbl = QLabel(display + ":")
            lbl.setStyleSheet("color: #aaa; font-size: 8px;")
            lay.addRow(lbl, combo)
            _ctrl_widgets[pid] = combo

        elif hint == "integer":
            spin = QSpinBox()
            spin.setRange(int(p_min), int(p_max))
            spin.setValue(int(round(float(stored))))
            if p_step > 0:
                spin.setSingleStep(int(p_step))
            spin.setStyleSheet(STYLE_ACTIVE)
            spin.setMaximumWidth(80)
            spin.valueChanged.connect(
                lambda v, k=pid_capture: on_change(node.node_id, k, float(v)))
            lbl = QLabel(display + ":")
            lbl.setStyleSheet("color: #aaa; font-size: 8px;")
            lay.addRow(lbl, spin)
            _ctrl_widgets[pid] = spin

        elif hint == "meter":
            # Read-only meter — just show label for now (future: VU bar)
            val_lbl = QLabel(f"{float(stored):.2f}")
            val_lbl.setStyleSheet("color: #6bcb77; font-size: 8px;")
            lay.addRow(QLabel(display + ":"), val_lbl)

        else:
            # Continuous (default)
            span = p_max - p_min if p_max != p_min else 1.0
            if span <= 0.1:
                step, dec = 0.001, 4
            elif span <= 2.0:
                step, dec = 0.01, 3
            elif span <= 20.0:
                step, dec = 0.1, 2
            elif span <= 200.0:
                step, dec = 1.0, 1
            else:
                step, dec = 10.0, 0

            spin = QDoubleSpinBox()
            spin.setRange(p_min, p_max)
            spin.setSingleStep(step)
            spin.setDecimals(dec)
            spin.setValue(float(stored))
            spin.setStyleSheet(STYLE_ACTIVE)
            spin.setMaximumWidth(90)
            spin.valueChanged.connect(
                lambda v, k=pid_capture: on_change(node.node_id, k, v))
            lbl = QLabel(display + ":")
            lbl.setStyleSheet("color: #aaa; font-size: 8px;")
            lay.addRow(lbl, spin)
            _ctrl_widgets[pid] = spin

    def refresh_wired_ports(wired: set):
        for sym, widget in _ctrl_widgets.items():
            driven = sym in wired
            widget.setEnabled(not driven)
            if hasattr(widget, 'setStyleSheet') and not isinstance(widget, QCheckBox):
                widget.setStyleSheet(STYLE_DISABLED if driven else STYLE_ACTIVE)

    w.refresh_wired_ports = refresh_wired_ports
    return w


# ---------------------------------------------------------------------------
# Geometry / drawing helpers
# ---------------------------------------------------------------------------

def _bezier_path(p0: QPointF, p1: QPointF) -> QPainterPath:
    """Cubic bezier from p0 (output port) to p1 (input port or cursor)."""
    dx = abs(p1.x() - p0.x()) * 0.5 + 40
    path = QPainterPath(p0)
    path.cubicTo(
        QPointF(p0.x() + dx, p0.y()),
        QPointF(p1.x() - dx, p1.y()),
        p1,
    )
    return path


def _point_to_bezier_dist(pt: QPointF, p0: QPointF, p1: QPointF,
                           samples: int = 30) -> float:
    """Approximate minimum distance from pt to the bezier curve."""
    dx = abs(p1.x() - p0.x()) * 0.5 + 40
    best = math.inf
    for i in range(samples + 1):
        t = i / samples
        mt = 1 - t
        bx = (mt**3 * p0.x() +
              3 * mt**2 * t * (p0.x() + dx) +
              3 * mt * t**2 * (p1.x() - dx) +
              t**3 * p1.x())
        by = (mt**3 * p0.y() +
              3 * mt**2 * t * p0.y() +
              3 * mt * t**2 * p1.y() +
              t**3 * p1.y())
        d = math.hypot(pt.x() - bx, pt.y() - by)
        if d < best:
            best = d
    return best


def _wire_color(ptype: PortType) -> QColor:
    return C_PORT.get(ptype, C_WIRE)
