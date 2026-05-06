"""Fleet-mode tab — multi-camera operations.

Owns a ``FleetWorkerManager`` (which spawns one subprocess per camera) and a
grid of ``CameraTile`` widgets. Routes manager signals to per-tile updates and
the combined event log on the right-hand sidebar.

Phase 2c scope: add/remove cameras, start/test/stop per tile and "all",
combined event log with Clear, hardware-based worker cap. Thumbnails,
full-screen mode, save/load fleet, display options, and log filters land in
Phase 2d.
"""
from __future__ import annotations
import json
import os
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QFileDialog, QMessageBox,
    QScrollArea, QGridLayout, QStackedWidget,
)
from core.fleet_manager import (
    FleetWorkerManager,
    S_RUNNING, S_SPAWNING, S_STOPPING, S_STOPPED, S_ERROR,
)
from core.hardware import get_hardware_info
from models.config_schema import ProjectConfig
from ui.camera_tile import CameraTile
from ui.camera_fullscreen import CameraFullScreenView
from ui.fleet_sidebar import FleetSidebar
from ui.noti_setup_dialog import NotiSetupDialog


_TILES_PER_ROW = 3


class FleetTab(QWidget):
    status_text = Signal(str)

    def __init__(self, parent: "QWidget | None" = None):
        super().__init__(parent)
        self._hardware = get_hardware_info()
        self._max_workers = self._hardware["max_workers"]
        self._manager = FleetWorkerManager(self)
        self._tiles: dict[str, CameraTile] = {}
        self._names: dict[str, str] = {}            # cam_id -> project_name (for tooltips, dialog)
        self._worker_index: dict[str, int] = {}     # cam_id -> 1, 2, 3 ...
        self._next_worker_index: int = 1            # monotonic; never reused
        self._mark_for_removal: set[str] = set()

        self._build_ui()
        self._wire_manager()

    # ───────── public surface (matches SingleTab shape) ─────────

    def is_any_running(self) -> bool:
        return self._manager.running_count() > 0

    def is_full_screen(self) -> bool:
        return self._full_view.is_attached()

    def enter_full_screen(self, cam_id: str):
        if not self._tiles.get(cam_id):
            return
        # Asking the worker to stream live frames is what makes the view useful;
        # other cameras keep doing their normal thumbnail-only behavior.
        self._manager.set_streaming(cam_id, True)
        worker = self._worker_name(cam_id)
        project = self._names.get(cam_id, "")
        title = f"{worker} — {project}" if project else worker
        self._full_view.attach(cam_id, title)
        self._stack.setCurrentWidget(self._full_view)

    def exit_full_screen(self):
        cam_id = self._full_view.attached_camera_id()
        if cam_id is not None:
            self._manager.set_streaming(cam_id, False)
        self._full_view.detach()
        self._stack.setCurrentWidget(self._grid_page)

    def stop_all(self):
        self._on_stop_all()

    def shutdown(self):
        # Make sure no worker is told to keep streaming after shutdown.
        if self._full_view.is_attached():
            self.exit_full_screen()
        self._manager.shutdown(timeout_s=5.0)

    # ───────── build ─────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._stack = QStackedWidget()
        outer.addWidget(self._stack)

        # ── Page 0: grid + sidebar ──
        self._grid_page = QWidget()
        page_lay = QHBoxLayout(self._grid_page)
        page_lay.setContentsMargins(5, 5, 5, 5)
        page_lay.setSpacing(5)

        self._grid_scroll = QScrollArea()
        self._grid_scroll.setWidgetResizable(True)
        self._grid_scroll.setStyleSheet("QScrollArea { border: none; }")
        self._grid_container = QWidget()
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(8)
        self._grid_layout.setContentsMargins(8, 8, 8, 8)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop |
                                        Qt.AlignmentFlag.AlignLeft)
        self._grid_scroll.setWidget(self._grid_container)
        page_lay.addWidget(self._grid_scroll, 1)

        self._sidebar = FleetSidebar(self._hardware)
        page_lay.addWidget(self._sidebar)

        self._sidebar.add_project_requested.connect(self._on_add_project)
        self._sidebar.start_all_requested.connect(self._on_start_all)
        self._sidebar.test_all_requested.connect(self._on_test_all)
        self._sidebar.stop_all_requested.connect(self._on_stop_all)
        self._sidebar.save_fleet_requested.connect(self._on_save_fleet)
        self._sidebar.load_fleet_requested.connect(self._on_load_fleet)
        self._sidebar.display_options_changed.connect(self._on_display_options_changed)
        # Seed our cached copy from whatever the sidebar starts with.
        self._display_opts = self._sidebar.current_display_options()

        self._stack.addWidget(self._grid_page)

        # ── Page 1: full-screen single-camera view ──
        self._full_view = CameraFullScreenView()
        self._full_view.back_clicked.connect(self.exit_full_screen)
        self._stack.addWidget(self._full_view)

        self._stack.setCurrentWidget(self._grid_page)

        self._reflow_grid()
        self._refresh_counts()

    def _wire_manager(self):
        m = self._manager
        m.camera_started.connect(self._on_started)
        m.camera_event.connect(self._on_event)
        m.camera_noti_result.connect(self._on_noti_result)
        m.camera_status.connect(self._on_status)
        m.camera_thumbnail.connect(self._on_thumbnail)
        m.camera_live_frame.connect(self._on_live_frame)
        m.camera_error.connect(self._on_error)
        m.camera_finished.connect(self._on_finished)

    # ───────── grid layout helpers ─────────

    def _reflow_grid(self):
        # Detach any current grid items, deleteLater() non-tile widgets, then
        # re-add tiles in insertion order.
        live_tiles = set(self._tiles.values())
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None and widget not in live_tiles:
                widget.deleteLater()

        if not self._tiles:
            empty = QLabel("No cameras yet — click + Add Project to begin.")
            empty.setStyleSheet("color: #888; font-size: 13px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid_layout.addWidget(empty, 0, 0, 1, _TILES_PER_ROW)
            return

        for i, tile in enumerate(self._tiles.values()):
            row, col = divmod(i, _TILES_PER_ROW)
            self._grid_layout.addWidget(tile, row, col)

    def _refresh_counts(self):
        self._sidebar.update_counts(self._manager.running_count(),
                                     len(self._tiles))

    # ───────── add / remove ─────────

    @Slot()
    def _on_add_project(self):
        if len(self._tiles) >= self._max_workers:
            QMessageBox.information(
                self, "Worker limit",
                f"Worker limit reached ({self._max_workers}). "
                "Remove a project before adding another.")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Add Project", "", "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        cam_id = self._add_project_path(path)
        if cam_id is not None:
            display_name = self._names.get(cam_id, "")
            self.status_text.emit(f"Added: {display_name}")

    def _add_project_path(self, path: str) -> "str | None":
        """Shared add-project flow used by Add and Load Fleet."""
        try:
            proj = ProjectConfig.load(path)
        except Exception as e:
            QMessageBox.warning(self, "Load Error", f"{path}\n\n{e}")
            return None

        cam_id = self._manager.add_camera(path, test_mode=False)
        project_name = proj.project_name or os.path.splitext(os.path.basename(path))[0]
        self._names[cam_id] = project_name

        # Assign a stable, monotonic worker index. Never reuse a number — even
        # after a worker is removed — so LINE messages from a given tile keep
        # a stable identity.
        self._worker_index[cam_id] = self._next_worker_index
        self._next_worker_index += 1
        worker_name = self._worker_name(cam_id)

        tile = CameraTile(cam_id, title=worker_name, subtitle=project_name)
        tile.start_clicked.connect(lambda cid: self._start_one(cid, test=False))
        tile.test_clicked.connect(lambda cid: self._start_one(cid, test=True))
        tile.stop_clicked.connect(self._stop_one)
        tile.remove_clicked.connect(self._remove_one)
        tile.tile_clicked.connect(self._on_tile_clicked)
        tile.settings_clicked.connect(self._on_settings_clicked)
        self._tiles[cam_id] = tile

        self._reflow_grid()
        self._refresh_counts()
        return cam_id

    def _worker_name(self, cam_id: str) -> str:
        idx = self._worker_index.get(cam_id)
        return f"Worker_{idx}" if idx else cam_id[:8]

    @Slot()
    def _on_save_fleet(self):
        if not self._tiles:
            QMessageBox.information(
                self, "Save Fleet", "No cameras to save — add some first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Fleet", "fleet.json", "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        # Pull current project paths from manager (authoritative source).
        cams = self._manager.list_cameras()
        # Preserve the order of self._tiles (insertion order).
        ordered_paths: list[str] = []
        seen: set[str] = set()
        for cam_id in self._tiles:
            for c in cams:
                if c["id"] == cam_id and c["project_path"] not in seen:
                    ordered_paths.append(c["project_path"])
                    seen.add(c["project_path"])
                    break
        data = {"version": 1, "projects": ordered_paths}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            QMessageBox.warning(self, "Save Error", str(e))
            return
        self.status_text.emit(f"Fleet saved: {path}")

    @Slot()
    def _on_load_fleet(self):
        if self.is_any_running():
            answer = QMessageBox.question(
                self, "Stop running cameras?",
                "Loading a fleet will replace the current camera list. "
                "Stop all running cameras and continue?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                return
            # Defer the actual replace until workers exit; simpler path is to
            # block here on shutdown of running ones. shutdown() also stops
            # the poll timer, which we don't want; use stop_all + manual wait.
            self._on_stop_all()
            # Workers exit asynchronously; rely on the user re-issuing Load
            # after the tiles have all turned stopped. Simpler: tell them.
            QMessageBox.information(
                self, "Wait for stop",
                "Cameras are stopping. Click Load Fleet again once all tiles "
                "show stopped (○).")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Load Fleet", "", "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "Load Error", str(e))
            return

        projects = list(data.get("projects", []) or [])
        if not isinstance(projects, list):
            QMessageBox.warning(self, "Load Error",
                                "Fleet file is missing a 'projects' list.")
            return

        # Cap by hardware-detected max_workers. If file has more, warn and
        # truncate (matches the design we agreed on).
        if len(projects) > self._max_workers:
            answer = QMessageBox.question(
                self, "Too many projects",
                f"This fleet has {len(projects)} projects but this machine "
                f"supports max {self._max_workers}. Load only the first "
                f"{self._max_workers}?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                return
            projects = projects[: self._max_workers]

        # Wipe existing (stopped) tiles before adding loaded set. Reset the
        # worker-index counter so the loaded fleet starts at Worker_1.
        for cam_id in list(self._tiles):
            self._do_remove(cam_id)
        self._next_worker_index = 1

        loaded = 0
        skipped = 0
        for p in projects:
            if not isinstance(p, str) or not os.path.isfile(p):
                skipped += 1
                continue
            if self._add_project_path(p) is not None:
                loaded += 1
            else:
                skipped += 1

        msg = f"Fleet loaded: {loaded} projects"
        if skipped:
            msg += f" ({skipped} skipped — missing or invalid)"
        self.status_text.emit(msg)

    @Slot(str)
    def _remove_one(self, cam_id: str):
        if cam_id not in self._tiles:
            return
        state = self._manager.camera_state(cam_id)
        if state in (S_RUNNING, S_SPAWNING, S_STOPPING):
            answer = QMessageBox.question(
                self, "Stop and remove?",
                f"Camera '{self._names.get(cam_id, cam_id)}' is running. "
                "Stop and remove it from the fleet?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                return
            self._mark_for_removal.add(cam_id)
            self._manager.stop_camera(cam_id)
            tile = self._tiles.get(cam_id)
            if tile:
                tile.set_state("stopping")
            return
        # Idle path — remove immediately
        self._do_remove(cam_id)

    def _do_remove(self, cam_id: str):
        self._manager.remove_camera(cam_id)
        tile = self._tiles.pop(cam_id, None)
        self._names.pop(cam_id, None)
        self._worker_index.pop(cam_id, None)
        if tile:
            tile.deleteLater()
        self._reflow_grid()
        self._refresh_counts()

    # ───────── start / stop ─────────

    def _start_one(self, cam_id: str, test: bool):
        tile = self._tiles.get(cam_id)
        if tile is None:
            return
        # Re-check the limit at start time too — running count must stay <=
        # max_workers. We already enforce add-time, but keep this defensive.
        if self._manager.running_count() >= self._max_workers \
                and self._manager.camera_state(cam_id) == S_STOPPED:
            QMessageBox.information(
                self, "Worker limit",
                f"Already running {self._max_workers} workers — stop one first.")
            return
        tile.set_state("spawning", test_mode=test)
        self._manager.start_camera(cam_id, test_mode=test,
                                    display_opts=self._display_opts)
        self._refresh_counts()

    @Slot(str)
    def _stop_one(self, cam_id: str):
        tile = self._tiles.get(cam_id)
        if tile is not None:
            tile.set_state("stopping")
        self._manager.stop_camera(cam_id)

    @Slot()
    def _on_start_all(self):
        for cam_id in list(self._tiles.keys()):
            if self._manager.camera_state(cam_id) == S_STOPPED:
                self._start_one(cam_id, test=False)

    @Slot()
    def _on_test_all(self):
        for cam_id in list(self._tiles.keys()):
            if self._manager.camera_state(cam_id) == S_STOPPED:
                self._start_one(cam_id, test=True)

    @Slot()
    def _on_stop_all(self):
        for cam_id in list(self._tiles.keys()):
            state = self._manager.camera_state(cam_id)
            if state in (S_RUNNING, S_SPAWNING):
                self._stop_one(cam_id)

    # ───────── manager signal handlers ─────────

    @Slot(str, dict)
    def _on_started(self, cam_id: str, payload: dict):
        tile = self._tiles.get(cam_id)
        if tile is None:
            return
        tile.set_state("running", test_mode=tile.test_mode)
        tile.set_status_text(f"on {payload.get('device', '?')}")
        self._refresh_counts()

    @Slot(str, str, str)
    def _on_event(self, cam_id: str, text: str, color: str):
        self._sidebar.append_event(self._worker_name(cam_id), text, color)
        tile = self._tiles.get(cam_id)
        if tile is not None:
            tile.increment_event_count()

    @Slot(str, str, bool)
    def _on_noti_result(self, cam_id: str, text: str, success: bool):
        color = "#44cc44" if success else "#ffaa00"
        self._sidebar.append_event(
            self._worker_name(cam_id), f"  → {text}", color)

    @Slot(str, dict)
    def _on_status(self, cam_id: str, payload: dict):
        tile = self._tiles.get(cam_id)
        if tile is not None:
            tile.set_status_text(payload.get("text", ""))

    @Slot(str, bytes)
    def _on_thumbnail(self, cam_id: str, jpeg: bytes):
        tile = self._tiles.get(cam_id)
        if tile is not None:
            tile.set_thumbnail(jpeg)

    @Slot(str, bytes)
    def _on_live_frame(self, cam_id: str, jpeg: bytes):
        # Only forward to the full-screen view if it's currently attached to
        # this camera. Other cameras' live frames (which shouldn't even be
        # streaming) are dropped.
        if self._full_view.attached_camera_id() == cam_id:
            self._full_view.set_frame(jpeg)

    @Slot(str)
    def _on_tile_clicked(self, cam_id: str):
        # Only meaningful while the camera is actually running — stopped tiles
        # have nothing to stream.
        state = self._manager.camera_state(cam_id)
        if state != S_RUNNING:
            return
        self.enter_full_screen(cam_id)

    @Slot(dict)
    def _on_display_options_changed(self, opts: dict):
        self._display_opts = dict(opts)
        # Push to every currently-running worker so the change is live.
        for cam_id in list(self._tiles):
            state = self._manager.camera_state(cam_id)
            if state in (S_SPAWNING, S_RUNNING):
                self._manager.set_overrides(
                    cam_id, {"display": self._display_opts})

    @Slot(str)
    def _on_settings_clicked(self, cam_id: str):
        # Look up the project path from the manager and re-load it from disk
        # so the dialog reflects whatever's currently saved (in case the user
        # edited it in Single tab while this fleet was loaded).
        cams = self._manager.list_cameras()
        path = next((c["project_path"] for c in cams if c["id"] == cam_id), None)
        if not path:
            return
        try:
            proj = ProjectConfig.load(path)
        except Exception as e:
            QMessageBox.warning(self, "Load Error",
                                f"Could not read project for view:\n{e}")
            return
        dlg = NotiSetupDialog(proj, project_path=path,
                               worker_name=self._worker_name(cam_id),
                               parent=self)
        dlg.exec()

    @Slot(str, str)
    def _on_error(self, cam_id: str, msg: str):
        tile = self._tiles.get(cam_id)
        if tile is not None:
            tile.set_state("error")
            tile.set_status_text(f"Error: {msg}")
        self._sidebar.append_event(
            self._worker_name(cam_id), f"ERROR: {msg}", "#ff4444")
        self._refresh_counts()

    @Slot(str, str)
    def _on_finished(self, cam_id: str, reason: str):
        tile = self._tiles.get(cam_id)
        # The manager fires this both when the worker emits MSG_FINISHED and
        # when the process is reaped. Make the handler idempotent.
        if tile is not None and tile.state() not in ("stopped", "error"):
            tile.set_state("stopped")
            tile.set_status_text(f"finished: {reason}")
        self._refresh_counts()

        # If the camera that just stopped is the one in full-screen, leave the
        # view so the user isn't stuck staring at a frozen final frame.
        if self._full_view.attached_camera_id() == cam_id:
            self.exit_full_screen()

        if cam_id in self._mark_for_removal:
            self._mark_for_removal.discard(cam_id)
            self._do_remove(cam_id)
