"""ONNX-Runtime-based YOLO inference wrapper.

MIRROR: source of truth is Smart_Relabeler_V2/core/onnx_runtime.py. Keep
this file in sync — there's no shared package yet (Phase 1 of
ultralytics-replacement.md). When you fix a bug or add an argument here,
copy the change back across the Baksters_Tools repos that use this
wrapper. A future refactor may extract these into a single package; until
then, the duplication is the simpler trade.
"""

# ======================================
# -------- 0. IMPORTS --------
# ======================================

from __future__ import annotations
import ctypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence, Union

import cv2
import numpy as np


# ======================================
# -------- 0a. CUDNN PRELOAD --------
# ======================================

# onnxruntime-gpu dlopens libcudnn.so.9 lazily and fails silently to CPU if
# it can't find it on LD_LIBRARY_PATH. The pip-installed nvidia-cudnn-cu12
# package bundles the libs under site-packages/nvidia/cudnn/lib/ — preload
# them with RTLD_GLOBAL so onnxruntime's later dlopen() finds them in the
# process symbol table without touching LD_LIBRARY_PATH or system state.
def _preload_cudnn() -> None:
    try:
        import nvidia.cudnn  # type: ignore[import-untyped]
    except ImportError:
        return  # cuDNN not installed — onnxruntime falls back to CPU
    # nvidia.cudnn is a PEP-420 namespace package, so __file__ is None;
    # __path__ is the canonical way to locate it.
    cudnn_dir = Path(list(nvidia.cudnn.__path__)[0]) / "lib"
    if not cudnn_dir.is_dir():
        return
    # Order matters — base libcudnn.so.9 first, then the engines that depend on it.
    for name in ("libcudnn.so.9", "libcudnn_graph.so.9", "libcudnn_ops.so.9",
                 "libcudnn_adv.so.9", "libcudnn_cnn.so.9", "libcudnn_engines_precompiled.so.9",
                 "libcudnn_engines_runtime_compiled.so.9"):
        p = cudnn_dir / name
        if p.is_file():
            try:
                ctypes.CDLL(str(p), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass  # one missing engine shouldn't break the rest


_preload_cudnn()
import onnxruntime as ort  # noqa: E402  — must come after cuDNN preload


# ======================================
# -------- 0b. CPU/GPU CONFLICT REPAIR --------
# ======================================

# ultralytics' ONNX export auto-installs `onnxruntime` (CPU) as a missing
# requirement, even when `onnxruntime-gpu` is already present. Both share
# the `onnxruntime` Python module, and pip resolves to whichever was
# installed most recently — so the CPU package silently shadows GPU on
# the next process launch, and the GUI mysteriously falls back to CPU
# inference. This helper detects + fixes in-place so the user doesn't
# have to remember the manual cleanup after every smart fine-tune.
def fix_onnxruntime_gpu_conflict(verbose: bool = True) -> bool:
    """Repair the onnxruntime / onnxruntime-gpu shadow conflict.

    Returns True if a repair was performed. Safe to call anytime; if no
    conflict exists, this is a fast no-op (one importlib.metadata scan).
    """
    try:
        from importlib.metadata import distributions
    except ImportError:
        return False
    installed = {d.metadata.get("Name", "").lower()
                 for d in distributions()}
    has_cpu = "onnxruntime" in installed
    has_gpu = "onnxruntime-gpu" in installed
    if not (has_cpu and has_gpu):
        return False  # no conflict
    if verbose:
        print("[onnxruntime] CPU + GPU packages both installed — "
              "repairing so CUDA stays active on next launch...")
    import subprocess, sys
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "onnxruntime"],
            check=False, capture_output=True, timeout=60)
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--force-reinstall", "--no-deps", "onnxruntime-gpu"],
            check=False, capture_output=True, timeout=300)
        if verbose:
            print("[onnxruntime] repair complete — onnxruntime-gpu restored.")
    except Exception as e:
        if verbose:
            print(f"[onnxruntime] auto-repair failed: {e}\n"
                  f"  Run manually: pip uninstall onnxruntime -y && "
                  f"pip install --force-reinstall --no-deps onnxruntime-gpu")
        return False
    return True


# ======================================
# -------- 1. RESULT WRAPPER TYPES --------
# ======================================

# These mimic the slice of the `ultralytics.engine.results.Results` API that
# downstream code actually touches. Two distinct usage styles in this
# codebase:
#   - per-box iteration (auto_labeler / smart_relabeler):
#         for box in r.boxes: int(box.cls[0]); box.xywhn[0].tolist()
#   - bulk torch-tensor-shaped access (Live_Detection_Tester app.py):
#         r.boxes.cls.cpu().numpy().astype(int); r.boxes.xyxy.cpu().numpy()
# `_Boxes` supports both via iter()/len() AND .cls/.conf/.xyxy/.xywhn props.

