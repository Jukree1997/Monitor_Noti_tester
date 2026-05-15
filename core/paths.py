"""Filesystem path helpers — single source of truth for "where do project
JSONs and model files live by default" so file pickers across tabs all
start in the same place.

## Two execution modes

- **Dev (running from source)**: paths resolve to the repo root.
- **Frozen (PyInstaller / AppImage / Inno Setup)**: paths resolve to a
  per-user writable data dir (`~/.local/share/MNT/` on Linux,
  `%APPDATA%/Baksters/MNT/` on Windows).

## Why the writable-dir split (v1.0.2 fix)

v1.0.0 and v1.0.1 used `<install-dir>/models/` and
`<install-dir>/config/` as the default file-picker locations. That
worked fine in dev because the repo root is writable. In a real
install it doesn't:

- AppImage mounts itself read-only via FUSE — `mkdir` raises
  `OSError: Read-only file system`.
- Windows Inno Setup installs to ``C:/Program Files/MNT/`` which is
  admin-write-only — non-admin users get ``PermissionError``.

The exception propagated out of click handlers, and PySide6's default
slot handler silently swallowed it → "click does nothing" reports
for Load Project, Browse Model, etc. in the bundled app. Fleet's
+ Add Project worked because it didn't call these helpers.

Fixing this: in frozen mode, default to the same user-data dir tree
that already holds the license cache. Customers find their stuff in
one place (`~/.local/share/MNT/`) regardless of where the app is
installed.
"""
from __future__ import annotations
import sys
from pathlib import Path

from platformdirs import user_data_dir


# ======================================
# -------- APP ROOT --------
# ======================================

def is_frozen() -> bool:
    """True when running as a PyInstaller bundle (or any other frozen
    executable). Used by other modules that need to know whether
    ``__file__`` paths are meaningful."""
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """Directory the app's binary lives in.

    In a frozen bundle this is the install dir (where ``MNT.exe`` /
    ``MNT`` lives). In dev this is the repo root (parent of ``core/``).

    Note: this directory may be READ-ONLY in real installs (AppImage
    FUSE mount, Windows Program Files). Use ``writable_data_dir()`` for
    anything you need to write to."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def writable_data_dir() -> Path:
    """A guaranteed-writable directory for user-created files.

    In frozen installs this is the per-user data dir (same tree the
    license cache lives in):
      - Linux:   ``~/.local/share/MNT/``
      - Windows: ``%APPDATA%/Baksters/MNT/``
      - macOS:   ``~/Library/Application Support/Baksters/MNT/``

    In dev (source runs) this is the repo root, matching how
    contributors actually use the project."""
    if is_frozen():
        return Path(user_data_dir("MNT", "Baksters"))
    return app_dir()


# ======================================
# -------- DEFAULT SUBDIRS --------
# ======================================

def default_models_dir() -> Path:
    """Default directory the "Browse Model" file picker opens to.

    Created on first call so a brand-new install gets a sensible empty
    folder ready for the user to drop ``.pt`` / ``.onnx`` files into.
    """
    d = writable_data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_config_dir() -> Path:
    """Default directory the "Load Project" / "Save Project" pickers
    open to. Also auto-created on first call."""
    d = writable_data_dir() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_videos_dir() -> Path:
    """Default directory the "Browse Video File" picker opens to.
    Not auto-created — users typically already have a videos folder
    they want to use, and creating an unused empty dir is noise."""
    return writable_data_dir() / "videos"
