"""Reject clips the count cannot be trusted on: dark, fogged/blurred, frozen,
or with a tracker that keeps losing vehicles. Simple metrics, explicit reasons.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class QualityRules:
    min_brightness: float = 45.0      # mean gray 0..255; unlit night ~ 15-35
    min_sharpness: float = 25.0       # Laplacian variance; fog / dirty lens is low
    min_motion: float = 0.4           # mean abs frame diff; a frozen feed is ~0
    max_fragmentation: float = 0.45   # share of tracks shorter than 3 frames
    min_frames: int = 50


class FrameStats:
    """Accumulates cheap per-frame statistics during the detection pass."""

    def __init__(self):
        self.brightness: list[float] = []
        self.sharpness: list[float] = []
        self.motion: list[float] = []
        self.dets: list[int] = []
        self._prev = None

    def add(self, frame_bgr: np.ndarray, n_dets: int) -> None:
        import cv2

        small = cv2.resize(frame_bgr, (320, max(2, round(320 * frame_bgr.shape[0] / frame_bgr.shape[1]))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        self.brightness.append(float(gray.mean()))
        self.sharpness.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
        if self._prev is not None:
            self.motion.append(float(np.abs(gray.astype(np.int16) - self._prev).mean()))
        self._prev = gray.astype(np.int16)
        self.dets.append(n_dets)

    def summary(self) -> dict:
        med = lambda xs: round(float(np.median(xs)), 2) if xs else 0.0
        return {
            "frames": len(self.brightness),
            "brightness": med(self.brightness),
            "sharpness": med(self.sharpness),
            "motion": med(self.motion),
            "dets_per_frame": round(float(np.mean(self.dets)), 2) if self.dets else 0.0,
        }


def judge(metrics: dict, rules: QualityRules = QualityRules()) -> list[str]:
    """Lithuanian rejection reasons; empty list = clip is usable."""
    reasons = []
    if metrics.get("frames", 0) < rules.min_frames:
        reasons.append(f"per trumpas klipas ({metrics.get('frames', 0)} kadrų)")
    if metrics.get("brightness", 255) < rules.min_brightness:
        reasons.append(f"per tamsu (šviesumas {metrics['brightness']})")
    if metrics.get("sharpness", 1e9) < rules.min_sharpness:
        reasons.append(f"neryškus vaizdas / rūkas (ryškumas {metrics['sharpness']})")
    if metrics.get("motion", 1e9) < rules.min_motion:
        reasons.append(f"užstrigęs vaizdas (judesys {metrics['motion']})")
    if metrics.get("n_tracks", 0) >= 5 and metrics.get("fragmentation", 0) > rules.max_fragmentation:
        reasons.append(f"sekimas trūkinėja (fragmentacija {metrics['fragmentation']})")
    return reasons