@dataclass
class _OnnxBox:
    cls: np.ndarray    # shape (1,)  int64   class id
    conf: np.ndarray   # shape (1,)  float32 confidence
    xywhn: np.ndarray  # shape (1,4) float32 normalized cx, cy, w, h


class _ArrayProxy:
    """Mimics `torch.Tensor.cpu().numpy()` on a plain numpy array so call
    sites that wrote against ultralytics' torch-backed result API don't
    need to branch on backend."""

    def __init__(self, arr: np.ndarray):
        self._arr = arr

    def cpu(self):
        return self

    def numpy(self):
        return self._arr


class _Boxes:
    """Aggregator that satisfies both APIs at once:
       - iter / len / index → individual `_OnnxBox` instances
       - .xyxy / .cls / .conf / .xywhn → `_ArrayProxy` (bulk numpy with
         `.cpu().numpy()`).
       - .id is always None — tracker IDs are added downstream by the
         tracker layer (e.g. Monitor_Noti_tester's `_TrackResult`)."""

    def __init__(
        self,
        boxes_list: list[_OnnxBox],
        xyxy: np.ndarray,
        cls: np.ndarray,
        conf: np.ndarray,
        xywhn: np.ndarray,
    ):
        self._boxes = boxes_list
        self.xyxy = _ArrayProxy(xyxy)
        self.cls = _ArrayProxy(cls)
        self.conf = _ArrayProxy(conf)
        self.xywhn = _ArrayProxy(xywhn)
        self.id = None

    def __iter__(self):
        return iter(self._boxes)

    def __len__(self):
        return len(self._boxes)

    def __getitem__(self, i):
        return self._boxes[i]


def _empty_boxes() -> _Boxes:
    return _Boxes(
        [],
        xyxy=np.zeros((0, 4), dtype=np.float32),
        cls=np.zeros(0, dtype=np.int64),
        conf=np.zeros(0, dtype=np.float32),
        xywhn=np.zeros((0, 4), dtype=np.float32),
    )


@dataclass
class _OnnxResult:
    boxes: _Boxes = field(default_factory=_empty_boxes)
    names: dict[int, str] = field(default_factory=dict)  # class id → name
    obb: None = None  # oriented bounding boxes — always None for axis-aligned models


# ======================================
# -------- 2. DETECTOR --------
# ======================================

ImageOrPath = Union[str, Path, np.ndarray]


