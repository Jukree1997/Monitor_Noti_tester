"""Manages a fleet of camera worker subprocesses on the UI side.

Spawns / tracks / stops one ``multiprocessing.Process`` per camera. Drains
each worker's outbound queue from the UI thread via a ``QTimer`` and re-emits
events as Qt signals carrying the camera_id.

The UI never touches ``multiprocessing.Queue`` directly — it talks to this
manager and receives signals.
"""
from __future__ import annotations
import multiprocessing as mp
import time
import uuid
from dataclasses import dataclass
from PySide6.QtCore import QObject, Signal, QTimer

from core.worker_process import (
    worker_main,
    MSG_STARTED, MSG_EVENT, MSG_NOTI, MSG_STATUS,
    MSG_THUMBNAIL, MSG_LIVE, MSG_ERROR, MSG_FINISHED,
    CMD_STOP, CMD_SET_STREAMING, CMD_SET_OVERRIDES,
)


# State strings (kept simple — UI compares directly).
S_STOPPED  = "stopped"
S_SPAWNING = "spawning"
S_RUNNING  = "running"
S_STOPPING = "stopping"
S_ERROR    = "error"


@dataclass
class _CameraWorker:
    camera_id: str
    project_path: str
    state: str
    test_mode: bool
    process: mp.Process | None = None
    in_queue: "mp.Queue | None" = None
    out_queue: "mp.Queue | None" = None


