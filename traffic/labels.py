"""Frame labeling ("pažymėk mašinas kadre") and export for detector fine-tuning.

Labels are boxes the admin drew or corrected on sampled frames. Exported as a
COCO dataset that YOLOX trains on directly. All 80 COCO categories are kept in
the export so the class indices line up with the pretrained YOLOX head — with a
few hundred frames it is far better to fine-tune that head than to train a new
4-class one from scratch.
"""

from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

from . import config, store
from .params import CameraParams
from .video import iter_frames

# COCO 2017 categories: (id, name). YOLOX maps sorted ids -> class index 0..79.
COCO_CATEGORIES = [
    (1, "person"), (2, "bicycle"), (3, "car"), (4, "motorcycle"), (5, "airplane"),
    (6, "bus"), (7, "train"), (8, "truck"), (9, "boat"), (10, "traffic light"),
    (11, "fire hydrant"), (13, "stop sign"), (14, "parking meter"), (15, "bench"),
    (16, "bird"), (17, "cat"), (18, "dog"), (19, "horse"), (20, "sheep"), (21, "cow"),
    (22, "elephant"), (23, "bear"), (24, "zebra"), (25, "giraffe"), (27, "backpack"),
    (28, "umbrella"), (31, "handbag"), (32, "tie"), (33, "suitcase"), (34, "frisbee"),
    (35, "skis"), (36, "snowboard"), (37, "sports ball"), (38, "kite"),
    (39, "baseball bat"), (40, "baseball glove"), (41, "skateboard"), (42, "surfboard"),
    (43, "tennis racket"), (44, "bottle"), (46, "wine glass"), (47, "cup"), (48, "fork"),
    (49, "knife"), (50, "spoon"), (51, "bowl"), (52, "banana"), (53, "apple"),
    (54, "sandwich"), (55, "orange"), (56, "broccoli"), (57, "carrot"), (58, "hot dog"),
    (59, "pizza"), (60, "donut"), (61, "cake"), (62, "chair"), (63, "couch"),
    (64, "potted plant"), (65, "bed"), (67, "dining table"), (70, "toilet"), (72, "tv"),
    (73, "laptop"), (74, "mouse"), (75, "remote"), (76, "keyboard"), (77, "cell phone"),
    (78, "microwave"), (79, "oven"), (80, "toaster"), (81, "sink"), (82, "refrigerator"),
    (84, "book"), (85, "clock"), (86, "vase"), (87, "scissors"), (88, "teddy bear"),
    (89, "hair drier"), (90, "toothbrush"),
]
COCO_ID = {name: cid for cid, name in COCO_CATEGORIES}
LABEL_CLASSES = ("car", "truck", "bus", "motorcycle", "bicycle")


def clip_processing(clip) -> CameraParams:
    return CameraParams.from_dict(json.loads(clip["params_json"] or "{}"))


def frames_dir(clip) -> Path:
    return store.clip_dir(clip["camera_id"], clip["id"]) / "frames"


