"""Debug instrumentation for PySide6 widget lifecycle issues.

Hooks into widget creation, deletion, and event processing to detect
use-after-free bugs where Python GC and C++ Qt ownership disagree.

Usage:
    from .debug_widgets import install_hooks
    install_hooks()  # call before QApplication or right after

The module logs to stderr and optionally to a file. When a potential
use-after-free is detected, it logs a traceback of the offending access
and (when possible) the original deletion traceback.

Key suspects in the sequencer:
  1. PatternItem / BeatPatternItem in pattern_list.py — created fresh on
     every refresh(), old ones deleteLater()'d. If a queued event (paint,
     mouse) fires between takeAt() and actual C++ destruction, the
     Shiboken wrapper is already invalid.
  2. _switch_editor() hides piano_roll/beat_grid and re-parents them.
     If a queued event targets the hidden widget's children after
     re-parenting, the C++ pointer chain can be stale.
  3. track_panel.py _clear_frame() same deleteLater() pattern.
  4. Undo/redo replaces entire state lists, triggering _refresh_all()
     which does deleteLater() in pattern_list + track_panel while
     arrangement/piano_roll still reference old state objects.

The 0x55 pattern in the valgrind log (Address 0x5555555555555555) is the
freed-memory fill, confirming a read of already-freed C++ memory.
"""

import sys
import traceback
import weakref
import functools
import time
import shiboken6
from PySide6.QtWidgets import QWidget, QApplication
from PySide6.QtCore import QEvent, QObject

# ── Configuration ──────────────────────────────────────────────────

LOG_FILE = "widget_debug.log"    # set to None for stderr only
TRACK_CLASSES = None             # None = all QWidget subclasses; or set({'PatternItem', ...})
VERBOSE = False                  # log every event dispatch (very noisy)

# ── State ──────────────────────────────────────────────────────────

_tracked = {}        # id(obj) -> {class, created_tb, deleted_tb, repr, weak}
_log_fh = None
_original_deleteLater = None
_original_notify = None
_installed = False


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[WDG {ts}] {msg}\n"
    sys.stderr.write(line)
    if _log_fh:
        _log_fh.write(line)
        _log_fh.flush()


def _short_tb(skip=2):
    """Return a compact traceback string, skipping the top `skip` frames."""
    frames = traceback.extract_stack()[:-skip]
    # Only keep frames from our project, not Qt/shiboken internals
    relevant = [f for f in frames if '/seq/' in f.filename or 'standalone' in f.filename]
    if not relevant:
        relevant = frames[-5:]  # fallback: last 5 frames
    return " <- ".join(f"{f.filename.split('/')[-1]}:{f.lineno}({f.name})" for f in relevant[-6:])


def _widget_desc(w):
    """Best-effort description of a widget."""
    try:
        cls = type(w).__name__
        name = w.objectName() if shiboken6.isValid(w) else "???"
        parent_cls = type(w.parent()).__name__ if shiboken6.isValid(w) and w.parent() else "None"
        return f"{cls}(name={name}, parent={parent_cls}, id={id(w):#x})"
    except (RuntimeError, ReferenceError):
        return f"<DEAD widget id={id(w):#x}>"


def _should_track(w):
    if TRACK_CLASSES is None:
        return True
    return type(w).__name__ in TRACK_CLASSES


# ── Monkey-patches ─────────────────────────────────────────────────

def _patched_init(original_init, self, *args, **kwargs):
    """Wrap QWidget.__init__ to track creation."""
    original_init(self, *args, **kwargs)
    if _should_track(self):
        tb = _short_tb(skip=2)
        _tracked[id(self)] = {
            'class': type(self).__name__,
            'created_tb': tb,
            'deleted_tb': None,
            'repr': _widget_desc(self),
            'weak': weakref.ref(self, functools.partial(_ref_collected, id(self))),
            'alive': True,
        }
        if VERBOSE:
            _log(f"CREATED {_widget_desc(self)} at {tb}")


def _patched_deleteLater(self):
    """Wrap deleteLater to record when a widget is scheduled for deletion."""
    wid = id(self)
    tb = _short_tb(skip=2)
    desc = _widget_desc(self)

    if wid in _tracked:
        entry = _tracked[wid]
        if entry['deleted_tb'] is not None:
            _log(f"⚠ DOUBLE deleteLater on {desc}\n"
                 f"    first: {entry['deleted_tb']}\n"
                 f"    second: {tb}")
        entry['deleted_tb'] = tb
        entry['alive'] = False
        _log(f"DELETE-LATER {desc} at {tb}")
    else:
        _log(f"DELETE-LATER (untracked) {desc} at {tb}")

    _original_deleteLater(self)


def _ref_collected(wid, ref):
    """Called when Python GC collects a tracked widget."""
    entry = _tracked.pop(wid, None)
    if entry and entry['deleted_tb'] is None:
        _log(f"⚠ GC-COLLECTED without deleteLater: {entry['repr']} "
             f"(class={entry['class']})\n"
             f"    created at: {entry['created_tb']}")


