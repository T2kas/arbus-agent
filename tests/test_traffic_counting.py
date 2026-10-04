"""Counting rule, tracker integration, detector decoding, calibration helpers."""

import json

import pytest

np = pytest.importorskip("numpy")   # traffic worker deps (traffic/requirements.txt)

from traffic.calibration import match_taps
from traffic.counting import ZoneCounter
from traffic.params import CameraParams, GridSpec
from traffic.quality import judge

W, H = 1000, 1000
ZONE = ((0.0, 0.4), (1.0, 0.4), (1.0, 0.6), (0.0, 0.6))     # horizontal band
DOWN = CameraParams(zone=ZONE, direction=(0.0, 1.0), min_frames=3, min_travel=0.01)


def inside(y):
    return 400 <= y <= 600


def run(counter, tracks):
    """tracks: {frame: [(tid, x, y), ...]} -> events."""
    for f in range(max(tracks) + 1):
        here = tracks.get(f, [])
        counter.update(f, [t for t, _, _ in here], np.array([[x, y] for _, x, y in here]).reshape(-1, 2),
                       [inside(y) for _, _, y in here])
    return counter.events


def test_vehicle_driving_through_counts_once():
    tracks = {f: [(1, 500, 300 + 20 * f)] for f in range(25)}       # y 300 -> 780
    assert len(run(ZoneCounter(DOWN, W, H), tracks)) == 1


def test_wrong_direction_is_not_counted():
    tracks = {f: [(1, 500, 780 - 20 * f)] for f in range(25)}
    assert run(ZoneCounter(DOWN, W, H), tracks) == []


def test_any_direction_counts_both_ways():
    p = DOWN.with_(direction=None)
    tracks = {f: [(1, 300, 300 + 20 * f), (2, 700, 780 - 20 * f)] for f in range(25)}
    assert len(run(ZoneCounter(p, W, H), tracks)) == 2


def test_parked_vehicle_in_zone_is_not_counted():
    p = DOWN.with_(skip_initial=False, direction=None)
    tracks = {f: [(1, 500, 500 + (f % 2))] for f in range(5, 40)}    # jitter only
    assert run(ZoneCounter(p, W, H), tracks) == []


def test_flicker_shorter_than_min_frames_is_ignored():
    tracks = {f: [] for f in range(10)}
    tracks[4] = [(7, 500, 500)]
    tracks[5] = [(7, 500, 520)]
    assert run(ZoneCounter(DOWN, W, H), tracks) == []


def test_id_switch_after_count_is_not_a_double_count():
    tracks = {f: [(1, 500, 300 + 20 * f)] for f in range(12)}        # counted by f~8
    # tracker loses it for 3 frames, then it reappears under a new id where it
    # was predicted to be
    tracks.update({f: [(2, 500, 300 + 20 * f)] for f in range(15, 25)})
    ev = run(ZoneCounter(DOWN, W, H), tracks)
    assert len(ev) == 1 and ev[0][1] == 1


def test_new_vehicle_elsewhere_is_still_counted_after_a_switch_window():
    tracks = {f: [(1, 200, 300 + 20 * f)] for f in range(12)}
    tracks.update({f: [(2, 800, 300 + 20 * (f - 14))] for f in range(14, 40)})   # other lane
    assert len(run(ZoneCounter(DOWN, W, H), tracks)) == 2


def test_vehicle_already_in_zone_at_start_is_skipped_by_default():
    tracks = {f: [(1, 500, 420 + 20 * f)] for f in range(15)}         # in zone at f=0
    assert run(ZoneCounter(DOWN, W, H), tracks) == []
    assert len(run(ZoneCounter(DOWN.with_(skip_initial=False), W, H), tracks)) == 1


def test_params_roundtrip():
    p = DOWN.with_(classes=("car",), blur_plates=True)
    assert CameraParams.from_dict(json.loads(json.dumps(p.to_dict()))) == p


def test_grid_variants_and_guard():
    variants = GridSpec({"min_frames": [2, 3], "conf": [0.3, 0.4]}).variants(DOWN)
    assert len(variants) == 4
    assert {v.track_activation for v in variants} == {0.3, 0.4}   # follows conf
    with pytest.raises(ValueError):
        GridSpec({"model": ["yolox_m"]}).variants(DOWN)


def test_match_taps_finds_misses_and_extras():
    missed, extra = match_taps([1.0, 5.0, 9.0], [1.6, 5.2, 14.0])
    assert missed == [9.0] and extra == [14.0]


def test_quality_judge_reasons():
    ok = {"frames": 300, "brightness": 120, "sharpness": 200, "motion": 3, "n_tracks": 10,
          "fragmentation": 0.1}
    assert judge(ok) == []
    assert any("tamsu" in r for r in judge({**ok, "brightness": 20}))
    assert any("užstrigęs" in r for r in judge({**ok, "motion": 0.0}))


# ── with the CV stack installed ────────────────────────────────────────────

def _synthetic_cache(n_frames=40, cars=((300, 0), (700, 6))):
    """Boxes driving down the frame; (x, start_frame) per car."""
    frames, xyxy = [], []
    for x, start in cars:
        for f in range(start, n_frames):
            y = 150 + 25 * (f - start)
            if y > 950:
                break
            frames.append(f)
            xyxy.append([x - 40, y - 60, x + 40, y])
    k = len(frames)
    return {"frame": np.array(frames, np.int32), "xyxy": np.array(xyxy, np.float32),
            "conf": np.full(k, 0.9, np.float32), "cls": np.full(k, 2, int),
            "n_frames": n_frames, "fps": 12.0, "width": W, "height": H}


