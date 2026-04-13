"""FlowLayout — a Qt layout that wraps children across rows/columns.

Based on the standard Qt FlowLayout example (public-domain), adapted for
PySide6. Lays out children left-to-right, wrapping to the next row when
the available width is exceeded.
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin: int = 4, h_spacing: int = 6,
                 v_spacing: int = 6):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(QMargins(margin, margin, margin, margin))
        self._h_space = h_spacing
        self._v_space = v_spacing
        self._items: list = []

    def __del__(self):
        # Qt takes ownership of laid-out widgets; items need to be released
        # individually on destruction.
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    # -- QLayout overrides ------------------------------------------------

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(),
                      margins.top() + margins.bottom())
        return size

    # -- Private ---------------------------------------------------------

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            widget = item.widget()
            h_space = self._h_space
            v_space = self._v_space
            if widget is not None:
                # Let the widget override spacing via its layout style.
                try:
                    sty = widget.style()
                    h_space = max(h_space, sty.layoutSpacing(
                        QSizePolicy.PushButton, QSizePolicy.PushButton,
                        Qt.Horizontal))
                    v_space = max(v_space, sty.layoutSpacing(
                        QSizePolicy.PushButton, QSizePolicy.PushButton,
                        Qt.Vertical))
                except Exception:
                    pass
            next_x = x + item.sizeHint().width() + h_space
            if next_x - h_space > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + v_space
                next_x = x + item.sizeHint().width() + h_space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x = next_x
            line_height = max(line_height, item.sizeHint().height())
        return y + line_height - rect.y() + margins.bottom()
