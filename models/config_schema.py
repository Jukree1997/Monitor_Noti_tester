from __future__ import annotations
import json
import uuid
import time
from dataclasses import dataclass, field, asdict, fields as dc_fields
from typing import Optional


def _only_known(cls, data: dict) -> dict:
    """Drop keys not declared on the dataclass so loading tolerates fields
    added by sibling tools (e.g. Monitoring_Config_Tester)."""
    allowed = {f.name for f in dc_fields(cls)}
    return {k: v for k, v in data.items() if k in allowed}


# === Config structures (same as Monitoring_Config_Tester) ===

@dataclass
class Zone:
    id: str
    name: str
    points: list[list[int]]  # [[x,y], [x,y], ...]
    trigger: str = "enter"  # "enter" | "exit" | "present"
    target_classes: Optional[list[str]] = None
    color: str = "#FF6600"
    enabled: bool = True
    # Debounce — object must remain continuously inside the zone for at least
    # this many seconds before zone_enter fires (and continuously outside
    # before zone_exit fires). 0 = no debounce, current behavior.
    min_inside_seconds: float = 0.0

    @staticmethod
    def new(name: str, points: list[list[int]], **kwargs) -> Zone:
        return Zone(id=f"zone_{uuid.uuid4().hex[:8]}", name=name, points=points, **kwargs)


@dataclass
class Line:
    id: str
    name: str
    start: list[int]  # [x, y]
    end: list[int]  # [x, y]
    invert: bool = False
    target_classes: Optional[list[str]] = None
    color: str = "#00AAFF"
    enabled: bool = True

    @staticmethod
    def new(name: str, start: list[int], end: list[int], **kwargs) -> Line:
        return Line(id=f"line_{uuid.uuid4().hex[:8]}", name=name, start=start, end=end, **kwargs)


@dataclass
class MonitorConfig:
    version: int = 1
    source_resolution: list[int] = field(default_factory=lambda: [1920, 1080])
    zones: list[Zone] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

    @staticmethod
    def load(path: str) -> MonitorConfig:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        zones = [Zone(**z) for z in data.get("zones", [])]
        raw_lines = data.get("lines", [])
        for ln in raw_lines:
            ln.pop("direction", None)
            ln.pop("function", None)
            ln.setdefault("invert", False)
        lines = [Line(**ln) for ln in raw_lines]
        return MonitorConfig(
            version=data.get("version", 1),
            source_resolution=data.get("source_resolution", [1920, 1080]),
            zones=zones,
            lines=lines,
        )


@dataclass
class SourceConfig:
    type: str = "camera"  # "rtsp" | "camera" | "file"
    value: str = "0"

@dataclass
class DetectionConfig:
    conf: float = 0.40
    iou: float = 0.45
    imgsz: int = 640

@dataclass
class NotificationConfig:
    channel_token: str = ""
    target_id: str = ""
    cooldown_seconds: int = 30
    send_image: bool = True
    # S3 credentials for snapshot upload (config file is the single source of truth)
    s3_bucket: str = "4market"
    s3_region: str = "ap-southeast-1"
    s3_access_key: str = "AKIATAWAYJAURVP3L6P2"
    s3_secret_key: str = "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"
    s3_url_expiry: int = 600

@dataclass
class LineNotiRule:
    """Per-line notification rule, matched back to a line by line_id."""
    line_id: str
    enabled: bool = True
    function: str = "bidirectional"  # "entrance" | "exit" | "bidirectional"
    notify_in: bool = True
    notify_out: bool = True


@dataclass
class LineAlertConfig:
    cooldown_seconds: int = 30
    rules: list[LineNotiRule] = field(default_factory=list)


@dataclass
class AreaOverstayConfig:
    enabled: bool = False
    threshold_seconds: int = 120
    reminder_seconds: int = 60
    target_classes: list[str] = field(default_factory=list)


@dataclass
class ZoneNotiRule:
    """Per-zone notification rule, matched back to a zone by zone_id."""
    zone_id: str
    enabled: bool = True
    notify_enter: bool = False
    notify_exit: bool = False
    notify_overstay: bool = True
    max_seconds: int = 300
    enter_cooldown: int = 0
    overstay_reminder: int = 300
    target_classes: list[str] = field(default_factory=list)


@dataclass
class ZoneAreaConfig:
    area_overstay: AreaOverstayConfig = field(default_factory=AreaOverstayConfig)
    zone_rules: list[ZoneNotiRule] = field(default_factory=list)


