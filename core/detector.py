# ======================================
# -------- 0. IMPORTS --------
# ======================================

from __future__ import annotations
import time
import numpy as np
import supervision as sv
from PySide6.QtCore import QObject, QThread, Signal, Slot
from trackers import ByteTrackTracker

from core.video_source import VideoSource
from core.onnx_runtime import OnnxYoloDetector


# ======================================
# -------- 1. RESULT ADAPTERS --------
# ======================================
# The runner reads tracking output as `result.boxes.xyxy.cpu().numpy()` —
# the ultralytics shape. Phase 1 of ultralytics-replacement.md splits the
# detection (OnnxYoloDetector) from tracking (ByteTrackTracker), so we
# rebuild a tiny duck-typed surface here so runner.py keeps working
# unchanged.

class _ArrayProxy:
    """Mimics `torch.Tensor.cpu().numpy()` on a numpy array."""

    def __init__(self, arr: np.ndarray):
        self._arr = arr

    def cpu(self):
        return self

    def numpy(self):
        return self._arr


class _BoxesView:
    """Bulk-array view over the detections kept by the runner: .xyxy, .cls,
    .id, .conf as torch-tensor-shaped accessors. `.id` is None when no
    track survived (mirrors ultralytics' "no tracker" behavior)."""

    def __init__(
        self,
        xyxy: np.ndarray,
        cls: np.ndarray,
        ids: np.ndarray | None,
        conf: np.ndarray,
    ):
        self.xyxy = _ArrayProxy(xyxy)
        self.cls = _ArrayProxy(cls)
        self.id = _ArrayProxy(ids) if ids is not None else None
        self.conf = _ArrayProxy(conf)

    def __len__(self):
        return self.xyxy._arr.shape[0]


class _TrackResult:
    """Per-frame detection + tracking output, shaped like an ultralytics
    Results object so runner._on_frame_ready needs no changes. An empty
    result has `.boxes` with len() == 0, never None."""

    def __init__(
        self,
        xyxy: np.ndarray | None = None,
        cls: np.ndarray | None = None,
        ids: np.ndarray | None = None,
        conf: np.ndarray | None = None,
    ):
        if xyxy is None or len(xyxy) == 0:
            self.boxes = _BoxesView(
                np.zeros((0, 4), dtype=np.float32),
                np.zeros(0, dtype=int),
                None,
                np.zeros(0, dtype=np.float32),
            )
        else:
            self.boxes = _BoxesView(xyxy, cls, ids, conf)


# ======================================
# -------- 2. DETECTION WORKER --------
# ======================================

