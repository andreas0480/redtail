# Setup and deployment

This guide walks from a clean Debian box to a running dashboard at
`http://<host>:8765`. Assumes a Linux host; macOS and Windows hosts will work
under Docker Desktop with minor path-mount adjustments.

## Prerequisites

| Requirement | Tested with | Notes |
|---|---|---|
| Linux host | Debian 12 (bookworm) | Any distro with Docker support works |
| Docker Engine | 26.x | Docker Compose v2 (`docker compose`, not `docker-compose`) |
| UniFi Protect | 4.x | Camera must have the RTSPS stream enabled |
| Google AI Studio account | n/a | Free tier covers this workload comfortably |
| (Optional) NVIDIA GPU + driver | RTX 4070, CUDA 12.1 | Only needed for the narrator; not the main service |
| (Optional) ntfy account / self-host | ntfy.sh | Any topic works; recommended for unattended deploys |
| (Optional) SMB share | Samba 4.x | If you want media on a NAS instead of a local Docker volume |
| (Optional) Cloudflare account | Free plan | Required for `cloudflared` tunnel to reach a public hostname |

The production system runs comfortably on a 4-core/8 GB Debian box. Disk
usage grows by ~150 MB/day with the default snapshot cadence — most of that
is the timelapse source frames.

## 1. Clone and configure

```bash
git clone https://github.com/andreas0480/redtail.git
cd redtail
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

At minimum, set:

```env
RTSP_URL=rtsps://<protect-host>:7441/<stream-token>
GEMINI_API_KEY=<your-google-ai-studio-key>
TZ=Europe/Stockholm
```

### Finding the RTSP URL

In the UniFi Protect web UI:

1. *Cameras* → select your camera → *Settings* → *Manage*.
2. Find *RTSPS* in the stream list, toggle it on if necessary, and click the
   stream you want (low-quality is fine for a nest box).
3. Copy the URL. It looks like `rtsps://192.168.1.1:7441/abc123XYZ`.

Use `rtsps://` (RTSPS over TCP). Stock ffmpeg cannot handle UniFi's optional
SRTP extension — there is no GitHub issue to file, it's a deliberate ffmpeg
upstream decision. The TCP variant works without issue.

### Storage choice

By default, Docker manages two named volumes:

- `redtail_data` — snapshots, clips, timelapses, thumbnails, narrations
- `redtail_state` — `events.db`, rolling segment buffer

For local-only deployments leave `SMB_*` blank. To put `/data` on a NAS so
the media survives a host reinstall, fill in:

```env
SMB_HOST=<nas-ip>
SMB_SHARE=<share-name>
SMB_SUBDIR=redtail
```

The compose file will mount the share via CIFS as `redtail_data`. The state
volume always stays local — SQLite + WAL over CIFS is asking for trouble.

## 2. Build and start

```bash
docker compose up -d --build
```

Expect ~3 minutes for the first build (ffmpeg, ML libs, fonts). Subsequent
rebuilds are fast (only the `app/` layer changes).

## 3. Verify

```bash
curl http://localhost:8765/health
```

A healthy response:

```json
{
  "ok": true,
  "checked_at": "2026-05-19T10:32:11+02:00",
  "checks": [
    {"name": "rtsp",     "ok": true, "severity": "critical"},
    {"name": "disk",     "ok": true, "severity": "warn"},
    {"name": "recorder", "ok": true, "severity": "critical"},
    {"name": "db",       "ok": true, "severity": "warn"}
  ]
}
```

```bash
docker compose logs -f redtail
```

Within a minute or two you should see:

```
redtail: redtail starting up
app.recorder: starting recorder: ffmpeg → /state/buffer/seg-%05d.ts
app.motion: starting motion detector (threshold=0.020)
analyzer: critic configured: provider=gemini model=gemini-2.5-flash
app.capture: snapshot 103000.jpg (37 KB)
app.analyzer: snap 103000.jpg → eggs_visible (0.95)
```

If you see `RTSP_URL` errors, double-check the URL is reachable from inside
the container (it shares the host's network namespace by default? No —
Docker Compose puts the container on its own bridge network; make sure your
RTSP host is reachable from that bridge).

Open the dashboard in a browser: `http://localhost:8765`.

## 4. Update

Standard pull-and-rebuild:

```bash
git pull
docker compose up -d --build
```

The database and media volumes are unaffected by a rebuild. Forward-only
schema migrations are applied automatically on startup; no manual SQL needed.

## Scheduled jobs

In active mode, these run continuously:

| Job | Schedule | Purpose |
|---|---|---|
| Snapshot | every 5 min | One JPEG via RTSP → `snapshots` table |
| Analyze | every 60 s | Pull pending snapshots/clips, run Gemini, write events |
| Daily timelapse | 00:10 | Build each previous day's H.264 timelapse, then the cumulative |
| Daily summary (final) | 23:55 | Write the day's journal entry from its events |
| Daily summary (refresh) | every 3 h | Re-write today's entry as the day progresses |
| Health monitor | every 2 min | RTSP/disk/recorder/DB checks; ntfy on transition |

