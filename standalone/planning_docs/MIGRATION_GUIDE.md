# PySide6 Migration Guide

## Overview
This conversion migrates your tkinter-based MIDI sequencer to PySide6 (Qt6). The migration addresses:
- Interface flickering issues
- Drag-drop behavior problems  
- Translucency/transparency errors in drawing code

## Key Changes

### 1. Import Changes
```python
# Old (tkinter)
import tkinter as tk
from tkinter import ttk

# New (PySide6)
from PySide6.QtWidgets import QWidget, QFrame, QLabel, etc.
from PySide6.QtCore import Qt, Signal, QRect, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QBrush
```

### 2. Widget Hierarchy
- `ttk.Frame` → `QFrame` or `QWidget`
- `ttk.Label` → `QLabel`
- `ttk.Button` → `QPushButton`
- `ttk.Entry` → `QLineEdit`
- `ttk.Spinbox` → `QSpinBox`
- `ttk.Combobox` → `QComboBox`
- `tk.Canvas` → Custom `QWidget` with `paintEvent()`

### 3. Layout Management
tkinter's pack/grid becomes Qt layouts:
```python
# Old
widget.pack(side=tk.LEFT, fill=tk.X, expand=True)

# New
layout = QHBoxLayout()
layout.addWidget(widget, stretch=1)
```

### 4. Custom Painting
Instead of Canvas methods, use QPainter in paintEvent:
```python
def paintEvent(self, event):
    painter = QPainter(self)
    painter.setRenderHint(QPainter.Antialiasing)
    
    # Drawing operations
    painter.setPen(QColor('#ffffff'))
    painter.setBrush(QColor('#000000'))
    painter.drawRect(x, y, w, h)
```

### 5. Scroll Areas
```python
# Old
canvas = tk.Canvas(parent)
scrollbar = ttk.Scrollbar(parent, command=canvas.yview)

# New
scroll_area = QScrollArea()
scroll_area.setWidget(content_widget)
scroll_area.setWidgetResizable(True)
```

### 6. Drag and Drop
```python
# In the draggable widget
def mousePressEvent(self, event):
    if event.button() == Qt.LeftButton:
        self.drag_start_pos = event.pos()

def mouseMoveEvent(self, event):
    if not (event.buttons() & Qt.LeftButton):
        return
    if (event.pos() - self.drag_start_pos).manhattanLength() < 10:
        return
    
    drag = QDrag(self)
    mime_data = QMimeData()
    mime_data.setText(f'pattern:{self.pattern.id}')
    drag.setMimeData(mime_data)
    drag.exec_(Qt.CopyAction)

# In the drop target
def dragEnterEvent(self, event):
    if event.mimeData().hasText():
        event.accept()
    else:
        event.ignore()

def dropEvent(self, event):
    data = event.mimeData().text()
    # Process drop
```

### 7. Modal Dialogs
```python
# Old
dialog = tk.Toplevel(parent)
dialog.transient(parent)
dialog.grab_set()

# New  
dialog = QDialog(parent)
dialog.setModal(True)
result = dialog.exec_()  # Blocks until closed
```

### 8. Events and Signals
```python
# Old
button.configure(command=callback)
widget.bind('<Button-1>', callback)

# New
button.clicked.connect(callback)
# Override mousePressEvent for custom mouse handling
```

## Performance Improvements

### Double Buffering
Qt automatically provides double buffering, eliminating flicker. For custom widgets:
```python
self.setAttribute(Qt.WA_OpaquePaintEvent)  # If fully opaque
# Or enable updates optimization:
self.setUpdatesEnabled(False)
# ... make changes ...
self.setUpdatesEnabled(True)
```

### Transparency
Qt handles transparency properly in QPainter:
```python
# Transparent colors work correctly
painter.setBrush(QColor(255, 0, 0, 128))  # Red with 50% alpha
```

### Rendering Hints
```python
painter.setRenderHint(QPainter.Antialiasing)
painter.setRenderHint(QPainter.TextAntialiasing)
painter.setRenderHint(QPainter.SmoothPixmapTransform)
```

## Files Converted

1. **beat_grid.py** - Custom canvas painting for step sequencer grid
2. **dialogs.py** - Modal dialogs for pattern editing
3. **pattern_list.py** - Scrollable list with drag support
4. **topbar.py** - Control bar with spinboxes and buttons

## Files Needing Conversion

You'll need to convert these additional files following the same patterns:
- `arrangement.py` - Timeline/arrangement view
- `piano_roll.py` - Piano roll editor  
- `track_panel.py` - Track list/mixer

## Common Patterns

### Creating a Custom Drawing Widget
```python
class MyWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # Drawing code here
    
    def mousePressEvent(self, event):
        # Handle clicks
        pass
```

### Synchronized Scrolling
```python
# Connect scroll bars
scroll1.verticalScrollBar().valueChanged.connect(
    scroll2.verticalScrollBar().setValue
)
```

### Color Management
```python
# RGB
color = QColor(255, 128, 0)
# Hex
color = QColor('#ff8000')
# RGBA
color = QColor(255, 128, 0, 180)
```

## Testing Checklist

- [ ] No flicker during redraw
- [ ] Drag and drop works smoothly
- [ ] Transparency renders correctly
- [ ] Scroll synchronization works
- [ ] Modal dialogs block properly
- [ ] All keyboard shortcuts work
- [ ] Performance is acceptable

## Additional Resources

- Qt Documentation: https://doc.qt.io/qtforpython/
- PySide6 Examples: https://github.com/qt/pyside-examples
- Qt Style Sheets: https://doc.qt.io/qt-6/stylesheet.html

## Notes

The converted code maintains the same logic flow as the original but uses Qt's more robust widget system. The main benefits:

1. **No flicker** - Qt uses proper double buffering
2. **Native drag-drop** - Built into Qt's event system  
3. **Proper transparency** - QPainter handles alpha channels correctly
4. **Better performance** - Optimized C++ rendering backend
5. **Cross-platform** - Better consistency across OS platforms