def test_count_clip_with_bytetrack():
    pytest.importorskip("supervision")
    from traffic.counting import count_clip, count_variants

    cache = _synthetic_cache()
    r = count_clip(cache, DOWN)
    assert r.count == 2 and r.n_tracks == 2
    # rows carry the counted flag the renderer colours by
    assert set(r.rows[:, 8].tolist()) == {0.0, 1.0}
    # the "up" direction counts nothing; variants share one tracking run
    assert count_variants(cache, [DOWN, DOWN.with_(direction=(0.0, -1.0))]) == [2, 0]


def test_low_confidence_detections_are_filtered():
    pytest.importorskip("supervision")
    from traffic.counting import count_clip

    cache = _synthetic_cache()
    cache["conf"][:] = 0.2
    assert count_clip(cache, DOWN).count == 0


def test_yolox_decode_and_class_agnostic_nms():
    pytest.importorskip("cv2")
    from traffic.detector import decode, postprocess

    size = (64, 64)
    n = sum((64 // s) ** 2 for s in (8, 16, 32))
    raw = np.zeros((n, 85), np.float32)
    raw[:, 4] = -10
    # anchor 9 on stride 8 = grid (1, 1); centre (1+0.5)*8 = 12
    raw[9, :4] = [0.5, 0.5, np.log(2.0), np.log(2.0)]     # w = h = 16
    raw[9, 4] = 1.0
    raw[9, 5 + 2] = 0.9        # car
    raw[9, 5 + 7] = 0.8        # truck on the same box -> must not survive NMS
    pred = decode(raw, size)
    xyxy, conf, cls = postprocess(pred, ratio=0.5, class_ids=[1, 2, 3, 5, 7], conf=0.3, nms=0.45)
    assert len(xyxy) == 1 and cls[0] == 2
    assert xyxy[0] == pytest.approx([8, 8, 40, 40])         # (12 +- 8) / 0.5


def test_store_and_coco_export(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    from traffic import config, labels, store

    monkeypatch.setattr(config, "CLIPS_DIR", tmp_path / "clips")
    conn = store.connect(tmp_path / "t.db")
    store.add_camera(conn, "cam", "Kamera")
    ids = []
    for _ in range(2):
        cid = store.add_clip(conn, "cam", tmp_path / "raw.mp4", "2026-10-03T08:00:00+03:00")
        store.update_clip(conn, cid, params_json=DOWN.to_dict())
        clip = store.get_clip(conn, cid)
        d = labels.frames_dir(clip)
        d.mkdir(parents=True)
        cv2.imwrite(str(d / "00000.jpg"), np.zeros((40, 60, 3), np.uint8))
        (d / "index.json").write_text("[0]")
        store.save_labels(conn, cid, 0, [{"cls": "car", "box": [1, 2, 31, 22]},
                                         {"cls": "truck", "box": [5, 5, 6, 6]}], 60, 40)
        ids.append(cid)
    summary = labels.export_coco(conn, tmp_path / "out", "cam")
    assert summary["train2017"]["images"] + summary["val2017"]["images"] == 2
    doc = json.loads((tmp_path / "out/annotations/instances_train2017.json").read_text())
    assert len(doc["categories"]) == 80                      # keeps the COCO head
    ann = doc["annotations"][0]
    assert ann["category_id"] == 3 and ann["bbox"] == [1, 2, 30, 20]   # tiny box dropped
    assert (tmp_path / "out/arbus_yolox_exp.py").exists()


# ── line mode ──────────────────────────────────────────────────────────────

LINE = CameraParams(line=((0.1, 0.5), (0.9, 0.5)), min_frames=3)    # horizontal at y=500


def test_line_counts_both_directions_without_choosing_a_side():
    tracks = {f: [(1, 300, 300 + 20 * f), (2, 700, 700 - 20 * f)] for f in range(25)}
    assert len(run(ZoneCounter(LINE, W, H), tracks)) == 2


def test_line_with_direction_counts_one_way_only():
    p = LINE.with_(direction=(0.0, 1.0))
    tracks = {f: [(1, 300, 300 + 20 * f), (2, 700, 700 - 20 * f)] for f in range(25)}
    ev = run(ZoneCounter(p, W, H), tracks)
    assert [tid for _, tid in ev] == [1]


def test_line_wobble_counts_once():
    ys = [450, 470, 490, 510, 520, 530, 495, 480, 515, 530, 545, 560]   # back and forth
    tracks = {f: [(1, 500, y)] for f, y in enumerate(ys)}
    assert len(run(ZoneCounter(LINE, W, H), tracks)) == 1


def test_line_crossing_beyond_the_drawn_segment_is_ignored():
    tracks = {f: [(1, 970, 300 + 20 * f)] for f in range(25)}      # x past the line's end
    assert run(ZoneCounter(LINE, W, H), tracks) == []


def test_line_crossing_during_an_id_switch_still_counts_once():
    tracks = {f: [(1, 500, 380 + 20 * f)] for f in range(5)}        # y 380..460
    tracks.update({f: [(2, 500, 380 + 20 * f)] for f in range(8, 20)})   # reappears below
    assert len(run(ZoneCounter(LINE, W, H), tracks)) == 1


def test_line_params_roundtrip():
    assert CameraParams.from_dict(json.loads(json.dumps(LINE.to_dict()))) == LINE


def test_count_clip_line_mode_with_bytetrack():
    pytest.importorskip("supervision")
    from traffic.counting import count_clip

    assert count_clip(_synthetic_cache(), LINE).count == 2
