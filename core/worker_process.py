"""Subprocess entrypoint for one camera in Fleet mode.

The parent ``FleetWorkerManager`` spawns this with ``multiprocessing.Process``;
each worker has its own QCoreApplication, DetectionEngine, and PipelineRunner.
IPC is two ``multiprocessing.Queue`` objects:

  * ``in_queue``  — UI → worker (commands)
  * ``out_queue`` — worker → UI (events / results)

All messages are tuples ``(kind: str, payload: dict)``.
"""
from __future__ import annotations

# ── Message kinds ───────────────────────────────────────────────
# worker → UI
MSG_STARTED   = "started"        # payload: {"device": str}
MSG_EVENT     = "event"          # payload: {"text": str, "color": str}
MSG_NOTI      = "noti_result"    # payload: {"text": str, "success": bool}
MSG_STATUS    = "status"         # payload: {"text": str}  (rate-limited)
MSG_THUMBNAIL = "thumbnail"      # payload: {"jpeg": bytes}  (Phase 2d)
MSG_LIVE      = "live_frame"     # payload: {"jpeg": bytes}  (only when streaming)
MSG_ERROR     = "error"          # payload: {"msg": str}
MSG_FINISHED  = "finished"       # payload: {"reason": str}

# UI → worker
CMD_STOP          = "stop"
CMD_SET_STREAMING = "set_streaming"   # payload: {"enabled": bool}
CMD_SET_OVERRIDES = "set_overrides"   # payload: dict for runner.set_runtime_overrides
CMD_SET_PLAYBACK  = "set_playback"    # payload: {"mode": "normal"|"process"} — no-op for live sources


