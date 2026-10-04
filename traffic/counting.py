"""Vehicle counting: ByteTrack tracks + a polygon zone + a direction rule.

A vehicle is counted once: the first time its track has been inside the zone
for `min_frames` consecutive frames while having travelled at least
`min_travel` along the configured direction. Two failure modes are handled
explicitly because they are what makes a count wrong:

* flicker — a one-frame false detection inside the zone. Needs `min_frames`.
* already there — a vehicle inside the zone on the clip's first frame did not
  enter during the round and is not counted (`skip_initial`).
* ID switch — the tracker loses a vehicle (occlusion, a missed frame) and gives
  it a new ID a moment later. Without care that is a double count. A new track
  that appears where a recently vanished track was predicted to be inherits
  that track's history, including "already counted".

Line mode (`params.line`): instead of a zone, one line. A vehicle counts when
its anchor crosses the drawn segment and stays on the other side for
`min_frames` frames — from either side, unless `direction` is set. Each track
counts at most once, so a vehicle wobbling on the line is not counted twice.

`ZoneCounter` is pure (numpy only) so the rule is unit-testable; `count_clip`
wires it to supervision's ByteTrack and PolygonZone over cached detections, so
re-counting with new parameters never re-runs the detector.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config
from .params import CameraParams


@dataclass
class _Track:
    first: np.ndarray
    last: np.ndarray
    last_frame: int
    velocity: np.ndarray = field(default_factory=lambda: np.zeros(2))
    streak: int = 0
    counted: bool = False
    linked: bool = False      # a successor already inherited this track
    excluded: bool = False    # inside the zone when the clip started
    side0: int = 0            # line mode: side of the line the track started on
    crossed: bool = False     # line mode: crossed the segment, confirming


class ZoneCounter:
    def __init__(self, params: CameraParams, frame_w: int, frame_h: int):
        self.p = params
        self.w, self.h = frame_w, frame_h
        d = np.array(params.direction or (0.0, 0.0), dtype=float)
        # normalized direction -> pixel space, then unit length
        d = d * np.array([frame_w, frame_h])
        n = np.linalg.norm(d)
        self.direction = d / n if n > 0 else None
        self.tracks: dict[int, _Track] = {}
        self.events: list[tuple[int, int]] = []   # (frame, track_id)
        self.line = None
        if params.line:
            (x1, y1), (x2, y2) = params.line
            self.line = (np.array([x1 * (frame_w - 1), y1 * (frame_h - 1)]),
                         np.array([x2 * (frame_w - 1), y2 * (frame_h - 1)]))
            self.dead_band = 0.004 * frame_w   # on the line itself = no side yet

    def _side(self, pt: np.ndarray) -> int:
        a, b = self.line
        ab = b - a
        dist = (ab[0] * (pt[1] - a[1]) - ab[1] * (pt[0] - a[0])) / (np.linalg.norm(ab) or 1)
        return 0 if abs(dist) < self.dead_band else (1 if dist > 0 else -1)

    def _crosses_segment(self, p: np.ndarray, q: np.ndarray) -> bool:
        """Does the move p -> q pass through the drawn segment (not its extension)?"""
        a, b = self.line
        ab, pq = b - a, q - p
        denom = ab[0] * pq[1] - ab[1] * pq[0]
        if abs(denom) < 1e-9:
            return False
        ap = p - a
        s = (ap[0] * pq[1] - ap[1] * pq[0]) / denom      # position along the line
        return -0.03 <= s <= 1.03

    def _line_step(self, t: _Track, prev: np.ndarray | None, pt: np.ndarray) -> bool:
        """Advance the line-crossing state; True when the track should count now."""
        side = self._side(pt)
        if t.side0 == 0:
            t.side0 = side
            return False
        if side == t.side0:
            t.crossed, t.streak = False, 0
            return False
        if side == -t.side0:
            if not t.crossed and prev is not None and self._crosses_segment(prev, pt):
                t.crossed = True
            if t.crossed:
                t.streak += 1
        if not t.crossed or t.counted or t.streak < self.p.min_frames:
            return False
        if self.direction is not None and float((t.last - t.first) @ self.direction) <= 0:
            return False
        return True

    def _predecessor(self, frame: int, point: np.ndarray, present: set[int]) -> _Track | None:
        best, best_d = None, self.p.merge_dist * self.w
        for tid, t in self.tracks.items():
            gap = frame - t.last_frame
            if tid in present or t.linked or not (0 < gap <= self.p.merge_gap):
                continue
            predicted = t.last + t.velocity * gap
            dist = float(np.linalg.norm(point - predicted))
            if dist <= best_d:
                best, best_d = t, dist
        return best

    def _travel(self, t: _Track) -> float:
        disp = t.last - t.first
        if self.direction is None:
            return float(np.linalg.norm(disp))
        return float(disp @ self.direction)

    def update(self, frame: int, track_ids, anchors, in_zone) -> list[int]:
        """Feed one frame; returns the track IDs counted on this frame."""
        counted_now: list[int] = []
        present = {int(t) for t in track_ids}
        for tid, pt, inside in zip(track_ids, np.asarray(anchors, float), in_zone):
            tid = int(tid)
            t = self.tracks.get(tid)
            prev = None
            if t is None:
                t = _Track(first=pt.copy(), last=pt.copy(), last_frame=frame)
                parent = self._predecessor(frame, pt, present)
                if parent is not None:
                    parent.linked = True
                    t.first = parent.first.copy()
                    t.counted = parent.counted
                    t.excluded = parent.excluded
                    t.side0, t.crossed = parent.side0, parent.crossed
                    t.streak = parent.streak if (inside or self.line is not None) else 0
                    t.velocity = parent.velocity.copy()
                    prev = parent.last.copy()    # a crossing during the gap still counts
                elif self.p.skip_initial and frame <= 1 and inside and self.line is None:
                    t.excluded = True
                self.tracks[tid] = t
            else:
                prev = t.last.copy()
                gap = max(frame - t.last_frame, 1)
                v = (pt - t.last) / gap
                t.velocity = 0.6 * t.velocity + 0.4 * v
                t.last = pt.copy()
                t.last_frame = frame
            if self.line is not None:
                if self._line_step(t, prev, pt):
                    t.counted = True
                    self.events.append((frame, tid))
                    counted_now.append(tid)
                continue
            t.streak = t.streak + 1 if inside else 0
            if (not t.counted and not t.excluded and t.streak >= self.p.min_frames
                    and self._travel(t) >= self.p.min_travel * self.w):
                t.counted = True
                self.events.append((frame, tid))
                counted_now.append(tid)
        return counted_now

    def is_counted(self, tid: int) -> bool:
        t = self.tracks.get(int(tid))
        return bool(t and t.counted)

    @property
    def count(self) -> int:
        return len(self.events)


def line_pixels(params: CameraParams, w: int, h: int) -> np.ndarray | None:
    if not params.line:
        return None
    return np.array([[round(x * (w - 1)), round(y * (h - 1))] for x, y in params.line],
                    dtype=np.int32)


def zone_pixels(params: CameraParams, w: int, h: int) -> np.ndarray:
    if not params.zone:
        return np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.int32)
    return np.array([[round(x * (w - 1)), round(y * (h - 1))] for x, y in params.zone],
                    dtype=np.int32)


@dataclass
class CountResult:
    count: int
    events: list[tuple[int, int]]           # (frame, track_id)
    rows: np.ndarray                         # (K, 9): f, id, x1, y1, x2, y2, cls, conf, counted
    n_tracks: int
    short_tracks: int                        # tracks shorter than 3 frames (fragmentation)
    n_frames: int
    fps: float


def split_by_frame(cache: dict) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    frames = cache["frame"]
    n = int(cache["n_frames"])
    order = np.argsort(frames, kind="stable")
    f = frames[order]
    bounds = np.searchsorted(f, np.arange(n + 1))
    xyxy, conf, cls = cache["xyxy"][order], cache["conf"][order], cache["cls"][order]
    return [(xyxy[a:b], conf[a:b], cls[a:b]) for a, b in zip(bounds[:-1], bounds[1:])]


TRACK_FIELDS = ("classes", "conf", "track_activation", "lost_buffer", "match_thresh",
                "min_consecutive")


def track_key(params: CameraParams) -> tuple:
    """Parameters that change the tracker's output. Variants sharing this key
    share one tracking run; only the (cheap) counting differs."""
    return tuple(getattr(params, f) for f in TRACK_FIELDS)


def track_clip(cache: dict, params: CameraParams, per_frame=None) -> list:
    """ByteTrack over cached detections -> tracked sv.Detections per frame."""
    import supervision as sv

    per_frame = per_frame if per_frame is not None else split_by_frame(cache)
    wanted = np.array([config.VEHICLE_CLASSES[c] for c in params.classes
                       if c in config.VEHICLE_CLASSES])
    tracker = sv.ByteTrack(
        track_activation_threshold=params.track_activation,
        lost_track_buffer=params.lost_buffer,
        minimum_matching_threshold=params.match_thresh,
        frame_rate=max(int(round(float(cache["fps"]))), 1),
        minimum_consecutive_frames=params.min_consecutive,
    )
    out = []
    for xyxy, conf, cls in per_frame:
        keep = (conf >= params.conf) & np.isin(cls, wanted)
        dets = sv.Detections(xyxy=xyxy[keep].astype(np.float32),
                             confidence=conf[keep].astype(np.float32),
                             class_id=cls[keep].astype(int))
        out.append(tracker.update_with_detections(dets))
    return out


def count_tracked(tracked_frames: list, params: CameraParams, w: int, h: int,
                  fps: float) -> CountResult:
    """Apply the zone + direction counting rule to tracked detections."""
    import supervision as sv

    anchor = sv.Position.CENTER if params.anchor == "center" else sv.Position.BOTTOM_CENTER
    zone = (None if params.line else
            sv.PolygonZone(polygon=zone_pixels(params, w, h), triggering_anchors=(anchor,)))
    counter = ZoneCounter(params, w, h)

    rows: list[list[float]] = []
    lengths: dict[int, int] = {}
    for f, tracked in enumerate(tracked_frames):
        if len(tracked) == 0:
            counter.update(f, [], np.zeros((0, 2)), [])
            continue
        inside = zone.trigger(tracked) if zone is not None else np.ones(len(tracked), bool)
        anchors = tracked.get_anchors_coordinates(anchor)
        counter.update(f, tracked.tracker_id, anchors, inside)
        for i, tid in enumerate(tracked.tracker_id):
            lengths[int(tid)] = lengths.get(int(tid), 0) + 1
            x1, y1, x2, y2 = tracked.xyxy[i]
            rows.append([f, int(tid), x1, y1, x2, y2, int(tracked.class_id[i]),
                         float(tracked.confidence[i]) if tracked.confidence is not None else 0.0,
                         1.0 if counter.is_counted(tid) else 0.0])

    return CountResult(
        count=counter.count,
        events=list(counter.events),
        rows=np.array(rows, dtype=np.float32).reshape(-1, 9),
        n_tracks=len(lengths),
        short_tracks=sum(1 for n in lengths.values() if n < 3),
        n_frames=len(tracked_frames),
        fps=fps,
    )


def count_clip(cache: dict, params: CameraParams, per_frame=None) -> CountResult:
    """Track + count over cached detections (see pipeline.detect_clip)."""
    tracked = track_clip(cache, params, per_frame)
    return count_tracked(tracked, params, int(cache["width"]), int(cache["height"]),
                         float(cache["fps"]))


def count_variants(cache: dict, variants: list[CameraParams]) -> list[int]:
    """Counts for many parameter sets, tracking once per distinct tracker key."""
    per_frame = split_by_frame(cache)
    w, h, fps = int(cache["width"]), int(cache["height"]), float(cache["fps"])
    tracked_by_key: dict[tuple, list] = {}
    counts = []
    for p in variants:
        key = track_key(p)
        if key not in tracked_by_key:
            tracked_by_key[key] = track_clip(cache, p, per_frame)
        counts.append(count_tracked(tracked_by_key[key], p, w, h, fps).count)
    return counts
