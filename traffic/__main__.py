"""CLI: python -m traffic <command>. See traffic/README.md."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, store


def _conn():
    return store.connect()


def cmd_fetch_model(a):
    from .detector import fetch_model

    print(fetch_model(a.name))


def cmd_camera_add(a):
    from .params import CameraParams

    conn = _conn()
    store.add_camera(conn, a.id, a.name, a.city, a.source or "", a.permission,
                     CameraParams(model=a.model))
    print(f"kamera {a.id} pridėta")


def cmd_camera_list(a):
    for c in store.list_cameras(_conn()):
        print(f"{c['id']:24} {c['name']:30} {c['city'] or '':12} "
              f"leidimas={c['permission_status']} įjungta={c['enabled']}")


def cmd_camera_show(a):
    conn = _conn()
    c = store.get_camera(conn, a.id)
    if c is None:
        sys.exit(f"nėra kameros {a.id}")
    print(json.dumps({**dict(c), "params_json": json.loads(c["params_json"])},
                     ensure_ascii=False, indent=2))


def cmd_camera_set(a):
    conn = _conn()
    p = store.camera_params(conn, a.id)
    changes = {}
    for kv in a.values:
        k, _, v = kv.partition("=")
        changes[k] = json.loads(v)
    from .params import CameraParams

    store.set_camera_params(conn, a.id, CameraParams.from_dict({**p.to_dict(), **changes}))
    print(json.dumps(store.camera_params(conn, a.id).to_dict(), indent=2))


def cmd_camera_enable(a):
    from .calibration import report

    conn = _conn()
    r = report(conn, a.id)
    if not r["ready"] and not a.force:
        sys.exit(f"kamera dar neparuošta: {r['n']} įvertinta, teisingame intervale "
                 f"{r['in_bucket']} (reikia {r['ready_rule']}). --force, jei tikrai.")
    store.update_camera(conn, a.id, enabled=1)
    print(f"kamera {a.id} įjungta į žaidimą")


def cmd_ingest(a):
    from .pipeline import ingest

    conn = _conn()
    files = []
    for pattern in a.files:
        p = Path(pattern)
        files += sorted(p.parent.glob(p.name)) if any(ch in p.name for ch in "*?[") else [p]
    ids = [ingest(conn, a.camera, f) for f in files]
    print(f"įkelta {len(ids)} klipų")
    if a.process:
        _process(conn, ids)


def _process(conn, ids):
    from .pipeline import process_clip

    for i, cid in enumerate(ids, 1):
        try:
            r = process_clip(conn, cid)
            q = r["quality"]
            extra = f" — ATMESTA: {'; '.join(q['reasons'])}" if q["reasons"] else ""
            print(f"[{i}/{len(ids)}] {cid}: {r['count']} pravažiavo{extra}")
        except Exception as e:      # one bad file must not stop the batch
            print(f"[{i}/{len(ids)}] {cid}: KLAIDA {type(e).__name__}: {e}")


def cmd_process(a):
    conn = _conn()
    if a.clip:
        ids = a.clip
    else:
        status = None if a.all else "processing"
        ids = [c["id"] for c in store.list_clips(conn, a.camera, status)
               if c["status"] not in ("deleted",)]
    _process(conn, ids)


def cmd_record(a):
    from .record import record

    conn = _conn()
    ids = record(conn, a.camera, a.minutes)
    print(f"įrašyta {len(ids)} klipų")
    if not a.no_process:
        _process(conn, ids)


def cmd_report(a):
    from .calibration import report

    r = report(_conn(), a.camera)
    print(json.dumps(r, ensure_ascii=False, indent=2))


def cmd_search(a):
    from .calibration import grid_search
    from .params import CameraParams, GridSpec

    conn = _conn()
    grid = GridSpec(json.loads(a.grid)) if a.grid else GridSpec()
    res = grid_search(conn, a.camera, grid,
                      progress=lambda i, n: print(f"\r{i}/{n}", end="", file=sys.stderr))
    print(file=sys.stderr)
    print(json.dumps({k: v for k, v in res.items() if k != "results"}, ensure_ascii=False))
    for r in res["results"]:
        print(f"intervale={r['in_bucket']}  tikslus={r['exact']}  MAE={r['mae']}  "
              f"conf={r['params']['conf']} N={r['params']['min_frames']} "
              f"lost={r['params']['lost_buffer']} match={r['params']['match_thresh']}")
    if a.apply and res["results"]:
        store.set_camera_params(conn, a.camera, CameraParams.from_dict(res["results"][0]["params"]))
        print("geriausi parametrai pritaikyti; perskaičiuok klipus: "
              f"python -m traffic process --camera {a.camera} --all")


def cmd_export_labels(a):
    from .labels import export_coco

    print(json.dumps(export_coco(_conn(), a.out, a.camera), indent=2))
    print(f"YOLOX exp: {Path(a.out) / 'arbus_yolox_exp.py'}")


def cmd_odds(a):
    from . import odds
    from .calibration import camera_stats

    conn = _conn()
    stats = camera_stats(conn, a.camera)
    n, m, v, level = odds.segment(stats, a.camera, a.weekday, a.hour)
    if n == 0:
        sys.exit("nėra statistikos (reikia patvirtintų klipų)")
    book = odds.opening_book(odds.fit(m, v), a.stake)
    print(json.dumps({"segment": level, "n": n, **book}, ensure_ascii=False, indent=2))


def cmd_cleanup(a):
    from .record import cleanup

    ids = cleanup(_conn(), a.dry_run)
    print(f"{'būtų ištrinta' if a.dry_run else 'ištrinta'}: {len(ids)}")


def cmd_admin(a):
    from .admin import serve

    serve(a.host, a.port)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m traffic")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fetch-model", help="atsisiųsti YOLOX ONNX modelį")
    s.add_argument("name", nargs="?", default="yolox_s", choices=config.YOLOX_MODELS)
    s.set_defaults(fn=cmd_fetch_model)

    cam = sub.add_parser("camera", help="kameros").add_subparsers(dest="sub", required=True)
    s = cam.add_parser("add")
    s.add_argument("id")
    s.add_argument("--name", required=True)
    s.add_argument("--city", default="")
    s.add_argument("--source", help="HLS/RTSP adresas")
    s.add_argument("--permission", default="pending", choices=["pending", "granted", "denied"])
    s.add_argument("--model", default="yolox_s")
    s.set_defaults(fn=cmd_camera_add)
    s = cam.add_parser("list")
    s.set_defaults(fn=cmd_camera_list)
    s = cam.add_parser("show")
    s.add_argument("id")
    s.set_defaults(fn=cmd_camera_show)
    s = cam.add_parser("set", help='pvz.: conf=0.4 min_frames=4 direction="[0,1]"')
    s.add_argument("id")
    s.add_argument("values", nargs="+")
    s.set_defaults(fn=cmd_camera_set)
    s = cam.add_parser("enable", help="įjungti į žaidimą (tik pasiekus tikslumą)")
    s.add_argument("id")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_camera_enable)

    s = sub.add_parser("ingest", help="įkelti vaizdo failus kaip klipus")
    s.add_argument("camera")
    s.add_argument("files", nargs="+")
    s.add_argument("--process", action="store_true", help="iškart ir suskaičiuoti")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("process", help="suskaičiuoti / perskaičiuoti klipus")
    s.add_argument("--camera")
    s.add_argument("--clip", nargs="*")
    s.add_argument("--all", action="store_true", help="ir jau apdorotus (pvz. po parametrų keitimo)")
    s.set_defaults(fn=cmd_process)

    s = sub.add_parser("record", help="įrašyti srautą ~30 s klipais (ffmpeg)")
    s.add_argument("camera")
    s.add_argument("--minutes", type=float, default=10)
    s.add_argument("--no-process", action="store_true")
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("report", help="kameros skaičiavimo tikslumas")
    s.add_argument("camera")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("search", help="parametrų paieška per įvertintus klipus")
    s.add_argument("camera")
    s.add_argument("--grid", help='JSON, pvz. \'{"conf":[0.3,0.4],"min_frames":[2,4]}\'')
    s.add_argument("--apply", action="store_true", help="pritaikyti geriausius")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("export-labels", help="pažymėti kadrai -> COCO (YOLOX mokymui)")
    s.add_argument("out")
    s.add_argument("--camera")
    s.set_defaults(fn=cmd_export_labels)

    s = sub.add_parser("odds", help="pradinės tikimybės iš statistikos")
    s.add_argument("camera")
    s.add_argument("--weekday", type=int, required=True, help="0=pirmadienis")
    s.add_argument("--hour", type=int, required=True)
    s.add_argument("--stake", type=float, default=100, help="tipinis statymas")
    s.set_defaults(fn=cmd_odds)

    s = sub.add_parser("cleanup", help="ištrinti senus klipus (14 d. / 30 d.)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_cleanup)

    s = sub.add_parser("admin", help="kalibravimo ir žymėjimo įrankis naršyklėje")
    s.add_argument("--host", default=config.ADMIN_HOST)
    s.add_argument("--port", type=int, default=config.ADMIN_PORT)
    s.set_defaults(fn=cmd_admin)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
