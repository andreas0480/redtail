import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    rtsp_url: str
    dashboard_port: int
    tz: str

    data_dir: Path
    snapshots_dir: Path
    clips_dir: Path
    timelapses_dir: Path
    thumbnails_dir: Path
    buffer_dir: Path
    live_dir: Path
    db_path: Path

    gemini_api_key: str
    gemini_model: str

    ntfy_server: str
    ntfy_topic: str

    snapshot_interval_seconds: int
    buffer_segment_seconds: int
    buffer_keep_segments: int
    motion_scene_threshold: float
    motion_cooldown_seconds: int

    log_level: str


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v else default


def load_config() -> Config:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    state_dir = Path(os.environ.get("STATE_DIR", "/state"))
    return Config(
        rtsp_url=os.environ["RTSP_URL"],
        dashboard_port=_int("DASHBOARD_PORT", 8765),
        tz=os.environ.get("TZ", "UTC"),
        data_dir=data_dir,
        snapshots_dir=data_dir / "snapshots",
        clips_dir=data_dir / "clips",
        timelapses_dir=data_dir / "timelapses",
        thumbnails_dir=data_dir / "thumbnails",
        buffer_dir=state_dir / "buffer",
        live_dir=state_dir / "live",
        db_path=state_dir / "events.db",
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
        snapshot_interval_seconds=_int("SNAPSHOT_INTERVAL_SECONDS", 300),
        buffer_segment_seconds=_int("BUFFER_SEGMENT_SECONDS", 30),
        buffer_keep_segments=_int("BUFFER_KEEP_SEGMENTS", 20),
        motion_scene_threshold=_float("MOTION_SCENE_THRESHOLD", 0.06),
        motion_cooldown_seconds=_int("MOTION_COOLDOWN_SECONDS", 120),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
