import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .config import Config
from .util import ensure_dir

log = logging.getLogger(__name__)


class SegmentRecorder:
    """
    Runs ffmpeg continuously in the background, writing rolling segment files
    to the buffer directory. Older segments are pruned automatically.

    Each segment is BUFFER_SEGMENT_SECONDS long; we keep BUFFER_KEEP_SEGMENTS files.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pruner_thread: Optional[threading.Thread] = None
        self.last_started: Optional[float] = None
        self.restart_count = 0

    @property
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        ensure_dir(self.cfg.buffer_dir)
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="recorder")
        self._thread.start()
        self._pruner_thread = threading.Thread(target=self._prune_loop, daemon=True, name="buffer-pruner")
        self._pruner_thread.start()

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
                self._spawn_ffmpeg()
                self.last_started = time.time()
                _, stderr_bytes = self.proc.communicate() if self.proc else (None, b"")
                rc = self.proc.returncode if self.proc else -1
                if self._stop.is_set():
                    return
                tail = (stderr_bytes or b"").decode(errors="replace").strip().splitlines()[-5:]
                log.warning("recorder ffmpeg exited rc=%s, last stderr: %s", rc, " | ".join(tail))
                self.restart_count += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                log.exception("recorder loop error: %s", e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                backoff = 2

    def _spawn_ffmpeg(self) -> None:
        seg = self.cfg.buffer_segment_seconds
        keep = self.cfg.buffer_keep_segments
        pattern = str(self.cfg.buffer_dir / "seg-%05d.ts")
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", self.cfg.rtsp_url,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(seg),
            "-segment_wrap", str(keep),
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            "-strftime", "0",
            pattern,
        ]
        log.info("starting recorder: ffmpeg → %s", pattern)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def _prune_loop(self) -> None:
        """Belt-and-braces: also delete unexpectedly old segments outside the wrap window."""
        while not self._stop.is_set():
            try:
                max_age = self.cfg.buffer_segment_seconds * self.cfg.buffer_keep_segments * 2
                cutoff = time.time() - max_age
                for f in self.cfg.buffer_dir.glob("seg-*.ts"):
                    try:
                        if f.stat().st_mtime < cutoff:
                            f.unlink(missing_ok=True)
                    except FileNotFoundError:
                        pass
            except Exception as e:
                log.warning("buffer prune error: %s", e)
            self._stop.wait(60)

    def list_segments(self) -> list[Path]:
        return sorted(self.cfg.buffer_dir.glob("seg-*.ts"), key=lambda p: p.stat().st_mtime)
