"""Application shell — owns the menu bar, status bar, and a tab widget that
hosts SingleTab + FleetTab. All pipeline-specific state and behavior live in
the tabs themselves.
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow, QStatusBar, QTabWidget, QMessageBox,
)
from ui.single_tab import SingleTab
from ui.fleet_tab import FleetTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Baksters Notification Runner")
        self.setMinimumSize(1100, 600)
        self.showMaximized()

        self._build_central()
        self._build_menu()
        self._build_status_bar()
        self._wire()

        # Track current tab for the switch-confirm logic. We can't read it from
        # the QTabWidget mid-callback because currentChanged fires AFTER the
        # change is committed — we need the "from" index.
        self._current_index = self._tabs.currentIndex()

    # ───────── build ─────────

    def _build_central(self):
        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._single_tab = SingleTab()
        self._fleet_tab = FleetTab()
        self._tabs.addTab(self._single_tab, "Single Project")
        self._tabs.addTab(self._fleet_tab, "Fleet")
        self.setCentralWidget(self._tabs)

    def _build_menu(self):
        menu = self.menuBar()
        file_menu = menu.addMenu("File")
        load = QAction("Load Project", self)
        load.setShortcut(QKeySequence("Ctrl+O"))
        load.triggered.connect(self._on_menu_load)
        file_menu.addAction(load)
        save = QAction("Save Project", self)
        save.setShortcut(QKeySequence("Ctrl+S"))
        save.triggered.connect(self._on_menu_save)
        file_menu.addAction(save)
        save_as = QAction("Save Project As…", self)
        save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save_as.triggered.connect(self._on_menu_save_as)
        file_menu.addAction(save_as)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.setShortcut(QKeySequence("Alt+F4"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def _build_status_bar(self):
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

    def _wire(self):
        self._single_tab.status_text.connect(self._status_bar.showMessage)
        self._fleet_tab.status_text.connect(self._status_bar.showMessage)
        self._tabs.currentChanged.connect(self._on_tab_changed)

    # ───────── menu actions ─────────
    # Project I/O always targets Single tab — Fleet has no per-tab project.
    # When the user is on the Fleet tab, the menu silently switches to Single
    # and applies the action; that matches user intent (you're editing a
    # project, not the fleet itself).

    @Slot()
    def _on_menu_load(self):
        if self._tabs.currentWidget() is not self._single_tab:
            self._tabs.setCurrentWidget(self._single_tab)
        self._single_tab.load_project_dialog()

    @Slot()
    def _on_menu_save(self):
        if self._tabs.currentWidget() is not self._single_tab:
            self._tabs.setCurrentWidget(self._single_tab)
        self._single_tab.save_project(False)

    @Slot()
    def _on_menu_save_as(self):
        if self._tabs.currentWidget() is not self._single_tab:
            self._tabs.setCurrentWidget(self._single_tab)
        self._single_tab.save_project(True)

    # ───────── tab-switch confirm ─────────

    @Slot(int)
    def _on_tab_changed(self, new_index: int):
        if new_index == self._current_index:
            return  # programmatic / no-op

        # The tab the user is leaving (the "from" tab) is the one whose state
        # we need to check. We deliberately stored the previous index because
        # currentChanged fires post-commit.
        from_tab = self._tabs.widget(self._current_index)

        # If leaving Fleet while in full-screen on a camera, drop out of
        # full-screen first (user's intent is to leave the tab anyway).
        if from_tab is self._fleet_tab and self._fleet_tab.is_full_screen():
            self._fleet_tab.exit_full_screen()

        is_running = False
        from_label = ""
        if from_tab is self._single_tab:
            is_running = self._single_tab.is_running()
            from_label = "Single"
        elif from_tab is self._fleet_tab:
            is_running = self._fleet_tab.is_any_running()
            from_label = "Fleet"

        if is_running:
            answer = QMessageBox.question(
                self, "Stop running pipeline?",
                f"Stop the running {from_label} pipeline before switching tabs?\n"
                "Running detection will be terminated.",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                # Revert the tab change without re-firing this handler.
                self._tabs.blockSignals(True)
                self._tabs.setCurrentIndex(self._current_index)
                self._tabs.blockSignals(False)
                return
            # User confirmed — stop the running work in the from-tab.
            if from_tab is self._single_tab:
                self._single_tab.stop_running()
            elif from_tab is self._fleet_tab:
                self._fleet_tab.stop_all()

        self._current_index = new_index

    # ───────── close ─────────

    def closeEvent(self, event):
        self._single_tab.shutdown()
        self._fleet_tab.shutdown()
        event.accept()
