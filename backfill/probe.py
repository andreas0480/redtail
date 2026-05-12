"""Probe UniFi Protect API surface so we know which endpoints to use in the backfill."""
import os
import sys
from pathlib import Path

import httpx

ENV_FILE = Path(__file__).parent / ".env.local"
for line in ENV_FILE.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

HOST = os.environ["UNIFI_HOST"]
KEY = os.environ["UNIFI_API_KEY"]

base = f"https://{HOST}"
client = httpx.Client(verify=False, headers={"X-API-KEY": KEY}, timeout=15)


def probe(method: str, path: str, **kwargs):
    url = base + path
    try:
        r = client.request(method, url, **kwargs)
        return r.status_code, r.headers.get("content-type", ""), r.text[:200]
    except Exception as e:
        return 0, "", f"error: {e}"


targets = [
    ("GET", "/proxy/protect/integrations/v1/meta/info"),
    ("GET", "/proxy/protect/integrations/v1/cameras"),
    ("GET", "/proxy/protect/integrations/v1/nvrs"),
    ("GET", "/proxy/protect/integrations/v1/subscribe/events"),
    # Common camera-scoped video paths (need a camera id, but we just want to see 404 vs 401)
    ("GET", "/proxy/protect/integrations/v1/cameras/_test/snapshot"),
    ("GET", "/proxy/protect/integrations/v1/cameras/_test/video-export"),
    # Internal API (works if the key is accepted there)
    ("GET", "/proxy/protect/api/cameras"),
]

print(f"Probing {HOST} with key {KEY[:8]}...\n")
for method, path in targets:
    code, ct, body = probe(method, path)
    marker = "OK " if code < 400 else "  "
    print(f"  {marker} {code:>3}  {method} {path}")
    if code in (200, 401, 403, 404, 405):
        snippet = body.replace("\n", " ")[:120]
        print(f"        ↳ {snippet}")
