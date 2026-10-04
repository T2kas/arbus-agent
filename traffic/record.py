"""Recording (ffmpeg -> ~30 s segments) and retention cleanup."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, store
from .pipeline import ingest
from .video import ffmpeg_exe


def record_cmd(source: str, out_dir: Path, minutes: float, segment_s: int = 30,
               width: int = 960, fps: int = 15) -> list[str]:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "warning", "-y"]
    if source.startswith("rtsp://"):
        cmd += ["-rtsp_transport", "tcp"]
    cmd += [
        "-i", source, "-t", str(int(minutes * 60)),
        "-an",                                           # no audio, ever
        "-vf", f"scale='min({width},iw)':-2,fps={fps}",  # reduced resolution
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-f", "segment", "-segment_time", str(segment_s), "-reset_timestamps", "1",
        "-strftime", "1", str(out_dir / "%Y%m%dT%H%M%S.mp4"),
    ]
    return cmd


def record(conn, cam_id: str, minutes: float = 10, segment_s: int = 30,
           min_seconds: float = 20) -> list[str]:
    """Record a camera for `minutes`, then ingest every complete segment.

    Only cameras with permission_status = granted are recorded.
    """
    cam = store.get_camera(conn, cam_id)
    if cam is None:
        raise KeyError(cam_id)
    if cam["permission_status"] != "granted":
        raise PermissionError(f"kamera {cam_id}: permission_status={cam['permission_status']}"
                              " (įrašoma tik su 'granted')")
    if not cam["source_url"]:
        raise ValueError(f"kamera {cam_id}: nėra source_url")
    tmp = config.ROOT / "incoming" / cam_id
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(record_cmd(cam["source_url"], tmp, minutes, segment_s), check=False)
    from .video import probe

    ids = []
    for seg in sorted(tmp.glob("*.mp4")):
        try:
            fps, n, _, _ = probe(seg)
        except OSError:
            seg.unlink(missing_ok=True)
            continue
        if n / fps < min_seconds:          # cut-off first/last segment
            seg.unlink(missing_ok=True)
            continue
        ids.append(ingest(conn, cam_id, seg, move=True))
    return ids


def cleanup(conn, dry_run: bool = False) -> list[str]:
    """Delete clip media past retention: used 14 d, others 30 d. Rows stay
    (cv_count feeds the statistics, which are kept indefinitely)."""
    now = datetime.now(timezone.utc)
    removed = []
    for c in store.list_clips(conn):
        if c["status"] == "deleted":
            continue
        keep = config.KEEP_USED_DAYS if c["status"] == "used" else config.KEEP_UNUSED_DAYS
        created = datetime.fromisoformat(c["created_at"])
        if now - created < timedelta(days=keep):
            continue
        reviewed = conn.execute("select 1 from calibration where clip_id = ? limit 1",
                                (c["id"],)).fetchone()
        labeled = conn.execute("select 1 from labels where clip_id = ? limit 1",
                               (c["id"],)).fetchone()
        if reviewed or labeled:
            continue   # calibration / training material is kept
        removed.append(c["id"])
        if not dry_run:
            shutil.rmtree(store.clip_dir(c["camera_id"], c["id"]), ignore_errors=True)
            store.update_clip(conn, c["id"], status="deleted")
    return removed
