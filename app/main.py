import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .analyzer import Analyzer
from .capture import capture_snapshot
from .config import load_config
from .db import Database
from .monitor import HealthMonitor, send_ntfy
from .motion import MotionDetector
from .recorder import SegmentRecorder
from .timelapse import TimelapseBuilder
from .util import ensure_dir, now_iso, setup_logging, today_str

log = logging.getLogger("redtail")

cfg = load_config()
setup_logging(cfg.log_level)

for p in (cfg.snapshots_dir, cfg.clips_dir, cfg.timelapses_dir, cfg.thumbnails_dir, cfg.buffer_dir, cfg.live_dir):
    ensure_dir(p)

db = Database(cfg.db_path)
recorder = SegmentRecorder(cfg)
motion = MotionDetector(cfg, db, recorder)
analyzer = Analyzer(cfg, db)
timelapse = TimelapseBuilder(cfg, db)
monitor = HealthMonitor(cfg, db, recorder)

scheduler = BackgroundScheduler(timezone=cfg.tz)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Cache-buster derived from style.css mtime, exposed to every template
try:
    _ASSET_VERSION = str(int((STATIC_DIR / "style.css").stat().st_mtime))
except OSError:
    _ASSET_VERSION = "1"
templates.env.globals["asset_v"] = _ASSET_VERSION


def _job_snapshot():
    capture_snapshot(cfg, db)


def _job_analyze():
    analyzer.process_pending()


def _job_daily_timelapse():
    timelapse.build_all_missing_daily()
    timelapse.build_cumulative()


def _job_daily_summary():
    analyzer.daily_summary(today_str())


def _job_monitor():
    monitor.run_once()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("redtail starting up")
    recorder.start()
    motion.start()
    monitor.start()

    scheduler.add_job(_job_snapshot, IntervalTrigger(seconds=cfg.snapshot_interval_seconds), id="snapshot", max_instances=1, coalesce=True)
    scheduler.add_job(_job_analyze, IntervalTrigger(seconds=60), id="analyze", max_instances=1, coalesce=True)
    scheduler.add_job(_job_daily_timelapse, CronTrigger(hour=0, minute=10), id="timelapse_daily")
    scheduler.add_job(_job_daily_summary, CronTrigger(hour=23, minute=55), id="daily_summary")
    scheduler.add_job(_job_daily_summary, IntervalTrigger(hours=3), id="daily_summary_live", max_instances=1, coalesce=True)
    scheduler.add_job(_job_monitor, IntervalTrigger(seconds=120), id="monitor", max_instances=1, coalesce=True)
    scheduler.start()

    # Fire-and-forget startup ping (best effort; ignore if offline)
    if cfg.ntfy_topic:
        send_ntfy(cfg, title="Redtail • online", message=f"Service started on {os.uname().nodename}", priority="low", tags=["white_check_mark"])

    yield

    log.info("redtail shutting down")
    scheduler.shutdown(wait=False)
    motion.stop()
    recorder.stop()
    monitor.stop()


app = FastAPI(title="Redtail Nest Box", lifespan=lifespan)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- dashboard pages ----------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    latest = db.latest_snapshot()
    latest_snapshot_url = None
    if latest:
        latest_snapshot_url = f"/media/snapshots/{Path(latest['path']).relative_to(cfg.snapshots_dir).as_posix()}"
    events = db.recent_events(limit=30)
    
    # Get today's summary for the "notable" section
    today = today_str()
    with db.connect() as c:
        row = c.execute("SELECT summary FROM daily_summaries WHERE day = ?", (today,)).fetchone()
        notable_summary = row["summary"] if row else None

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "latest_snapshot_url": latest_snapshot_url,
            "latest_captured_at": latest["captured_at"] if latest else None,
            "events": events,
            "notable_summary": notable_summary,
            "ntfy_topic": cfg.ntfy_topic,
            "snapshot_interval": cfg.snapshot_interval_seconds,
        },
    )


