"""Project Editor tab — draw zones/lines on a video preview and save to
the project JSON, all without leaving Monitor_Noti_tester.

This is a lift of ``Monitoring_Config_Tester``'s MainWindow stripped of
the detection live-preview (no detector, no tracker, no zone-manager
live evaluation — just raw frames + a click-to-draw overlay) and
re-shaped as a ``QWidget`` so it can sit beside ``SingleTab`` and
``FleetTab`` in the main tab bar.

The detection-quality controls in the sidebar (model path, conf/iou/
imgsz) stay visible because they're saved into the project JSON for
the runtime tabs to consume — they just don't drive any in-tab
behavior here.
"""
# ======================================
# -------- 0. IMPORTS --------
# ======================================
from __future__ import annotations
import os
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QFileDialog, QMessageBox,
)

from ui.editor_video_widget import EditorVideoWidget
from ui.editor_sidebar import EditorSidebar
from ui.zone_editor import ZoneLineEditor
from core.detector import DetectionEngine
from core.paths import default_config_dir, default_models_dir, default_videos_dir
from core.video_source import VideoSource
from models.config_schema import (
    MonitorConfig, ProjectConfig, SourceConfig, DetectionConfig,
    NotificationConfig, NotiSettings, Zone, Line,
)


# ======================================
# -------- 1. PREVIEW WORKER --------
# ======================================

class _PreviewWorker(QObject):
    """Read frames from a VideoSource on a background thread; emit each
    one for the EditorVideoWidget to paint. No detection — this is what
    keeps the editor tab cheap when SingleTab is the heavyweight runtime."""

    frame_ready = Signal(np.ndarray)
    error = Signal(str)
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._source: VideoSource | None = None

    def set_source(self, source: VideoSource) -> None:
        self._source = source

    @Slot()
    def run(self) -> None:
        self._running = True
        while self._running:
            if self._source is None:
                break
            ret, frame = self._source.read()
            if not ret or frame is None:
                if not self._source.is_live:
                    break  # file ended
                continue
            if self._source.is_live:
                self._source.grab()
            self.frame_ready.emit(frame)
            # Cap preview ~30 fps so the editor doesn't burn CPU.
            QThread.msleep(30)
        self._running = False
        self.finished.emit()

    def stop(self) -> None:
        self._running = False


# ======================================
# -------- 2. PROJECT EDITOR TAB --------
# ======================================

