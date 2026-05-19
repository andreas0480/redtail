"""Motion detection + clip extraction.

Runs a low-resolution, low-FPS parse of the live RTSP stream with ffmpeg's
`select` and `metadata` filters. Each frame whose scene-change score
crosses `MOTION_SCENE_THRESHOLD` is treated as a trigger. The detector
then pulls the relevant segments out of the recorder's rolling buffer
(pre-roll plus one post-roll segment), concatenates them with the
`concat` demuxer (no re-encode), and records a `clips` row.

A cooldown between triggers (default 120 s) keeps a single sustained
event from producing dozens of overlapping clips.
"""

import logging
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import Config
from .db import Database
from .recorder import SegmentRecorder
from .util import ensure_dir, now_iso, run_ffmpeg

log = logging.getLogger(__name__)

_SCENE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


class MotionDetector:
    """
    Runs a low-FPS ffmpeg parse of the RTSP stream with the showinfo filter to
    detect scene-change events. On a positive trigger it pulls the most recent
    segments out of the recorder's buffer and concatenates them into a clip.
    """

    def __init__(self, cfg: Config, db: Database, recorder: SegmentRecorder):
        self.cfg = cfg
        self.db = db
        self.recorder = recorder
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.proc: Optional[subprocess.Popen] = None
        self.last_trigger: float = 0.0
        self.trigger_count = 0

    def start(self) -> None:
        ensure_dir(self.cfg.clips_dir)
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="motion")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    def _run_loop(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            try:
                self._spawn_detector()
                if not self.proc or not self.proc.stderr:
                    raise RuntimeError("ffmpeg stderr unavailable")
                for line in self.proc.stderr:
                    if self._stop.is_set():
                        break
                    m = _SCENE_RE.search(line)
                    if not m:
                        continue
                    try:
                        score = float(m.group(1))
                    except ValueError:
                        continue
                    # The select filter has already gated on motion_scene_threshold,
                    # so anything that reaches us has crossed the bar.
                    self._on_trigger(score)
                rc = self.proc.wait() if self.proc else -1
                if self._stop.is_set():
                    return
                log.warning("motion detector exited rc=%s, restarting", rc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                log.exception("motion loop error: %s", e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                backoff = 2

    def _spawn_detector(self) -> None:
        # 2 fps downsample for cheap scene scoring; copy nothing, write nothing.
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "info",
            "-rtsp_transport", "tcp",
            "-i", self.cfg.rtsp_url,
            "-vf", f"fps=2,scale=320:-2,select='gte(scene\\,{self.cfg.motion_scene_threshold})',metadata=print",
            "-f", "null",
            "-",
        ]
        log.info("starting motion detector (threshold=%.3f)", self.cfg.motion_scene_threshold)
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def _on_trigger(self, score: float) -> None:
        now_ts = time.time()
        if now_ts - self.last_trigger < self.cfg.motion_cooldown_seconds:
            return
        self.last_trigger = now_ts
        self.trigger_count += 1
        log.info("motion triggered score=%.3f", score)
        threading.Thread(
            target=self._extract_clip, args=(score,), daemon=True, name="clip-extract"
        ).start()

    def _extract_clip(self, score: float) -> None:
        """Take the last 2 segments (pre-roll + trigger), wait for one more
        post-roll segment, then concat them all into a single mp4 clip."""
        try:
            pre_segments = self.recorder.list_segments()[-2:]
            if not pre_segments:
                log.warning("motion triggered but recorder buffer empty")
                return

            # Wait one segment for post-roll content to land
            time.sleep(self.cfg.buffer_segment_seconds + 2)
            all_segments = self.recorder.list_segments()
            # Take whatever segments arrived after the pre-roll set
            kept_names = {p.name for p in pre_segments}
            post_segments = [p for p in all_segments if p.name not in kept_names][:2]
            segments = pre_segments + post_segments
            if not segments:
                return

            now = datetime.now().astimezone()
            day_dir = ensure_dir(self.cfg.clips_dir / now.strftime("%Y-%m-%d"))
            out_path = day_dir / now.strftime("%H%M%S.mp4")
            tmp_dir = ensure_dir(self.cfg.buffer_dir / ".concat")

            # Copy segments to a stable spot first because the buffer is rolling
            # underneath us. Concat demuxer needs files that won't vanish.
            staged: list[Path] = []
            for i, seg in enumerate(segments):
                try:
                    target = tmp_dir / f"stage-{now.strftime('%H%M%S')}-{i:02d}.ts"
                    shutil.copy2(seg, target)
                    staged.append(target)
                except FileNotFoundError:
                    continue
            if not staged:
                return

            list_file = tmp_dir / f"list-{now.strftime('%H%M%S')}.txt"
            list_file.write_text(
                "".join(f"file '{p.as_posix()}'\n" for p in staged), encoding="utf-8"
            )

            result = run_ffmpeg(
                [
                    "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(list_file),
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(out_path),
                ],
                timeout=120,
            )

            for f in staged:
                f.unlink(missing_ok=True)
            list_file.unlink(missing_ok=True)

            if result.returncode != 0 or not out_path.exists():
                log.warning(
                    "clip mux failed rc=%s err=%s",
                    result.returncode,
                    result.stderr.strip()[:300],
                )
                return

            duration = self._probe_duration(out_path)
            self.db.record_clip(now_iso(), duration, str(out_path), f"motion:{score:.3f}")
            log.info("clip saved %s (%.1fs)", out_path.name, duration)
        except Exception:
            log.exception("clip extract failed")

    def _probe_duration(self, path: Path) -> float:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=nokey=1:noprint_wrappers=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return float(result.stdout.strip() or 0)
        except Exception:
            return 0.0
