"""Local admin tool: calibrate cameras, verify counts, mark vehicles in frames.

    python -m traffic admin        ->  http://127.0.0.1:8765

Binds to localhost only: it has no login, it is a workbench on the admin's
own machine. Heavy work (detection, rendering, parameter search) runs in one
background worker thread so the page stays responsive.
"""

from __future__ import annotations

import json
import queue
import threading
import traceback
import uuid
import warnings
from datetime import datetime
from pathlib import Path

from . import config, labels, store
from .params import CameraParams, GridSpec

warnings.filterwarnings("ignore", category=FutureWarning)

STATIC = Path(__file__).parent / "static"

_local = threading.local()


def db():
    if not hasattr(_local, "conn"):
        _local.conn = store.connect()
    return _local.conn


# ── background jobs ────────────────────────────────────────────────────────

class Jobs:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, kind: str, fn, *args) -> str:
        jid = uuid.uuid4().hex[:8]
        with self.lock:
            self.jobs[jid] = {"id": jid, "kind": kind, "status": "queued", "progress": None,
                              "result": None, "error": None, "created": store.now()}
        self.q.put((jid, fn, args))
        return jid

    def progress(self, jid: str, text) -> None:
        with self.lock:
            self.jobs[jid]["progress"] = text

    def _worker(self):
        while True:
            jid, fn, args = self.q.get()
            self.jobs[jid]["status"] = "running"
            try:
                self.jobs[jid]["result"] = fn(jid, *args)
                self.jobs[jid]["status"] = "done"
            except Exception as e:
                traceback.print_exc()
                self.jobs[jid]["status"] = "error"
                self.jobs[jid]["error"] = f"{type(e).__name__}: {e}"

    def get(self, jid: str) -> dict | None:
        with self.lock:
            j = self.jobs.get(jid)
            return dict(j) if j else None

    def active(self) -> list[dict]:
        with self.lock:
            return [dict(j) for j in self.jobs.values() if j["status"] in ("queued", "running")]


JOBS = Jobs()


def _job_process(jid: str, clip_ids: list[str]) -> dict:
    from .pipeline import process_clip

    done, failed = [], []
    for i, cid in enumerate(clip_ids, 1):
        JOBS.progress(jid, f"{i}/{len(clip_ids)} klipas")
        try:
            r = process_clip(db(), cid, progress=lambda f: JOBS.progress(
                jid, f"{i}/{len(clip_ids)} klipas, kadras {f}"))
            done.append({"clip_id": cid, "count": r["count"], "status": r["status"]})
        except Exception as e:
            failed.append({"clip_id": cid, "error": f"{type(e).__name__}: {e}"})
    return {"done": done, "failed": failed}


def _job_search(jid: str, cam_id: str, axes: dict | None) -> dict:
    from .calibration import grid_search

    grid = GridSpec(axes) if axes else GridSpec()
    return grid_search(db(), cam_id, grid,
                       progress=lambda i, n: JOBS.progress(jid, f"{i}/{n} klipų"))


def _job_export(jid: str, cam_id: str | None) -> dict:
    out = config.ROOT / "export" / datetime.now().strftime("%Y%m%dT%H%M%S")
    summary = labels.export_coco(db(), out, cam_id)
    return {"path": str(out.resolve()), **summary}


# ── app ────────────────────────────────────────────────────────────────────

