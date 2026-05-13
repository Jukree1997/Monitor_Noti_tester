"""Filesystem path helpers — single source of truth for "where do project
JSONs and model files live by default" so file pickers across tabs all
start in the same place.

Two execution modes:
  - **Dev (running from source)**: app_dir() returns the project root
    (``<repo>/``). Default model/config/video dirs are subdirs of that.
  - **Frozen (PyInstaller bundle)**: app_dir() returns the directory
    containing the .exe (in one-folder mode that's the install dir).
    Customers can drop models into ``<install>/models/`` next to the
    exe.

The frozen path uses ``sys.executable`` rather than ``__file__`` because
PyInstaller's one-folder mode lays out files relative to the exe, while
one-file mode has ``__file__`` pointing into a temp _MEIPASS extraction
that gets wiped between runs. ``sys.executable.parent`` works for both
modes and matches what users see in their installer.
"""
from __future__ import annotations
import sys
from pathlib import Path


# ======================================
# -------- APP ROOT --------
# ======================================

def is_frozen() -> bool:
    """True when running as a PyInstaller bundle (or any other frozen
    executable). Used by other modules that need to know whether
    ``__file__`` paths are meaningful."""
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """Return the directory the app considers its 'root' for default
    file-picker locations.

    In a frozen bundle this is the install dir (where the .exe lives).
    In dev this is the repo root (parent of ``core/``)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# ======================================
# -------- DEFAULT SUBDIRS --------
# ======================================

def default_models_dir() -> Path:
    """Default directory the "Browse Model" file picker opens to.

    Created on first call so a brand-new install gets a sensible empty
    folder ready for the user to drop ``.pt`` / ``.onnx`` files into.
    """
    d = app_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_config_dir() -> Path:
    """Default directory the "Load Project" / "Save Project" pickers
    open to. Also auto-created on first call."""
    d = app_dir() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_videos_dir() -> Path:
    """Default directory the "Browse Video File" picker opens to.
    Not auto-created — users typically already have a videos folder
    they want to use, and creating an unused empty dir is noise."""
    return app_dir() / "videos"
