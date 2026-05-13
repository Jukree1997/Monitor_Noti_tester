"""Headless pipeline runner — same detection/notification logic as the UI,
no widgets. Useful for running a saved project on a server, and the basis for
Fleet mode subprocesses (Phase 2).

Usage:
    python headless_main.py --project path/to/cam.json
    python headless_main.py --project path/to/cam.json --test
    python headless_main.py --project path/to/cam.json --duration 60

Test mode skips the actual S3/LINE send but still classifies events the same way
(useful for verifying a project's rules without spamming a LINE channel).
"""
from __future__ import annotations
import argparse
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QCoreApplication, QTimer

from core.detector import DetectionEngine
from core.video_source import VideoSource
from core.runner import PipelineRunner
from models.config_schema import ProjectConfig


def _build_source(project: ProjectConfig) -> VideoSource:
    src = project.source
    if src.type == "camera":
        return VideoSource(int(src.value))
    if src.type == "rtsp":
        return VideoSource(src.value)
    if src.type == "file":
        if not os.path.isfile(src.value):
            raise FileNotFoundError(f"Video file not found: {src.value}")
        return VideoSource(src.value)
    raise ValueError(f"Unknown source type: {src.type}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Headless pipeline runner")
    parser.add_argument("--project", required=True, help="Path to project .json")
    parser.add_argument("--test", action="store_true",
                        help="Test mode — skip the actual LINE/S3 send")
    parser.add_argument("--duration", type=int, default=0,
                        help="Auto-exit after N seconds (0 = run until source ends or Ctrl+C)")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.project):
        print(f"ERROR: project file not found: {args.project}", file=sys.stderr)
        return 2

    # License gate — same check as the GUI uses. A headless runner can't
    # pop UI to walk the user through activation, so on first launch the
    # customer MUST open the GUI once to activate. After that, the
    # cached license persists and headless invocations work.
    #
    # We construct a QCoreApplication early just so QSettings + the
    # LicenseManager's QObject base work. Doing this before any heavy
    # imports keeps startup fast when the license is invalid.
    from PySide6.QtCore import QCoreApplication
    _bootstrap_app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    _bootstrap_app.setOrganizationName("Baksters")
    _bootstrap_app.setApplicationName("MNT")

    from core.license import LicenseManager, LicenseState
    license_mgr = LicenseManager()
    if license_mgr.state not in (LicenseState.ACTIVE, LicenseState.OFFLINE_GRACE):
        if license_mgr.state == LicenseState.UNLICENSED:
            print("ERROR: this PC is not licensed.\n"
                  "Run the GUI app once to activate "
                  "(python main.py).", file=sys.stderr)
        elif license_mgr.state == LicenseState.EXPIRED:
            print("ERROR: license expired or offline grace period exceeded.\n"
                  "Open the GUI app and use Help → Check for updates / "
                  "License Info to re-validate.", file=sys.stderr)
        elif license_mgr.state == LicenseState.REVOKED:
            print("ERROR: license revoked. Contact support.", file=sys.stderr)
        return 4

    # Headless runs one project = one camera. Sanity-check the cap.
    max_cameras = int(license_mgr.entitlements.get("max_cameras") or 0)
    if max_cameras < 1:
        print(f"ERROR: license cap is {max_cameras} cameras; cannot run.",
              file=sys.stderr)
        return 4
    print(f"[headless] license: {license_mgr.entitlements.get('tier_name', '?')} "
          f"({max_cameras} cameras allowed)")

    project = ProjectConfig.load(args.project)
    print(f"[headless] project: {project.project_name or args.project}")
    print(f"[headless] source: {project.source.type} = {project.source.value}")
    print(f"[headless] model:  {project.model_path}")
    print(f"[headless] mode:   {'TEST (no LINE send)' if args.test else 'LIVE'}")

    engine = DetectionEngine()
    if not project.model_path or not os.path.isfile(project.model_path):
        print(f"ERROR: model file not found: {project.model_path}", file=sys.stderr)
        return 2
    device = engine.load_model(project.model_path)
    print(f"[headless] device: {device}")

    source = _build_source(project)
    if not source.open():
        print(f"ERROR: failed to open source: {project.source.value}", file=sys.stderr)
        return 3

    # Reuse the QCoreApplication created during the license-gate
    # bootstrap above. Qt enforces a single QCoreApplication per process.
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    class_name_to_id = {name: cid for cid, name in engine.model_names.items()}
    runner = PipelineRunner(engine=engine, project=project, source=source,
                             class_name_to_id=class_name_to_id)

    def _on_event(text: str, _color: str):
        # Strip Qt-friendly hex color, just print the text
        print(f"[event] {text}", flush=True)

    def _on_noti_result(text: str, success: bool):
        tag = "OK" if success else "FAIL"
        print(f"[noti:{tag}] {text}", flush=True)

    def _on_status(text: str):
        # Status line is high-volume (~1/frame). Print once per second by
        # rate-limiting via a flag on the closure.
        pass  # uncomment if you want raw status: print(f"[status] {text}")

    def _on_error(msg: str):
        print(f"[error] {msg}", file=sys.stderr, flush=True)

    def _on_finished():
        print("[headless] source ended", flush=True)
        app.quit()

    runner.event_logged.connect(_on_event)
    runner.noti_result.connect(_on_noti_result)
    runner.status_text.connect(_on_status)
    runner.error.connect(_on_error)
    runner.source_finished.connect(_on_finished)

    # Ctrl+C → clean stop
    def _sigint_handler(*_):
        print("\n[headless] interrupted, stopping…", flush=True)
        runner.stop()
        app.quit()
    signal.signal(signal.SIGINT, _sigint_handler)
    # Wake the Qt loop frequently so SIGINT is handled promptly on Windows.
    _wakeup = QTimer()
    _wakeup.start(200)
    _wakeup.timeout.connect(lambda: None)

    if args.duration > 0:
        QTimer.singleShot(args.duration * 1000, lambda: (runner.stop(), app.quit()))

    runner.start(test_mode=args.test)
    rc = app.exec()

    # Clean shutdown
    runner.wait_for_pending_noti(5000)
    if source.is_opened:
        source.release()
    print("[headless] exited cleanly", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
