# Eismo raundas — vehicle counter

The counting bot behind the traffic round: it records ~30 s camera clips,
counts the vehicles that **enter a blue zone in a set direction**, burns the
overlay into an MP4 (zone, a box per vehicle that turns green when counted, a
big "Pravažiavo: N"), and gives you a local tool to check the counts, tune the
parameters and mark vehicles in frames to fine-tune the detector.

```
record (ffmpeg, 30 s, ≤960 px, no audio)
  → detect   YOLOX (ONNX Runtime, CPU)        ~0.1 s/frame, cached per clip
  → track    ByteTrack (supervision)
  → count    PolygonZone + direction + N frames + ID-switch merge
  → quality  dark / fog / frozen / fragmented tracking → rejected
  → render   annotated.mp4 (H.264) + tracks.json
```

Licences: YOLOX Apache-2.0, supervision MIT, ONNX Runtime MIT. Ultralytics
(AGPL) is deliberately not used.

The number on screen and `cv_count` come from the **same** count events, so
they cannot disagree.

## Install

```sh
pip install -r traffic/requirements.txt
python -m traffic fetch-model yolox_s       # 35 MB → models/yolox_s.onnx
```

No system ffmpeg is needed (imageio-ffmpeg bundles one). Everything is stored
under `data/traffic/` (SQLite `traffic.db` + per-clip folders), git-ignored.

## Test run with 10 clips

1. **Camera**
   `python -m traffic camera add vilnius-test --name "Testas" --city Vilnius`
   (for a stream you are allowed to record, add
   `--source <HLS/RTSP URL> --permission granted`)
2. **Clips** — any 10 traffic videos of ~30 s (mp4). From files:
   `python -m traffic ingest vilnius-test "D:/klipai/*.mp4" --process`.
   From the stream: `python -m traffic record vilnius-test --minutes 5`
   (records, splits into 30 s clips, ingests and counts them).
3. **Admin tool** — `python -m traffic admin` → <http://127.0.0.1:8765>
4. **Zone and parameters** tab: "Brėžti liniją" and two clicks across the road —
   a vehicle counts when it crosses the line, **from either side**, no direction
   needed. (Or "Brėžti zoną" for a polygon. "Kryptis" only if you want to count
   one direction of traffic.)
   "Išsaugoti ir perskaičiuoti" recounts every clip from the detection cache
   (fast).
5. **Peržiūra** (review) tab, for each clip: play it and press **Space** every
   time a vehicle enters the zone in the counted direction (**Backspace** undoes
   the last press). Your presses become the true count. "Teisinga" if the system
   got it right. Tick "be perdangos" to count on the raw video without seeing
   the system's boxes. After saving, it tells you at which seconds the system
   **missed** a vehicle and where it counted **one too many**.
6. **Ataskaita** (report): mean absolute error, % exact, % in the right interval,
   missed vs. over-counted totals, worst clips (click one to open it).
7. **Parametrų paieška** (parameter search): recounts every reviewed clip with each
   combination (e.g. conf 0.25/0.35/0.45 × N 2/3/5 × …) and lists the best;
   "Pritaikyti" applies one. It reuses cached detections, and clips run in
   parallel across CPU cores — about 4 min for 81 combinations × 50 clips.
8. **Žymėjimas** (marking) tab: mark vehicles on frames for detector fine-tuning (below).

`python -m pytest tests/test_traffic_*.py` runs the unit tests (no model or
video needed).

### What counts (the rule reviewers follow too)

A vehicle counts **once**: when it **crosses the line** (either side), or when it **enters** the zone moving in the set
direction and stays in it for N frames. Not counted: vehicles already inside
the zone on the first frame, parked/standing vehicles, vehicles going the
other way. Same rule in the code (`counting.py`) and in your Space presses, so
the accuracy numbers mean something.

### Which knob fixes what

