from __future__ import annotations
from dataclasses import asdict
from PySide6.QtCore import Qt, Signal, QLocale, QEvent
from PySide6.QtGui import QFont, QColor, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QComboBox, QCheckBox, QScrollArea, QListWidget,
    QListWidgetItem, QSpinBox, QDoubleSpinBox, QSizePolicy, QSplitter,
)

ACCENT = "#E8740C"
ACCENT_HOVER = "#CF6700"
SIDEBAR_WIDTH = 340
_EN_LOCALE = QLocale(QLocale.Language.English, QLocale.Country.UnitedStates)


def styled_button(text, parent=None, width=None) -> QPushButton:
    btn = QPushButton(text, parent)
    btn.setStyleSheet(f"""
        QPushButton {{
            background-color: {ACCENT}; color: white; border: none;
            border-radius: 4px; padding: 6px 12px; font-weight: bold;
        }}
        QPushButton:hover {{ background-color: {ACCENT_HOVER}; }}
        QPushButton:pressed {{ background-color: #B85A00; }}
    """)
    if width:
        btn.setFixedWidth(width)
    return btn


def arabic_spinbox(parent=None) -> QSpinBox:
    spin = QSpinBox(parent)
    spin.setLocale(_EN_LOCALE)
    return spin


def arabic_double_spinbox(parent=None) -> QDoubleSpinBox:
    """Float spinbox forced to English locale so we always render the
    decimal point as ``.`` regardless of OS regional settings."""
    spin = QDoubleSpinBox(parent)
    spin.setLocale(_EN_LOCALE)
    return spin


class CheckableComboBox(QComboBox):
    """Combo box with checkable items for multi-class selection."""
    selection_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(False)
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self._model.itemChanged.connect(self._on_item_changed)
        self._updating = False
        # Intercept clicks on the dropdown items so they toggle the check state
        # (and keep the popup open) instead of just closing the combo.
        self.view().viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.view().viewport() \
                and event.type() == QEvent.Type.MouseButtonRelease:
            index = self.view().indexAt(event.pos())
            if index.isValid():
                item = self._model.itemFromIndex(index)
                if item is not None and item.isCheckable():
                    new_state = (Qt.CheckState.Unchecked
                                 if item.checkState() == Qt.CheckState.Checked
                                 else Qt.CheckState.Checked)
                    item.setCheckState(new_state)
                    # consume the event so QComboBox doesn't close the popup
                    return True
        return super().eventFilter(obj, event)

    def add_classes(self, class_names: list[str]):
        self._updating = True
        self._model.clear()
        all_item = QStandardItem("All")
        all_item.setCheckable(True)
        all_item.setCheckState(Qt.CheckState.Checked)
        all_item.setData("__all__", Qt.ItemDataRole.UserRole)
        self._model.appendRow(all_item)
        for name in class_names:
            item = QStandardItem(name)
            item.setCheckable(True)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(name, Qt.ItemDataRole.UserRole)
            self._model.appendRow(item)
        self._updating = False
        self._update_display_text()

    def _on_item_changed(self, changed_item: QStandardItem):
        if self._updating:
            return
        self._updating = True
        all_item = self._model.item(0)
        if changed_item.data(Qt.ItemDataRole.UserRole) == "__all__":
            state = changed_item.checkState()
            for i in range(1, self._model.rowCount()):
                self._model.item(i).setCheckState(state)
        else:
            all_checked = all(
                self._model.item(i).checkState() == Qt.CheckState.Checked
                for i in range(1, self._model.rowCount())
            )
            all_item.setCheckState(
                Qt.CheckState.Checked if all_checked else Qt.CheckState.Unchecked
            )
        self._updating = False
        self._update_display_text()
        self.selection_changed.emit()

    def _update_display_text(self):
        all_item = self._model.item(0)
        if all_item and all_item.checkState() == Qt.CheckState.Checked:
            self.setCurrentIndex(0)
            return
        for i in range(1, self._model.rowCount()):
            if self._model.item(i).checkState() == Qt.CheckState.Checked:
                self.setCurrentIndex(i)
                return
        self.setCurrentIndex(-1)

    def get_selected_classes(self) -> list[str]:
        all_item = self._model.item(0)
        if all_item and all_item.checkState() == Qt.CheckState.Checked:
            return []
        return [
            self._model.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(1, self._model.rowCount())
            if self._model.item(i).checkState() == Qt.CheckState.Checked
        ]

    def set_selected_classes(self, names: list[str]):
        """Empty list = "All" (mirrors get_selected_classes semantics)."""
        self._updating = True
        all_item = self._model.item(0)
        if not names:
            if all_item:
                all_item.setCheckState(Qt.CheckState.Checked)
            for i in range(1, self._model.rowCount()):
                self._model.item(i).setCheckState(Qt.CheckState.Checked)
        else:
            wanted = set(names)
            if all_item:
                all_item.setCheckState(Qt.CheckState.Unchecked)
            for i in range(1, self._model.rowCount()):
                item = self._model.item(i)
                state = (Qt.CheckState.Checked
                         if item.data(Qt.ItemDataRole.UserRole) in wanted
                         else Qt.CheckState.Unchecked)
                item.setCheckState(state)
            if all_item and all(
                self._model.item(i).checkState() == Qt.CheckState.Checked
                for i in range(1, self._model.rowCount())
            ):
                all_item.setCheckState(Qt.CheckState.Checked)
        self._updating = False
        self._update_display_text()
        self.selection_changed.emit()

    def currentText(self) -> str:
        all_item = self._model.item(0)
        if all_item and all_item.checkState() == Qt.CheckState.Checked:
            return "All"
        selected = self.get_selected_classes()
        if not selected:
            return "None"
        if len(selected) <= 2:
            return ", ".join(selected)
        return f"{selected[0]}, +{len(selected)-1} more"


