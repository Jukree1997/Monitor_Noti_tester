from __future__ import annotations
import time
import cv2
import platform


class VideoSource:
    """Unified video capture: USB camera, RTSP stream, or video file."""

    def __init__(self, source: str | int):
        self._source = source
        self._cap: cv2.VideoCapture | None = None
        self._is_live = False
        self._frame_index = 0  # frames successfully read (file sources only)
        self._fps_cached = 0.0
        # Wall-clock anchor captured at open() for file sources. video_time
        # adds content-seconds (frame_index / fps) on top so dwell deltas
        # reflect what happened in the video — not how fast we processed it.
        self._opened_at = 0.0

    def open(self) -> bool:
        if isinstance(self._source, int):
            # USB camera — use DirectShow on Windows
            backend = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_ANY
            self._cap = cv2.VideoCapture(self._source, backend)
            self._is_live = True
        elif isinstance(self._source, str) and self._source.lower().startswith(("rtsp://", "rtmp://", "http://", "https://")):
            self._cap = cv2.VideoCapture(self._source)
            self._is_live = True
        else:
            # Video file
            self._cap = cv2.VideoCapture(self._source)
            self._is_live = False

        opened = self._cap is not None and self._cap.isOpened()
        if opened:
            self._fps_cached = self._cap.get(cv2.CAP_PROP_FPS) or 0.0
            self._frame_index = 0
            self._opened_at = time.time()
        return opened

    def read(self) -> tuple[bool, any]:
        if self._cap is None:
            return False, None
        ret, frame = self._cap.read()
        if ret and not self._is_live:
            self._frame_index += 1
        return ret, frame

    @property
    def video_time(self) -> float:
        """Wall-clock-shaped timer that increments with video content for file
        sources and with real time for live sources. For files, returns
        ``opened_at + frame_index/fps`` so deltas reflect *content* seconds
        (a 15-min video always reports 15 min of dwell, regardless of how
        fast the GPU processes it) while the absolute value still looks like
        a real timestamp in the report. For live sources, returns
        ``time.time()`` because real-world seconds *are* dwell seconds."""
        if self._cap is None or self._is_live:
            return time.time()
        if self._fps_cached > 0:
            return self._opened_at + self._frame_index / self._fps_cached
        pos_ms = self._cap.get(cv2.CAP_PROP_POS_MSEC)
        if pos_ms and pos_ms > 0:
            return self._opened_at + pos_ms / 1000.0
        return time.time()

    def grab(self):
        """Drain buffered frames for live sources to reduce latency."""
        if self._cap and self._is_live:
            self._cap.grab()

    def release(self):
        if self._cap:
            self._cap.release()
            self._cap = None

    @property
    def is_live(self) -> bool:
        return self._is_live

    @property
    def resolution(self) -> tuple[int, int]:
        if self._cap:
            w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            return (w, h)
        return (0, 0)

    @property
    def fps(self) -> float:
        if self._cap:
            return self._cap.get(cv2.CAP_PROP_FPS)
        return 0.0

    @property
    def is_opened(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    @staticmethod
    def detect_usb_cameras(max_index: int = 10) -> list[int]:
        """Scan for available USB camera indices.

        On Linux, restrict to indices that have a `/dev/videoN` device
        AND are real capture devices — `cv2.CAP_ANY` falls through every
        backend (V4L2/FFMPEG/GStreamer) for each missing index and
        spams the terminal with warnings."""
        available = []
        system = platform.system()
        if system == "Linux":
            import os
            backend = cv2.CAP_V4L2
            candidate_indices = [
                i for i in range(max_index)
                if os.path.exists(f"/dev/video{i}")
            ]
        elif system == "Windows":
            backend = cv2.CAP_DSHOW
            candidate_indices = list(range(max_index))
        else:
            backend = cv2.CAP_ANY
            candidate_indices = list(range(max_index))

        for i in candidate_indices:
            cap = cv2.VideoCapture(i, backend)
            if cap.isOpened():
                ret, _ = cap.read()
                if ret:
                    available.append(i)
                cap.release()
        return available