def worker_main(project_path: str, in_queue, out_queue,
                test_mode: bool = False,
                display_opts: "dict | None" = None) -> int:
    """Run one camera in a child process. Imports happen lazily so the parent
    process never pays for PySide6 / torch / cv2 on its own import."""
    # ── path setup must happen BEFORE imports of project modules ──
    import os
    import sys
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    import time
    import queue as queue_mod

    from PySide6.QtCore import QCoreApplication, QTimer
    from core.detector import DetectionEngine
    from core.video_source import VideoSource
    from core.runner import PipelineRunner
    from models.config_schema import ProjectConfig

    def _put(kind: str, payload: dict):
        try:
            out_queue.put((kind, payload))
        except Exception:
            # Parent's queue may be torn down during shutdown; swallow.
            pass

    # ── load project + model + source ──
    try:
        project = ProjectConfig.load(project_path)
    except Exception as e:
        _put(MSG_ERROR, {"msg": f"Failed to load project: {e}"})
        _put(MSG_FINISHED, {"reason": "load_error"})
        return 1

    if not project.model_path or not os.path.isfile(project.model_path):
        _put(MSG_ERROR, {"msg": f"Model not found: {project.model_path}"})
        _put(MSG_FINISHED, {"reason": "model_missing"})
        return 2

    engine = DetectionEngine()
    try:
        device = engine.load_model(project.model_path)
    except Exception as e:
        _put(MSG_ERROR, {"msg": f"Model load failed: {e}"})
        _put(MSG_FINISHED, {"reason": "model_load_error"})
        return 3

    src_cfg = project.source
    try:
        if src_cfg.type == "camera":
            source = VideoSource(int(src_cfg.value))
        elif src_cfg.type == "rtsp":
            source = VideoSource(src_cfg.value)
        elif src_cfg.type == "file":
            if not os.path.isfile(src_cfg.value):
                _put(MSG_ERROR, {"msg": f"Video file not found: {src_cfg.value}"})
                _put(MSG_FINISHED, {"reason": "source_missing"})
                return 4
            source = VideoSource(src_cfg.value)
        else:
            _put(MSG_ERROR, {"msg": f"Unknown source type: {src_cfg.type}"})
            _put(MSG_FINISHED, {"reason": "source_bad_type"})
            return 5
    except Exception as e:
        _put(MSG_ERROR, {"msg": f"Source init failed: {e}"})
        _put(MSG_FINISHED, {"reason": "source_init_error"})
        return 5

    if not source.open():
        _put(MSG_ERROR, {"msg": f"Failed to open source: {src_cfg.value}"})
        _put(MSG_FINISHED, {"reason": "source_open_failed"})
        return 6

    # ── runner + Qt event loop ──
    app = QCoreApplication([])
    class_name_to_id = {name: cid for cid, name in engine.model_names.items()}
    runner = PipelineRunner(engine=engine, project=project, source=source,
                             class_name_to_id=class_name_to_id)

    # Mutable closure state — accessible from runner-signal lambdas.
    state = {"streaming": False, "stopping": False,
             "last_status_emit": 0.0, "first_thumb_sent": False}

    def _on_event(text: str, color: str):
        _put(MSG_EVENT, {"text": text, "color": color})

    def _on_noti(text: str, success: bool):
        _put(MSG_NOTI, {"text": text, "success": bool(success)})

    def _on_status(text: str):
        # Rate-limit: status fires once per frame (~30 Hz). Cap at ~2 Hz so the
        # queue never builds up if the UI lags.
        now = time.perf_counter()
        if now - state["last_status_emit"] < 0.5:
            return
        state["last_status_emit"] = now
        _put(MSG_STATUS, {"text": text})

    def _on_frame(frame, _result, render_data):
        import cv2  # local — avoids unconditional cost in idle workers
        # Initial snapshot — send the first detected frame as a thumbnail so
        # tiles aren't blank for cameras that don't trigger any noti yet.
        if not state["first_thumb_sent"]:
            state["first_thumb_sent"] = True
            try:
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                if ok:
                    _put(MSG_THUMBNAIL, {"jpeg": bytes(buf)})
            except Exception:
                pass

        # Live-frame stream is only emitted while UI has explicitly opted in
        # for this camera (user is viewing the full-screen for that tile).
        if not state["streaming"]:
            return
        try:
            annotated = runner.build_live_overlay_frame(frame, render_data)
            ok, buf = cv2.imencode(".jpg", annotated,
                                    [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                _put(MSG_LIVE, {"jpeg": bytes(buf)})
        except Exception:
            pass

    def _on_noti_frame(jpeg_bytes: bytes):
        # Update the tile thumbnail to the noti frame (test-mode and live-mode
        # both fire this — runner emits regardless of mode when noti would fire).
        _put(MSG_THUMBNAIL, {"jpeg": jpeg_bytes})

    def _on_error(msg: str):
        _put(MSG_ERROR, {"msg": msg})

    def _on_source_finished():
        _put(MSG_FINISHED, {"reason": "source_ended"})
        state["stopping"] = True
        QTimer.singleShot(100, app.quit)

    runner.event_logged.connect(_on_event)
    runner.noti_result.connect(_on_noti)
    runner.status_text.connect(_on_status)
    runner.frame_ready.connect(_on_frame)
    runner.noti_frame_built.connect(_on_noti_frame)
    runner.error.connect(_on_error)
    runner.source_finished.connect(_on_source_finished)

    def _drain_inbound():
        if state["stopping"]:
            return
        try:
            while True:
                kind, payload = in_queue.get_nowait()
                if kind == CMD_STOP:
                    state["stopping"] = True
                    runner.stop()
                    QTimer.singleShot(500, app.quit)
                    return
                if kind == CMD_SET_STREAMING:
                    state["streaming"] = bool(payload.get("enabled", False))
                elif kind == CMD_SET_OVERRIDES:
                    runner.set_runtime_overrides(payload)
                elif kind == CMD_SET_PLAYBACK:
                    mode = payload.get("mode", "normal")
                    engine.set_playback_mode(mode)
        except queue_mod.Empty:
            pass

    drain_timer = QTimer()
    drain_timer.timeout.connect(_drain_inbound)
    drain_timer.start(50)

    if display_opts:
        runner.set_runtime_overrides({"display": dict(display_opts)})

    runner.start(test_mode=test_mode)
    _put(MSG_STARTED, {"device": device})

    rc = app.exec()

    # Clean teardown
    try:
        runner.wait_for_pending_noti(3000)
    except Exception:
        pass
    if source.is_opened:
        try:
            source.release()
        except Exception:
            pass
    _put(MSG_FINISHED, {"reason": "exited"})
    return rc
