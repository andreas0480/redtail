"""Tiny UniFi Protect internal-API client. Handles login, CSRF rotation,
session refresh, and the few endpoints we need for backfill."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterator, Optional

import httpx

log = logging.getLogger(__name__)


@dataclass
class Event:
    id: str
    type: str
    start_ms: int
    end_ms: int
    score: int
    camera: str
    smart_detect_types: list[str]


class UnifiClient:
    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False):
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.base = f"https://{self.host}"
        self.client = httpx.Client(
            verify=verify_ssl,
            timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10),
            follow_redirects=False,
        )
        self.csrf: Optional[str] = None
        self.last_login: float = 0.0
        self._login_ttl_seconds = 90 * 60  # re-login every 90 min; server expires at 2h

    # ---------- session ----------

    def login(self) -> None:
        url = f"{self.base}/api/auth/login"
        r = self.client.post(
            url,
            json={"username": self.username, "password": self.password},
        )
        if r.status_code != 200:
            raise RuntimeError(f"login failed: HTTP {r.status_code} {r.text[:200]}")
        self.csrf = r.headers.get("x-csrf-token") or r.headers.get("X-Csrf-Token")
        self.last_login = time.time()
        log.info("logged in to %s as %s", self.host, self.username)

    def _ensure_session(self) -> None:
        if self.csrf is None or (time.time() - self.last_login) > self._login_ttl_seconds:
            self.login()

    def _headers(self) -> dict:
        h = {}
        if self.csrf:
            h["X-Csrf-Token"] = self.csrf
        return h

    def _update_csrf(self, response: httpx.Response) -> None:
        new_csrf = response.headers.get("x-updated-csrf-token") or response.headers.get("X-Updated-Csrf-Token")
        if new_csrf:
            self.csrf = new_csrf

    def _get(self, path: str, **kwargs) -> httpx.Response:
        self._ensure_session()
        url = f"{self.base}{path}"
        for attempt in range(3):
            r = self.client.get(url, headers=self._headers(), **kwargs)
            self._update_csrf(r)
            if r.status_code == 401:
                self.login()
                continue
            if r.status_code == 429:
                wait = 2 ** attempt
                log.warning("429 from %s, sleeping %ds", path, wait)
                time.sleep(wait)
                continue
            return r
        return r  # last attempt

    # ---------- endpoints ----------

    def cameras(self) -> list[dict]:
        r = self._get("/proxy/protect/api/cameras")
        r.raise_for_status()
        return r.json()

    def camera(self, camera_id: str) -> dict:
        r = self._get(f"/proxy/protect/api/cameras/{camera_id}")
        r.raise_for_status()
        return r.json()

    def recording_window(self, camera_id: str) -> tuple[int, int]:
        info = self.camera(camera_id)
        v = info.get("stats", {}).get("video", {})
        start = v.get("recordingStart")
        end = v.get("recordingEnd")
        if start is None or end is None:
            raise RuntimeError(f"camera {camera_id} has no recording window")
        return int(start), int(end)

    def recording_snapshot(self, camera_id: str, ts_ms: int) -> Optional[bytes]:
        r = self._get(
            f"/proxy/protect/api/cameras/{camera_id}/recording-snapshot",
            params={"ts": ts_ms},
        )
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
            return r.content
        if r.status_code == 404:
            return None  # no footage at that timestamp
        log.warning("snapshot ts=%d HTTP %s ct=%s body=%s",
                    ts_ms, r.status_code, r.headers.get("content-type"), r.text[:120])
        return None

    def motion_events(self, camera_id: str, start_ms: int, end_ms: int, page_size: int = 1000) -> Iterator[Event]:
        """Yield motion events between start_ms and end_ms. The API caps responses;
        we paginate by walking the window in chunks if necessary."""
        # The UniFi events endpoint returns all matching events for the window. We
        # query in 24h slices to keep response sizes bounded and resumable.
        slice_size = 24 * 3600 * 1000
        cursor = start_ms
        while cursor < end_ms:
            slice_end = min(cursor + slice_size, end_ms)
            r = self._get(
                "/proxy/protect/api/events",
                params={
                    "cameras": camera_id,
                    "start": cursor,
                    "end": slice_end,
                    "types": "motion",
                    "limit": page_size,
                },
            )
            r.raise_for_status()
            for raw in r.json():
                if raw.get("end") is None:
                    continue  # in-progress event
                yield Event(
                    id=raw["id"],
                    type=raw["type"],
                    start_ms=int(raw["start"]),
                    end_ms=int(raw["end"]),
                    score=int(raw.get("score") or 0),
                    camera=raw.get("camera", camera_id),
                    smart_detect_types=raw.get("smartDetectTypes") or [],
                )
            cursor = slice_end

    def video_export(
        self,
        camera_id: str,
        start_ms: int,
        end_ms: int,
        channel: int = 0,
        out_path: Optional[str] = None,
    ) -> bytes:
        """Download MP4 for a time range. The API streams the encoded file."""
        self._ensure_session()
        url = f"{self.base}/proxy/protect/api/video/export"
        params = {
            "camera": camera_id,
            "start": start_ms,
            "end": end_ms,
            "channel": channel,
        }
        with self.client.stream("GET", url, params=params, headers=self._headers()) as r:
            self._update_csrf(r)
            r.raise_for_status()
            if out_path:
                with open(out_path, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
                return b""
            return r.read()
