# Architecture

## Problem statement

Document the breeding cycle of a single Common Redstart pair in a garden nest
box over the course of one nesting season, in a way that:

- Captures both passive (timelapse) and event-driven (motion clips) media.
- Produces a *human-readable* narrative without manual review of every frame.
- Keeps running unattended for weeks, on hardware the author already owns,
  with a near-zero recurring cost.
- Survives the hard parts of camera-system operation: dropped RTSP
  connections, full disks, container restarts, third-party API outages.

## Constraints

| Constraint | Implication |
|---|---|
| Single camera, single nest box | No HA, no clustering, no message bus |
| Owner-only audience initially | Edge served behind Cloudflare Tunnel rather than a public-facing load balancer |
| Hobby budget | Gemini Flash, free ntfy tier, self-hosted TTS — keep cloud spend under €1/month |
| Author's existing hardware | Production runs on a Debian 12 home server (.30.103); GPU work on a workstation (.10.84) over SSH |
| Bird is gone after fledging / abandonment | Must support a no-traffic *watch-only* mode that pauses everything but still alerts on change |

## Top-level architecture

```
┌────────────────── Production host (Docker, .30.103) ───────────────────────┐
│                                                                            │
│  UniFi Protect ──RTSPS──┬──► capture.py   (5-min snapshots) ──┐            │
│                         │                                      ▼            │
│                         ├──► recorder.py  (rolling segment buffer)         │
│                         │                                      ▼            │
│                         └──► motion.py    (scene-diff trigger) ┘            │
│                                                  │                          │
│                                                  ▼                          │
│              ┌──────────────── events.db (SQLite, WAL) ─────────────────┐  │
│              │  snapshots │ clips │ events │ daily_summaries │ alerts   │  │
│              └────────────────────┬────────────────────────────────────┘   │
│                                   │                                         │
│  analyzer.py ◄──── pending rows ──┤                                         │
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
│   ├─ Coqui XTTS-v2 fine-tune (bring your own checkpoint)                   │
│   ├─ Pulls pending summaries, synthesizes per-day MP3                      │
│   └─ Pushes MP3 + DB update back over SSH                                  │
└────────────────────────────────────────────────────────────────────────────┘
```

## Component responsibilities

Each module has a single job. They communicate through the SQLite database
(for state) and a single shared `Config` dataclass (for settings); there is no
in-process message bus.

| Module | Responsibility | Lives in |
|---|---|---|
| `app.config` | Load and freeze runtime settings from environment | a dataclass |
| `app.db` | Schema, migrations, every SQL statement | a class |
| `app.capture` | One RTSP snapshot per call → `snapshots` table | a function |
| `app.recorder` | Continuous rolling segment buffer (ffmpeg) | a long-running thread |
| `app.motion` | Scene-diff detection → assemble & store clip | a long-running thread |
| `app.analyzer` | Per-frame / per-clip Gemini calls; daily summary + critic | a class |
| `app.timelapse` | Daily and cumulative H.264 builders | a class |
| `app.monitor` | Health probes + ntfy notifications | a long-running thread |
| `app.watcher` | Single daily nest-check, alert on change (watch-only mode) | a function |
| `app.main` | FastAPI app, scheduler, lifecycle, routes | the entry point |

### `capture.py`

Synchronous. Called by APScheduler every `SNAPSHOT_INTERVAL_SECONDS` (default
300). One ffmpeg invocation with `-frames:v 1` against the RTSP stream;
output goes to `/data/snapshots/<YYYY-MM-DD>/<HHMMSS>.jpg`. A row in
`snapshots` is inserted only if the file ended up >1 KB (an undersized JPEG
usually means the RTSP handshake failed mid-frame). The analyzer picks the
row up on its next tick.

### `recorder.py`

Background thread that owns one long-running ffmpeg process. ffmpeg is
configured with `-c copy -f segment -segment_time 30 -segment_wrap 20` so it
writes a ring of 20 fixed-duration MPEG-TS files to
`/state/buffer/seg-NNNNN.ts`. The thread's only real job is to supervise the
process: restart it with exponential back-off if it exits unexpectedly, kill
it cleanly on shutdown. Because ffmpeg handles segment rotation, there is no
janitor thread or cron sweep.

### `motion.py`

Background thread running a second ffmpeg in parallel with the recorder.
This one re-decodes the stream at 2 fps and 320 px wide, then applies a
filter chain:

```
fps=2,scale=320:-2,select='gte(scene\,THRESHOLD)',metadata=print
```

