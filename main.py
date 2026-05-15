import sys
import os
import faulthandler

# Install Python's faulthandler before anything else. Two purposes:
#   1. If the app ever does crash with SIGSEGV/SIGBUS/SIGFPE in the field,
#      we get a Python stack trace in the terminal/log instead of a silent
#      core dump.
#   2. Faulthandler installs its signal handlers via sigaltstack, which on
#      some PySide6 + conda combos avoids a transient SIGBUS during the
#      first cross-thread QThread.start() — observed during Part 1 testing.
faulthandler.enable()

# Add project root to path (source-only — PyInstaller sets sys.path itself).
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtCore import Qt
from core.license import LicenseManager, LicenseState
from ui.license_dialog import ActivationDialog
from ui.main_window import MainWindow


def _install_uncaught_exception_hook():
    """PySide6's default slot handler silently swallows exceptions
    raised inside Qt callbacks — that's how the v1.0.0/v1.0.1 paths
    bug (mkdir on read-only FS) presented as "click does nothing".
    Install a process-wide hook that turns any unhandled exception
    into a visible error dialog instead. Logging to stderr still
    happens; this just adds a user-visible surface."""
    import traceback
    from PySide6.QtWidgets import QMessageBox

    def _hook(exc_type, exc_value, tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, tb))
        sys.__excepthook__(exc_type, exc_value, tb)  # still log to stderr
        try:
            QMessageBox.critical(None, "Internal error", text)
        except Exception:
            # If the GUI itself is dying we can't pop a dialog; the
            # stderr trace above is the fallback.
            pass

    sys.excepthook = _hook


def main():
    app = QApplication(sys.argv)
    # Set org/app name so QSettings is consistent across the app
    # (used by the update checker's 24h cache + dismissed-versions list).
    app.setOrganizationName("Baksters")
    app.setApplicationName("MNT")
    app.setStyle("Fusion")
    _install_uncaught_exception_hook()

    # Apply dark theme
    app.setStyleSheet("""
        QMainWindow, QWidget {
            background-color: #2b2b2b;
            color: #d4d4d4;
        }
        QLineEdit, QComboBox, QSpinBox {
            background-color: #3c3c3c;
            color: #d4d4d4;
            border: 1px solid #555;
            border-radius: 3px;
            padding: 4px;
        }
        QSlider::groove:horizontal {
            height: 6px;
            background: #555;
            border-radius: 3px;
        }
        QSlider::handle:horizontal {
            background: #E8740C;
            width: 16px;
            height: 16px;
            margin: -5px 0;
            border-radius: 8px;
        }
        QRadioButton, QCheckBox { color: #d4d4d4; }
        QLabel { color: #d4d4d4; }
        QStatusBar { background-color: #252525; color: #999; }
        QMenuBar { background-color: #2b2b2b; color: #d4d4d4; }
        QMenuBar::item:selected { background-color: #3c3c3c; }
        QMenu { background-color: #2b2b2b; color: #d4d4d4; }
        QMenu::item:selected { background-color: #E8740C; }
        QListWidget {
            background-color: #1e1e1e;
            color: #d4d4d4;
            border: 1px solid #555;
        }
        QScrollBar:vertical {
            background: #2b2b2b;
            width: 10px;
        }
        QScrollBar::handle:vertical {
            background: #555;
            border-radius: 5px;
            min-height: 20px;
        }
        QGroupBox {
            border: 1px solid #555;
            border-radius: 4px;
            margin-top: 8px;
            padding-top: 8px;
            color: #d4d4d4;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }
    """)

    # License gate. UNLICENSED or EXPIRED → show activation dialog; if
    # the user can't / won't activate, exit cleanly. ACTIVE / OFFLINE_GRACE
    # → proceed straight to the main window. REVOKED → show a clear
    # "contact support" message and exit.
    license_mgr = LicenseManager()
    if license_mgr.state == LicenseState.REVOKED:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(
            None, "License revoked",
            "This license has been revoked. Please contact support.")
        sys.exit(1)
    if license_mgr.state in (LicenseState.UNLICENSED, LicenseState.EXPIRED):
        dlg = ActivationDialog(license_mgr)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)

    window = MainWindow(license_mgr)
    window.show()

    # Once the window is visible, kick off a background revalidate. We
    # do this after show() so the user isn't waiting on the network
    # before seeing UI.
    if license_mgr.state in (LicenseState.ACTIVE, LicenseState.OFFLINE_GRACE):
        license_mgr.revalidate_async()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
