"""Per-camera counting parameters — everything the admin tool calibrates.

Geometry is stored in normalized coordinates (0..1 of frame width/height) so a
zone drawn on one resolution still fits clips recorded at another.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace


@dataclass(frozen=True)
class CameraParams:
    # Detection zone polygon, normalized [[x, y], ...]. Empty = whole frame.
    zone: tuple[tuple[float, float], ...] = ()
    # Counting line, normalized ((x1, y1), (x2, y2)). When set it replaces the
    # zone: a vehicle counts when it crosses the line, from either side unless
    # `direction` is also set.
    line: tuple[tuple[float, float], tuple[float, float]] | None = None
    # Direction a vehicle must travel to count, normalized [dx, dy] in image
    # coordinates (y grows downward). None = any direction.
    direction: tuple[float, float] | None = None
    classes: tuple[str, ...] = ("car", "truck", "bus", "motorcycle")

    # detector
    model: str = "yolox_s"
    conf: float = 0.35            # minimum detection confidence fed to the tracker
    nms: float = 0.45
    fps: float = 12.0             # processing frame rate
    width: int = 960              # processing frame width (height keeps aspect)

    # tracker (supervision ByteTrack)
    track_activation: float = 0.35
    lost_buffer: int = 30         # frames a lost track is kept for re-matching
    match_thresh: float = 0.8
    min_consecutive: int = 1

    # counting
    anchor: str = "bottom_center"  # point of the box tested against the zone
    min_frames: int = 3           # consecutive frames inside the zone to count
    min_travel: float = 0.01      # min travel along `direction`, fraction of width
    merge_gap: int = 12           # a new track this soon after a counted one...
    merge_dist: float = 0.04      # ...this close to where it vanished is the same vehicle
    skip_initial: bool = True     # vehicles already inside the zone on the first
                                  # frame did not enter during the round

    # privacy
    blur_plates: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["zone"] = [list(p) for p in self.zone]
        d["direction"] = list(self.direction) if self.direction else None
        d["line"] = [list(p) for p in self.line] if self.line else None
        d["classes"] = list(self.classes)
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "CameraParams":
        d = dict(d or {})
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in known}
        if "zone" in d:
            d["zone"] = tuple((float(x), float(y)) for x, y in (d["zone"] or []))
        if d.get("direction"):
            dx, dy = d["direction"]
            d["direction"] = (float(dx), float(dy))
        else:
            d["direction"] = None
        if d.get("line"):
            a, b = d["line"]
            d["line"] = ((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
        else:
            d["line"] = None
        if "classes" in d:
            d["classes"] = tuple(d["classes"])
        return cls(**d)

    def with_(self, **changes) -> "CameraParams":
        return replace(self, **changes)


# Fields that only change the tracking/counting stage: a search over these can
# reuse cached detections. Anything else (model, nms, fps, width) means
# re-running the detector.
COUNT_ONLY_FIELDS = {
    "zone", "line", "direction", "classes", "conf", "track_activation", "lost_buffer",
    "match_thresh", "min_consecutive", "anchor", "min_frames", "min_travel",
    "merge_gap", "merge_dist", "skip_initial",
}

DEFAULT_GRID: dict[str, list] = {
    "conf": [0.25, 0.35, 0.45],
    "min_frames": [2, 3, 5],
    "lost_buffer": [15, 30, 60],
    "match_thresh": [0.7, 0.8, 0.9],
}


@dataclass
class GridSpec:
    axes: dict[str, list] = field(default_factory=lambda: dict(DEFAULT_GRID))

    def variants(self, base: CameraParams) -> list[CameraParams]:
        out = [base]
        for name, values in self.axes.items():
            if name not in COUNT_ONLY_FIELDS:
                raise ValueError(f"{name} needs the detector re-run; not searchable")
            out = [p.with_(**{name: v}) for p in out for v in values]
        # keep the tracker activation in step with the confidence threshold
        out = [p.with_(track_activation=max(p.conf, 0.1)) if "conf" in self.axes else p
               for p in out]
        seen, unique = set(), []
        for p in out:
            key = tuple(sorted((k, str(v)) for k, v in p.to_dict().items()))
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique
