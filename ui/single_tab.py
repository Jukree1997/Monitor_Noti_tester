"""Single-project tab — the original Noti UI as a self-contained QWidget.

Moved out of MainWindow so the shell can host both Single and Fleet tabs.
Status-bar messages bubble up via the status_text signal; the parent
MainWindow displays them on its global QStatusBar.
"""
from __future__ import annotations
import os
import time
import numpy as np
from PySide6.QtCore import Qt, Slot, QThread, QObject, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QComboBox,
    QFileDialog, QMessageBox, QApplication,
)
from collections import deque
from ui.video_widget import VideoWidget
from ui.sidebar import Sidebar
from core.detector import DetectionEngine
from core.video_source import VideoSource
from core.runner import PipelineRunner
from core.event_exporter import export_events
from core.paths import default_config_dir, default_videos_dir
from models.config_schema import (
    ProjectConfig, MonitorConfig,
    NotiSettings, LineAlertConfig, LineNotiRule,
    AreaOverstayConfig, ZoneAreaConfig, ZoneNotiRule,
)


# Hard cap for the in-memory event buffer. Each record is small (<1KB), so
# 50k entries ≈ 50MB. Old records are dropped FIFO when the cap is hit.
EVENT_BUFFER_CAP = 50_000


class _PreviewWorker(QObject):
    """Raw-feed preview when connected but not running detection."""

    frame_ready = Signal(np.ndarray)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self._running = False
        self._source: VideoSource | None = None

    def set_source(self, source: VideoSource):
        self._source = source

    @Slot()
    def run(self):
        self._running = True
        while self._running:
            if self._source is None:
                break
            ret, frame = self._source.read()
            if not ret or frame is None:
                if not self._source.is_live:
                    break
                continue
            if self._source.is_live:
                self._source.grab()
            self.frame_ready.emit(frame)
            QThread.msleep(30)
        self._running = False
        self.finished.emit()

    def stop(self):
        self._running = False


