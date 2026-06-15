"""PluginsDock — top-level dock widget that hosts active :class:`PluginBlock`s."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDockWidget, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .flow_layout import FlowLayout
from .plugin_block import PluginBlock

logger = logging.getLogger(__name__)


class PluginsDock(QDockWidget):
    """Dockable container showing all active plugin blocks.

    Layout:

    - header: "Plugins" title + "Add Plugin" button
    - scroll area with a FlowLayout wrapping the blocks across columns
    - empty-state label when no blocks are active
    """

    def __init__(self, host, parent=None):
        super().__init__("Plugins", parent)
        self.host = host
        self.setObjectName("PluginsDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)

        container = QWidget()
        container.setStyleSheet("background-color: #16213e;")
        root = QVBoxLayout(container)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # -- Header row --
        header = QHBoxLayout()
        title = QLabel("Plugins")
        f = QFont()
        f.setBold(True)
        f.setPointSize(10)
        title.setFont(f)
        title.setStyleSheet("color: #e94560;")
        header.addWidget(title)
        header.addStretch()

        self._add_btn = QPushButton("+ Add Plugin")
        self._add_btn.clicked.connect(self._show_add_menu)
        header.addWidget(self._add_btn)

        root.addLayout(header)

        # -- Scroll area with flow layout --
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_inner = QWidget()
        self._scroll_inner.setStyleSheet("background-color: #16213e;")
        self._flow = FlowLayout(self._scroll_inner, margin=4, h_spacing=6,
                                v_spacing=6)
        self._scroll_inner.setLayout(self._flow)
        self._scroll.setWidget(self._scroll_inner)
        root.addWidget(self._scroll, stretch=1)

        # -- Empty state --
        self._empty_label = QLabel(
            "No plugins active. Click \u201c+ Add Plugin\u201d to start."
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(
            "color: #666; font-style: italic; padding: 16px;"
        )
        root.addWidget(self._empty_label)

        # Plugin blocks are 360px wide and the flow area has no horizontal
        # scrollbar, so the dock must be at least that wide or tiles get
        # clipped. This also sets the default width of the tabbed dock group.
        container.setMinimumWidth(380)
        self.setWidget(container)
        self._refresh_empty_state()

    # -- Add-plugin menu -------------------------------------------------

    def _show_add_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #1a2236; color: #eee;"
            " border: 1px solid #2a3a5c; }"
            "QMenu::item:selected { background: #3a7bd5; }"
        )
        plugins = self.host.plugins
        if not plugins:
            act = menu.addAction("(no plugins registered)")
            act.setEnabled(False)
        else:
            # Sort by human-readable name.
            items = sorted(plugins.items(),
                           key=lambda kv: kv[1].manifest.name.lower())
            for pid, cls in items:
                m = cls.manifest
                label = m.name
                if m.description:
                    label = f"{m.name}  —  {m.description}"
                act = menu.addAction(label)
                act.triggered.connect(
                    lambda _checked=False, c=cls: self.add_plugin(c))
        # Position menu under the button.
        pos = self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft())
        menu.exec(pos)

    # -- Block add/remove ------------------------------------------------

    def add_plugin(self, plugin_cls) -> PluginBlock:
        block = PluginBlock(self.host, plugin_cls, parent=self._scroll_inner)
        block.removed.connect(self._on_block_removed)
        self._flow.addWidget(block)
        self._refresh_empty_state()
        return block

    def _on_block_removed(self, block) -> None:
        self._flow.removeWidget(block)
        self._refresh_empty_state()

    def _refresh_empty_state(self) -> None:
        has_any = False
        for i in range(self._flow.count()):
            item = self._flow.itemAt(i)
            if item is not None and item.widget() is not None:
                has_any = True
                break
        self._empty_label.setVisible(not has_any)
        self._scroll.setVisible(has_any)