@app.get("/timelapse", response_class=HTMLResponse)
async def timelapse_page(request: Request):
    cumulative = cfg.timelapses_dir / "cumulative.mp4"
    cumulative_thumb = cfg.thumbnails_dir / "timelapses" / "cumulative.jpg"
    return templates.TemplateResponse(
        "timelapse.html",
        {
            "request": request,
            "cumulative_url": "/media/timelapses/cumulative.mp4" if cumulative.exists() else None,
            "cumulative_thumbnail_url": "/media/thumbnails/timelapses/cumulative.jpg" if cumulative_thumb.exists() else None,
        },
    )


CLIP_PERIODS = [
    ("Night",     "00:00–06:00",  0,  6),
    ("Morning",   "06:00–12:00",  6, 12),
    ("Afternoon", "12:00–18:00", 12, 18),
    ("Evening",   "18:00–24:00", 18, 24),
]


def _period_for(hour: int) -> str:
    for name, _range, start, end in CLIP_PERIODS:
        if start <= hour < end:
            return name
    return "Evening"


@app.get("/clips", response_class=HTMLResponse)
async def clips_page(request: Request):
    grouped: dict[str, dict] = {}
    local_tz = ZoneInfo(cfg.tz)

    with db.connect() as c:
        rows = c.execute("""
            SELECT c.*, e.narrative
            FROM clips c
            LEFT JOIN events e ON e.source = 'clip' AND e.source_id = c.id
            WHERE c.keep = 1
            ORDER BY c.started_at DESC
            LIMIT 1000
        """).fetchall()

    for row in rows:
        path = Path(row["path"])
        try:
            rel = path.relative_to(cfg.clips_dir).as_posix()
        except ValueError:
            continue

        try:
            dt_utc = datetime.fromisoformat(row["started_at"])
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=ZoneInfo("UTC"))
            dt_local = dt_utc.astimezone(local_tz)
        except (ValueError, TypeError):
            continue

        day_key = dt_local.strftime("%Y-%m-%d")
        period = _period_for(dt_local.hour)

        if day_key not in grouped:
            grouped[day_key] = {
                "date_display": dt_local.strftime("%-d %b %Y"),
                "periods": {p[0]: [] for p in CLIP_PERIODS},
            }

        thumb_url = None
        if row["thumbnail_path"]:
            try:
                t_path = Path(row["thumbnail_path"])
                t_rel = t_path.relative_to(cfg.thumbnails_dir).as_posix()
                thumb_url = f"/media/thumbnails/{t_rel}"
            except ValueError:
                pass

        grouped[day_key]["periods"][period].append({
            "id": row["id"],
            "started_at": row["started_at"],
            "local_date": dt_local.strftime("%-d %b %Y"),
            "local_time": dt_local.strftime("%H:%M:%S"),
            "duration": row["duration_seconds"],
            "label": row["label"] or "(unlabeled)",
            "narrative": row["narrative"] or "",
            "url": f"/media/clips/{rel}",
            "thumbnail_url": thumb_url,
            "trigger": row["trigger"],
        })

    sorted_days = sorted(grouped.keys(), reverse=True)
    # Auto-open the period of the most recent clip in the most recent day
    auto_open = None
    if sorted_days:
        latest = grouped[sorted_days[0]]["periods"]
        for name, _, _, _ in reversed(CLIP_PERIODS):  # newest first
            if latest[name]:
                auto_open = (sorted_days[0], name)
                break

    return templates.TemplateResponse(
        "clips.html",
        {
            "request": request,
            "sorted_days": sorted_days,
            "grouped_clips": grouped,
            "periods": CLIP_PERIODS,
            "auto_open": auto_open,
        },
    )