class FleetWorkerManager(QObject):
    """Aggregates events from N worker subprocesses into Qt signals.

    Signals are all keyed by camera_id (str) so a single UI slot can multiplex
    across cameras. The UI's tile widget(s) filter on their own id.
    """

    camera_started     = Signal(str, dict)   # cam_id, payload (e.g. {"device": "..."})
    camera_event       = Signal(str, str, str)   # cam_id, text, color
    camera_noti_result = Signal(str, str, bool)  # cam_id, text, success
    camera_status      = Signal(str, dict)       # cam_id, payload
    camera_thumbnail   = Signal(str, bytes)      # cam_id, jpeg
    camera_live_frame  = Signal(str, bytes)      # cam_id, jpeg
    camera_error       = Signal(str, str)        # cam_id, msg
    camera_finished    = Signal(str, str)        # cam_id, reason

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        # Force spawn — required for CUDA + PySide6 safety on all platforms.
        # If the start method has already been set elsewhere this raises
        # RuntimeError; that's fine, we just need it to be 'spawn'.
        try:
            mp.set_start_method("spawn", force=False)
        except RuntimeError:
            pass

        self._workers: dict[str, _CameraWorker] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(50)  # 20 Hz
        self._poll.timeout.connect(self._drain_outbound)
        self._poll.start()

    # ───────── public API ─────────

    def add_camera(self, project_path: str, test_mode: bool = False) -> str:
        cam_id = f"cam_{uuid.uuid4().hex[:8]}"
        self._workers[cam_id] = _CameraWorker(
            camera_id=cam_id, project_path=project_path,
            state=S_STOPPED, test_mode=test_mode,
        )
        return cam_id

    def remove_camera(self, camera_id: str):
        worker = self._workers.get(camera_id)
        if not worker:
            return
        if worker.state in (S_SPAWNING, S_RUNNING, S_STOPPING):
            self.stop_camera(camera_id)
            # Caller should usually wait for camera_finished before remove.
        self._workers.pop(camera_id, None)

    def start_camera(self, camera_id: str, test_mode: bool | None = None,
                     display_opts: "dict | None" = None):
        worker = self._workers.get(camera_id)
        if not worker:
            return
        if worker.state in (S_SPAWNING, S_RUNNING):
            return
        if test_mode is not None:
            worker.test_mode = test_mode
        worker.in_queue = mp.Queue()
        worker.out_queue = mp.Queue()
        worker.process = mp.Process(
            target=worker_main,
            args=(worker.project_path, worker.in_queue, worker.out_queue,
                  worker.test_mode, display_opts),
            daemon=True,
            name=f"worker-{camera_id}",
        )
        worker.state = S_SPAWNING
        worker.process.start()

    def stop_camera(self, camera_id: str):
        worker = self._workers.get(camera_id)
        if not worker or worker.state == S_STOPPED:
            return
        if worker.in_queue is not None:
            try:
                worker.in_queue.put_nowait((CMD_STOP, {}))
            except Exception:
                pass
        worker.state = S_STOPPING

    def set_streaming(self, camera_id: str, enabled: bool):
        worker = self._workers.get(camera_id)
        if not worker or worker.in_queue is None:
            return
        try:
            worker.in_queue.put_nowait(
                (CMD_SET_STREAMING, {"enabled": bool(enabled)}))
        except Exception:
            pass

    def set_overrides(self, camera_id: str, overrides: dict):
        worker = self._workers.get(camera_id)
        if not worker or worker.in_queue is None:
            return
        try:
            worker.in_queue.put_nowait((CMD_SET_OVERRIDES, dict(overrides)))
        except Exception:
            pass

    def list_cameras(self) -> list[dict]:
        return [{"id": w.camera_id, "project_path": w.project_path,
                 "state": w.state, "test_mode": w.test_mode}
                for w in self._workers.values()]

    def camera_state(self, camera_id: str) -> str:
        w = self._workers.get(camera_id)
        return w.state if w else "unknown"

    def running_count(self) -> int:
        return sum(1 for w in self._workers.values()
                   if w.state in (S_SPAWNING, S_RUNNING, S_STOPPING))

    def shutdown(self, timeout_s: float = 5.0):
        """Stop all workers and wait for them to exit. Force-terminate stragglers."""
        self._poll.stop()
        for cam_id in list(self._workers.keys()):
            self.stop_camera(cam_id)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            still_alive = [w for w in self._workers.values()
                           if w.process is not None and w.process.is_alive()]
            if not still_alive:
                break
            time.sleep(0.1)

        for w in self._workers.values():
            if w.process is not None and w.process.is_alive():
                try:
                    w.process.terminate()
                    w.process.join(2)
                except Exception:
                    pass
            w.state = S_STOPPED

    # ───────── outbound drain ─────────

    def _drain_outbound(self):
        for cam_id, worker in list(self._workers.items()):
            if worker.out_queue is not None:
                # Cap per-tick so a flood from one camera can't starve UI.
                for _ in range(64):
                    try:
                        kind, payload = worker.out_queue.get_nowait()
                    except Exception:
                        break
                    self._dispatch(cam_id, worker, kind, payload)

            # Reap exited processes
            if worker.process is not None and not worker.process.is_alive():
                if worker.state not in (S_STOPPED, S_ERROR):
                    worker.state = S_STOPPED
                    self.camera_finished.emit(cam_id, "process_exited")

    def _dispatch(self, cam_id, worker, kind, payload):
        if kind == MSG_STARTED:
            worker.state = S_RUNNING
            self.camera_started.emit(cam_id, payload)
        elif kind == MSG_EVENT:
            self.camera_event.emit(
                cam_id, payload.get("text", ""), payload.get("color", "#888"))
        elif kind == MSG_NOTI:
            self.camera_noti_result.emit(
                cam_id, payload.get("text", ""), bool(payload.get("success", False)))
        elif kind == MSG_STATUS:
            self.camera_status.emit(cam_id, payload)
        elif kind == MSG_THUMBNAIL:
            self.camera_thumbnail.emit(cam_id, payload.get("jpeg", b""))
        elif kind == MSG_LIVE:
            self.camera_live_frame.emit(cam_id, payload.get("jpeg", b""))
        elif kind == MSG_ERROR:
            worker.state = S_ERROR
            self.camera_error.emit(cam_id, payload.get("msg", ""))
        elif kind == MSG_FINISHED:
            # Worker emitted finished; actual reaping happens on next tick.
            self.camera_finished.emit(cam_id, payload.get("reason", "finished"))