def create_app():
    from flask import Flask, abort, jsonify, request, send_file

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3

    def clip_or_404(cid):
        c = store.get_clip(db(), cid)
        if c is None:
            abort(404)
        return c

    def latest_review(cid):
        return db().execute("select * from calibration where clip_id = ? order by id desc limit 1",
                            (cid,)).fetchone()

    @app.get("/")
    def index():
        return send_file(STATIC / "admin.html")

    @app.get("/api/cameras")
    def cameras():
        out = []
        for c in store.list_cameras(db()):
            n = db().execute("select status, count(*) n from clips where camera_id = ?"
                             " group by status", (c["id"],)).fetchall()
            reviewed = len(store.latest_reviews(db(), c["id"]))
            out.append({**{k: c[k] for k in c.keys() if k != "params_json"},
                        "params": json.loads(c["params_json"]),
                        "clips": {r["status"]: r["n"] for r in n}, "reviewed": reviewed})
        return jsonify(out)

    @app.post("/api/cameras")
    def camera_add():
        d = request.get_json(force=True)
        cid = (d.get("id") or "").strip()
        if not cid or store.get_camera(db(), cid):
            return jsonify(error="trūksta id arba toks jau yra"), 400
        store.add_camera(db(), cid, d.get("name") or cid, d.get("city", ""),
                         d.get("source_url", ""), d.get("permission_status", "pending"))
        return jsonify(ok=True)

    @app.put("/api/cameras/<cam>")
    def camera_update(cam):
        d = request.get_json(force=True)
        if "enabled" in d and d["enabled"]:
            from .calibration import report

            if not report(db(), cam)["ready"] and not d.get("force"):
                return jsonify(error="kamera dar nepasiekė tikslumo slenksčio"), 400
        store.update_camera(db(), cam, **{k: v for k, v in d.items() if k != "force"})
        return jsonify(ok=True)

    @app.put("/api/cameras/<cam>/params")
    def camera_params(cam):
        cur = store.camera_params(db(), cam).to_dict()
        new = CameraParams.from_dict({**cur, **request.get_json(force=True)})
        store.set_camera_params(db(), cam, new)
        return jsonify(new.to_dict())

    @app.get("/api/cameras/<cam>/clips")
    def camera_clips(cam):
        rows = []
        for c in store.list_clips(db(), cam):
            if c["status"] == "deleted":
                continue
            r = latest_review(c["id"])
            q = json.loads(c["quality_json"]) if c["quality_json"] else {}
            n_labels = db().execute("select count(*) from labels where clip_id = ?",
                                    (c["id"],)).fetchone()[0]
            rows.append({"id": c["id"], "recorded_at": c["recorded_at"], "status": c["status"],
                         "cv_count": c["cv_count"], "error": c["error"],
                         "true_count": r["true_count"] if r else None,
                         "reviewed_cv": r["cv_count"] if r else None,
                         "reasons": q.get("reasons", []), "labeled_frames": n_labels})
        return jsonify(rows)

    @app.post("/api/cameras/<cam>/upload")
    def upload(cam):
        from .pipeline import ingest

        tmp = config.ROOT / "incoming" / "upload"
        tmp.mkdir(parents=True, exist_ok=True)
        ids = []
        for f in request.files.getlist("files"):
            name = Path(f.filename or "clip.mp4").name
            dest = tmp / f"{uuid.uuid4().hex[:6]}_{name}"
            f.save(dest)
            # keep the original timestamp-bearing name for recorded_at parsing
            from .pipeline import recorded_at_from_name

            ids.append(ingest(db(), cam, dest, recorded_at_from_name(name), move=True))
        jid = JOBS.submit("process", _job_process, ids) if ids else None
        return jsonify(clip_ids=ids, job=jid)

    @app.post("/api/cameras/<cam>/reprocess")
    def reprocess(cam):
        ids = [c["id"] for c in store.list_clips(db(), cam) if c["status"] not in ("deleted",)]
        return jsonify(job=JOBS.submit("process", _job_process, ids))

    @app.get("/api/cameras/<cam>/report")
    def report(cam):
        from .calibration import report as rep

        return jsonify(rep(db(), cam))

    @app.post("/api/cameras/<cam>/search")
    def search(cam):
        axes = (request.get_json(silent=True) or {}).get("axes")
        return jsonify(job=JOBS.submit("search", _job_search, cam, axes))

    @app.post("/api/export")
    def export():
        cam = (request.get_json(silent=True) or {}).get("camera")
        return jsonify(job=JOBS.submit("export", _job_export, cam))

    @app.get("/api/jobs/<jid>")
    def job(jid):
        j = JOBS.get(jid)
        return jsonify(j) if j else (jsonify(error="nėra"), 404)

    @app.get("/api/jobs")
    def jobs():
        return jsonify(JOBS.active())

    @app.get("/api/clips/<cid>")
    def clip(cid):
        c = clip_or_404(cid)
        tracks = {}
        if c["tracks_path"] and Path(c["tracks_path"]).exists():
            t = json.loads(Path(c["tracks_path"]).read_text(encoding="utf-8"))
            tracks = {k: t[k] for k in ("fps", "width", "height", "n_frames", "count", "events")}
        r = latest_review(cid)
        return jsonify({**{k: c[k] for k in ("id", "camera_id", "recorded_at", "status",
                                             "cv_count", "error")},
                        "quality": json.loads(c["quality_json"]) if c["quality_json"] else {},
                        "params": json.loads(c["params_json"]) if c["params_json"] else None,
                        "tracks": tracks,
                        "review": dict(r) if r else None,
                        "has_annotated": bool(c["annotated_path"]
                                              and Path(c["annotated_path"]).exists())})

    @app.post("/api/clips/<cid>/process")
    def clip_process(cid):
        clip_or_404(cid)
        return jsonify(job=JOBS.submit("process", _job_process, [cid]))

    @app.post("/api/clips/<cid>/review")
    def clip_review(cid):
        c = clip_or_404(cid)
        d = request.get_json(force=True)
        if c["cv_count"] is None:
            return jsonify(error="klipas dar nesuskaičiuotas"), 400
        true = int(d["true_count"])
        if true < 0:
            return jsonify(error="neigiamas skaičius"), 400
        params = CameraParams.from_dict(json.loads(c["params_json"] or "{}"))
        taps = d.get("taps")
        store.add_review(db(), cid, int(c["cv_count"]), true, params,
                         [round(float(t), 2) for t in taps] if taps is not None else None)
        missed = extra = None
        if taps is not None and c["tracks_path"]:
            from .calibration import match_taps

            t = json.loads(Path(c["tracks_path"]).read_text(encoding="utf-8"))
            missed, extra = match_taps(taps, [e["t"] for e in t["events"]])
        return jsonify(ok=True, missed_at=missed, extra_at=extra)

    @app.get("/api/clips/<cid>/video/<kind>")
    def clip_video(cid, kind):
        c = clip_or_404(cid)
        path = c["annotated_path"] if kind == "annotated" else c["raw_path"]
        if not path or not Path(path).exists():
            abort(404)
        return send_file(Path(path).resolve(), mimetype="video/mp4", conditional=True)

    @app.get("/api/clips/<cid>/frames")
    def clip_frames(cid):
        c = clip_or_404(cid)
        if not c["params_json"]:
            return jsonify(error="pirmiausia suskaičiuok klipą"), 400
        idx = labels.ensure_frames(c)
        done = [r["frame_index"] for r in db().execute(
            "select frame_index from labels where clip_id = ?", (cid,))]
        return jsonify(frames=idx, labeled=done)

    @app.get("/api/clips/<cid>/frame/<int:n>.jpg")
    def clip_frame(cid, n):
        c = clip_or_404(cid)
        p = labels.frame_path(c, n)
        if not p.exists():
            labels.ensure_frames(c)
        if not p.exists():
            abort(404)
        return send_file(p.resolve(), mimetype="image/jpeg")

    @app.get("/api/clips/<cid>/labels/<int:n>")
    def clip_labels(cid, n):
        c = clip_or_404(cid)
        r = store.get_labels(db(), cid, n)
        if r:
            return jsonify(source="labels", boxes=json.loads(r["boxes_json"]))
        return jsonify(source="detector", boxes=labels.predictions(c, n))

    @app.put("/api/clips/<cid>/labels/<int:n>")
    def clip_labels_save(cid, n):
        clip_or_404(cid)
        d = request.get_json(force=True)
        boxes = [{"cls": b.get("cls", "car") if b.get("cls") in labels.LABEL_CLASSES else "car",
                  "box": [round(float(v), 1) for v in b["box"]]} for b in d.get("boxes", [])]
        store.save_labels(db(), cid, n, boxes, int(d["width"]), int(d["height"]))
        return jsonify(ok=True, n=len(boxes))

    return app


def serve(host: str = config.ADMIN_HOST, port: int = config.ADMIN_PORT) -> None:
    app = create_app()
    print(f"Arbus eismo kalibravimas: http://{host}:{port}")
    app.run(host=host, port=port, threaded=True, debug=False)
