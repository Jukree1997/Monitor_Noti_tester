"""Update-notification system — asks GitHub Releases "is there a newer
version of this app?" on startup, emits a signal that MainWindow turns
into a non-modal "Update available" dialog.

Designed as a self-contained module: to drop into another tool, change
only the two constants at the top (GITHUB_REPO + PRODUCT_NAME) and add
QSettings org/app name in that tool's main.py.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from PySide6.QtCore import QObject, QSettings, QThread, Signal, Slot
from packaging.version import InvalidVersion, parse as parse_version

from core.version import __version__


# ======================================
# -------- 0. CONSTANTS --------
# ======================================

GITHUB_REPO = "Jukree1997/Monitor_Noti_tester"
PRODUCT_NAME = "Baksters Notification Runner"

_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_REQUEST_TIMEOUT_S = 5
_AUTO_CHECK_INTERVAL = timedelta(hours=24)
_RELEASE_NOTES_PREVIEW_CHARS = 500

# QSettings keys (org "Baksters", app "MNT" — set in main.py)
_QS_LAST_CHECK = "updater/last_check_utc"
_QS_DISMISSED = "updater/dismissed_versions"


# ======================================
# -------- 1. WORKER (network thread) --------
# ======================================

class _CheckWorker(QObject):
    """Runs the HTTP call off the GUI thread. Emits a single `finished`
    signal carrying a result-kind tag + payload dict."""

    # kind ∈ {"update_available", "no_update", "error"}
    finished = Signal(str, dict)

    @Slot()
    def run(self):
        try:
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": f"{PRODUCT_NAME}/{__version__}",
            }
            resp = requests.get(
                _API_URL, headers=headers, timeout=_REQUEST_TIMEOUT_S
            )
        except requests.RequestException as exc:
            self.finished.emit("error", {"reason": str(exc)})
            return

        # 404 = repo exists but no releases yet. Treat as "no update".
        if resp.status_code == 404:
            self.finished.emit("no_update", {})
            return
        if resp.status_code != 200:
            self.finished.emit(
                "error", {"reason": f"HTTP {resp.status_code}"}
            )
            return

        try:
            data = resp.json()
            latest_tag = str(data["tag_name"])
            release_url = str(data.get("html_url", ""))
            release_notes = str(data.get("body", "") or "")
        except (ValueError, KeyError) as exc:
            self.finished.emit(
                "error", {"reason": f"unexpected response: {exc}"}
            )
            return

        latest_version = latest_tag.lstrip("v").strip()
        try:
            is_newer = parse_version(latest_version) > parse_version(__version__)
        except InvalidVersion:
            self.finished.emit(
                "error",
                {"reason": f"unparseable version tag: {latest_tag}"},
            )
            return

        if not is_newer:
            self.finished.emit("no_update", {})
            return

        self.finished.emit(
            "update_available",
            {
                "version": latest_version,
                "url": release_url,
                "notes": release_notes,
            },
        )


# ======================================
# -------- 2. PUBLIC API --------
# ======================================

class UpdateChecker(QObject):
    """Non-blocking GitHub Releases version check.

    Usage from MainWindow:
        self._updater = UpdateChecker(self)
        self._updater.update_available.connect(self._on_update_available)
        self._updater.no_update_available.connect(self._on_no_update_manual)
        self._updater.check_failed.connect(self._on_update_check_failed)
        self._updater.check_async(force=False)   # quiet auto-check
    """

    update_available = Signal(str, str, str)   # version, url, notes
    no_update_available = Signal()              # for manual trigger UX
    check_failed = Signal(str)                  # reason; silent on auto-check

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = QSettings()
        # We MUST hold Python refs to thread + worker while a check is
        # in flight: worker has no QObject parent (so Qt's C++-side
        # parent ownership doesn't keep it alive), and locals going out
        # of scope would let PySide6 GC the wrapper, destroying the C++
        # worker mid-HTTP-call. The check_in_flight check uses
        # `self._thread is not None`, NOT a separate bool, so we always
        # reason about the actual ref state.
        self._thread: Optional[QThread] = None
        self._worker: Optional[_CheckWorker] = None
        # _force_mode is set by check_async() and read by the result
        # handler to decide whether to surface check_failed silently.
        self._force_mode = False

    # ---------- entry points ----------

    def check_async(self, force: bool = False) -> None:
        """Kick off a background check. `force=True` bypasses the 24h
        cache AND surfaces errors to the user (used by Help → Check for
        updates…). `force=False` is the silent startup check."""
        if self._thread is not None:
            # A check is already in flight — silently skip the duplicate.
            return
        if not force and self._within_cache_window():
            return

        self._force_mode = force
        self._thread = QThread(self)
        self._worker = _CheckWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_worker_finished)
        # Cleanup chain: worker done → quit thread → both objects scheduled
        # for deletion on the next event-loop tick. We still null out
        # `self._thread`/`self._worker` in `_on_worker_finished`'s finally
        # block so subsequent check_async calls work AND we don't hold
        # dangling Python wrappers pointing at deleteLater'd C++.
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def dismiss_version(self, version: str) -> None:
        """Persist a version the user clicked 'Skip this version' on, so
        we don't re-prompt about the same release on every launch."""
        version = version.strip()
        if not version:
            return
        dismissed = self._load_dismissed()
        if version not in dismissed:
            dismissed.append(version)
            self._settings.setValue(_QS_DISMISSED, json.dumps(dismissed))

    # ---------- internal ----------

    @Slot(str, dict)
    def _on_worker_finished(self, kind: str, payload: dict) -> None:
        try:
            # Cache the timestamp ONLY on no_update. If an update is
            # available, we want every launch to re-show the dialog
            # until the user explicitly dismisses it via Skip — so we
            # deliberately don't suppress the next-launch check.
            if kind == "no_update":
                self._settings.setValue(
                    _QS_LAST_CHECK, datetime.now(timezone.utc).isoformat()
                )

            if kind == "update_available":
                version = payload["version"]
                if version in self._load_dismissed() and not self._force_mode:
                    # Auto-check on launch + user previously skipped
                    # this version → silently respect the skip (don't
                    # nag). Manual "Check for updates…" falls through
                    # to re-show the dialog: clicking that menu item is
                    # a deliberate ask to see what's actually out there,
                    # so silencing it would be dishonest UX.
                    return
                notes = self._truncate_notes(payload["notes"], payload["url"])
                self.update_available.emit(version, payload["url"], notes)
            elif kind == "no_update":
                if self._force_mode:
                    self.no_update_available.emit()
            else:  # error
                # On auto-check, swallow silently (offline shouldn't nag).
                # On manual trigger, surface so the user knows the click
                # did something.
                if self._force_mode:
                    self.check_failed.emit(
                        payload.get("reason", "unknown error")
                    )
        finally:
            # Always release thread/worker refs, even on early return or
            # exception. Without this, the dismissed-version branch
            # would leave self._thread pointing at a dead C++ object and
            # the next check_async would think a check is in flight.
            self._thread = None
            self._worker = None
            self._force_mode = False

    def _within_cache_window(self) -> bool:
        raw = self._settings.value(_QS_LAST_CHECK, "")
        if not raw:
            return False
        try:
            last = datetime.fromisoformat(str(raw))
        except ValueError:
            return False
        return datetime.now(timezone.utc) - last < _AUTO_CHECK_INTERVAL

    def _load_dismissed(self) -> list[str]:
        raw = self._settings.value(_QS_DISMISSED, "[]")
        try:
            data = json.loads(str(raw))
            return [str(v) for v in data] if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    @staticmethod
    def _truncate_notes(notes: str, url: str) -> str:
        if len(notes) <= _RELEASE_NOTES_PREVIEW_CHARS:
            return notes
        return (
            notes[:_RELEASE_NOTES_PREVIEW_CHARS].rstrip()
            + f"\n\n…see full notes on the release page: {url}"
        )