| Symptom (from the report / review) | Try |
|---|---|
| over-counts, extras at random moments | raise `conf`, raise N (`min_frames`) |
| over-counts, the same car twice | raise `merge_gap` / `merge_dist`, raise `lost_buffer` |
| misses small / far vehicles | lower `conf`, bigger model (`yolox_m`), move the zone closer |
| misses in dense traffic | lower `match_thresh`, `anchor: center` |
| counts parked cars / wrong lane | set the direction, tighten the zone to the lane |

The search finds the combination; the table tells you what to put in it.

## Teaching the detector (marking vehicles in frames)

Parameter tuning cannot fix a detector that does not see a vehicle (night,
odd angle, far away). For that, mark frames:

1. **Žymėjimas** tab → choose a clip. One frame per second is offered; each
   starts pre-filled with the detector's guesses (dashed). Fix them: drag to add
   a box, click a box to select it and change its class (keys 1-5) or delete it
   (**Del**). Then **S** = save, next frame. **Mark every visible vehicle**,
   far ones too: a missing box teaches the model "this is not a car".
2. Prioritize frames from clips where the report shows misses.
3. With a few hundred frames: "Eksportuoti COCO" (or
   `python -m traffic export-labels out/arbus-coco`). This writes a COCO
   dataset (split by clip, so near-identical frames never leak into
   validation) plus `arbus_yolox_exp.py`.
4. Fine-tune (needs a GPU machine or Colab; torch is not needed for anything else):
   ```sh
   git clone https://github.com/Megvii-BaseDetection/YOLOX && cd YOLOX && pip install -v -e .
   wget https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.pth
   python tools/train.py -f <out>/arbus_yolox_exp.py -c yolox_s.pth -d 1 -b 8 --fp16 -o
   python tools/export_onnx.py -f <out>/arbus_yolox_exp.py -c YOLOX_outputs/arbus_yolox_exp/best_ckpt.pth --output-name yolox_s_arbus.onnx
   ```
5. Copy `yolox_s_arbus.onnx` to `models/` and set the camera's model to it:
   `python -m traffic camera set vilnius-test 'model="models/yolox_s_arbus.onnx"'`,
   then "Perskaičiuoti visus" (this re-runs detection, so it is slower) and
   compare the report.

The export keeps all 80 COCO classes so the pretrained head is fine-tuned
rather than replaced. With a small dataset that is far more reliable.

## Going live per camera

`python -m traffic camera enable <id>` (or the button in the report) refuses
until the camera has **≥ 50 reviewed clips with ≥ 95 % in the right
interval** (`config.READY_*`). Intervals come from the camera's own statistics
once it has 30+ clips, defaults `0–4 / 5–8 / 9–12 / 13+` before that.

## Recording, privacy, retention

* Only cameras with `permission_status = granted` are recorded.
* Recording uses `-an`, a ≤960 px width and 15 fps. Plates are rarely
  legible at that size; where they are, enable `blur_plates` (blurs the lower
  part of every vehicle box in the rendered video).
* `python -m traffic cleanup` removes media past retention: used 14 d, others
  30 d. Clips you reviewed or labeled are **kept**, because they are the
  calibration and training set. They stay local and never reach players;
  delete them by hand if that is not acceptable. Statistics (`cv_count` per
  clip) are kept indefinitely.

## Odds

`traffic/odds.py` builds the opening book from statistics only, never from the
clip that will play:

* Poisson, or negative binomial when var > 1.3 × mean.
* Fallback for thin segments: hour → ±1 h → ±2 h → all days ±1 h → whole camera.
* Quantile intervals at ~15–40 % each, 2 % floor.
* LMSR liquidity `b` sized so a typical stake moves a price ≤ 5 p.p.

The Arbus engine stores a market as option probabilities + `liquidity`, so
opening at p_i with that `b` is the same as q_i = b·ln p_i.

Try it with `python -m traffic odds <camera> --weekday 0 --hour 8`.

## Not built yet

Supabase tables / RLS, the round engine (open → lock → CSPRNG clip pick at lock
→ play → resolve / void) and the player screens. The local store mirrors the
planned `traffic_cameras` / `traffic_clips` / `traffic_calibration` columns, so
syncing is a straight copy once those exist.
