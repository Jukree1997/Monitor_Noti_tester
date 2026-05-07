"""Detection + tracking + zone/line + notification pipeline.

Owns: DetectionEngine driver, VideoSource, ZoneLineManager, all overstay /
cooldown bookkeeping, async LINE/S3 notification thread pool. Emits Qt signals
that a UI (or headless harness) consumes — the runner has no widget code, so
the same instance can run inside the Single-tab UI today and inside a
subprocess for Fleet mode tomorrow.
"""
from __future__ import annotations
import time
import cv2
import numpy as np
from dataclasses import asdict
from PySide6.QtCore import QObject, QThreadPool, QRunnable, Signal, Slot

from core.detector import DetectionEngine
from core.tracker import TrackedObject
from core.video_source import VideoSource
from core.zone_manager import ZoneLineManager
from core.line_api import send_text, send_text_and_image
from models.config_schema import ProjectConfig, MonitorConfig, Event


# Grace window (in content seconds) during which an object missing from the
# current-frame detections still counts as "live". Bridges 1–N frame
# detection misses so dwell timers, zone state, and overstay bookkeeping
# don't reset when the YOLO/ByteTrack track ID resumes on re-detection.
OBJECT_GRACE_SECONDS = 2.0

# Cap the long edge of the frame buffer sent to the GUI's video widget.
# WQHD (2560×1440) sources were saturating the UI thread with cv2.cvtColor
# + QImage.copy on every frame; that starved the worker QThread of CPU and
# the worker's wall-clock-measured `dt` ballooned to ~100 ms even though
# the actual GPU inference was 5 ms. Downscaling to 1280 long edge cuts
# render work ~4× without affecting box/zone overlay alignment — the
# video widget keys overlay transforms off MonitorConfig.source_resolution,
# not the displayed buffer size, so source-coord boxes still land on the
# right pixels. self._last_frame stays full-resolution for noti thumbnails.
DISPLAY_LONG_EDGE_PX = 1280



class _NotificationSignals(QObject):
    """Result channel for the async LINE worker — lives on the runner's thread
    so result slots are delivered there via Qt::QueuedConnection."""
    result = Signal(bool, str)


class _NotificationJob(QRunnable):
    """S3 upload + LINE push on a thread-pool worker so the runner's thread
    never blocks on HTTP. Frame is owned by the job; s3_config is a plain dict."""

    def __init__(self, token: str, target: str, message: str,
                 frame: np.ndarray | None, s3_config: dict | None,
                 signals: _NotificationSignals):
        super().__init__()
        self._token = token
        self._target = target
        self._message = message
        self._frame = frame
        self._s3_config = s3_config
        self._signals = signals

    def run(self):
        try:
            if self._frame is not None and self._s3_config:
                ok, status = send_text_and_image(
                    self._token, self._target, self._message,
                    self._frame, self._s3_config)
            else:
                ok, status = send_text(self._token, self._target, self._message)
        except Exception as e:
            ok, status = False, f"Error: {e}"
        self._signals.result.emit(ok, status)


