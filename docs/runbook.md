# Operations runbook

Copy-pasteable procedures for the situations I have actually encountered
during a season of running Redtail. Each section starts with the trigger
and ends with a verification step.

All `ssh` commands assume `REDTAIL_HOST` points at the production box; all
in-container Python is shown via `docker exec`.

## Daily

### Watch ntfy for the noon nest-check ping

In watch-only mode you should see one `min`-priority "no change" ping
between 12:00 and 12:05 local. If it goes silent for a day, the container
is probably dead — check `docker ps` and `docker logs redtail`.

### Inspect the latest entries

```bash
ssh $REDTAIL_HOST 'docker exec redtail sqlite3 -header /state/events.db \
  "SELECT day, substr(summary,1,80) FROM daily_summaries ORDER BY day DESC LIMIT 5"'
```

(works for any SQLite-installed container — for ours `sqlite3` is in the
base image only sometimes, use the `python3 -c` fallback if not present:)

```bash
ssh $REDTAIL_HOST 'docker exec redtail python3 -c "
import sqlite3
c = sqlite3.connect(\"/state/events.db\")
for d, s in c.execute(\"SELECT day, substr(summary,1,80) FROM daily_summaries ORDER BY day DESC LIMIT 5\"):
    print(d, s)
"'
```

## Mode switches

### Switch to watch-only mode (end-of-season)

```bash
ssh $REDTAIL_HOST 'echo WATCH_ONLY=1 >> /home/belitz/redtail/.env'
ssh $REDTAIL_HOST 'docker compose --project-directory /home/belitz/redtail up -d'
```

The container restarts with only the noon nest check scheduled. You'll
get a ntfy ping confirming "online — watch-only mode" within seconds.

### Resume active mode (next clutch starts)

```bash
ssh $REDTAIL_HOST "sed -i '/^WATCH_ONLY=/d' /home/belitz/redtail/.env"
ssh $REDTAIL_HOST 'docker compose --project-directory /home/belitz/redtail up -d'
```

Update the per-season ground truth in `app/analyzer.py`:

- `FIRST_EGG_DATE`
- `EARLIEST_HATCH_DATE`
- Any per-day egg-count schedule in `DAILY_PROMPT_TEMPLATE`
- The hard-coded "currently five eggs" / abandonment date in `CRITIC_PROMPT`

Rebuild and deploy:

```bash
docker compose --project-directory /home/belitz/redtail build
docker compose --project-directory /home/belitz/redtail up -d
```

## AI

### Re-run analysis on a clip that came out wrong

If a clip got an obviously-wrong `event_type` or `label`:

```bash
ssh $REDTAIL_HOST 'docker exec redtail python3 -c "
import sqlite3
c = sqlite3.connect(\"/state/events.db\")
# reset clip 743 — the analyzer will pick it up on next tick
c.execute(\"UPDATE clips SET analyzed=0, label=NULL, thumbnail_path=NULL WHERE id=743\")
c.execute(\"DELETE FROM events WHERE source=\\\"clip\\\" AND source_id=743\")
c.commit()
"'
```

The 60-second analyzer interval will re-process it next time it fires.

### Regenerate a daily summary

If a summary needs a fresh take (new ground truth in the prompt, etc.):

```bash
ssh $REDTAIL_HOST 'docker exec -i redtail python3' << 'PYEOF'
import sys, os, sqlite3
sys.path.insert(0, "/app")
os.environ.setdefault("DATA_DIR", "/data"); os.environ.setdefault("STATE_DIR", "/state")
os.environ.setdefault("RTSP_URL", "rtsps://placeholder")
from app.config import load_config
from app.db import Database
from app.analyzer import Analyzer

cfg = load_config()
db = Database(cfg.db_path)
analyzer = Analyzer(cfg, db)
day = "2026-05-13"

conn = sqlite3.connect(cfg.db_path)
conn.execute("DELETE FROM daily_summaries WHERE day=?", (day,))
conn.commit(); conn.close()

analyzer.daily_summary(day)
PYEOF
```

### Regenerate all daily summaries (batch)

Use case: changed the `CRITIC_PROMPT` or `DAILY_PROMPT_TEMPLATE` materially
and want to re-render the season. **Wipes** all current summaries and
recreates them.

```bash
ssh $REDTAIL_HOST 'docker exec -i redtail python3' << 'PYEOF'
import sys, os, sqlite3
sys.path.insert(0, "/app")
os.environ.setdefault("DATA_DIR", "/data"); os.environ.setdefault("STATE_DIR", "/state")
os.environ.setdefault("RTSP_URL", "rtsps://placeholder")
from app.config import load_config
from app.db import Database
from app.analyzer import Analyzer
cfg = load_config(); db = Database(cfg.db_path); analyzer = Analyzer(cfg, db)

# get all days with events
conn = sqlite3.connect(cfg.db_path)
days = [r[0] for r in conn.execute(
    "SELECT DISTINCT date(occurred_at) FROM events ORDER BY 1"
).fetchall()]
conn.execute("DELETE FROM daily_summaries")
conn.commit(); conn.close()

for day in days:
    print(day, "...", end=" ", flush=True)
    r = analyzer.daily_summary(day)
    print("OK" if r else "FAIL")
PYEOF
```

