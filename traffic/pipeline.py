"""Clip processing: detect (cached) -> track + count -> quality -> annotated MP4.

Detection is the slow part (~0.1 s/frame for yolox_s on a laptop CPU), so its
output is cached per clip at a low confidence. Everything the admin tunes —
zone, direction, thresholds, tracker settings — only re-runs the fast
tracking/counting stage on that cache.
"""

from __future__ import annotations

import json
import shutil
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import config, store
from .counting import CountResult, count_clip, line_pixels, zone_pixels
from .params import CameraParams
from .quality import FrameStats, QualityRules, judge
from .video import iter_frames, probe

warnings.filterwarnings("ignore", message=".*ByteTrack.*deprecated", category=FutureWarning)

_detectors: dict = {}


def get_detector(model: str):
    from .detector import Detector

    if model not in _detectors:
        _detectors[model] = Detector(model)
    return _detectors[model]


def _cache_key(p: CameraParams) -> dict:
    return {"model": p.model, "nms": p.nms, "fps": p.fps, "width": p.width}


def detect_clip(raw_path: str | Path, params: CameraParams, out_path: str | Path,
                progress=None) -> dict:
    """Run the detector over every processing frame; save and return the cache."""
    det = get_detector(params.model)
    frames, xyxy, conf, cls = [], [], [], []
    stats = FrameStats()
    w = h = 0
    _, n_src, _, _ = probe(raw_path)
    for i, frame in enumerate(iter_frames(raw_path, params.fps, params.width)):
        h, w = frame.shape[:2]
        b, s, c = det(frame, conf=config.CACHE_CONF, nms=params.nms)
        frames.append(np.full(len(b), i, np.int32))
        xyxy.append(b)
        conf.append(s)
        cls.append(c)
        stats.add(frame, int((s >= params.conf).sum()))
        if progress and i % 10 == 0:
            progress(i)
    cache = {
        "frame": np.concatenate(frames) if frames else np.zeros(0, np.int32),
        "xyxy": np.concatenate(xyxy) if xyxy else np.zeros((0, 4), np.float32),
        "conf": np.concatenate(conf) if conf else np.zeros(0, np.float32),
        "cls": np.concatenate(cls) if cls else np.zeros(0, int),
        "n_frames": len(stats.brightness),
        "fps": params.fps, "width": w, "height": h,
        "meta": json.dumps({**_cache_key(params), "stats": stats.summary()}),
    }
    np.savez_compressed(out_path, **cache)
    return cache


def load_cache(path: str | Path | None, params: CameraParams) -> dict | None:
    if not path or not Path(path).exists():
        return None
    with np.load(path, allow_pickle=False) as z:
        cache = {k: z[k] for k in z.files}
    meta = json.loads(str(cache["meta"]))
    if {k: meta.get(k) for k in _cache_key(params)} != _cache_key(params):
        return None   # detector settings changed -> must re-detect
    cache["stats"] = meta.get("stats", {})
    return cache


def write_tracks(path: Path, params: CameraParams, cache: dict, result: CountResult) -> None:
    w, h = int(cache["width"]), int(cache["height"])
    names = {v: k for k, v in config.VEHICLE_CLASSES.items()}
    first_frame: dict[int, int] = {tid: f for f, tid in result.events}
    tracks: dict[int, dict] = {}
    for r in result.rows:
        tid = int(r[1])
        t = tracks.setdefault(tid, {"id": tid, "cls": names.get(int(r[6]), str(int(r[6]))),
                                    "counted_frame": first_frame.get(tid), "frames": []})
        t["frames"].append([int(r[0]), *[round(float(v), 1) for v in r[2:6]]])
    doc = {
        "fps": result.fps, "width": w, "height": h, "n_frames": result.n_frames,
        "zone": zone_pixels(params, w, h).tolist(), "direction": params.direction,
        "line": line_pixels(params, w, h).tolist() if params.line else None,
        "count": result.count,
        "events": [{"frame": f, "t": round(f / result.fps, 2), "track_id": tid}
                   for f, tid in result.events],
        "tracks": list(tracks.values()),
    }
    path.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")


def process_clip(conn, clip_id: str, params: CameraParams | None = None,
                 render_video: bool = True, rules: QualityRules = QualityRules(),
                 progress=None) -> dict:
    """Full processing of one clip; updates the clip row and returns a summary."""
    from .render import render

    clip = store.get_clip(conn, clip_id)
    if clip is None:
        raise KeyError(clip_id)
    params = params or store.camera_params(conn, clip["camera_id"])
    out = store.clip_dir(clip["camera_id"], clip_id)
    out.mkdir(parents=True, exist_ok=True)
    store.update_clip(conn, clip_id, status="processing", error=None)
    try:
        dets_path = out / "dets.npz"
        cache = load_cache(dets_path, params)
        if cache is None:
            cache = detect_clip(clip["raw_path"], params, dets_path, progress)
            cache["stats"] = json.loads(str(cache["meta"]))["stats"]
        result = count_clip(cache, params)
        quality = dict(cache["stats"])
        quality.update(n_tracks=result.n_tracks,
                       fragmentation=round(result.short_tracks / max(result.n_tracks, 1), 3))
        reasons = judge(quality, rules)
        quality["reasons"] = reasons
        tracks_path = out / "tracks.json"
        write_tracks(tracks_path, params, cache, result)
        annotated = out / "annotated.mp4"
        if render_video:
            render(clip["raw_path"], annotated, params, result)
        store.update_clip(
            conn, clip_id, status="rejected" if reasons else "approved",
            cv_count=result.count, quality_json=quality, params_json=params.to_dict(),
            dets_path=str(dets_path), tracks_path=str(tracks_path),
            annotated_path=str(annotated) if render_video else clip["annotated_path"],
            processed_at=store.now())
        return {"clip_id": clip_id, "count": result.count, "quality": quality,
                "status": "rejected" if reasons else "approved"}
    except Exception as e:
        store.update_clip(conn, clip_id, status="error", error=f"{type(e).__name__}: {e}")
        raise


def recount(conn, clip_id: str, params: CameraParams) -> CountResult | None:
    """Count with other parameters on the cached detections (no detector run)."""
    clip = store.get_clip(conn, clip_id)
    cache = load_cache(clip["dets_path"], params) if clip else None
    return count_clip(cache, params) if cache is not None else None


def ingest(conn, cam_id: str, src: str | Path, recorded_at: str | None = None,
           move: bool = False) -> str:
    """Copy a video into storage and register it as a clip (status processing)."""
    src = Path(src)
    if store.get_camera(conn, cam_id) is None:
        raise KeyError(f"camera {cam_id} not found")
    if recorded_at is None:
        recorded_at = recorded_at_from_name(src.stem) or datetime.fromtimestamp(
            src.stat().st_mtime, ZoneInfo(config.LOCAL_TZ)).isoformat(timespec="seconds")
    clip_id = store.add_clip(conn, cam_id, "", recorded_at)
    dest_dir = store.clip_dir(cam_id, clip_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"raw{src.suffix.lower() or '.mp4'}"
    (shutil.move if move else shutil.copy2)(str(src), str(dest))
    store.update_clip(conn, clip_id, raw_path=str(dest))
    return clip_id


def recorded_at_from_name(stem: str) -> str | None:
    """`20261003T081530` (as `record` names segments) -> ISO time, Vilnius."""
    import re

    m = re.search(r"(\d{8}T\d{6})", stem)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(config.LOCAL_TZ))
    return dt.isoformat(timespec="seconds")
