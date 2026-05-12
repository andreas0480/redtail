"""Push backfilled snapshots, clips, and DB rows to production.

Runs on `ullm`. Streams files into the container via SSH + docker, then merges
the local SQLite into the production DB. Triggers cumulative timelapse rebuild.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("deploy")

REMOTE = os.environ.get("REDTAIL_HOST", "")
if not REMOTE:
    sys.exit("Set REDTAIL_HOST=<ip-or-hostname> before running deploy.py")


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, **kwargs)


def stream_dir(local: Path, container_path: str) -> None:
    """Tar a local directory into the container's filesystem."""
    if not local.exists():
        log.info("skipping %s (does not exist)", local)
        return
    file_count = sum(1 for _ in local.rglob("*") if _.is_file())
    log.info("streaming %s (%d files) → container:%s", local, file_count, container_path)
    # Use tar -C local . to preserve relative structure; extract inside the container
    # Pre-create the container dir in a separate exec so stdin is exclusive to the tar exec
    subprocess.run(
        ["ssh", REMOTE, f"docker exec redtail mkdir -p {container_path}"],
        check=True,
    )
    tar = subprocess.Popen(
        ["tar", "-cf", "-", "-C", str(local), "."],  # no gzip — Gb LAN is faster than CPU compression
        stdout=subprocess.PIPE,
    )
    ssh_cmd = [
        "ssh", REMOTE,
        f"docker exec -i redtail tar -xf - -C {container_path}",
    ]
    result = subprocess.run(ssh_cmd, stdin=tar.stdout, check=True)
    tar.stdout.close()
    tar.wait()
    if tar.returncode != 0:
        raise RuntimeError(f"local tar exited {tar.returncode}")


def merge_database(local_db: Path) -> None:
    """Copy local SQLite to the container and merge its rows into events.db."""
    log.info("copying local DB %s → remote", local_db)
    run(["scp", str(local_db), f"{REMOTE}:/tmp/backfill.db"])
    run(["ssh", REMOTE, "docker cp /tmp/backfill.db redtail:/tmp/backfill.db"])

    merge_script = r'''
import sqlite3, sys, os
src = sqlite3.connect("/tmp/backfill.db")
dst = sqlite3.connect("/state/events.db")
dst.execute("PRAGMA foreign_keys=ON")

def insert_snapshots():
    src_rows = src.execute("SELECT captured_at, path FROM snapshots").fetchall()
    inserted = 0
    skipped = 0
    for captured_at, path in src_rows:
        # Translate local backfill path to production container path
        if "snapshots/" in path:
            path = "/data/snapshots/" + path.split("snapshots/")[1]
        cur = dst.execute(
            "INSERT OR IGNORE INTO snapshots (captured_at, path, analyzed) VALUES (?, ?, 1)",
            (captured_at, path),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped

def insert_clips():
    src_rows = src.execute("SELECT started_at, duration_seconds, path, trigger, keep, label, analyzed FROM clips").fetchall()
    inserted = 0; skipped = 0
    for started_at, dur, path, trig, keep, label, analyzed in src_rows:
        # Translate local backfill path to production container path
        if "clips/" in path:
            path = "/data/clips/" + path.split("clips/")[1]
        cur = dst.execute(
            "INSERT OR IGNORE INTO clips (started_at, duration_seconds, path, trigger, analyzed, keep, label) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (started_at, dur, path, trig, analyzed, keep, label),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped

def insert_events():
    # Map source_ids from backfill to prod by looking up the same path.
    # snapshot mapping
    snap_map = {}
    for row in src.execute("SELECT id, path FROM snapshots"):
        prod = dst.execute("SELECT id FROM snapshots WHERE path = ?", (row[1],)).fetchone()
        if prod: snap_map[row[0]] = prod[0]
    clip_map = {}
    for row in src.execute("SELECT id, path FROM clips"):
        prod = dst.execute("SELECT id FROM clips WHERE path = ?", (row[1],)).fetchone()
        if prod: clip_map[row[0]] = prod[0]

    inserted = 0
    for occurred_at, source, source_id, event_type, confidence, narrative, raw_json in \
            src.execute("SELECT occurred_at, source, source_id, event_type, confidence, narrative, raw_json FROM events"):
        if source == "snapshot":
            mapped = snap_map.get(source_id)
        elif source == "clip":
            mapped = clip_map.get(source_id)
        else:
            mapped = source_id
        cur = dst.execute(
            "INSERT INTO events (occurred_at, source, source_id, event_type, confidence, narrative, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (occurred_at, source, mapped, event_type, confidence, narrative, raw_json),
        )
        inserted += 1
    return inserted

def insert_daily_summaries():
    inserted = 0
    for day, summary, events_count, timelapse_path, created_at in \
            src.execute("SELECT day, summary, events_count, timelapse_path, created_at FROM daily_summaries"):
        cur = dst.execute(
            "INSERT OR REPLACE INTO daily_summaries (day, summary, events_count, timelapse_path, created_at) VALUES (?, ?, ?, ?, ?)",
            (day, summary, events_count, timelapse_path, created_at),
        )
        inserted += 1
    return inserted

si, ss = insert_snapshots()
ci, cs = insert_clips()
ei = insert_events()
di = insert_daily_summaries()
dst.commit()
print(f"snapshots merged: +{si} (skipped {ss})")
print(f"clips merged: +{ci} (skipped {cs})")
print(f"events merged: +{ei}")
print(f"daily summaries: +{di}")
src.close(); dst.close()
'''
    log.info("running DB merge inside container...")
    run([
        "ssh", REMOTE,
        f"docker exec -i redtail python -c '{merge_script}'",
    ])


def rebuild_cumulative(timeout: int = 600) -> None:
    """Kick the in-app endpoint to rebuild the cumulative timelapse."""
    log.info("triggering cumulative timelapse rebuild (this can take a few minutes)...")
    run([
        "ssh", REMOTE,
        "curl -sS -X POST http://127.0.0.1:8765/admin/build-timelapse",
    ])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--work-dir", default=str(Path(__file__).parent / "work"))
    p.add_argument("--skip-snapshots", action="store_true")
    p.add_argument("--skip-clips", action="store_true")
    p.add_argument("--skip-merge", action="store_true")
    p.add_argument("--skip-timelapse", action="store_true")
    args = p.parse_args()

    work = Path(args.work_dir).resolve()
    if not work.exists():
        log.error("work dir %s does not exist", work)
        sys.exit(1)

    if not args.skip_snapshots:
        stream_dir(work / "snapshots", "/data/snapshots")
    if not args.skip_clips:
        stream_dir(work / "clips", "/data/clips")
    if not args.skip_merge:
        db = work / "state" / "events.db"
        if not db.exists():
            log.error("local DB %s not found", db)
            sys.exit(2)
        merge_database(db)
    if not args.skip_timelapse:
        rebuild_cumulative()

    log.info("deploy complete")


if __name__ == "__main__":
    main()