@dataclass
class NotiSettings:
    """Notification UI settings — written by Monitor_Noti_tester only."""
    line_alert: LineAlertConfig = field(default_factory=LineAlertConfig)
    zone_area: ZoneAreaConfig = field(default_factory=ZoneAreaConfig)


def _load_noti_settings(data: dict) -> NotiSettings:
    la_data = data.get("line_alert", {})
    rules = [LineNotiRule(**_only_known(LineNotiRule, r))
             for r in la_data.get("rules", [])]
    line_alert = LineAlertConfig(
        cooldown_seconds=la_data.get("cooldown_seconds", 30),
        rules=rules,
    )
    za_data = data.get("zone_area", {})
    area_overstay = AreaOverstayConfig(
        **_only_known(AreaOverstayConfig, za_data.get("area_overstay", {})))
    zone_rules = [ZoneNotiRule(**_only_known(ZoneNotiRule, r))
                  for r in za_data.get("zone_rules", [])]
    zone_area = ZoneAreaConfig(area_overstay=area_overstay, zone_rules=zone_rules)
    return NotiSettings(line_alert=line_alert, zone_area=zone_area)


@dataclass
class ProjectConfig:
    """Full pipeline config — saved by the Config UI, consumed here."""
    version: int = 1
    project_name: str = ""
    model_path: str = ""
    source: SourceConfig = field(default_factory=SourceConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    noti_settings: NotiSettings = field(default_factory=NotiSettings)

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

    @staticmethod
    def load(path: str) -> ProjectConfig:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        source = SourceConfig(**_only_known(SourceConfig, data.get("source", {})))
        detection = DetectionConfig(**_only_known(DetectionConfig, data.get("detection", {})))
        notification = NotificationConfig(**_only_known(NotificationConfig, data.get("notification", {})))
        mon_data = data.get("monitor", {})
        zones = [Zone(**z) for z in mon_data.get("zones", [])]
        raw_lines = mon_data.get("lines", [])
        for ln in raw_lines:
            ln.pop("direction", None)
            ln.pop("function", None)
            ln.setdefault("invert", False)
        lines = [Line(**ln) for ln in raw_lines]
        monitor = MonitorConfig(
            version=mon_data.get("version", 1),
            source_resolution=mon_data.get("source_resolution", [1920, 1080]),
            zones=zones,
            lines=lines,
        )
        noti_settings = _load_noti_settings(data.get("noti_settings", {}))
        return ProjectConfig(
            version=data.get("version", 1),
            project_name=data.get("project_name", ""),
            model_path=data.get("model_path", ""),
            source=source,
            detection=detection,
            notification=notification,
            monitor=monitor,
            noti_settings=noti_settings,
        )


@dataclass
class Event:
    timestamp: float
    event_type: str  # "line_cross_in" | "line_cross_out" | "stuck" | "zone_overstay"
    region_id: str
    region_name: str
    object_id: int
    class_name: str
    details: str = ""
    reminder_count: int = 0  # zone_overstay: 0 = initial, 1 = 1st reminder, 2 = 2nd reminder

    @property
    def time_str(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))

    def __str__(self) -> str:
        labels = {
            "line_cross_in": "IN",
            "line_cross_out": "OUT",
            "stuck": "STUCK",
            "zone_overstay": "OVERSTAY",
            "zone_enter": "ENTER",
            "zone_exit": "EXIT",
            "line_in": "IN",
            "line_out": "OUT",
        }
        label = labels.get(self.event_type, self.event_type)
        msg = f"{self.time_str} | {label} | {self.region_name} | {self.class_name}#{self.object_id}"
        if self.details:
            msg += f" | {self.details}"
        return msg


# === Notification Runner specific configs ===

@dataclass
class LineRule:
    """Per-line notification rule — assigned in the Noti UI."""
    line_id: str
    line_name: str
    function: str = "bidirectional"  # "entrance" | "exit" | "bidirectional"
    enabled: bool = True
    notify_in: bool = True
    notify_out: bool = True


@dataclass
class StuckConfig:
    enabled: bool = False
    threshold_seconds: int = 120


@dataclass
class ZoneOverstayRule:
    """Per-zone overstay notification rule."""
    zone_id: str
    zone_name: str
    enabled: bool = True
    max_seconds: int = 300
    target_classes: list[str] = field(default_factory=list)  # empty = all classes
