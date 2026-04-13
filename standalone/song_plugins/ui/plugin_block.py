"""PluginBlock — one widget per active plugin instance in the dock."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..api import (
    Annotation, PluginManifest, PluginResult,
    SelectionEmpty, SelectionMismatch, is_broadcast_eligible,
)
from ..apply_ops import OperationError, apply_ops
from .param_widgets import build_param_widget, evaluate_visible_when
from .renderers import make_renderer
from .renderers.operations_preview import OperationsPreview
from .runner import PluginRunner

logger = logging.getLogger(__name__)

# Status glyphs (unicode, no emoji)
_GLYPH_IDLE = "\u25cb"      # hollow circle
_GLYPH_OK = "\u25cf"        # filled circle
_GLYPH_RUNNING = "\u27f3"   # clockwise gapped arrow
_GLYPH_STALE = "\u25d0"     # half-filled circle
_GLYPH_ERROR = "!"


class PluginBlock(QFrame):
    """Self-contained plugin instance widget.

    Header (name + status + Run + Live + close) / param form /
    output region / footer status line.
    """

    # Signal emitted when the user dismisses the block.
    removed = Signal(object)  # self

    def __init__(self, host, plugin_cls, parent=None):
        super().__init__(parent)
        self.host = host
        self.plugin_cls = plugin_cls
        self.plugin = plugin_cls()
        self.manifest: PluginManifest = plugin_cls.manifest
        self.instance_id = uuid.uuid4().hex[:8]

        self._runner: Optional[PluginRunner] = None
        self._last_annotation: Optional[Annotation] = None
        self._pending_ops: Optional[tuple] = None
        self._ops_preview: Optional[OperationsPreview] = None
        caps = tuple(self.manifest.capabilities or ())
        self._has_transform = ("transform" in caps) or ("generate" in caps)
        self._has_analyze = "analyze" in caps
        self._supports_apply = self._has_transform
        # Transform-only: collapse Run+Apply into a single Apply click that
        # runs the plugin and immediately commits the operations.
        self._transform_only = self._has_transform and not self._has_analyze
        # Set when the next runner-finished callback should auto-apply the
        # resulting ops (for the transform-only single-click flow).
        self._apply_after_run = False
        self._live_debounce = QTimer(self)
        self._live_debounce.setSingleShot(True)
        self._live_debounce.setInterval(200)
        self._live_debounce.timeout.connect(self._run_now)

        self._param_getters: dict = {}
        self._param_widgets: dict = {}
        self._param_rows: dict = {}  # key -> (label_widget, field_widget)

        self._build_ui()
        self._set_status("idle", "Ready.")
        host.register_block(self)

    # -- Build -----------------------------------------------------------

    def _build_ui(self) -> None:
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "PluginBlock { background-color: #1a1d33; border: 1px solid #2a2a4a;"
            " border-radius: 3px; }"
        )
        self.setMinimumSize(260, 200)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        # Prefer ~360px wide.
        self._preferred_width = 360

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # -- Header --
        header = QHBoxLayout()
        header.setSpacing(6)

        self._status_label = QLabel(_GLYPH_IDLE)
        self._status_label.setFixedWidth(14)
        self._status_label.setStyleSheet("color: #888;")
        header.addWidget(self._status_label)

        title = QLabel(self.manifest.name)
        f = QFont()
        f.setBold(True)
        title.setFont(f)
        title.setToolTip(self.manifest.description or self.manifest.id)
        header.addWidget(title)
        header.addStretch()

        # Run button: hidden for transform-only plugins (single-click Apply
        # does both). Still created so internal callers can toggle enabled
        # state uniformly; just never added to the header.
        self._run_btn = QPushButton("Run")
        self._run_btn.setMaximumWidth(60)
        self._run_btn.clicked.connect(self._on_run_clicked)
        if not self._transform_only:
            header.addWidget(self._run_btn)
        else:
            self._run_btn.hide()

        self._live_cb: Optional[QCheckBox] = None
        if self.manifest.live_supported:
            self._live_cb = QCheckBox("Live")
            self._live_cb.setToolTip("Re-run automatically when relevant state changes.")
            self._live_cb.toggled.connect(self._on_live_toggled)
            header.addWidget(self._live_cb)

        self._apply_btn: Optional[QPushButton] = None
        if self._supports_apply:
            self._apply_btn = QPushButton("Apply")
            self._apply_btn.setMaximumWidth(60)
            if self._transform_only:
                self._apply_btn.setToolTip(
                    "Run the plugin and commit the resulting operations."
                )
                # No pending-ops gating; Apply is always enabled for
                # transform-only plugins (until a run is in flight).
                self._apply_btn.setEnabled(True)
            else:
                self._apply_btn.setToolTip(
                    "Commit the pending operations to the project."
                )
                self._apply_btn.setEnabled(False)
            self._apply_btn.clicked.connect(self._on_apply_clicked)
            header.addWidget(self._apply_btn)

        # Broadcast toggle — shown only when the plugin's schemas include
        # a beat-time axis (or manifest.broadcast_eligible explicitly
        # forces it on). Checking this claims the shared broadcast band
        # near the arranger; unchecking (or another block taking over)
        # releases it. Radio-button semantics are enforced by the host.
        self._broadcast_cb: Optional[QCheckBox] = None
        if is_broadcast_eligible(self.manifest):
            self._broadcast_cb = QCheckBox("Broadcast")
            self._broadcast_cb.setToolTip(
                "Show this plugin's visualization in the broadcast band."
            )
            self._broadcast_cb.toggled.connect(self._on_broadcast_toggled)
            header.addWidget(self._broadcast_cb)

        close_btn = QPushButton("\u00d7")  # ×
        close_btn.setFixedWidth(22)
        close_btn.setToolTip("Remove plugin")
        close_btn.clicked.connect(self._on_remove_clicked)
        header.addWidget(close_btn)

        root.addLayout(header)

        # -- Params (collapsible-ish: a toggle button + inner form) --
        self._params_toggle = QPushButton("Parameters")
        self._params_toggle.setCheckable(True)
        self._params_toggle.setChecked(True)
        self._params_toggle.setStyleSheet(
            "text-align: left; background-color: #1a1a2e; padding: 3px 6px;"
        )
        self._params_toggle.toggled.connect(self._on_params_toggled)
        root.addWidget(self._params_toggle)

        self._params_box = QFrame()
        self._params_form = QFormLayout(self._params_box)
        self._params_form.setContentsMargins(4, 2, 4, 2)
        self._params_form.setSpacing(3)
        for spec in self.manifest.params:
            widget, getter = build_param_widget(spec)
            label = QLabel(spec.label or spec.key)
            self._params_form.addRow(label, widget)
            self._param_widgets[spec.key] = widget
            self._param_getters[spec.key] = getter
            self._param_rows[spec.key] = (label, widget)
            self._wire_param_signal(spec.key, widget)
        root.addWidget(self._params_box)

        # -- Output region --
        self._output_holder = QFrame()
        self._output_layout = QVBoxLayout(self._output_holder)
        self._output_layout.setContentsMargins(0, 0, 0, 0)
        self._output_placeholder = QLabel("Run to see output.")
        self._output_placeholder.setAlignment(Qt.AlignCenter)
        self._output_placeholder.setStyleSheet("color: #666; font-style: italic;")
        self._output_placeholder.setMinimumHeight(80)
        self._output_layout.addWidget(self._output_placeholder)
        self._renderer: Optional[QWidget] = None
        # Transform-only plugins have no Run step, so "Run to see output"
        # is misleading — give the output region zero visible footprint.
        if self._transform_only:
            self._output_placeholder.hide()
            self._output_holder.hide()
            root.addWidget(self._output_holder)
        else:
            root.addWidget(self._output_holder, stretch=1)

        # -- Footer --
        self._footer = QLabel("")
        self._footer.setWordWrap(True)
        self._footer.setStyleSheet("color: #8a94b0; font-size: 10px;")
        root.addWidget(self._footer)

        # Initial param visibility pass.
        self._refresh_param_visibility()

    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(self._preferred_width, 280)

    # -- Param handling --------------------------------------------------

    def _wire_param_signal(self, key, widget) -> None:
        """Connect whatever change signal the widget offers so visibility
        filters and live-mode re-runs fire."""
        for sig_name in ("valueChanged", "toggled", "currentIndexChanged",
                         "textChanged"):
            sig = getattr(widget, sig_name, None)
            if sig is not None:
                try:
                    sig.connect(self._on_param_changed)
                    return
                except Exception:
                    continue

    def _on_param_changed(self, *_args) -> None:
        self._refresh_param_visibility()
        # Any param change invalidates pending operations — the user must
        # Run again to recompute against the current params/state.
        had_output = (self._last_annotation is not None
                      or self._pending_ops is not None)
        self._discard_pending_ops("Params changed; Run again to refresh.")
        if had_output:
            self._set_status("stale", self._footer.text())
            if self._live_cb is not None and self._live_cb.isChecked():
                self._schedule_live_rerun()

    def _refresh_param_visibility(self) -> None:
        values = self._current_params(only_visible=False)
        for spec in self.manifest.params:
            label, widget = self._param_rows[spec.key]
            visible = evaluate_visible_when(spec.visible_when, values)
            label.setVisible(visible)
            widget.setVisible(visible)

    def _current_params(self, only_visible: bool = True) -> dict:
        values: dict = {}
        for spec in self.manifest.params:
            getter = self._param_getters.get(spec.key)
            if getter is None:
                continue
            try:
                values[spec.key] = getter()
            except Exception:
                values[spec.key] = spec.default
        if only_visible:
            visible_map = {}
            for spec in self.manifest.params:
                if evaluate_visible_when(spec.visible_when, values):
                    visible_map[spec.key] = values[spec.key]
            return visible_map
        return values

    # -- Status + footer -------------------------------------------------

    def _set_status(self, status: str, footer: str = "") -> None:
        glyph_map = {
            "idle": (_GLYPH_IDLE, "#8a94b0"),
            "running": (_GLYPH_RUNNING, "#e0c060"),
            "ok": (_GLYPH_OK, "#6fdd8a"),
            "stale": (_GLYPH_STALE, "#e0a060"),
            "error": (_GLYPH_ERROR, "#e94560"),
        }
        glyph, color = glyph_map.get(status, (_GLYPH_IDLE, "#8a94b0"))
        self._status_label.setText(glyph)
        self._status_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        if footer is not None:
            self._footer.setText(footer)
        self._current_status = status

    # -- Buttons / toggles ----------------------------------------------

    def _on_params_toggled(self, checked: bool) -> None:
        self._params_box.setVisible(checked)

    def _on_run_clicked(self) -> None:
        if self._runner is not None:
            return  # already in-flight
        self._run_now()

    def _on_live_toggled(self, checked: bool) -> None:
        if checked and self._last_annotation is None:
            self._schedule_live_rerun()
        elif not checked:
            self._live_debounce.stop()

    def _on_broadcast_toggled(self, checked: bool) -> None:
        """User toggled the Broadcast checkbox on this block."""
        host = self.host
        if checked:
            # Radio-button: host.set_broadcaster un-checks any prior block.
            if hasattr(host, "set_broadcaster"):
                host.set_broadcaster(self)
        else:
            # Only clear if we're the current broadcaster. If we were
            # merely released by another block taking over, the host
            # already handled the state — calling clear_broadcaster here
            # would wrongly wipe the new broadcaster.
            cur = None
            if hasattr(host, "current_broadcaster"):
                cur = host.current_broadcaster()
            if cur is self and hasattr(host, "clear_broadcaster"):
                host.clear_broadcaster()

    def _on_broadcast_released(self) -> None:
        """Host hook: another block took over (or the band was cleared).

        Untick our own checkbox without re-firing the toggled signal —
        otherwise ``_on_broadcast_toggled(False)`` would try to
        ``clear_broadcaster`` and clobber the new broadcaster.
        """
        if self._broadcast_cb is None:
            return
        blocked = self._broadcast_cb.blockSignals(True)
        try:
            self._broadcast_cb.setChecked(False)
        finally:
            self._broadcast_cb.blockSignals(blocked)

    def _on_remove_clicked(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
        # Discard any pending ops silently; no auto-apply on removal.
        self._pending_ops = None
        self._apply_after_run = False
        self.host.unregister_block(self)
        self.removed.emit(self)
        self.deleteLater()

    # -- Apply flow ------------------------------------------------------

    def _set_apply_enabled(self, enabled: bool) -> None:
        if self._apply_btn is not None:
            self._apply_btn.setEnabled(bool(enabled))

    def _discard_pending_ops(self, footer: Optional[str] = None) -> None:
        """Clear pending ops and disable Apply.

        Called on param change, state-edit staleness, successful apply,
        validation failure, or block removal.

        For transform-only plugins, Apply is not gated on pending ops
        (it always runs-and-commits on click), so the Apply button
        stays enabled — we only reset the pending-ops cache.
        """
        self._pending_ops = None
        if not self._transform_only:
            self._set_apply_enabled(False)
        if footer is not None and self._supports_apply:
            self._footer.setText(footer)

    def _on_apply_clicked(self) -> None:
        if self._transform_only:
            # Single-click flow: run the plugin, auto-apply on finish.
            if self._runner is not None:
                return  # already in-flight
            self._apply_after_run = True
            self._run_now()
            return
        if not self._pending_ops:
            return
        ops = self._pending_ops
        app = getattr(self.host, "app", None)
        if app is None:
            self._set_status("error", "No app available to apply operations.")
            self._discard_pending_ops()
            return
        label = self.manifest.name or self.manifest.id
        try:
            apply_ops(ops, app, label=label)
        except OperationError as exc:
            self._discard_pending_ops()
            self._set_status("error", f"Apply failed: {exc.reason}")
            return
        except Exception as exc:  # noqa: BLE001 — last-resort log+display
            logger.exception("apply_ops raised unexpectedly: %s", exc)
            self._discard_pending_ops()
            self._set_status("error", f"{type(exc).__name__}: {exc}")
            return
        # Success. The ops are consumed; user must Run again for a new batch.
        n = len(ops)
        self._discard_pending_ops()
        self._set_status("ok",
                         f"Applied {n} operation{'s' if n != 1 else ''}.")

    def _mount_ops_preview(self, ops) -> None:
        """Mount the operations-preview widget in the output region."""
        if self._output_placeholder is not None:
            self._output_placeholder.setParent(None)
            self._output_placeholder = None
        if self._renderer is not None:
            self._renderer.setParent(None)
            self._renderer = None
        if self._ops_preview is None:
            self._ops_preview = OperationsPreview(self._output_holder)
            self._output_layout.addWidget(self._ops_preview)
        self._ops_preview.update_data(ops, None)

    # -- Running ---------------------------------------------------------

    def _schedule_live_rerun(self) -> None:
        self._live_debounce.start()  # restart on each call

    def _run_now(self) -> None:
        if self._runner is not None:
            return
        params = self._current_params()
        host = self.host

        def view_factory():
            return host.build_song_view()

        # Validate selection scope up front (so we can show a friendly
        # error without spawning a thread when it won't work).
        try:
            self._preflight_scope()
        except (SelectionEmpty, SelectionMismatch) as exc:
            self._set_status("error", f"Selection: {exc}")
            self._apply_after_run = False
            return

        self._set_status("running", "Running…")
        self._run_btn.setEnabled(False)
        if self._transform_only:
            # Disable Apply while a run is in flight.
            self._set_apply_enabled(False)

        runner = PluginRunner(self.plugin, view_factory, params)
        runner.finished.connect(self._on_runner_finished)
        runner.failed.connect(self._on_runner_failed)
        runner.progress.phase_changed.connect(self._on_phase)
        runner.progress.progress_changed.connect(self._on_progress)
        self._runner = runner
        runner.start()

    def _preflight_scope(self) -> None:
        # Plugins may choose 'whole' or 'selection' via a scope param —
        # only raise if the manifest *requires* a selection scope and
        # nothing is selected. The plugin itself is allowed to choose;
        # we only pre-flight when ``scopes`` is exactly ``("selection",)``.
        scopes = tuple(self.manifest.scopes)
        if scopes == ("selection",):
            sel = self.host.selection_snapshot()
            if not sel.notes and not sel.placements:
                raise SelectionEmpty("no selection")

    def _on_runner_finished(self, result: PluginResult) -> None:
        self._run_btn.setEnabled(True)
        if self._transform_only:
            self._set_apply_enabled(True)
        self._teardown_runner()
        auto_apply = self._apply_after_run
        self._apply_after_run = False
        if result is None:
            self._set_status("error", "Plugin returned no result.")
            return
        msg = result.message or ""
        ops = tuple(result.operations or ())

        # Transform-only single-click flow: commit ops immediately.
        if auto_apply and self._transform_only:
            self._auto_apply_ops(ops, msg)
            return

        if result.annotation is not None:
            ann = result.annotation
            ann.live = bool(self._live_cb and self._live_cb.isChecked())
            ann.stale = False
            self._last_annotation = ann
            self._mount_renderer(ann)
            # If we're currently broadcasting, push the fresh annotation
            # to the shared band.
            if (self._broadcast_cb is not None
                    and self._broadcast_cb.isChecked()
                    and hasattr(self.host, "broadcaster_annotation_updated")):
                try:
                    self.host.broadcaster_annotation_updated(self, ann)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "broadcaster_annotation_updated failed: %s", exc,
                    )
            footer = msg or self._default_footer(ann)
            status = ann.status if ann.status in ("ok", "error") else "ok"
            if self._supports_apply and ops:
                self._pending_ops = ops
                self._set_apply_enabled(True)
                extra = f"Ready to apply {len(ops)} operation{'s' if len(ops) != 1 else ''}. Click Apply."
                footer = f"{footer} \u00b7 {extra}" if footer else extra
            self._set_status(status, footer)
            return

        # No annotation. For transform/generate, show the ops preview.
        if self._supports_apply:
            self._pending_ops = ops if ops else None
            self._mount_ops_preview(ops)
            if ops:
                self._set_apply_enabled(True)
                footer = msg or f"Ready to apply {len(ops)} operation{'s' if len(ops) != 1 else ''}."
                self._set_status("ok", footer)
            else:
                self._set_apply_enabled(False)
                self._set_status("ok", msg or "No changes needed.")
            return

        self._set_status("ok", msg or "Done (no annotation).")

    def _on_runner_failed(self, exc: BaseException) -> None:
        self._run_btn.setEnabled(True)
        if self._transform_only:
            # Re-enable Apply so the user can try again after a failure.
            self._set_apply_enabled(True)
        self._teardown_runner()
        self._apply_after_run = False
        self._discard_pending_ops()
        self._set_status("error", f"{type(exc).__name__}: {exc}")

    def _auto_apply_ops(self, ops: tuple, msg: str) -> None:
        """Run-and-commit entry point for transform-only plugins."""
        if not ops:
            self._set_status("ok", msg or "No changes needed.")
            return
        app = getattr(self.host, "app", None)
        if app is None:
            self._set_status("error", "No app available to apply operations.")
            return
        label = self.manifest.name or self.manifest.id
        try:
            apply_ops(ops, app, label=label)
        except OperationError as exc:
            self._set_status("error", f"Apply failed: {exc.reason}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("apply_ops raised unexpectedly: %s", exc)
            self._set_status("error", f"{type(exc).__name__}: {exc}")
            return
        n = len(ops)
        self._set_status(
            "ok",
            f"Applied {n} operation{'s' if n != 1 else ''}.",
        )

    def _teardown_runner(self) -> None:
        if self._runner is not None:
            self._runner.shutdown()
            self._runner = None

    def _on_phase(self, name: str) -> None:
        self._footer.setText(f"Phase: {name}")

    def _on_progress(self, fraction: float, message) -> None:
        pct = int(max(0.0, min(1.0, fraction)) * 100)
        if message:
            self._footer.setText(f"{pct}%  {message}")
        else:
            self._footer.setText(f"{pct}%")

    def _default_footer(self, ann: Annotation) -> str:
        parts = []
        if ann.last_run_ms is not None:
            parts.append(f"{ann.last_run_ms} ms")
        if ann.schema:
            parts.append(ann.schema)
        return " · ".join(parts)

    # -- Renderer mount --------------------------------------------------

    def _mount_renderer(self, ann: Annotation) -> None:
        # Replace whatever is in the output region.
        if self._output_placeholder is not None:
            self._output_placeholder.setParent(None)
            self._output_placeholder = None
        if self._ops_preview is not None:
            self._ops_preview.setParent(None)
            self._ops_preview = None
        if (self._renderer is not None
                and getattr(self._renderer, "_schema_tag", None) == ann.schema):
            # Same schema — just update data.
            try:
                self._renderer.update_data(ann.data, ann.render_hint)
            except Exception as exc:  # noqa: BLE001
                logger.exception("renderer update_data failed: %s", exc)
            return
        # Build fresh renderer.
        if self._renderer is not None:
            self._renderer.setParent(None)
            self._renderer = None
        widget = make_renderer(ann.schema, self._output_holder)
        widget._schema_tag = ann.schema  # type: ignore[attr-defined]
        self._output_layout.addWidget(widget)
        self._renderer = widget
        try:
            widget.update_data(ann.data, ann.render_hint)
        except Exception as exc:  # noqa: BLE001
            logger.exception("renderer update_data failed: %s", exc)

    # -- Dep watcher hook ------------------------------------------------

    def handle_state_change(self, deps: frozenset) -> None:
        if not deps:
            return
        my_deps = set(self.manifest.deps or ())
        if not my_deps & deps:
            return
        # Mark stale. Discard pending ops: they were computed against a
        # now-superseded state snapshot.
        if self._last_annotation is not None:
            self._last_annotation.stale = True
        self._discard_pending_ops()
        # Don't override "running" status.
        if getattr(self, '_current_status', 'idle') != 'running':
            self._set_status("stale", "Input changed.")
        if self._live_cb is not None and self._live_cb.isChecked():
            self._schedule_live_rerun()

    # -- Qt lifecycle ----------------------------------------------------

    def closeEvent(self, event):
        if self._runner is not None:
            self._runner.cancel()
            self._runner.shutdown()
        self.host.unregister_block(self)
        super().closeEvent(event)
