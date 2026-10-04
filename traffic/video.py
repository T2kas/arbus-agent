"""Frame decoding at a fixed processing rate, and browser-playable MP4 writing.

Every stage (detection, rendering, frame export for labeling) decodes through
`iter_frames`, so frame index N means the same picture everywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path: str | Path) -> tuple[float, int, int, int]:
    """(fps, frame_count, width, height) of a video file."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise IOError(f"cannot open video {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if not 1 <= fps <= 120:
        fps = 25.0
    return fps, n, w, h


def out_size(src_w: int, src_h: int, width: int) -> tuple[int, int]:
    """Processing size: at most `width` wide, aspect kept, both sides even."""
    w = min(width, src_w) if src_w else width
    h = round(src_h * w / src_w) if src_w else round(w * 9 / 16)
    return w - w % 2, h - h % 2


def iter_frames(path: str | Path, fps: float, width: int) -> Iterator[np.ndarray]:
    """Yield BGR frames resampled to `fps` and resized to the processing width."""
    import cv2

    src_fps, _, src_w, src_h = probe(path)
    w, h = out_size(src_w, src_h, width)
    step = src_fps / fps if fps < src_fps else 1.0
    cap = cv2.VideoCapture(str(path))
    i, next_pick = 0, 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if i + 1e-6 >= next_pick:
                next_pick += step
                if frame.shape[1] != w or frame.shape[0] != h:
                    frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                yield frame
            i += 1
    finally:
        cap.release()


def read_frame(path: str | Path, index: int, fps: float, width: int) -> np.ndarray | None:
    for i, frame in enumerate(iter_frames(path, fps, width)):
        if i == index:
            return frame
    return None


class Mp4Writer:
    """H.264 / yuv420p / faststart — plays in every browser, unlike cv2's mp4v."""

    def __init__(self, path: str | Path, size: tuple[int, int], fps: float):
        import imageio_ffmpeg

        self.gen = imageio_ffmpeg.write_frames(
            str(path), size, fps=fps, codec="libx264", pix_fmt_in="bgr24",
            pix_fmt_out="yuv420p", macro_block_size=2, quality=None,
            output_params=["-crf", "26", "-preset", "veryfast", "-movflags", "+faststart"],
        )
        self.gen.send(None)

    def write(self, frame_bgr: np.ndarray) -> None:
        self.gen.send(np.ascontiguousarray(frame_bgr))

    def close(self) -> None:
        self.gen.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
