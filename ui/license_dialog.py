"""License-related dialogs.

Two top-level widgets:
  - ActivationDialog: shown when LicenseManager.state == UNLICENSED.
    Customer pastes their license key + clicks Activate.
  - LicenseInfoDialog: shown from Help → License Info. Displays tier,
    cap, expiry; offers Deactivate this PC and Refresh now.

Both are modal QDialogs styled to fit the rest of the dark-themed UI.
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QHBoxLayout, QMessageBox, QFrame, QSizePolicy,
)

from core.fingerprint import get_fingerprint
from core.license import LicenseManager, LicenseState
from core.version import __product_name__


# ======================================
# -------- 0. ACTIVATION DIALOG --------
# ======================================

class ActivationDialog(QDialog):
    """First-run / re-activate flow. Modal, blocks app launch.

    Returns QDialog.Accepted on successful activation, Rejected if user
    closes/cancels. main.py exits if Rejected so the unlicensed app
    never runs.
    """

    def __init__(self, license_mgr: LicenseManager, parent=None):
        super().__init__(parent)
        self._mgr = license_mgr
        self.setWindowTitle(f"Activate {__product_name__}")
        self.setMinimumWidth(520)
        self._build()
        self._wire()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Header
        title = QLabel(f"<h3>Activate {__product_name__}</h3>")
        title.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(title)

        intro = QLabel(
            "Paste your license key below to activate this PC. The key was "
            "emailed to you when you purchased a license."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Key input
        key_label = QLabel("License key:")
        layout.addWidget(key_label)
        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText(
            "ABCD-EFGH-... or key/eyJ... (paste the entire string)")
        # Monospace so long signed keys are readable
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._key_input.setFont(mono)
        layout.addWidget(self._key_input)

        # Fingerprint preview (so customer can quote it on support tickets)
        fp = get_fingerprint()
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        fp_label = QLabel(
            f"<small>This PC's fingerprint (for support): "
            f"<code>{fp[:16]}…{fp[-8:]}</code></small>"
        )
        fp_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(fp_label)

        # Status line (populated on error)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #E0625F;")
        layout.addWidget(self._status)

        # Buttons
        btn_row = QHBoxLayout()
        self._buy_btn = QPushButton("Buy a license →")
        self._buy_btn.setFlat(True)
        self._buy_btn.setStyleSheet("color: #3A7CA5;")
        btn_row.addWidget(self._buy_btn)
        btn_row.addStretch(1)
        self._cancel_btn = QPushButton("Cancel")
        btn_row.addWidget(self._cancel_btn)
        self._activate_btn = QPushButton("Activate")
        self._activate_btn.setDefault(True)
        self._activate_btn.setStyleSheet(
            "background-color: #1F7A8C; color: white; padding: 6px 14px;")
        btn_row.addWidget(self._activate_btn)
        layout.addLayout(btn_row)

    def _wire(self):
        self._cancel_btn.clicked.connect(self.reject)
        self._activate_btn.clicked.connect(self._on_activate_clicked)
        self._buy_btn.clicked.connect(self._on_buy_clicked)
        # Surface validation_failed inline rather than as a popup so
        # multi-attempt flow is smooth.
        self._mgr.validation_failed.connect(self._on_validation_failed)

    @Slot()
    def _on_activate_clicked(self):
        self._status.setText("")
        self._activate_btn.setEnabled(False)
        self._activate_btn.setText("Activating…")
        # Force the UI to repaint before the blocking HTTP call
        self.repaint()

        ok = self._mgr.activate(self._key_input.text())

        self._activate_btn.setEnabled(True)
        self._activate_btn.setText("Activate")

        if ok:
            self.accept()

    @Slot()
    def _on_buy_clicked(self):
        import webbrowser
        # Placeholder URL — replace with real product/sales page when set up.
        webbrowser.open("https://github.com/Jukree1997/Monitor_Noti_tester")

    @Slot(str)
    def _on_validation_failed(self, reason: str):
        self._status.setText(reason)


# ======================================
# -------- 1. LICENSE INFO DIALOG --------
# ======================================

class LicenseInfoDialog(QDialog):
    """Shown via Help → License Info. Read-only display of current
    license + buttons to deactivate or force-refresh."""

    def __init__(self, license_mgr: LicenseManager, parent=None):
        super().__init__(parent)
        self._mgr = license_mgr
        self.setWindowTitle("License Info")
        self.setMinimumWidth(440)
        self._build()
        self._populate()
        self._wire()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self._title_label = QLabel("<h3>License</h3>")
        self._title_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._title_label)

        self._info_label = QLabel("")
        self._info_label.setTextFormat(Qt.TextFormat.RichText)
        self._info_label.setWordWrap(True)
        self._info_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._info_label)

        # Buttons
        btn_row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh now")
        btn_row.addWidget(self._refresh_btn)
        btn_row.addStretch(1)
        self._deactivate_btn = QPushButton("Deactivate this PC")
        self._deactivate_btn.setStyleSheet(
            "background-color: #8B2C2C; color: white; padding: 6px 14px;")
        btn_row.addWidget(self._deactivate_btn)
        self._close_btn = QPushButton("Close")
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

    def _populate(self):
        s = self._mgr.state
        ent = self._mgr.entitlements

        state_label = {
            LicenseState.ACTIVE:        '<span style="color:#3CAF6A;">Active</span>',
            LicenseState.OFFLINE_GRACE: '<span style="color:#D4A93C;">Offline grace</span>',
            LicenseState.EXPIRED:       '<span style="color:#E0625F;">Expired</span>',
            LicenseState.REVOKED:       '<span style="color:#E0625F;">Revoked</span>',
            LicenseState.UNLICENSED:    '<span style="color:#999;">Not activated</span>',
        }.get(s, str(s))

        tier = ent.get("tier_name", "—")
        max_cameras = ent.get("max_cameras", "—")
        max_machines = ent.get("max_machines", "—")
        expiry = ent.get("expiry") or "perpetual"
        fp = get_fingerprint()

        self._info_label.setText(
            f"<p><b>Status:</b> {state_label}</p>"
            f"<p><b>Tier:</b> {tier}</p>"
            f"<p><b>Cameras (concurrent):</b> {max_cameras}</p>"
            f"<p><b>PC slots:</b> {max_machines}</p>"
            f"<p><b>Expiry:</b> {expiry}</p>"
            f"<p><small><b>This PC fingerprint:</b> "
            f"<code>{fp[:16]}…{fp[-8:]}</code></small></p>"
        )

    def _wire(self):
        self._close_btn.clicked.connect(self.accept)
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._deactivate_btn.clicked.connect(self._on_deactivate)
        self._mgr.state_changed.connect(lambda _s: self._populate())

    @Slot()
    def _on_refresh(self):
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Refreshing…")
        self._mgr.revalidate_async()
        # Re-enable shortly — revalidate is non-blocking, the populate
        # update from state_changed will reflect the new state.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: (
            self._refresh_btn.setEnabled(True),
            self._refresh_btn.setText("Refresh now"),
        ))

    @Slot()
    def _on_deactivate(self):
        confirm = QMessageBox.question(
            self, "Deactivate this PC?",
            "This will release this PC's license slot. The app will "
            "stop working until you re-activate.\n\n"
            "Are you sure?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return
        self._mgr.deactivate()
        # State will transition to UNLICENSED; close the dialog so the
        # main app's state-change handler can decide what to do (likely
        # show ActivationDialog or exit).
        self.accept()
