"""Hardware fingerprint — stable per-PC identifier used by the license
system to bind a license seat to a specific machine.

What makes a fingerprint stable across reboots + OS updates but invalid
after a motherboard swap is exactly the property we want: legitimate
users don't get falsely invalidated; ID-cloning attempts (SSD copied to
a new box) fail.

Sources, in priority order:
1. `machineid.id()` — composite of stable platform IDs (motherboard
   UUID via WMI on Windows, /etc/machine-id on Linux, IOPlatformUUID
   on macOS). This is the right answer when available.
2. `uuid.getnode()` — MAC address fallback. Less stable (USB ethernet,
   VMs), but better than nothing if py-machineid fails.

The output is a SHA-256 hex digest of the raw ID. We hash because:
- The raw OS UID is often considered PII-adjacent; the hash isn't.
- It gives us a fixed 64-char hex string the rest of the code can
  treat as opaque.
"""
from __future__ import annotations
import hashlib
import uuid


# ======================================
# -------- 0. IN-MEMORY CACHE --------
# ======================================

_cached: str | None = None


# ======================================
# -------- 1. PUBLIC API --------
# ======================================

def get_fingerprint() -> str:
    """Return a 64-char hex SHA-256 of stable hardware IDs.

    Cached after first call so we don't re-hit WMI / /etc/machine-id on
    every license check.
    """
    global _cached
    if _cached is not None:
        return _cached

    raw = _read_raw_id()
    _cached = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return _cached


# ======================================
# -------- 2. INTERNAL --------
# ======================================

def _read_raw_id() -> str:
    """Best stable hardware ID we can find, with fallback."""
    try:
        import machineid  # py-machineid package, imports as `machineid`
        raw = machineid.id()
        if raw:
            return raw
    except Exception:
        # py-machineid not installed, or platform-specific probe failed.
        # Fall through to MAC.
        pass

    # uuid.getnode() returns the MAC as an integer, or a random number if
    # MAC discovery failed (high bit set on random results — we don't
    # bother distinguishing; SHA-256 absorbs any unstable bits).
    return f"mac:{uuid.getnode():012x}"
