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

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    # Set org/app name so QSettings is consistent across the app
    # (used by the update checker's 24h cache + dismissed-versions list).
    app.setOrganizationName("Baksters")
    app.setApplicationName("MNT")
    app.setStyle("Fusion")

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

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
