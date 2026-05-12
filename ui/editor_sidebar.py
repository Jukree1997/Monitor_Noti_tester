from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QComboBox, QSlider, QLineEdit, QRadioButton, QButtonGroup,
    QFileDialog, QScrollArea, QListWidget, QListWidgetItem, QCheckBox,
    QInputDialog, QSizePolicy,
)

ACCENT = "#E8740C"
ACCENT_HOVER = "#CF6700"
SIDEBAR_WIDTH = 320


def styled_button(text, parent=None, width=None) -> QPushButton:
    btn = QPushButton(text, parent)
    btn.setStyleSheet(f"""
        QPushButton {{
            background-color: {ACCENT};
            color: white;
            border: none;
            border-radius: 4px;
            padding: 6px 12px;
            font-weight: bold;
        }}
        QPushButton:hover {{ background-color: {ACCENT_HOVER}; }}
        QPushButton:pressed {{ background-color: #B85A00; }}
    """)
    if width:
        btn.setFixedWidth(width)
    return btn


class CollapsibleSection(QWidget):
    """A section with chevron header that collapses/expands its body."""

    expanded_signal = Signal(object)  # emits self when expanded

    def __init__(self, title: str, parent=None, start_collapsed: bool = False):
        super().__init__(parent)
        self._expanded = not start_collapsed
        self._title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        self._header = QFrame()
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet("QFrame { padding: 4px; }")
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(10, 4, 10, 4)

        self._chevron = QLabel("\u25bc" if self._expanded else "\u25b6")
        self._chevron.setFont(QFont("Segoe UI", 10))
        self._chevron.setFixedWidth(16)
        header_layout.addWidget(self._chevron)

        self._title_label = QLabel(title)
        self._title_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()

        self._header.mousePressEvent = lambda e: self.toggle()
        layout.addWidget(self._header)

        # Body
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(10, 4, 10, 8)
        self._body_layout.setSpacing(6)
        self._body.setVisible(self._expanded)
        layout.addWidget(self._body)

    @property
    def title(self) -> str:
        return self._title

    @property
    def body(self) -> QWidget:
        return self._body

    @property
    def body_layout(self) -> QVBoxLayout:
        return self._body_layout

    @property
    def expanded(self) -> bool:
        return self._expanded

    def toggle(self):
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._chevron.setText("\u25bc" if self._expanded else "\u25b6")  # ▼ / ▶
        if self._expanded:
            self.expanded_signal.emit(self)

    def collapse(self):
        if self._expanded:
            self._expanded = False
            self._body.setVisible(False)
            self._chevron.setText("\u25b6")

    def expand(self):
        if not self._expanded:
            self._expanded = True
            self._body.setVisible(True)
            self._chevron.setText("\u25bc")
            self.expanded_signal.emit(self)


