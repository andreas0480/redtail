# Historical Backfill

The `backfill/` directory contains tools for importing historical footage from UniFi
Protect's cloud storage into the redtail dataset. This is useful when you deploy the
system after nesting has already started.

## Prerequisites

```bash
cd backfill
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # uses root requirements.txt
```

Create `backfill/.env.local`:

```
UNIFI_HOST=<protect-host>
UNIFI_USERNAME=<user>
UNIFI_PASSWORD=<password>
GEMINI_API_KEY=<key>
```

## Workflow

### 1. Fetch snapshots and clips

```bash
python backfill.py \
  --camera-id <camera-id> \
  --start-ms <epoch-ms> \
  --end-ms   <epoch-ms> \
  --snapshot-interval 300 \
  --skip-analysis          # do analysis in step 2
```

This downloads snapshots every 5 min and all motion clips for the date range into
`backfill/work/`. It is fully **resumable** — already-downloaded files are skipped.

Find your camera ID in the UniFi Protect UI under Camera → Settings, or by calling the
Protect API at `https://<host>/proxy/protect/api/cameras`.

### 2. Batch analysis (optional, parallel)

For large date ranges the built-in analyzer is slow. The coordinator lets you process
analysis batches with parallel subagents:

```bash
# Extract clip frames (one-time)
python coordinator.py extract-clip-frames

# Write batch manifests
python coordinator.py build-batches

# ... spawn subagents to process batches/snap_NNNN.json and write results/snap_NNNN.jsonl ...

# Merge results into local DB
python coordinator.py merge

# Check progress
python coordinator.py status
```

### 3. Deploy to production

```bash
export REDTAIL_HOST=<your-host>

python deploy.py \
  --work-dir backfill/work

# Individual phases can be skipped:
# --skip-snapshots  --skip-clips  --skip-merge  --skip-timelapse
```

`deploy.py` streams the media files into the running container over SSH + docker and
merges the SQLite rows with `INSERT OR IGNORE` (safe to run multiple times).

## Notes

- The backfill DB lives at `backfill/work/state/events.db` and uses the same schema as
  the production DB.
- Snapshot timestamps are aligned to whole multiples of `--snapshot-interval` so they
  interleave cleanly with live snapshots.
- Motion clips include a 2 s pre- and post-padding around the event window.