class ProjectEditorTab(QWidget):
    """The whole MCT editor experience as a single tab in MNT.

    Public API for MainWindow (mirrors the SingleTab surface):
      - ``status_text(str)`` signal → forwarded to the app status bar
      - ``load_project_dialog()`` / ``save_project(force_as: bool)`` →
        wired to the File menu so Ctrl+O / Ctrl+S / Ctrl+Shift+S work
      - ``is_running() -> bool`` → True while the preview thread is live;
        MainWindow uses this in its tab-switch confirm flow
      - ``shutdown()`` → called from closeEvent to release the video
        source + join the preview thread
    """

    status_text = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # State.
        self._source: VideoSource | None = None
        self._config = MonitorConfig()
        self._project_path: str = ""
        # Preserved across a load→save round-trip so the editor doesn't
        # silently drop the runtime tab's line/zone notification rules.
        # The editor doesn't have UI for editing these (yet), so we just
        # round-trip whatever was loaded.
        self._loaded_noti_settings: NotiSettings = NotiSettings()
        self._model_path: str = ""
        self._conf = 0.40
        self._iou = 0.45
        self._imgsz = 640

        # Live-detection engine. Owns its own DetectionEngine (NOT shared
        # with Single tab) so the user can A/B detection vs zone geometry
        # without disturbing a running pipeline. GPU contention is
        # avoided via the ``can_start_detection_cb`` set by MainWindow.
        self._engine = DetectionEngine()
        self._model_loaded = False
        self._detecting = False
        self._class_name_to_id: dict[str, int] = {}
        self.can_start_detection_cb: callable | None = None

        # Preview thread.
        self._preview_worker: _PreviewWorker | None = None
        self._preview_thread: QThread | None = None

        self._build_ui()
        self._wire_signals()

    # ======================================
    # -------- 3. UI BUILD --------
    # ======================================

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        self._video = EditorVideoWidget()
        layout.addWidget(self._video, stretch=1)

        self._sidebar = EditorSidebar()
        layout.addWidget(self._sidebar)

        # All MCT sidebar widgets visible — including the Start Detection
        # button + Show Detections checkbox + Box/Dot draw-mode combo.
        # The editor's live preview is opt-in via Start Detection so the
        # user can verify zone/line geometry against real moving objects.

        # The interactive zone/line edit overlay is a QObject that hooks
        # into the video widget's mouse signals — not a widget added
        # to a layout.
        self._editor = ZoneLineEditor(self._video)

    def _wire_signals(self) -> None:
        sb = self._sidebar
        sb.browse_model.connect(self._on_browse_model)
        sb.btn_refresh_cameras.clicked.connect(self._on_scan_cameras)
        sb.btn_browse_file.clicked.connect(self._on_browse_video_file)
        sb.btn_connect.clicked.connect(self._on_toggle_connect)
        sb.btn_start_stop.clicked.connect(self._on_toggle_detection)

        sb.conf_changed.connect(lambda v: self._update_param("conf", v))
        sb.iou_changed.connect(lambda v: self._update_param("iou", v))
        sb.imgsz_changed.connect(lambda v: self._update_param("imgsz", v))

        # Detect-classes filter — set on the engine so YOLO only emits
        # the user-selected classes (per-frame; takes effect on the next
        # detection run).
        sb.detect_classes_changed.connect(self._on_detect_classes_changed)

        # View toggles wired straight to the video widget.
        sb.show_detections_changed.connect(self._video.set_show_detections)
        sb.draw_mode_changed.connect(self._video.set_draw_mode)

        sb.add_zone_requested.connect(self._on_start_draw_zone)
        sb.add_line_requested.connect(self._on_start_draw_line)
        sb.delete_region.connect(self._on_delete_region)
        sb.rename_region.connect(self._on_rename_region)
        sb.line_flip.connect(self._on_flip_line)
        sb.edit_mode_requested.connect(self._on_enter_edit_mode)

        sb.load_project_requested.connect(self.load_project_dialog)
        sb.save_project_requested.connect(lambda: self.save_project(False))

        self._editor.zone_created.connect(self._on_zone_created)
        self._editor.line_created.connect(self._on_line_created)
        self._editor.config_modified.connect(self._refresh_region_list)
        self._editor.status_message.connect(self._sidebar.lbl_status.setText)

    # ======================================
    # -------- 4. SOURCE / CONNECT --------
    # ======================================

    @Slot()
    def _on_browse_model(self) -> None:
        # Default to <app>/models/ so models shipped with the install
        # show up without the user navigating to home dir first.
        start_dir = str(default_models_dir())
        if self._model_path and os.path.isfile(self._model_path):
            start_dir = self._model_path  # remember last pick
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Model", start_dir,
            "Model Files (*.pt *.onnx);;All Files (*)")
        if not path:
            return
        self._load_model_into_engine(path)

    def _load_model_into_engine(self, path: str) -> None:
        """Load ``path`` into the editor's DetectionEngine and populate
        the class-filter combo. Used both by the Browse Model button and
        by Load Project (when the saved project has a valid model_path)."""
        self._model_path = path
        self._sidebar.model_entry.setText(os.path.basename(path))
        self._sidebar.lbl_model_status.setText("Loading model...")
        try:
            device_name = self._engine.load_model(path)
            self._model_loaded = True
            self._sidebar.lbl_model_status.setText(f"Loaded on {device_name}")
            self._sidebar.lbl_device.setText(f"Device: {device_name}")
            class_names = list(self._engine.model_names.values())
            self._class_name_to_id = {
                name: cid for cid, name in self._engine.model_names.items()}
            self._sidebar.set_available_classes(class_names)
            self.status_text.emit(f"Model loaded on {device_name}")
        except Exception as e:
            self._model_loaded = False
            self._sidebar.lbl_model_status.setText(f"Error: {e}")
            QMessageBox.warning(self, "Model Error", str(e))

    @Slot()
    def _on_scan_cameras(self) -> None:
        cams = VideoSource.detect_usb_cameras()
        self._sidebar.set_available_cameras(cams)

    @Slot()
    def _on_browse_video_file(self) -> None:
        # Default to <app>/videos/ if it exists, else user's home dir.
        # We don't auto-create the dir — users usually have their own
        # videos folder elsewhere.
        videos = default_videos_dir()
        start_dir = str(videos) if videos.is_dir() else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Video File", start_dir,
            "Video Files (*.mp4 *.avi *.mov *.mkv);;All Files (*)")
        if path:
            self._sidebar.file_entry.setText(path)

    @Slot()
    def _on_toggle_connect(self) -> None:
        if self._source is None or not self._source.is_opened:
            self._connect()
        else:
            self._disconnect()

    def _connect(self) -> None:
        source_type, source_value = self._sidebar.get_source()
        if source_value is None or source_value == "":
            QMessageBox.warning(self, "No source", "Pick a camera, RTSP URL, or video file first.")
            return
        try:
            src = VideoSource(int(source_value) if source_type == "camera" else source_value)
            if not src.open():
                raise RuntimeError("VideoSource.open() returned False")
        except Exception as e:
            QMessageBox.warning(self, "Connection Error", str(e))
            return

        self._source = src
        w, h = src.resolution
        self._sidebar.set_connected(True, f"{w}x{h}")
        self._update_scale_info()
        self._start_preview()

    def _disconnect(self) -> None:
        self._stop_preview()
        if self._source is not None and self._source.is_opened:
            self._source.release()
        self._source = None
        self._sidebar.set_connected(False)

    # ======================================
    # -------- 5a. DETECTION LIFECYCLE --------
    # ======================================

    @Slot()
    def _on_toggle_detection(self) -> None:
        if self._detecting:
            self._stop_detection()
        else:
            self._start_detection()

    def _start_detection(self) -> None:
        # Guard rails: model loaded, source connected.
        if not self._model_loaded:
            QMessageBox.warning(self, "No model", "Load a model first.")
            return
        if self._source is None or not self._source.is_opened:
            QMessageBox.warning(self, "No source",
                                "Connect to a video source first.")
            return
        # GPU mutual-exclusion with Single/Fleet tabs (set by MainWindow).
        if self.can_start_detection_cb is not None and not self.can_start_detection_cb():
            QMessageBox.warning(
                self, "Pipeline already running",
                "Another tab is running a pipeline. Stop it before starting "
                "detection here — only one CUDA session per model at a time.")
            return
        # License-cap gate (set by MainWindow). Pops its own dialog
        # on failure with the uniform "license cap reached" wording.
        if getattr(self, "cap_check_cb", None) is not None and not self.cap_check_cb():
            return

        # Stop the no-detect preview so the DetectionEngine can own the
        # video source exclusively.
        self._stop_preview()

        ids = self._current_detect_class_ids()
        self._engine.set_classes(ids)
        self._engine.start(
            source=self._source,
            conf=self._conf, iou=self._iou, imgsz=self._imgsz,
            on_frame=self._on_detection_frame,
            on_error=self._on_detection_error,
            on_finished=self._on_detection_finished,
            classes=ids,
        )
        self._detecting = True
        self._sidebar.btn_start_stop.setText("■  Stop Detection")
        self.status_text.emit("Detection started")

    def _stop_detection(self) -> None:
        if not self._detecting:
            return
        self._engine.stop()
        self._detecting = False
        self._sidebar.btn_start_stop.setText("▶  Start Detection")
        # Resume the no-detect preview so the user keeps seeing frames.
        if self._source is not None and self._source.is_opened:
            self._start_preview()
        self.status_text.emit("Detection stopped")

    def _current_detect_class_ids(self) -> list[int] | None:
        names = self._sidebar.detect_class_combo.get_selected_classes()
        if not names or not self._class_name_to_id:
            return None
        ids = [self._class_name_to_id[n] for n in names if n in self._class_name_to_id]
        return ids or None

    @Slot(list)
    def _on_detect_classes_changed(self, _names: list[str]) -> None:
        ids = self._current_detect_class_ids()
        self._engine.set_classes(ids)

    @Slot(np.ndarray, object, float, float)
    def _on_detection_frame(self, frame: np.ndarray, result,
                            inference_ms: float, _frame_time: float) -> None:
        # No tracker/zone event evaluation here — the user just wants to
        # see boxes overlaid on their zone/line geometry. For real event
        # firing they switch to Single tab.
        self._video.update_frame(frame, result, self._config)

    @Slot(str)
    def _on_detection_error(self, msg: str) -> None:
        self.status_text.emit(f"Detection error: {msg}")

    @Slot()
    def _on_detection_finished(self) -> None:
        # Engine finished naturally (e.g. file source ended). Reset UI.
        self._detecting = False
        self._sidebar.btn_start_stop.setText("▶  Start Detection")

    # ======================================
    # -------- 5. PREVIEW LOOP --------
    # ======================================

    def _start_preview(self) -> None:
        if self._source is None:
            return
        self._preview_worker = _PreviewWorker()
        self._preview_worker.set_source(self._source)
        self._preview_thread = QThread()
        self._preview_worker.moveToThread(self._preview_thread)
        self._preview_thread.started.connect(self._preview_worker.run)
        self._preview_worker.frame_ready.connect(self._on_preview_frame)
        self._preview_worker.error.connect(self._on_preview_error)
        self._preview_worker.finished.connect(self._on_preview_finished)
        self._preview_thread.start()

    def _stop_preview(self) -> None:
        if self._preview_worker is not None:
            self._preview_worker.stop()
        if self._preview_thread is not None and self._preview_thread.isRunning():
            self._preview_thread.quit()
            self._preview_thread.wait(3000)
        self._preview_worker = None
        self._preview_thread = None

    @Slot(np.ndarray)
    def _on_preview_frame(self, frame: np.ndarray) -> None:
        self._video.update_frame(frame)

    @Slot(str)
    def _on_preview_error(self, msg: str) -> None:
        self.status_text.emit(f"Preview error: {msg}")

    @Slot()
    def _on_preview_finished(self) -> None:
        # If we stopped the preview to hand off to live detection, the
        # source is still open and owned by the engine — leave the
        # Connect button alone. (Without this guard, the file-source
        # branch below would mark the sidebar disconnected the moment
        # Start Detection ran, even though the engine was about to use
        # the same source.)
        if self._detecting:
            return
        # File source ended naturally — keep video widget on last frame,
        # reflect disconnected state.
        if self._source is not None and not self._source.is_live:
            self._sidebar.set_connected(False)

    # ======================================
    # -------- 6. PARAMS / SCALE --------
    # ======================================

    def _update_param(self, name: str, value) -> None:
        if name == "conf":
            self._conf = float(value)
        elif name == "iou":
            self._iou = float(value)
        elif name == "imgsz":
            self._imgsz = int(value)
            self._update_scale_info()

    def _update_scale_info(self) -> None:
        if self._source is None or not self._source.is_opened:
            return
        w, h = self._source.resolution
        self._sidebar.update_scale_info(w, h, self._imgsz)

    # ======================================
    # -------- 7. ZONE / LINE EDIT --------
    # ======================================

    @Slot(str)
    def _on_start_draw_zone(self, name: str) -> None:
        self._editor.set_config(self._config)
        self._editor.start_zone(name)

    @Slot(str)
    def _on_start_draw_line(self, name: str) -> None:
        self._editor.set_config(self._config)
        self._editor.start_line(name)

    @Slot()
    def _on_enter_edit_mode(self) -> None:
        self._editor.set_config(self._config)
        self._editor.start_edit()

    @Slot(Zone)
    def _on_zone_created(self, zone: Zone) -> None:
        zone.name = self._deduplicate_name(zone.name)
        self._config.zones.append(zone)
        self._sync_video_overlay()
        self._refresh_region_list()

    @Slot(Line)
    def _on_line_created(self, line: Line) -> None:
        line.name = self._deduplicate_name(line.name)
        self._config.lines.append(line)
        self._sync_video_overlay()
        self._refresh_region_list()

    @Slot(str, str)
    def _on_delete_region(self, region_type: str, region_id: str) -> None:
        if region_type == "zone":
            self._config.zones = [z for z in self._config.zones if z.id != region_id]
        elif region_type == "line":
            self._config.lines = [l for l in self._config.lines if l.id != region_id]
        self._sync_video_overlay()
        self._refresh_region_list()

    @Slot(str, str, str)
    def _on_rename_region(self, region_type: str, region_id: str, new_name: str) -> None:
        items = self._config.zones if region_type == "zone" else self._config.lines
        for it in items:
            if it.id == region_id:
                it.name = new_name
                break
        self._refresh_region_list()

    @Slot(str)
    def _on_flip_line(self, line_id: str) -> None:
        for ln in self._config.lines:
            if ln.id == line_id:
                ln.start, ln.end = list(ln.end), list(ln.start)
                ln.invert = not ln.invert
                break
        self._sync_video_overlay()
        self._refresh_region_list()

    def _refresh_region_list(self) -> None:
        self._sidebar.update_region_list(self._config.zones, self._config.lines)
        self._sync_video_overlay()

    def _sync_video_overlay(self) -> None:
        self._video.set_config(self._config)

    def _deduplicate_name(self, name: str) -> str:
        """Lifted from MCT's MainWindow: if ``name`` already exists across
        zones+lines, auto-renumber both the old and new ones so the user
        never gets two regions with the same name. Falls back to
        ``name_<n+1>`` when only numbered variants exist."""
        all_names = ([z.name for z in self._config.zones]
                     + [ln.name for ln in self._config.lines])

        exact_count = all_names.count(name)
        if exact_count == 0:
            numbered = [n for n in all_names
                        if n.startswith(name + "_")
                        and n[len(name) + 1:].isdigit()]
            if not numbered:
                return name
            max_num = max(int(n[len(name) + 1:]) for n in numbered)
            return f"{name}_{max_num + 1}"

        if exact_count == 1:
            for z in self._config.zones:
                if z.name == name:
                    z.name = f"{name}_1"
                    break
            else:
                for ln in self._config.lines:
                    if ln.name == name:
                        ln.name = f"{name}_1"
                        break
            return f"{name}_2"

        numbered = [n for n in all_names
                    if n == name or (n.startswith(name + "_")
                                     and n[len(name) + 1:].isdigit())]
        max_num = 0
        for n in numbered:
            if n == name:
                continue
            num = int(n[len(name) + 1:])
            max_num = max(max_num, num)
        return f"{name}_{max_num + 1}"

    # ======================================
    # -------- 8. PROJECT JSON --------
    # ======================================

    def load_project_dialog(self) -> None:
        """Called from MainWindow's File → Load Project menu."""
        # Default to <app>/config/ so project JSONs that ship with the
        # install (or that the user saved earlier) are one click away.
        start_dir = self._project_path or str(default_config_dir())
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Project Config", start_dir,
            "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        self._load_project_path(path)

    def _load_project_path(self, path: str) -> None:
        try:
            project = ProjectConfig.load(path)
        except Exception as e:
            QMessageBox.warning(self, "Load Error", str(e))
            return

        self._project_path = path
        if project.model_path and os.path.isfile(project.model_path):
            # Auto-load the model so the user can hit Start Detection
            # immediately after Load Project.
            self._load_model_into_engine(project.model_path)
        elif project.model_path:
            self._model_path = project.model_path
            self._sidebar.model_entry.setText(os.path.basename(project.model_path))
            self._sidebar.lbl_model_status.setText("Model file not found at saved path")

        src = project.source
        if src.type == "rtsp":
            self._sidebar.radio_rtsp.setChecked(True)
            self._sidebar.rtsp_entry.setText(src.value)
        elif src.type == "camera":
            self._sidebar.radio_camera.setChecked(True)
        elif src.type == "file":
            self._sidebar.radio_file.setChecked(True)
            self._sidebar.file_entry.setText(src.value)

        det = project.detection
        self._conf, self._iou, self._imgsz = det.conf, det.iou, det.imgsz
        self._sidebar.slider_conf.setValue(int(det.conf * 100))
        self._sidebar.slider_iou.setValue(int(det.iou * 100))
        self._sidebar.combo_imgsz.setCurrentText(str(det.imgsz))

        self._sidebar.set_notification_settings(asdict(project.notification))

        self._config = project.monitor
        self._sync_video_overlay()
        self._refresh_region_list()

        # Preserve noti_settings (line/zone notification rules) so we
        # don't drop them on the next Save Project.
        self._loaded_noti_settings = project.noti_settings

        self.status_text.emit(f"Project loaded: {path}")

    def save_project(self, force_as: bool = False) -> None:
        """Save the current project. ``force_as=True`` for Save-As; otherwise
        re-uses the last loaded/saved path (if any) for a plain Save."""
        if not force_as and self._project_path:
            self._save_project_path(self._project_path)
            return

        start_dir = self._project_path or str(default_config_dir())
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "Save Project Config",
            start_dir,
            "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        if "*.json" in selected_filter and not path.lower().endswith(".json"):
            path += ".json"
        self._save_project_path(path)

    def _save_project_path(self, path: str) -> None:
        source_type, source_value = self._sidebar.get_source()
        source = SourceConfig(type=source_type, value=str(source_value))
        detection = DetectionConfig(conf=self._conf, iou=self._iou, imgsz=self._imgsz)
        notif_settings = self._sidebar.get_notification_settings()
        notification = NotificationConfig(**notif_settings)

        project = ProjectConfig(
            project_name=os.path.splitext(os.path.basename(path))[0],
            model_path=self._model_path,
            source=source,
            detection=detection,
            notification=notification,
            monitor=self._config,
            noti_settings=self._loaded_noti_settings,
        )

        try:
            project.save(path)
            self._project_path = path
            self.status_text.emit(f"Project saved: {path}")
        except Exception as e:
            QMessageBox.warning(self, "Save Error", str(e))

    # ======================================
    # -------- 10. PUBLIC LIFECYCLE --------
    # ======================================

    def is_running(self) -> bool:
        """True while live detection OR the no-detect preview thread is
        alive. main_window's tab-switch confirm uses this — leaving the
        tab with either running prompts the user."""
        if self._detecting:
            return True
        return (self._preview_thread is not None
                and self._preview_thread.isRunning())

    def stop_running(self) -> None:
        """Stop detection (if running), stop preview, release the source.
        Called by main_window when the user confirms a tab switch."""
        if self._detecting:
            self._stop_detection()
        self._stop_preview()
        if self._source is not None and self._source.is_opened:
            self._source.release()
        self._source = None
        self._sidebar.set_connected(False)

    def shutdown(self) -> None:
        """Window close — release everything and join the preview thread."""
        self.stop_running()
