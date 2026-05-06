"""Camera tile for the Fleet grid.

Compact widget showing one camera's name, status dot, a placeholder for a
thumbnail (filled in Phase 2d), a short status line, and state-dependent
buttons. Emits per-camera action signals keyed by camera_id; the FleetTab
routes them to the FleetWorkerManager.
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
)


# state -> (dot glyph, color)
_STATE_DOT = {
    "stopped":  ("○", "#888"),       # ○
    "spawning": ("◐", "#ffaa00"),    # ◐
    "running":  ("●", "#44cc44"),    # ●
    "test":     ("◉", "#00ccff"),    # ◉ — running in test mode
    "stopping": ("◐", "#ff8844"),    # ◐
    "error":    ("⚠", "#ff4444"),    # ⚠
}


def _btn_css(bg: str, hover: str) -> str:
    return (
        f"QPushButton {{ background: {bg}; color: white; border: none; "
        f"border-radius: 3px; padding: 4px 8px; font-size: 10px; font-weight: bold; }}"
        f"QPushButton:hover {{ background: {hover}; }}"
        f"QPushButton:disabled {{ background: #555; color: #aaa; }}"
    )


class CameraTile(QFrame):
    start_clicked    = Signal(str)   # camera_id
    test_clicked     = Signal(str)
    stop_clicked     = Signal(str)
    remove_clicked   = Signal(str)
    tile_clicked     = Signal(str)   # full-screen toggle
    settings_clicked = Signal(str)   # open noti-setup viewer

    def __init__(self, camera_id: str, title: str, subtitle: str = "",
                 parent: "QWidget | None" = None):
        super().__init__(parent)
        self._camera_id = camera_id
        self._title = title
        self._subtitle = subtitle
        self._state = "stopped"
        self._test_mode = False
        self._event_count = 0

        self.setFixedSize(220, 240)
        self.setStyleSheet("""
            CameraTile {
                background: #2e2e2e;
                border: 1px solid #444;
                border-radius: 6px;
            }
            CameraTile:hover { border-color: #888; }
        """)
        self._build_ui()
        self._refresh()

    # ───────── public API ─────────

    @property
    def camera_id(self) -> str:
        return self._camera_id

    @property
    def title(self) -> str:
        return self._title

    @property
    def subtitle(self) -> str:
        return self._subtitle

    @property
    def test_mode(self) -> bool:
        return self._test_mode

    def state(self) -> str:
        return self._state

    def set_state(self, state: str, test_mode: "bool | None" = None):
        self._state = state
        if test_mode is not None:
            self._test_mode = test_mode
        if state in ("stopped", "error"):
            # Drop test_mode flag once we're idle so a fresh Start is "live" by default.
            self._test_mode = False
        self._refresh()

    def set_status_text(self, text: str):
        # Trim long status lines (e.g. error messages) so they don't break layout.
        if len(text) > 64:
            text = text[:61] + "…"
        self._lbl_status.setText(text)

    def increment_event_count(self):
        self._event_count += 1
        self._lbl_count.setText(f"\U0001F4AC {self._event_count}")

    def _update_subtitle_elide(self):
        """Elide the project-name subtitle so a long name fits the tile."""
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self._lbl_subtitle.font())
        max_w = max(40, self.width() - 16)
        elided = fm.elidedText(self._subtitle, Qt.TextElideMode.ElideMiddle, max_w)
        self._lbl_subtitle.setText(elided)

    def set_thumbnail(self, jpeg_bytes: bytes):
        """Update the thumbnail from JPEG-encoded bytes (sent by the worker)."""
        if not jpeg_bytes:
            return
        pix = QPixmap()
        if not pix.loadFromData(jpeg_bytes, "JPG"):
            return
        # Scale to fit while preserving aspect; cache the original so a future
        # tile resize can rescale without re-decoding.
        target = self._thumb.size()
        scaled = pix.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        self._thumb.setPixmap(scaled)
        self._thumb.setText("")  # clear placeholder text once we have a real image

    # ───────── build ─────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)

        # Header: status dot + spacer + settings + remove
        header = QHBoxLayout()
        header.setSpacing(4)
        self._lbl_dot = QLabel("○")
        self._lbl_dot.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        header.addWidget(self._lbl_dot)
        header.addStretch(1)
        self._btn_settings = QPushButton("⚙")
        self._btn_settings.setFixedSize(20, 20)
        self._btn_settings.setStyleSheet(
            "QPushButton { background: transparent; color: #888; "
            " border: none; font-size: 13px; }"
            "QPushButton:hover { color: #d4d4d4; }")
        self._btn_settings.setToolTip("View noti setup for this project")
        self._btn_settings.clicked.connect(
            lambda: self.settings_clicked.emit(self._camera_id))
        header.addWidget(self._btn_settings)
        self._btn_remove = QPushButton("✕")
        self._btn_remove.setFixedSize(20, 20)
        self._btn_remove.setStyleSheet(
            "QPushButton { background: transparent; color: #888; "
            " border: none; font-weight: bold; }"
            "QPushButton:hover { color: #ff6666; }")
        self._btn_remove.setToolTip("Remove from fleet")
        self._btn_remove.clicked.connect(
            lambda: self.remove_clicked.emit(self._camera_id))
        header.addWidget(self._btn_remove)
        lay.addLayout(header)

        # Title row — Worker_N centered above the thumbnail.
        self._lbl_title = QLabel(self._title)
        self._lbl_title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self._lbl_title.setStyleSheet("color: #d4d4d4;")
        self._lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_title.setToolTip(self._title)
        lay.addWidget(self._lbl_title)

        # Subtitle — project name shown directly under the worker title.
        self._lbl_subtitle = QLabel(self._subtitle)
        self._lbl_subtitle.setFont(QFont("Segoe UI", 9))
        self._lbl_subtitle.setStyleSheet("color: #999;")
        self._lbl_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_subtitle.setToolTip(self._subtitle)
        # Elide long names so they don't break the layout.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._update_subtitle_elide)
        lay.addWidget(self._lbl_subtitle)

        # Thumbnail (Phase 2d.1 paints actual JPEGs here)
        self._thumb = QLabel("(thumbnail in Phase 2d)")
        self._thumb.setFixedHeight(86)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setStyleSheet(
            "background: #1a1a1a; color: #555; border-radius: 3px; font-size: 10px;")
        self._thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._thumb.mousePressEvent = self._on_thumb_clicked  # bound below
        lay.addWidget(self._thumb)

        # Status line
        self._lbl_status = QLabel("idle")
        self._lbl_status.setStyleSheet("color: #aaa; font-size: 10px;")
        lay.addWidget(self._lbl_status)

        # Event count
        self._lbl_count = QLabel("\U0001F4AC 0")
        self._lbl_count.setStyleSheet("color: #888; font-size: 10px;")
        lay.addWidget(self._lbl_count)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._btn_start = QPushButton("▶ Start")
        self._btn_start.setFixedHeight(24)
        self._btn_start.setStyleSheet(_btn_css("#2E8B57", "#256B45"))
        self._btn_start.clicked.connect(
            lambda: self.start_clicked.emit(self._camera_id))
        btn_row.addWidget(self._btn_start)

        self._btn_test = QPushButton("▷ Test")
        self._btn_test.setFixedHeight(24)
        self._btn_test.setStyleSheet(_btn_css("#1F7A8C", "#175E6D"))
        self._btn_test.clicked.connect(
            lambda: self.test_clicked.emit(self._camera_id))
        btn_row.addWidget(self._btn_test)

        self._btn_stop = QPushButton("■ Stop")
        self._btn_stop.setFixedHeight(24)
        self._btn_stop.setStyleSheet(_btn_css("#CC3333", "#AA2222"))
        self._btn_stop.clicked.connect(
            lambda: self.stop_clicked.emit(self._camera_id))
        btn_row.addWidget(self._btn_stop)
        lay.addLayout(btn_row)

    def _on_thumb_clicked(self, _ev):
        # Phase 2d: full-screen view of this camera. Hooked here so the API is
        # in place — FleetTab can ignore the signal until 2d.
        self.tile_clicked.emit(self._camera_id)

    # ───────── refresh visual state ─────────

    def _refresh(self):
        is_running_test = (self._state == "running" and self._test_mode)
        key = "test" if is_running_test else self._state
        dot, color = _STATE_DOT.get(key, _STATE_DOT["stopped"])
        self._lbl_dot.setText(dot)
        self._lbl_dot.setStyleSheet(f"color: {color};")

        active = self._state in ("running", "spawning", "stopping")
        self._btn_start.setVisible(not active)
        self._btn_test.setVisible(not active)
        self._btn_stop.setVisible(active)
        # Disable Stop while spawning so the user can't double-fire commands
        # before the worker has even reported started.
        self._btn_stop.setEnabled(self._state == "running")
