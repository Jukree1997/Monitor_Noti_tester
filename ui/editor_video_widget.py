from __future__ import annotations
import cv2
import numpy as np
from PySide6.QtCore import Qt, Signal, QPointF, QRectF
from PySide6.QtGui import QImage, QPainter, QColor, QPen, QBrush, QFont, QPolygonF, QMouseEvent
from PySide6.QtWidgets import QWidget
from models.config_schema import MonitorConfig, Zone, Line
from utils.colors import get_class_color, bgr_to_rgb, hex_to_rgb


class EditorVideoWidget(QWidget):
    """High-performance video display with zone/line/detection overlay."""

    mouse_clicked = Signal(QPointF)   # source-frame coordinates
    mouse_moved = Signal(QPointF)
    mouse_double_clicked = Signal(QPointF)
    mouse_right_clicked = Signal(QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 480)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._frame: np.ndarray | None = None
        self._qimage: QImage | None = None
        self._result = None
        self._config: MonitorConfig | None = None
        self._events: list = []
        self._show_detections = True
        self._draw_mode = "Box"  # "Box" or "Dot"

        # Editor overlay
        self._editor_points: list[QPointF] = []  # points in source coords
        self._editor_mode: str = "none"  # "none", "zone", "line"
        self._mouse_pos: QPointF | None = None  # current mouse in source coords

        # Display transform (computed in paintEvent)
        self._scale = 1.0
        self._offset_x = 0
        self._offset_y = 0
        self._frame_w = 0
        self._frame_h = 0

    def update_frame(self, frame: np.ndarray, result=None, config: MonitorConfig = None,
                     events: list = None):
        self._frame = frame
        self._result = result
        if config is not None:
            self._config = config
        if events is not None:
            self._events = events

        # Convert BGR to RGB and create QImage
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        self._frame_w = w
        self._frame_h = h
        bytes_per_line = ch * w
        self._qimage = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
        self.update()

    def set_config(self, config: MonitorConfig):
        """Update the zone/line overlay config without waiting for the
        next frame. Useful when the editor tab loads a project before
        the user has connected to a source — the overlay can render on
        whatever was last displayed (or on the blank canvas)."""
        self._config = config
        self.update()

    def set_show_detections(self, show: bool):
        self._show_detections = show
        self.update()

    def set_draw_mode(self, mode: str):
        self._draw_mode = mode
        self.update()

    def set_editor_state(self, mode: str, points: list[QPointF]):
        self._editor_mode = mode
        self._editor_points = points
        self.update()

    def _compute_transform(self):
        """Compute letterbox transform to fit frame in widget."""
        if self._frame_w == 0 or self._frame_h == 0:
            return
        cw = self.width()
        ch = self.height()
        self._scale = min(cw / self._frame_w, ch / self._frame_h)
        disp_w = self._frame_w * self._scale
        disp_h = self._frame_h * self._scale
        self._offset_x = (cw - disp_w) / 2
        self._offset_y = (ch - disp_h) / 2

    def _source_to_display(self, sx: float, sy: float) -> tuple[float, float]:
        dx = sx * self._scale + self._offset_x
        dy = sy * self._scale + self._offset_y
        return dx, dy

    def _display_to_source(self, dx: float, dy: float) -> tuple[float, float]:
        if self._scale == 0:
            return 0, 0
        sx = (dx - self._offset_x) / self._scale
        sy = (dy - self._offset_y) / self._scale
        return sx, sy

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        painter.fillRect(self.rect(), QColor(43, 43, 43))

        if self._qimage is None:
            painter.setPen(QColor(128, 128, 128))
            painter.setFont(QFont("Segoe UI", 14))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Load a model and select input to start")
            painter.end()
            return

        self._compute_transform()

        # Draw frame
        disp_w = self._frame_w * self._scale
        disp_h = self._frame_h * self._scale
        target_rect = QRectF(self._offset_x, self._offset_y, disp_w, disp_h)
        painter.drawImage(target_rect, self._qimage)

        # Draw zones
        if self._config:
            for zone in self._config.zones:
                if zone.enabled:
                    self._draw_zone(painter, zone)
            for line in self._config.lines:
                if line.enabled:
                    self._draw_line(painter, line)

        # Draw detections
        if self._show_detections and self._result is not None:
            self._draw_detections(painter, self._result)

        # Draw edit mode handles on existing zones/lines
        if self._editor_mode == "edit" and self._config:
            self._draw_edit_handles(painter)

        # Draw editor overlay (drawing in progress)
        if self._editor_mode in ("zone", "line"):
            self._draw_editor_overlay(painter)

        painter.end()

    def _draw_zone(self, painter: QPainter, zone: Zone):
        r, g, b = hex_to_rgb(zone.color)
        points = []
        for pt in zone.points:
            dx, dy = self._source_to_display(pt[0], pt[1])
            points.append(QPointF(dx, dy))

        if len(points) < 3:
            return

        polygon = QPolygonF(points)

        # Semi-transparent fill
        fill_color = QColor(r, g, b, 50)
        painter.setBrush(QBrush(fill_color))
        # Border
        pen = QPen(QColor(r, g, b, 200), 2)
        painter.setPen(pen)
        painter.drawPolygon(polygon)

        # Label
        painter.setPen(QColor(r, g, b))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(points[0] + QPointF(5, -5), zone.name)

    def _draw_line(self, painter: QPainter, line: Line):
        r, g, b = hex_to_rgb(line.color)
        sx, sy = self._source_to_display(line.start[0], line.start[1])
        ex, ey = self._source_to_display(line.end[0], line.end[1])

        # Main line
        pen = QPen(QColor(r, g, b, 200), 3)
        painter.setPen(pen)
        painter.drawLine(QPointF(sx, sy), QPointF(ex, ey))

        # IN direction arrow — perpendicular to line
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        dx, dy = ex - sx, ey - sy
        length = max((dx**2 + dy**2) ** 0.5, 1)

        # Normal vector = IN direction. Flip if invert is True.
        sign = -1.0 if line.invert else 1.0
        nx = sign * (-dy / length) * 20
        ny = sign * (dx / length) * 20

        # Draw IN arrow
        arrow_color = QColor(r, g, b, 220)
        painter.setPen(QPen(arrow_color, 2))
        painter.drawLine(QPointF(mx, my), QPointF(mx + nx, my + ny))

        # Arrowhead
        tip_x, tip_y = mx + nx, my + ny
        ax1 = tip_x - nx * 0.3 - dx / length * 5
        ay1 = tip_y - ny * 0.3 - dy / length * 5
        ax2 = tip_x - nx * 0.3 + dx / length * 5
        ay2 = tip_y - ny * 0.3 + dy / length * 5
        painter.drawLine(QPointF(tip_x, tip_y), QPointF(ax1, ay1))
        painter.drawLine(QPointF(tip_x, tip_y), QPointF(ax2, ay2))

        # "IN" label at arrow tip
        painter.setPen(QColor(r, g, b, 180))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.drawText(QPointF(tip_x + 4, tip_y + 4), "IN")

        # Name label
        painter.setPen(QColor(r, g, b))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(QPointF(mx + 10, my - 10), line.name)

    def _draw_detections(self, painter: QPainter, result):
        boxes = result.boxes if result.boxes is not None else None
        if boxes is None or len(boxes) == 0:
            return

        xyxy = boxes.xyxy.cpu().numpy()
        cls_ids = boxes.cls.cpu().numpy().astype(int)
        confs = boxes.conf.cpu().numpy()

        for i in range(len(xyxy)):
            x1, y1, x2, y2 = xyxy[i]
            cls_id = cls_ids[i]
            conf = confs[i]

            bgr = get_class_color(cls_id)
            r, g, b = bgr_to_rgb(bgr)
            color = QColor(r, g, b)

            dx1, dy1 = self._source_to_display(x1, y1)
            dx2, dy2 = self._source_to_display(x2, y2)

            if self._draw_mode == "Box":
                pen = QPen(color, 2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(QRectF(dx1, dy1, dx2 - dx1, dy2 - dy1))

                # Label
                cls_name = ""
                try:
                    from core.detector import DetectionEngine
                except Exception:
                    cls_name = str(cls_id)
                label = f"{cls_id} {conf:.0%}"
                painter.setFont(QFont("Consolas", 9))
                painter.setPen(color)
                painter.drawText(QPointF(dx1, dy1 - 4), label)
            else:
                # Dot mode
                cx = (dx1 + dx2) / 2
                cy = (dy1 + dy2) / 2
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(color))
                painter.drawEllipse(QPointF(cx, cy), 5, 5)

    def _draw_edit_handles(self, painter: QPainter):
        """Draw draggable handle squares on all zone/line vertices in edit mode."""
        handle_size = 6
        painter.setPen(QPen(QColor(255, 255, 0), 1))
        painter.setBrush(QBrush(QColor(255, 255, 0, 200)))

        for zone in self._config.zones:
            for pt in zone.points:
                dx, dy = self._source_to_display(pt[0], pt[1])
                painter.drawRect(QRectF(dx - handle_size, dy - handle_size,
                                        handle_size * 2, handle_size * 2))

        painter.setBrush(QBrush(QColor(0, 200, 255, 200)))
        for line in self._config.lines:
            for pt in [line.start, line.end]:
                dx, dy = self._source_to_display(pt[0], pt[1])
                painter.drawRect(QRectF(dx - handle_size, dy - handle_size,
                                        handle_size * 2, handle_size * 2))

    def _draw_editor_overlay(self, painter: QPainter):
        """Draw in-progress zone/line being drawn."""
        if not self._editor_points:
            return

        pen = QPen(QColor(255, 255, 0), 2, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        display_points = []
        for pt in self._editor_points:
            dx, dy = self._source_to_display(pt.x(), pt.y())
            display_points.append(QPointF(dx, dy))

        # Draw existing segments
        for i in range(len(display_points) - 1):
            painter.drawLine(display_points[i], display_points[i + 1])

        # Draw rubber-band to mouse position
        if self._mouse_pos and display_points:
            mx, my = self._source_to_display(self._mouse_pos.x(), self._mouse_pos.y())
            painter.drawLine(display_points[-1], QPointF(mx, my))

            # For zone mode, also draw closing line
            if self._editor_mode == "zone" and len(display_points) >= 2:
                painter.setPen(QPen(QColor(255, 255, 0, 100), 1, Qt.PenStyle.DotLine))
                painter.drawLine(QPointF(mx, my), display_points[0])

        # Draw vertex handles
        painter.setPen(QPen(QColor(255, 255, 0), 1))
        painter.setBrush(QBrush(QColor(255, 255, 0, 180)))
        for dp in display_points:
            painter.drawRect(QRectF(dp.x() - 4, dp.y() - 4, 8, 8))

    # --- Mouse events ---
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            sx, sy = self._display_to_source(event.position().x(), event.position().y())
            self.mouse_clicked.emit(QPointF(sx, sy))
        elif event.button() == Qt.MouseButton.RightButton:
            sx, sy = self._display_to_source(event.position().x(), event.position().y())
            self.mouse_right_clicked.emit(QPointF(sx, sy))

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            sx, sy = self._display_to_source(event.position().x(), event.position().y())
            self.mouse_double_clicked.emit(QPointF(sx, sy))

    def mouseMoveEvent(self, event: QMouseEvent):
        sx, sy = self._display_to_source(event.position().x(), event.position().y())
        self._mouse_pos = QPointF(sx, sy)
        self.mouse_moved.emit(QPointF(sx, sy))
        if self._editor_mode != "none":
            self.update()  # redraw rubber-band