class SingleTab(QWidget):
    """The classic single-project view: video on the left, sidebar on the right.

    Public surface used by the MainWindow shell:
      - load_project_dialog(), save_project(force_dialog=False)
      - is_running(), is_connected()
      - stop_running(), shutdown()
      - signal status_text(str) — bubbles to the global status bar
    """

    status_text = Signal(str)

    def __init__(self, parent: QWidget | None = None,
                 *, license_mgr=None, can_start_camera=None):
        super().__init__(parent)

        # License integration. `can_start_camera` is a callable provided
        # by MainWindow — it returns True if the global cap allows one
        # more camera (and shows a uniform refusal dialog if not). We
        # keep license_mgr too for future direct queries.
        self._license_mgr = license_mgr
        self._can_start_camera = can_start_camera

        self._engine = DetectionEngine()
        self._source: VideoSource | None = None
        self._project: ProjectConfig | None = None
        self._project_path: str | None = None
        self._class_name_to_id: dict[str, int] = {}
        self._config = MonitorConfig()

        self._model_loaded = False
        self._connected = False
        self._running = False
        self._run_mode = ""
        self._show_live = True

        self._runner: PipelineRunner | None = None

        # Structured event buffer for the export feature. Bounded deque so a
        # long-running session can't grow without limit; oldest records drop
        # FIFO. Tracks runtime start so the metadata.txt reflects the buffer
        # window, not just the last session click.
        self._event_buffer: deque[dict] = deque(maxlen=EVENT_BUFFER_CAP)
        self._buffer_started_at: float = 0.0

        self._preview_worker: _PreviewWorker | None = None
        self._preview_thread: QThread | None = None

        self._build_ui()
        self._connect_signals()

    # ───────────────────── UI build ─────────────────────

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        # Video column: video on top, playback-speed control bar beneath.
        video_col = QVBoxLayout()
        video_col.setContentsMargins(0, 0, 0, 0)
        video_col.setSpacing(4)
        self._video = VideoWidget()
        video_col.addWidget(self._video, stretch=1)

        # Bottom bar: speed control aligned to the left, the rest empty.
        # Only affects file sources — RTSP/USB are camera-paced and ignore it.
        speed_bar = QHBoxLayout()
        speed_bar.setContentsMargins(2, 0, 2, 0)
        speed_bar.addWidget(QLabel("Playback:"))
        self._speed_combo = QComboBox()
        self._speed_combo.addItem("Normal", "normal")
        self._speed_combo.addItem("Process speed", "process")
        self._speed_combo.setCurrentIndex(0)
        self._speed_combo.setToolTip(
            "Normal: paced to source FPS (matches the original video tempo, "
            "best for demos).\nProcess speed: run as fast as inference allows."
            "\nIgnored for RTSP / USB sources.")
        speed_bar.addWidget(self._speed_combo)
        speed_bar.addStretch(1)
        video_col.addLayout(speed_bar)

        layout.addLayout(video_col, stretch=1)
        self._sidebar = Sidebar()
        layout.addWidget(self._sidebar)

    def _connect_signals(self):
        self._speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        sb = self._sidebar
        sb.load_project_requested.connect(self.load_project_dialog)
        sb.save_project_requested.connect(self.save_project)
        sb.btn_connect.clicked.connect(self._toggle_connect)
        sb.btn_reset_all.clicked.connect(self._reset_all)
        sb.btn_test.clicked.connect(self._on_test_clicked)
        sb.btn_start.clicked.connect(self._on_start_clicked)
        sb.show_live_changed.connect(self._on_show_live_changed)
        sb.show_detections_changed.connect(self._video.set_show_detections)
        sb.show_labels_changed.connect(self._video.set_show_labels)
        sb.draw_mode_changed.connect(self._video.set_draw_mode)
        sb.detect_classes_changed.connect(self._on_detect_classes_changed)
        sb.browse_video_requested.connect(self._on_browse_video_requested)
        sb.imgsz_changed.connect(self._on_imgsz_changed)
        sb.export_events_requested.connect(self._on_export_events_requested)

    # ───────────────────── public surface ─────────────────────

    def is_running(self) -> bool:
        return self._running

    def is_connected(self) -> bool:
        return self._connected

    def stop_running(self):
        if self._running:
            self._stop_running()

    def shutdown(self):
        """Full clean shutdown — called from MainWindow.closeEvent."""
        if self._running:
            self._stop_running()
        if self._connected:
            self._disconnect()
        if self._runner:
            self._runner.wait_for_pending_noti(5000)

    # ───────────────────── project I/O ─────────────────────

    @Slot()
    def load_project_dialog(self):
        # Default to <app>/config/ so project JSONs that ship with the
        # install are one click away. Same default the Project Editor
        # tab uses — keeps file-pickers consistent across tabs.
        start_dir = self._project_path or str(default_config_dir())
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Project Config", start_dir,
            "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        self._load_project_from_path(path)

    def _load_project_from_path(self, path: str):
        try:
            self._project = ProjectConfig.load(path)
        except Exception as e:
            QMessageBox.warning(self, "Load Error", str(e))
            return

        self._project_path = path
        self._config = self._project.monitor
        # Fresh project = fresh buffer. Otherwise events from the previous
        # project would bleed into this project's export.
        self._event_buffer.clear()
        self._buffer_started_at = time.time()

        device_name = "--"
        model_name = (os.path.basename(self._project.model_path)
                      if self._project.model_path else "--")
        if self._project.model_path and os.path.isfile(self._project.model_path):
            self._sidebar.lbl_model_info.setText(f"Model: {model_name} (loading...)")
            self._sidebar.lbl_device_info.setText("Device: loading...")
            QApplication.processEvents()
            try:
                device_name = self._engine.load_model(self._project.model_path)
                self._model_loaded = True
                self._sidebar.lbl_device_info.setText(f"Device: {device_name}")
                self._class_name_to_id = {
                    name: cid for cid, name in self._engine.model_names.items()}
                class_names = list(self._engine.model_names.values())
                self._sidebar.set_available_classes(class_names)
            except Exception as e:
                QMessageBox.warning(self, "Model Error", str(e))
                self._sidebar.lbl_device_info.setText(f"Device: Error - {e}")
        elif self._project.model_path:
            self._sidebar.lbl_model_info.setText(f"Model: {model_name} (file not found)")

        src = self._project.source
        self._sidebar.set_project_info(
            project_name=self._project.project_name or os.path.basename(path),
            model_name=model_name, device=device_name,
            source_type=src.type, source_value=src.value,
            zone_count=len(self._config.zones), line_count=len(self._config.lines))
        self._sidebar.populate_line_rules(self._config.lines)
        self._sidebar.populate_zones_area(self._config.zones)
        self._sidebar.set_inference_size(self._project.detection.imgsz)
        self._update_scale_info()
        ns = self._project.noti_settings
        self._sidebar.set_line_cooldown(ns.line_alert.cooldown_seconds)
        self._sidebar.apply_line_rules(ns.line_alert.rules)
        self._sidebar.set_area_config(ns.zone_area.area_overstay)
        self._sidebar.apply_zone_rules(ns.zone_area.zone_rules)
        self._sidebar.set_project_loaded(True)
        self._sidebar.set_source_browse_visible(src.type == "file")
        self._update_entrance_exit_summary()
        self.status_text.emit(f"Project loaded: {path}")
        self._sidebar.lbl_status.setText("Project loaded. Connect to input source.")

    @Slot()
    def save_project(self, force_dialog: bool = False):
        if self._project is None:
            QMessageBox.information(self, "No Project",
                                    "Load a project before saving.")
            return
        path = self._project_path if (self._project_path and not force_dialog) else None
        if path is None:
            start_dir = self._project_path or str(default_config_dir())
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Project Config",
                start_dir,
                "JSON Files (*.json);;All Files (*)")
            if not path:
                return
        line_rules_dicts = self._sidebar.get_line_rules()
        line_rules = [LineNotiRule(
            line_id=r["line_id"], enabled=r["enabled"],
            function=r["function"], notify_in=r["notify_in"],
            notify_out=r["notify_out"],
        ) for r in line_rules_dicts]
        line_alert = LineAlertConfig(
            cooldown_seconds=self._sidebar.get_line_cooldown(),
            rules=line_rules,
        )
        area_cfg = self._sidebar.get_area_config()
        area_overstay = AreaOverstayConfig(
            enabled=area_cfg["enabled"],
            threshold_seconds=area_cfg["threshold_seconds"],
            reminder_seconds=area_cfg["reminder_seconds"],
            target_classes=list(area_cfg.get("target_classes", []) or []),
        )
        zone_rules_dicts = self._sidebar.get_zone_rules()
        zone_rules = [ZoneNotiRule(
            zone_id=r["zone_id"], enabled=r["enabled"],
            notify_enter=r["notify_enter"], notify_exit=r["notify_exit"],
            notify_overstay=r["notify_overstay"],
            max_seconds=r["max_seconds"], enter_cooldown=r["enter_cooldown"],
            overstay_reminder=r["overstay_reminder"],
            target_classes=list(r.get("target_classes", []) or []),
        ) for r in zone_rules_dicts]
        self._project.noti_settings = NotiSettings(
            line_alert=line_alert,
            zone_area=ZoneAreaConfig(area_overstay=area_overstay,
                                     zone_rules=zone_rules),
        )
        try:
            self._project.save(path)
        except Exception as e:
            QMessageBox.warning(self, "Save Error", str(e))
            return
        self._project_path = path
        self.status_text.emit(f"Project saved: {path}")
        self._sidebar.lbl_status.setText("Project saved.")

    def _update_entrance_exit_summary(self):
        line_rules = self._sidebar.get_line_rules()
        entrance_names, exit_names = [], []
        for r in line_rules:
            if r["function"] in ("entrance", "bidirectional"):
                entrance_names.append(r["line_name"])
            if r["function"] in ("exit", "bidirectional"):
                exit_names.append(r["line_name"])
        zone_names = [z.name for z in self._config.zones]
        self._sidebar.update_entrance_exit_summary(entrance_names, exit_names, zone_names)

    # ───────────────────── connect / disconnect ─────────────────────

    def _toggle_connect(self):
        if self._connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        if not self._project:
            QMessageBox.warning(self, "No Project", "Please load a project config first.")
            return
        src = self._project.source
        if src.type == "rtsp":
            self._source = VideoSource(src.value)
        elif src.type == "camera":
            self._source = VideoSource(int(src.value))
        elif src.type == "file":
            if not os.path.isfile(src.value):
                QMessageBox.warning(self, "File Not Found",
                                    f"Video file not found:\n{src.value}")
                return
            self._source = VideoSource(src.value)
        else:
            return
        self._sidebar.lbl_status.setText("Connecting...")
        QApplication.processEvents()
        if not self._source.open():
            QMessageBox.warning(self, "Connection Failed",
                                f"Failed to open {src.type} source: {src.value}")
            self._source = None
            self._sidebar.lbl_status.setText("Connection failed")
            return
        w, h = self._source.resolution
        if w > 0 and h > 0:
            self._config.source_resolution = [w, h]
        self._update_scale_info()
        self._connected = True
        self._sidebar.set_connected(True)
        self._sidebar.lbl_source_info.setText(
            f"Source: {src.type} | {src.value} | {w}x{h} | {self._source.fps:.1f} FPS")
        self.status_text.emit(f"Connected: {src.type} | {w}x{h}")
        self._sidebar.lbl_status.setText("Connected. Press Test or Start.")
        self._start_preview()

    def _disconnect(self):
        if self._running:
            self._stop_running()
        self._stop_preview()
        if self._source:
            self._source.release()
            self._source = None
        self._connected = False
        self._sidebar.set_connected(False)
        self.status_text.emit("Disconnected")
        self._sidebar.lbl_status.setText("Disconnected")

    def _start_preview(self):
        self._stop_preview()
        self._preview_worker = _PreviewWorker()
        self._preview_worker.set_source(self._source)
        self._preview_thread = QThread()
        self._preview_worker.moveToThread(self._preview_thread)
        self._preview_thread.started.connect(self._preview_worker.run)
        self._preview_worker.frame_ready.connect(self._on_preview_frame)
        self._preview_worker.finished.connect(self._preview_thread.quit)
        self._preview_worker.finished.connect(self._on_preview_finished)
        self._preview_thread.start()

    def _stop_preview(self):
        if self._preview_worker:
            self._preview_worker.stop()
        if self._preview_thread and self._preview_thread.isRunning():
            self._preview_thread.quit()
            self._preview_thread.wait(3000)
        self._preview_worker = None
        self._preview_thread = None

    @Slot(np.ndarray)
    def _on_preview_frame(self, frame: np.ndarray):
        if not self._connected or self._running:
            return
        self._video.update_frame(frame, None, self._config)
        h, w = frame.shape[:2]
        imgsz = (self._project.detection.imgsz
                 if self._project is not None else 640)
        scale = imgsz / max(w, h) if max(w, h) > 0 else 1.0
        self.status_text.emit(
            f"Preview | Feed: {w}x{h} | Inference: {imgsz}px ({scale:.2f}×) "
            f"| Detection not running")

    def _on_preview_finished(self):
        if self._connected and not self._running:
            self.status_text.emit("Preview: video ended")

    # ───────────────────── test / start ─────────────────────

    def _on_test_clicked(self):
        if self._running and self._run_mode == "test":
            self._stop_running()
        else:
            self._start_running("test")

    def _on_start_clicked(self):
        if self._running and self._run_mode == "start":
            self._stop_running()
        else:
            self._start_running("start")

    def _start_running(self, mode: str):
        if not self._project:
            QMessageBox.warning(self, "No Project", "Please load a project config first.")
            return
        if not self._model_loaded:
            QMessageBox.warning(self, "No Model", "Model is not loaded.")
            return
        if not self._connected or self._source is None:
            QMessageBox.warning(self, "Not Connected", "Please connect first.")
            return
        # License-cap preflight. MainWindow's callable already includes
        # Fleet + Editor in the total, so this works across tabs.
        if self._can_start_camera is not None and not self._can_start_camera():
            return
        if mode == "start":
            noti = self._project.notification
            if not noti.channel_token:
                QMessageBox.warning(self, "Missing Token",
                    "LINE Channel Access Token is missing from project config.")
                return
            if not noti.target_id:
                QMessageBox.warning(self, "Missing Target",
                    "Target ID is missing from project config.")
                return

        self._stop_preview()

        self._runner = PipelineRunner(
            engine=self._engine, project=self._project, source=self._source,
            class_name_to_id=self._class_name_to_id,
        )
        self._runner.frame_ready.connect(self._on_runner_frame)
        self._runner.status_text.connect(self.status_text)
        self._runner.event_logged.connect(self._sidebar.add_event)
        self._runner.event_recorded.connect(self._on_event_recorded)
        self._runner.noti_result.connect(self._sidebar.add_noti_result)
        self._runner.stuck_changed.connect(self._sidebar.update_stuck_count)
        self._runner.error.connect(self._on_runner_error)
        self._runner.source_finished.connect(self._on_runner_finished)
        self._push_overrides_to_runner()

        self._run_mode = mode
        self._running = True
        self._sidebar.set_running(True, mode)
        label = ("Testing (no notifications)" if mode == "test"
                 else "Running (notifications active)")
        self._sidebar.lbl_status.setText(label)
        self.status_text.emit(label)
        self._update_entrance_exit_summary()
        self._runner.start(test_mode=(mode == "test"))

    def _stop_running(self):
        self._running = False
        if self._runner:
            self._runner.stop()
        self._sidebar.set_running(False)
        self._sidebar.lbl_status.setText("Stopped")
        self.status_text.emit("Stopped")
        if self._connected and self._source and self._source.is_opened:
            self._start_preview()

    def _push_overrides_to_runner(self):
        if not self._runner:
            return
        self._runner.set_runtime_overrides({
            "line_cooldown": self._sidebar.get_line_cooldown(),
            "line_rules": self._sidebar.get_line_rules(),
            "zone_rules": self._sidebar.get_zone_rules(),
            "area_config": self._sidebar.get_area_config(),
            "detect_class_ids": self._current_detect_class_ids(),
        })

    @Slot(np.ndarray, object, dict)
    def _on_runner_frame(self, frame: np.ndarray, result, render_data: dict):
        self._push_overrides_to_runner()
        if self._show_live:
            self._video.update_frame(
                frame, result, self._config,
                render_data["events"],
                render_data["box_colors"],
                render_data["det_labels"])

    @Slot(str)
    def _on_runner_error(self, msg: str):
        self.status_text.emit(f"Error: {msg}")

    @Slot()
    def _on_runner_finished(self):
        if self._running:
            self._running = False
            self._sidebar.set_running(False)
            self.status_text.emit("Source ended")
            self._sidebar.lbl_status.setText("Source ended")
            if self._connected and self._source and self._source.is_opened:
                self._start_preview()

    # ───────────────────── reset / misc ─────────────────────

    def _reset_all(self):
        if self._runner:
            self._runner.reset()
        else:
            self._sidebar.add_event(
                f"{time.strftime('%H:%M:%S')} | RESET | (idle - nothing to clear)",
                "#888888")

    def _on_show_live_changed(self, show: bool):
        self._show_live = show

    def _current_detect_class_ids(self) -> list[int] | None:
        names = self._sidebar.detect_class_combo.get_selected_classes()
        if not names or not self._class_name_to_id:
            return None
        ids = [self._class_name_to_id[n] for n in names if n in self._class_name_to_id]
        return ids or None

    def _on_detect_classes_changed(self, _names: list[str]):
        ids = self._current_detect_class_ids()
        self._engine.set_classes(ids)
        if self._runner:
            self._runner.set_runtime_overrides({"detect_class_ids": ids})

    @Slot(dict, str)
    def _on_event_recorded(self, ev_dict: dict, status: str):
        """Append a structured event to the export buffer. The runner emits
        this in lockstep with event_logged, so display and buffer can never
        drift. Status is folded into the same record for easier export."""
        record = dict(ev_dict)
        record["status"] = status
        self._event_buffer.append(record)

    @Slot()
    def _on_export_events_requested(self):
        if not self._event_buffer:
            QMessageBox.information(
                self, "No events",
                "No events have been recorded yet. Run Test or Start, "
                "let some events occur, then try again.")
            return
        target_dir = QFileDialog.getExistingDirectory(
            self, "Choose folder to export to",
            os.path.dirname(self._project_path or "") or os.path.expanduser("~"))
        if not target_dir:
            return

        # Snapshot semantics: copy the buffer + tracked-id set right now.
        # Events arriving after this point belong to the next export.
        snapshot = list(self._event_buffer)
        tracked_ids: set[int] = set()
        if self._runner is not None:
            tracked_ids = self._runner.get_currently_tracked_ids()

        src = self._project.source if self._project else None
        src_w, src_h = (0, 0)
        src_fps = 0.0
        if self._source is not None and self._source.is_opened:
            src_w, src_h = self._source.resolution
            src_fps = self._source.fps
        imgsz = self._project.detection.imgsz if self._project else 640
        scale_str = ""
        if src_w and src_h:
            scale = imgsz / max(src_w, src_h)
            inf_w = int(round(src_w * scale))
            inf_h = int(round(src_h * scale))
            scale_str = f"{scale:.2f}× (Feed {src_w}x{src_h} → {inf_w}x{inf_h})"

        project_name = (
            self._project.project_name if self._project
            and self._project.project_name
            else (os.path.splitext(os.path.basename(self._project_path or ""))[0]
                  or "project"))

        # Entrance/exit line IDs let the exporter compute area-dwell from
        # actual line crossings (entrance → exit) instead of recycling the
        # zone enter data, so "Area" stats are meaningfully different from
        # "Zone" stats. bidirectional_ids identifies lines where line_out
        # also acts as an area-exit (single-line boundary configs); for
        # strict entrance/exit lines, line_out is wrong-direction noise.
        entrance_ids: set[str] = set()
        exit_ids: set[str] = set()
        bidirectional_ids: set[str] = set()
        if self._project:
            for r in self._project.noti_settings.line_alert.rules:
                if r.function in ("entrance", "bidirectional"):
                    entrance_ids.add(r.line_id)
                if r.function in ("exit", "bidirectional"):
                    exit_ids.add(r.line_id)
                if r.function == "bidirectional":
                    bidirectional_ids.add(r.line_id)

        # export_time anchors "still_inside" dwell math in event_exporter:
        # it must live in the same domain as event.timestamp. The runner
        # emits event timestamps in content-time (for files) or wall-clock
        # (for live) via last_frame_time, so use that when a frame has been
        # processed; fall back to wall-clock before the first frame.
        export_now = (self._runner.last_frame_time
                      if self._runner and self._runner.last_frame_time
                      else time.time())
        metadata = {
            "export_time": export_now,
            "project_name": project_name,
            "project_path": self._project_path or "",
            "model_name": (os.path.basename(self._project.model_path)
                           if self._project and self._project.model_path else ""),
            "source_type": src.type if src else "",
            "source_value": src.value if src else "",
            "source_resolution": (f"{src_w}x{src_h}" if src_w and src_h else ""),
            "source_fps": src_fps,
            "imgsz": imgsz,
            "scale_str": scale_str,
            "buffer_started_at": self._buffer_started_at,
            "still_running": self._running,
            "entrance_line_ids": entrance_ids,
            "exit_line_ids": exit_ids,
            "bidirectional_line_ids": bidirectional_ids,
        }

        try:
            folder, count = export_events(
                output_root=target_dir,
                project_name=project_name,
                buffer=snapshot,
                currently_tracked_ids=tracked_ids,
                metadata=metadata)
        except Exception as e:
            QMessageBox.warning(self, "Export failed",
                                f"Could not write export:\n{e}")
            return

        msg = f"Exported {count} events to {folder}"
        self.status_text.emit(msg)
        self._sidebar.lbl_status.setText(msg)
        QMessageBox.information(
            self, "Export complete",
            f"Wrote {count} events to:\n{folder}")

    def _on_imgsz_changed(self, value: int):
        value = int(value)
        if self._project is not None:
            self._project.detection.imgsz = value
        # Push live to the engine so a running detector picks it up next loop.
        self._engine.update_params(imgsz=value)
        self._update_scale_info()

    @Slot(int)
    def _on_speed_changed(self, _index: int):
        mode = self._speed_combo.currentData()
        if mode:
            self._engine.set_playback_mode(mode)

    def _update_scale_info(self):
        imgsz = (self._project.detection.imgsz
                 if self._project is not None else 640)
        if self._source is not None and self._source.is_opened:
            w, h = self._source.resolution
        else:
            sr = getattr(self._config, "source_resolution", None) or [0, 0]
            w, h = (int(sr[0]), int(sr[1])) if len(sr) >= 2 else (0, 0)
        self._sidebar.update_scale_info(w, h, imgsz)

    def _on_browse_video_requested(self):
        if not self._project or self._project.source.type != "file":
            return
        # Prefer the project's current file location; else fall back to
        # <app>/videos/ if it exists; else home dir.
        videos = default_videos_dir()
        start_dir = (os.path.dirname(self._project.source.value)
                     if self._project.source.value
                     else (str(videos) if videos.is_dir() else ""))
        new_path, _ = QFileDialog.getOpenFileName(
            self, "Select video file", start_dir,
            "Videos (*.mp4 *.avi *.mov *.mkv *.webm);;All Files (*)")
        if not new_path:
            return

        if self._running:
            self._stop_running()
        if self._connected:
            self._disconnect()

        self._project.source.value = new_path
        self._sidebar.lbl_source_info.setText(f"Source: file | {new_path}")

        if self._project_path:
            answer = QMessageBox.question(
                self, "Save source?",
                "Save this as the new default source in the project file?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                try:
                    self._project.save(self._project_path)
                    self.status_text.emit(
                        f"Saved new source to {self._project_path}")
                except Exception as e:
                    QMessageBox.warning(self, "Save Error",
                                         f"Could not save project file:\n{e}")

        self._connect()
