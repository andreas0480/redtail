import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    analyzed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_captured ON snapshots(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_pending ON snapshots(analyzed) WHERE analyzed = 0;

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    path TEXT NOT NULL UNIQUE,
    trigger TEXT NOT NULL,
    analyzed INTEGER NOT NULL DEFAULT 0,
    keep INTEGER NOT NULL DEFAULT 1,
    label TEXT,
    thumbnail_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_clips_started ON clips(started_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL,            -- 'snapshot' or 'clip'
    source_id INTEGER,
    event_type TEXT NOT NULL,        -- e.g. adult_present, feeding, eggs_visible, chicks_visible, empty, unknown
    confidence REAL,
    narrative TEXT NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, occurred_at DESC);

CREATE TABLE IF NOT EXISTS daily_summaries (
    day TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    events_count INTEGER NOT NULL DEFAULT 0,
    timelapse_path TEXT,
    featured_image_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at TEXT NOT NULL,
    check_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_fired ON alerts(fired_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as c:
            c.executescript(_SCHEMA)
            c.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
            finally:
                conn.close()

    def record_snapshot(self, captured_at: str, path: str) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO snapshots (captured_at, path) VALUES (?, ?)",
                (captured_at, path),
            )
            return cur.lastrowid or 0

    def record_clip(self, started_at: str, duration: float, path: str, trigger: str) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO clips (started_at, duration_seconds, path, trigger) VALUES (?, ?, ?, ?)",
                (started_at, duration, path, trigger),
            )
            return cur.lastrowid or 0

    def pending_snapshots(self, limit: int = 8) -> list[sqlite3.Row]:
        with self.connect() as c:
            return list(
                c.execute(
                    "SELECT * FROM snapshots WHERE analyzed = 0 ORDER BY captured_at ASC LIMIT ?",
                    (limit,),
                )
            )

    def pending_clips(self, limit: int = 4) -> list[sqlite3.Row]:
        with self.connect() as c:
            return list(
                c.execute(
                    "SELECT * FROM clips WHERE analyzed = 0 ORDER BY started_at ASC LIMIT ?",
                    (limit,),
                )
            )

    def mark_snapshot_analyzed(self, snapshot_id: int) -> None:
        with self.connect() as c:
            c.execute("UPDATE snapshots SET analyzed = 1 WHERE id = ?", (snapshot_id,))

    def mark_clip_analyzed(self, clip_id: int, keep: bool, label: Optional[str], thumbnail_path: Optional[str] = None) -> None:
        with self.connect() as c:
            c.execute(
                "UPDATE clips SET analyzed = 1, keep = ?, label = ?, thumbnail_path = ? WHERE id = ?",
                (1 if keep else 0, label, thumbnail_path, clip_id),
            )

    def add_event(
        self,
        occurred_at: str,
        source: str,
        source_id: Optional[int],
        event_type: str,
        confidence: Optional[float],
        narrative: str,
        raw_json: Optional[str],
    ) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO events (occurred_at, source, source_id, event_type, confidence, narrative, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (occurred_at, source, source_id, event_type, confidence, narrative, raw_json),
            )
            return cur.lastrowid or 0

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as c:
            return list(
                c.execute(
                    "SELECT * FROM events ORDER BY occurred_at DESC LIMIT ?",
                    (limit,),
                )
            )

    def events_for_day(self, day: str) -> list[sqlite3.Row]:
        with self.connect() as c:
            return list(
                c.execute(
                    "SELECT * FROM events WHERE date(occurred_at) = ? ORDER BY occurred_at ASC",
                    (day,),
                )
            )

    def list_clips(self, limit: int = 100, only_keep: bool = True) -> list[sqlite3.Row]:
        with self.connect() as c:
            q = "SELECT * FROM clips WHERE 1=1"
            if only_keep:
                q += " AND keep = 1"
            q += " ORDER BY started_at DESC LIMIT ?"
            return list(c.execute(q, (limit,)))

    def latest_snapshot(self) -> Optional[sqlite3.Row]:
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
            return row

    def upsert_daily_summary(
        self, day: str, summary: str, events_count: int, timelapse_path: Optional[str], featured_image_path: Optional[str], created_at: str
    ) -> None:
        with self.connect() as c:
            c.execute(
                """INSERT INTO daily_summaries (day, summary, events_count, timelapse_path, featured_image_path, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(day) DO UPDATE SET
                       summary=excluded.summary,
                       events_count=excluded.events_count,
                       timelapse_path=excluded.timelapse_path,
                       featured_image_path=excluded.featured_image_path,
                       created_at=excluded.created_at""",
                (day, summary, events_count, timelapse_path, featured_image_path, created_at),
            )

    def daily_summaries(self, limit: int = 30) -> list[sqlite3.Row]:
        with self.connect() as c:
            return list(c.execute("SELECT * FROM daily_summaries ORDER BY day DESC LIMIT ?", (limit,)))

    def record_alert(self, fired_at: str, check_name: str, severity: str, message: str) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO alerts (fired_at, check_name, severity, message) VALUES (?, ?, ?, ?)",
                (fired_at, check_name, severity, message),
            )

    def recent_alerts(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as c:
            return list(c.execute("SELECT * FROM alerts ORDER BY fired_at DESC LIMIT ?", (limit,)))
