"""Sonnet-subagent classification orchestrator.

Builds batches of pending snapshots/clips into JSON manifests, lets parallel
subagents process them, and merges the resulting JSONL into the SQLite DB.

Workflow:
    python coordinator.py extract-clip-frames    # one-time prep for clips
    python coordinator.py build-batches          # write batches/{snap,clip}_NNN.json
    # ... spawn subagents (separate Agent tool calls), each writes results/*.jsonl ...
    python coordinator.py merge                  # ingest all results/*.jsonl into DB
    python coordinator.py status                 # how many batches done, what's left
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("coord")

WORK = ROOT / "work"
DB_PATH = WORK / "state" / "events.db"
BATCHES_DIR = ROOT / "batches"
RESULTS_DIR = ROOT / "results"
CLIP_FRAMES_DIR = WORK / "clip_frames"

SNAPSHOTS_PER_BATCH = 30
CLIPS_PER_BATCH = 12  # each clip has 4 frames; 12 clips × 4 frames = 48 reads/agent


def _db():
    import sqlite3
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def extract_clip_frames(count: int = 4) -> None:
    """For every clip, sample N evenly-spaced JPEG frames into work/clip_frames/<clip_id>/."""
    CLIP_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    db = _db()
    rows = list(db.execute("SELECT id, path FROM clips"))
    todo = []
    for r in rows:
        d = CLIP_FRAMES_DIR / str(r["id"])
        if d.exists() and len(list(d.glob("frame_*.jpg"))) >= count:
            continue
        todo.append((r["id"], r["path"], d))
    log.info("extracting %d clip frame sets (%d already done)", len(todo), len(rows) - len(todo))

    for i, (clip_id, path, outdir) in enumerate(todo, 1):
        outdir.mkdir(parents=True, exist_ok=True)
        # Probe duration
        try:
            dur_out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nokey=1:noprint_wrappers=1", path],
                capture_output=True, text=True, timeout=15, check=False,
            )
            duration = float(dur_out.stdout.strip() or 0)
        except Exception:
            duration = 0.0
        if duration <= 0:
            log.warning("clip %s has no duration; skipping", clip_id)
            continue
        timestamps = [duration * (k + 0.5) / count for k in range(count)]
        for k, ts in enumerate(timestamps):
            out_path = outdir / f"frame_{k:02d}.jpg"
            if out_path.exists():
                continue
            subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-ss", f"{ts:.2f}", "-i", path,
                 "-frames:v", "1", "-q:v", "4",
                 str(out_path)],
                check=False, timeout=30,
            )
        if i % 50 == 0:
            log.info("frames extracted: %d/%d", i, len(todo))
    log.info("clip frame extraction complete")


def build_batches() -> None:
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    db = _db()

    # Snapshots
    snap_rows = list(db.execute("SELECT id, path, captured_at FROM snapshots WHERE analyzed = 0 ORDER BY captured_at ASC"))
    log.info("pending snapshots: %d", len(snap_rows))
    for i in range(0, len(snap_rows), SNAPSHOTS_PER_BATCH):
        chunk = snap_rows[i:i + SNAPSHOTS_PER_BATCH]
        batch_num = i // SNAPSHOTS_PER_BATCH
        batch = {
            "kind": "snapshot",
            "items": [
                {"id": r["id"], "path": r["path"], "captured_at": r["captured_at"]}
                for r in chunk
            ],
        }
        (BATCHES_DIR / f"snap_{batch_num:04d}.json").write_text(json.dumps(batch, indent=2))
    log.info("wrote %d snapshot batches of %d", (len(snap_rows) + SNAPSHOTS_PER_BATCH - 1) // SNAPSHOTS_PER_BATCH, SNAPSHOTS_PER_BATCH)

    # Clips (assumes extract-clip-frames was already run)
    clip_rows = list(db.execute("SELECT id, path, started_at, duration_seconds, trigger FROM clips WHERE analyzed = 0 ORDER BY started_at ASC"))
    log.info("pending clips: %d", len(clip_rows))
    for i in range(0, len(clip_rows), CLIPS_PER_BATCH):
        chunk = clip_rows[i:i + CLIPS_PER_BATCH]
        batch_num = i // CLIPS_PER_BATCH
        items = []
        for r in chunk:
            frame_dir = CLIP_FRAMES_DIR / str(r["id"])
            frames = sorted(frame_dir.glob("frame_*.jpg"))
            items.append({
                "id": r["id"],
                "started_at": r["started_at"],
                "duration_seconds": r["duration_seconds"],
                "trigger": r["trigger"],
                "frame_paths": [str(f) for f in frames],
            })
        batch = {"kind": "clip", "items": items}
        (BATCHES_DIR / f"clip_{batch_num:04d}.json").write_text(json.dumps(batch, indent=2))
    log.info("wrote %d clip batches of %d", (len(clip_rows) + CLIPS_PER_BATCH - 1) // CLIPS_PER_BATCH, CLIPS_PER_BATCH)


def merge() -> None:
    db = _db()
    inserted_events = 0
    marked_snaps = 0
    marked_clips = 0

    for jsonl in sorted(RESULTS_DIR.glob("snap_*.jsonl")):
        for line in jsonl.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                log.warning("bad jsonl line in %s: %s", jsonl.name, line[:100])
                continue
            sid = rec.get("id")
            if not sid:
                continue
            row = db.execute("SELECT captured_at FROM snapshots WHERE id = ?", (sid,)).fetchone()
            if not row:
                continue
            db.execute(
                "INSERT INTO events (occurred_at, source, source_id, event_type, confidence, narrative, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["captured_at"], "snapshot", sid, rec.get("event_type", "unknown"),
                 rec.get("confidence"), rec.get("narrative", "(no description)"),
                 json.dumps(rec)),
            )
            db.execute("UPDATE snapshots SET analyzed = 1 WHERE id = ?", (sid,))
            inserted_events += 1
            marked_snaps += 1
        db.commit()

    for jsonl in sorted(RESULTS_DIR.glob("clip_*.jsonl")):
        for line in jsonl.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = rec.get("id")
            if not cid:
                continue
            row = db.execute("SELECT started_at FROM clips WHERE id = ?", (cid,)).fetchone()
            if not row:
                continue
            db.execute(
                "INSERT INTO events (occurred_at, source, source_id, event_type, confidence, narrative, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["started_at"], "clip", cid, rec.get("event_type", "unknown"),
                 rec.get("confidence"), rec.get("narrative", "(no description)"),
                 json.dumps(rec)),
            )
            db.execute(
                "UPDATE clips SET analyzed = 1, keep = ?, label = ? WHERE id = ?",
                (1 if rec.get("keep", True) else 0, rec.get("label"), cid),
            )
            inserted_events += 1
            marked_clips += 1
        db.commit()

    log.info("merged: +%d events, marked %d snapshots and %d clips analyzed",
             inserted_events, marked_snaps, marked_clips)


def status() -> None:
    db = _db()
    snap_pending = db.execute("SELECT COUNT(*) FROM snapshots WHERE analyzed = 0").fetchone()[0]
    snap_total = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    clip_pending = db.execute("SELECT COUNT(*) FROM clips WHERE analyzed = 0").fetchone()[0]
    clip_total = db.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    snap_batches = sorted(BATCHES_DIR.glob("snap_*.json"))
    clip_batches = sorted(BATCHES_DIR.glob("clip_*.json"))
    snap_done_files = sorted(RESULTS_DIR.glob("snap_*.jsonl"))
    clip_done_files = sorted(RESULTS_DIR.glob("clip_*.jsonl"))
    print(f"snapshots: {snap_total - snap_pending}/{snap_total} analyzed")
    print(f"clips:     {clip_total - clip_pending}/{clip_total} analyzed")
    print(f"events recorded: {events}")
    print(f"snapshot batches: {len(snap_done_files)}/{len(snap_batches)} have results")
    print(f"clip batches:     {len(clip_done_files)}/{len(clip_batches)} have results")
    todo = [b for b in snap_batches if not (RESULTS_DIR / (b.stem + ".jsonl")).exists()]
    todo += [b for b in clip_batches if not (RESULTS_DIR / (b.stem + ".jsonl")).exists()]
    print(f"batches still TODO: {len(todo)}")
    if todo[:5]:
        print("  next:", [b.name for b in todo[:5]])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["extract-clip-frames", "build-batches", "merge", "status"])
    args = p.parse_args()
    if args.cmd == "extract-clip-frames":
        extract_clip_frames()
    elif args.cmd == "build-batches":
        build_batches()
    elif args.cmd == "merge":
        merge()
    elif args.cmd == "status":
        status()


if __name__ == "__main__":
    main()
