from __future__ import annotations
import time
import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Slot
from core.video_source import VideoSource


class DetectionWorker(QObject):
    """Runs YOLO inference + ByteTrack tracking loop on a background thread."""

    frame_ready = Signal(np.ndarray, object, float, float)  # frame, result, inference_ms, frame_time
    error = Signal(str)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self._running = False
        self._model = None
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

    def set_model(self, model):
        self._model = model

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
        """Restrict YOLO to these class IDs. None (or empty) = no filter."""
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
            if self._source is None or self._model is None:
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
                track_kwargs = dict(
                    imgsz=self._imgsz,
                    conf=self._conf,
                    iou=self._iou,
                    max_det=10000,
                    verbose=False,
                    tracker="bytetrack.yaml",
                    persist=True,
                )
                if self._classes is not None:
                    track_kwargs["classes"] = self._classes
                results = self._model.track(frame, **track_kwargs)
                dt = (time.perf_counter() - t0) * 1000  # ms
                result = results[0] if results else None
                self.frame_ready.emit(frame, result, dt, frame_time)
            except Exception as e:
                self.error.emit(str(e))

        self._running = False
        self.finished.emit()

    def stop(self):
        self._running = False


class DetectionEngine:
    """Manages the detection worker thread and model loading."""

    def __init__(self):
        self._thread: QThread | None = None
        self._worker: DetectionWorker | None = None
        self._model = None
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
        """Load YOLO model. Returns device info string."""
        from ultralytics import YOLO
        import torch

        self._model = YOLO(path)

        # Auto-detect CUDA
        if torch.cuda.is_available():
            self._device = "cuda"
            device_name = torch.cuda.get_device_name(0)
        else:
            self._device = "cpu"
            device_name = "CPU"

        # Warm-up inference
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        self._model(dummy, imgsz=640, verbose=False, device=self._device)

        self._model_names = self._model.names or {}
        return device_name

    def reset_tracker(self):
        """Reset ByteTrack state for a fresh tracking session."""
        if self._model and hasattr(self._model, "predictor") and self._model.predictor:
            if hasattr(self._model.predictor, "trackers"):
                for t in self._model.predictor.trackers:
                    t.reset()
            else:
                # Predictor exists from warm-up but has no trackers yet — clear it
                self._model.predictor = None

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
