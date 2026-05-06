from __future__ import annotations
import time
import cv2
import numpy as np
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QImage, QPainter, QColor, QPen, QBrush, QFont, QPolygonF
from PySide6.QtWidgets import QWidget
from models.config_schema import MonitorConfig, Zone, Line
from utils.colors import hex_to_rgb

# Object state colors
STATE_COLORS = {
    "entered":  "#FFD700",  # yellow — just crossed entrance line
    "in_zone":  "#00CC66",  # green  — currently inside a zone
    "stuck":    "#FF3333",  # red    — stuck (no zone, no exit)
    "overstay": "#FF3333",  # red    — zone overstay exceeded
    "normal":   "#4488FF",  # blue   — default
}


class VideoWidget(QWidget):
    """Video display with zone/line/detection overlay and event flash."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 480)
        self.setMouseTracking(True)

        self._frame: np.ndarray | None = None
        self._qimage: QImage | None = None
        self._result = None
        self._config: MonitorConfig | None = None
        self._show_detections = True
        self._show_labels = True
        self._draw_mode = "Dot"

        # Per-detection box colors (aligned with result boxes by index)
        self._box_colors: list[str] = []
        # Per-detection label info dicts (aligned with result boxes by index)
        self._det_labels: list[dict] = []

        # Event flash overlay
        self._flash_regions: dict[str, float] = {}
        self._flash_duration = 1.0

        # Display transform
        self._scale = 1.0
        self._offset_x = 0
        self._offset_y = 0
        self._frame_w = 0
        self._frame_h = 0

    def update_frame(self, frame: np.ndarray, result=None, config: MonitorConfig = None,
                     events: list = None, box_colors: list[str] = None,
                     det_labels: list[dict] = None):
        self._frame = frame
        self._result = result
        if config is not None:
            self._config = config
        if events:
            now = time.time()
            for ev in events:
                self._flash_regions[ev.region_id] = now
        if box_colors is not None:
            self._box_colors = box_colors
        else:
            self._box_colors = []
        if det_labels is not None:
            self._det_labels = det_labels
        else:
            self._det_labels = []

        # Convert BGR to RGB and create QImage
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        self._frame_w = w
        self._frame_h = h
        bytes_per_line = ch * w
        self._qimage = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
        self.update()

    def set_show_detections(self, show: bool):
        self._show_detections = show
        self.update()

    def set_show_labels(self, show: bool):
        self._show_labels = show
        self.update()

    def set_draw_mode(self, mode: str):
        self._draw_mode = mode
        self.update()

    def _compute_transform(self):
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

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.fillRect(self.rect(), QColor(43, 43, 43))

        if self._qimage is None:
            painter.setPen(QColor(128, 128, 128))
            painter.setFont(QFont("Segoe UI", 14))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Load a project config to start")
            painter.end()
            return

        self._compute_transform()

        # Draw frame
        disp_w = self._frame_w * self._scale
        disp_h = self._frame_h * self._scale
        target_rect = QRectF(self._offset_x, self._offset_y, disp_w, disp_h)
        painter.drawImage(target_rect, self._qimage)

        # Draw zones and lines
        if self._config:
            now = time.time()
            for zone in self._config.zones:
                if zone.enabled:
                    is_flashing = self._is_flashing(zone.id, now)
                    self._draw_zone(painter, zone, is_flashing)
            for line in self._config.lines:
                if line.enabled:
                    is_flashing = self._is_flashing(line.id, now)
                    self._draw_line(painter, line, is_flashing)

            self._flash_regions = {
                k: v for k, v in self._flash_regions.items()
                if now - v < self._flash_duration
            }

        # Draw detections with state-based colors
        if self._show_detections and self._result is not None:
            self._draw_detections(painter, self._result)

        painter.end()

    def _is_flashing(self, region_id: str, now: float) -> bool:
        start = self._flash_regions.get(region_id)
        if start is None:
            return False
        return (now - start) < self._flash_duration

    def _draw_zone(self, painter: QPainter, zone: Zone, flashing: bool = False):
        r, g, b = hex_to_rgb(zone.color)
        points = []
        for pt in zone.points:
            dx, dy = self._source_to_display(pt[0], pt[1])
            points.append(QPointF(dx, dy))

        if len(points) < 3:
            return

        polygon = QPolygonF(points)

        alpha = 120 if flashing else 50
        fill_color = QColor(r, g, b, alpha)
        painter.setBrush(QBrush(fill_color))

        border_width = 4 if flashing else 2
        pen = QPen(QColor(r, g, b, 200), border_width)
        painter.setPen(pen)
        painter.drawPolygon(polygon)

        painter.setPen(QColor(r, g, b))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(points[0] + QPointF(5, -5), zone.name)

    def _draw_line(self, painter: QPainter, line: Line, flashing: bool = False):
        r, g, b = hex_to_rgb(line.color)
        sx, sy = self._source_to_display(line.start[0], line.start[1])
        ex, ey = self._source_to_display(line.end[0], line.end[1])

        line_width = 5 if flashing else 3
        pen = QPen(QColor(r, g, b, 200), line_width)
        painter.setPen(pen)
        painter.drawLine(QPointF(sx, sy), QPointF(ex, ey))

        # IN direction arrow
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        dx, dy = ex - sx, ey - sy
        length = max((dx**2 + dy**2) ** 0.5, 1)

        sign = -1.0 if line.invert else 1.0
        nx = sign * (-dy / length) * 20
        ny = sign * (dx / length) * 20

        arrow_color = QColor(r, g, b, 220)
        painter.setPen(QPen(arrow_color, 2))
        painter.drawLine(QPointF(mx, my), QPointF(mx + nx, my + ny))

        tip_x, tip_y = mx + nx, my + ny
        ax1 = tip_x - nx * 0.3 - dx / length * 5
        ay1 = tip_y - ny * 0.3 - dy / length * 5
        ax2 = tip_x - nx * 0.3 + dx / length * 5
        ay2 = tip_y - ny * 0.3 + dy / length * 5
        painter.drawLine(QPointF(tip_x, tip_y), QPointF(ax1, ay1))
        painter.drawLine(QPointF(tip_x, tip_y), QPointF(ax2, ay2))

        painter.setPen(QColor(r, g, b, 180))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.drawText(QPointF(tip_x + 4, tip_y + 4), "IN")

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

            # Get state-based color or default blue
            if i < len(self._box_colors):
                hex_color = self._box_colors[i]
            else:
                hex_color = STATE_COLORS["normal"]

            r, g, b = hex_to_rgb(hex_color)
            color = QColor(r, g, b)

            dx1, dy1 = self._source_to_display(x1, y1)
            dx2, dy2 = self._source_to_display(x2, y2)

            label_info = self._det_labels[i] if i < len(self._det_labels) else None
            label_lines = self._build_label_lines(label_info, cls_id) \
                if self._show_labels else []

            if self._draw_mode == "Box":
                # Thicker border for red (stuck/overstay) boxes
                pen_width = 3 if hex_color == STATE_COLORS["stuck"] else 2
                pen = QPen(color, pen_width)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(QRectF(dx1, dy1, dx2 - dx1, dy2 - dy1))

                if label_lines:
                    self._draw_label(painter, label_lines, color, dx1, dy1,
                                     anchor="box_top", box_bottom=dy2)
            else:
                # Dot mode
                cx = (dx1 + dx2) / 2
                cy = (dy1 + dy2) / 2
                # Larger dot for stuck/overstay
                radius = 7 if hex_color == STATE_COLORS["stuck"] else 5
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(color))
                painter.drawEllipse(QPointF(cx, cy), radius, radius)

                if label_lines:
                    self._draw_label(painter, label_lines, color,
                                     cx + radius + 4, cy, anchor="dot")

    @staticmethod
    def _build_label_lines(info: dict | None, cls_id: int) -> list[str]:
        """Produce 1–4 text lines summarising class / id / dwell."""
        if info is None:
            return [str(cls_id)]
        cls_name = info.get("class_name") or str(cls_id)
        obj_id = info.get("object_id")
        header = f"{cls_name} #{obj_id}" if obj_id is not None else cls_name
        lines = [header]
        fd = info.get("frame_dwell")
        ad = info.get("area_dwell")
        zd = info.get("zone_dwell")
        if fd is not None:
            lines.append(f"Frame: {VideoWidget._fmt_dwell(fd)}")
        if ad is not None:
            lines.append(f"Area:  {VideoWidget._fmt_dwell(ad)}")
        if zd is not None:
            zname = info.get("zone_name") or "zone"
            lines.append(f"Zone:  {VideoWidget._fmt_dwell(zd)} ({zname})")
        return lines

    @staticmethod
    def _fmt_dwell(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m{s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"

    def _draw_label(self, painter: QPainter, lines: list[str], color: QColor,
                    x: float, y: float, anchor: str = "box_top",
                    box_bottom: float | None = None):
        """Render multi-line label with a dark background for legibility."""
        font = QFont("Consolas", 9)
        painter.setFont(font)
        fm = painter.fontMetrics()
        line_h = fm.height()
        pad = 3
        text_w = max(fm.horizontalAdvance(ln) for ln in lines)
        block_w = text_w + pad * 2
        block_h = line_h * len(lines) + pad * 2

        if anchor == "box_top":
            # Place above the box; if it would clip at top, place below.
            top = y - block_h - 2
            if top < 0 and box_bottom is not None:
                top = box_bottom + 2
            left = x
        else:  # "dot"
            # Place to the right of the dot, vertically centered.
            top = y - block_h / 2
            left = x

        bg = QColor(0, 0, 0, 170)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg))
        painter.drawRect(QRectF(left, top, block_w, block_h))

        painter.setPen(color)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for idx, ln in enumerate(lines):
            baseline_y = top + pad + fm.ascent() + idx * line_h
            painter.drawText(QPointF(left + pad, baseline_y), ln)
