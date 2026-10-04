"""Burn the overlay into the video (v1): blue zone, a box per tracked vehicle
that turns green once counted, and a big "Pravažiavo: N" counter.

The counter is drawn from the same count events that become `cv_count`, so the
number on screen and the number that settles the round cannot disagree.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

from .counting import CountResult, line_pixels, zone_pixels
from .params import CameraParams
from .video import Mp4Writer, iter_frames

ZONE_BGR = (235, 140, 30)       # blue
TRACK_BGR = (235, 235, 235)     # not (yet) counted
COUNTED_BGR = (60, 200, 60)     # counted

_FONTS = (
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)


@lru_cache(maxsize=8)
def _font(size: int):
    from PIL import ImageFont

    for path in _FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


@lru_cache(maxsize=256)
def _badge(text: str, height: int) -> np.ndarray:
    """Pre-rendered BGRA counter badge (PIL, so Lithuanian letters render)."""
    from PIL import Image, ImageDraw

    font = _font(int(height * 0.62))
    pad = int(height * 0.3)
    w = int(font.getlength(text)) + 2 * pad
    img = Image.new("RGBA", (w, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, height - 1), radius=height // 4, fill=(10, 14, 24, 200))
    d.text((pad, height / 2), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
    rgba = np.array(img)
    return rgba[..., [2, 1, 0, 3]]


def _blit(frame: np.ndarray, bgra: np.ndarray, x: int, y: int) -> None:
    h, w = bgra.shape[:2]
    h, w = min(h, frame.shape[0] - y), min(w, frame.shape[1] - x)
    a = bgra[:h, :w, 3:4].astype(np.float32) / 255
    roi = frame[y:y + h, x:x + w].astype(np.float32)
    frame[y:y + h, x:x + w] = (roi * (1 - a) + bgra[:h, :w, :3] * a).astype(np.uint8)


def draw_zone(frame: np.ndarray, poly: np.ndarray, alpha: float = 0.28) -> None:
    import cv2

    overlay = frame.copy()
    cv2.fillPoly(overlay, [poly], ZONE_BGR)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, dst=frame)
    cv2.polylines(frame, [poly], True, ZONE_BGR, 2, cv2.LINE_AA)


GLOW_BGR = np.array([255, 150, 40], np.float32)    # electric blue
CORE_BGR = np.array([255, 235, 190], np.float32)   # near-white blue core
PULSE_FRAMES = 6                                   # flash after each count


class GlowLine:
    """Neon counting line: a wide blurred halo plus a bright core.

    The halo is blurred once per clip and only alpha-blended per frame, so the
    glow costs a few milliseconds a frame. `strength` > 1 makes it flare —
    used for a short pulse every time a vehicle is counted.
    """

    def __init__(self, seg: np.ndarray, w: int, h: int):
        import cv2

        a, b = (tuple(int(v) for v in seg[0]), tuple(int(v) for v in seg[1]))
        thick = max(5, round(min(w, h) * 0.02))
        halo = np.zeros((h, w), np.float32)
        cv2.line(halo, a, b, 1.0, thick, cv2.LINE_AA)
        halo = cv2.GaussianBlur(halo, (0, 0), thick * 0.9)
        self.halo = (halo / max(float(halo.max()), 1e-6))[..., None]
        core = np.zeros((h, w), np.float32)
        cv2.line(core, a, b, 1.0, max(2, thick // 4), cv2.LINE_AA)
        self.core = cv2.GaussianBlur(core, (0, 0), 1.0)[..., None]
        ys, xs = np.nonzero((self.halo[..., 0] > 0.01) | (self.core[..., 0] > 0.01))
        self.box = (ys.min(), ys.max() + 1, xs.min(), xs.max() + 1) if len(ys) else None

    def draw(self, frame: np.ndarray, strength: float = 1.0) -> None:
        if self.box is None:
            return
        y0, y1, x0, x1 = self.box
        roi = frame[y0:y1, x0:x1].astype(np.float32)
        ha = self.halo[y0:y1, x0:x1] * min(0.5 * strength, 0.75)   # cars stay visible
        roi = roi * (1 - ha) + GLOW_BGR * ha
        ca = np.clip(self.core[y0:y1, x0:x1] * min(strength, 1.0), 0, 1)
        roi = roi * (1 - ca) + CORE_BGR * ca
        frame[y0:y1, x0:x1] = roi.astype(np.uint8)


def draw_direction(frame: np.ndarray, poly: np.ndarray, direction) -> None:
    import cv2

    if not direction:
        return
    h, w = frame.shape[:2]
    c = poly.mean(0)
    d = np.array(direction, float) * [w, h]
    n = np.linalg.norm(d)
    if n == 0:
        return
    d = d / n * min(w, h) * 0.08
    cv2.arrowedLine(frame, tuple((c - d).astype(int)), tuple((c + d).astype(int)),
                    (255, 255, 255), 2, cv2.LINE_AA, tipLength=0.35)


def blur_plates(frame: np.ndarray, boxes: np.ndarray) -> None:
    """Blur the lower part of each vehicle box, where plates (and drivers) are."""
    import cv2

    for x1, y1, x2, y2 in boxes.astype(int):
        y0 = y1 + int((y2 - y1) * 0.55)
        x1, x2 = max(x1, 0), min(x2, frame.shape[1])
        y0, y2 = max(y0, 0), min(y2, frame.shape[0])
        if x2 - x1 > 4 and y2 - y0 > 4:
            roi = frame[y0:y2, x1:x2]
            k = max(3, ((x2 - x1) // 6) | 1)
            frame[y0:y2, x1:x2] = cv2.GaussianBlur(roi, (k, k), 0)


def render(raw_path: str | Path, out_path: str | Path, params: CameraParams,
           result: CountResult) -> None:
    import cv2

    rows = result.rows
    by_frame: dict[int, np.ndarray] = {}
    if len(rows):
        f = rows[:, 0].astype(int)
        for i in np.unique(f):
            by_frame[int(i)] = rows[f == i]
    events = sorted(e[0] for e in result.events)

    writer = None
    try:
        count, ei = 0, 0
        for fi, frame in enumerate(iter_frames(raw_path, params.fps, params.width)):
            if writer is None:
                h, w = frame.shape[:2]
                poly = zone_pixels(params, w, h)
                seg = line_pixels(params, w, h)
                glow = GlowLine(seg, w, h) if seg is not None else None
                last_event = -10 ** 6
                badge_h = max(28, h // 11)
                writer = Mp4Writer(out_path, (w, h), params.fps)
            while ei < len(events) and events[ei] <= fi:
                count += 1
                last_event = events[ei]
                ei += 1
            here = by_frame.get(fi)
            if params.blur_plates and here is not None:
                blur_plates(frame, here[:, 2:6])
            if glow is not None:
                since = fi - last_event
                pulse = 1 - since / PULSE_FRAMES if 0 <= since < PULSE_FRAMES else 0.0
                glow.draw(frame, 1.0 + 0.9 * pulse)
                draw_direction(frame, seg, params.direction)
            else:
                draw_zone(frame, poly)
                draw_direction(frame, poly, params.direction)
            if here is not None:
                for r in here:
                    x1, y1, x2, y2 = r[2:6].astype(int)
                    color = COUNTED_BGR if r[8] > 0 else TRACK_BGR
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3 if r[8] > 0 else 2,
                                  cv2.LINE_AA)
            _blit(frame, _badge(f"Pravažiavo: {count}", badge_h), 12, 12)
            writer.write(frame)
    finally:
        if writer is not None:
            writer.close()