# ── Event filter for detecting access to dead widgets ──────────────

class WidgetLifecycleFilter(QObject):
    """Global event filter that checks widget validity before delivery."""

    # Events that are especially dangerous on dead widgets
    RISKY_EVENTS = {
        QEvent.Type.Paint, QEvent.Type.Resize, QEvent.Type.Move,
        QEvent.Type.Show, QEvent.Type.Hide,
        QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseMove, QEvent.Type.Enter, QEvent.Type.Leave,
        QEvent.Type.FocusIn, QEvent.Type.FocusOut,
        QEvent.Type.ChildAdded, QEvent.Type.ChildRemoved,
        QEvent.Type.DeferredDelete,
        QEvent.Type.LayoutRequest,
    }

    def eventFilter(self, obj, event):
        if not isinstance(obj, QWidget):
            return False

        # Check Shiboken validity
        if not shiboken6.isValid(obj):
            _log(f"🔥 EVENT on INVALID widget! event={event.type()} "
                 f"obj_id={id(obj):#x}")
            entry = _tracked.get(id(obj))
            if entry:
                _log(f"    class={entry['class']}\n"
                     f"    created: {entry['created_tb']}\n"
                     f"    deleted: {entry['deleted_tb']}")
            _log(f"    current tb: {_short_tb(skip=2)}")
            return True  # swallow the event to prevent crash

        if VERBOSE and event.type() in self.RISKY_EVENTS:
            wid = id(obj)
            if wid in _tracked:
                entry = _tracked[wid]
                if entry['deleted_tb'] is not None:
                    _log(f"⚠ EVENT {event.type()} on deleteLater'd widget: "
                         f"{_widget_desc(obj)}\n"
                         f"    deleted at: {entry['deleted_tb']}")

        # Special attention to DeferredDelete (this is when deleteLater actually fires)
        if event.type() == QEvent.Type.DeferredDelete:
            desc = _widget_desc(obj)
            wid = id(obj)
            entry = _tracked.get(wid)
            if entry:
                _log(f"DEFERRED-DELETE executing: {desc}")
            else:
                cls = type(obj).__name__
                _log(f"DEFERRED-DELETE executing (untracked): {cls} id={wid:#x}")

        return False


# ── Refresh-cycle guard ────────────────────────────────────────────
# Detect when deleteLater'd widgets still have pending events

_refresh_count = 0
_pending_deletes = []  # list of (widget_desc, tb) added during refresh


def mark_refresh_start():
    """Call at the start of _refresh_all to begin tracking."""
    global _refresh_count
    _refresh_count += 1
    _pending_deletes.clear()


def mark_refresh_end():
    """Call at the end of _refresh_all. Warns if deletes happened mid-refresh."""
    if _pending_deletes:
        _log(f"⚠ REFRESH #{_refresh_count} created {len(_pending_deletes)} "
             f"pending deleteLater calls:")
        for desc, tb in _pending_deletes:
            _log(f"    {desc} at {tb}")


# ── Validity checker you can sprinkle in suspicious code ───────────

def check_valid(widget, context=""):
    """Call this to assert a widget is still valid. Logs and returns False if dead."""
    if not shiboken6.isValid(widget):
        _log(f"🔥 check_valid FAILED: {context} widget id={id(widget):#x}")
        entry = _tracked.get(id(widget))
        if entry:
            _log(f"    class={entry['class']}\n"
                 f"    created: {entry['created_tb']}\n"
                 f"    deleted: {entry['deleted_tb']}")
        _log(f"    tb: {_short_tb(skip=2)}")
        return False
    return True


# ── Install ────────────────────────────────────────────────────────

def install_hooks():
    """Install all debug hooks. Call once, early in startup."""
    global _log_fh, _original_deleteLater, _installed

    if _installed:
        return
    _installed = True

    if LOG_FILE:
        _log_fh = open(LOG_FILE, 'w')

    _log("=== Widget lifecycle debug hooks installed ===")

    # Patch deleteLater
    _original_deleteLater = QWidget.deleteLater
    QWidget.deleteLater = _patched_deleteLater

    # Install global event filter (requires QApplication to exist)
    # We defer this to first use if QApp doesn't exist yet
    _try_install_event_filter()

    _log("Hooks ready. TRACK_CLASSES=%s VERBOSE=%s" % (TRACK_CLASSES, VERBOSE))


def _try_install_event_filter():
    app = QApplication.instance()
    if app:
        filt = WidgetLifecycleFilter(app)
        app.installEventFilter(filt)
        # prevent GC
        app._widget_debug_filter = filt
        _log("Event filter installed on QApplication")
    else:
        _log("QApplication not yet created; event filter will be installed later.")
        # Caller should call install_event_filter() after QApp creation


def install_event_filter():
    """Install the event filter. Call after QApplication is created."""
    _try_install_event_filter()
