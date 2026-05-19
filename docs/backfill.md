# Historical backfill

UniFi Protect retains recorded video for as long as its disk allows (a few
weeks on the typical Cloud Key, longer with attached storage). The
`backfill/` toolchain pulls that history into the Redtail data set: media
files into `/data/{snapshots,clips}/`, rows into the SQLite DB, all idempotent.

## When you'd use this

- **You deployed after nesting started** and want the season's record to
  start at the beginning, not at deploy time.
- **The system was down for a stretch** — host reboot, network outage,
  container crash — and UniFi has the missing window.
- **You want to re-classify** a date range after improving a prompt
  (run backfill with `--skip-snapshots --skip-clips --skip-summaries`,
  reset `analyzed=0` for the snapshots in question, let the analyzer
  re-process).

## Architecture

```
┌──────────────── ullm (development machine, has Python venv) ──────────────┐
│                                                                            │
│  backfill.py  ──┬──► UniFi Protect API (HTTPS, basic auth)                │
│                 │       ├─ /api/cameras                                    │
│                 │       ├─ /api/events?cameras=...&types=motion            │
│                 │       └─ /api/video/export?camera=...&start=...&end=...  │
│                 │                                                          │
│                 ├──► JPEGs → backfill/work_today/snapshots/<day>/...       │
│                 ├──► MP4s  → backfill/work_today/clips/<day>/...           │
│                 └──► SQLite → backfill/work_today/state/events.db          │
│                                                                            │
│  coordinator.py (optional, for parallel Gemini batching of large windows) │
│                                                                            │
│  deploy.py   ─── ssh + scp + docker cp + sqlite merge ───►  .30.103       │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

Backfill writes to a self-contained `work_today/` (or `work/`) directory
that mirrors the production layout. Deploy then streams that into the
running container.

## Phase 1 — fetch

```bash
cd backfill
python -m venv .venv
.venv/bin/pip install -r ../requirements.txt   # reuses the main requirements
cp .env.example .env.local
$EDITOR .env.local
```

Required in `.env.local`:

```env
UNIFI_HOST=<protect-host>
UNIFI_USERNAME=<user>
UNIFI_PASSWORD=<password>
GEMINI_API_KEY=<key>   # only needed if you'll run analysis here
```

Find your camera ID in the Protect web UI under *Camera → Settings →
Manage* (URL contains the ID), or via the API: `GET /api/cameras`.

```bash
.venv/bin/python backfill.py \
  --camera-id <camera-id> \
  --start-ms <epoch-ms-start> \
  --end-ms   <epoch-ms-end> \
  --work-dir backfill/work_today \
  --snapshot-interval 300 \
  --skip-analysis        # analyse later, in production
```

What you get:

- One JPEG every `--snapshot-interval` seconds in the window. Snapshots are
  fetched from UniFi's still-image endpoint (cheap, no decode).
- One MP4 per motion event UniFi recorded. Clips include 2 s pre- and
  post-padding.
- One row per snapshot/clip in `backfill/work_today/state/events.db`,
  with `analyzed=0`.

### Resumability

Every file write is idempotent. Snapshots are skipped if the destination
already exists and is >1 KB. Clips are skipped if >50 KB on disk.
`backfill.py` can be killed and re-run; it picks up where it stopped. The
SQLite inserts use `INSERT OR IGNORE` on the `path` UNIQUE constraint.

Typical throughput from a UniFi cloud key over the LAN:

- Snapshots: 4–6/sec
- Clips: 2–4/sec (transcoding-bound on the cloud key)

A full season backfill is therefore minutes, not hours.

## Phase 2 — analyze (optional, parallel)

For large windows, the in-app analyzer (60 s tick, 8+4 items per tick) is
slow. The coordinator splits the work into JSON batches for parallel
Sonnet/Claude subagent processing:

```bash
.venv/bin/python coordinator.py extract-clip-frames    # one-time prep
.venv/bin/python coordinator.py build-batches          # write JSON manifests

# ... spawn N subagents from your editor of choice, each writes results/<batch>.jsonl ...

.venv/bin/python coordinator.py merge                  # merge JSONL → DB
.venv/bin/python coordinator.py status                 # progress check
```

In the common case (small backfill, ≤500 clips), it's simpler to skip this
and let the production analyzer chew through them at 4/min after deploy.

## Phase 3 — deploy to production

```bash
export REDTAIL_HOST=192.168.x.x
.venv/bin/python deploy.py --work-dir backfill/work_today
```

Available flags:

| Flag | Default | Effect |
|---|---|---|
| `--skip-snapshots` | — | Don't stream JPEGs |
| `--skip-clips` | — | Don't stream MP4s |
| `--skip-merge` | — | Don't merge the SQLite DBs |
| `--skip-timelapse` | — | Don't trigger the cumulative timelapse rebuild |

What deploy does, in order:

1. **Stream snapshots and clips** into the container's `/data/...` via
   `tar | ssh | docker exec tar -x` (no intermediate temp files, no compression
   — gigabit LAN is faster than CPU compression).
2. **scp the backfill DB** to `/tmp/backfill.db` on the host.
3. **`docker cp`** it into the container.
4. **Run an inline merge script** (`python -c` over docker exec) that
   INSERTs each row with `INSERT OR IGNORE` and maps source IDs from the
   backfill DB to the production DB by joining on `path`.
5. **POST `/admin/build-timelapse`** to trigger a cumulative rebuild.

The merge is **idempotent**: re-running deploy never duplicates rows.
Existing snapshots/clips/events/summaries are preserved.

### Important: the `analyzed` flag

`deploy.py` uses the source row's `analyzed` value rather than hardcoding 1.
That means: if you ran `backfill.py` with `--skip-analysis`, the rows
arrive in production with `analyzed=0`, and the production analyzer will
pick them up at 4 clips/min.

This was a regression caught mid-season — see commit `6e754a4`.

## Common pitfalls

- **Wrong tunnel — DNS points elsewhere.** If `cloudflared tunnel route dns`
  silently picks a zone you don't own, the DNS resolves to a non-Cloudflare
  IP and 404s. Verify with `dig redtail.example.com CNAME` after registering.
- **`analyzed=1` arrives without events.** Pre-fix this used to happen when
  deploying without analysis. If you find a clip that's `analyzed=1` with
  no row in `events`, reset it: `UPDATE clips SET analyzed=0 WHERE id=…;`.
- **Camera ID drift.** The same physical camera gets a new ID after a
  Protect factory reset. The backfill clips end up in `/data/clips/<day>/`
  regardless, but their `path` won't match production's expected layout if
  the production app was running against a different camera ID. Always
  pass `--camera-id` explicitly.
