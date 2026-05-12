# Redtail — Nest Box Monitor

An AI-powered monitoring system for a Common Redstart (*Phoenicurus phoenicurus*) nest box.
Captures snapshots and motion clips from a UniFi Protect camera, classifies each frame with
Google Gemini vision, and presents everything in a self-hosted dashboard with a daily
field-journal narrative.

---

## Features

- **Live snapshot feed** — captures a JPEG every 5 minutes via RTSP for a full timelapse record
- **Motion detection** — frame-diff trigger clips pre-buffered RTSP footage the moment anything stirs in the box
- **Gemini vision analysis** — every snapshot and clip is classified with an `event_type` (e.g. `incubating`, `eggs_visible`, `adult_arrives`) and a one-sentence narrative
- **Daily journal** — Gemini writes a warm, factual 3-5 sentence journal entry from each day's events, updated every 3 hours
- **Daily & cumulative timelapses** — per-day H.264 MP4s built at midnight; a season-wide cumulative film updated nightly
- **Health monitoring** — checks RTSP stream, disk space, and DB activity every 2 min; pushes alerts via [ntfy](https://ntfy.sh)
- **Historical backfill** — tools to import the full UniFi Protect recording history retroactively

## Dashboard pages

| Page | Description |
|---|---|
| **Home** | Latest snapshot, recent event log, today's journal summary |
| **Clips** | Motion-triggered video gallery, grouped by day, labeled and narrated by AI |
| **Journal** | Day-by-day narrative entries with embedded daily timelapse |
| **Timelapse** | Full-season cumulative video |

## Tech stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.12, Docker |
| Web framework | FastAPI + Jinja2 + HTMX |
| AI | Google Gemini 2.5 Flash (vision) |
| Video | ffmpeg |
| Database | SQLite (WAL mode) |
| Notifications | ntfy |

## Architecture

```
UniFi Protect RTSP
      │
      ├──► capture.py  ──► snapshots/  ──┐
      │                                  │
      └──► recorder.py ──► motion.py    ─┤──► events.db ──► analyzer.py (Gemini)
                           (clips/)      │                         │
                                         └─────────────────────────┘
                                                                    │
                                               FastAPI dashboard ◄──┘
```

See [`docs/architecture.md`](docs/architecture.md) for a detailed breakdown.

## Quick start

```bash
git clone https://github.com/belitz/redtail.git
cd redtail
cp .env.example .env
# Edit .env — at minimum set RTSP_URL, GEMINI_API_KEY, and TZ
docker compose up -d --build
```

Dashboard: `http://localhost:8765`  
Full setup guide: [`docs/setup.md`](docs/setup.md)

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and edit.

| Variable | Description |
|---|---|
| `RTSP_URL` | UniFi Protect RTSPS URL (`rtsps://host:7441/token`) |
| `GEMINI_API_KEY` | Google AI Studio API key |
| `TZ` | Timezone for timestamps and timelapse labels |
| `SMB_HOST` / `SMB_SHARE` / `SMB_SUBDIR` | NAS mount for data persistence (optional) |
| `NTFY_TOPIC` | Push notification topic (optional) |
| `SNAPSHOT_INTERVAL_SECONDS` | Capture cadence, default 300 |
| `MOTION_SCENE_THRESHOLD` | Frame-diff sensitivity, default 0.02 |

## Historical backfill

If the system was deployed after nesting started, the backfill tools can import the full
UniFi Protect recording history. See [`docs/backfill.md`](docs/backfill.md).

## Project context

Built to document a Common Redstart (*rödstjärt*) nesting in a garden nest box in Sweden
during the 2026 season. The species is a small insectivorous passerine — the Gemini prompts
are carefully tuned to the species, distinguishing pre-laying nest preparation from true
incubation, and tracking the clutch day by day.

## License

MIT
