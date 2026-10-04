"""Paths and defaults for the traffic worker. Override paths with env vars."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("TRAFFIC_HOME", "data/traffic"))
DB_PATH = Path(os.environ.get("TRAFFIC_DB", ROOT / "traffic.db"))
CLIPS_DIR = ROOT / "clips"
LABELS_DIR = ROOT / "labels"
MODELS_DIR = Path(os.environ.get("TRAFFIC_MODELS", "models"))

# YOLOX ONNX releases (Apache-2.0). `python -m traffic fetch-model` downloads one.
YOLOX_RELEASE = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"
YOLOX_MODELS = ("yolox_nano", "yolox_tiny", "yolox_s", "yolox_m", "yolox_l")

# COCO class indices (0-based, as YOLOX outputs them) for the vehicle classes.
VEHICLE_CLASSES = {"bicycle": 1, "car": 2, "motorcycle": 3, "bus": 5, "truck": 7}

# Detections are cached at this confidence so a parameter search can raise the
# threshold later without running the detector again.
CACHE_CONF = 0.10

LOCAL_TZ = "Europe/Vilnius"

# Retention (days): clips used in a round are kept for disputes, others expire.
KEEP_USED_DAYS = 14
KEEP_UNUSED_DAYS = 30

# A camera goes into the game only after this much verified accuracy.
READY_MIN_REVIEWED = 50
READY_MIN_IN_BUCKET = 0.95

# Buckets used for "in the right interval" accuracy until a camera has enough
# reviewed clips to derive its own (lower bounds; the last is open-ended).
DEFAULT_BUCKET_LOWS = (0, 5, 9, 13)

ADMIN_HOST = "127.0.0.1"
ADMIN_PORT = int(os.environ.get("TRAFFIC_ADMIN_PORT", "8765"))
