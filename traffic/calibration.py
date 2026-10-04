"""Accuracy of the counter against admin-verified counts, and parameter search.

This is parameter calibration, not model training: it finds the zone /
threshold / tracker settings under which the existing detector counts best.
Fine-tuning the detector itself uses the frame labels (see labels.py).
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, odds, store
from .counting import count_variants
from .params import CameraParams, GridSpec
from .pipeline import load_cache

TAP_TOLERANCE_S = 2.0


def camera_stats(conn, cam_id: str) -> list[dict]:
    """(weekday, hour) -> n, mean, variance of cv_count over approved/used clips."""
    groups: dict[tuple[int, int], list[int]] = defaultdict(list)
    for c in store.list_clips(conn, cam_id):
        if c["status"] in ("approved", "used") and c["cv_count"] is not None:
            t = datetime.fromisoformat(c["recorded_at"]).astimezone(ZoneInfo(config.LOCAL_TZ))
            groups[(t.weekday(), t.hour)].append(int(c["cv_count"]))
    out = []
    for (wd, hr), counts in sorted(groups.items()):
        n, m, v = odds.summarize(counts)
        out.append({"camera_id": cam_id, "weekday": wd, "hour": hr, "n": n, "mean": m,
                    "variance": v})
    return out


def camera_buckets(conn, cam_id: str) -> tuple[tuple[int, ...], str]:
    stats = camera_stats(conn, cam_id)
    n, m, v = odds.pool((s["n"], s["mean"], s["variance"]) for s in stats)
    if n >= odds.MIN_SEGMENT_N:
        return odds.choose_buckets(odds.fit(m, v)), f"iš {n} klipų statistikos"
    return config.DEFAULT_BUCKET_LOWS, "numatytieji (dar mažai statistikos)"


def match_taps(taps: list[float], events: list[float], tol: float = TAP_TOLERANCE_S):
    """Greedy time matching of reviewer taps to count events.

    Unmatched taps = vehicles the counter missed; unmatched events = vehicles
    counted that the reviewer did not see (false or double counts).
    """
    taps, events = sorted(taps), sorted(events)
    used = [False] * len(events)
    missed = []
    for t in taps:
        best, best_d = None, tol
        for j, e in enumerate(events):
            # the counter fires a little after the vehicle enters; allow that lag
            d = abs(e - t)
            if not used[j] and d <= best_d:
                best, best_d = j, d
        if best is None:
            missed.append(t)
        else:
            used[best] = True
    extra = [e for j, e in enumerate(events) if not used[j]]
    return missed, extra


def _metrics(pairs: list[tuple[int, int]], lows) -> dict:
    n = len(pairs)
    if n == 0:
        return {"n": 0, "mae": None, "exact": None, "in_bucket": None, "under": 0,
                "over": 0, "bias": None}
    err = [cv - true for cv, true in pairs]
    return {
        "n": n,
        "mae": round(sum(abs(e) for e in err) / n, 3),
        "exact": round(sum(e == 0 for e in err) / n, 4),
        "in_bucket": round(sum(odds.bucket_of(cv, lows) == odds.bucket_of(t, lows)
                               for cv, t in pairs) / n, 4),
        "under": sum(-e for e in err if e < 0),     # vehicles missed in total
        "over": sum(e for e in err if e > 0),       # vehicles over-counted in total
        "bias": round(sum(err) / n, 3),
    }


def report(conn, cam_id: str) -> dict:
    reviews = store.latest_reviews(conn, cam_id)
    lows, lows_src = camera_buckets(conn, cam_id)
    pairs = [(r["cv_count"], r["true_count"]) for r in reviews]
    m = _metrics(pairs, lows)
    missed_n = extra_n = 0
    tapped = 0
    clips = []
    for r in reviews:
        row = {"clip_id": r["clip_id"], "cv": r["cv_count"], "true": r["true_count"],
               "err": r["cv_count"] - r["true_count"]}
        taps = json.loads(r["taps_json"]) if r["taps_json"] else None
        clip = store.get_clip(conn, r["clip_id"])
        if taps is not None and clip and clip["tracks_path"]:
            try:
                tracks = json.loads(open(clip["tracks_path"], encoding="utf-8").read())
                missed, extra = match_taps(taps, [e["t"] for e in tracks["events"]])
                row.update(missed_at=missed, extra_at=extra)
                missed_n += len(missed)
                extra_n += len(extra)
                tapped += 1
            except (OSError, ValueError, KeyError):
                pass
        clips.append(row)
    clips.sort(key=lambda c: -abs(c["err"]))
    ready = (m["n"] >= config.READY_MIN_REVIEWED
             and (m["in_bucket"] or 0) >= config.READY_MIN_IN_BUCKET)
    return {
        "camera_id": cam_id, **m,
        "buckets": odds.labels(lows), "buckets_source": lows_src,
        "taps": {"clips": tapped, "missed": missed_n, "extra": extra_n},
        "worst": clips[:15],
        "ready": ready,
        "ready_rule": f"≥{config.READY_MIN_REVIEWED} įvertintų ir "
                      f"≥{int(config.READY_MIN_IN_BUCKET * 100)} % teisingame intervale",
    }


def _reviewed(conn, cam_id: str, params: CameraParams) -> tuple[list[tuple[int, str]], int]:
    """[(true_count, dets_path)] for reviewed clips whose detection cache fits."""
    out, skipped = [], 0
    for r in store.latest_reviews(conn, cam_id):
        if load_cache(r["dets_path"], params) is None:
            skipped += 1
        else:
            out.append((r["true_count"], r["dets_path"]))
    return out, skipped


def _clip_counts(job: tuple[str, list[dict]]) -> list[int]:
    """Worker: one clip, every variant (top-level so it pickles to a process)."""
    import warnings

    warnings.filterwarnings("ignore", category=FutureWarning)
    dets_path, variant_dicts = job
    variants = [CameraParams.from_dict(d) for d in variant_dicts]
    cache = load_cache(dets_path, variants[0])
    return count_variants(cache, variants)


def _run(reviewed, variants: list[CameraParams], progress=None, workers: int | None = None):
    """counts[clip][variant], clips spread over CPU cores."""
    jobs = [(path, [v.to_dict() for v in variants]) for _, path in reviewed]
    workers = workers if workers is not None else min(len(jobs), os.cpu_count() or 1)
    if workers <= 1 or len(jobs) <= 1:
        results = []
        for i, job in enumerate(jobs, 1):
            results.append(_clip_counts(job))
            if progress:
                progress(i, len(jobs))
        return results
    results: list = [None] * len(jobs)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_clip_counts, job): i for i, job in enumerate(jobs)}
        for done, fut in enumerate(as_completed(futures), 1):
            results[futures[fut]] = fut.result()
            if progress:
                progress(done, len(jobs))
    return results


def evaluate(conn, cam_id: str, params: CameraParams) -> dict:
    reviewed, skipped = _reviewed(conn, cam_id, params)
    lows, _ = camera_buckets(conn, cam_id)
    counts = _run(reviewed, [params], workers=1)
    pairs = [(c[0], true) for c, (true, _) in zip(counts, reviewed)]
    return {**_metrics(pairs, lows), "skipped": skipped}


def grid_search(conn, cam_id: str, grid: GridSpec | None = None, base: CameraParams | None = None,
                progress=None, top: int = 10, workers: int | None = None) -> dict:
    """Re-count every reviewed clip under each parameter set; best first.

    The current parameters are always evaluated too, so "best" is compared with
    what the camera runs today.
    """
    base = base or store.camera_params(conn, cam_id)
    variants = (grid or GridSpec()).variants(base)
    if base not in variants:
        variants.insert(0, base)
    reviewed, skipped = _reviewed(conn, cam_id, base)
    if not reviewed:
        return {"results": [], "skipped": skipped, "tested": 0, "clips": 0,
                "note": "nėra įvertintų klipų su detekcijų talpykla"}
    lows, _ = camera_buckets(conn, cam_id)
    counts = _run(reviewed, variants, progress, workers)
    results = []
    for j, p in enumerate(variants):
        pairs = [(counts[i][j], true) for i, (true, _) in enumerate(reviewed)]
        results.append({"params": p.to_dict(), **_metrics(pairs, lows)})
    current = results[variants.index(base)]
    results.sort(key=lambda r: (-(r["in_bucket"] or 0), -(r["exact"] or 0), r["mae"] or 0))
    return {"results": results[:top], "current": current, "tested": len(variants),
            "clips": len(reviewed), "skipped": skipped}
