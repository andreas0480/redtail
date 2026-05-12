# Setup & Deployment

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose | Tested on Debian 12 |
| UniFi Protect | Camera must have RTSP enabled |
| Google AI Studio key | Free tier works; Gemini 2.5 Flash is recommended |
| (Optional) ntfy topic | For push notifications |
| (Optional) SMB share | For data persistence across container restarts |

## 1. Clone and configure

```bash
git clone https://github.com/andreas0480/redtail.git
cd redtail
cp .env.example .env
$EDITOR .env
```

Fill in at minimum:

```
RTSP_URL=rtsps://<protect-host>:7441/<stream-token>
GEMINI_API_KEY=<your-key>
TZ=<your-timezone>
```

### Finding your RTSP URL

In UniFi Protect → Camera → Settings → RTSP → enable the stream and copy the URL.
Use `rtsps://` (plain RTSPS over TCP) rather than the `rtsp://` variant — stock `ffmpeg` 
cannot handle UniFi's SRTP extension.

### Data storage

By default the container writes to Docker volumes. To persist data on a Samba/NAS share,
fill in `SMB_HOST`, `SMB_SHARE`, and `SMB_SUBDIR`; the compose file will mount it as a 
CIFS volume. Leave them blank to use a local named volume instead.

## 2. Build and start

```bash
docker compose up -d --build
```

The dashboard is available at `http://<host>:8765`.

## 3. Verify

```bash
curl http://localhost:8765/health
```

A healthy response looks like:

```json
{"ok": true, "checks": [...]}
```

Check logs:

```bash
docker compose logs -f
```

Within a few minutes you should see snapshot and analysis log lines.

## 4. Update

Pull the new code, rebuild, and restart:

```bash
git pull
docker compose up -d --build
```

The SQLite database and all media are in the data volume and are not affected by rebuilds.

## Scheduled jobs

| Job | Schedule | What it does |
|---|---|---|
| Snapshot | Every 5 min (configurable) | Captures a JPEG from the RTSP stream |
| Analyze | Every 60 s | Runs Gemini vision on pending snapshots and clips |
| Daily timelapse | 00:10 every night | Builds per-day MP4 from the day's snapshots |
| Daily summary | 23:55 every night + every 3 h | Writes the journal entry for the current day |
| Health monitor | Every 2 min | Checks stream, disk, and DB; sends ntfy alert on failure |

## Ports

| Port | Service |
|---|---|
| 8765 | Dashboard (HTTP) |

## Running the review and deploy tools

`review.py` and `backfill/deploy.py` SSH into the host machine and operate on the running
container. They require a `REDTAIL_HOST` environment variable:

```bash
export REDTAIL_HOST=192.168.x.x   # IP or hostname of the Docker host
python review.py prepare --since 36 --out ./review_packet
```

## Environment variables reference

See [`.env.example`](../.env.example) for full documentation of every variable.
