"""Filesystem path helpers — single source of truth for "where do project
JSONs and model files live by default" so file pickers across tabs all
start in the same place, and so a future packaging pass only has to
update one function.
"""
from __future__ import annotations
from pathlib import Path


# ======================================
# -------- APP ROOT --------
# ======================================

def app_dir() -> Path:
    """Return the Monitor_Noti_tester project root.

    Resolved from this file's location: ``<root>/core/paths.py`` → parent
    of ``core/`` is the root. Works when running from source. When the
    app is later bundled via PyInstaller, this needs an override to
    return the user-data dir (``~/.config/MonitorNoti/`` on Linux,
    ``%APPDATA%/MonitorNoti/`` on Windows) instead — see the packaging
    plan for the swap.
    """
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
