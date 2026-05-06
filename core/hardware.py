"""Hardware probing for Fleet mode.

Exposes a one-shot ``get_hardware_info()`` snapshot the UI shows in the Fleet
sidebar, plus ``pick_max_workers()`` which yields a conservative worker cap
based on GPU VRAM and CPU count. The cap is set once at app startup; user can
override it downward but not upward (preventing OOM is worth the friction).
"""
from __future__ import annotations
import os


def pick_max_workers(model_size_mb: int = 300, headroom_gb: float = 1.0,
                     hard_cap: int = 16) -> int:
    """Conservative worker cap.

    GPU path: count how many copies of a model_size_mb-class model fit in free
    VRAM after reserving headroom_gb for the OS / driver. Also limit by CPU
    cores (each worker still does decode + tracking on CPU).

    CPU-only path: half the logical cores, leaves room for OS + decoders.
    """
    cpus = os.cpu_count() or 4
    try:
        import torch  # type: ignore[import-not-found]
        if torch.cuda.is_available():
            free, _total = torch.cuda.mem_get_info()
            free_gb = max(0.0, free / (1024 ** 3) - headroom_gb)
            by_vram = max(1, int(free_gb * 1024 / max(1, model_size_mb)))
            by_cpu = max(1, cpus // 2)
            return min(by_vram, by_cpu, hard_cap)
    except Exception:
        pass
    return min(max(1, cpus // 2), hard_cap)


def get_hardware_info() -> dict:
    """Snapshot for display. Safe to call without torch installed."""
    info = {
        "cpu_count": os.cpu_count() or 0,
        "cuda": False,
        "gpu_name": "N/A",
        "vram_free_gb": 0.0,
        "vram_total_gb": 0.0,
        "max_workers": pick_max_workers(),
    }
    try:
        import torch  # type: ignore[import-not-found]
        if torch.cuda.is_available():
            info["cuda"] = True
            info["gpu_name"] = torch.cuda.get_device_name(0)
            free, total = torch.cuda.mem_get_info()
            info["vram_free_gb"] = round(free / (1024 ** 3), 1)
            info["vram_total_gb"] = round(total / (1024 ** 3), 1)
    except Exception:
        pass
    return info
