"""Read-only viewer for a project's notification settings.

Opened from the ⚙ button on a CameraTile. Reads the saved ``noti_settings``
from the project file (so what you see is what the worker will use).

Color scheme:
  * Each Line / Zone is tinted with its actual color from monitor.lines/zones,
    matching what's drawn on the video — easy to map a row to a region.
  * "on"  → green   (#44cc44)
  * "off" → red     (#ff5555)
  * "DISABLED" → red, "enabled" → muted (default text color).
"""
from __future__ import annotations
import html
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QScrollArea, QPushButton, QWidget, QFrame,
)
from models.config_schema import ProjectConfig


# Semantic colors used inside the dialog body.
_C_ON          = "#44cc44"
_C_OFF         = "#ff5555"
_C_DISABLED    = "#ff5555"
_C_MUTED       = "#888888"
_C_VALUE       = "#d4d4d4"


def _tag_onoff(flag: bool) -> str:
    """Return an HTML span tagging on/off with the right color."""
    return (f'<span style="color:{_C_ON};font-weight:bold">on</span>'
            if flag else
            f'<span style="color:{_C_OFF};font-weight:bold">off</span>')


def _tag_state(enabled: bool) -> str:
    """Return an HTML span for enabled / DISABLED."""
    if enabled:
        return f'<span style="color:{_C_MUTED}">enabled</span>'
    return f'<span style="color:{_C_DISABLED};font-weight:bold">DISABLED</span>'


def _esc(s: str) -> str:
    return html.escape(s or "")


class NotiSetupDialog(QDialog):
    def __init__(self, project: ProjectConfig, project_path: str = "",
                 worker_name: str = "",
                 parent: "QWidget | None" = None):
        super().__init__(parent)
        title = (f"Noti setup — {worker_name or project.project_name or 'Project'}")
        self.setWindowTitle(title)
        self.setMinimumSize(460, 500)
        self._build_ui(project, project_path, worker_name)

    # ───────── build ─────────

    def _build_ui(self, project: ProjectConfig, project_path: str,
                  worker_name: str):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(6)

        # Header — Worker_N (big), project name (medium), path (small)
        if worker_name:
            top = QLabel(_esc(worker_name))
            top.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
            outer.addWidget(top)
        proj_name = project.project_name or "(unnamed project)"
        sub = QLabel(_esc(proj_name))
        sub.setFont(QFont("Segoe UI", 11))
        sub.setStyleSheet(f"color: {_C_VALUE};")
        outer.addWidget(sub)
        if project_path:
            path = QLabel(_esc(project_path))
            path.setStyleSheet("color: #888; font-size: 10px;")
            path.setWordWrap(True)
            outer.addWidget(path)

        outer.addWidget(self._hsep())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(8)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        ns = project.noti_settings
        line_color_by_id = {ln.id: ln.color for ln in project.monitor.lines}
        line_name_by_id  = {ln.id: ln.name  for ln in project.monitor.lines}
        zone_color_by_id = {z.id: z.color for z in project.monitor.zones}
        zone_name_by_id  = {z.id: z.name  for z in project.monitor.zones}

        # ── Line crossing alert ──
        body_lay.addWidget(self._section_title("Line crossing alert"))
        body_lay.addWidget(QLabel(
            f"Cooldown: <b>{ns.line_alert.cooldown_seconds} s</b>"))
        if not ns.line_alert.rules:
            body_lay.addWidget(self._muted(
                "(no per-line rules saved — defaults will apply)"))
        else:
            for r in ns.line_alert.rules:
                name  = line_name_by_id.get(r.line_id, r.line_id)
                color = line_color_by_id.get(r.line_id, "#888888")
                line_html = (
                    f'  • <b style="color:{color}">{_esc(name)}</b>  '
                    f'({_tag_state(r.enabled)}, '
                    f'function=<b>{_esc(r.function)}</b>, '
                    f'IN={_tag_onoff(r.notify_in)}, '
                    f'OUT={_tag_onoff(r.notify_out)})'
                )
                body_lay.addWidget(self._rich(line_html))

        # ── Area overstay ──
        body_lay.addWidget(self._section_title("Area Overstay"))
        ao = ns.zone_area.area_overstay
        if not ao.enabled:
            body_lay.addWidget(self._rich(
                f'(<span style="color:{_C_DISABLED};font-weight:bold">disabled</span>)'))
        else:
            body_lay.addWidget(self._rich(
                f'Enabled: {_tag_onoff(True)}'))
            body_lay.addWidget(QLabel(
                f"Threshold: <b>{ao.threshold_seconds} s</b>"))
            body_lay.addWidget(QLabel(
                f"Reminder every: <b>{ao.reminder_seconds} s</b> "
                "<span style='color:#888'>(0 = once)</span>"))
            classes = (", ".join(ao.target_classes)
                       if ao.target_classes else "(all classes)")
            body_lay.addWidget(QLabel(f"Target classes: <b>{_esc(classes)}</b>"))

        # ── Per-zone rules ──
        body_lay.addWidget(self._section_title("Zone rules"))
        if not ns.zone_area.zone_rules:
            body_lay.addWidget(self._muted(
                "(no per-zone rules saved — defaults will apply)"))
        else:
            for r in ns.zone_area.zone_rules:
                name  = zone_name_by_id.get(r.zone_id, r.zone_id)
                color = zone_color_by_id.get(r.zone_id, "#888888")
                head_html = (
                    f'<b style="color:{color};font-size:12px">{_esc(name)}</b>  '
                    f'({_tag_state(r.enabled)})'
                )
                body_lay.addWidget(self._rich(head_html))
                body_lay.addWidget(self._rich(
                    f'     enter:{_tag_onoff(r.notify_enter)}   '
                    f'exit:{_tag_onoff(r.notify_exit)}   '
                    f'overstay:{_tag_onoff(r.notify_overstay)}'))
                body_lay.addWidget(QLabel(
                    f"     max=<b>{r.max_seconds}s</b>   "
                    f"enter_cooldown=<b>{r.enter_cooldown}s</b>   "
                    f"reminder=<b>{r.overstay_reminder}s</b>"))
                classes = (", ".join(r.target_classes)
                           if r.target_classes else "(all classes)")
                body_lay.addWidget(QLabel(
                    f"     target_classes: <b>{_esc(classes)}</b>"))

        body_lay.addStretch(1)

        btn_close = QPushButton("Close")
        btn_close.setFixedWidth(80)
        btn_close.clicked.connect(self.accept)
        outer.addWidget(btn_close, alignment=Qt.AlignmentFlag.AlignRight)

    # ───────── helpers ─────────

    def _section_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        lbl.setStyleSheet("color: #d4d4d4; padding-top: 4px;")
        return lbl

    def _muted(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {_C_MUTED}; font-size: 10px;")
        return lbl

    def _rich(self, html_text: str) -> QLabel:
        lbl = QLabel(html_text)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        return lbl

    def _hsep(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #555;")
        return sep