The `select` filter emits only frames whose computed scene-change score
exceeds the threshold. The `metadata=print` filter writes
`lavfi.scene_score=X.XXX` to stderr for each surviving frame. A single regex
parses those lines; each match calls `_on_trigger(score)`, which honors a
configurable cooldown (default 120 s) to prevent a single sustained event
from producing dozens of overlapping clips.

When a clip is triggered, the detector stages the last 2 buffer segments
(pre-roll), waits one segment-time for post-roll, then concatenates
everything with ffmpeg's `concat` demuxer in copy mode — no re-encode.

> **Lesson learned.** This module used `showinfo` originally and parsed
> `scene_score=` from its output. ffmpeg 7.x dropped that field from
> `showinfo`, which silently broke live motion detection until I noticed
> the only clips in the DB came from the UniFi backfill, never from
> live capture. `metadata=print` is the supported way to surface filter
> metadata in current ffmpeg.

### `analyzer.py`

The largest module by far, and the only one that talks to language models.
Three flows, all triggered by APScheduler:

1. **`process_pending()`** — every 60 seconds. Pulls up to 8 unanalyzed
   snapshots and 4 unanalyzed clips, classifies each with Gemini, writes an
   `events` row, and marks the source row analyzed.

2. **`daily_summary(day)`** — at 23:55 and every 3 hours. Gathers the day's
   events, formats them as a bulleted list with local-time timestamps, asks
   Gemini for a 3–5 sentence journal entry, asks Gemini again for a 2–3
   sentence biological footnote, then routes both through `_critique_and_fix()`
   before storing.

3. **`_critique_and_fix(text, mode)`** — the **critic pass**. A second LLM
   call against an editor prompt that lists known failure modes (meta-AI
   commentary, timezone leaks, date headings, clock-time readings, etc.) and
   either approves the text verbatim or rewrites it. The provider is
   configurable (`CRITIC_PROVIDER` ∈ `gemini` | `anthropic` | `openai`); the
   SDKs are lazy-imported so unused providers cost nothing.

Every prompt is a module-level constant, version-controlled, and reviewable
in PRs.

### `timelapse.py`

Two builders, both invoked by the daily timelapse job.

- **`build_daily(day)`** — writes a concat list whose frame duration is
  `target_seconds / frame_count` (clamped to 2–25 fps). The output runtime
  is therefore roughly constant — about 30 seconds — regardless of how many
  snapshots that day produced. ffmpeg is run with `-vsync vfr -c:v libx264
  -preset veryfast -crf 22 -movflags +faststart`.

- **`build_cumulative()`** — samples up to `max_per_day` (default 96)
  evenly-spaced frames from every day in the snapshots directory and stitches
  them into a single season-wide film. A poster thumbnail is saved from the
  middle frame so the dashboard's `<video poster=…>` attribute has something
  to show before the user hits play.

### `monitor.py`

Background thread plus an APScheduler interval. Every 2 minutes it runs four
checks:

| Check | Verdict |
|---|---|
| RTSP TCP reachable | TCP connect to host:7441 with 5 s timeout |
| Free disk on `/data` | `shutil.disk_usage()`; thresholds: warn <5 GB, fail <1 GB |
| Recorder alive | The segment buffer's newest file is <90 s old |
| DB written recently | Latest `snapshots` row is <2 × snapshot interval old |

Each transition (OK → fail, fail → OK) emits a ntfy notification.
De-duplication against the `alerts` table prevents storm-style repeated pings
during a sustained failure.

### `watcher.py`

