"""Daily and cumulative timelapse generation.

Daily timelapses are built once per night from each day's snapshots with
ffmpeg's concat demuxer. Frame duration is computed as `target_seconds /
frame_count` (clamped 2–25 fps) so the output runtime is roughly constant
regardless of how many snapshots were captured. The cumulative film
samples up to `max_per_day` frames from every day to keep the season-wide
video manageable.
"""

import logging
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import Config
from .db import Database
from .util import ensure_dir, run_ffmpeg, today_str

log = logging.getLogger(__name__)


class TimelapseBuilder:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    def build_daily(self, day: Optional[str] = None, target_seconds: int = 30) -> Optional[Path]:
        day = day or (datetime.now().astimezone() - timedelta(seconds=60)).strftime("%Y-%m-%d")
        src = self.cfg.snapshots_dir / day
        if not src.is_dir():
            log.info("no snapshot dir for %s, skip", day)
            return None
        images = sorted(src.glob("*.jpg"))
        if len(images) < 2:
            log.info("not enough images for %s (%d)", day, len(images))
            return None

        out_dir = ensure_dir(self.cfg.timelapses_dir / "daily")
        out_path = out_dir / f"{day}.mp4"
        list_file = out_dir / f".{day}.list.txt"

        # Save first image as thumbnail
        thumb_dir = ensure_dir(self.cfg.thumbnails_dir / "timelapses")
        thumb_path = thumb_dir / f"{day}.jpg"
        if not thumb_path.exists():
            try:
                shutil.copy2(images[0], thumb_path)
            except Exception:
                log.exception("failed to save timelapse thumbnail")

        # Spread frames evenly across target_seconds; clamp to 2–25 fps
        frame_dur = target_seconds / len(images)
        frame_dur = max(1.0 / 25, min(0.5, frame_dur))

        list_file.write_text(
            "".join(f"file '{p.as_posix()}'\nduration {frame_dur:.5f}\n" for p in images)
            + f"file '{images[-1].as_posix()}'\n",
            encoding="utf-8",
        )

        result = run_ffmpeg(
            [
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-vsync", "vfr",
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "22",
                "-movflags", "+faststart",
                str(out_path),
            ],
            timeout=600,
        )
        list_file.unlink(missing_ok=True)
        if result.returncode != 0 or not out_path.exists():
            log.warning("daily timelapse failed: %s", result.stderr.strip()[:300])
            return None
        log.info("daily timelapse %s (%d frames)", out_path.name, len(images))
        return out_path

    def build_cumulative(self, fps: int = 30, max_per_day: int = 96) -> Optional[Path]:
        """Build a single video that walks through the entire nesting season.
        Sample up to max_per_day frames per day to keep the duration manageable."""
        out_path = self.cfg.timelapses_dir / "cumulative.mp4"
        list_file = self.cfg.timelapses_dir / ".cumulative.list.txt"
        day_dirs = sorted(d for d in self.cfg.snapshots_dir.iterdir() if d.is_dir())
        if not day_dirs:
            return None

        all_images: list[Path] = []
        for day_dir in day_dirs:
            images = sorted(day_dir.glob("*.jpg"))
            if not images:
                continue
            if len(images) <= max_per_day:
                all_images.extend(images)
            else:
                step = len(images) / max_per_day
                all_images.extend(images[int(i * step)] for i in range(max_per_day))

        if len(all_images) < 2:
            return None

        list_file.write_text(
            "".join(f"file '{p.as_posix()}'\nduration {1.0/fps:.5f}\n" for p in all_images)
            + f"file '{all_images[-1].as_posix()}'\n",
            encoding="utf-8",
        )
        ensure_dir(self.cfg.timelapses_dir)
        result = run_ffmpeg(
            [
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-vsync", "vfr",
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-movflags", "+faststart",
                str(out_path),
            ],
            timeout=1800,
        )
        list_file.unlink(missing_ok=True)
        if result.returncode != 0 or not out_path.exists():
            log.warning("cumulative timelapse failed: %s", result.stderr.strip()[:300])
            return None
        log.info("cumulative timelapse rebuilt (%d frames across %d days)", len(all_images), len(day_dirs))

        # Thumbnail: middle frame of the cumulative video
        thumb_dir = ensure_dir(self.cfg.thumbnails_dir / "timelapses")
        thumb_path = thumb_dir / "cumulative.jpg"
        mid = all_images[len(all_images) // 2]
        try:
            shutil.copy2(mid, thumb_path)
        except Exception:
            log.exception("failed to save cumulative timelapse thumbnail")

        return out_path

    def build_all_missing_daily(self):
        """Check all snapshot directories and build daily timelapses for any missing days."""
        if not self.cfg.snapshots_dir.exists():
            return
        days = sorted([d.name for d in self.cfg.snapshots_dir.iterdir() if d.is_dir()])
        for day in days:
            # Skip today, it's still being captured
            if day == today_str():
                continue
            out_path = self.cfg.timelapses_dir / "daily" / f"{day}.mp4"
            if not out_path.exists():
                log.info("building missing daily timelapse for %s", day)
                self.build_daily(day)

