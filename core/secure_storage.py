"""Encrypted local cache of the license activation state.

Stores Keygen's validation response + a timestamp on disk so we can:
  - Show "you have a Pro license, 16 cameras" without re-hitting Keygen.
  - Sustain a 30-day offline grace period.

Security model — defense in depth on top of Keygen's seat tracking:
  - The cache is Fernet-encrypted (AES-128-CBC + HMAC-SHA256).
  - The Fernet key is derived from THIS PC's hardware fingerprint.
  - If a user copies license.dat to a different PC, decryption fails
    there (different fingerprint → different key). They have to
    re-activate, which Keygen will either accept (consuming another
    seat) or deny (seat limit reached).

Note: this is anti-copy hardening, not unbreakable DRM. A determined
attacker can reverse-engineer the algorithm; the goal is to make
casual key-sharing not work.
"""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from platformdirs import user_data_dir

from core.fingerprint import get_fingerprint


# ======================================
# -------- 0. CONSTANTS --------
# ======================================

_APP_NAME = "MNT"
_ORG_NAME = "Baksters"
_FILE_NAME = "license.dat"


# ======================================
# -------- 1. PATH --------
# ======================================

def license_file_path() -> Path:
    """Cross-platform location for the license cache.

    Windows: %APPDATA%/Baksters/MNT/license.dat
    Linux:   ~/.local/share/Baksters/MNT/license.dat
    macOS:   ~/Library/Application Support/Baksters/MNT/license.dat
    """
    d = Path(user_data_dir(_APP_NAME, _ORG_NAME))
    d.mkdir(parents=True, exist_ok=True)
    return d / _FILE_NAME


# ======================================
# -------- 2. ENCRYPTION KEY --------
# ======================================

def _derive_fernet_key() -> bytes:
    """Derive a Fernet key from this PC's fingerprint.

    Fernet wants a 32-byte url-safe base64 key. SHA-256 of the
    fingerprint hex string gives us 32 bytes deterministically — same
    machine always gets the same key.
    """
    fp = get_fingerprint().encode("utf-8")
    raw = hashlib.sha256(fp).digest()
    return base64.urlsafe_b64encode(raw)


# ======================================
# -------- 3. PUBLIC API --------
# ======================================

def save(data: dict) -> None:
    """Encrypt `data` (a JSON-serializable dict) and write to disk."""
    fernet = Fernet(_derive_fernet_key())
    plaintext = json.dumps(data).encode("utf-8")
    ciphertext = fernet.encrypt(plaintext)
    path = license_file_path()
    # Write atomically to avoid corruption on crash mid-write.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(ciphertext)
    tmp.replace(path)


def load() -> Optional[dict]:
    """Decrypt and return the cached state, or None if missing or
    unreadable on this PC. Treats a fingerprint mismatch (decryption
    failure) the same as a missing file: caller falls back to the
    activation flow."""
    path = license_file_path()
    if not path.exists():
        return None
    try:
        ciphertext = path.read_bytes()
        fernet = Fernet(_derive_fernet_key())
        plaintext = fernet.decrypt(ciphertext)
        return json.loads(plaintext.decode("utf-8"))
    except (InvalidToken, ValueError, OSError):
        # InvalidToken: file copied from another PC (different fingerprint),
        # OR the file has been tampered with. ValueError: JSON garbage.
        # OSError: filesystem issue. In all cases, treat as "no license".
        return None


def clear() -> None:
    """Delete the cached license file. Used on Deactivate."""
    path = license_file_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
