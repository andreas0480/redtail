import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx

from .config import Config
from .db import Database
from .recorder import SegmentRecorder
from .util import now_iso

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "warning"  # warning | critical


class HealthMonitor:
    """
    Background poller that runs a set of checks periodically and fires ntfy
    alerts on transitions from ok→fail. Each check has its own cooldown so a
    sustained failure doesn't spam.
    """

    def __init__(self, cfg: Config, db: Database, recorder: SegmentRecorder):
        self.cfg = cfg
        self.db = db
        self.recorder = recorder
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: dict[str, bool] = {}
        self._last_alert: dict[str, float] = {}
        self._alert_cooldown_seconds = 3600  # at most one alert per check per hour
        self.last_run: Optional[str] = None
        self.last_results: list[CheckResult] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # Wait a bit for everything else to come up before first check
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("monitor loop crashed")
            self._stop.wait(120)

    def run_once(self) -> list[CheckResult]:
        results = [
            self._check_rtsp(),
            self._check_recorder(),
            self._check_recent_snapshot(),
            self._check_data_mount(),
            self._check_gemini_configured(),
        ]
        self.last_results = results
        self.last_run = now_iso()
        for r in results:
            self._handle_transition(r)
        return results

    def _check_rtsp(self) -> CheckResult:
        try:
            parsed = urlparse(self.cfg.rtsp_url)
            host = parsed.hostname
            port = parsed.port or 7441
            if not host:
                return CheckResult("rtsp_url", False, "invalid url", "critical")
            with socket.create_connection((host, port), timeout=5):
                return CheckResult("rtsp_url", True, f"{host}:{port} reachable")
        except Exception as e:
            return CheckResult("rtsp_url", False, str(e), "critical")

    def _check_recorder(self) -> CheckResult:
        if self.recorder.is_running:
            return CheckResult("recorder", True, f"restarts={self.recorder.restart_count}")
        return CheckResult("recorder", False, "ffmpeg recorder not running", "critical")

    def _check_recent_snapshot(self) -> CheckResult:
        row = self.db.latest_snapshot()
        if not row:
            return CheckResult("snapshot_freshness", False, "no snapshots yet", "warning")
        try:
            captured = datetime.fromisoformat(row["captured_at"])
        except ValueError:
            return CheckResult("snapshot_freshness", False, "unparseable timestamp", "warning")
        age = datetime.now().astimezone() - captured
        # Allow 2.5x the configured interval as healthy threshold
        if age > timedelta(seconds=self.cfg.snapshot_interval_seconds * 2.5):
            return CheckResult(
                "snapshot_freshness",
                False,
                f"last snapshot {int(age.total_seconds())}s ago",
                "critical",
            )
        return CheckResult("snapshot_freshness", True, f"{int(age.total_seconds())}s ago")

    def _check_data_mount(self) -> CheckResult:
        try:
            # Writable + has space
            probe = self.cfg.data_dir / ".healthcheck"
            probe.write_text(str(time.time()))
            probe.unlink(missing_ok=True)
            usage = subprocess.run(
                ["df", "-P", str(self.cfg.data_dir)],
                capture_output=True, text=True, timeout=5, check=False,
            )
            line = (usage.stdout.splitlines() or [""])[-1]
            return CheckResult("data_mount", True, line.strip())
        except Exception as e:
            return CheckResult("data_mount", False, str(e), "critical")

    def _check_gemini_configured(self) -> CheckResult:
        if self.cfg.gemini_api_key:
            return CheckResult("gemini_configured", True, "key present")
        return CheckResult("gemini_configured", False, "GEMINI_API_KEY missing", "warning")

    def _handle_transition(self, result: CheckResult) -> None:
        prev = self._state.get(result.name)
        self._state[result.name] = result.ok
        if result.ok:
            return
        if prev is False:
            # already failing; only re-alert after cooldown
            last = self._last_alert.get(result.name, 0)
            if time.time() - last < self._alert_cooldown_seconds:
                return
        self._fire_alert(result)

    def _fire_alert(self, result: CheckResult) -> None:
        msg = f"[{result.severity}] {result.name}: {result.detail}"
        log.warning("ALERT %s", msg)
        self.db.record_alert(now_iso(), result.name, result.severity, result.detail)
        self._last_alert[result.name] = time.time()
        send_ntfy(self.cfg, title=f"Redtail • {result.name}", message=result.detail, priority="high" if result.severity == "critical" else "default", tags=["warning"])


def _ascii_safe(s: str) -> str:
    """ntfy HTTP headers must be ASCII; encode non-ASCII via RFC 2047."""
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        from email.header import Header
        return Header(s, "utf-8").encode()


def send_ntfy(cfg: Config, title: str, message: str, priority: str = "default", tags: Optional[list[str]] = None) -> bool:
    if not cfg.ntfy_topic:
        return False
    url = f"{cfg.ntfy_server}/{cfg.ntfy_topic}"
    headers = {
        "Title": _ascii_safe(title),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        r = httpx.post(url, content=message.encode("utf-8"), headers=headers, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("ntfy publish failed: %s", e)
        return False
