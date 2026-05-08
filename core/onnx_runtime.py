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
    """ONNX-Runtime-backed object detector with auto-format dispatch.

    Loads a detector ONNX file and dispatches to the right pre/post-process
    based on the export format:
      * **YOLO** (ultralytics, `dynamic=True, simplify=True`) — single output,
        shape `(1, 4+nc, anchors)`. Letterbox preprocess, anchor-grid
        postprocess + NMS.
      * **RF-DETR** (`rfdetr.RFDETRMedium().export()`) — two outputs named
        `dets` (shape `(1, 300, 4)`, cxcywh normalized) and `labels` (shape
        `(1, 300, n+1)`, raw logits with a trailing background slot).
        Square-resize + ImageNet-mean/std preprocess; sigmoid + topk + filter
        postprocess (no NMS — DETR already deduplicates via Hungarian
        matching during training).

    Falls back to CPU automatically if no CUDA provider is available.
    """

    def __init__(self, model_path: ImageOrPath, imgsz: int = 640):
        self._path = str(model_path)
        # CUDA first, CPU fallback. Per the ONNX docs, listing CPU last lets
        # the runtime pick CUDA when available without us having to detect it.
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self._session = ort.InferenceSession(self._path, providers=providers)
        self._input_name = self._session.get_inputs()[0].name
        self._imgsz = int(imgsz)
        self._format = self._detect_format()
        self._names = self._extract_class_names()
        if self._format == "rfdetr":
            # RF-DETR has a fixed input resolution baked into the export —
            # use the model's actual input H/W so caller-provided imgsz is
            # ignored on this path.
            in_shape = self._session.get_inputs()[0].shape
            self._rfdetr_size = (int(in_shape[2]), int(in_shape[3]))
            # Derive num_classes_no_bg from the labels output shape (n+1
            # logits per query — the last slot is the implicit background).
            label_shape = next(
                o.shape for o in self._session.get_outputs() if o.name == "labels"
            )
            self._rfdetr_n_logits = int(label_shape[2])
            print(f"[OnnxDetector] format=rfdetr  input={self._rfdetr_size}  "
                  f"logits={self._rfdetr_n_logits} (incl. bg)  names={self._names}")

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

    @property
    def format(self) -> str:
        """'yolo' or 'rfdetr' — selected at __init__ from ONNX I/O shape."""
        return self._format

    def _detect_format(self) -> str:
        """Sniff the ONNX export style from output names + shapes.

        RF-DETR exports two outputs named `dets` and `labels`. Anything else
        (single output, ultralytics-style 4+nc anchors) is treated as YOLO."""
        outputs = self._session.get_outputs()
        out_names = {o.name for o in outputs}
        if {"dets", "labels"}.issubset(out_names) and len(outputs) >= 2:
            return "rfdetr"
        return "yolo"

    def _extract_class_names(self) -> dict[int, str]:
        """Recover the {0: "person", ...} dict from the source the export
        wrote it to:

        * ultralytics writes `names` into ONNX `custom_metadata_map` as a
          Python-repr string;
        * `rfdetr.export()` does NOT embed names — look for a sidecar
          `<model>.names.json` next to the .onnx (list or {"0": ...} dict).

        Falls back to {} when nothing is found; callers should display int IDs."""
        # 1. ONNX-embedded names (ultralytics path).
        try:
            meta = self._session.get_modelmeta().custom_metadata_map
            raw = meta.get("names")
            if raw:
                import ast
                parsed = ast.literal_eval(raw)
                if isinstance(parsed, dict):
                    return {int(k): str(v) for k, v in parsed.items()}
        except (ValueError, SyntaxError, AttributeError):
            pass
        # 2. Sidecar JSON (RF-DETR path; produced by Smart_Relabeler_V2's
        # Phase 2 trainer alongside the ONNX export).
        import json
        sidecar = Path(self._path).with_suffix(".names.json")
        if sidecar.is_file():
            try:
                data = json.loads(sidecar.read_text())
                if isinstance(data, list):
                    return {i: str(n) for i, n in enumerate(data)}
                if isinstance(data, dict):
                    return {int(k): str(v) for k, v in data.items()}
            except (ValueError, OSError):
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
        if self._format == "rfdetr":
            inp = self._preprocess_rfdetr(img_bgr)
            outputs = self._session.run(None, {self._input_name: inp})
            # rfdetr exports outputs in the order [dets, labels] (per
            # rfdetr/detr.py:837 output_names = ["dets", "labels"]).
            dets, labels = outputs[0], outputs[1]
            result = self._postprocess_rfdetr(
                dets, labels, (h0, w0), conf, cls_filter, max_det)
        else:
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
    # -------- 6b. RF-DETR PATH --------
    # ======================================

    # ImageNet-style normalization — the only preprocessing RF-DETR was
    # trained with (no letterbox; square resize directly to model resolution).
    _RFDETR_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 3, 1, 1)
    _RFDETR_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 3, 1, 1)

    def _preprocess_rfdetr(self, img_bgr: np.ndarray) -> np.ndarray:
        """Resize-and-normalize an FHD frame to the RF-DETR input layout.

        Returns NCHW float32 with ImageNet normalization. Scale is implicit
        in the postprocess (boxes come back in normalized [0,1] coords and
        get scaled by the original image size)."""
        h, w = self._rfdetr_size
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_LINEAR)
        chw = np.transpose(resized.astype(np.float32) / 255.0, (2, 0, 1))[None]
        return ((chw - self._RFDETR_MEAN) / self._RFDETR_STD).astype(np.float32)

    def _postprocess_rfdetr(
        self, dets: np.ndarray, labels: np.ndarray,
        orig_hw: tuple[int, int], conf_thr: float,
        cls_filter: set[int] | None, max_det: int,
    ) -> _OnnxResult:
        """Decode RF-DETR's (dets, labels) outputs into the same `_OnnxResult`
        surface YOLO produces.

        Mirrors `rfdetr.models.lwdetr.PostProcess.forward`:
          1. sigmoid the logits
          2. flatten + topk over (queries × classes)
          3. recover query index + class index from the flat index
          4. cxcywh → xyxy, scale to original image size
          5. drop the implicit-background slot
          6. apply user conf threshold + class filter
        """
        # Logits sigmoid → per-(query, class) probability. Background slot is
        # the last class index (== self._rfdetr_n_logits - 1).
        prob = 1.0 / (1.0 + np.exp(-labels[0]))  # (300, n_logits)
        n_q, n_logits = prob.shape

        # Top-k over the entire (query × class) score grid, k = num_queries.
        flat = prob.reshape(-1)
        if flat.size == 0:
            return _OnnxResult()
        k = min(n_q, max_det if max_det > 0 else n_q)
        topk_idx = np.argpartition(-flat, k - 1)[:k]
        topk_idx = topk_idx[np.argsort(-flat[topk_idx])]
        scores = flat[topk_idx]
        query_idx = topk_idx // n_logits
        class_idx = topk_idx % n_logits

        # Drop background slot AND apply conf threshold in one mask. Also
        # apply caller's class filter if any.
        bg_class = n_logits - 1
        keep = (class_idx != bg_class) & (scores >= conf_thr)
        if cls_filter is not None and len(cls_filter) > 0:
            keep &= np.isin(class_idx, list(cls_filter))
        if not keep.any():
            return _OnnxResult()
        scores = scores[keep]
        query_idx = query_idx[keep]
        cls_ids = class_idx[keep].astype(np.int64)

        # Gather boxes (cxcywh, normalized) for the surviving queries, then
        # convert to xyxy in pixel coords.
        cxcywh = dets[0, query_idx]  # (K, 4) cxcywh in [0,1]
        h0, w0 = orig_hw
        cxn = cxcywh[:, 0]
        cyn = cxcywh[:, 1]
        wn = cxcywh[:, 2]
        hn = cxcywh[:, 3]
        cx = cxn * w0
        cy = cyn * h0
        bw = wn * w0
        bh = hn * h0
        x1 = cx - bw / 2.0
        y1 = cy - bh / 2.0
        x2 = cx + bw / 2.0
        y2 = cy + bh / 2.0

        # Build per-box list for ultralytics-style iteration.
        boxes_list: list[_OnnxBox] = []
        for i in range(len(scores)):
            boxes_list.append(_OnnxBox(
                cls=np.array([cls_ids[i]], dtype=np.int64),
                conf=np.array([scores[i]], dtype=np.float32),
                xywhn=np.array([[cxn[i], cyn[i], wn[i], hn[i]]],
                                dtype=np.float32),
            ))

        return _OnnxResult(boxes=_Boxes(
            boxes_list=boxes_list,
            xyxy=np.stack([x1, y1, x2, y2], axis=1).astype(np.float32),
            cls=cls_ids,
            conf=scores.astype(np.float32),
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