class CollapsibleSection(QWidget):
    expanded_signal = Signal(object)

    def __init__(self, title: str, parent=None, start_collapsed: bool = False):
        super().__init__(parent)
        self._expanded = not start_collapsed
        self._title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

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

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(10, 4, 10, 8)
        self._body_layout.setSpacing(6)
        self._body.setVisible(self._expanded)
        layout.addWidget(self._body)

    @property
    def body_layout(self) -> QVBoxLayout:
        return self._body_layout

    @property
    def expanded(self) -> bool:
        return self._expanded

    def toggle(self):
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._chevron.setText("\u25bc" if self._expanded else "\u25b6")
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


class Sidebar(QWidget):
    # Signals
    load_project_requested = Signal()
    save_project_requested = Signal()
    connect_requested = Signal()
    reset_all_requested = Signal()
    show_live_changed = Signal(bool)
    show_detections_changed = Signal(bool)
    show_labels_changed = Signal(bool)
    draw_mode_changed = Signal(str)
    detect_classes_changed = Signal(list)
    browse_video_requested = Signal()
    imgsz_changed = Signal(int)
    export_events_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(SIDEBAR_WIDTH)
        self._sections: list[CollapsibleSection] = []
        self._accordion_mode = True
        self._prev_section_state: dict[int, bool] = {}
        self._line_rule_widgets: list[dict] = []
        self._zone_area_widgets: list[dict] = []
        self._available_classes: list[str] = []
        self._project_loaded = False
        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(10, 5, 10, 5)
        top_bar.addStretch()
        self.btn_collapse_all = styled_button("\u2630", width=35)
        self.btn_collapse_all.setFont(QFont("Segoe UI", 14))
        self.btn_collapse_all.clicked.connect(self._toggle_all_sections)
        top_bar.addWidget(self.btn_collapse_all)
        main_layout.addLayout(top_bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        scroll_content = QWidget()
        self._sections_layout = QVBoxLayout(scroll_content)
        self._sections_layout.setContentsMargins(0, 0, 0, 0)
        self._sections_layout.setSpacing(2)

        self._build_project_section()
        self._build_line_crossing_section()
        self._build_zones_area_section()

        self._sections_layout.addStretch()
        scroll.setWidget(scroll_content)

        # Splitter between sections scroll area and the bottom (controls + log)
        # lets the user drag the divider to resize the event log area.
        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setHandleWidth(6)
        self._main_splitter.setStyleSheet(
            "QSplitter::handle { background-color: #444; }"
            "QSplitter::handle:hover { background-color: #E8740C; }"
        )
        self._main_splitter.addWidget(scroll)
        self._build_bottom_block(self._main_splitter)
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 0)
        # Initial proportions: sections get majority, bottom gets ~320px
        self._main_splitter.setSizes([600, 320])
        main_layout.addWidget(self._main_splitter, stretch=1)

    def _add_section(self, title: str) -> CollapsibleSection:
        start_collapsed = len(self._sections) > 0
        section = CollapsibleSection(title, start_collapsed=start_collapsed)
        section.expanded_signal.connect(self._on_section_expanded)
        self._sections.append(section)
        self._sections_layout.addWidget(section)
        return section

    def _on_section_expanded(self, expanded_section):
        if not self._accordion_mode:
            return
        for sec in self._sections:
            if sec is not expanded_section:
                sec.collapse()

    # ─── Section 1: Project Config ───
    def _build_project_section(self):
        sec = self._add_section("Project Config")
        lay = sec.body_layout

        load_save_row = QHBoxLayout()
        load_save_row.setSpacing(6)
        self.btn_load_project = styled_button("Load Project")
        self.btn_load_project.setStyleSheet("""
            QPushButton { background-color: #3A7CA5; color: white; border: none;
                border-radius: 4px; padding: 6px 12px; font-weight: bold; }
            QPushButton:hover { background-color: #2E6585; }
            QPushButton:disabled { background-color: #555; color: #aaa; }
        """)
        self.btn_load_project.clicked.connect(self.load_project_requested.emit)
        load_save_row.addWidget(self.btn_load_project, 1)

        self.btn_save_project = styled_button("Save Project")
        self.btn_save_project.setEnabled(False)
        self.btn_save_project.setStyleSheet("""
            QPushButton { background-color: #1F7A8C; color: white; border: none;
                border-radius: 4px; padding: 6px 12px; font-weight: bold; }
            QPushButton:hover { background-color: #175E6D; }
            QPushButton:disabled { background-color: #555; color: #aaa; }
        """)
        self.btn_save_project.clicked.connect(self.save_project_requested.emit)
        load_save_row.addWidget(self.btn_save_project, 1)

        lay.addLayout(load_save_row)
        self.lbl_project_name = QLabel("No project loaded")
        self.lbl_project_name.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_project_name.setWordWrap(True)
        lay.addWidget(self.lbl_project_name)
        self.lbl_model_info = QLabel("Model: --")
        self.lbl_model_info.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_model_info.setWordWrap(True)
        lay.addWidget(self.lbl_model_info)
        self.lbl_device_info = QLabel("Device: --")
        self.lbl_device_info.setStyleSheet("color: gray; font-size: 11px;")
        lay.addWidget(self.lbl_device_info)
        self.lbl_source_info = QLabel("Source: --")
        self.lbl_source_info.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_source_info.setWordWrap(True)
        lay.addWidget(self.lbl_source_info)

        # Browse button for swapping the video file (file-source projects only)
        self.btn_browse_video = QPushButton("Browse video...")
        self.btn_browse_video.setStyleSheet("""
            QPushButton { background-color: #555; color: white; border: none;
                border-radius: 4px; padding: 4px 10px; font-size: 11px; }
            QPushButton:hover { background-color: #666; }
        """)
        self.btn_browse_video.clicked.connect(self.browse_video_requested.emit)
        self.btn_browse_video.setVisible(False)
        lay.addWidget(self.btn_browse_video)

        self.lbl_zones_lines_info = QLabel("Zones: 0  |  Lines: 0")
        self.lbl_zones_lines_info.setStyleSheet("color: gray; font-size: 11px;")
        lay.addWidget(self.lbl_zones_lines_info)

        # Inference size selector + downscale info. The combo mirrors the
        # Config UI so users can A/B test 640 vs 1280 without re-saving from
        # the other tool. Mutates project.detection.imgsz live; the runner
        # reads it on start, the engine's update_params handles mid-run swap.
        infsize_row = QHBoxLayout()
        infsize_row.addWidget(QLabel("Inference size:"))
        self.combo_imgsz = QComboBox()
        self.combo_imgsz.addItems(["320", "416", "640", "800", "1024", "1280"])
        self.combo_imgsz.setCurrentText("640")
        self.combo_imgsz.currentTextChanged.connect(
            lambda t: self.imgsz_changed.emit(int(t)))
        infsize_row.addWidget(self.combo_imgsz)
        self.lbl_imgsz_warning = QLabel("")
        self.lbl_imgsz_warning.setStyleSheet(
            "color: #FFA500; font-size: 14px; font-weight: bold;")
        infsize_row.addWidget(self.lbl_imgsz_warning)
        infsize_row.addStretch()
        lay.addLayout(infsize_row)

        self.lbl_scale_info = QLabel("Scale: -- (connect source)")
        self.lbl_scale_info.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_scale_info.setWordWrap(True)
        lay.addWidget(self.lbl_scale_info)

        # Global detect-classes selector: drives YOLO inference class filter
        detect_row = QHBoxLayout()
        detect_row.addWidget(QLabel("Detect classes:"))
        self.detect_class_combo = CheckableComboBox()
        self.detect_class_combo.selection_changed.connect(
            lambda: self.detect_classes_changed.emit(
                self.detect_class_combo.get_selected_classes()))
        detect_row.addWidget(self.detect_class_combo, stretch=1)
        lay.addLayout(detect_row)

    # ─── Section 2: Line Crossing Alerts ───
    def _build_line_crossing_section(self):
        sec = self._add_section("Line Crossing Alerts")
        lay = sec.body_layout

        # Cooldown at top
        cd_row = QHBoxLayout()
        cd_row.addWidget(QLabel("Cooldown:"))
        self.spin_line_cooldown = arabic_spinbox()
        self.spin_line_cooldown.setRange(0, 3600)
        self.spin_line_cooldown.setValue(30)
        self.spin_line_cooldown.setSuffix(" sec")
        self.spin_line_cooldown.setToolTip("0 = no cooldown, send all events")
        cd_row.addWidget(self.spin_line_cooldown)
        cd_row.addStretch()
        lay.addLayout(cd_row)

        self.lbl_line_rules_empty = QLabel("Load a project to see lines")
        self.lbl_line_rules_empty.setStyleSheet("color: #888; font-size: 11px;")
        lay.addWidget(self.lbl_line_rules_empty)

        self._line_rules_container = QWidget()
        self._line_rules_layout = QVBoxLayout(self._line_rules_container)
        self._line_rules_layout.setContentsMargins(0, 0, 0, 0)
        self._line_rules_layout.setSpacing(4)
        lay.addWidget(self._line_rules_container)
        self._line_rules_container.setVisible(False)

    def populate_line_rules(self, lines: list):
        self._line_rule_widgets.clear()
        while self._line_rules_layout.count():
            item = self._line_rules_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not lines:
            self.lbl_line_rules_empty.setVisible(True)
            self._line_rules_container.setVisible(False)
            return
        self.lbl_line_rules_empty.setVisible(False)
        self._line_rules_container.setVisible(True)

        for line in lines:
            row_widget = QFrame()
            row_widget.setStyleSheet(
                "QFrame { background-color: #333; border-radius: 4px; padding: 4px; }"
            )
            row_layout = QVBoxLayout(row_widget)
            row_layout.setContentsMargins(6, 4, 6, 4)
            row_layout.setSpacing(3)

            top_row = QHBoxLayout()
            chk = QCheckBox(line.name)
            chk.setChecked(True)
            chk.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            chk.setStyleSheet(f"color: {line.color};")
            top_row.addWidget(chk)
            top_row.addStretch()
            row_layout.addLayout(top_row)

            bottom_row = QHBoxLayout()
            func_combo = QComboBox()
            func_combo.addItems(["Entrance", "Exit", "Bidirectional"])
            func_combo.setCurrentText("Bidirectional")
            func_combo.setFixedWidth(110)
            bottom_row.addWidget(func_combo)
            chk_in = QCheckBox("IN")
            chk_in.setChecked(True)
            bottom_row.addWidget(chk_in)
            chk_out = QCheckBox("OUT")
            chk_out.setChecked(True)
            bottom_row.addWidget(chk_out)
            bottom_row.addStretch()
            row_layout.addLayout(bottom_row)

            def _on_func_changed(text, ci=chk_in, co=chk_out):
                if text in ("Entrance", "Exit"):
                    ci.setVisible(True)
                    ci.setChecked(True)
                    co.setVisible(False)
                    co.setChecked(False)
                else:
                    ci.setVisible(True)
                    co.setVisible(True)
                    ci.setChecked(True)
                    co.setChecked(True)
            func_combo.currentTextChanged.connect(_on_func_changed)

            self._line_rules_layout.addWidget(row_widget)
            self._line_rule_widgets.append({
                "line_id": line.id, "line_name": line.name,
                "checkbox": chk, "function": func_combo,
                "notify_in": chk_in, "notify_out": chk_out,
            })

    # ─── Section 4: Zones & Area ───
    def _build_zones_area_section(self):
        sec = self._add_section("Zones & Area")
        lay = sec.body_layout

        # --- Area ---
        area_header = QLabel("-- Area (between entrance/exit lines) --")
        area_header.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        area_header.setStyleSheet("color: #E8740C;")
        lay.addWidget(area_header)

        self._area_widget = QFrame()
        self._area_widget.setStyleSheet(
            "QFrame { background-color: #333; border-radius: 4px; padding: 4px; }"
        )
        area_layout = QVBoxLayout(self._area_widget)
        area_layout.setContentsMargins(6, 4, 6, 4)
        area_layout.setSpacing(3)

        self.chk_area_overstay = QCheckBox("Area Overstay")
        self.chk_area_overstay.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        area_layout.addWidget(self.chk_area_overstay)

        # Overstay after
        r1 = QHBoxLayout()
        self._lbl_area_after = QLabel("Overstay after:")
        r1.addWidget(self._lbl_area_after)
        self.spin_area_threshold = arabic_spinbox()
        self.spin_area_threshold.setRange(0, 3600)
        self.spin_area_threshold.setValue(120)
        self.spin_area_threshold.setSuffix(" sec")
        r1.addWidget(self.spin_area_threshold)
        r1.addStretch()
        area_layout.addLayout(r1)

        # Reminder every
        r2 = QHBoxLayout()
        self._lbl_area_reminder = QLabel("Reminder every:")
        r2.addWidget(self._lbl_area_reminder)
        self.spin_area_reminder = arabic_spinbox()
        self.spin_area_reminder.setRange(0, 3600)
        self.spin_area_reminder.setValue(60)
        self.spin_area_reminder.setSuffix(" sec")
        self.spin_area_reminder.setToolTip("0 = send once, no repeat")
        r2.addWidget(self.spin_area_reminder)
        r2.addStretch()
        area_layout.addLayout(r2)

        # Gray out when unchecked
        def _on_area_toggled(checked):
            self.spin_area_threshold.setEnabled(checked)
            self._lbl_area_after.setEnabled(checked)
            self.spin_area_reminder.setEnabled(checked)
            self._lbl_area_reminder.setEnabled(checked)
            self.area_class_combo.setEnabled(checked)
        self.chk_area_overstay.toggled.connect(_on_area_toggled)

        # Class selector
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("For:"))
        self.area_class_combo = CheckableComboBox()
        r3.addWidget(self.area_class_combo)
        area_layout.addLayout(r3)

        # Summary labels
        self.lbl_entrance_lines = QLabel("Entrance lines: --")
        self.lbl_entrance_lines.setStyleSheet("color: #888; font-size: 10px;")
        self.lbl_entrance_lines.setWordWrap(True)
        area_layout.addWidget(self.lbl_entrance_lines)
        self.lbl_exit_lines = QLabel("Exit lines: --")
        self.lbl_exit_lines.setStyleSheet("color: #888; font-size: 10px;")
        self.lbl_exit_lines.setWordWrap(True)
        area_layout.addWidget(self.lbl_exit_lines)
        self.lbl_watched_zones = QLabel("Watched zones: --")
        self.lbl_watched_zones.setStyleSheet("color: #888; font-size: 10px;")
        self.lbl_watched_zones.setWordWrap(True)
        area_layout.addWidget(self.lbl_watched_zones)
        self.lbl_stuck_count = QLabel("Stuck objects: 0")
        self.lbl_stuck_count.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        area_layout.addWidget(self.lbl_stuck_count)

        lay.addWidget(self._area_widget)

        # --- Zones ---
        zone_header = QLabel("-- Zones --")
        zone_header.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        zone_header.setStyleSheet("color: #E8740C; margin-top: 6px;")
        lay.addWidget(zone_header)

        self.lbl_zones_empty = QLabel("Load a project to see zones")
        self.lbl_zones_empty.setStyleSheet("color: #888; font-size: 11px;")
        lay.addWidget(self.lbl_zones_empty)

        self._zones_container = QWidget()
        self._zones_layout = QVBoxLayout(self._zones_container)
        self._zones_layout.setContentsMargins(0, 0, 0, 0)
        self._zones_layout.setSpacing(4)
        lay.addWidget(self._zones_container)
        self._zones_container.setVisible(False)

    def set_available_classes(self, class_names: list[str]):
        self._available_classes = class_names
        self.detect_class_combo.add_classes(class_names)
        self.area_class_combo.add_classes(class_names)
        for w in self._zone_area_widgets:
            w["class_combo"].add_classes(class_names)

    def set_source_browse_visible(self, show: bool):
        self.btn_browse_video.setVisible(show)

    def populate_zones_area(self, zones: list):
        self._zone_area_widgets.clear()
        while self._zones_layout.count():
            item = self._zones_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not zones:
            self.lbl_zones_empty.setVisible(True)
            self._zones_container.setVisible(False)
            return
        self.lbl_zones_empty.setVisible(False)
        self._zones_container.setVisible(True)

        for zone in zones:
            row_widget = QFrame()
            row_widget.setStyleSheet(
                "QFrame { background-color: #333; border-radius: 4px; padding: 4px; }"
            )
            row_layout = QVBoxLayout(row_widget)
            row_layout.setContentsMargins(6, 4, 6, 4)
            row_layout.setSpacing(3)

            # Row 1: checkbox + name
            top_row = QHBoxLayout()
            chk = QCheckBox(zone.name)
            chk.setChecked(True)
            chk.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            chk.setStyleSheet(f"color: {zone.color};")
            top_row.addWidget(chk)
            top_row.addStretch()
            row_layout.addLayout(top_row)

            # Row 2: Enter + Exit + Overstay checkboxes
            event_row = QHBoxLayout()
            chk_enter = QCheckBox("Enter")
            chk_enter.setChecked(False)
            event_row.addWidget(chk_enter)
            chk_exit = QCheckBox("Exit")
            chk_exit.setChecked(False)
            event_row.addWidget(chk_exit)
            chk_overstay = QCheckBox("Overstay")
            chk_overstay.setChecked(True)
            event_row.addWidget(chk_overstay)
            event_row.addStretch()
            row_layout.addLayout(event_row)

            # Row 2b: Min dwell to register — debounce against boundary jitter
            # between adjacent zones. 0 = no debounce, fires immediately like
            # the original behavior. Value is mutated live on the Zone object
            # so changes take effect mid-detection AND persist on save.
            min_dwell_row = QHBoxLayout()
            lbl_min_dwell = QLabel("Min dwell to register:")
            min_dwell_row.addWidget(lbl_min_dwell)
            spin_min_dwell = arabic_double_spinbox()
            spin_min_dwell.setRange(0.0, 60.0)
            spin_min_dwell.setSingleStep(0.1)
            spin_min_dwell.setDecimals(1)
            spin_min_dwell.setSuffix(" sec")
            spin_min_dwell.setValue(float(getattr(zone, "min_inside_seconds", 0.0)))
            spin_min_dwell.setToolTip(
                "Object must remain continuously inside for this many seconds\n"
                "before zone_enter fires (and continuously outside before\n"
                "zone_exit fires). Helps suppress boundary jitter between\n"
                "adjacent zones. 0 = no debounce.")
            # Capture zone by default-arg so each closure binds its own zone
            # rather than the loop variable. Mutates the live Zone object so
            # the runner's zone_manager sees the change on the very next frame.
            spin_min_dwell.valueChanged.connect(
                lambda v, z=zone: setattr(z, "min_inside_seconds", float(v)))
            min_dwell_row.addWidget(spin_min_dwell)
            min_dwell_row.addStretch()
            row_layout.addLayout(min_dwell_row)

            # Row 3: Overstay after
            r_after = QHBoxLayout()
            lbl_after = QLabel("Overstay after:")
            r_after.addWidget(lbl_after)
            spin_after = arabic_spinbox()
            spin_after.setRange(0, 36000)
            spin_after.setValue(300)
            spin_after.setSuffix(" sec")
            r_after.addWidget(spin_after)
            r_after.addStretch()
            row_layout.addLayout(r_after)

            # Row 4: Enter/Exit cooldown (shared)
            r_enter_cd = QHBoxLayout()
            lbl_enter_cd = QLabel("Enter/Exit cooldown:")
            r_enter_cd.addWidget(lbl_enter_cd)
            spin_enter_cd = arabic_spinbox()
            spin_enter_cd.setRange(0, 3600)
            spin_enter_cd.setValue(0)
            spin_enter_cd.setSuffix(" sec")
            spin_enter_cd.setToolTip("0 = no cooldown, send all enter/exit events")
            r_enter_cd.addWidget(spin_enter_cd)
            r_enter_cd.addStretch()
            row_layout.addLayout(r_enter_cd)

            # Row 5: Overstay reminder
            r_reminder = QHBoxLayout()
            lbl_reminder = QLabel("Reminder every:")
            r_reminder.addWidget(lbl_reminder)
            spin_reminder = arabic_spinbox()
            spin_reminder.setRange(0, 3600)
            spin_reminder.setValue(300)
            spin_reminder.setSuffix(" sec")
            spin_reminder.setToolTip("0 = send once, no repeat")
            r_reminder.addWidget(spin_reminder)
            r_reminder.addStretch()
            row_layout.addLayout(r_reminder)

            # Gray out based on checkboxes
            def _on_enter_exit_toggled(_checked=None, ce=chk_enter, cx=chk_exit,
                                        lcd=lbl_enter_cd, scd=spin_enter_cd):
                active = ce.isChecked() or cx.isChecked()
                lcd.setEnabled(active)
                scd.setEnabled(active)

            def _on_overstay_toggled(checked, la=lbl_after, sa=spin_after,
                                      lr=lbl_reminder, sr=spin_reminder):
                la.setEnabled(checked)
                sa.setEnabled(checked)
                lr.setEnabled(checked)
                sr.setEnabled(checked)

            chk_enter.toggled.connect(_on_enter_exit_toggled)
            chk_exit.toggled.connect(_on_enter_exit_toggled)
            chk_overstay.toggled.connect(_on_overstay_toggled)
            # Init state
            _on_enter_exit_toggled()

            # Row 6: class selector
            class_row = QHBoxLayout()
            class_row.addWidget(QLabel("For:"))
            class_combo = CheckableComboBox()
            if self._available_classes:
                class_combo.add_classes(self._available_classes)
            class_row.addWidget(class_combo)
            row_layout.addLayout(class_row)

            self._zones_layout.addWidget(row_widget)
            self._zone_area_widgets.append({
                "zone_id": zone.id, "zone_name": zone.name,
                "checkbox": chk,
                "chk_enter": chk_enter, "chk_exit": chk_exit, "chk_overstay": chk_overstay,
                "max_seconds": spin_after,
                "enter_cooldown": spin_enter_cd,
                "overstay_reminder": spin_reminder,
                "class_combo": class_combo,
            })

    # ─── Bottom Block ───
    def _build_bottom_block(self, parent_container):
        """parent_container: accepts either a QLayout (addLayout) or QSplitter/QWidget (addWidget)."""
        bottom = QFrame()
        bottom.setMinimumHeight(180)
        bottom.setStyleSheet("QFrame { border-top: 1px solid #555; }")
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(10, 8, 10, 8)
        bottom_layout.setSpacing(4)

        ctrl_row = QHBoxLayout()
        self.btn_connect = styled_button("\u25b6  Connect")
        self.btn_connect.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.btn_connect.setFixedHeight(34)
        ctrl_row.addWidget(self.btn_connect)
        self.btn_reset_all = styled_button("Reset All")
        self.btn_reset_all.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.btn_reset_all.setFixedHeight(34)
        self.btn_reset_all.setStyleSheet("""
            QPushButton { background-color: #666; color: white; border: none;
                border-radius: 4px; padding: 6px 12px; font-weight: bold; }
            QPushButton:hover { background-color: #555; }
        """)
        ctrl_row.addWidget(self.btn_reset_all)
        bottom_layout.addLayout(ctrl_row)

        btn_row = QHBoxLayout()
        self.btn_test = styled_button("\u25b6  Test")
        self.btn_test.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.btn_test.setFixedHeight(38)
        btn_row.addWidget(self.btn_test)
        self.btn_start = styled_button("\u25b6  Start")
        self.btn_start.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.btn_start.setFixedHeight(38)
        self.btn_start.setStyleSheet("""
            QPushButton { background-color: #2E8B57; color: white; border: none;
                border-radius: 4px; padding: 6px 12px; font-weight: bold; }
            QPushButton:hover { background-color: #256B45; }
        """)
        btn_row.addWidget(self.btn_start)
        bottom_layout.addLayout(btn_row)

        options_row = QHBoxLayout()
        self.chk_show_live = QCheckBox("Show live view")
        self.chk_show_live.setChecked(True)
        self.chk_show_live.stateChanged.connect(
            lambda s: self.show_live_changed.emit(s == Qt.CheckState.Checked.value)
        )
        options_row.addWidget(self.chk_show_live)
        self.chk_show_detections = QCheckBox("Detections")
        self.chk_show_detections.setChecked(True)
        self.chk_show_detections.stateChanged.connect(
            lambda s: self.show_detections_changed.emit(s == Qt.CheckState.Checked.value)
        )
        options_row.addWidget(self.chk_show_detections)
        self.chk_show_labels = QCheckBox("Labels")
        self.chk_show_labels.setChecked(True)
        self.chk_show_labels.stateChanged.connect(
            lambda s: self.show_labels_changed.emit(s == Qt.CheckState.Checked.value)
        )
        options_row.addWidget(self.chk_show_labels)
        options_row.addStretch()
        self.combo_draw_mode = QComboBox()
        self.combo_draw_mode.addItems(["Dot", "Box"])
        self.combo_draw_mode.currentTextChanged.connect(self.draw_mode_changed.emit)
        options_row.addWidget(self.combo_draw_mode)
        bottom_layout.addLayout(options_row)

        filter_row = QHBoxLayout()
        self.chk_hide_nonnoti = QCheckBox("Hide non-notified events")
        self.chk_hide_nonnoti.setChecked(False)
        self.chk_hide_nonnoti.setToolTip(
            "Hide gray log entries (events that were not sent as a notification)")
        self.chk_hide_nonnoti.stateChanged.connect(self._apply_event_filter)
        filter_row.addWidget(self.chk_hide_nonnoti)
        filter_row.addStretch()
        bottom_layout.addLayout(filter_row)

        self._events_expanded = True
        events_header = QFrame()
        events_header.setCursor(Qt.CursorShape.PointingHandCursor)
        eh_layout = QHBoxLayout(events_header)
        eh_layout.setContentsMargins(0, 4, 0, 2)
        self._events_chevron = QLabel("\u25bc")
        self._events_chevron.setFont(QFont("Segoe UI", 10))
        self._events_chevron.setFixedWidth(16)
        eh_layout.addWidget(self._events_chevron)
        lbl_events = QLabel("Event Log")
        lbl_events.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        eh_layout.addWidget(lbl_events)
        eh_layout.addStretch()
        self._events_count_label = QLabel("(0)")
        self._events_count_label.setStyleSheet("color: #888; font-size: 10px;")
        eh_layout.addWidget(self._events_count_label)
        events_header.mousePressEvent = lambda e: self._toggle_events_log()
        bottom_layout.addWidget(events_header)

        self.events_list = QListWidget()
        self.events_list.setMinimumHeight(80)
        self.events_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.events_list.setFont(QFont("Consolas", 9))
        self.events_list.setStyleSheet("""
            QListWidget { background-color: #1e1e1e; color: #00ff88;
                border: 2px solid #E8740C; border-radius: 6px; }
        """)
        bottom_layout.addWidget(self.events_list, stretch=1)

        clear_row = QHBoxLayout()
        clear_row.addStretch()
        # Export pulls the full structured buffer (not just what's visible) —
        # see SingleTab._on_export_events_requested.
        self.btn_export_events = QPushButton("Export…")
        self.btn_export_events.setFixedHeight(24)
        self.btn_export_events.setToolTip(
            "Export the full event buffer (since project load) as CSV files "
            "in a folder.")
        self.btn_export_events.setStyleSheet("""
            QPushButton { background-color: #3A7CA5; color: white; border: none;
                border-radius: 4px; padding: 2px 12px; font-size: 11px; }
            QPushButton:hover { background-color: #2E6585; }
            QPushButton:disabled { background-color: #555; color: #aaa; }
        """)
        self.btn_export_events.clicked.connect(self.export_events_requested.emit)
        clear_row.addWidget(self.btn_export_events)
        self.btn_clear_events = QPushButton("Clear")
        self.btn_clear_events.setFixedHeight(24)
        self.btn_clear_events.setStyleSheet("""
            QPushButton { background-color: #555; color: white; border: none;
                border-radius: 4px; padding: 2px 12px; font-size: 11px; }
            QPushButton:hover { background-color: #666; }
        """)
        self.btn_clear_events.clicked.connect(self.clear_event_log)
        clear_row.addWidget(self.btn_clear_events)
        bottom_layout.addLayout(clear_row)

        self.lbl_status = QLabel("Ready -- load a project config to begin")
        self.lbl_status.setStyleSheet("color: #888; font-size: 10px;")
        self.lbl_status.setWordWrap(True)
        bottom_layout.addWidget(self.lbl_status)
        parent_container.addWidget(bottom)

    def _toggle_events_log(self):
        self._events_expanded = not self._events_expanded
        self.events_list.setVisible(self._events_expanded)
        self._events_chevron.setText("\u25bc" if self._events_expanded else "\u25b6")

    def _toggle_all_sections(self):
        any_expanded = any(s.expanded for s in self._sections)
        if any_expanded:
            self._prev_section_state = {i: s.expanded for i, s in enumerate(self._sections)}
            self._accordion_mode = False
            for s in self._sections:
                s.collapse()
            self._accordion_mode = True
        else:
            self._accordion_mode = False
            prev = self._prev_section_state or {i: True for i in range(len(self._sections))}
            for i, s in enumerate(self._sections):
                if prev.get(i, True):
                    s.expand()
            self._accordion_mode = True

    # ─── Getters ───

    # Color used for non-notified log entries (see main_window._log_event)
    _GRAY_NONNOTI = "#888888"

    def add_event(self, text: str, color: str = "#00ff88"):
        item = QListWidgetItem(text)
        item.setForeground(QColor(color))
        item.setData(Qt.ItemDataRole.UserRole, color)
        self.events_list.insertItem(0, item)
        if self.chk_hide_nonnoti.isChecked() and color == self._GRAY_NONNOTI:
            item.setHidden(True)
        while self.events_list.count() > 200:
            self.events_list.takeItem(self.events_list.count() - 1)
        self._events_count_label.setText(f"({self.events_list.count()})")

    def add_noti_result(self, text: str, success: bool):
        color = "#00cc66" if success else "#cc3333"
        item = QListWidgetItem(f"  \u2192 {text}")
        item.setForeground(QColor(color))
        item.setData(Qt.ItemDataRole.UserRole, color)
        self.events_list.insertItem(1, item)
        self._events_count_label.setText(f"({self.events_list.count()})")

    def clear_event_log(self):
        self.events_list.clear()
        self._events_count_label.setText("(0)")

    def _apply_event_filter(self, *_):
        hide_gray = self.chk_hide_nonnoti.isChecked()
        for i in range(self.events_list.count()):
            it = self.events_list.item(i)
            color = it.data(Qt.ItemDataRole.UserRole)
            it.setHidden(bool(hide_gray and color == self._GRAY_NONNOTI))

    def set_inference_size(self, imgsz: int):
        """Set the imgsz combo without firing imgsz_changed (used during load)."""
        text = str(int(imgsz))
        self.combo_imgsz.blockSignals(True)
        if self.combo_imgsz.findText(text) < 0:
            self.combo_imgsz.addItem(text)
        self.combo_imgsz.setCurrentText(text)
        self.combo_imgsz.blockSignals(False)

    def update_scale_info(self, src_w: int, src_h: int, imgsz: int):
        """Refresh the "Feed W×H → infW×infH (scale)" label and the warning
        glyph next to the imgsz combo. Threshold for the warning is 0.4 —
        below that, small/distant objects are very likely to fall under YOLO's
        ~16px detection floor after letterboxing."""
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

    def set_project_info(self, project_name: str, model_name: str, device: str,
                         source_type: str, source_value: str,
                         zone_count: int, line_count: int):
        self.lbl_project_name.setText(f"Project: {project_name}")
        self.lbl_project_name.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        self.lbl_model_info.setText(f"Model: {model_name}")
        self.lbl_device_info.setText(f"Device: {device}")
        self.lbl_source_info.setText(f"Source: {source_type} | {source_value}")
        self.lbl_zones_lines_info.setText(f"Zones: {zone_count}  |  Lines: {line_count}")

    def get_line_cooldown(self) -> int:
        return self.spin_line_cooldown.value()

    def get_line_rules(self) -> list[dict]:
        return [{
            "line_id": w["line_id"], "line_name": w["line_name"],
            "function": w["function"].currentText().lower(),
            "enabled": w["checkbox"].isChecked(),
            "notify_in": w["notify_in"].isChecked(),
            "notify_out": w["notify_out"].isChecked(),
        } for w in self._line_rule_widgets]

    def get_area_config(self) -> dict:
        return {
            "enabled": self.chk_area_overstay.isChecked(),
            "threshold_seconds": self.spin_area_threshold.value(),
            "reminder_seconds": self.spin_area_reminder.value(),
            "target_classes": self.area_class_combo.get_selected_classes(),
        }

    def get_zone_rules(self) -> list[dict]:
        return [{
            "zone_id": w["zone_id"], "zone_name": w["zone_name"],
            "enabled": w["checkbox"].isChecked(),
            "notify_enter": w["chk_enter"].isChecked(),
            "notify_exit": w["chk_exit"].isChecked(),
            "notify_overstay": w["chk_overstay"].isChecked(),
            "max_seconds": w["max_seconds"].value(),
            "enter_cooldown": w["enter_cooldown"].value(),
            "overstay_reminder": w["overstay_reminder"].value(),
            "target_classes": w["class_combo"].get_selected_classes(),
        } for w in self._zone_area_widgets]

    # ─── Hydration setters (called after populate_*) ───
    def set_project_loaded(self, loaded: bool):
        self._project_loaded = loaded
        self.btn_save_project.setEnabled(loaded)

    def set_line_cooldown(self, seconds: int):
        self.spin_line_cooldown.setValue(int(seconds))

    def apply_line_rules(self, rules: list):
        """Apply saved per-line rules. Each rule is a dict or LineNotiRule with
        line_id; widgets without a matching saved rule keep their defaults."""
        by_id = {}
        for r in rules:
            rd = r if isinstance(r, dict) else asdict(r)
            by_id[rd.get("line_id")] = rd
        func_label = {"entrance": "Entrance", "exit": "Exit",
                      "bidirectional": "Bidirectional"}
        for w in self._line_rule_widgets:
            rd = by_id.get(w["line_id"])
            if rd is None:
                continue
            w["checkbox"].setChecked(bool(rd.get("enabled", True)))
            w["function"].setCurrentText(
                func_label.get(rd.get("function", "bidirectional"), "Bidirectional"))
            w["notify_in"].setChecked(bool(rd.get("notify_in", True)))
            w["notify_out"].setChecked(bool(rd.get("notify_out", True)))

    def set_area_config(self, cfg):
        cd = cfg if isinstance(cfg, dict) else asdict(cfg)
        self.chk_area_overstay.setChecked(bool(cd.get("enabled", False)))
        self.spin_area_threshold.setValue(int(cd.get("threshold_seconds", 120)))
        self.spin_area_reminder.setValue(int(cd.get("reminder_seconds", 60)))
        if self._available_classes:
            self.area_class_combo.set_selected_classes(cd.get("target_classes", []) or [])

    def apply_zone_rules(self, rules: list):
        """Apply saved per-zone rules; zones without a saved rule keep defaults."""
        by_id = {}
        for r in rules:
            rd = r if isinstance(r, dict) else asdict(r)
            by_id[rd.get("zone_id")] = rd
        for w in self._zone_area_widgets:
            rd = by_id.get(w["zone_id"])
            if rd is None:
                continue
            w["checkbox"].setChecked(bool(rd.get("enabled", True)))
            w["chk_enter"].setChecked(bool(rd.get("notify_enter", False)))
            w["chk_exit"].setChecked(bool(rd.get("notify_exit", False)))
            w["chk_overstay"].setChecked(bool(rd.get("notify_overstay", True)))
            w["max_seconds"].setValue(int(rd.get("max_seconds", 300)))
            w["enter_cooldown"].setValue(int(rd.get("enter_cooldown", 0)))
            w["overstay_reminder"].setValue(int(rd.get("overstay_reminder", 300)))
            if self._available_classes:
                w["class_combo"].set_selected_classes(rd.get("target_classes", []) or [])

    def update_stuck_count(self, count: int, obj_ids: list[int] = None):
        if count == 0:
            self.lbl_stuck_count.setText("Stuck objects: 0")
            self.lbl_stuck_count.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        else:
            ids_str = ", ".join(f"#{oid}" for oid in (obj_ids or []))
            self.lbl_stuck_count.setText(f"Stuck objects: {count} ({ids_str})")
            self.lbl_stuck_count.setStyleSheet("color: #ff4444; font-size: 11px; font-weight: bold;")

    def update_entrance_exit_summary(self, entrance_lines: list[str],
                                      exit_lines: list[str], zone_names: list[str]):
        self.lbl_entrance_lines.setText(
            f"Entrance lines: {', '.join(entrance_lines)}" if entrance_lines
            else "Entrance lines: (none assigned)")
        self.lbl_exit_lines.setText(
            f"Exit lines: {', '.join(exit_lines)}" if exit_lines
            else "Exit lines: (none assigned)")
        self.lbl_watched_zones.setText(
            f"Watched zones: {', '.join(zone_names)}" if zone_names
            else "Watched zones: (none)")

    def set_connected(self, connected: bool, info: str = ""):
        if connected:
            self.btn_connect.setText("\u25a0  Disconnect")
            self.btn_connect.setStyleSheet("""
                QPushButton { background-color: #CC3333; color: white; border: none;
                    border-radius: 4px; padding: 6px 12px; font-weight: bold; }
                QPushButton:hover { background-color: #AA2222; }
            """)
            self.lbl_source_info.setStyleSheet("color: #00cc66; font-size: 11px;")
        else:
            self.btn_connect.setText("\u25b6  Connect")
            self.btn_connect.setStyleSheet(f"""
                QPushButton {{ background-color: {ACCENT}; color: white; border: none;
                    border-radius: 4px; padding: 6px 12px; font-weight: bold; }}
                QPushButton:hover {{ background-color: {ACCENT_HOVER}; }}
            """)
            self.lbl_source_info.setStyleSheet("color: gray; font-size: 11px;")

    def set_running(self, running: bool, mode: str = ""):
        if running:
            self.btn_connect.setEnabled(False)
            self.btn_load_project.setEnabled(False)
            self.btn_save_project.setEnabled(False)
            if mode == "test":
                self.btn_test.setText("\u25a0  Stop Test")
                self.btn_test.setStyleSheet("""
                    QPushButton { background-color: #CC3333; color: white; border: none;
                        border-radius: 4px; padding: 6px 12px; font-weight: bold; }
                    QPushButton:hover { background-color: #AA2222; }
                """)
                self.btn_start.setEnabled(False)
            elif mode == "start":
                self.btn_start.setText("\u25a0  Stop")
                self.btn_start.setStyleSheet("""
                    QPushButton { background-color: #CC3333; color: white; border: none;
                        border-radius: 4px; padding: 6px 12px; font-weight: bold; }
                    QPushButton:hover { background-color: #AA2222; }
                """)
                self.btn_test.setEnabled(False)
        else:
            self.btn_connect.setEnabled(True)
            self.btn_load_project.setEnabled(True)
            self.btn_save_project.setEnabled(self._project_loaded)
            self.btn_test.setText("\u25b6  Test")
            self.btn_test.setStyleSheet(f"""
                QPushButton {{ background-color: {ACCENT}; color: white; border: none;
                    border-radius: 4px; padding: 6px 12px; font-weight: bold; }}
                QPushButton:hover {{ background-color: {ACCENT_HOVER}; }}
            """)
            self.btn_start.setText("\u25b6  Start")
            self.btn_start.setStyleSheet("""
                QPushButton { background-color: #2E8B57; color: white; border: none;
                    border-radius: 4px; padding: 6px 12px; font-weight: bold; }
                QPushButton:hover { background-color: #256B45; }
            """)
            self.btn_test.setEnabled(True)
            self.btn_start.setEnabled(True)