class DetectionWorker(QObject):
    """Runs ONNX inference + ByteTrack tracking loop on a background thread."""

    frame_ready = Signal(np.ndarray, object, float, float)  # frame, result, inference_ms, frame_time
    error = Signal(str)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self._running = False
        self._model: OnnxYoloDetector | None = None
        self._tracker: ByteTrackTracker | None = None
        self._source: VideoSource | None = None
        self._conf = 0.40
        self._iou = 0.45
        self._imgsz = 640
        self._classes: list[int] | None = None
        # Playback pacing for file sources. "normal" sleeps between frames
        # so a 30 fps file plays at wall-clock 30 fps regardless of how
        # fast the GPU runs inference (so demo recordings show the actual
        # video tempo). "process" runs flat-out (legacy behavior — useful
        # for crunching through long files quickly). Live sources (USB /
        # RTSP) ignore this — the camera dictates the pace.
        self._playback_mode: str = "normal"
        self._pace_anchor_perf: float | None = None
        self._pace_anchor_frame: int = 0
        self._frames_read: int = 0

    def set_model(self, model: OnnxYoloDetector):
        self._model = model

    def set_tracker(self, tracker: ByteTrackTracker):
        self._tracker = tracker

    def set_source(self, source: VideoSource):
        self._source = source

    def set_params(self, conf: float = None, iou: float = None, imgsz: int = None):
        if conf is not None:
            self._conf = conf
        if iou is not None:
            self._iou = iou
        if imgsz is not None:
            self._imgsz = imgsz

    def set_classes(self, classes: list[int] | None):
        """Restrict detection to these class IDs. None (or empty) = no filter."""
        self._classes = classes if classes else None

    def set_playback_mode(self, mode: str):
        """Switch between 'normal' (paced to source FPS for files) and
        'process' (run as fast as inference allows). No-op for live sources."""
        if mode not in ("normal", "process") or mode == self._playback_mode:
            return
        self._playback_mode = mode
        # Rebuild the pacing anchor on the next frame so a mid-stream
        # toggle doesn't interpret the time elapsed in the other mode as
        # accumulated lag (which would skip the next sleeps entirely).
        self._pace_anchor_perf = None

    @Slot()
    def run(self):
        """Main detection + tracking loop — runs on QThread."""
        self._running = True
        self._pace_anchor_perf = None
        self._pace_anchor_frame = 0
        self._frames_read = 0
        while self._running:
            if self._source is None or self._model is None or self._tracker is None:
                break

            ret, frame = self._source.read()
            if not ret or frame is None:
                if not self._source.is_live:
                    break
                continue
            self._frames_read += 1

            # Capture per-frame timestamp (video time for files, wall-clock
            # for live). Sampled immediately after read() so it reflects when
            # the frame was captured, not when the UI thread processes it.
            frame_time = self._source.video_time

            # File pacing: sleep so wall-clock playback tracks the source
            # FPS in "normal" mode. Live sources self-pace, so skip them.
            if (self._playback_mode == "normal"
                    and not self._source.is_live
                    and self._source.fps > 0):
                now_perf = time.perf_counter()
                if self._pace_anchor_perf is None:
                    self._pace_anchor_perf = now_perf
                    self._pace_anchor_frame = self._frames_read
                else:
                    frames_since = self._frames_read - self._pace_anchor_frame
                    target = (self._pace_anchor_perf
                              + frames_since / self._source.fps)
                    delay = target - now_perf
                    if delay > 0:
                        time.sleep(delay)

            # Drain buffered frames for live sources
            if self._source.is_live:
                self._source.grab()

            try:
                t0 = time.perf_counter()
                # Detect → track in two explicit steps. Was a single
                # ultralytics .track() call before Phase 1 split it.
                det_results = self._model.predict(
                    frame,
                    conf=self._conf, iou=self._iou,
                    classes=self._classes,
                    max_det=10000, imgsz=self._imgsz,
                    stream=False, verbose=False,
                )
                det_result = det_results[0] if det_results else None
                result = self._track(frame, det_result)
                dt = (time.perf_counter() - t0) * 1000  # ms
                self.frame_ready.emit(frame, result, dt, frame_time)
            except Exception as e:
                self.error.emit(str(e))

        self._running = False
        self.finished.emit()

    def _track(self, frame: np.ndarray, det_result) -> _TrackResult:
        """Feed the per-frame detections into ByteTrack and return the
        tracker's confirmed-track result. Detections that ByteTrack hasn't
        confirmed yet (tracker_id == -1) are dropped — the runner keys
        every state map on the obj_id, and -1 would collide across frames."""
        if det_result is None or not det_result.boxes:
            tracked = self._tracker.update(sv.Detections.empty())
        else:
            h, w = frame.shape[:2]
            xywhn = np.array([b.xywhn[0] for b in det_result.boxes],
                             dtype=np.float32)
            cls_arr = np.array([int(b.cls[0]) for b in det_result.boxes],
                               dtype=int)
            conf_arr = np.array([float(b.conf[0]) for b in det_result.boxes],
                                dtype=np.float32)
            cx = xywhn[:, 0] * w
            cy = xywhn[:, 1] * h
            bw = xywhn[:, 2] * w
            bh = xywhn[:, 3] * h
            xyxy_arr = np.stack([cx - bw / 2, cy - bh / 2,
                                 cx + bw / 2, cy + bh / 2], axis=1)
            dets = sv.Detections(
                xyxy=xyxy_arr, confidence=conf_arr, class_id=cls_arr)
            tracked = self._tracker.update(dets)

        if len(tracked) == 0:
            return _TrackResult()
        valid = tracked.tracker_id != -1
        if not valid.any():
            return _TrackResult()
        return _TrackResult(
            xyxy=tracked.xyxy[valid],
            cls=tracked.class_id[valid],
            ids=tracked.tracker_id[valid].astype(int),
            conf=(tracked.confidence[valid]
                  if tracked.confidence is not None
                  else np.zeros(int(valid.sum()), dtype=np.float32)),
        )

    def stop(self):
        self._running = False


