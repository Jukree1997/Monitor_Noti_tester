"""License manager — Keygen.sh client + local state machine.

Public surface used by main.py / headless_main.py / MainWindow:
  - LicenseState enum
  - LicenseManager(QObject)
      .state                — current LicenseState
      .entitlements         — dict: max_cameras, max_pcs, tier_name, expiry
      .machine_id           — Keygen machine UUID (for deactivate)
      .activate(key)        — first-time activation (sync, called from dialog)
      .deactivate()         — release the seat back to Keygen
      .revalidate_async()   — periodic online check; runs on a QThread
  - Signals: state_changed(LicenseState), validation_failed(str)

Designed as a self-contained module: porting to another tool only
requires changing the three constants at the top (account ID, public
key, product ID).
"""
from __future__ import annotations
import enum
import platform
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from core import secure_storage
from core.fingerprint import get_fingerprint


# ======================================
# -------- 0. CONSTANTS --------
# ======================================

# Three values from the Keygen dashboard. The only things to change
# when porting this module to another tool.
KEYGEN_ACCOUNT_ID = "f550b6f4-66ba-42bd-b5ae-ab526a25d1c1"
PRODUCT_ID        = "e2ee601a-be46-4f72-b979-0ddf5a6ecd2f"
KEYGEN_PUBLIC_KEY = "0da0ffd17ed2573a5c7aaf094f0ef5f678af6ea14f188ffaaffca8571d54c4e1"

_API_BASE = f"https://api.keygen.sh/v1/accounts/{KEYGEN_ACCOUNT_ID}"
_HTTP_TIMEOUT_S = 10

# How long the license can run offline before refusing to start. Matches
# the policy's checkInInterval (30 days) so we never block a customer
# while Keygen would still accept them.
_OFFLINE_GRACE = timedelta(days=30)
# How often the background revalidate fires while the app is running.
_REVALIDATE_INTERVAL_MS = 24 * 60 * 60 * 1000  # 24h


# ======================================
# -------- 1. STATE --------
# ======================================

class LicenseState(enum.Enum):
    UNLICENSED    = "unlicensed"     # no cached token; needs activation
    ACTIVE        = "active"         # validated within the last check-in window
    OFFLINE_GRACE = "offline_grace"  # cached, online check failed but still within grace
    EXPIRED       = "expired"        # Keygen says license is expired (or grace exceeded)
    REVOKED       = "revoked"        # Keygen says license was revoked/suspended


# ======================================
# -------- 2. KEYGEN REST HELPERS --------
# ======================================

class _KeygenError(Exception):
    """Thrown on any Keygen API failure that isn't a clean validation result."""


def _headers(license_key: str) -> dict:
    return {
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
        "Authorization": f"License {license_key}",
    }


