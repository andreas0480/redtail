"""Daily review helper for the redtail event log.

Pulls suspect events from the production DB on .30.103, copies the underlying
snapshots/clips locally so Claude can inspect them, and writes a manifest
listing what likely needs correction. Supports applying corrections back to
the production DB once Claude has reviewed.

Usage:
  python review.py prepare [--since HOURS] [--out ./review_packet]
  python review.py apply PATCH_FILE

  PATCH_FILE is a JSON array of objects:
    {"event_id": 123, "set": {"event_type": "incubating", "narrative": "...", "confidence": 0.95}}
    {"event_id": 124, "delete": true}

Designed to be invoked manually (or by a scheduled agent) once per day.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("review")

REMOTE = os.environ.get("REDTAIL_HOST", "")
if not REMOTE:
    sys.exit("Set REDTAIL_HOST=<ip-or-hostname> before running review.py")
EARLIEST_HATCH_DATE = "2026-05-28"
SUSPECT_EVENT_TYPES_BEFORE_HATCH = ("chicks_visible", "chick_hatching", "feeding")


def ssh(cmd: str, capture: bool = True) -> str:
    full = ["ssh", REMOTE, cmd]
    if capture:
        return subprocess.check_output(full, text=True)
    subprocess.run(full, check=True)
    return ""


def dump_db_query(sql: str) -> list[dict]:
    """Run a SELECT against the prod SQLite via docker exec; return list of dicts."""
    py = f'''
import sqlite3, json
c = sqlite3.connect("/state/events.db")
c.row_factory = sqlite3.Row
print(json.dumps([dict(r) for r in c.execute({sql!r}).fetchall()], default=str))
'''
    out = ssh(f"docker exec -i redtail python -c {json.dumps(py)}")
    return json.loads(out)


def prepare(since_hours: int, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "images").mkdir(exist_ok=True)
    cutoff = (datetime.now().astimezone() - timedelta(hours=since_hours)).isoformat(timespec="seconds")
    log.info("collecting events newer than %s", cutoff)

    # Pull recent events with associated snapshot/clip path
    events = dump_db_query(f'''
        SELECT e.id AS event_id, e.occurred_at, e.source, e.source_id, e.event_type, e.confidence, e.narrative,
               s.path AS snapshot_path, c.path AS clip_path
        FROM events e
        LEFT JOIN snapshots s ON e.source = 'snapshot' AND s.id = e.source_id
        LEFT JOIN clips c     ON e.source = 'clip' AND c.id = e.source_id
        WHERE e.occurred_at >= '{cutoff}'
        ORDER BY e.occurred_at ASC
    '''.strip())
    log.info("%d events in window", len(events))

    suspect = []
    for ev in events:
        reasons = []
        ev_date = ev["occurred_at"][:10]
        if ev["confidence"] is not None and ev["confidence"] < 0.7:
            reasons.append(f"low_confidence={ev['confidence']:.2f}")
        if ev["event_type"] in SUSPECT_EVENT_TYPES_BEFORE_HATCH and ev_date < EARLIEST_HATCH_DATE:
            reasons.append(f"biologically_impossible_before_{EARLIEST_HATCH_DATE}")
        if ev["event_type"] == "intruder":
            reasons.append("intruder_needs_human_review")
        if reasons:
            ev["suspect_reasons"] = reasons
            suspect.append(ev)

    log.info("flagged %d suspect events", len(suspect))

    # Copy media for suspect events into the packet
    for ev in suspect:
        media = ev["snapshot_path"] or ev["clip_path"]
        if not media:
            continue
        # The remote path is the SMB-mounted /data path inside the container; via the
        # host SMB mount it's /mnt/data/redtail/<rel>. We use docker cp from the
        # container's /data/ to keep things simple.
        rel = Path(media).name
        local = out / "images" / f"event_{ev['event_id']:06d}_{rel}"
        try:
            subprocess.run(
                ["ssh", REMOTE, f"docker cp redtail:{media} -"],
                check=True,
                stdout=open(out / f".tmp_event_{ev['event_id']}.tar", "wb"),
            )
            # docker cp '-' streams a tar. Extract.
            tar_path = out / f".tmp_event_{ev['event_id']}.tar"
            subprocess.run(["tar", "-xf", str(tar_path), "-C", str(out / "images")], check=True)
            extracted = out / "images" / Path(media).name
            if extracted.exists() and extracted != local:
                shutil.move(str(extracted), str(local))
            tar_path.unlink(missing_ok=True)
            ev["local_image"] = str(local.relative_to(out))
        except Exception as e:
            log.warning("failed to copy media for event %s: %s", ev["event_id"], e)
            ev["local_image"] = None

    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "since": cutoff,
        "total_events": len(events),
        "suspect_count": len(suspect),
        "rules_applied": {
            "low_confidence_threshold": 0.7,
            "earliest_hatch_date": EARLIEST_HATCH_DATE,
            "biologically_impossible_types_before_hatch": list(SUSPECT_EVENT_TYPES_BEFORE_HATCH),
        },
        "suspect_events": suspect,
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    log.info("manifest → %s", manifest_path)
    log.info("review the images in %s, then write a patch.json and run: python review.py apply patch.json", out)


def apply(patch_file: Path) -> None:
    patches = json.loads(patch_file.read_text())
    if not isinstance(patches, list):
        raise ValueError("patch file must be a JSON array")

    log.info("applying %d patches to production DB...", len(patches))
    payload = json.dumps(patches)
    py = f'''
import sqlite3, json
patches = json.loads({json.dumps(payload)})
c = sqlite3.connect("/state/events.db")
updated = 0; deleted = 0
with c:
    for p in patches:
        eid = p["event_id"]
        if p.get("delete"):
            c.execute("DELETE FROM events WHERE id = ?", (eid,))
            deleted += 1
            continue
        sets = p.get("set", {{}})
        cols = ", ".join(f"{{k}} = ?" for k in sets)
        if not cols:
            continue
        c.execute(f"UPDATE events SET {{cols}} WHERE id = ?", list(sets.values()) + [eid])
        updated += 1
print(f"updated={{updated}} deleted={{deleted}}")
'''
    out = ssh(f"docker exec -i redtail python -c {json.dumps(py)}")
    log.info(out.strip())

    # Force a daily summary refresh for affected days
    days = sorted({p.get("day") for p in patches if "day" in p}) or [datetime.now().astimezone().strftime("%Y-%m-%d")]
    for day in days:
        py2 = f'''
from app.config import load_config
from app.db import Database
from app.analyzer import Analyzer
cfg = load_config()
analyzer = Analyzer(cfg, Database(cfg.db_path))
print("rewrote summary:", analyzer.daily_summary({day!r}))
'''
        ssh(f"docker exec -i redtail python -c {json.dumps(py2)}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("prepare", help="Collect suspect events into a review packet")
    pp.add_argument("--since", type=int, default=36, help="Hours to look back (default 36)")
    pp.add_argument("--out", type=Path, default=Path("./review_packet"))

    ap = sub.add_parser("apply", help="Apply a JSON patch file back to production")
    ap.add_argument("patch_file", type=Path)

    args = p.parse_args()
    if args.cmd == "prepare":
        prepare(args.since, args.out)
    elif args.cmd == "apply":
        apply(args.patch_file)


if __name__ == "__main__":
    main()
