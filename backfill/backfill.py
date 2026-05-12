"""Backfill snapshots + motion clips + Gemini AI log from UniFi Protect history.

Runs locally on `ullm` for speed. Produces a /home/belitz/redtail/backfill/work/
tree that mirrors the production data layout, plus a backfill.db SQLite that
slots into the production schema. Deploy step (separate script) rsync's the
artifacts to .30.103 and merges the DB rows.

Resumable: snapshots/clips are skipped if already present in the local DB.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make production app/ importable so we reuse Analyzer / Database / etc.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analyzer import Analyzer  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.util import ensure_dir, setup_logging  # noqa: E402

from unifi import UnifiClient  # noqa: E402

log = logging.getLogger("backfill")


# ---- env loading ----

def _load_env_local(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# ---- helpers ----

def _dt_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000).astimezone()


def _iso(ms: int) -> str:
    return _dt_from_ms(ms).isoformat(timespec="seconds")


def _snapshot_path(root: Path, ts_ms: int) -> Path:
    dt = _dt_from_ms(ts_ms)
    return root / "snapshots" / dt.strftime("%Y-%m-%d") / f"{dt.strftime('%H%M%S')}.jpg"


def _clip_path(root: Path, ts_ms: int) -> Path:
    dt = _dt_from_ms(ts_ms)
    return root / "clips" / dt.strftime("%Y-%m-%d") / f"{dt.strftime('%H%M%S')}.mp4"


# ---- phases ----

def fetch_snapshots(
    unifi: UnifiClient,
    db: Database,
    camera_id: str,
    start_ms: int,
    end_ms: int,
    work_root: Path,
    interval_seconds: int,
    workers: int,
) -> int:
    interval_ms = interval_seconds * 1000
    # Align to whole interval boundaries for a clean timelapse cadence
    aligned = (start_ms // interval_ms + 1) * interval_ms
    timestamps = list(range(aligned, end_ms, interval_ms))

    log.info("planning %d snapshot fetches (every %ds across %.1f days)",
             len(timestamps), interval_seconds, (end_ms - start_ms) / 86400000)

    # Skip ones already on disk + in DB
    todo = []
    for ts in timestamps:
        path = _snapshot_path(work_root, ts)
        if path.exists() and path.stat().st_size > 1024:
            continue
        todo.append((ts, path))

    if not todo:
        log.info("all snapshots already present, skipping fetch")
        return 0

    log.info("%d snapshots to fetch (%d already on disk)", len(todo), len(timestamps) - len(todo))

    count_ok = 0
    count_missing = 0
    count_fail = 0
    start_time = time.time()

    def _one(item):
        ts, path = item
        try:
            data = unifi.recording_snapshot(camera_id, ts)
            if data is None:
                return ts, path, None
            ensure_dir(path.parent)
            path.write_bytes(data)
            return ts, path, len(data)
        except Exception as e:
            log.warning("snapshot ts=%d failed: %s", ts, e)
            return ts, path, -1

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (ts, path, size) in enumerate(pool.map(_one, todo), 1):
            if size is None:
                count_missing += 1
            elif size < 0:
                count_fail += 1
            else:
                count_ok += 1
                db.record_snapshot(_iso(ts), str(path))
            if i % 50 == 0 or i == len(todo):
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed else 0
                eta = (len(todo) - i) / rate if rate else 0
                log.info("snapshots: %d/%d  ok=%d missing=%d fail=%d  %.1f/s  eta=%ds",
                         i, len(todo), count_ok, count_missing, count_fail, rate, int(eta))

    return count_ok


def fetch_motion_clips(
    unifi: UnifiClient,
    db: Database,
    camera_id: str,
    start_ms: int,
    end_ms: int,
    work_root: Path,
    pre_padding_seconds: int = 2,
    post_padding_seconds: int = 2,
    workers: int = 3,
) -> int:
    log.info("listing motion events across %.1f days...", (end_ms - start_ms) / 86400000)
    events = list(unifi.motion_events(camera_id, start_ms, end_ms))
    log.info("%d motion events found", len(events))
    if not events:
        return 0

    # Skip events already downloaded
    todo = []
    for e in events:
        path = _clip_path(work_root, e.start_ms)
        if path.exists() and path.stat().st_size > 50_000:
            continue
        todo.append((e, path))

    if not todo:
        log.info("all motion clips already on disk")
        return 0

    log.info("%d clips to download (%d already present)", len(todo), len(events) - len(todo))

    pre_ms = pre_padding_seconds * 1000
    post_ms = post_padding_seconds * 1000
    count_ok = 0
    count_fail = 0
    start_time = time.time()

    def _one(item):
        e, path = item
        try:
            ensure_dir(path.parent)
            unifi.video_export(
                camera_id,
                e.start_ms - pre_ms,
                e.end_ms + post_ms,
                out_path=str(path),
            )
            size = path.stat().st_size if path.exists() else 0
            if size < 50_000:
                log.warning("clip event=%s suspiciously small (%d bytes)", e.id, size)
                return e, path, -1
            return e, path, size
        except Exception as ex:
            log.warning("clip event=%s failed: %s", e.id, ex)
            return e, path, -1

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (e, path, size) in enumerate(pool.map(_one, todo), 1):
            if size and size > 0:
                count_ok += 1
                duration = (e.end_ms - e.start_ms + 4000) / 1000
                trigger = f"motion:unifi:score={e.score}"
                if e.smart_detect_types:
                    trigger += ":" + ",".join(e.smart_detect_types)
                db.record_clip(_iso(e.start_ms), duration, str(path), trigger)
            else:
                count_fail += 1
            if i % 10 == 0 or i == len(todo):
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed else 0
                eta = (len(todo) - i) / rate if rate else 0
                log.info("clips: %d/%d  ok=%d fail=%d  %.2f/s  eta=%ds",
                         i, len(todo), count_ok, count_fail, rate, int(eta))

    return count_ok


def analyze_all(db: Database, cfg, workers: int = 6) -> tuple[int, int]:
    """Run Gemini analysis on every unanalyzed snapshot and clip, in parallel."""
    analyzer = Analyzer(cfg, db)
    if not analyzer.enabled:
        log.warning("analyzer disabled (no GEMINI_API_KEY); skipping AI phase")
        return 0, 0

    # The Analyzer.process_pending() picks small batches; loop until empty.
    # For parallelism we drive it with a thread pool that each calls a single-item analyze.
    snap_total = 0
    clip_total = 0
    while True:
        snaps = db.pending_snapshots(limit=workers)
        clips = db.pending_clips(limit=workers)
        if not snaps and not clips:
            break

        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for row in snaps:
                futures.append(("snap", row, pool.submit(analyzer._analyze_snapshot, row)))
            for row in clips:
                futures.append(("clip", row, pool.submit(analyzer._analyze_clip, row)))
            for kind, row, fut in futures:
                try:
                    fut.result()
                except Exception:
                    log.exception("%s %s analysis raised; marking analyzed to skip", kind, row["id"])
                    # Avoid infinite loop on a poison row
                    if kind == "snap":
                        db.mark_snapshot_analyzed(row["id"])
                    else:
                        db.mark_clip_analyzed(row["id"], keep=True, label=None)
        snap_total += len(snaps)
        clip_total += len(clips)
        log.info("analyzed batch: +%d snaps, +%d clips  (running totals: %d, %d)",
                 len(snaps), len(clips), snap_total, clip_total)
    return snap_total, clip_total


def write_daily_summaries(db: Database, cfg, start_ms: int, end_ms: int) -> int:
    """Compose one Gemini-written narrative per day spanned by the backfill."""
    analyzer = Analyzer(cfg, db)
    if not analyzer.enabled:
        return 0
    start_d = _dt_from_ms(start_ms).date()
    end_d = _dt_from_ms(end_ms).date()
    days_written = 0
    cur = start_d
    while cur <= end_d:
        day = cur.isoformat()
        if analyzer.daily_summary(day):
            days_written += 1
        cur = cur.fromordinal(cur.toordinal() + 1)
    return days_written


# ---- main ----

def main():
    p = argparse.ArgumentParser(description="Backfill UniFi Protect history into the nest-box dataset")
    p.add_argument("--camera-id", required=True, help="UniFi Protect camera ID (find it in the Protect UI or via the API)")
    p.add_argument("--work-dir", default=str(Path(__file__).parent / "work"), help="Local mirror of production data layout")
    p.add_argument("--snapshot-interval", type=int, default=300, help="Seconds between backfilled snapshots (default 300)")
    p.add_argument("--snapshot-workers", type=int, default=6, help="Concurrent snapshot downloads")
    p.add_argument("--clip-workers", type=int, default=3, help="Concurrent clip downloads")
    p.add_argument("--analyze-workers", type=int, default=4, help="Concurrent Gemini analyses")
    p.add_argument("--skip-snapshots", action="store_true")
    p.add_argument("--skip-clips", action="store_true")
    p.add_argument("--skip-analysis", action="store_true")
    p.add_argument("--skip-summaries", action="store_true")
    p.add_argument("--start-ms", type=int, help="Override start of backfill window (epoch ms)")
    p.add_argument("--end-ms", type=int, help="Override end of backfill window (epoch ms)")
    p.add_argument("--dry-run", action="store_true", help="Plan only; don't fetch")
    args = p.parse_args()

    _load_env_local(Path(__file__).parent / ".env.local")
    # Reuse the production config for paths/keys but point at our local work dir
    work = Path(args.work_dir)
    os.environ["DATA_DIR"] = str(work)
    os.environ["STATE_DIR"] = str(work / "state")
    os.environ["RTSP_URL"] = os.environ.get("RTSP_URL", "rtsps://placeholder")  # not used in backfill
    cfg = load_config()
    setup_logging(cfg.log_level)

    for d in (cfg.snapshots_dir, cfg.clips_dir, cfg.timelapses_dir, work / "state"):
        ensure_dir(d)

    db = Database(cfg.db_path)

    unifi = UnifiClient(
        host=os.environ["UNIFI_HOST"],
        username=os.environ["UNIFI_USERNAME"],
        password=os.environ["UNIFI_PASSWORD"],
    )
    unifi.login()

    rec_start, rec_end = unifi.recording_window(args.camera_id)
    start_ms = args.start_ms or rec_start
    end_ms = args.end_ms or rec_end
    log.info("recording window: %s → %s  (%.1f days)",
             _iso(rec_start), _iso(rec_end), (rec_end - rec_start) / 86400000)
    log.info("backfilling:      %s → %s  (%.1f days)",
             _iso(start_ms), _iso(end_ms), (end_ms - start_ms) / 86400000)

    if args.dry_run:
        n_snaps = (end_ms - start_ms) // (args.snapshot_interval * 1000)
        log.info("dry-run: would fetch ~%d snapshots and list motion events", n_snaps)
        return

    if not args.skip_snapshots:
        fetch_snapshots(unifi, db, args.camera_id, start_ms, end_ms,
                        work, args.snapshot_interval, args.snapshot_workers)
    if not args.skip_clips:
        fetch_motion_clips(unifi, db, args.camera_id, start_ms, end_ms,
                           work, workers=args.clip_workers)
    if not args.skip_analysis:
        analyze_all(db, cfg, workers=args.analyze_workers)
    if not args.skip_summaries:
        n = write_daily_summaries(db, cfg, start_ms, end_ms)
        log.info("wrote %d daily summaries", n)

    log.info("backfill complete. work dir: %s", work)


if __name__ == "__main__":
    main()
