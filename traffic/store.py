"""Local SQLite store for cameras, clips, reviews and frame labels.

Mirrors the Supabase tables (traffic_cameras, traffic_clips,
traffic_calibration) so calibrating locally needs no network; the column names
match so syncing later is a straight copy.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .params import CameraParams

SCHEMA = """
create table if not exists cameras (
  id text primary key,
  name text not null,
  city text,
  source_url text,
  permission_status text not null default 'pending',   -- pending / granted / denied
  photo_path text,
  still_path text,
  params_json text not null default '{}',
  enabled integer not null default 0,
  created_at text not null
);
create table if not exists clips (
  id text primary key,
  camera_id text not null references cameras(id),
  recorded_at text not null,
  raw_path text not null,
  annotated_path text,
  tracks_path text,
  dets_path text,
  status text not null default 'processing',   -- processing/approved/rejected/used/error
  cv_count integer,
  quality_json text,
  params_json text,
  error text,
  created_at text not null,
  processed_at text
);
create index if not exists clips_camera_status on clips(camera_id, status);
create table if not exists calibration (
  id integer primary key autoincrement,
  clip_id text not null references clips(id),
  cv_count integer not null,
  true_count integer not null,
  params_json text not null,
  taps_json text,                 -- video times (s) the reviewer tapped a vehicle
  reviewer text,
  created_at text not null
);
create index if not exists calibration_clip on calibration(clip_id);
create table if not exists labels (
  id integer primary key autoincrement,
  clip_id text not null references clips(id),
  frame_index integer not null,
  boxes_json text not null,       -- [{"cls": "car", "box": [x1,y1,x2,y2]}], frame px
  width integer not null,
  height integer not null,
  reviewer text,
  created_at text not null,
  unique (clip_id, frame_index)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.executescript(SCHEMA)
    return conn


# ── cameras ────────────────────────────────────────────────────────────────

def add_camera(conn, cam_id: str, name: str, city: str = "", source_url: str = "",
               permission_status: str = "pending", params: CameraParams | None = None) -> None:
    conn.execute(
        "insert into cameras (id, name, city, source_url, permission_status, params_json,"
        " created_at) values (?, ?, ?, ?, ?, ?, ?)",
        (cam_id, name, city, source_url, permission_status,
         json.dumps((params or CameraParams()).to_dict()), now()))
    conn.commit()


def get_camera(conn, cam_id: str) -> sqlite3.Row | None:
    return conn.execute("select * from cameras where id = ?", (cam_id,)).fetchone()


def list_cameras(conn) -> list[sqlite3.Row]:
    return conn.execute("select * from cameras order by created_at").fetchall()


def camera_params(conn, cam_id: str) -> CameraParams:
    row = get_camera(conn, cam_id)
    if row is None:
        raise KeyError(f"camera {cam_id} not found")
    return CameraParams.from_dict(json.loads(row["params_json"] or "{}"))


def set_camera_params(conn, cam_id: str, params: CameraParams) -> None:
    conn.execute("update cameras set params_json = ? where id = ?",
                 (json.dumps(params.to_dict()), cam_id))
    conn.commit()


def update_camera(conn, cam_id: str, **fields) -> None:
    allowed = {"name", "city", "source_url", "permission_status", "photo_path",
               "still_path", "enabled"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    conn.execute(f"update cameras set {', '.join(f'{k} = ?' for k in sets)} where id = ?",
                 (*sets.values(), cam_id))
    conn.commit()


# ── clips ──────────────────────────────────────────────────────────────────

def clip_dir(cam_id: str, clip_id: str) -> Path:
    return config.CLIPS_DIR / cam_id / clip_id


def add_clip(conn, cam_id: str, raw_path: str | Path, recorded_at: str) -> str:
    clip_id = uuid.uuid4().hex[:12]
    conn.execute(
        "insert into clips (id, camera_id, recorded_at, raw_path, status, created_at)"
        " values (?, ?, ?, ?, 'processing', ?)",
        (clip_id, cam_id, recorded_at, str(raw_path), now()))
    conn.commit()
    return clip_id


def get_clip(conn, clip_id: str) -> sqlite3.Row | None:
    return conn.execute("select * from clips where id = ?", (clip_id,)).fetchone()


def list_clips(conn, cam_id: str | None = None, status: str | None = None) -> list[sqlite3.Row]:
    q, args = "select * from clips where 1=1", []
    if cam_id:
        q += " and camera_id = ?"
        args.append(cam_id)
    if status:
        q += " and status = ?"
        args.append(status)
    return conn.execute(q + " order by recorded_at", args).fetchall()


def update_clip(conn, clip_id: str, **fields) -> None:
    for k in ("quality_json", "params_json"):
        if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
            fields[k] = json.dumps(fields[k])
    conn.execute(f"update clips set {', '.join(f'{k} = ?' for k in fields)} where id = ?",
                 (*fields.values(), clip_id))
    conn.commit()


# ── calibration ────────────────────────────────────────────────────────────

def add_review(conn, clip_id: str, cv_count: int, true_count: int, params: CameraParams,
               taps: list[float] | None = None, reviewer: str = "admin") -> None:
    conn.execute(
        "insert into calibration (clip_id, cv_count, true_count, params_json, taps_json,"
        " reviewer, created_at) values (?, ?, ?, ?, ?, ?, ?)",
        (clip_id, cv_count, true_count, json.dumps(params.to_dict()),
         json.dumps(taps) if taps is not None else None, reviewer, now()))
    conn.commit()


def latest_reviews(conn, cam_id: str) -> list[sqlite3.Row]:
    """The newest review of each clip of a camera (a re-review replaces the old)."""
    return conn.execute(
        """select c.*, k.camera_id, k.dets_path, k.raw_path, k.cv_count as clip_cv_count
             from calibration c
             join clips k on k.id = c.clip_id
            where k.camera_id = ?
              and c.id = (select max(id) from calibration where clip_id = c.clip_id)
            order by k.recorded_at""", (cam_id,)).fetchall()


# ── labels ─────────────────────────────────────────────────────────────────

def save_labels(conn, clip_id: str, frame_index: int, boxes: list[dict], width: int,
                height: int, reviewer: str = "admin") -> None:
    conn.execute(
        "insert into labels (clip_id, frame_index, boxes_json, width, height, reviewer,"
        " created_at) values (?, ?, ?, ?, ?, ?, ?)"
        " on conflict (clip_id, frame_index) do update set boxes_json = excluded.boxes_json,"
        " width = excluded.width, height = excluded.height, reviewer = excluded.reviewer,"
        " created_at = excluded.created_at",
        (clip_id, frame_index, json.dumps(boxes), width, height, reviewer, now()))
    conn.commit()


def get_labels(conn, clip_id: str, frame_index: int) -> sqlite3.Row | None:
    return conn.execute("select * from labels where clip_id = ? and frame_index = ?",
                        (clip_id, frame_index)).fetchone()


def all_labels(conn, cam_id: str | None = None) -> list[sqlite3.Row]:
    q = ("select l.*, k.camera_id, k.raw_path, k.params_json as clip_params"
         " from labels l join clips k on k.id = l.clip_id")
    args = []
    if cam_id:
        q += " where k.camera_id = ?"
        args.append(cam_id)
    return conn.execute(q + " order by l.clip_id, l.frame_index", args).fetchall()