Expect ~3 seconds per day at Gemini Flash speed.

### Apply a manual correction to a summary

Sometimes a summary needs a hand edit (the AI got a subtle thing wrong and
no prompt change is justified). Edit it directly:

```bash
ssh $REDTAIL_HOST 'docker exec -i redtail python3' << 'PYEOF'
import sqlite3
c = sqlite3.connect("/state/events.db")
c.execute("UPDATE daily_summaries SET summary=? WHERE day=?",
          ("New corrected summary text here.", "2026-05-13"))
c.commit()
PYEOF
```

Then re-narrate that day (next section).

## Narrator

### Generate any missing narrations

The narrator runs at 00:30 daily. To force a run now:

```bash
cd ~/redtail/narrator
COQUI_TOS_AGREED=1 REDTAIL_HOST=$REDTAIL_HOST .venv/bin/python narrate.py
```

### Narrate a specific day (or re-narrate after edits)

```bash
cd ~/redtail/narrator
COQUI_TOS_AGREED=1 REDTAIL_HOST=$REDTAIL_HOST .venv/bin/python narrate.py --day 2026-05-13
```

The MP3 is overwritten in place; the DB path stays the same. **Cloudflare
caches MP3 files** — if you don't see the new audio after a re-narrate,
either wait 4 h for the cache to expire or hard-refresh the journal page.

## Backfill

### Backfill UniFi Protect clips for a gap

When the system was down for a window and UniFi has the recording history.

```bash
cd ~/redtail/backfill

# find the gap
LAST_MS=$(ssh $REDTAIL_HOST 'docker exec redtail python3 -c "
import sqlite3
from datetime import datetime
c = sqlite3.connect(\"/state/events.db\")
r = c.execute(\"SELECT started_at FROM clips ORDER BY started_at DESC LIMIT 1\").fetchone()
print(int(datetime.fromisoformat(r[0]).timestamp() * 1000))
"')
NOW_MS=$(python3 -c "import time; print(int(time.time()*1000))")

.venv/bin/python backfill.py \
  --camera-id <camera-id> \
  --start-ms "$LAST_MS" --end-ms "$NOW_MS" \
  --work-dir /tmp/redtail_gap \
  --skip-snapshots --skip-analysis --skip-summaries

REDTAIL_HOST=$REDTAIL_HOST .venv/bin/python deploy.py \
  --work-dir /tmp/redtail_gap \
  --skip-snapshots --skip-timelapse
```

The deploy step merges clips with `INSERT OR IGNORE` so re-running is safe.
The live analyzer will pick them up — they ship with `analyzed=0` since the
`deploy.py` fix in commit `6e754a4`.

## Troubleshooting

### No motion clips appearing despite obvious activity

The most likely culprit is a prompt/regex mismatch with the running ffmpeg
version. Check:

```bash
ssh $REDTAIL_HOST 'docker exec redtail sh -c "
  timeout 15 ffmpeg -nostdin -hide_banner -loglevel info \
    -rtsp_transport tcp -i \"\$RTSP_URL\" \
    -vf \"fps=2,scale=320:-2,select=gte(scene\,0.005),metadata=print\" \
    -f null - 2>&1 | grep lavfi.scene_score | head -5
"'
```

You should see lines like `lavfi.scene_score=0.0XX` once every few seconds.
If you see them but no clips appear in the DB, the threshold is too high
for the actual motion in your scene; lower `MOTION_SCENE_THRESHOLD` in
`.env` and restart.

### Container keeps restarting

```bash
ssh $REDTAIL_HOST 'docker logs redtail --tail 100'
```

Common causes:

- Missing required env var (`RTSP_URL`, `GEMINI_API_KEY`) — startup throws
  on `load_config()`.
- RTSP host unreachable for >5 s — the recorder ffmpeg exits, the supervisor
  retries; usually only a problem if the UniFi system is genuinely down.
- Disk full — SQLite WAL can't write. Free space, then restart.

### CSS / template changes not showing up

Cloudflare caches static assets for 4 hours. The CSS is automatically
cache-busted via `style.css?v=<mtime>`, but other static files (JS, audio,
images) are not. After a deploy:

```bash
ssh $REDTAIL_HOST 'curl -sI https://redtail.example.com/static/style.css | grep cf-cache-status'
```

`cf-cache-status: HIT` means you're seeing the old version. Either wait,
purge in the Cloudflare dashboard, or hard-refresh.

### Health endpoint returns 503

```bash
curl http://localhost:8765/health | jq
```

Each failing check is annotated. Most common:

- **`rtsp` failure** — UniFi rebooted, network blip; clears on its own
  within minutes.
- **`disk` failure** — `/data` filling up. Clear old snapshots/clips:
  ```bash
  ssh $REDTAIL_HOST 'docker exec redtail find /data/snapshots -name "*.jpg" -mtime +60 -delete'
  ```
- **`recorder` failure** — segment buffer hasn't been written to recently.
  Almost always paired with an `rtsp` failure.
- **`db` failure** — capture isn't running. Check container logs for the
  snapshot job; it may be erroring on a permission issue.