Watch-only mode's heartbeat. Captures one snapshot, runs `_analyze_snapshot()`
on it, and inspects the resulting event. If the event_type is one of
`{eggs_visible, empty, unknown}` it pings ntfy at minimum priority ("no
change"); anything else — `adult_present`, `incubating`, `intruder`, etc. —
is sent at high priority ("change at the nest!"). The dashboard, journal,
clips, narrations and species page all continue to work because they're
purely read-only.

### `main.py`

FastAPI app, APScheduler instance, request routes, and the lifespan handler
that wires the background components together. Two startup branches based on
`cfg.watch_only`: a full-pipeline mode (all components running, six scheduled
jobs) and a minimal mode (one scheduled job, nothing else running). The
shutdown handler is symmetric so containers can restart cleanly.

## Data flow

### Snapshot pipeline

```
APScheduler (every 5 min)
   ▼
capture.capture_snapshot()
   ├─ ffmpeg -frames:v 1 → /data/snapshots/<day>/<time>.jpg
   └─ db.record_snapshot()  → INSERT INTO snapshots (analyzed=0)
                                            │
APScheduler (every 60 s)                    │
   ▼                                        │
analyzer.process_pending() ◄────────────────┘
   ├─ db.pending_snapshots(limit=8)
   ├─ For each row:
   │   ├─ Gemini vision call (SNAPSHOT_PROMPT + JPEG bytes)
   │   ├─ Parse JSON response  →  event_type, confidence, narrative, subjects
   │   ├─ db.add_event()       →  INSERT INTO events
   │   └─ db.mark_snapshot_analyzed()
```

### Motion-clip pipeline

```
recorder thread                motion thread
    │                              │
    │  ffmpeg writes rolling       │  ffmpeg parses live stream at 2 fps
    │  segment buffer              │  ├─ select='gte(scene,THRESHOLD)'
    │  (20 × 30 s = 10 min)        │  └─ metadata=print
    │                              │     │
    │                              │     ▼  lavfi.scene_score=X.XXX
    │                              │  regex match  →  _on_trigger(score)
    │                              │     │
    │                              │     │  cooldown check
    │                              │     ▼
    │                              │  Stage last 2 segments + wait for post-roll
    │                              │     │
    │  ────────── shared ──────────►  ffmpeg concat (no re-encode)
    │              buffer dir         │
    │                                  ▼
    │                                /data/clips/<day>/<time>.mp4
    │                                db.record_clip()  →  INSERT INTO clips

(then the per-clip analyzer flow on next analyze tick: sample 4 frames,
 send to Gemini, write event, set label/thumbnail/keep)
```

### Daily summary pipeline

```
APScheduler (23:55 + every 3 h)
   ▼
analyzer.daily_summary(day)
   ├─ db.events_for_day(day)
   ├─ Convert event timestamps to local tz
   ├─ Build bulleted event list
   ├─ Gemini call (DAILY_PROMPT_TEMPLATE)            → summary
   ├─ _strip_heading(summary)                         (regex safety net)
   ├─ Critic pass: GEMINI/CLAUDE/GPT (CRITIC_PROMPT) → approved summary
   ├─ Gemini call (BIO_CONTEXT_PROMPT)               → bio_context
   ├─ Critic pass on bio_context                     → approved bio_context
   ├─ Pick featured snapshot for that day
   └─ db.upsert_daily_summary()  →  INSERT/REPLACE INTO daily_summaries
```

Deep dive on every prompt and the critic: [`ai-pipeline.md`](ai-pipeline.md).

## Database schema

```sql
snapshots       (id PK, captured_at, path UNIQUE, analyzed)
clips           (id PK, started_at, duration_seconds, path UNIQUE, trigger,
                 analyzed, keep, label, thumbnail_path)
events          (id PK, occurred_at, source, source_id, event_type,
                 confidence, narrative, raw_json)
daily_summaries (day PK, summary, events_count, timelapse_path,
                 featured_image_path, bio_context, narration_path,
                 created_at)
alerts          (id PK, fired_at, check_name, severity, message)
```

### Design notes

- **No foreign keys** between `events` and `snapshots`/`clips`. The `source`
  + `source_id` columns are a polymorphic association — Gemini's classification
  of a snapshot lives in the same table as its classification of a clip, which
  makes the event log render trivial. The cost is referential integrity has to
  be enforced in application code.

- **`analyzed` flag instead of an analysis queue.** Pending work is just
  `SELECT … WHERE analyzed = 0 ORDER BY captured_at ASC LIMIT N`. A partial
  index on `analyzed = 0` makes that O(pending), not O(total). No background
  worker, no Redis, no broker.

- **`raw_json` column on `events`.** Every Gemini response is stored verbatim.
  Subject counts and any non-canonical keys live there. This let me re-run
  retroactive corrections (the white-feather miscount episode, the date-prefix
  cleanup) without re-paying for the Gemini calls.

- **Forward-only migrations in `Database.__init__`.** A `PRAGMA table_info`
  check before `ALTER TABLE ADD COLUMN` lets existing deployments upgrade in
  place. The `bio_context` and `narration_path` columns were both added this
  way mid-season without any downtime.

- **`day TEXT PRIMARY KEY` in `daily_summaries`.** The format is fixed at
  `YYYY-MM-DD` and there is exactly one entry per local-tz day. Inserts use
  `INSERT … ON CONFLICT(day) DO UPDATE`, so the every-3-hours summary refresh
  is naturally idempotent.

## Cross-cutting concerns

### Persistence

- SQLite in WAL mode, single writer (this process), unlimited concurrent
  readers (the dashboard).
- The data volume (`/data`) holds media (JPEGs, MP4s, MP3s); the state
  volume (`/state`) holds the database and rolling segment buffer.
- Backup is `cp /state/events.db backups/events-$(date).db`. The media in
  `/data` is large but reproducible from the camera's own retention.

### Observability

- Stdout structured logs (`[%levelname] %name: %message`) via the standard
  `logging` module. Docker captures these; `docker logs -f redtail` is the
  primary observability surface.
- Health checks: `/health` returns JSON with each check's verdict.
  `/health/quick` is a 1-byte 200/503 for Docker's healthcheck.
- ntfy notifications for transitions, never spam: each `alerts` row is the
  state change that triggered a push.

### Scheduling

- APScheduler `BackgroundScheduler` with two trigger families:
  `IntervalTrigger` for the high-frequency jobs (snapshot, analyze, monitor,
  3-hourly summary) and `CronTrigger` for the wall-clock-anchored ones
  (00:10 timelapse, 23:55 final summary, 12:00 nest watch).
- `max_instances=1, coalesce=True` on all jobs — if a tick is missed (because
  the previous one is still running) the scheduler quietly skips rather than
  queueing.

### Security

- No public ports. The dashboard binds to `0.0.0.0:8765` inside Docker; the
  internet hits it via Cloudflare Tunnel at `redtail.belitz.se`. The tunnel
  is the only path in.
- API keys (Gemini, optionally Anthropic/OpenAI, UniFi for backfill) live in
  the host's `.env` file (0600, never committed) and are passed to the
  container as environment variables.
- The application never executes shell strings; every subprocess is invoked
  with a list of args (`subprocess.run(["ffmpeg", "-i", ...])`).

## Operating modes

### Active mode

All scheduled jobs and threads running:

| Job | Schedule |
|---|---|
| Snapshot | every 5 min |
| Analyze (snapshots + clips) | every 60 s |
| Daily timelapse | 00:10 |
| Daily summary (final) | 23:55 |
| Daily summary (refresh) | every 3 h |
| Health monitor | every 2 min |

Plus the recorder and motion-detector threads. This is the default — set
`WATCH_ONLY=` (empty or unset).

### Watch-only mode

`WATCH_ONLY=1`. Everything above is paused. Only the daily 12:00 nest check
fires. The dashboard, journal, narrations, clips and species page remain
read-accessible.

Used in the post-season abandoned-clutch phase to keep the historical
record online without burning API quota.

## Trade-offs and decisions

| Decision | Trade-off |
|---|---|
| **Single SQLite file, single process** | No HA, can't horizontally scale. But the workload is one camera and one writer; anything fancier is overkill. |
| **Server-rendered HTML + HTMX** | No client-side richness (no live event stream over WebSocket). But the build pipeline is `pip install` and the codebase has zero JavaScript. |
| **Gemini Flash for primary classification** | Occasional misclassifications (a feather counted as an egg). But mitigated by the critic pass, ground-truth in prompts, and a hand-written failure-mode catalogue. Cost is ~€0.10/day. |
| **XTTS-v2 self-hosted TTS** | Needs a GPU and you bring your own checkpoint with its own licensing. But avoids per-character cloud cost and keeps audio generation under your control. |
| **One-file SQLite, no message broker** | All inter-component coordination is "set a flag in a table." Slightly less elegant than a queue, but completely transparent and trivial to debug. |
| **Provider-agnostic critic** | Three SDKs in `requirements.txt`. But lazy imports mean only the active provider is loaded, and the abstraction is one function. |

## Limitations

- One camera, one box. The schema and prompts hard-code a single subject.
- The motion detector's scene-change threshold needs hand-tuning per camera.
- No automated tests. This is a hobby project; correctness is checked by
  observation. The critic pass and the strip-heading regex catch most
  regressions.
- The narrator is a separate machine, accessed over SSH. There is no
  fallback if the GPU box is offline at 00:30 — that day's narration is
  simply missing until the next run.
- Cloudflare caches static assets aggressively. The CSS is cache-busted via
  `?v=<mtime>`, but the MP3 narrations are not — re-generating one means
  the previous file is served for up to 4 h unless you hard-refresh.

## Future work

- Re-using the same architecture for next year's clutch would require:
  resetting the per-season ground truth in `analyzer.py` (egg dates, hatch
  date), clearing the relevant DB rows, and switching back to active mode.
  The species page, prompts, and pipeline don't need to change.
- A proper "second-pass classifier" that uses the critic provider (a
  stronger model) for ambiguous *per-snapshot* events would catch the
  feather hallucination at source rather than at the summary stage.
- Real metric emission (Prometheus exposition) would beat scraping the
  health endpoint, but it's not warranted for a one-box workload.
