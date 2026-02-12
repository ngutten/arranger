"""Left panel - pattern and beat pattern lists with drag support."""

from PySide6.QtWidgets import (QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, 
                                QScrollArea, QWidget)
from PySide6.QtCore import Qt, QMimeData, Signal
from PySide6.QtGui import QPainter, QColor, QDrag, QFont

from ..state import PALETTE


class PatternList(QFrame):
    """Left panel containing melodic pattern list and beat pattern list."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self.setFixedWidth(260)
        self._drag_data = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Melodic patterns section
        hdr = QFrame()
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        title_label = QLabel('Patterns')
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        title_label.setFont(font)
        hdr_layout.addWidget(title_label)
        hdr_layout.addStretch()
        new_btn = QPushButton('+ New')
        new_btn.setMaximumWidth(60)
        new_btn.clicked.connect(self._new_pattern)
        hdr_layout.addWidget(new_btn)
        layout.addWidget(hdr)

        # Pattern scroll area
        self.pat_scroll = QScrollArea()
        self.pat_scroll.setWidgetResizable(True)
        self.pat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pat_container = QWidget()
        self.pat_layout = QVBoxLayout(self.pat_container)
        self.pat_layout.setContentsMargins(0, 0, 0, 0)
        self.pat_layout.setSpacing(1)
        self.pat_layout.addStretch()
        self.pat_scroll.setWidget(self.pat_container)
        layout.addWidget(self.pat_scroll, stretch=1)

        # Key info label
        self.key_info = QLabel('')
        font = QFont()
        font.setPointSize(9)
        self.key_info.setFont(font)
        layout.addWidget(self.key_info)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep)

        # Beat patterns section
        bhdr = QFrame()
        bhdr_layout = QHBoxLayout(bhdr)
        bhdr_layout.setContentsMargins(0, 0, 0, 0)
        btitle_label = QLabel('Beat Patterns')
        btitle_label.setFont(font)
        bhdr_layout.addWidget(btitle_label)
        bhdr_layout.addStretch()
        bnew_btn = QPushButton('+ New')
        bnew_btn.setMaximumWidth(60)
        bnew_btn.clicked.connect(self._new_beat_pattern)
        bhdr_layout.addWidget(bnew_btn)
        layout.addWidget(bhdr)

        # Beat pattern scroll area
        self.beat_scroll = QScrollArea()
        self.beat_scroll.setWidgetResizable(True)
        self.beat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.beat_container = QWidget()
        self.beat_layout = QVBoxLayout(self.beat_container)
        self.beat_layout.setContentsMargins(0, 0, 0, 0)
        self.beat_layout.setSpacing(1)
        self.beat_layout.addStretch()
        self.beat_scroll.setWidget(self.beat_container)
        layout.addWidget(self.beat_scroll, stretch=1)

    def _new_pattern(self):
        from ..ops.patterns import add_pattern
        add_pattern(self.state)

    def _new_beat_pattern(self):
        from ..ops.patterns import add_beat_pattern
        add_beat_pattern(self.state)

    def refresh(self):
        """Rebuild pattern lists from state."""
        self._render_patterns()
        self._render_beat_patterns()
        # Key info
        pat = self.state.find_pattern(self.state.sel_pat)
        if pat:
            self.key_info.setText(f'Key: {pat.key} {pat.scale}')
        else:
            self.key_info.setText('')

    def _render_patterns(self):
        # Clear existing widgets except stretch
        while self.pat_layout.count() > 1:
            item = self.pat_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        for pat in self.state.patterns:
            item = PatternItem(self, pat, self.state.sel_pat == pat.id)
            self.pat_layout.insertWidget(self.pat_layout.count() - 1, item)

    def _render_beat_patterns(self):
        # Clear existing widgets except stretch
        while self.beat_layout.count() > 1:
            item = self.beat_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not self.state.beat_patterns:
            lbl = QLabel('No beat patterns')
            lbl.setStyleSheet('color: #666;')
            font = QFont()
            font.setPointSize(9)
            lbl.setFont(font)
            lbl.setAlignment(Qt.AlignCenter)
            self.beat_layout.insertWidget(0, lbl)
            return

        for pat in self.state.beat_patterns:
            item = BeatPatternItem(self, pat, self.state.sel_beat_pat == pat.id)
            self.beat_layout.insertWidget(self.beat_layout.count() - 1, item)

    def _select_pat(self, pid):
        self.state.sel_pat = pid
        self.state.sel_beat_pat = None
        # Clear piano roll selection when selecting pattern
        self.app.piano_roll.clear_selection()
        # Clear arrangement selection when selecting pattern
        self.app.arrangement.selected_placements = []
        self.app.arrangement.selected_beat_placements = []
        self.state.notify('sel_pat')

    def _select_beat_pat(self, pid):
        self.state.sel_beat_pat = pid
        self.state.sel_pat = None
        # Clear piano roll selection when selecting beat pattern
        self.app.piano_roll.clear_selection()
        # Clear arrangement selection when selecting pattern
        self.app.arrangement.selected_placements = []
        self.app.arrangement.selected_beat_placements = []
        self.state.notify('sel_beat_pat')

    def _del_pat(self, pid):
        from ..ops.patterns import delete_pattern
        delete_pattern(self.state, pid)

    def _dup_pat(self, pid):
        from ..ops.patterns import duplicate_pattern
        duplicate_pattern(self.state, pid)

    def _del_beat_pat(self, pid):
        from ..ops.patterns import delete_beat_pattern
        delete_beat_pattern(self.state, pid)

    def _dup_beat_pat(self, pid):
        from ..ops.patterns import duplicate_beat_pattern
        duplicate_beat_pattern(self.state, pid)


class PatternItem(QFrame):
    """Single pattern item in the list."""

    def __init__(self, parent_list, pattern, selected):
        super().__init__(parent_list)
        self.parent_list = parent_list
        self.pattern = pattern
        self.setFrameStyle(QFrame.Box if selected else QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.drag_start_pos = None
        self.drag_timer = None
        
        bg_color = '#1e2a4a' if selected else '#16213e'
        border_color = '#e94560' if selected else '#16213e'
        self.setStyleSheet(f'background-color: {bg_color}; border: 1px solid {border_color};')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 4, 4)
        layout.setSpacing(4)

        # Color dot
        dot_widget = ColorDot(pattern.color)
        dot_widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(dot_widget)

        # Text container with two lines
        text_container = QWidget()
        text_container.setStyleSheet('background-color: transparent; border: none;')
        text_container.setAttribute(Qt.WA_TransparentForMouseEvents)
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)
        
        # Name on first line
        name_label = QLabel(pattern.name)
        name_label.setStyleSheet('color: #eee; background-color: transparent; border: none;')
        name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        # Prevent text from expanding and enable elision
        name_label.setWordWrap(False)
        from PySide6.QtWidgets import QSizePolicy
        name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        name_font = QFont()
        name_font.setPointSize(9)
        name_label.setFont(name_font)
        text_layout.addWidget(name_label)
        
        # Details on second line (smaller font)
        info = f'{pattern.length}b · {pattern.key} {pattern.scale}'
        info_label = QLabel(info)
        info_label.setStyleSheet('color: #888; background-color: transparent; border: none;')
        info_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        info_font = QFont()
        info_font.setPointSize(7)
        info_label.setFont(info_font)
        text_layout.addWidget(info_label)
        
        layout.addWidget(text_container, stretch=1)

        # Overlay mode button
        overlay_btn = QPushButton(self._overlay_symbol(pattern.overlay_mode))
        overlay_btn.setStyleSheet('background-color: transparent; color: #aaa; border: 1px solid #555; font-size: 14px; padding: 2px;')
        overlay_btn.setFixedWidth(26)
        overlay_btn.setToolTip(self._overlay_tooltip(pattern.overlay_mode))
        overlay_btn.clicked.connect(lambda: self._toggle_overlay(pattern.id))
        layout.addWidget(overlay_btn)
        self.overlay_btn = overlay_btn

        # Action buttons - fixed width container so buttons don't get pushed off
        btn_frame = QFrame()
        btn_frame.setStyleSheet('background-color: transparent; border: none;')
        btn_frame.setFixedWidth(84)
        btn_layout = QHBoxLayout(btn_frame)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(2)

        # Copy button (overlapping squares icon) - with visible border for debugging
        dup_btn = QPushButton('⧉')
        dup_btn.setStyleSheet('background-color: transparent; color: #aaa; border: 1px solid #555; font-size: 16px; padding: 2px;')
        dup_btn.setFixedWidth(26)
        dup_btn.setToolTip('Duplicate pattern')
        dup_btn.clicked.connect(lambda: parent_list._dup_pat(pattern.id))
        btn_layout.addWidget(dup_btn)

        # Edit button (quill/pen icon)
        edit_btn = QPushButton('✎')
        edit_btn.setStyleSheet('background-color: transparent; color: #aaa; border: 1px solid #555; font-size: 16px; padding: 2px;')
        edit_btn.setFixedWidth(26)
        edit_btn.setToolTip('Edit pattern')
        edit_btn.clicked.connect(lambda: parent_list.app.show_pattern_dialog(pattern.id))
        btn_layout.addWidget(edit_btn)

        # Delete button
        del_btn = QPushButton('✕')
        del_btn.setStyleSheet('background-color: transparent; color: #aaa; border: 1px solid #555; font-size: 16px; padding: 2px;')
        del_btn.setFixedWidth(26)
        del_btn.setToolTip('Delete pattern')
        del_btn.clicked.connect(lambda: parent_list._del_pat(pattern.id))
        btn_layout.addWidget(del_btn)

        layout.addWidget(btn_frame)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.parent_list._select_pat(self.pattern.id)

    def _overlay_symbol(self, mode):
        """Get symbol for overlay mode."""
        if mode == 'off':
            return '○'  # Empty circle
        elif mode == 'playing':
            return '◐'  # Half-filled circle
        else:  # 'always'
            return '●'  # Filled circle
    
    def _overlay_tooltip(self, mode):
        """Get tooltip for overlay mode."""
        if mode == 'off':
            return 'Overlay: Off (click to set Playing)'
        elif mode == 'playing':
            return 'Overlay: When Playing (click to set Always)'
        else:  # 'always'
            return 'Overlay: Always On (click to set Off)'
    
    def _toggle_overlay(self, pattern_id):
        """Cycle through overlay modes."""
        pat = self.parent_list.state.find_pattern(pattern_id)
        if not pat:
            return
        
        # Cycle: off -> playing -> always -> off
        modes = ['off', 'playing', 'always']
        current_idx = modes.index(pat.overlay_mode) if pat.overlay_mode in modes else 1
        next_idx = (current_idx + 1) % len(modes)
        pat.overlay_mode = modes[next_idx]
        
        # Update button
        self.overlay_btn.setText(self._overlay_symbol(pat.overlay_mode))
        self.overlay_btn.setToolTip(self._overlay_tooltip(pat.overlay_mode))
        
        # Notify state change to trigger piano roll refresh
        self.parent_list.state.notify('overlay_mode')


class BeatPatternItem(QFrame):
    """Single beat pattern item in the list."""

    def __init__(self, parent_list, pattern, selected):
        super().__init__(parent_list)
        self.parent_list = parent_list
        self.pattern = pattern
        self.setFrameStyle(QFrame.Box if selected else QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.drag_start_pos = None
        self.drag_timer = None
        
        bg_color = '#1e2a4a' if selected else '#16213e'
        border_color = '#e94560' if selected else '#16213e'
        self.setStyleSheet(f'background-color: {bg_color}; border: 1px solid {border_color};')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 4, 4)
        layout.setSpacing(4)

        # Color dot
        dot_widget = ColorDot(pattern.color)
        dot_widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(dot_widget)

        # Text container with two lines
        text_container = QWidget()
        text_container.setStyleSheet('background-color: transparent; border: none;')
        text_container.setAttribute(Qt.WA_TransparentForMouseEvents)
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)
        
        # Name on first line
        name_label = QLabel(pattern.name)
        name_label.setStyleSheet('color: #eee; background-color: transparent; border: none;')
        name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        # Prevent text from expanding and enable elision
        name_label.setWordWrap(False)
        from PySide6.QtWidgets import QSizePolicy
        name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        name_font = QFont()
        name_font.setPointSize(9)
        name_label.setFont(name_font)
        text_layout.addWidget(name_label)
        
        # Details on second line (smaller font)
        info = f'{pattern.length}b · ÷{pattern.subdivision}'
        info_label = QLabel(info)
        info_label.setStyleSheet('color: #888; background-color: transparent; border: none;')
        info_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        info_font = QFont()
        info_font.setPointSize(7)
        info_label.setFont(info_font)
        text_layout.addWidget(info_label)
        
        layout.addWidget(text_container, stretch=1)

        # Action buttons - fixed width container so buttons don't get pushed off
        btn_frame = QFrame()
        btn_frame.setStyleSheet('background-color: transparent; border: none;')
        btn_frame.setFixedWidth(84)
        btn_layout = QHBoxLayout(btn_frame)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(2)

        # Copy button (overlapping squares icon) - with visible border for debugging
        dup_btn = QPushButton('⧉')
        dup_btn.setStyleSheet('background-color: transparent; color: #aaa; border: 1px solid #555; font-size: 16px; padding: 2px;')
        dup_btn.setFixedWidth(26)
        dup_btn.setToolTip('Duplicate pattern')
        dup_btn.clicked.connect(lambda: parent_list._dup_beat_pat(pattern.id))
        btn_layout.addWidget(dup_btn)

        # Edit button (quill/pen icon)
        edit_btn = QPushButton('✎')
        edit_btn.setStyleSheet('background-color: transparent; color: #aaa; border: 1px solid #555; font-size: 16px; padding: 2px;')
        edit_btn.setFixedWidth(26)
        edit_btn.setToolTip('Edit pattern')
        edit_btn.clicked.connect(lambda: parent_list.app.show_beat_pattern_dialog(pattern.id))
        btn_layout.addWidget(edit_btn)

        # Delete button
        del_btn = QPushButton('✕')
        del_btn.setStyleSheet('background-color: transparent; color: #aaa; border: 1px solid #555; font-size: 16px; padding: 2px;')
        del_btn.setFixedWidth(26)
        del_btn.setToolTip('Delete pattern')
        del_btn.clicked.connect(lambda: parent_list._del_beat_pat(pattern.id))
        btn_layout.addWidget(del_btn)

        layout.addWidget(btn_frame)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.parent_list._select_beat_pat(self.pattern.id)


class ColorDot(QWidget):
    """Small circular color indicator."""

    def __init__(self, color):
        super().__init__()
        self.color = QColor(color)
        self.setFixedSize(12, 12)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.color)
        painter.drawEllipse(2, 2, 8, 8)