In watch-only mode (`WATCH_ONLY=1`), only one job runs: the noon nest check
(`watcher.daily_nest_check`).

## Network requirements

Outbound only — no inbound ports needed (the dashboard reaches the world via
the Cloudflare tunnel if you choose to set that up):

| Destination | Port | Purpose |
|---|---|---|
| Your UniFi Protect host | 7441/tcp | RTSPS stream |
| `generativelanguage.googleapis.com` | 443/tcp | Gemini API |
| `ntfy.sh` (or your own ntfy server) | 443/tcp | Health/change notifications |
| (Optional) `api.cloudflare.com` | 443/tcp | If using `cloudflared` |
| (Optional) `api.anthropic.com` | 443/tcp | If `CRITIC_PROVIDER=anthropic` |
| (Optional) `api.openai.com` | 443/tcp | If `CRITIC_PROVIDER=openai` |

## Cloudflare Tunnel (optional public hostname)

To expose the dashboard at e.g. `redtail.example.com` without opening any
inbound port:

```bash
# On the host, after cloudflared is installed
cloudflared tunnel route dns <your-tunnel-id> redtail.example.com
```

Then add the hostname to `/etc/cloudflared/config.yml`:

```yaml
ingress:
  - hostname: redtail.example.com
    service: http://localhost:8765
  - service: http_status:404
```

Reload (not restart — cloudflared needs a full restart to re-read config):

```bash
sudo systemctl restart cloudflared
```

If your DNS zone is managed by a Cloudflare account different from the one
the tunnel cert belongs to, `cloudflared tunnel route dns` will silently add
the CNAME to the wrong zone. Verify with `dig redtail.example.com CNAME` —
the answer should look like `<id>.cfargotunnel.com`. If it doesn't, add the
CNAME manually in the correct zone:

```
Type:   CNAME
Name:   redtail
Target: <your-tunnel-id>.cfargotunnel.com
Proxy:  ✅ (orange cloud)
```

## Operating the system from another machine

`backfill/deploy.py` and `review.py` SSH into the production host and operate
on the container. They both require `REDTAIL_HOST` pointed at the host:

```bash
export REDTAIL_HOST=192.168.30.103
python review.py prepare --since 36 --out ./review_packet
```

The host's user must have password-less SSH (`~/.ssh/authorized_keys`) and
permission to `docker exec redtail`.

## Switching to watch-only mode

When the active nesting is over and you want to keep the dashboard online
without burning API quota or recording motion:

```bash
echo 'WATCH_ONLY=1' >> .env
docker compose up -d
```

This pauses everything except a 12:00-daily nest check that pings ntfy with
"no change" or escalates on the first frame that doesn't match the
abandoned-but-intact baseline. The Today page also surfaces a "Season closed"
banner explaining the context to anyone visiting the dashboard. See the
[runbook](runbook.md) for the
reverse procedure.

## Environment variables — complete reference

| Variable | Default | Purpose |
|---|---|---|
| `RTSP_URL` | (required) | UniFi Protect RTSPS URL |
| `DASHBOARD_PORT` | `8765` | Host port to bind the dashboard on |
| `TZ` | `UTC` | Timezone used for local-time conversions in the journal |
| `GEMINI_API_KEY` | (required for AI) | Google AI Studio key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Primary classifier model |
| `CRITIC_PROVIDER` | `gemini` | `gemini` / `anthropic` / `openai` |
| `CRITIC_MODEL` | `${GEMINI_MODEL}` | Model id within the chosen provider |
| `ANTHROPIC_API_KEY` | — | Required if `CRITIC_PROVIDER=anthropic` |
| `OPENAI_API_KEY` | — | Required if `CRITIC_PROVIDER=openai` |
| `NTFY_SERVER` | `https://ntfy.sh` | Self-host or use the free public instance |
| `NTFY_TOPIC` | — | Unique topic; leave blank to disable notifications |
| `SMB_HOST` / `SMB_SHARE` / `SMB_SUBDIR` | — | NAS mount for the data volume |
| `SNAPSHOT_INTERVAL_SECONDS` | `300` | Capture cadence |
| `BUFFER_SEGMENT_SECONDS` | `30` | RTSP buffer segment length |
| `BUFFER_KEEP_SEGMENTS` | `20` | Buffer ring size (× segment = pre-roll length) |
| `MOTION_SCENE_THRESHOLD` | `0.06` | ffmpeg scene-change score gate (0–1) |
| `MOTION_COOLDOWN_SECONDS` | `120` | Minimum gap between motion-triggered clips |
| `LOG_LEVEL` | `INFO` | Standard library `logging` level name |
| `WATCH_ONLY` | (empty) | Set `1` to pause pipeline; only the noon nest check runs |

A working `.env.example` lives at the repo root; everything above is also
documented inline there.
