# Architecture

## Overview

```
┌─────────────────────────────────────────────────────────────┐
│                      Docker container                        │
│                                                             │
│  ┌──────────────┐   RTSP   ┌──────────────────────────────┐ │
│  │   capture.py  │◄────────│  UniFi Protect / IP camera   │ │
│  │  (snapshots)  │         └──────────────────────────────┘ │
│  └──────┬───────┘                                           │
│         │ JPEG                                              │
│  ┌──────▼───────┐   ┌────────────────┐                     │
│  │  recorder.py  │   │   motion.py    │                     │
│  │  (RTSP buf)   │──►│ (frame diff)   │                     │
│  └──────────────┘   └───────┬────────┘                     │
│                             │ clip saved                    │
│  ┌──────────────────────────▼──────────────────────────┐   │
│  │                    events.db  (SQLite)               │   │
│  │  snapshots · clips · events · daily_summaries        │   │
│  └──────────────────────────┬──────────────────────────┘   │
│                             │                               │
│  ┌──────────────────────────▼──────────────────────────┐   │
│  │                   analyzer.py                        │   │
│  │   Gemini 2.5 Flash vision → event_type + narrative   │   │
│  └──────────────────────────┬──────────────────────────┘   │
│                             │                               │
│  ┌──────────────────────────▼──────────────────────────┐   │
│  │              FastAPI dashboard  (port 8765)          │   │
│  │  index · clips · journal · timelapse · species       │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## Key components

### `app/capture.py`
Fires once per `SNAPSHOT_INTERVAL_SECONDS` (default: 5 min). Grabs a single JPEG from
the RTSP stream via ffmpeg and inserts a row into the `snapshots` table.

### `app/recorder.py` + `app/motion.py`
`recorder.py` continuously segments the RTSP stream into short MP4 buffers (default: 30 s
segments, 10 min of pre-roll). `motion.py` compares consecutive snapshots; when the
frame-diff exceeds `MOTION_SCENE_THRESHOLD` it copies the relevant buffer segments into
a motion clip and inserts a row into the `clips` table.

### `app/analyzer.py`
Runs every 60 s and picks up unanalyzed snapshots and clips. For each one it sends frames
to the Gemini vision API with a species-specific prompt and stores the result
(`event_type`, `confidence`, `narrative`) in the `events` table. Also generates the
daily narrative journal entry every 3 hours and at 23:55, plus a `bio_context` paragraph
(2–3 sentences of species biology relevant to that day's events).

### `app/timelapse.py`
At 00:10 every night it builds a per-day H.264 timelapse from the previous day's snapshots
using the ffmpeg concat demuxer, targeting **~30 seconds** regardless of frame count (frame
duration = 30 / frame_count, clamped to 2–25 fps). A cumulative season-wide timelapse is
rebuilt after each nightly run.

### `app/monitor.py`
Checks every 2 min that the RTSP stream is reachable, disk space is adequate, and the DB
is being written to. Fires a push notification via [ntfy](https://ntfy.sh) on any failure.

## Database schema

```
snapshots       (id, captured_at, path, analyzed)
clips           (id, started_at, duration_seconds, path, trigger, analyzed, keep, label, thumbnail_path)
events          (id, occurred_at, source, source_id, event_type, confidence, narrative, raw_json)
daily_summaries (day PK, summary, events_count, timelapse_path, featured_image_path, bio_context, created_at)
alerts          (id, fired_at, check_name, severity, message)
```

`bio_context` is a Gemini-generated 2–3 sentence paragraph explaining the species biology
relevant to that day's observed events (e.g. incubation physiology on a day of long sitting
bouts; egg-laying interval on a day a new egg appeared). Added via live migration if the
column is absent on startup.

## AI event types

| event_type | When used |
|---|---|
| `empty` | No adult and no visible eggs |
| `adult_present` | Adult visible but not sitting on nest |
| `adult_arrives` | Bird flies in (clips only) |
| `adult_leaves` | Bird flies out (clips only) |
| `eggs_visible` | Eggs clearly visible with no adult covering |
| `incubating` | Adult sitting on nest (eggs hidden) |
| `feeding` | Adult delivering food to chicks |
| `chicks_visible` | Nestlings visible |
| `chick_hatching` | Egg breaking open |
| `intruder` | Non-target species |
| `unknown` | Image too poor to classify |

## Media layout

```
/data/
  snapshots/
    2026-05-08/    ← one directory per day
      073000.jpg
      073500.jpg
      ...
  clips/
    2026-05-08/
      073012.mp4
      ...
  timelapses/
    daily/
      2026-05-08.mp4    ← ~30 s per day
    cumulative.mp4      ← full season
  thumbnails/
    clips/
    timelapses/
      cumulative.jpg    ← mid-season frame used as poster
      2026-05-08.jpg    ← first frame of each daily timelapse
/state/
  events.db
  buffer/          ← rolling RTSP segments

app/static/species/    ← locally-served CC images for the species page
  male_thkraft.jpg
  male_perched.jpg
  female.jpg
  eggs.jpg
  chicks_2d.jpg
  chicks_10d.jpg
```