@app.get("/journal", response_class=HTMLResponse)
async def journal_page(request: Request):
    summaries = []
    for row in db.daily_summaries(limit=60):
        img_url = None
        if row["featured_image_path"]:
            try:
                path = Path(row["featured_image_path"])
                rel = path.relative_to(cfg.snapshots_dir).as_posix()
                img_url = f"/media/snapshots/{rel}"
            except ValueError:
                pass

        tl_path = cfg.timelapses_dir / "daily" / f"{row['day']}.mp4"
        tl_url = f"/media/timelapses/daily/{row['day']}.mp4" if tl_path.exists() else None

        summaries.append({
            "day": row["day"],
            "summary": row["summary"],
            "events_count": row["events_count"],
            "featured_image_url": img_url,
            "timelapse_url": tl_url,
            "bio_context": row["bio_context"],
        })

    return templates.TemplateResponse(
        "journal.html",
        {"request": request, "summaries": summaries},
    )


# ---------- HTMX fragments ----------


@app.get("/species", response_class=HTMLResponse)
async def species_page(request: Request):
    return templates.TemplateResponse("species.html", {"request": request})


@app.get("/fragments/events", response_class=HTMLResponse)
async def fragment_events(request: Request):
    events = db.recent_events(limit=30)
    return templates.TemplateResponse("_events_list.html", {"request": request, "events": events})


@app.get("/fragments/latest", response_class=HTMLResponse)
async def fragment_latest(request: Request):
    latest = db.latest_snapshot()
    latest_snapshot_url = None
    if latest:
        latest_snapshot_url = f"/media/snapshots/{Path(latest['path']).relative_to(cfg.snapshots_dir).as_posix()}?t={int(datetime.now().timestamp())}"
    return templates.TemplateResponse(
        "_latest.html",
        {
            "request": request,
            "latest_snapshot_url": latest_snapshot_url,
            "latest_captured_at": latest["captured_at"] if latest else None,
        },
    )


@app.get("/fragments/notable", response_class=HTMLResponse)
async def fragment_notable(request: Request):
    today = today_str()
    with db.connect() as c:
        row = c.execute("SELECT summary FROM daily_summaries WHERE day = ?", (today,)).fetchone()
        notable_summary = row["summary"] if row else None
    return templates.TemplateResponse("_notable.html", {"request": request, "notable_summary": notable_summary})


# ---------- media + health ----------


def _safe_media(base: Path, rel: str) -> Path:
    candidate = (base / rel).resolve()
    base_resolved = base.resolve()
    if not str(candidate).startswith(str(base_resolved)):
        raise HTTPException(status_code=400, detail="bad path")
    if not candidate.exists():
        raise HTTPException(status_code=404)
    return candidate


@app.get("/media/snapshots/{path:path}")
async def media_snapshot(path: str):
    return FileResponse(_safe_media(cfg.snapshots_dir, path))


@app.get("/media/clips/{path:path}")
async def media_clip(path: str):
    return FileResponse(_safe_media(cfg.clips_dir, path))


@app.get("/media/timelapses/{path:path}")
async def media_timelapse(path: str):
    return FileResponse(_safe_media(cfg.timelapses_dir, path))


@app.get("/media/thumbnails/{path:path}")
async def media_thumbnail(path: str):
    return FileResponse(_safe_media(cfg.thumbnails_dir, path))


@app.get("/health/quick")
async def health_quick():
    return {"ok": True}


@app.get("/health")
async def health():
    results = monitor.last_results or monitor.run_once()
    payload = {
        "ok": all(r.ok for r in results),
        "checked_at": monitor.last_run,
        "checks": [
            {"name": r.name, "ok": r.ok, "severity": r.severity, "detail": r.detail}
            for r in results
        ],
    }
    status = 200 if payload["ok"] else 503
    return JSONResponse(payload, status_code=status)


@app.post("/admin/test-alert")
async def admin_test_alert():
    ok = send_ntfy(cfg, title="Redtail • test", message="manual test from dashboard", priority="default", tags=["bell"])
    return {"sent": ok}


@app.post("/admin/build-timelapse")
async def admin_build_timelapse():
    out = timelapse.build_cumulative()
    return {"path": str(out) if out else None}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=cfg.dashboard_port, log_level=cfg.log_level.lower())
