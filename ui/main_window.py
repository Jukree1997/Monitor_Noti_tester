"""Application shell — owns the menu bar, status bar, and a tab widget that
hosts SingleTab + FleetTab + ProjectEditorTab. All pipeline-specific state
and behavior live in the tabs themselves.
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow, QStatusBar, QTabWidget, QMessageBox,
)
from ui.single_tab import SingleTab
from ui.fleet_tab import FleetTab
from ui.project_editor_tab import ProjectEditorTab


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
        self._editor_tab = ProjectEditorTab()
        self._tabs.addTab(self._single_tab, "Single Project")
        self._tabs.addTab(self._fleet_tab, "Fleet")
        self._tabs.addTab(self._editor_tab, "Project Editor")
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
        self._editor_tab.status_text.connect(self._status_bar.showMessage)
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # GPU mutual-exclusion: the editor tab calls this before starting
        # its own DetectionEngine so we never have two CUDA sessions
        # fighting over the same model in the same process.
        self._editor_tab.can_start_detection_cb = (
            lambda: not (self._single_tab.is_running()
                         or self._fleet_tab.is_any_running()))

    # ───────── menu actions ─────────
    # Project I/O routes to whichever project-aware tab is active:
    #   - Single tab: the runtime project that gets run.
    #   - Project Editor tab: same project JSON shape, just opened for
    #     editing without inference.
    # Fleet has no per-tab project, so the menu silently switches to
    # Single when invoked from Fleet — matches user intent (you're
    # editing/loading a project, not the fleet itself).

    def _project_tab(self):
        """Return the currently-active project-aware tab — Single or
        Editor. Falls back to Single for Fleet (no per-tab project),
        switching to the Single tab on the way."""
        current = self._tabs.currentWidget()
        if current is self._editor_tab:
            return self._editor_tab
        if current is not self._single_tab:
            self._tabs.setCurrentWidget(self._single_tab)
        return self._single_tab

    @Slot()
    def _on_menu_load(self):
        self._project_tab().load_project_dialog()

    @Slot()
    def _on_menu_save(self):
        self._project_tab().save_project(False)

    @Slot()
    def _on_menu_save_as(self):
        self._project_tab().save_project(True)

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
        elif from_tab is self._editor_tab:
            is_running = self._editor_tab.is_running()
            from_label = "Editor preview"

        if is_running:
            answer = QMessageBox.question(
                self, "Stop running pipeline?",
                f"Stop the running {from_label} before switching tabs?\n"
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
            elif from_tab is self._editor_tab:
                self._editor_tab.stop_running()

        self._current_index = new_index

    # ───────── close ─────────

    def closeEvent(self, event):
        self._single_tab.shutdown()
        self._fleet_tab.shutdown()
        self._editor_tab.shutdown()
        event.accept()