class EditorSidebar(QWidget):
    """Right panel with collapsible sections and fixed bottom block."""

    # Signals
    browse_model = Signal()
    source_changed = Signal(str, object)  # source_type, value
    connect_requested = Signal()
    disconnect_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    conf_changed = Signal(float)
    iou_changed = Signal(float)
    imgsz_changed = Signal(int)
    add_zone_requested = Signal(str)   # zone name
    add_line_requested = Signal(str)   # line name
    load_project_requested = Signal()
    save_project_requested = Signal()
    draw_mode_changed = Signal(str)
    show_detections_changed = Signal(bool)
    zone_toggled = Signal(str, bool)   # zone_id, enabled
    line_toggled = Signal(str, bool)   # line_id, enabled
    delete_region = Signal(str, str)   # type ("zone"/"line"), id
    rename_region = Signal(str, str, str)  # type ("zone"/"line"), id, new_name
    line_flip = Signal(str)  # line_id
    edit_mode_requested = Signal()  # enter edit/drag mode

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(SIDEBAR_WIDTH)
        self._sections: list[CollapsibleSection] = []
        self._prev_section_state: dict[int, bool] = {}
        self._accordion_mode = True  # only one section open at a time
        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # === Row 0: Top Bar ===
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(10, 5, 10, 5)

        self.btn_theme = styled_button("\u2600", width=35)  # ☀
        self.btn_theme.setFont(QFont("Segoe UI", 14))
        top_bar.addWidget(self.btn_theme)
        top_bar.addStretch()

        self.btn_collapse_all = styled_button("\u2630", width=35)  # ☰
        self.btn_collapse_all.setFont(QFont("Segoe UI", 14))
        self.btn_collapse_all.clicked.connect(self._toggle_all_sections)
        top_bar.addWidget(self.btn_collapse_all)
        main_layout.addLayout(top_bar)

        # === Row 0b: Project Load/Save — always visible, matches the
        # Single Project tab's sidebar styling so the two tabs feel like
        # parts of the same app. Lives outside the collapsible sections
        # so the user can save at any time without scrolling around.
        project_row = QHBoxLayout()
        project_row.setContentsMargins(10, 0, 10, 6)
        project_row.setSpacing(6)

        self.btn_load_project = styled_button("Load Project")
        self.btn_load_project.setStyleSheet("""
            QPushButton { background-color: #3A7CA5; color: white; border: none;
                border-radius: 4px; padding: 6px 12px; font-weight: bold; }
            QPushButton:hover { background-color: #2E6585; }
            QPushButton:disabled { background-color: #555; color: #aaa; }
        """)
        self.btn_load_project.clicked.connect(self.load_project_requested.emit)
        project_row.addWidget(self.btn_load_project, 1)

        self.btn_save_project = styled_button("Save Project")
        self.btn_save_project.setStyleSheet("""
            QPushButton { background-color: #1F7A8C; color: white; border: none;
                border-radius: 4px; padding: 6px 12px; font-weight: bold; }
            QPushButton:hover { background-color: #175E6D; }
            QPushButton:disabled { background-color: #555; color: #aaa; }
        """)
        self.btn_save_project.clicked.connect(self.save_project_requested.emit)
        project_row.addWidget(self.btn_save_project, 1)

        main_layout.addLayout(project_row)

        # === Row 1: Scrollable Sections ===
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        scroll_content = QWidget()
        self._sections_layout = QVBoxLayout(scroll_content)
        self._sections_layout.setContentsMargins(0, 0, 0, 0)
        self._sections_layout.setSpacing(2)

        self._build_model_section()
        self._build_source_section()
        self._build_zones_section()
        self._build_config_section()

        self._sections_layout.addStretch()
        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll, stretch=1)

        # === Row 2: Bottom Block (FIXED) ===
        self._build_bottom_block(main_layout)

    def _add_section(self, title: str) -> CollapsibleSection:
        # First section starts expanded, rest collapsed
        start_collapsed = len(self._sections) > 0
        section = CollapsibleSection(title, start_collapsed=start_collapsed)
        section.expanded_signal.connect(self._on_section_expanded)
        self._sections.append(section)
        self._sections_layout.addWidget(section)
        return section

    def _on_section_expanded(self, expanded_section):
        """Accordion: collapse all other sections when one is opened."""
        if not self._accordion_mode:
            return
        for sec in self._sections:
            if sec is not expanded_section:
                sec.collapse()

    # --- Model & Detection Section ---
    def _build_model_section(self):
        sec = self._add_section("Model & Detection")
        lay = sec.body_layout

        row = QHBoxLayout()
        self.model_entry = QLineEdit()
        self.model_entry.setPlaceholderText("No model selected...")
        self.model_entry.setReadOnly(True)
        row.addWidget(self.model_entry)

        self.btn_browse_model = styled_button("Browse", width=70)
        self.btn_browse_model.clicked.connect(self.browse_model.emit)
        row.addWidget(self.btn_browse_model)
        lay.addLayout(row)

        self.lbl_model_status = QLabel("No model loaded")
        self.lbl_model_status.setStyleSheet("color: gray; font-size: 11px;")
        lay.addWidget(self.lbl_model_status)

        # Device label
        self.lbl_device = QLabel("Device: --")
        self.lbl_device.setStyleSheet("color: gray; font-size: 11px;")
        lay.addWidget(self.lbl_device)

        # --- Detection settings (merged) ---
        # Confidence
        lay.addWidget(QLabel("Confidence Threshold:"))
        conf_row = QHBoxLayout()
        self.slider_conf = QSlider(Qt.Orientation.Horizontal)
        self.slider_conf.setRange(5, 95)
        self.slider_conf.setValue(40)
        self.slider_conf.setTickInterval(5)
        conf_row.addWidget(self.slider_conf)
        self.lbl_conf = QLabel("0.40")
        self.lbl_conf.setFixedWidth(40)
        conf_row.addWidget(self.lbl_conf)
        lay.addLayout(conf_row)

        self.slider_conf.valueChanged.connect(
            lambda v: (self.lbl_conf.setText(f"{v/100:.2f}"), self.conf_changed.emit(v / 100))
        )

        # IoU
        lay.addWidget(QLabel("IoU Threshold:"))
        iou_row = QHBoxLayout()
        self.slider_iou = QSlider(Qt.Orientation.Horizontal)
        self.slider_iou.setRange(5, 95)
        self.slider_iou.setValue(45)
        self.slider_iou.setTickInterval(5)
        iou_row.addWidget(self.slider_iou)
        self.lbl_iou = QLabel("0.45")
        self.lbl_iou.setFixedWidth(40)
        iou_row.addWidget(self.lbl_iou)
        lay.addLayout(iou_row)

        self.slider_iou.valueChanged.connect(
            lambda v: (self.lbl_iou.setText(f"{v/100:.2f}"), self.iou_changed.emit(v / 100))
        )

        # Image size
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Inference Size:"))
        self.combo_imgsz = QComboBox()
        self.combo_imgsz.addItems(["320", "416", "640", "800", "1024", "1280"])
        self.combo_imgsz.setCurrentText("640")
        self.combo_imgsz.currentTextChanged.connect(lambda t: self.imgsz_changed.emit(int(t)))
        size_row.addWidget(self.combo_imgsz)
        self.lbl_imgsz_warning = QLabel("")
        self.lbl_imgsz_warning.setStyleSheet(
            "color: #FFA500; font-size: 14px; font-weight: bold;")
        size_row.addWidget(self.lbl_imgsz_warning)
        size_row.addStretch()
        lay.addLayout(size_row)

        # Source → inference scale info. Updated by main_window when the source
        # connects or imgsz changes. Warns when imgsz/max(src) < 0.4 because
        # downscaled small objects fall under YOLO's ~16px detection floor.
        self.lbl_scale_info = QLabel("Scale: -- (connect source)")
        self.lbl_scale_info.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_scale_info.setWordWrap(True)
        lay.addWidget(self.lbl_scale_info)

    def update_scale_info(self, src_w: int, src_h: int, imgsz: int):
        """Refresh the feed→inference scale label and the warning glyph."""
        if src_w <= 0 or src_h <= 0:
            self.lbl_scale_info.setText("Scale: -- (connect source)")
            self.lbl_scale_info.setStyleSheet("color: gray; font-size: 11px;")
            self.lbl_imgsz_warning.setText("")
            self.lbl_imgsz_warning.setToolTip("")
            return
        longest = max(src_w, src_h)
        scale = imgsz / longest if longest > 0 else 1.0
        inf_w = max(1, int(round(src_w * scale)))
        inf_h = max(1, int(round(src_h * scale)))
        self.lbl_scale_info.setText(
            f"Feed {src_w}×{src_h} → {inf_w}×{inf_h} ({scale:.2f}×)")
        if scale < 0.4:
            self.lbl_scale_info.setStyleSheet("color: #FFA500; font-size: 11px;")
            self.lbl_imgsz_warning.setText("⚠")
            self.lbl_imgsz_warning.setToolTip(
                f"Inference scale {scale:.2f}× is below 0.4×.\n"
                "Small/distant objects may fall below YOLO's detection floor "
                "after letterboxing.\nConsider raising the inference size or "
                "cropping the ROI before inference.")
        else:
            self.lbl_scale_info.setStyleSheet("color: gray; font-size: 11px;")
            self.lbl_imgsz_warning.setText("")
            self.lbl_imgsz_warning.setToolTip("")

    # --- Source Section ---
    def _build_source_section(self):
        sec = self._add_section("Input Source")
        lay = sec.body_layout

        self._source_group = QButtonGroup(self)

        # RTSP
        self.radio_rtsp = QRadioButton("RTSP / URL")
        self._source_group.addButton(self.radio_rtsp)
        lay.addWidget(self.radio_rtsp)

        self.rtsp_entry = QLineEdit()
        self.rtsp_entry.setPlaceholderText("rtsp://user:pass@ip:port/stream")
        lay.addWidget(self.rtsp_entry)

        # USB Camera
        self.radio_camera = QRadioButton("USB Camera")
        self._source_group.addButton(self.radio_camera)
        self.radio_camera.setChecked(True)
        lay.addWidget(self.radio_camera)

        cam_row = QHBoxLayout()
        self.camera_combo = QComboBox()
        self.camera_combo.setMinimumWidth(120)
        cam_row.addWidget(self.camera_combo)

        self.btn_refresh_cameras = styled_button("\u21bb", width=35)  # ↻
        self.btn_refresh_cameras.setToolTip("Scan for cameras")
        cam_row.addWidget(self.btn_refresh_cameras)
        lay.addLayout(cam_row)

        # Video File
        self.radio_file = QRadioButton("Video File")
        self._source_group.addButton(self.radio_file)
        lay.addWidget(self.radio_file)

        file_row = QHBoxLayout()
        self.file_entry = QLineEdit()
        self.file_entry.setPlaceholderText("No file selected...")
        self.file_entry.setReadOnly(True)
        file_row.addWidget(self.file_entry)

        self.btn_browse_file = styled_button("Browse", width=70)
        file_row.addWidget(self.btn_browse_file)
        lay.addLayout(file_row)

        # Connect / Disconnect button
        self.btn_connect = styled_button("\u25b6  Connect")
        self.btn_connect.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.btn_connect.setFixedHeight(34)
        lay.addWidget(self.btn_connect)

        # Connection status
        self.lbl_source_status = QLabel("Not connected")
        self.lbl_source_status.setStyleSheet("color: gray; font-size: 11px;")
        lay.addWidget(self.lbl_source_status)

        # Source resolution info
        self.lbl_source_resolution = QLabel("")
        self.lbl_source_resolution.setStyleSheet("color: gray; font-size: 11px;")
        lay.addWidget(self.lbl_source_resolution)

    # --- Zones & Lines Section ---
    def _build_zones_section(self):
        sec = self._add_section("Zones & Lines")
        lay = sec.body_layout

        # Drawing mode radio buttons: Zone | Line | Edit
        self._draw_mode_group = QButtonGroup(self)
        mode_row = QHBoxLayout()

        self.radio_draw_zone = QRadioButton("Zone")
        self.radio_draw_zone.setChecked(True)
        self._draw_mode_group.addButton(self.radio_draw_zone)
        mode_row.addWidget(self.radio_draw_zone)

        self.radio_draw_line = QRadioButton("Line")
        self._draw_mode_group.addButton(self.radio_draw_line)
        mode_row.addWidget(self.radio_draw_line)

        self.radio_edit = QRadioButton("Edit")
        self._draw_mode_group.addButton(self.radio_edit)
        mode_row.addWidget(self.radio_edit)

        lay.addLayout(mode_row)

        # Name entry
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.region_name_entry = QLineEdit()
        self.region_name_entry.setPlaceholderText("e.g. parking")
        name_row.addWidget(self.region_name_entry)
        lay.addLayout(name_row)

        # Draw button
        self.btn_draw = styled_button("Draw")
        self.btn_draw.clicked.connect(self._on_draw)
        lay.addWidget(self.btn_draw)

        # Merged list
        self.region_list = QListWidget()
        self.region_list.setMaximumHeight(150)
        self.region_list.setStyleSheet("""
            QListWidget::item { padding: 3px 4px; }
            QListWidget::item:selected { background-color: #E8740C; color: white; }
        """)
        lay.addWidget(self.region_list)

        # Action buttons row: Rename | Delete
        action_row = QHBoxLayout()
        self.btn_rename_region = styled_button("Rename")
        self.btn_rename_region.clicked.connect(self._on_rename_region)
        action_row.addWidget(self.btn_rename_region)

        self.btn_del_region = styled_button("Delete")
        self.btn_del_region.clicked.connect(self._on_delete_region)
        action_row.addWidget(self.btn_del_region)
        lay.addLayout(action_row)

        # Line flip button (shown when a line is selected)
        self._line_settings_frame = QFrame()
        line_settings_layout = QHBoxLayout(self._line_settings_frame)
        line_settings_layout.setContentsMargins(0, 4, 0, 0)

        self.btn_flip = styled_button("\u2194  Flip Direction")
        self.btn_flip.clicked.connect(self._on_flip_clicked)
        line_settings_layout.addWidget(self.btn_flip)

        lay.addWidget(self._line_settings_frame)
        self._line_settings_frame.setVisible(False)

        # Connect list selection to show/hide line settings
        self.region_list.currentItemChanged.connect(self._on_region_selected)

        # Project Load/Save lives at the top of the sidebar (outside
        # this collapsible section) so it's reachable regardless of
        # which section is expanded — see _build_ui below.

    # --- Notification Section (LINE OA) ---
    def _build_config_section(self):
        sec = self._add_section("Notification")
        lay = sec.body_layout

        # Channel Access Token
        lay.addWidget(QLabel("Channel Access Token:"))
        self.notif_token_entry = QLineEdit()
        self.notif_token_entry.setPlaceholderText("Paste LINE channel token...")
        self.notif_token_entry.setEchoMode(QLineEdit.EchoMode.Password)
        lay.addWidget(self.notif_token_entry)

        # Target ID
        lay.addWidget(QLabel("Target ID (User/Group):"))
        self.notif_target_entry = QLineEdit()
        self.notif_target_entry.setPlaceholderText("U1234... or C1234...")
        lay.addWidget(self.notif_target_entry)

        # Cooldown
        cooldown_row = QHBoxLayout()
        cooldown_row.addWidget(QLabel("Cooldown:"))
        self.notif_cooldown_spin = QComboBox()
        self.notif_cooldown_spin.addItems(["10", "15", "30", "60", "120", "300"])
        self.notif_cooldown_spin.setCurrentText("30")
        cooldown_row.addWidget(self.notif_cooldown_spin)
        cooldown_row.addWidget(QLabel("sec"))
        lay.addLayout(cooldown_row)

        # Send snapshot
        self.notif_send_image = QCheckBox("Send snapshot image")
        self.notif_send_image.setChecked(True)
        lay.addWidget(self.notif_send_image)

    # --- Bottom Block (fixed) ---
    def _build_bottom_block(self, parent_layout: QVBoxLayout):
        bottom = QFrame()
        bottom.setStyleSheet("QFrame { border-top: 1px solid #555; }")
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(10, 8, 10, 8)
        bottom_layout.setSpacing(4)

        # Start/Stop button
        self.btn_start_stop = styled_button("\u25b6  Start Detection")
        self.btn_start_stop.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self.btn_start_stop.setFixedHeight(40)
        bottom_layout.addWidget(self.btn_start_stop)

        # Draw mode + show detections
        options_row = QHBoxLayout()

        self.chk_show_detections = QCheckBox("Show Detections")
        self.chk_show_detections.setChecked(True)
        self.chk_show_detections.stateChanged.connect(
            lambda s: self.show_detections_changed.emit(s == Qt.CheckState.Checked.value)
        )
        options_row.addWidget(self.chk_show_detections)

        options_row.addStretch()

        self.combo_draw_mode = QComboBox()
        self.combo_draw_mode.addItems(["Box", "Dot"])
        self.combo_draw_mode.currentTextChanged.connect(self.draw_mode_changed.emit)
        options_row.addWidget(self.combo_draw_mode)

        bottom_layout.addLayout(options_row)

        # Events Log — collapsible header
        self._events_expanded = True
        events_header = QFrame()
        events_header.setCursor(Qt.CursorShape.PointingHandCursor)
        events_header_layout = QHBoxLayout(events_header)
        events_header_layout.setContentsMargins(0, 4, 0, 2)

        self._events_chevron = QLabel("\u25bc")
        self._events_chevron.setFont(QFont("Segoe UI", 10))
        self._events_chevron.setFixedWidth(16)
        events_header_layout.addWidget(self._events_chevron)

        lbl_events = QLabel("Events Log")
        lbl_events.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        events_header_layout.addWidget(lbl_events)
        events_header_layout.addStretch()

        self._events_count_label = QLabel("(0)")
        self._events_count_label.setStyleSheet("color: #888; font-size: 10px;")
        events_header_layout.addWidget(self._events_count_label)

        events_header.mousePressEvent = lambda e: self._toggle_events_log()
        bottom_layout.addWidget(events_header)

        # Events list (collapsible body)
        self.events_list = QListWidget()
        self.events_list.setVisible(True)
        self.events_list.setFixedHeight(130)
        self.events_list.setFont(QFont("Consolas", 9))
        self.events_list.setStyleSheet("""
            QListWidget {
                background-color: #1e1e1e;
                color: #00ff88;
                border: 2px solid #E8740C;
                border-radius: 6px;
            }
        """)
        bottom_layout.addWidget(self.events_list)

        # Status label
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #888; font-size: 10px;")
        self.lbl_status.setWordWrap(True)
        bottom_layout.addWidget(self.lbl_status)

        parent_layout.addWidget(bottom)

    def _toggle_events_log(self):
        self._events_expanded = not self._events_expanded
        self.events_list.setVisible(self._events_expanded)
        self._events_chevron.setText("\u25bc" if self._events_expanded else "\u25b6")

    # --- Helpers ---
    def _toggle_all_sections(self):
        """Toggle between accordion mode (one at a time) and expand-all mode."""
        any_expanded = any(s.expanded for s in self._sections)
        if any_expanded:
            # Collapse all
            self._prev_section_state = {i: s.expanded for i, s in enumerate(self._sections)}
            self._accordion_mode = False  # temporarily disable so collapse doesn't trigger
            for s in self._sections:
                s.collapse()
            self._accordion_mode = True
        else:
            # Expand all (temporarily disable accordion)
            self._accordion_mode = False
            prev = self._prev_section_state or {i: True for i in range(len(self._sections))}
            for i, s in enumerate(self._sections):
                if prev.get(i, True):
                    s.expand()
            self._accordion_mode = True

    def _get_region_name(self, default: str) -> str:
        """Get name from entry field, fallback to default."""
        name = self.region_name_entry.text().strip()
        if not name:
            name = default
        return name

    def _on_draw(self):
        """Handle Draw button based on selected radio."""
        if self.radio_draw_zone.isChecked():
            name = self._get_region_name("zone")
            self.add_zone_requested.emit(name)
        elif self.radio_draw_line.isChecked():
            name = self._get_region_name("line")
            self.add_line_requested.emit(name)
        elif self.radio_edit.isChecked():
            self.edit_mode_requested.emit()

    def _on_region_selected(self, current, previous):
        """Show flip button only when a line is selected."""
        if current is None:
            self._line_settings_frame.setVisible(False)
            return
        region_type = current.data(Qt.ItemDataRole.UserRole + 1)
        self._line_settings_frame.setVisible(region_type == "line")

    def _on_flip_clicked(self):
        item = self.region_list.currentItem()
        if not item:
            return
        region_type = item.data(Qt.ItemDataRole.UserRole + 1)
        region_id = item.data(Qt.ItemDataRole.UserRole)
        if region_type == "line" and region_id:
            self.line_flip.emit(region_id)

    def _on_delete_region(self):
        item = self.region_list.currentItem()
        if not item:
            return
        region_id = item.data(Qt.ItemDataRole.UserRole)
        region_type = item.data(Qt.ItemDataRole.UserRole + 1)
        if region_id and region_type:
            self.delete_region.emit(region_type, region_id)

    def _on_rename_region(self):
        item = self.region_list.currentItem()
        if not item:
            return
        region_id = item.data(Qt.ItemDataRole.UserRole)
        region_type = item.data(Qt.ItemDataRole.UserRole + 1)
        old_name = item.data(Qt.ItemDataRole.UserRole + 2)
        if not region_id or not region_type:
            return
        type_label = "Zone" if region_type == "zone" else "Line"
        new_name, ok = QInputDialog.getText(
            self, f"Rename {type_label}", f"New name for '{old_name}':",
            text=old_name
        )
        if ok and new_name.strip() and new_name.strip() != old_name:
            self.rename_region.emit(region_type, region_id, new_name.strip())

    def add_event(self, text: str):
        self.events_list.insertItem(0, text)
        # Keep max 100 events
        while self.events_list.count() > 100:
            self.events_list.takeItem(self.events_list.count() - 1)
        self._events_count_label.setText(f"({self.events_list.count()})")

    def update_region_list(self, zones, lines):
        """Rebuild the merged zone + line list."""
        self.region_list.clear()
        for zone in zones:
            label = f"[Zone] {zone.name}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, zone.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, "zone")
            item.setData(Qt.ItemDataRole.UserRole + 2, zone.name)
            # Color indicator
            item.setForeground(QColor(zone.color))
            self.region_list.addItem(item)
        for line in lines:
            flip_mark = " flipped" if line.invert else ""
            label = f"[Line{flip_mark}] {line.name}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, line.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, "line")
            item.setData(Qt.ItemDataRole.UserRole + 2, line.name)
            item.setForeground(QColor(line.color))
            self.region_list.addItem(item)

    def set_connected(self, connected: bool, info: str = "",
                      resolution: tuple[int, int] = (0, 0), fps: float = 0.0):
        if connected:
            self.btn_connect.setText("\u25a0  Disconnect")
            self.btn_connect.setStyleSheet(f"""
                QPushButton {{
                    background-color: #CC3333;
                    color: white; border: none; border-radius: 4px;
                    padding: 6px 12px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: #AA2222; }}
            """)
            self.lbl_source_status.setText(f"Connected: {info}")
            self.lbl_source_status.setStyleSheet("color: #00cc66; font-size: 11px;")
            w, h = resolution
            res_text = f"Feed Resolution: {w} x {h}"
            if fps > 0:
                res_text += f"  |  {fps:.1f} FPS"
            self.lbl_source_resolution.setText(res_text)
        else:
            self.btn_connect.setText("\u25b6  Connect")
            self.btn_connect.setStyleSheet(f"""
                QPushButton {{
                    background-color: {ACCENT};
                    color: white; border: none; border-radius: 4px;
                    padding: 6px 12px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: {ACCENT_HOVER}; }}
            """)
            self.lbl_source_status.setText("Not connected")
            self.lbl_source_status.setStyleSheet("color: gray; font-size: 11px;")
            self.lbl_source_resolution.setText("")

    def set_running(self, running: bool):
        if running:
            self.btn_start_stop.setText("\u25a0  Stop Detection")
            self.btn_start_stop.setStyleSheet(f"""
                QPushButton {{
                    background-color: #CC3333;
                    color: white; border: none; border-radius: 4px;
                    padding: 6px 12px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: #AA2222; }}
            """)
        else:
            self.btn_start_stop.setText("\u25b6  Start Detection")
            self.btn_start_stop.setStyleSheet(f"""
                QPushButton {{
                    background-color: {ACCENT};
                    color: white; border: none; border-radius: 4px;
                    padding: 6px 12px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: {ACCENT_HOVER}; }}
            """)

    def get_source(self) -> tuple[str, object]:
        """Returns (source_type, value)."""
        if self.radio_rtsp.isChecked():
            return "rtsp", self.rtsp_entry.text().strip()
        elif self.radio_camera.isChecked():
            idx = self.camera_combo.currentIndex()
            text = self.camera_combo.currentText()
            # Extract camera index from text like "Camera 0"
            try:
                cam_idx = int(text.split()[-1]) if text else 0
            except (ValueError, IndexError):
                cam_idx = idx
            return "camera", cam_idx
        else:
            return "file", self.file_entry.text().strip()

    def get_notification_settings(self) -> dict:
        return {
            "channel_token": self.notif_token_entry.text().strip(),
            "target_id": self.notif_target_entry.text().strip(),
            "cooldown_seconds": int(self.notif_cooldown_spin.currentText()),
            "send_image": self.notif_send_image.isChecked(),
        }

    def set_notification_settings(self, settings: dict):
        self.notif_token_entry.setText(settings.get("channel_token", ""))
        self.notif_target_entry.setText(settings.get("target_id", ""))
        cooldown = str(settings.get("cooldown_seconds", 30))
        idx = self.notif_cooldown_spin.findText(cooldown)
        if idx >= 0:
            self.notif_cooldown_spin.setCurrentIndex(idx)
        self.notif_send_image.setChecked(settings.get("send_image", True))
