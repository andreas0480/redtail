"""Single-frame RTSP snapshot capture.

Runs on a scheduled interval (default: every 5 minutes). Each capture
becomes one row in `snapshots` and one file under
`/data/snapshots/<YYYY-MM-DD>/<HHMMSS>.jpg`. The analyzer picks pending
rows up asynchronously.
"""

import logging
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import Database
from .util import ensure_dir, now_iso, run_ffmpeg

log = logging.getLogger(__name__)


def capture_snapshot(cfg: Config, db: Database) -> Path | None:
    """Pull a single keyframe from the RTSP stream into a dated folder."""
    now = datetime.now().astimezone()
    day_dir = ensure_dir(cfg.snapshots_dir / now.strftime("%Y-%m-%d"))
    out_path = day_dir / now.strftime("%H%M%S.jpg")

    result = run_ffmpeg(
        [
            "-rtsp_transport", "tcp",
            "-y",
            "-i", cfg.rtsp_url,
            "-frames:v", "1",
            "-q:v", "3",
            str(out_path),
        ],
        timeout=30,
    )

    if result.returncode != 0 or not out_path.exists():
        log.warning("snapshot failed: rc=%s err=%s", result.returncode, result.stderr.strip()[:300])
        return None

    if out_path.stat().st_size < 1024:
        log.warning("snapshot suspiciously small (%d bytes), deleting", out_path.stat().st_size)
        out_path.unlink(missing_ok=True)
        return None

    db.record_snapshot(now_iso(), str(out_path))
    log.info("snapshot %s (%d KB)", out_path.name, out_path.stat().st_size // 1024)
    return out_path
