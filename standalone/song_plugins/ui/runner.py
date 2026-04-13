"""Threaded plugin runner with a Qt-signal-based Progress implementation."""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..api import PluginResult, SongPlugin
from ..song_view import SongView

logger = logging.getLogger(__name__)


class QtProgress(QObject):
    """Qt-signal-bearing Progress implementation.

    Signals are emitted on the worker thread; connect them with
    ``Qt.QueuedConnection`` for thread-safe UI updates.
    """

    phase_changed = Signal(str)
    progress_changed = Signal(float, object)  # fraction, message (str | None)

    def __init__(self):
        super().__init__()
        self._cancel = threading.Event()

    # -- Progress protocol -----------------------------------------------

    def phase(self, name: str) -> None:
        try:
            self.phase_changed.emit(str(name))
        except RuntimeError:
            # Receiver deleted mid-run; swallow.
            pass

    def update(self, fraction: float, message: Optional[str] = None) -> None:
        try:
            self.progress_changed.emit(float(fraction), message)
        except RuntimeError:
            pass

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()


class PluginRunner(QObject):
    """Owns a single in-flight run of a plugin on a daemon thread."""

    finished = Signal(object)     # PluginResult
    failed = Signal(object)       # Exception

    def __init__(self, plugin: SongPlugin,
                 view_factory: Callable[[], SongView],
                 params: dict):
        super().__init__()
        self._plugin = plugin
        self._view_factory = view_factory
        self._params = dict(params)
        self.progress = QtProgress()
        self._thread: Optional[threading.Thread] = None
        self._alive = True

    # -- Public API ------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.progress.cancel()

    def run_blocking(self) -> PluginResult:
        """Synchronous run used by tests. Exceptions propagate."""
        view = self._view_factory()
        return self._plugin.run(view, self._params, self.progress)

    # -- Internals -------------------------------------------------------

    def _work(self) -> None:
        try:
            view = self._view_factory()
            result = self._plugin.run(view, self._params, self.progress)
        except Exception as exc:  # noqa: BLE001 — report it
            logger.exception("Plugin run failed: %s", exc)
            self._emit_failed(exc)
            return
        self._emit_finished(result)

    def _emit_finished(self, result) -> None:
        if not self._alive:
            return
        try:
            self.finished.emit(result)
        except RuntimeError:
            pass

    def _emit_failed(self, exc) -> None:
        if not self._alive:
            return
        try:
            self.failed.emit(exc)
        except RuntimeError:
            pass

    def shutdown(self) -> None:
        self._alive = False
        self.cancel()
