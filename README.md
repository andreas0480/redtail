# Redtail

**A small, opinionated nest-box monitoring system.** Redtail watches a single
camera inside a garden nest box, classifies every frame and every motion clip
with a vision model, writes a daily naturalist field journal that reads aloud
in a documentary-narrator voice, and ships the whole thing as a self-hosted
dashboard at `https://redtail.belitz.se`.

It was built over a few weekends to document a Common Redstart
(*Phoenicurus phoenicurus*) nesting attempt in Sweden during the 2026 season.
The bird is gone for the year, but the system is still online — running in a
minimal *watch-only* mode that alerts if anything changes.

---

## What it does

- **Captures** a JPEG snapshot every five minutes from a UniFi Protect camera
  via RTSPS, and a motion clip whenever the live frame-difference score crosses
  a configurable threshold. Motion clips include 60 seconds of pre-roll from a
  rolling buffer.
- **Classifies** every snapshot and clip with Google Gemini 2.5 Flash. The
  output is a structured event with a type (`incubating`, `eggs_visible`,
  `adult_arrives`, etc.), a confidence score, a one-sentence narrative, and
  subject counts (adults, eggs, chicks).
- **Writes a daily journal entry** every three hours (and once at 23:55) that
  condenses the day's events into a warm 3–5 sentence naturalist field-log,
  followed by a 2–3 sentence biological footnote that explains the science of
  what was observed. A second LLM pass (provider-agnostic — Gemini, Claude,
  or GPT) reviews each entry against a catalogue of failure modes and either
  approves or rewrites it.
- **Narrates** every journal entry as an MP3 in a Sir-David-Attenborough-style
  voice, generated on a separate GPU machine via Coqui XTTS-v2 fine-tuned on
  audiobook audio.
- **Builds timelapses** — a ~30 second daily film, plus a cumulative
  season-wide reel rebuilt every night.
- **Monitors its own health** — RTSP, disk, DB liveness — and pushes ntfy
  notifications when anything degrades. A single noon nest-check job in
  watch-only mode pings *"no change"* daily and escalates to *"change at the
  nest!"* the moment the AI sees anything other than the abandoned baseline.

## What it looks like

The dashboard is intentionally low-chrome: warm paper tones, serif headlines,
a single nest-box illustration. Five pages:

| Page | What's on it |
|---|---|
| **Today** | Latest snapshot, scrolling event log, today's journal entry |
| **Journal** | Day-by-day entries, each with its own daily timelapse, biological footnote, and audio player |
| **Clips** | Motion clips grouped by day and 6-hour period (night/morning/afternoon/evening), collapsed by default |
| **Timelapse** | Cumulative season-wide video with a poster thumbnail |
| **Species** | A 4,000-word naturalist profile of the Common Redstart with CC-licensed photography and a 10-minute audio reading |

## Architecture at a glance

```
┌────────────────── Production host (Docker, .30.103) ──────────────────────┐
│                                                                            │
│  UniFi Protect ──RTSPS──┬──► capture.py   (5-min snapshots) ──┐            │
│                         │                                      ▼            │
│                         ├──► recorder.py  (rolling segment buffer)         │
│                         │                                      ▼            │
│                         └──► motion.py    (scene-diff trigger) ┘            │
│                                                  │                          │
│                                                  ▼                          │
│              ┌──────────────────── events.db (SQLite) ─────────────────┐   │
│              │  snapshots │ clips │ events │ daily_summaries │ alerts  │   │
│              └─────────────────────────┬────────────────────────────────┘   │
│                                        │                                    │
│  analyzer.py ◄──── pending rows ───────┤                                    │
│   ├─ Gemini vision → events                                                 │
│   ├─ Gemini summary → daily journal entry                                   │
│   └─ Critic pass (Gemini / Claude / GPT) → approved entry                   │
│                                                                            │
│  timelapse.py     monitor.py     watcher.py                                │
│   nightly H.264   ntfy alerts    daily noon check (watch-only mode)        │
│                                                                            │
│  FastAPI + Jinja2 + HTMX dashboard  ◄── /journal /clips /species /...      │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                        ▲
                                        │  daily 00:30 cron over SSH
                                        │
┌────────────────── GPU machine (NVIDIA, .10.84) ────────────────────────────┐
│  narrator/                                                                  │
│   ├─ Coqui XTTS-v2 fine-tuned on Attenborough audio                        │
│   ├─ Pulls pending summaries, synthesizes per-day MP3                      │
│   └─ Pushes MP3 + DB update back over SSH                                  │
└────────────────────────────────────────────────────────────────────────────┘
```

Deep architecture write-up: [`docs/architecture.md`](docs/architecture.md).

## Tech stack and rationale

| Layer | Choice | Why |
|---|---|---|
| Runtime | Python 3.12, Docker Compose | Single-file deploy, no orchestrator needed for a one-camera system |
| Web | FastAPI + Jinja2 + HTMX | Server-rendered HTML with surgical updates; no SPA to maintain |
| AI (vision) | Google Gemini 2.5 Flash | Cheap, fast, excellent at the bird-naïve naturalist prompt |
| AI (critic) | Provider-agnostic (Gemini / Claude / OpenAI) | Lets a stronger model review the cheap model's output |
| AI (TTS) | Coqui XTTS-v2 fine-tune | Self-hosted, GPU-accelerated, no per-character cost |
| Video | ffmpeg only | Concat demuxer for clip stitching, no re-encode |
| Database | SQLite + WAL | Single-writer workload; backup is `cp` |
| Notifications | [ntfy](https://ntfy.sh) | Push to phone with one HTTPS POST |
| Edge | Cloudflare Tunnel | Public hostname without opening a port |

## Quick start

```bash
git clone https://github.com/andreas0480/redtail.git
cd redtail
cp .env.example .env
$EDITOR .env                    # set RTSP_URL, GEMINI_API_KEY, TZ at minimum
docker compose up -d --build
```

Dashboard: `http://localhost:8765` (or whatever `DASHBOARD_PORT` you chose).

Full guides:

- [Setup & deployment](docs/setup.md) — prerequisites, configuration reference, first-run checklist
- [Architecture](docs/architecture.md) — system design, data flow, schema, operating modes
- [AI pipeline](docs/ai-pipeline.md) — every prompt explained, critic design, multi-provider
- [Operations runbook](docs/runbook.md) — common scenarios with copy-pasteable commands
- [Historical backfill](docs/backfill.md) — importing UniFi Protect archive
- [Attenborough narrator](narrator/README.md) — self-hosted XTTS-v2 TTS subsystem

## Project status

The 2026 nesting attempt ended in abandonment on 13 May after a complete clutch
of five sky-blue eggs. The system has been running in **watch-only mode** since
18 May: dashboard, journal, narrations, clips and timelapses are all still
accessible, but capture/motion/recorder/analyzer/health-monitor have been
paused. A single Gemini classification at 12:00 local fires daily and pushes a
ntfy ping if anything changes. If a second nesting attempt begins the same box
the watcher will detect it and surface immediately.

## License

[MIT](LICENSE). The bundled Attenborough XTTS fine-tune is non-commercial only
(Coqui Public Model License) and uses an unauthorized voice clone — see
[`narrator/README.md`](narrator/README.md) for the ethical caveat.
