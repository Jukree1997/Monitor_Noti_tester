"""Sidebar for the Fleet tab — hardware info, worker slots, action buttons,
combined event log."""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QListWidget, QListWidgetItem, QSizePolicy, QCheckBox,
    QRadioButton, QButtonGroup,
)


def _btn_css(bg: str, hover: str) -> str:
    return (
        f"QPushButton {{ background: {bg}; color: white; border: none; "
        f"border-radius: 4px; padding: 5px 10px; font-weight: bold; }}"
        f"QPushButton:hover {{ background: {hover}; }}"
        f"QPushButton:disabled {{ background: #555; color: #aaa; }}"
    )


class FleetSidebar(QWidget):
    add_project_requested   = Signal()
    start_all_requested     = Signal()
    test_all_requested      = Signal()
    stop_all_requested      = Signal()
    save_fleet_requested    = Signal()
    load_fleet_requested    = Signal()
    display_options_changed = Signal(dict)   # {show_detections, show_labels, mode}

    SIDEBAR_WIDTH = 320
    LOG_CAP = 500

    def __init__(self, hardware_info: dict, parent: "QWidget | None" = None):
        super().__init__(parent)
        self.setFixedWidth(self.SIDEBAR_WIDTH)
        self._max_workers = max(1, int(hardware_info.get("max_workers", 1)))
        self._active_count = 0
        self._total_count = 0
        self._build_ui(hardware_info)

    # ───────── build ─────────

    def _build_ui(self, hw: dict):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # Hardware section
        lay.addWidget(self._section_header("Hardware"))

        gpu_text = (f"GPU {hw.get('gpu_name', 'N/A')}" if hw.get("cuda")
                    else "GPU N/A (CPU-only)")
        if hw.get("cuda"):
            gpu_text += (f"\nVRAM {hw.get('vram_free_gb', 0)} / "
                         f"{hw.get('vram_total_gb', 0)} GB free")
        self._lbl_gpu = QLabel(gpu_text)
        self._lbl_gpu.setStyleSheet("color: #aaa; font-size: 10px;")
        self._lbl_gpu.setWordWrap(True)
        lay.addWidget(self._lbl_gpu)

        self._lbl_cpu = QLabel(f"CPU {hw.get('cpu_count', '?')} cores")
        self._lbl_cpu.setStyleSheet("color: #aaa; font-size: 10px;")
        lay.addWidget(self._lbl_cpu)

        self._lbl_workers = QLabel(f"Workers max {self._max_workers}")
        self._lbl_workers.setStyleSheet(
            "color: #d4d4d4; font-size: 11px; font-weight: bold;")
        lay.addWidget(self._lbl_workers)

        self._lbl_active = QLabel("")
        self._lbl_active.setStyleSheet(
            "color: #d4d4d4; font-size: 11px; font-family: monospace;")
        lay.addWidget(self._lbl_active)

        lay.addWidget(self._hsep())

        # ── Display section (applied to live full-screen frames) ──
        lay.addWidget(self._section_header("Display"))
        self._chk_show_detections = QCheckBox("Show detections")
        self._chk_show_detections.setChecked(True)
        self._chk_show_detections.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        self._chk_show_detections.toggled.connect(self._on_display_changed)
        lay.addWidget(self._chk_show_detections)

        self._chk_show_labels = QCheckBox("Show labels")
        self._chk_show_labels.setChecked(False)  # cleaner monitoring view
        self._chk_show_labels.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        self._chk_show_labels.toggled.connect(self._on_display_changed)
        lay.addWidget(self._chk_show_labels)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        mode_lbl = QLabel("Mode:")
        mode_lbl.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        mode_row.addWidget(mode_lbl)
        self._radio_dot = QRadioButton("Dot")
        self._radio_dot.setChecked(True)  # default Dot
        self._radio_dot.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        mode_row.addWidget(self._radio_dot)
        self._radio_box = QRadioButton("Box")
        self._radio_box.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        mode_row.addWidget(self._radio_box)
        mode_row.addStretch()
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._radio_dot)
        self._mode_group.addButton(self._radio_box)
        self._mode_group.buttonClicked.connect(self._on_display_changed)
        lay.addLayout(mode_row)

        lay.addWidget(self._hsep())

        # Action buttons
        self._btn_add = QPushButton("+ Add Project")
        self._btn_add.setFixedHeight(30)
        self._btn_add.setStyleSheet(_btn_css("#3A7CA5", "#2E6585"))
        self._btn_add.clicked.connect(self.add_project_requested.emit)
        lay.addWidget(self._btn_add)

        run_row = QHBoxLayout()
        run_row.setSpacing(4)
        self._btn_start_all = QPushButton("▶ Start All")
        self._btn_start_all.setFixedHeight(28)
        self._btn_start_all.setStyleSheet(_btn_css("#2E8B57", "#256B45"))
        self._btn_start_all.clicked.connect(self.start_all_requested.emit)
        run_row.addWidget(self._btn_start_all)

        self._btn_test_all = QPushButton("▷ Test All")
        self._btn_test_all.setFixedHeight(28)
        self._btn_test_all.setStyleSheet(_btn_css("#1F7A8C", "#175E6D"))
        self._btn_test_all.clicked.connect(self.test_all_requested.emit)
        run_row.addWidget(self._btn_test_all)
        lay.addLayout(run_row)

        self._btn_stop_all = QPushButton("■ Stop All")
        self._btn_stop_all.setFixedHeight(28)
        self._btn_stop_all.setStyleSheet(_btn_css("#CC3333", "#AA2222"))
        self._btn_stop_all.clicked.connect(self.stop_all_requested.emit)
        lay.addWidget(self._btn_stop_all)

        # Fleet save / load
        fleet_io_row = QHBoxLayout()
        fleet_io_row.setSpacing(4)
        self._btn_save_fleet = QPushButton("\U0001F4BE Save Fleet")
        self._btn_save_fleet.setFixedHeight(26)
        self._btn_save_fleet.setStyleSheet(_btn_css("#666", "#555"))
        self._btn_save_fleet.clicked.connect(self.save_fleet_requested.emit)
        fleet_io_row.addWidget(self._btn_save_fleet)
        self._btn_load_fleet = QPushButton("\U0001F4C2 Load Fleet")
        self._btn_load_fleet.setFixedHeight(26)
        self._btn_load_fleet.setStyleSheet(_btn_css("#666", "#555"))
        self._btn_load_fleet.clicked.connect(self.load_fleet_requested.emit)
        fleet_io_row.addWidget(self._btn_load_fleet)
        lay.addLayout(fleet_io_row)

        lay.addWidget(self._hsep())

        # Combined Event Log
        log_hdr_row = QHBoxLayout()
        log_hdr_row.addWidget(self._section_header("Combined Event Log"), 1)
        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setFixedSize(56, 22)
        self._btn_clear.setStyleSheet(_btn_css("#666", "#555"))
        self._btn_clear.clicked.connect(self.clear_log)
        log_hdr_row.addWidget(self._btn_clear)
        lay.addLayout(log_hdr_row)

        # Filter checkboxes
        self._chk_hide_nonnoti = QCheckBox("Hide non-notified events")
        self._chk_hide_nonnoti.setStyleSheet("color: #aaa; font-size: 11px;")
        self._chk_hide_nonnoti.toggled.connect(self._reapply_log_filter)
        lay.addWidget(self._chk_hide_nonnoti)
        self._chk_hide_test = QCheckBox("Hide test events")
        self._chk_hide_test.setStyleSheet("color: #aaa; font-size: 11px;")
        self._chk_hide_test.toggled.connect(self._reapply_log_filter)
        lay.addWidget(self._chk_hide_test)

        self._log = QListWidget()
        self._log.setStyleSheet(
            "QListWidget { background: #1a1a1a; color: #d4d4d4; "
            " border: 1px solid #444; }"
            "QListWidget::item { padding: 2px 4px; }")
        self._log.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._log.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.Expanding)
        lay.addWidget(self._log, 1)

        self._refresh_active_label()

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        lbl.setStyleSheet("color: #d4d4d4;")
        return lbl

    def _hsep(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #555;")
        return sep

    # ───────── public API ─────────

    def current_display_options(self) -> dict:
        return {
            "show_detections": self._chk_show_detections.isChecked(),
            "show_labels": self._chk_show_labels.isChecked(),
            "mode": "dot" if self._radio_dot.isChecked() else "box",
        }

    def _on_display_changed(self, *_args):
        # buttonClicked passes the clicked button; toggled passes a bool.
        # We ignore both — just re-emit the current state.
        self.display_options_changed.emit(self.current_display_options())

    def update_counts(self, active: int, total: int):
        self._active_count = max(0, min(active, self._max_workers))
        self._total_count = total
        self._refresh_active_label()
        # Hard cap: disable Add when total cameras (loaded) hits the limit.
        at_limit = total >= self._max_workers
        self._btn_add.setEnabled(not at_limit)
        self._btn_add.setToolTip(
            f"Worker limit reached ({self._max_workers}). Remove a project to add another."
            if at_limit else "")

    def append_event(self, cam_name: str, text: str, color: str):
        item = QListWidgetItem(f"[{cam_name}] {text}")
        item.setForeground(QColor(color))
        # Stash filter metadata on the item — filters check this without
        # re-parsing the text.
        is_test = "[TEST" in text
        is_non_noti = color.lower() in ("#888", "#888888")
        item.setData(Qt.ItemDataRole.UserRole,
                      {"test": is_test, "non_noti": is_non_noti})
        self._log.addItem(item)
        self._apply_filter_to_item(item)
        if not item.isHidden():
            self._log.scrollToBottom()
        if self._log.count() > self.LOG_CAP:
            self._log.takeItem(0)

    def clear_log(self):
        self._log.clear()

    def _reapply_log_filter(self):
        for i in range(self._log.count()):
            self._apply_filter_to_item(self._log.item(i))

    def _apply_filter_to_item(self, item: QListWidgetItem):
        meta = item.data(Qt.ItemDataRole.UserRole) or {}
        hide = ((self._chk_hide_nonnoti.isChecked() and meta.get("non_noti"))
                or (self._chk_hide_test.isChecked() and meta.get("test")))
        item.setHidden(bool(hide))

    def _refresh_active_label(self):
        on = "▮"
        off = "▯"
        n = self._max_workers
        a = self._active_count
        bar = on * a + off * (n - a)
        self._lbl_active.setText(f"Active {bar}  {a}/{n}")