def ensure_frames(clip, every_s: float = 1.0) -> list[int]:
    """Write sampled processing frames as JPEGs once; return their indices.

    Indices are processing-frame indices, the same ones the detection cache and
    tracks.json use, so a label on frame N can be compared with detections on N.
    """
    import cv2

    p = clip_processing(clip)
    out = frames_dir(clip)
    step = max(1, round(p.fps * every_s))
    done = out / "index.json"
    if done.exists():
        return json.loads(done.read_text())
    out.mkdir(parents=True, exist_ok=True)
    indices = []
    for i, frame in enumerate(iter_frames(clip["raw_path"], p.fps, p.width)):
        if i % step == 0:
            cv2.imwrite(str(out / f"{i:05d}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            indices.append(i)
    done.write_text(json.dumps(indices))
    return indices


def frame_path(clip, index: int) -> Path:
    return frames_dir(clip) / f"{index:05d}.jpg"


def predictions(clip, index: int, min_conf: float = 0.3) -> list[dict]:
    """Detector boxes on a frame (from the cache) to pre-fill the labeling canvas."""
    import numpy as np

    if not clip["dets_path"] or not Path(clip["dets_path"]).exists():
        return []
    names = {v: k for k, v in config.VEHICLE_CLASSES.items()}
    with np.load(clip["dets_path"]) as z:
        keep = (z["frame"] == index) & (z["conf"] >= min_conf)
        return [{"cls": names.get(int(c), "car"), "box": [round(float(v), 1) for v in b],
                 "conf": round(float(s), 3)}
                for b, s, c in zip(z["xyxy"][keep], z["conf"][keep], z["cls"][keep])]


def export_coco(conn, out_dir: str | Path, cam_id: str | None = None, val_frac: float = 0.15,
                seed: int = 7) -> dict:
    out = Path(out_dir)
    rows = [r for r in store.all_labels(conn, cam_id)]
    if not rows:
        raise ValueError("nėra pažymėtų kadrų")
    # split by clip, not by frame: neighbouring frames of one clip are near
    # duplicates and would leak between train and val
    clips = sorted({r["clip_id"] for r in rows})
    random.Random(seed).shuffle(clips)
    n_val = max(1, round(len(clips) * val_frac)) if len(clips) > 1 else 0
    val_clips = set(clips[:n_val])
    splits = {"train2017": [], "val2017": []}
    for r in rows:
        splits["val2017" if r["clip_id"] in val_clips else "train2017"].append(r)

    (out / "annotations").mkdir(parents=True, exist_ok=True)
    summary = {}
    ann_id = 1
    for split, items in splits.items():
        img_dir = out / split
        img_dir.mkdir(parents=True, exist_ok=True)
        images, anns = [], []
        for img_id, r in enumerate(items, start=1):
            clip = store.get_clip(conn, r["clip_id"])
            src = frame_path(clip, r["frame_index"])
            if not src.exists():
                ensure_frames(clip)
            name = f"{r['camera_id']}_{r['clip_id']}_{r['frame_index']:05d}.jpg"
            shutil.copy2(src, img_dir / name)
            images.append({"id": img_id, "file_name": name, "width": r["width"],
                           "height": r["height"]})
            for b in json.loads(r["boxes_json"]):
                x1, y1, x2, y2 = b["box"]
                w, h = max(x2 - x1, 0), max(y2 - y1, 0)
                if w < 2 or h < 2:
                    continue
                anns.append({"id": ann_id, "image_id": img_id,
                             "category_id": COCO_ID[b.get("cls", "car")],
                             "bbox": [round(x1, 1), round(y1, 1), round(w, 1), round(h, 1)],
                             "area": round(w * h, 1), "iscrowd": 0})
                ann_id += 1
        doc = {"images": images, "annotations": anns,
               "categories": [{"id": i, "name": n} for i, n in COCO_CATEGORIES]}
        (out / "annotations" / f"instances_{split}.json").write_text(json.dumps(doc))
        summary[split] = {"images": len(images), "boxes": len(anns)}
    (out / "arbus_yolox_exp.py").write_text(EXP_TEMPLATE.format(data_dir=out.resolve().as_posix()),
                                            encoding="utf-8")
    return summary


EXP_TEMPLATE = '''# YOLOX experiment for fine-tuning yolox_s on Arbus traffic frames.
# Usage (from a YOLOX checkout, see traffic/README.md "Mokymas"):
#   python tools/train.py -f {data_dir}/arbus_yolox_exp.py -c yolox_s.pth -d 1 -b 8 --fp16 -o
#   python tools/export_onnx.py -f {data_dir}/arbus_yolox_exp.py -c YOLOX_outputs/arbus_yolox_exp/best_ckpt.pth --output-name yolox_s_arbus.onnx
import os
from yolox.exp import Exp as MyExp


class Exp(MyExp):
    def __init__(self):
        super().__init__()
        self.depth = 0.33
        self.width = 0.50
        self.num_classes = 80          # keep the COCO head; we fine-tune it
        self.data_dir = "{data_dir}"
        self.train_ann = "instances_train2017.json"
        self.val_ann = "instances_val2017.json"
        self.max_epoch = 30
        self.no_aug_epochs = 5
        self.warmup_epochs = 1
        self.basic_lr_per_img = 0.002 / 64.0   # low LR: fine-tuning, not training
        self.eval_interval = 5
        self.exp_name = os.path.split(os.path.realpath(__file__))[1].split(".")[0]
'''
