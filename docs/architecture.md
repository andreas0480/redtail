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
│  │  index · clips · journal · timelapse  (HTMX)        │   │
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
daily narrative journal entry on a 3-hour interval and at 23:55.

### `app/timelapse.py`
At 00:10 every night it builds a per-day H.264 timelapse from the previous day's snapshots
using ffmpeg concat demuxer. A cumulative season-wide timelapse is rebuilt after each
nightly run.

### `app/monitor.py`
Checks every 2 min that the RTSP stream is reachable, disk space is adequate, and the DB
is being written to. Fires a push notification via [ntfy](https://ntfy.sh) on any failure.

## Database schema

```
snapshots   (id, captured_at, path, analyzed)
clips       (id, started_at, duration_seconds, path, trigger, analyzed, keep, label, thumbnail_path)
events      (id, occurred_at, source, source_id, event_type, confidence, narrative, raw_json)
daily_summaries (day PK, summary, events_count, timelapse_path, featured_image_path, created_at)
alerts      (id, fired_at, check_name, severity, message)
```

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
      2026-05-08.mp4
    cumulative.mp4
  thumbnails/
    clips/
    timelapses/
/state/
  events.db
  buffer/          ← rolling RTSP segments
```