def _validate(license_key: str, fingerprint: str) -> dict:
    """POST validate with fingerprint scope. Returns the full parsed
    JSON (data + meta + included). Raises _KeygenError on transport
    failure; meta.valid/meta.code tell you the validation outcome."""
    url = f"{_API_BASE}/licenses/{license_key}/actions/validate"
    body = {"meta": {"scope": {"fingerprint": fingerprint, "product": PRODUCT_ID}}}
    # ?include=policy embeds the policy (and its metadata) in
    # response.included so we don't need a follow-up call to read
    # the camera cap.
    params = {"include": "policy"}
    try:
        r = requests.post(url, json=body, headers=_headers(license_key),
                          params=params, timeout=_HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        raise _KeygenError(f"network error: {exc}") from exc
    if r.status_code >= 500:
        raise _KeygenError(f"Keygen server error: HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as exc:
        raise _KeygenError(f"unparseable Keygen response: {exc}") from exc


def _create_machine(license_key: str, license_id: str,
                    fingerprint: str) -> dict:
    """POST a new machine bound to (license_id, fingerprint). Returns
    the created machine dict. Raises _KeygenError on failure (incl. seat
    cap reached — caller distinguishes via the HTTP code)."""
    url = f"{_API_BASE}/machines"
    body = {
        "data": {
            "type": "machines",
            "attributes": {
                "fingerprint": fingerprint,
                "platform": platform.system(),
                "name": platform.node() or "Unknown PC",
            },
            "relationships": {
                "license": {"data": {"type": "licenses", "id": license_id}},
            },
        }
    }
    try:
        r = requests.post(url, json=body, headers=_headers(license_key),
                          timeout=_HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        raise _KeygenError(f"network error: {exc}") from exc
    if r.status_code == 201:
        return r.json()
    # Surface Keygen's own error message — it's good ("MACHINE_LIMIT_EXCEEDED",
    # "FINGERPRINT_TAKEN", etc.)
    try:
        detail = r.json().get("errors", [{}])[0].get("detail", "")
        code = r.json().get("errors", [{}])[0].get("code", "")
    except (ValueError, KeyError, IndexError):
        detail = r.text[:200]
        code = ""
    raise _KeygenError(f"machine creation failed (HTTP {r.status_code}): "
                       f"{code or detail}")


def _delete_machine(license_key: str, machine_id: str) -> None:
    """DELETE the machine to free the seat. Idempotent — 404 is OK."""
    url = f"{_API_BASE}/machines/{machine_id}"
    try:
        r = requests.delete(url, headers=_headers(license_key),
                            timeout=_HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        raise _KeygenError(f"network error: {exc}") from exc
    if r.status_code not in (204, 404):
        raise _KeygenError(f"machine deletion failed: HTTP {r.status_code}")


# ======================================
# -------- 3. RESPONSE PARSING --------
# ======================================

def _extract_entitlements(validate_resp: dict) -> dict:
    """From a validate response (with ?include=policy), pull out the
    fields our app actually cares about. Returns a flat dict ready to
    persist via secure_storage.save()."""
    data = validate_resp.get("data") or {}
    attrs = data.get("attributes", {})
    license_id = data.get("id", "")
    expiry = attrs.get("expiry")  # null = perpetual

    # Find the included policy object
    policy = None
    for inc in validate_resp.get("included", []) or []:
        if inc.get("type") == "policies":
            policy = inc
            break
    if policy is None:
        # Validate didn't include policy — should never happen with
        # ?include=policy, but be defensive.
        tier_name = "Unknown"
        max_machines = 1
        max_cameras = 1
    else:
        p_attrs = policy.get("attributes", {})
        tier_name = p_attrs.get("name", "Unknown")
        max_machines = int(p_attrs.get("maxMachines") or 1)
        # Policy metadata is where we stash maxCameras (see RELEASING /
        # Keygen setup doc). Keep the lookup forgiving so a typo in
        # Keygen UI doesn't crash the app.
        meta = p_attrs.get("metadata") or {}
        try:
            max_cameras = int(meta.get("maxCameras") or 1)
        except (TypeError, ValueError):
            max_cameras = 1

    return {
        "license_id": license_id,
        "tier_name": tier_name,
        "max_machines": max_machines,
        "max_cameras": max_cameras,
        "expiry": expiry,
    }


# ======================================
# -------- 4. REVALIDATE WORKER --------
# ======================================

class _RevalidateWorker(QObject):
    """Runs the validate HTTP call off the GUI thread. Emits one
    `finished` signal with (kind, payload). Kinds: 'ok', 'invalid',
    'network_error'."""
    finished = Signal(str, dict)

    def __init__(self, license_key: str, fingerprint: str):
        super().__init__()
        self._license_key = license_key
        self._fingerprint = fingerprint

    @Slot()
    def run(self):
        try:
            resp = _validate(self._license_key, self._fingerprint)
        except _KeygenError as exc:
            self.finished.emit("network_error", {"reason": str(exc)})
            return
        meta = resp.get("meta") or {}
        if meta.get("valid"):
            self.finished.emit("ok", {"resp": resp})
        else:
            self.finished.emit("invalid",
                               {"code": meta.get("code", "UNKNOWN"),
                                "detail": meta.get("detail", "")})


# ======================================
# -------- 5. LICENSE MANAGER --------
# ======================================

class LicenseManager(QObject):
    """Owns license state across the app lifetime."""

    state_changed = Signal(object)        # emits LicenseState
    validation_failed = Signal(str)        # human-readable error

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._state = LicenseState.UNLICENSED
        self._entitlements: dict = {}
        self._license_key: str = ""
        self._machine_id: str = ""
        self._last_validated_utc: Optional[datetime] = None

        self._thread: Optional[QThread] = None
        self._worker: Optional[_RevalidateWorker] = None

        self._load_cache()

        # Periodic revalidation while the app is open. We deliberately
        # don't auto-revalidate on app startup here — main.py runs the
        # gate logic first; if state is ACTIVE/OFFLINE_GRACE, it lets
        # the app continue and the periodic timer below will catch any
        # remote state changes within 24h.
        self._timer = QTimer(self)
        self._timer.setInterval(_REVALIDATE_INTERVAL_MS)
        self._timer.timeout.connect(self.revalidate_async)

    # ---------- public properties ----------

    @property
    def state(self) -> LicenseState:
        return self._state

    @property
    def entitlements(self) -> dict:
        return dict(self._entitlements)   # caller-immutable copy

    @property
    def machine_id(self) -> str:
        return self._machine_id

    @property
    def license_key(self) -> str:
        return self._license_key

    # ---------- activation (sync — called from dialog) ----------

    def activate(self, license_key: str) -> bool:
        """First-time activation. Returns True on success, False on
        failure (emits validation_failed with the reason)."""
        license_key = license_key.strip()
        if not license_key:
            self.validation_failed.emit("License key cannot be empty.")
            return False

        fingerprint = get_fingerprint()

        # 1. Validate the key with our fingerprint scope. This tells us
        # the license is real, what policy it's under, and whether the
        # current fingerprint is already activated (which would let us
        # skip the create-machine step on re-activation).
        try:
            resp = _validate(license_key, fingerprint)
        except _KeygenError as exc:
            self.validation_failed.emit(
                f"Could not reach Keygen: {exc}")
            return False

        meta = resp.get("meta") or {}
        code = meta.get("code", "")
        detail = meta.get("detail", "")

        # NO_MACHINE / NO_MACHINES / FINGERPRINT_SCOPE_MISMATCH all mean
        # "license is real, just not yet activated for this fingerprint".
        # Those are the happy first-time-activation path.
        is_real_but_unactivated = code in (
            "NO_MACHINE", "NO_MACHINES", "FINGERPRINT_SCOPE_MISMATCH",
        )
        if not meta.get("valid") and not is_real_but_unactivated:
            # Real validation failure — bad key, suspended, expired, etc.
            self.validation_failed.emit(
                f"License invalid: {detail or code}")
            return False

        # 2. Pull tier metadata from the response.
        entitlements = _extract_entitlements(resp)

        # 3. If valid==False because of NO_MACHINE etc., create a machine
        # to claim a seat. If valid==True already, this fingerprint was
        # registered previously — skip create.
        machine_id = ""
        if not meta.get("valid"):
            try:
                m_resp = _create_machine(
                    license_key, entitlements["license_id"], fingerprint)
            except _KeygenError as exc:
                self.validation_failed.emit(f"Activation failed: {exc}")
                return False
            machine_id = (m_resp.get("data") or {}).get("id", "")
        else:
            # Already has a machine registered for our fingerprint —
            # find its ID from the relationships in the validate
            # response. We don't strictly need it until deactivate, but
            # store it so the user can release the seat later.
            # (If the validate response doesn't include the machine ID,
            # we'll have to look it up via GET /machines on first
            # deactivate attempt — accepted trade-off for now.)
            machine_id = ""

        # 4. Persist + update in-memory state.
        self._license_key = license_key
        self._machine_id = machine_id
        self._entitlements = entitlements
        self._last_validated_utc = datetime.now(timezone.utc)
        self._save_cache()
        self._set_state(LicenseState.ACTIVE)
        self._timer.start()
        return True

    # ---------- deactivation ----------

    def deactivate(self) -> bool:
        """Release the seat back to Keygen and clear local cache.
        Returns True on success, False if Keygen call failed (local
        state still gets cleared so the user can re-activate)."""
        ok = True
        if self._license_key and self._machine_id:
            try:
                _delete_machine(self._license_key, self._machine_id)
            except _KeygenError as exc:
                self.validation_failed.emit(
                    f"Couldn't reach Keygen to release the seat: {exc}. "
                    "Local activation cleared anyway — contact support "
                    "if seat count looks wrong."
                )
                ok = False
        secure_storage.clear()
        self._license_key = ""
        self._machine_id = ""
        self._entitlements = {}
        self._last_validated_utc = None
        self._timer.stop()
        self._set_state(LicenseState.UNLICENSED)
        return ok

    # ---------- async revalidate ----------

    def revalidate_async(self) -> None:
        """Kick off a background revalidate. Non-blocking; result comes
        via state_changed."""
        if self._state == LicenseState.UNLICENSED:
            return
        if self._thread is not None:
            return  # in-flight check
        if not self._license_key:
            return

        self._thread = QThread(self)
        self._worker = _RevalidateWorker(self._license_key, get_fingerprint())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_revalidate_done)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @Slot(str, dict)
    def _on_revalidate_done(self, kind: str, payload: dict):
        try:
            if kind == "ok":
                resp = payload["resp"]
                self._entitlements = _extract_entitlements(resp)
                self._last_validated_utc = datetime.now(timezone.utc)
                self._save_cache()
                self._set_state(LicenseState.ACTIVE)
            elif kind == "invalid":
                code = payload.get("code", "")
                if code in ("SUSPENDED", "BANNED"):
                    self._set_state(LicenseState.REVOKED)
                elif code in ("EXPIRED",):
                    self._set_state(LicenseState.EXPIRED)
                else:
                    # Treat unknown invalid codes as REVOKED for safety —
                    # they all mean "Keygen says you can't use this".
                    self._set_state(LicenseState.REVOKED)
            else:  # network_error
                # Offline: stay functional if within grace window.
                if self._within_grace_window():
                    self._set_state(LicenseState.OFFLINE_GRACE)
                else:
                    # Grace expired — block until user reconnects.
                    self._set_state(LicenseState.EXPIRED)
        finally:
            self._thread = None
            self._worker = None

    # ---------- persistence ----------

    def _load_cache(self) -> None:
        data = secure_storage.load()
        if not data:
            self._set_state(LicenseState.UNLICENSED)
            return
        try:
            self._license_key = data.get("license_key", "")
            self._machine_id = data.get("machine_id", "")
            self._entitlements = data.get("entitlements", {})
            ts = data.get("last_validated_utc")
            self._last_validated_utc = (
                datetime.fromisoformat(ts) if ts else None)
        except (ValueError, TypeError):
            # Corrupt cache — start fresh.
            self._set_state(LicenseState.UNLICENSED)
            return

        if not self._license_key:
            self._set_state(LicenseState.UNLICENSED)
            return

        # Cached but how stale? Decide initial state without a network
        # call — main.py wants a synchronous answer so it knows whether
        # to gate or let the app open. The async revalidate will refine
        # later.
        if self._within_grace_window():
            self._set_state(LicenseState.ACTIVE)
            self._timer.start()
        else:
            self._set_state(LicenseState.EXPIRED)

    def _save_cache(self) -> None:
        secure_storage.save({
            "license_key": self._license_key,
            "machine_id": self._machine_id,
            "entitlements": self._entitlements,
            "last_validated_utc": (
                self._last_validated_utc.isoformat()
                if self._last_validated_utc else None),
        })

    def _within_grace_window(self) -> bool:
        if not self._last_validated_utc:
            return False
        return datetime.now(timezone.utc) - self._last_validated_utc < _OFFLINE_GRACE

    # ---------- state helper ----------

    def _set_state(self, new_state: LicenseState) -> None:
        if new_state == self._state:
            return
        self._state = new_state
        self.state_changed.emit(new_state)
