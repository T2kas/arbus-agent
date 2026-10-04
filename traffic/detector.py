"""YOLOX vehicle detector on ONNX Runtime (CPU).

YOLOX is Apache-2.0; we deliberately do not use Ultralytics (AGPL). The official
ONNX exports are used as-is, so no torch is needed to run — only to fine-tune
(see traffic/README.md "Mokymas").
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from . import config


def model_path(name: str) -> Path:
    """`yolox_s` -> models/yolox_s.onnx; a path to an .onnx file is used as-is."""
    p = Path(name)
    if p.suffix == ".onnx":
        return p
    return config.MODELS_DIR / f"{name}.onnx"


def fetch_model(name: str = "yolox_s") -> Path:
    import requests

    if name not in config.YOLOX_MODELS:
        raise ValueError(f"unknown model {name}; choose from {config.YOLOX_MODELS}")
    dest = model_path(name)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{config.YOLOX_RELEASE}/{name}.onnx"
    tmp = dest.with_suffix(".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def letterbox(img: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float]:
    """YOLOX preprocessing: keep aspect, pad bottom/right with 114, HWC->CHW float."""
    import cv2

    h, w = img.shape[:2]
    r = min(size[0] / h, size[1] / w)
    resized = cv2.resize(img, (int(w * r), int(h * r)), interpolation=cv2.INTER_LINEAR)
    padded = np.full((size[0], size[1], 3), 114, dtype=np.uint8)
    padded[: resized.shape[0], : resized.shape[1]] = resized
    return np.ascontiguousarray(padded.transpose(2, 0, 1), dtype=np.float32), r


def decode(raw: np.ndarray, size: tuple[int, int], strides=(8, 16, 32)) -> np.ndarray:
    """Turn YOLOX grid outputs (N, 5+C) into absolute cx, cy, w, h (in input px)."""
    grids, expanded = [], []
    for s in strides:
        hs, ws = size[0] // s, size[1] // s
        xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        expanded.append(np.full((hs * ws, 1), s))
    grid = np.concatenate(grids, 0)
    stride = np.concatenate(expanded, 0)
    out = raw.copy()
    out[:, :2] = (out[:, :2] + grid) * stride
    out[:, 2:4] = np.exp(out[:, 2:4]) * stride
    return out


def postprocess(pred: np.ndarray, ratio: float, class_ids: list[int], conf: float,
                nms: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Decoded predictions -> (xyxy, confidence, class_id) for vehicle classes.

    NMS is class-agnostic: the detector often fires both "car" and "truck" on
    one van, and two boxes on one vehicle is exactly a double count.
    """
    import cv2

    cls_scores = pred[:, 5:][:, class_ids] * pred[:, 4:5]
    best = cls_scores.argmax(1)
    score = cls_scores[np.arange(len(best)), best]
    keep = score >= conf
    if not keep.any():
        return np.zeros((0, 4), np.float32), np.zeros(0, np.float32), np.zeros(0, int)
    boxes = pred[keep, :4]
    score, best = score[keep], best[keep]
    xyxy = np.empty_like(boxes)
    xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
    xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
    xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
    xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
    xyxy /= ratio
    xywh = np.c_[xyxy[:, :2], xyxy[:, 2:] - xyxy[:, :2]]
    idx = cv2.dnn.NMSBoxes(xywh.tolist(), score.tolist(), conf, nms)
    idx = np.array(idx, dtype=int).reshape(-1)
    return (xyxy[idx].astype(np.float32), score[idx].astype(np.float32),
            np.array(class_ids)[best[idx]].astype(int))


class Detector:
    def __init__(self, model: str = "yolox_s", threads: int | None = None):
        import onnxruntime as ort

        path = model_path(model)
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found — run: python -m traffic fetch-model {model}")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads or os.cpu_count() or 4
        self.session = ort.InferenceSession(str(path), opts,
                                            providers=["CPUExecutionProvider"])
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.size = (int(inp.shape[2]), int(inp.shape[3]))
        self.name = model
        self.class_ids = sorted(config.VEHICLE_CLASSES.values())

    def __call__(self, frame_bgr: np.ndarray, conf: float = config.CACHE_CONF,
                 nms: float = 0.45):
        blob, ratio = letterbox(frame_bgr, self.size)
        raw = self.session.run(None, {self.input_name: blob[None]})[0][0]
        return postprocess(decode(raw, self.size), ratio, self.class_ids, conf, nms)
