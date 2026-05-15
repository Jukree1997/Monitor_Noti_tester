"""Single-camera full-screen view for Fleet mode.

When a tile is clicked, FleetTab swaps to this view and asks the worker to
stream live frames. Click the video / press Esc / hit Back to return to the
grid; FleetTab tells the worker to stop streaming.
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QPixmap, QKeyEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
)


def _btn_css(bg: str, hover: str) -> str:
    return (
        f"QPushButton {{ background: {bg}; color: white; border: none; "
        f"border-radius: 4px; padding: 5px 12px; font-weight: bold; }}"
        f"QPushButton:hover {{ background: {hover}; }}"
    )


class _ClickableLabel(QLabel):
    """QLabel that emits a clicked signal on left-click."""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class CameraFullScreenView(QWidget):
    back_clicked = Signal()

    def __init__(self, parent: "QWidget | None" = None):
        super().__init__(parent)
        self._camera_id: str | None = None
        self._last_pixmap: QPixmap | None = None
        self._build_ui()
        # Need focus to receive Esc.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Top overlay bar: Back / camera name / status
        bar = QWidget()
        bar.setFixedHeight(38)
        bar.setStyleSheet("background: #222;")
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 4, 8, 4)
        bar_lay.setSpacing(8)

        self._btn_back = QPushButton("← Back")
        self._btn_back.setFixedHeight(28)
        self._btn_back.setStyleSheet(_btn_css("#3A7CA5", "#2E6585"))
        self._btn_back.clicked.connect(self.back_clicked)
        bar_lay.addWidget(self._btn_back)

        self._lbl_name = QLabel("(no camera)")
        self._lbl_name.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self._lbl_name.setStyleSheet("color: #d4d4d4;")
        bar_lay.addWidget(self._lbl_name)

        bar_lay.addStretch()

        self._lbl_status = QLabel("")
        self._lbl_status.setStyleSheet("color: #aaa; font-size: 11px;")
        bar_lay.addWidget(self._lbl_status)

        lay.addWidget(bar)

        # Big video area
        self._video = _ClickableLabel("")
        self._video.setStyleSheet("background: #000;")
        self._video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
        self._video.clicked.connect(self.back_clicked)
        self._video.setCursor(Qt.CursorShape.PointingHandCursor)
        self._video.setToolTip("Click to return to overview")
        lay.addWidget(self._video, 1)

    # ───────── public API ─────────

    def attach(self, camera_id: str, display_name: str):
        self._camera_id = camera_id
        self._last_pixmap = None
        self._lbl_name.setText(display_name)
        self._lbl_status.setText("waiting for first frame…")
        self._video.setText("")
        self._video.setPixmap(QPixmap())
        self.setFocus()

    def detach(self):
        self._camera_id = None
        self._last_pixmap = None
        self._lbl_name.setText("(no camera)")
        self._lbl_status.setText("")
        self._video.setText("")
        self._video.setPixmap(QPixmap())

    def is_attached(self) -> bool:
        return self._camera_id is not None

    def attached_camera_id(self) -> "str | None":
        return self._camera_id

    def set_status_text(self, text: str):
        if len(text) > 100:
            text = text[:97] + "…"
        self._lbl_status.setText(text)

    def set_frame(self, jpeg_bytes: bytes):
        if not jpeg_bytes:
            return
        pix = QPixmap()
        if not pix.loadFromData(jpeg_bytes, "JPG"):
            return
        self._last_pixmap = pix
        self._render_pixmap()

    def _render_pixmap(self):
        if self._last_pixmap is None:
            return
        target = self._video.size()
        if target.width() <= 0 or target.height() <= 0:
            return
        scaled = self._last_pixmap.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._video.setPixmap(scaled)

    # Re-scale on resize so the video fills the new area.
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_pixmap()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.back_clicked.emit()
            return
        super().keyPressEvent(event)