class OnnxYoloDetector:
    """ONNX-Runtime-backed YOLO detector.

    Loads a YOLO ONNX file (exported with `dynamic=True, simplify=True` from
    ultralytics, or any future RF-DETR-style export with the same I/O shape)
    and runs inference via `onnxruntime-gpu`. Falls back to CPU automatically
    if no CUDA provider is available.
    """

    def __init__(self, model_path: ImageOrPath, imgsz: int = 640):
        self._path = str(model_path)
        # CUDA first, CPU fallback. Per the ONNX docs, listing CPU last lets
        # the runtime pick CUDA when available without us having to detect it.
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self._session = ort.InferenceSession(self._path, providers=providers)
        self._input_name = self._session.get_inputs()[0].name
        self._imgsz = int(imgsz)
        self._names = self._extract_class_names()

        # If we asked for CUDA but the session fell back to CPU, surface a
        # loud warning so the user notices (silent CPU fallback was the
        # cause of every "why is my detection slow" mystery so far). Also
        # kick off a background repair so the *next* process launch picks
        # up the real GPU package — can't fix the current process because
        # `onnxruntime` is already imported.
        active = self._session.get_providers()[0]
        if active != "CUDAExecutionProvider":
            print(f"[OnnxYoloDetector] WARNING: requested CUDA but active "
                  f"provider is {active}. Inference will be CPU-bound. "
                  f"Attempting environment repair for next launch...")
            try:
                fix_onnxruntime_gpu_conflict(verbose=True)
            except Exception:
                pass

    @property
    def names(self) -> dict[int, str]:
        """Class id → name. Populated from ultralytics-export metadata when
        present, else empty (callers should use the int id as a label)."""
        return self._names

    def _extract_class_names(self) -> dict[int, str]:
        """ultralytics' ONNX export writes the {0: "person", ...} dict into
        the model's `metadata.names` custom map. Recover it here so result
        consumers can do `result.names[cls_id]` like they did with YOLO."""
        try:
            meta = self._session.get_modelmeta().custom_metadata_map
            raw = meta.get("names")
            if not raw:
                return {}
            # ultralytics serializes as a Python-repr-like string, e.g.
            # "{0: 'person', 1: 'bicycle', ...}". `ast.literal_eval` parses
            # that safely without exec.
            import ast
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, dict):
                return {int(k): str(v) for k, v in parsed.items()}
        except (ValueError, SyntaxError, AttributeError):
            pass
        return {}

    # ======================================
    # -------- 3. PUBLIC PREDICT API --------
    # ======================================

    def predict(
        self,
        source: Union[ImageOrPath, Sequence[ImageOrPath]],
        conf: float = 0.25,
        iou: float = 0.45,
        classes: Sequence[int] | None = None,
        max_det: int = 300,
        stream: bool = True,
        imgsz: int | None = None,
        # Ignored kwargs accepted for ultralytics-API compatibility:
        augment: bool = False, half: bool = False, verbose: bool = False,
        device: str | None = None,
        **_kwargs,
    ) -> Iterator[_OnnxResult] | list[_OnnxResult]:
        """Run inference. Returns a generator (stream=True) or a list."""
        sources = self._normalize_source(source)
        target_size = int(imgsz) if imgsz else self._imgsz
        gen = self._predict_iter(
            sources, conf=conf, iou=iou, classes=classes, max_det=max_det,
            target_size=target_size)
        return gen if stream else list(gen)

    def __call__(self, source, **kwargs):
        # When called as `model(frame, ...)` (no `.predict`), default to
        # non-streaming so the result is a list — matches ultralytics so
        # `results[0]` keeps working at call sites that expect indexing.
        kwargs.setdefault("stream", False)
        return self.predict(source, **kwargs)

    # ======================================
    # -------- 4. ITERATION CORE --------
    # ======================================

    def _predict_iter(
        self,
        sources: Sequence[ImageOrPath],
        conf: float, iou: float,
        classes: Sequence[int] | None, max_det: int,
        target_size: int,
    ) -> Iterator[_OnnxResult]:
        """Yield one _OnnxResult per source. Unreadable images yield empty."""
        cls_filter = set(int(c) for c in classes) if classes else None
        for src in sources:
            img = self._read_image(src)
            if img is None:
                yield _OnnxResult(names=self._names)
                continue
            yield self._predict_one(img, conf, iou, cls_filter, max_det, target_size)

    def _predict_one(
        self, img_bgr: np.ndarray, conf: float, iou: float,
        cls_filter: set[int] | None, max_det: int, target_size: int,
    ) -> _OnnxResult:
        h0, w0 = img_bgr.shape[:2]
        inp, scale, pad_top, pad_left = self._letterbox(img_bgr, target_size)
        outputs = self._session.run(None, {self._input_name: inp})
        result = self._postprocess(
            outputs[0], (h0, w0), scale, pad_top, pad_left,
            conf, iou, cls_filter, max_det)
        result.names = self._names
        return result

    # ======================================
    # -------- 5. PRE-PROCESS (LETTERBOX) --------
    # ======================================

    @staticmethod
    def _letterbox(
        img_bgr: np.ndarray, target: int,
    ) -> tuple[np.ndarray, float, int, int]:
        """Resize-and-pad to a square `target` keeping aspect ratio.

        Mismatched letterbox between training and inference is the #1 cause
        of mAP drop after an ONNX export — keep this consistent with what
        ultralytics does (same scale for h+w, gray=114 padding, RGB+CHW).
        """
        h, w = img_bgr.shape[:2]
        scale = min(target / h, target / w)
        new_h, new_w = int(round(h * scale)), int(round(w * scale))
        resized = cv2.resize(img_bgr, (new_w, new_h),
                             interpolation=cv2.INTER_LINEAR)
        pad_top = (target - new_h) // 2
        pad_bottom = target - new_h - pad_top
        pad_left = (target - new_w) // 2
        pad_right = target - new_w - pad_left
        padded = cv2.copyMakeBorder(
            resized, pad_top, pad_bottom, pad_left, pad_right,
            cv2.BORDER_CONSTANT, value=(114, 114, 114))
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        chw = np.transpose(rgb, (2, 0, 1)).astype(np.float32) / 255.0
        return chw[np.newaxis, ...], scale, pad_top, pad_left

    # ======================================
    # -------- 6. POST-PROCESS (NMS + DELETTERBOX) --------
    # ======================================

    def _postprocess(
        self, raw: np.ndarray, orig_hw: tuple[int, int],
        scale: float, pad_top: int, pad_left: int,
        conf_thr: float, iou_thr: float,
        cls_filter: set[int] | None, max_det: int,
    ) -> _OnnxResult:
        # YOLO11 export shape is (1, 4+nc, A); transpose to (A, 4+nc) so each
        # row is one anchor. Some exports flip this; detect by which dim is
        # smaller.
        out = raw[0]
        if out.shape[0] < out.shape[1]:
            out = out.T  # → (A, 4+nc)
        if out.shape[1] < 5:
            return _OnnxResult()  # malformed output

        boxes_pix = out[:, :4]            # (A, 4) cx,cy,w,h in model pixels
        # Class channels start at index 4. Some exports keep extra trailing
        # channels (e.g., a 2-class architecture later retrained for 1
        # class — the head's second slot stays in the graph but holds raw
        # logits, not probabilities). Trust the metadata's `names` dict
        # over the raw output width when names is present.
        nc_total = out.shape[1] - 4
        if self._names and len(self._names) <= nc_total:
            nc = len(self._names)
        else:
            nc = nc_total
        class_probs = out[:, 4:4 + nc]    # (A, nc) — real probability channels only
        cls_ids = np.argmax(class_probs, axis=1).astype(np.int64)
        confs = class_probs[np.arange(class_probs.shape[0]), cls_ids]

        # Confidence + class filter early — drops thousands of anchors before
        # the NMS step, so NMS doesn't even see them.
        keep = confs >= conf_thr
        if cls_filter is not None:
            keep &= np.isin(cls_ids, list(cls_filter))
        boxes_pix = boxes_pix[keep]
        cls_ids = cls_ids[keep]
        confs = confs[keep]
        if boxes_pix.shape[0] == 0:
            return _OnnxResult()

        # cv2.dnn.NMSBoxes expects (x, y, w, h) in pixels — same coordinate
        # space as the model. Class-aware NMS by offsetting per-class.
        offsets = cls_ids.astype(np.float32) * 4096.0
        nms_in = boxes_pix.copy()
        nms_in[:, 0] += offsets
        nms_xywh = np.column_stack([
            nms_in[:, 0] - nms_in[:, 2] / 2.0,
            nms_in[:, 1] - nms_in[:, 3] / 2.0,
            nms_in[:, 2], nms_in[:, 3],
        ])
        idx = cv2.dnn.NMSBoxes(
            nms_xywh.tolist(), confs.astype(np.float32).tolist(),
            float(conf_thr), float(iou_thr))
        if len(idx) == 0:
            return _OnnxResult()
        idx = np.asarray(idx).reshape(-1)
        idx = idx[np.argsort(-confs[idx])][:max_det]

        # De-letterbox: subtract pad, divide by scale, normalize to original.
        h0, w0 = orig_hw
        cx = (boxes_pix[idx, 0] - pad_left) / scale
        cy = (boxes_pix[idx, 1] - pad_top) / scale
        bw = boxes_pix[idx, 2] / scale
        bh = boxes_pix[idx, 3] / scale
        cxn = np.clip(cx / w0, 0.0, 1.0)
        cyn = np.clip(cy / h0, 0.0, 1.0)
        wn = np.clip(bw / w0, 0.0, 1.0)
        hn = np.clip(bh / h0, 0.0, 1.0)

        # Pixel-space xyxy for the bulk-array surface (used by call sites
        # that work in pixel coords, e.g. Live_Detection_Tester drawing).
        x1_pix = cx - bw / 2.0
        y1_pix = cy - bh / 2.0
        x2_pix = cx + bw / 2.0
        y2_pix = cy + bh / 2.0

        # Build per-box list (for `for box in r.boxes:` iteration).
        boxes_list: list[_OnnxBox] = []
        for i, k in enumerate(idx):
            boxes_list.append(_OnnxBox(
                cls=np.array([cls_ids[k]], dtype=np.int64),
                conf=np.array([confs[k]], dtype=np.float32),
                xywhn=np.array([[cxn[i], cyn[i], wn[i], hn[i]]],
                                dtype=np.float32),
            ))

        return _OnnxResult(boxes=_Boxes(
            boxes_list=boxes_list,
            xyxy=np.stack([x1_pix, y1_pix, x2_pix, y2_pix], axis=1).astype(np.float32),
            cls=cls_ids[idx].astype(np.int64),
            conf=confs[idx].astype(np.float32),
            xywhn=np.stack([cxn, cyn, wn, hn], axis=1).astype(np.float32),
        ))

    # ======================================
    # -------- 7. SOURCE NORMALIZATION --------
    # ======================================

    @staticmethod
    def _normalize_source(
        source: Union[ImageOrPath, Sequence[ImageOrPath]],
    ) -> Sequence[ImageOrPath]:
        """Accept a single path/array or a list, return a sequence."""
        if isinstance(source, (str, Path, np.ndarray)):
            return [source]
        return list(source)

    @staticmethod
    def _read_image(src: ImageOrPath) -> np.ndarray | None:
        if isinstance(src, np.ndarray):
            return src
        img = cv2.imread(str(src))
        return img  # cv2 returns None for unreadable paths
