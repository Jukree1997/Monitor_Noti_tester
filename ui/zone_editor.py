from __future__ import annotations
from enum import Enum
from PySide6.QtCore import QObject, Signal, QPointF
from models.config_schema import Zone, Line, MonitorConfig
from ui.editor_video_widget import EditorVideoWidget

HANDLE_RADIUS = 12  # pixels in source coords for grabbing a point


class EditorMode(Enum):
    NONE = 0
    DRAW_ZONE = 1
    DRAW_LINE = 2
    EDIT = 3


class ZoneLineEditor(QObject):
    """Manages interactive drawing and editing of zones and lines."""

    zone_created = Signal(Zone)
    line_created = Signal(Line)
    config_modified = Signal()  # emitted when points are dragged in edit mode
    cancelled = Signal()
    status_message = Signal(str)

    def __init__(self, video_widget: EditorVideoWidget):
        super().__init__()
        self._video = video_widget
        self._mode = EditorMode.NONE
        self._points: list[QPointF] = []
        self._name = ""

        # Edit mode state
        self._config: MonitorConfig | None = None
        self._dragging = False
        self._drag_region_type: str | None = None  # "zone" or "line"
        self._drag_region_idx: int = -1
        self._drag_point_idx: int = -1

        # Connect mouse signals
        self._video.mouse_clicked.connect(self._on_click)
        self._video.mouse_right_clicked.connect(self._on_right_click)
        self._video.mouse_moved.connect(self._on_move)

    @property
    def mode(self) -> EditorMode:
        return self._mode

    @property
    def is_active(self) -> bool:
        return self._mode != EditorMode.NONE

    def set_config(self, config: MonitorConfig):
        self._config = config

    def start_zone(self, name: str):
        self._mode = EditorMode.DRAW_ZONE
        self._points.clear()
        self._name = name
        self._update_overlay()
        self.status_message.emit(
            "Left-click to add vertices. Right-click to finish polygon. Esc to cancel."
        )

    def start_line(self, name: str):
        self._mode = EditorMode.DRAW_LINE
        self._points.clear()
        self._name = name
        self._update_overlay()
        self.status_message.emit("Left-click to set line start point.")

    def start_edit(self):
        self._mode = EditorMode.EDIT
        self._dragging = False
        self._points.clear()
        self._update_overlay()
        self.status_message.emit(
            "Edit mode: drag zone/line points to move them. Esc to exit."
        )

    def cancel(self):
        self._mode = EditorMode.NONE
        self._points.clear()
        self._dragging = False
        self._update_overlay()
        self.cancelled.emit()
        self.status_message.emit("")

    def _on_click(self, point: QPointF):
        if self._mode == EditorMode.DRAW_ZONE:
            self._points.append(point)
            self._update_overlay()
            self.status_message.emit(
                f"Zone '{self._name}': {len(self._points)} vertices. "
                "Left-click to add more. Right-click to finish."
            )

        elif self._mode == EditorMode.DRAW_LINE:
            self._points.append(point)
            if len(self._points) == 1:
                self._update_overlay()
                self.status_message.emit("Left-click to set line end point.")
            elif len(self._points) >= 2:
                start = [int(self._points[0].x()), int(self._points[0].y())]
                end = [int(self._points[1].x()), int(self._points[1].y())]
                line = Line.new(name=self._name, start=start, end=end)
                self._mode = EditorMode.NONE
                self._points.clear()
                self._update_overlay()
                self.line_created.emit(line)
                self.status_message.emit(f"Line '{line.name}' created.")

        elif self._mode == EditorMode.EDIT:
            # Start dragging the nearest point
            hit = self._find_nearest_point(point)
            if hit:
                self._dragging = True
                self._drag_region_type, self._drag_region_idx, self._drag_point_idx = hit
                self.status_message.emit(
                    f"Dragging point. Release click to place."
                )

    def _on_move(self, point: QPointF):
        if self._mode == EditorMode.EDIT and self._dragging and self._config:
            # Move the point in real-time
            px, py = int(point.x()), int(point.y())
            if self._drag_region_type == "zone":
                zone = self._config.zones[self._drag_region_idx]
                zone.points[self._drag_point_idx] = [px, py]
            elif self._drag_region_type == "line":
                line = self._config.lines[self._drag_region_idx]
                if self._drag_point_idx == 0:
                    line.start = [px, py]
                else:
                    line.end = [px, py]
            self.config_modified.emit()
            self._video.update()

    def _on_right_click(self, point: QPointF):
        if self._mode == EditorMode.NONE:
            return

        if self._mode == EditorMode.EDIT:
            if self._dragging:
                # Drop the point
                self._dragging = False
                self.config_modified.emit()
                self.status_message.emit("Point placed. Drag another or Esc to exit.")
            return

        if self._mode == EditorMode.DRAW_ZONE:
            if len(self._points) >= 3:
                points = [[int(p.x()), int(p.y())] for p in self._points]
                zone = Zone.new(name=self._name, points=points)
                self._mode = EditorMode.NONE
                self._points.clear()
                self._update_overlay()
                self.zone_created.emit(zone)
                self.status_message.emit(
                    f"Zone '{zone.name}' created with {len(points)} vertices."
                )
            elif self._points:
                self._points.pop()
                self._update_overlay()
                self.status_message.emit(
                    f"Removed last point. Zone '{self._name}': {len(self._points)} vertices. "
                    "Need at least 3 to finish."
                )
            else:
                self.cancel()

        elif self._mode == EditorMode.DRAW_LINE:
            if self._points:
                self._points.pop()
                self._update_overlay()
                self.status_message.emit("Left-click to set line start point.")
            else:
                self.cancel()

    def _find_nearest_point(self, click: QPointF) -> tuple[str, int, int] | None:
        """Find the nearest zone/line vertex within HANDLE_RADIUS of click."""
        if not self._config:
            return None

        best_dist = HANDLE_RADIUS
        best = None

        for zi, zone in enumerate(self._config.zones):
            for pi, pt in enumerate(zone.points):
                d = ((click.x() - pt[0]) ** 2 + (click.y() - pt[1]) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best = ("zone", zi, pi)

        for li, line in enumerate(self._config.lines):
            for pi, pt in enumerate([line.start, line.end]):
                d = ((click.x() - pt[0]) ** 2 + (click.y() - pt[1]) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best = ("line", li, pi)

        return best

    def _update_overlay(self):
        mode_str = "none"
        if self._mode == EditorMode.DRAW_ZONE:
            mode_str = "zone"
        elif self._mode == EditorMode.DRAW_LINE:
            mode_str = "line"
        elif self._mode == EditorMode.EDIT:
            mode_str = "edit"
        self._video.set_editor_state(mode_str, list(self._points))