class PipelineRunner(QObject):
    """Drives one project's detection pipeline. UI-agnostic.

    Outputs:
        frame_ready(frame, result, render_data)
            render_data = {"events", "box_colors", "det_labels", "show_overlay"}
            UI passes these to its video widget.
        status_text(text)            — formatted status-bar line (FPS / counters)
        event_logged(text, color)    — pre-formatted log entry for sidebar.add_event
        noti_result(text, success)   — pre-formatted result for sidebar.add_noti_result
        stuck_changed(count, ids)    — for sidebar.update_stuck_count
        error(msg)                   — engine errors
        source_finished()            — source ended naturally (file end / RTSP closed)

    Inputs (slots):
        start(test_mode: bool), stop(), reset(),
        set_runtime_overrides(dict) — line_cooldown / line_rules / zone_rules /
                                       area_config / detect_class_ids
    """

    frame_ready      = Signal(np.ndarray, object, dict)
    status_text      = Signal(str)
    event_logged     = Signal(str, str)
    event_recorded   = Signal(dict, str)   # structured event + status (e.g. NOTI_SENT)
    noti_result      = Signal(str, bool)
    stuck_changed    = Signal(int, list)
    noti_frame_built = Signal(bytes)   # JPEG of the annotated noti frame
    error            = Signal(str)
    source_finished  = Signal()

    def __init__(self, engine: DetectionEngine, project: ProjectConfig,
                 source: VideoSource, class_name_to_id: dict[str, int],
                 parent: QObject | None = None):
        super().__init__(parent)
        self._engine = engine
        self._project = project
        self._monitor: MonitorConfig = project.monitor
        self._source = source
        self._class_name_to_id = class_name_to_id

        self._zone_manager = ZoneLineManager()
        self._zone_manager.set_config(self._monitor)
        self._prev_centroids: dict[int, tuple[int, int]] = {}

        # Runtime overrides — UI pushes these via set_runtime_overrides; the
        # initial values come from the saved project's noti_settings so headless
        # mode works without any UI involvement.
        ns = project.noti_settings
        self._line_cooldown: int = ns.line_alert.cooldown_seconds
        self._line_rules: list[dict] = [
            {"line_id": r.line_id, "line_name": "",
             "function": r.function, "enabled": r.enabled,
             "notify_in": r.notify_in, "notify_out": r.notify_out}
            for r in ns.line_alert.rules]
        ao = ns.zone_area.area_overstay
        self._area_config: dict = {
            "enabled": ao.enabled, "threshold_seconds": ao.threshold_seconds,
            "reminder_seconds": ao.reminder_seconds,
            "target_classes": list(ao.target_classes)}
        self._zone_rules: list[dict] = [
            {"zone_id": r.zone_id, "zone_name": "",
             "enabled": r.enabled,
             "notify_enter": r.notify_enter, "notify_exit": r.notify_exit,
             "notify_overstay": r.notify_overstay,
             "max_seconds": r.max_seconds, "enter_cooldown": r.enter_cooldown,
             "overstay_reminder": r.overstay_reminder,
             "target_classes": list(r.target_classes)}
            for r in ns.zone_area.zone_rules]
        self._detect_class_ids: list[int] | None = None

        # Live-overlay display preferences. Defaults match Single tab's
        # historical behaviour; Fleet workers push their own options at spawn.
        self._display_opts: dict = {
            "show_detections": True,
            "show_labels": True,
            "mode": "box",
        }

        # Run state
        self._running = False
        self._test_mode = False
        self._frame_count = 0
        self._last_fps_time = 0.0
        self._fps = 0.0
        self._last_frame: np.ndarray | None = None
        # Session bookkeeping — used by event_recorded consumers (export buffer)
        # to tag the very first frame's enters as "session_start" so they don't
        # poison dwell averages later. Reset on every start().
        self._session_start_time: float = 0.0
        # Most recent frame_time seen by _on_frame_ready. Used by the export
        # path so "still_inside" dwell at export time is measured in the
        # same domain as event timestamps (content time for files, wall
        # clock for live). 0.0 means no frame has been processed yet.
        self._last_frame_time: float = 0.0
        self._is_first_frame: bool = False
        # (object_id, zone_id) pairs whose centroid was already inside the
        # zone polygon on the very first frame after start(). The eventual
        # zone_enter event (which fires after the debounce window, when
        # _is_first_frame is already False) is tagged session_start using
        # this set. Each pair is consumed once when its zone_enter fires.
        self._session_start_pairs: set[tuple[int, str]] = set()

        # Area overstay (stuck) bookkeeping
        self._entered_objects: dict[int, float] = {}
        self._zone_occupied: dict[int, set] = {}
        self._stuck_objects: set[int] = set()
        self._stuck_noti_sent = False
        # Tracks per-frame "centroid is inside ANY zone polygon" state so
        # transitions (in→out, out→in) can reset the area timer immediately,
        # closing the gap that the zone-enter debounce would otherwise open.
        self._object_in_any_zone: dict[int, bool] = {}

        # Zone overstay bookkeeping
        self._zone_entry_times: dict[tuple[int, str], float] = {}
        self._zone_overstay_notified: set[tuple[int, str]] = set()
        self._overstay_noti_sent = False

        # Object state + bbox caches
        self._object_states: dict[int, str] = {}
        self._tracked_bboxes: dict[int, tuple[int, int, int, int]] = {}
        self._object_first_seen: dict[int, float] = {}
        # Last frame_time at which an obj_id appeared in the detections.
        # Drives the grace-window cleanup so a 1–2 frame detection miss
        # doesn't wipe dwell/overstay state when the same track ID resumes.
        self._obj_last_seen: dict[int, float] = {}
        # Expired-this-frame set, recomputed at the top of _on_frame_ready
        # and consumed by _check_area_overstay / _check_zone_events to keep
        # cleanup decisions consistent across the pipeline.
        self._expired_this_frame: set[int] = set()
        # ByteTrack burns through global IDs whenever it considers a
        # candidate track — so persistent IDs end up sparse (1, 2, 3, 30,
        # 32, …). We remap raw → display so events and on-screen labels
        # show contiguous IDs (1, 2, 3, 4, …). Raw IDs are not used past
        # this remap, so downstream state is keyed by display IDs.
        self._display_id_map: dict[int, int] = {}
        self._next_display_id: int = 1

        # Per-type cooldown timers
        self._last_noti_line: float = 0.0
        self._last_noti_area: float = 0.0
        self._last_noti_zone_enter: dict[str, float] = {}
        self._last_noti_zone_exit: dict[str, float] = {}
        self._zone_overstay_last_time: dict[tuple[int, str], float] = {}
        self._zone_overstay_reminder_count: dict[tuple[int, str], int] = {}

        # Async noti pool — owned by the runner so headless mode also has a
        # background channel for LINE/S3 HTTP without blocking the main loop.
        self._noti_pool = QThreadPool()
        self._noti_pool.setMaxThreadCount(2)
        self._noti_signals = _NotificationSignals()
        self._noti_signals.result.connect(self._on_noti_result)

    # ───────────────────── lifecycle ─────────────────────

    @Slot(bool)
    def start(self, test_mode: bool):
        """Start the detection loop. test_mode=True runs everything except the
        actual S3 upload + LINE push (events still classified, sidebar still
        gets [TEST] entries — same as current Single-tab behavior)."""
        if self._running:
            return
        self._engine.reset_tracker()
        self._prev_centroids.clear()
        self._zone_manager.reset()
        self._zone_manager.set_config(self._monitor)
        self._reset_all_state()
        self._frame_count = 0
        self._last_fps_time = time.perf_counter()
        self._test_mode = test_mode
        self._session_start_time = time.time()
        self._is_first_frame = True
        self._running = True
        self._engine.start(
            source=self._source,
            conf=self._project.detection.conf,
            iou=self._project.detection.iou,
            imgsz=self._project.detection.imgsz,
            on_frame=self._on_frame_ready,
            on_error=self._on_error,
            on_finished=self._on_engine_finished,
            classes=self._detect_class_ids,
        )

    @Slot()
    def stop(self):
        if not self._running:
            return
        self._running = False
        self._engine.stop()

    @Slot()
    def reset(self):
        self._reset_all_state()
        self.event_logged.emit(
            f"{time.strftime('%H:%M:%S')} | RESET | All tracking cleared (manual)",
            "#888888")

    @Slot(dict)
    def set_runtime_overrides(self, overrides: dict):
        """Push live UI changes into the runner. Only keys present in the dict
        are updated; absent keys keep their current values."""
        if "line_cooldown" in overrides:
            self._line_cooldown = int(overrides["line_cooldown"])
        if "line_rules" in overrides:
            self._line_rules = list(overrides["line_rules"])
        if "zone_rules" in overrides:
            self._zone_rules = list(overrides["zone_rules"])
        if "area_config" in overrides:
            self._area_config = dict(overrides["area_config"])
        if "detect_class_ids" in overrides:
            self._detect_class_ids = overrides["detect_class_ids"]
            # Engine has its own live setter for the running detector
            self._engine.set_classes(self._detect_class_ids)
        if "display" in overrides and isinstance(overrides["display"], dict):
            # Merge so partial pushes (e.g. just mode) don't reset siblings.
            self._display_opts.update(overrides["display"])

    def wait_for_pending_noti(self, timeout_ms: int = 5000):
        """Drain in-flight noti jobs — call from UI's closeEvent / headless exit."""
        self._noti_pool.waitForDone(timeout_ms)

    def get_currently_tracked_ids(self) -> set[int]:
        """Snapshot of object IDs the engine had on its last frame. Used by
        the exporter to distinguish "still inside a zone" from "lost ID",
        since a buffered enter with no exit could be either."""
        return set(self._tracked_bboxes.keys())

    @property
    def session_start_time(self) -> float:
        """Wall-clock time of the most recent start(). 0.0 before first start."""
        return self._session_start_time

    @property
    def last_frame_time(self) -> float:
        """Most recent per-frame timestamp emitted by the engine. Same domain
        as event.timestamp — wall-clock-shaped for both file and live, but
        for files it advances at the video's content rate. 0.0 before any
        frame has been processed."""
        return self._last_frame_time

    # ───────────────────── engine callbacks ─────────────────────

    @Slot(np.ndarray, object, float, float)
    def _on_frame_ready(self, frame: np.ndarray, result, inference_ms: float,
                        frame_time: float):
        if not self._running:
            return

        self._last_frame = frame
        self._frame_count += 1
        now_perf = time.perf_counter()
        if now_perf - self._last_fps_time >= 1.0:
            self._fps = self._frame_count / (now_perf - self._last_fps_time)
            self._frame_count = 0
            self._last_fps_time = now_perf

        detections = []
        tracked: dict[int, TrackedObject] = {}
        if result is not None and result.boxes is not None and len(result.boxes) > 0:
            xyxy = result.boxes.xyxy.cpu().numpy()
            cls_ids = result.boxes.cls.cpu().numpy().astype(int)
            names = self._engine.model_names
            track_ids = (result.boxes.id.cpu().numpy().astype(int)
                         if result.boxes.id is not None else None)

            for i in range(len(xyxy)):
                x1, y1, x2, y2 = (int(xyxy[i][0]), int(xyxy[i][1]),
                                  int(xyxy[i][2]), int(xyxy[i][3]))
                cls_id = int(cls_ids[i])
                cls_name = names.get(cls_id, str(cls_id))
                detections.append((x1, y1, x2, y2, cls_id, cls_name))
                if track_ids is not None:
                    raw_id = int(track_ids[i])
                    obj_id = self._display_id_map.get(raw_id)
                    if obj_id is None:
                        obj_id = self._next_display_id
                        self._display_id_map[raw_id] = obj_id
                        self._next_display_id += 1
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                    tracked[obj_id] = TrackedObject(
                        object_id=obj_id, centroid=(cx, cy),
                        prev_centroid=self._prev_centroids.get(obj_id),
                        class_id=cls_id, class_name=cls_name)
                    self._prev_centroids[obj_id] = (cx, cy)
                    self._tracked_bboxes[obj_id] = (x1, y1, x2, y2)

        # Update last-seen for currently-tracked objects, then compute the
        # set of obj_ids that have been missing past the grace window. We
        # only purge state for *expired* ids — anything still within grace
        # keeps its dwell, zone state, and overstay bookkeeping intact, so a
        # brief detection miss followed by ByteTrack resuming the same ID
        # doesn't show up as a fresh enter with reset timers.
        for oid in tracked:
            self._obj_last_seen[oid] = frame_time
        expired_ids = {oid for oid, ts in self._obj_last_seen.items()
                       if frame_time - ts > OBJECT_GRACE_SECONDS}
        for oid in expired_ids:
            del self._obj_last_seen[oid]
        self._expired_this_frame = expired_ids

        # Drop the raw→display mapping for IDs that have aged out so the
        # mapping dict can't grow without bound on long sessions.
        if expired_ids:
            self._display_id_map = {
                raw: disp for raw, disp in self._display_id_map.items()
                if disp not in expired_ids}

        for oid in expired_ids:
            self._prev_centroids.pop(oid, None)
            self._tracked_bboxes.pop(oid, None)

        # First-seen timestamps — uses frame_time so dwell stays consistent
        # under UI load.
        for obj_id in tracked:
            self._object_first_seen.setdefault(obj_id, frame_time)
        for oid in expired_ids:
            self._object_first_seen.pop(oid, None)

        # On the very first frame after start(), record any (object, zone)
        # pairs whose centroid is already inside the polygon. These are
        # pre-existing objects whose true enter time is unknown — their
        # eventual zone_enter event (after the debounce) will be tagged
        # quality=session_start so dwell stats can exclude them.
        if self._is_first_frame:
            for obj_id, obj in tracked.items():
                for zone in self._monitor.zones:
                    if not zone.enabled:
                        continue
                    polygon = np.array(zone.points, dtype=np.float32)
                    if cv2.pointPolygonTest(polygon, obj.centroid, False) >= 0:
                        self._session_start_pairs.add((obj_id, zone.id))

        self._last_frame_time = frame_time
        base_events = self._zone_manager.update(
            tracked, frame_time, expired_ids=self._expired_this_frame)
        self._check_area_overstay(tracked, base_events, frame_time)
        extra_events = self._check_zone_events(tracked, frame_time)
        self._update_object_states(tracked)
        box_colors = self._build_box_colors(detections, tracked)
        det_labels = self._build_det_labels(detections, tracked, result, frame_time)

        all_events = list(base_events) + extra_events
        for ev in all_events:
            self._log_event(ev)

        # First-frame flag drops *after* this frame's events are processed so
        # the runner's _log_event can stamp them as session_start.
        if self._is_first_frame:
            self._is_first_frame = False

        # Downscale the frame buffer for the UI emit to keep the UI thread
        # from saturating on cvtColor + QImage.copy at high source
        # resolutions (which back-pressures the worker QThread). Box and
        # zone coords stay in source resolution; the video widget keys its
        # source→display transform off MonitorConfig.source_resolution, so
        # alignment is preserved even when the displayed buffer is smaller.
        display_frame = frame
        long_edge = max(frame.shape[:2])
        if long_edge > DISPLAY_LONG_EDGE_PX:
            s = DISPLAY_LONG_EDGE_PX / long_edge
            new_w = int(round(frame.shape[1] * s))
            new_h = int(round(frame.shape[0] * s))
            display_frame = cv2.resize(
                frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

        # Hand the rendered ingredients to the UI; UI decides whether to
        # actually draw (e.g. respects "Show live" toggle). The Fleet worker
        # also uses these to render an overlay onto live frames before
        # JPEG-encoding for full-screen streaming.
        self.frame_ready.emit(display_frame, result, {
            "events": all_events,
            "box_colors": box_colors,
            "det_labels": det_labels,
            "detections": detections,
        })

        mode_tag = "TEST" if self._test_mode else "LIVE"
        self.status_text.emit(
            f"[{mode_tag}] FPS: {self._fps:.1f} | "
            f"Inf: {inference_ms:.1f}ms @ {self._project.detection.imgsz}px | "
            f"Obj: {len(detections)} | Track: {len(tracked)} | "
            f"Stuck: {len(self._stuck_objects)} | {self._engine.device}")

    @Slot(str)
    def _on_error(self, msg: str):
        self.error.emit(msg)

    @Slot()
    def _on_engine_finished(self):
        # Source ended naturally — engine thread has exited. Mark not running
        # and let UI restart preview if it wants.
        if self._running:
            self._running = False
            self.source_finished.emit()

    # ───────────────────── object states / labels ─────────────────────

    def _update_object_states(self, tracked: dict):
        objects_in_zones: set[int] = set()
        for obj_id, obj in tracked.items():
            for zone in self._monitor.zones:
                polygon = np.array(zone.points, dtype=np.float32)
                if cv2.pointPolygonTest(polygon, obj.centroid, False) >= 0:
                    objects_in_zones.add(obj_id)
                    break
        for obj_id in tracked:
            if obj_id in self._stuck_objects:
                self._object_states[obj_id] = "stuck"
            elif any(k[0] == obj_id for k in self._zone_overstay_notified):
                self._object_states[obj_id] = "overstay"
            elif obj_id in objects_in_zones:
                self._object_states[obj_id] = "in_zone"
            elif obj_id in self._entered_objects:
                self._object_states[obj_id] = "entered"
            else:
                self._object_states[obj_id] = "normal"
        for oid in set(self._object_states.keys()) - set(tracked.keys()):
            del self._object_states[oid]

    def _build_box_colors(self, detections: list, tracked: dict) -> list[str]:
        # STATE_COLORS lives in ui.video_widget — kept there because it's a
        # render-time mapping. Runner just emits state names; UI maps to colors.
        # For Phase 1 (zero behavior change) we mirror the current code path
        # which produced hex strings; UI will translate. To avoid cyclic
        # imports, we import lazily.
        from ui.video_widget import STATE_COLORS
        centroid_to_id = {obj.centroid: oid for oid, obj in tracked.items()}
        colors = []
        for det in detections:
            cx, cy = (det[0] + det[2]) // 2, (det[1] + det[3]) // 2
            obj_id = centroid_to_id.get((cx, cy))
            state = self._object_states.get(obj_id, "normal") if obj_id else "normal"
            colors.append(STATE_COLORS.get(state, STATE_COLORS["normal"]))
        return colors

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

    def _build_det_labels(self, detections: list, tracked: dict, result,
                          now: float) -> list[dict]:
        centroid_to_id = {obj.centroid: oid for oid, obj in tracked.items()}
        labels = []
        for i, det in enumerate(detections):
            cx, cy = (det[0] + det[2]) // 2, (det[1] + det[3]) // 2
            cls_name = det[5]
            obj_id = centroid_to_id.get((cx, cy))

            frame_dwell = area_dwell = zone_dwell = None
            zone_name = None
            if obj_id is not None:
                first = self._object_first_seen.get(obj_id)
                if first is not None:
                    frame_dwell = now - first
                # area_dwell is "loitering time outside zones in the watched
                # area" — meaningless while inside a zone (the zone_dwell
                # label covers that). Hide it during zone visits so the user
                # doesn't see two timers showing the same number.
                in_any_zone = self._object_in_any_zone.get(obj_id, False)
                if obj_id in self._entered_objects and not in_any_zone:
                    area_dwell = now - self._entered_objects[obj_id]
                zone_entries = [(k[1], v) for k, v in self._zone_entry_times.items()
                                if k[0] == obj_id]
                if zone_entries:
                    zid, zt = max(zone_entries, key=lambda kv: kv[1])
                    zone_dwell = now - zt
                    zmap = {z.id: z.name for z in self._monitor.zones}
                    zone_name = zmap.get(zid, zid)

            labels.append({
                "class_name": cls_name,
                "object_id": obj_id,
                "frame_dwell": frame_dwell,
                "area_dwell": area_dwell,
                "zone_dwell": zone_dwell,
                "zone_name": zone_name,
            })
        return labels

    # ───────────────────── event log + noti dispatch ─────────────────────

    def _log_event(self, ev: Event):
        """Classify and emit the event for UI display; send LINE in live mode.

        When the would-notify+cooldown-ok path fires (test or live), build the
        annotated noti frame ONCE and emit it as JPEG bytes for thumbnail
        consumers (Fleet tiles). Live mode reuses that same frame for the
        S3 upload — no double-build.
        """
        event_text = str(ev)
        would_notify = self._should_notify(ev)
        needs_cooldown = ev.event_type in ("line_in", "line_out",
                                            "zone_enter", "zone_exit")

        # Cooldown evaluation is *stateful* (it updates _last_noti_*), so call
        # it exactly once even though we read it twice below.
        cd_ok = True
        remaining = 0
        if would_notify and needs_cooldown:
            cd_ok, remaining = self._check_cooldown(ev, simulate=self._test_mode)

        # Frame for thumbnail / S3 — built once and reused.
        annotated = None
        if would_notify and cd_ok:
            annotated = self._build_noti_frame_and_emit_jpeg(ev)

        # Decide a single status string for the structured event_recorded
        # signal — kept in lockstep with the display branches below.
        if self._test_mode:
            status = "TEST_OK" if would_notify and cd_ok else (
                "TEST_WAIT" if would_notify else "TEST_LOG")
        else:
            status = "NOTI_SENT" if would_notify and cd_ok else (
                "WAIT_COOLDOWN" if would_notify else "LOG_ONLY")

        # Stamp the event with quality + mode and emit the structured copy
        # for buffer consumers. session_start is determined from the
        # pre-existing-pairs set populated on frame 1 — robust against
        # debounce delaying the first zone_enter past frame 1.
        ev_dict = asdict(ev)
        pair = (ev.object_id, ev.region_id)
        if (ev.event_type == "zone_enter"
                and pair in self._session_start_pairs):
            quality = "session_start"
            self._session_start_pairs.discard(pair)
        else:
            quality = "ok"
        ev_dict["quality"] = quality
        ev_dict["mode"] = "TEST" if self._test_mode else "LIVE"
        self.event_recorded.emit(ev_dict, status)

        if self._test_mode:
            if would_notify:
                if cd_ok:
                    self.event_logged.emit(f"[TEST ✓] {event_text}", "#00ccff")
                    self.noti_result.emit("Would send notification", True)
                else:
                    self.event_logged.emit(f"[TEST ⏳] {event_text}", "#ffaa00")
                    self.noti_result.emit(
                        f"Would cooldown ({remaining}s left)", False)
            else:
                self.event_logged.emit(f"[TEST -] {event_text}", "#888888")
        else:
            if would_notify:
                if cd_ok:
                    self.event_logged.emit(f"[\U0001f514 NOTI] {event_text}", "#00ff88")
                    self._send_line_notification(ev, prebuilt_frame=annotated)
                else:
                    self.event_logged.emit(f"[⏳ WAIT] {event_text}", "#ffaa00")
                    self.noti_result.emit(f"Cooldown ({remaining}s left)", False)
            else:
                self.event_logged.emit(f"[LOG] {event_text}", "#888888")

    def _build_noti_frame_and_emit_jpeg(self, ev: Event):
        """Build the annotated noti frame, emit it as JPEG for thumbnails.
        Returns the np.ndarray for downstream LINE/S3 reuse, or None if no
        last_frame is available."""
        if self._last_frame is None:
            return None
        try:
            annotated = self._build_notification_frame(ev)
        except Exception:
            return None
        try:
            ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                self.noti_frame_built.emit(bytes(buf))
        except Exception:
            pass
        return annotated

    def _send_line_notification(self, ev: Event, prebuilt_frame=None):
        noti = self._project.notification
        token = noti.channel_token
        target = noti.target_id
        if not token or not target:
            self.noti_result.emit("Missing token or target ID in config", False)
            return

        message = self._format_line_message(ev)

        s3_config = None
        annotated = None
        if (self._last_frame is not None and noti.s3_bucket
                and noti.s3_access_key and noti.s3_secret_key):
            s3_config = {
                "bucket": noti.s3_bucket,
                "region": noti.s3_region,
                "access_key": noti.s3_access_key,
                "secret_key": noti.s3_secret_key,
                "expiry": noti.s3_url_expiry,
            }
            # Reuse a pre-built frame if the caller already encoded it for
            # thumbnails — avoids drawing the overlay twice.
            annotated = (prebuilt_frame
                         if prebuilt_frame is not None
                         else self._build_notification_frame(ev))

        job = _NotificationJob(token, target, message, annotated, s3_config,
                                self._noti_signals)
        self._noti_pool.start(job)

    @Slot(bool, str)
    def _on_noti_result(self, success: bool, status: str):
        self.noti_result.emit(status, success)

    def _format_line_message(self, ev: Event) -> str:
        t = ev.time_str
        name = ev.region_name
        if ev.event_type == "line_in":
            body = f"Line Crossing IN\nLine: {name}\n{t}"
        elif ev.event_type == "line_out":
            body = f"Line Crossing OUT\nLine: {name}\n{t}"
        elif ev.event_type == "zone_enter":
            body = f"Zone Enter\nZone: {name}\n{t}"
        elif ev.event_type == "zone_exit":
            body = f"Zone Exit\nZone: {name}\n{t}"
        elif ev.event_type == "zone_overstay":
            suffix = ""
            if ev.reminder_count == 1:
                suffix = "\n1st reminder"
            elif ev.reminder_count == 2:
                suffix = "\n2nd reminder"
            body = f"Zone Overstay\nZone: {name}\n{ev.details}\n{t}{suffix}"
        elif ev.event_type == "stuck":
            body = f"Area Overstay\n{ev.details}\n{t}"
        else:
            body = f"{ev.event_type}\n{name}\n{t}"
        # Project name on top so the LINE channel reader can tell which
        # camera fired. Empty project_name → no prefix (legacy behavior).
        proj_name = self._project.project_name
        if proj_name:
            return f"[{proj_name}]\n{body}"
        return body

    def _draw_zones_and_lines(self, frame: np.ndarray) -> None:
        """In-place: shade zones, outline polygons, draw lines + labels."""
        for zone in self._monitor.zones:
            if not zone.enabled:
                continue
            pts = np.array(zone.points, dtype=np.int32)
            color = self._hex_to_bgr(zone.color)
            overlay = frame.copy()
            cv2.fillPoly(overlay, [pts], color)
            cv2.addWeighted(overlay, 0.2, frame, 0.8, 0, frame)
            cv2.polylines(frame, [pts], True, color, 2)
            if len(zone.points) > 0:
                cv2.putText(frame, zone.name,
                            (zone.points[0][0] + 5, zone.points[0][1] - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

        for line in self._monitor.lines:
            if not line.enabled:
                continue
            color = self._hex_to_bgr(line.color)
            cv2.line(frame, tuple(line.start), tuple(line.end), color, 2)
            mx = (line.start[0] + line.end[0]) // 2
            my = (line.start[1] + line.end[1]) // 2
            cv2.putText(frame, line.name, (mx + 5, my - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    def build_live_overlay_frame(self, frame: np.ndarray,
                                  render_data: dict) -> np.ndarray:
        """Annotated copy of `frame` for live full-screen view.

        Draws zones / lines unconditionally, then per-detection markers and
        labels gated by ``self._display_opts``:
            show_detections — draw boxes / dots at all
            show_labels     — draw 'class #id' labels next to markers
            mode            — "box" rectangle or "dot" filled circle at centroid
        """
        out = frame.copy()
        self._draw_zones_and_lines(out)

        if not self._display_opts.get("show_detections", True):
            return out

        detections = render_data.get("detections") or []
        box_colors = render_data.get("box_colors") or []
        det_labels = render_data.get("det_labels") or []
        mode = self._display_opts.get("mode", "box")
        show_labels = self._display_opts.get("show_labels", True)
        dot_radius = 6

        for i, det in enumerate(detections):
            x1, y1, x2, y2 = int(det[0]), int(det[1]), int(det[2]), int(det[3])
            cls_name = (det[5] if len(det) >= 6
                        else str(det[4]) if len(det) >= 5 else "?")
            color_hex = box_colors[i] if i < len(box_colors) else "#FFFFFF"
            bgr = self._hex_to_bgr(color_hex)

            if mode == "dot":
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                cv2.circle(out, (cx, cy), dot_radius, bgr, -1)
                lbl_x, lbl_baseline_y = cx + dot_radius + 4, cy + 4
            else:
                cv2.rectangle(out, (x1, y1), (x2, y2), bgr, 2)
                lbl_x = x1 + 3
                lbl_baseline_y = max(14, y1 - 4)

            if not show_labels:
                continue

            label_text = cls_name
            if i < len(det_labels):
                obj_id = det_labels[i].get("object_id")
                if obj_id is not None:
                    label_text = f"{cls_name} #{obj_id}"
            (tw, th), bl = cv2.getTextSize(
                label_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(out,
                          (lbl_x - 2, lbl_baseline_y - th - 3),
                          (lbl_x + tw + 4, lbl_baseline_y + bl),
                          (0, 0, 0), -1)
            cv2.putText(out, label_text, (lbl_x, lbl_baseline_y - 1),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, bgr, 1)
        return out

    def _build_notification_frame(self, ev: Event) -> np.ndarray:
        frame = self._last_frame.copy()
        self._draw_zones_and_lines(frame)

        if ev.event_type == "stuck":
            for obj_id in self._stuck_objects:
                bbox = self._tracked_bboxes.get(obj_id)
                if bbox:
                    x1, y1, x2, y2 = bbox
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
                    cv2.putText(frame, "STUCK", (x1, y1 - 8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        elif ev.object_id >= 0:
            bbox = self._tracked_bboxes.get(ev.object_id)
            if bbox:
                x1, y1, x2, y2 = bbox
                if ev.event_type in ("line_in", "line_out"):
                    color = (0, 215, 255)
                elif ev.event_type == "zone_enter":
                    color = (102, 204, 0)
                elif ev.event_type == "zone_exit":
                    color = (0, 140, 255)
                elif ev.event_type == "zone_overstay":
                    color = (51, 51, 255)
                else:
                    color = (255, 136, 68)
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
                labels = {
                    "line_in": "IN", "line_out": "OUT",
                    "zone_enter": "ENTER", "zone_exit": "EXIT",
                    "zone_overstay": "OVERSTAY",
                }
                label = labels.get(ev.event_type, ev.event_type)
                cv2.putText(frame, label, (x1, y1 - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        return frame

    @staticmethod
    def _hex_to_bgr(hex_color: str) -> tuple[int, int, int]:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (b, g, r)

    # ───────────────────── cooldown / should-notify ─────────────────────

    def _check_cooldown(self, ev: Event, simulate: bool) -> tuple[bool, int]:
        now = time.time()
        if ev.event_type in ("line_in", "line_out"):
            cd = self._line_cooldown
            if cd == 0:
                return True, 0
            elapsed = now - self._last_noti_line
            if elapsed >= cd:
                self._last_noti_line = now
                return True, 0
            return False, int(cd - elapsed)

        elif ev.event_type in ("zone_enter", "zone_exit"):
            zone_rules = {r["zone_id"]: r for r in self._zone_rules}
            rule = zone_rules.get(ev.region_id, {})
            cd = rule.get("enter_cooldown", 0)
            if cd == 0:
                return True, 0
            last_map = (self._last_noti_zone_enter if ev.event_type == "zone_enter"
                        else self._last_noti_zone_exit)
            last = last_map.get(ev.region_id, 0)
            elapsed = now - last
            if elapsed >= cd:
                last_map[ev.region_id] = now
                return True, 0
            return False, int(cd - elapsed)

        return True, 0

    def _should_notify(self, ev: Event) -> bool:
        if ev.event_type in ("line_in", "line_out"):
            for rule in self._line_rules:
                if rule["line_id"] == ev.region_id and rule["enabled"]:
                    if ev.event_type == "line_in" and rule["notify_in"]:
                        return True
                    if ev.event_type == "line_out" and rule["notify_out"]:
                        return True
            return False
        if ev.event_type == "stuck":
            return self._area_config["enabled"]
        if ev.event_type == "zone_enter":
            for rule in self._zone_rules:
                if rule["zone_id"] == ev.region_id and rule["enabled"] and rule["notify_enter"]:
                    return True
            return False
        if ev.event_type == "zone_exit":
            for rule in self._zone_rules:
                if rule["zone_id"] == ev.region_id and rule["enabled"] and rule["notify_exit"]:
                    return True
            return False
        if ev.event_type == "zone_overstay":
            for rule in self._zone_rules:
                if rule["zone_id"] == ev.region_id and rule["enabled"] and rule["notify_overstay"]:
                    return True
            return False
        return False

    # ───────────────────── area overstay ─────────────────────

    def _check_area_overstay(self, tracked: dict, events: list[Event], now: float):
        area_config = self._area_config
        if not area_config["enabled"]:
            return

        line_rules = self._line_rules
        threshold = area_config["threshold_seconds"]
        target_classes = area_config["target_classes"]

        entrance_ids: set[str] = set()
        exit_ids: set[str] = set()
        bidirectional_ids: set[str] = set()
        for r in line_rules:
            if r["function"] in ("entrance", "bidirectional"):
                entrance_ids.add(r["line_id"])
            if r["function"] in ("exit", "bidirectional"):
                exit_ids.add(r["line_id"])
            if r["function"] == "bidirectional":
                bidirectional_ids.add(r["line_id"])

        for ev in events:
            if target_classes and ev.class_name not in target_classes:
                continue
            if ev.event_type == "line_in":
                is_ent = ev.region_id in entrance_ids
                is_ext = ev.region_id in exit_ids
                if is_ent:
                    if ev.object_id not in self._entered_objects:
                        self._entered_objects[ev.object_id] = now
                        self._zone_occupied.setdefault(ev.object_id, set())
                elif is_ext:
                    self._entered_objects.pop(ev.object_id, None)
                    self._zone_occupied.pop(ev.object_id, None)
                    self._stuck_objects.discard(ev.object_id)
            elif ev.event_type == "line_out":
                # line_out only acts as area-exit on bidirectional lines.
                # On strict entrance/exit lines, line_out is a wrong-
                # direction crossing (camera shake, ID switch), not a real
                # exit — ignore it for area state so the area timer doesn't
                # reset on phantom reverse crossings.
                if ev.region_id in bidirectional_ids:
                    self._entered_objects.pop(ev.object_id, None)
                    self._zone_occupied.pop(ev.object_id, None)
                    self._stuck_objects.discard(ev.object_id)
            elif ev.event_type == "zone_enter":
                if ev.object_id in self._zone_occupied:
                    # Bookkeeping for the confirmed-zone set; the area-timer
                    # reset itself is now driven by the polygon-contact
                    # transition pass below (so the debounce window doesn't
                    # leak loitering time).
                    self._zone_occupied[ev.object_id].add(ev.region_id)
                    self._stuck_objects.discard(ev.object_id)
            elif ev.event_type == "zone_exit":
                if ev.object_id in self._zone_occupied:
                    self._zone_occupied[ev.object_id].discard(ev.region_id)

        # Polygon-contact transitions — reset the area timer the *moment*
        # the centroid enters any zone or leaves all zones, regardless of
        # whether zone_enter/zone_exit has fired yet (debounce). This is
        # what the user sees as "loitering time": tick while outside zones,
        # zero while inside any zone.
        for obj_id in list(tracked.keys()):
            if obj_id not in self._entered_objects:
                continue
            curr_in = self._zone_manager.is_object_in_any_zone_polygon(obj_id)
            prev_in = self._object_in_any_zone.get(obj_id, False)
            if curr_in != prev_in:
                self._entered_objects[obj_id] = now
                self._object_in_any_zone[obj_id] = curr_in
                if curr_in:
                    self._stuck_objects.discard(obj_id)

        for oid in self._expired_this_frame:
            self._entered_objects.pop(oid, None)
            self._zone_occupied.pop(oid, None)
            self._object_in_any_zone.pop(oid, None)
            self._stuck_objects.discard(oid)

        for obj_id, entry_time in list(self._entered_objects.items()):
            # Stuck if the area timer ran past threshold AND the object is
            # currently outside every zone polygon. Polygon check (not
            # _zone_occupied) so a brief debounce window can't accidentally
            # mark a freshly-entering object stuck.
            if (now - entry_time >= threshold
                    and not self._object_in_any_zone.get(obj_id, False)):
                self._stuck_objects.add(obj_id)

        for obj_id in list(self._stuck_objects):
            if obj_id not in tracked:
                continue
            if self._zone_manager.is_object_in_any_zone_polygon(obj_id):
                self._stuck_objects.discard(obj_id)

        self.stuck_changed.emit(len(self._stuck_objects),
                                 sorted(self._stuck_objects))

        if self._stuck_objects:
            interval = area_config.get("reminder_seconds", 0)
            should_fire = False
            if not self._stuck_noti_sent:
                should_fire = True
                self._stuck_noti_sent = True
                self._last_noti_area = now
            elif interval > 0:
                if now - self._last_noti_area >= interval:
                    should_fire = True
                    self._last_noti_area = now

            if should_fire:
                class_counts: dict[str, int] = {}
                for oid in self._stuck_objects:
                    obj = tracked.get(oid)
                    if obj:
                        class_counts[obj.class_name] = (
                            class_counts.get(obj.class_name, 0) + 1)
                class_summary = ", ".join(
                    f"{c} {n}" for n, c in class_counts.items())
                stuck_ev = Event(
                    timestamp=now, event_type="stuck",
                    region_id="stuck_group", region_name="Area",
                    object_id=-1, class_name="",
                    details=f"{class_summary} stuck")
                self._log_event(stuck_ev)

        if self._stuck_noti_sent and not self._stuck_objects:
            self._stuck_noti_sent = False
            self.event_logged.emit(
                f"{time.strftime('%H:%M:%S')} | CLEARED | Area overstay cleared",
                "#00cc66")

    # ───────────────────── zone events ─────────────────────

    def _check_zone_events(self, tracked: dict, now: float) -> list[Event]:
        zone_rules = self._zone_rules
        if not zone_rules:
            return []

        events: list[Event] = []
        rules_by_zone = {r["zone_id"]: r for r in zone_rules if r["enabled"]}

        for obj_id, obj in tracked.items():
            for zone in self._monitor.zones:
                if zone.id not in rules_by_zone:
                    continue
                rule = rules_by_zone[zone.id]
                if rule["target_classes"] and obj.class_name not in rule["target_classes"]:
                    continue

                # Use zone_manager's debounced view so a centroid wobble
                # doesn't reset the overstay timer / displayed dwell counter.
                # zone_manager.update() ran earlier in this frame, so the
                # state is already current.
                inside = self._zone_manager.is_in_zone_for_overstay(
                    obj_id, zone.id)
                key = (obj_id, zone.id)

                if inside:
                    if key not in self._zone_entry_times:
                        self._zone_entry_times[key] = now
                    else:
                        elapsed_t = now - self._zone_entry_times[key]
                        if rule["notify_overstay"] and elapsed_t >= rule["max_seconds"]:
                            interval = rule.get("overstay_reminder", 0)
                            should_fire = False
                            reminder_count = 0

                            if key not in self._zone_overstay_notified:
                                self._zone_overstay_notified.add(key)
                                self._zone_overstay_last_time[key] = now
                                self._zone_overstay_reminder_count[key] = 0
                                should_fire = True
                            elif interval > 0:
                                last = self._zone_overstay_last_time.get(key, 0)
                                current_count = (
                                    self._zone_overstay_reminder_count.get(key, 0))
                                if current_count < 2 and now - last >= interval:
                                    reminder_count = current_count + 1
                                    self._zone_overstay_reminder_count[key] = reminder_count
                                    self._zone_overstay_last_time[key] = now
                                    should_fire = True

                            if should_fire:
                                events.append(Event(
                                    timestamp=now,
                                    event_type="zone_overstay",
                                    region_id=zone.id, region_name=zone.name,
                                    object_id=obj_id, class_name=obj.class_name,
                                    details=(f"Duration: {int(elapsed_t)}s "
                                             f"(limit: {rule['max_seconds']}s)"),
                                    reminder_count=reminder_count))
                else:
                    self._zone_entry_times.pop(key, None)
                    self._zone_overstay_notified.discard(key)
                    self._zone_overstay_last_time.pop(key, None)
                    self._zone_overstay_reminder_count.pop(key, None)

        for k in [k for k in self._zone_entry_times if k[0] in self._expired_this_frame]:
            self._zone_entry_times.pop(k, None)
            self._zone_overstay_notified.discard(k)
            self._zone_overstay_last_time.pop(k, None)
            self._zone_overstay_reminder_count.pop(k, None)

        if self._overstay_noti_sent and not self._zone_overstay_notified:
            self._overstay_noti_sent = False
            self.event_logged.emit(
                f"{time.strftime('%H:%M:%S')} | CLEARED | Zone overstays cleared",
                "#00cc66")
        if self._zone_overstay_notified:
            self._overstay_noti_sent = True

        return events

    # ───────────────────── reset ─────────────────────

    def _reset_all_state(self):
        self._entered_objects.clear()
        self._zone_occupied.clear()
        self._stuck_objects.clear()
        self._stuck_noti_sent = False
        self.stuck_changed.emit(0, [])
        self._object_in_any_zone.clear()
        self._session_start_pairs.clear()
        self._zone_entry_times.clear()
        self._zone_overstay_notified.clear()
        self._overstay_noti_sent = False
        self._object_states.clear()
        self._prev_centroids.clear()
        self._tracked_bboxes.clear()
        self._object_first_seen.clear()
        self._obj_last_seen.clear()
        self._expired_this_frame = set()
        self._display_id_map.clear()
        self._next_display_id = 1
        self._last_noti_line = 0.0
        self._last_noti_area = 0.0
        self._last_noti_zone_enter.clear()
        self._last_noti_zone_exit.clear()
        self._zone_overstay_last_time.clear()
        self._zone_overstay_reminder_count.clear()