# ======================================
# -------- 3. DETECTION ENGINE --------
# ======================================

class DetectionEngine:
    """Manages the detection worker thread, model, and tracker lifecycle."""

    def __init__(self):
        self._thread: QThread | None = None
        self._worker: DetectionWorker | None = None
        self._model: OnnxYoloDetector | None = None
        self._tracker: ByteTrackTracker | None = None
        self._model_names: dict[int, str] = {}
        self._device = "cpu"
        # Sticky default so a mode chosen before start() carries over to
        # the worker created by the next start() call.
        self._default_playback_mode: str = "normal"

    @property
    def model(self):
        return self._model

    @property
    def model_names(self) -> dict[int, str]:
        return self._model_names

    @property
    def device(self) -> str:
        return self._device

    def load_model(self, path: str) -> str:
        """Load the ONNX detector. Returns a device-name string for the UI."""
        self._model = OnnxYoloDetector(path)
        active = self._model._session.get_providers()[0]
        if active == "CUDAExecutionProvider":
            self._device = "cuda"
            try:
                import torch
                device_name = torch.cuda.get_device_name(0)
            except Exception:
                device_name = "CUDA"
        else:
            self._device = "cpu"
            device_name = "CPU"

        # Warm-up inference so the first real frame doesn't pay session
        # graph-compile latency.
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        self._model(dummy, imgsz=640, verbose=False)

        self._model_names = self._model.names or {}
        return device_name

    # Tuned ByteTrack parameters for RF-DETR-class detectors. The defaults
    # (lost_track_buffer=30, minimum_consecutive_frames=2) lose IDs too
    # easily on small/far objects whose box edges jitter under transformer
    # query competition — observed as inflated `lost_id_count` and false
    # "fresh enter" events on the same physical object in distant zones.
    # Doubling the lost-track buffer + raising the minimum-consecutive
    # threshold to 3 closed most of that gap without harming near-camera
    # tracking. YOLO behaviour is unchanged (its centroids don't jitter
    # enough to benefit from the looser association window).
    _TRACKER_KWARGS = dict(
        lost_track_buffer=60,
        minimum_consecutive_frames=3,
    )

    def reset_tracker(self):
        """Recreate the ByteTracker so the next session starts with no
        tracks. ByteTrackTracker has a .reset() method too, but recreating
        is simpler and avoids inheriting any obscure state from prior runs."""
        self._tracker = ByteTrackTracker(**self._TRACKER_KWARGS)

    def start(self, source: VideoSource, conf: float, iou: float, imgsz: int,
              on_frame, on_error, on_finished,
              classes: list[int] | None = None):
        """Start detection + tracking loop on a background thread."""
        if self._model is None:
            return

        self.stop()
        self.reset_tracker()

        self._worker = DetectionWorker()
        self._worker.set_model(self._model)
        self._worker.set_tracker(self._tracker)
        self._worker.set_source(source)
        self._worker.set_params(conf=conf, iou=iou, imgsz=imgsz)
        self._worker.set_classes(classes)
        self._worker.set_playback_mode(self._default_playback_mode)

        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.frame_ready.connect(on_frame)
        self._worker.error.connect(on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(on_finished)

        self._thread.start()

    def stop(self):
        if self._worker:
            self._worker.stop()
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        self._worker = None
        self._thread = None

    def update_params(self, conf: float = None, iou: float = None, imgsz: int = None):
        if self._worker:
            self._worker.set_params(conf=conf, iou=iou, imgsz=imgsz)

    def set_classes(self, classes: list[int] | None):
        """Update the class filter live while the detector is running."""
        if self._worker:
            self._worker.set_classes(classes)

    def set_playback_mode(self, mode: str):
        """Switch playback pacing for file sources. ``mode`` is 'normal'
        (paced to source FPS) or 'process' (no pacing). Stored on the
        worker; takes effect on the next frame. Default is 'normal'."""
        if self._worker:
            self._worker.set_playback_mode(mode)
        self._default_playback_mode = mode

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()
